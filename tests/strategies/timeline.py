"""Generated Audit_Record sequences, and the inputs a Case_Timeline projection takes.

The subject of P56 through P58 is a *projection over a sequence of records*, so the generator has to
produce sequences rather than one record at a time, and the interesting variation is in three
dimensions at once:

**Which stages are witnessed.** A case at ``DETECTED`` has one record; a recovered case has ten or
more. P57 is a claim about every stage of every shape — *every ``DONE`` stage has its completing
record, every stage without one is ``UPCOMING`` or ``SKIPPED`` with a recorded reason* — so a
generator producing only complete histories would assert the interesting half of the property over
zero inputs. :func:`audit_sequences` therefore draws a *subset* of the event types, and the empty
subset is reachable.

**The order they arrive in.** The router reads in ascending sequence order and the projection sorts
anyway, and those two facts together are exactly the sort of redundancy that rots. So the generator
shuffles: a projection whose output depended on receiving records in order would fail P57 here, on
the shuffled draw, which is the point.

**Whether the sequence is whole.** ``with_gap`` is the third dimension and is a three-valued switch
rather than a boolean flag — see :func:`audit_sequences`. A gap is not ordinary wear: numbers are
allocated by incrementing ``recovery_case.audit_seq`` with ``RETURNING`` inside the same transaction
as the insert, under the case row's ``FOR UPDATE`` lock, and a rolled-back transaction rolls the
increment back too. So a gap means the allocation mechanism was bypassed, and R26.C11's banner
exists to say so. Generating gapped sequences is generating a state the writers cannot produce,
deliberately, because the reader has to survive it honestly.

**No money is generated here.** Every currency figure in a
:class:`~revora.timeline.stages.FigureView` is the server's pre-formatted string, so the generator
draws a formatted-looking string and the timeline has nothing to do arithmetic on — which is the
same guarantee the production path has, held in the tests by the same means rather than by a
different one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import strategies as st

from revora.audit import events
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    DiagnosisEvidenceSource,
    DiagnosisMethod,
    OutcomeClass,
    PolicyCheck,
    PolicyVerdict,
    RiskCause,
    SelectionReason,
    TerminalReason,
)
from revora.domain.transitions import TERMINAL_STATES
from revora.timeline.stages import (
    AuditRecordView,
    CaseView,
    FigureView,
    IntentView,
    SignalView,
)

__all__ = [
    "COMPLETING_EVENT_TYPES",
    "SKIP_EVENT_TYPES",
    "TIMELINE_EVENT_TYPES",
    "audit_sequences",
    "case_views",
    "figure_views",
    "intent_views",
    "signal_views",
    "timeline_inputs",
]

_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)
"""A fixed instant, so a generated timeline is reproducible.

Fixed rather than drawn, because no property here is about *when* anything happened — the instants
are presented verbatim from the records and never compared against a clock, so drawing them would
add search space that no assertion reads. The one instant a property does care about is ``now``, and
:func:`case_views` draws ``next_review_at`` around it explicitly."""

COMPLETING_EVENT_TYPES: tuple[str, ...] = (
    events.CASE_DETECTED,
    events.DIAGNOSIS_RECORDED,
    events.BASELINE_ESTIMATE_RECORDED,
    events.CANDIDATE_ESTIMATES_RECORDED,
    events.RECOMMENDATION_RECORDED,
    events.POLICY_DECISION_RECORDED,
    events.CUSTOMER_SIGNAL_RECORDED,
    events.RECOVERY_RECORDED,
    events.RECONCILED_TO_RECOVERED,
    events.DELAYED_RECOVERY_RECONCILED,
)
"""Event types that complete a stage by their presence alone.

Constants from :mod:`revora.audit.events`, never literals — the same rule the production rules
follow, and for the same reason: a literal here would let this generator and the projection disagree
about the spelling of an event type, and the property would then pass while testing a stage that
never completes.

Deliberately *not* imported from ``revora.timeline.stages._COMPLETION_RULES``. That table is the
thing under test, and a generator drawing from it would produce exactly the inputs the
implementation expects — so a stage dropped from the table would vanish from the generator too and
P57 would keep passing over a smaller world. Restating the list here means the two have to agree,
and a test asserts they do."""

SKIP_EVENT_TYPES: tuple[str, ...] = (
    events.BASELINE_ESTIMATION_FAILED,
    events.CANDIDATE_MEMORY_UNAVAILABLE,
    events.EXECUTION_ABANDONED_POLICY,
    events.EXECUTION_REFUSED,
    events.ACTION_CANCELLED_PAYMENT_RECEIVED,
    events.ACTION_CANCELLED_CONTACT_SUPPRESSED,
)
"""Event types that evidence a skipped stage (R26.C5).

Drawn alongside the completing types rather than instead of them, so a sequence can hold both — a
case whose estimation failed and then succeeded on a retry is a real history, and it must project as
``DONE`` rather than ``SKIPPED``. That ordering claim is only checkable if the generator can produce
the sequence that tests it."""

_IN_PROGRESS_EVENT_TYPES: tuple[str, ...] = (
    events.DIAGNOSIS_UNMAPPED_REASON,
    events.EXECUTION_STARTED,
    events.PAYMENT_STATE_CONFLICT,
    events.PAYMENT_STATE_READ_UNAVAILABLE,
)
"""Event types that mean a stage is under way. Included so ``IN_PROGRESS`` is reachable.

Without these the status enumeration would be explored three-quarters of the way and the fourth
member would appear in no generated example — which is the failure mode of a strategy that looks
thorough. P57's clause about stages *without* a completing record is specifically the one that has
to see them."""

_NOISE_EVENT_TYPES: tuple[str, ...] = (
    events.EVENT_ATTACHED_TO_CASE,
    events.DIAGNOSIS_ALREADY_RECORDED,
    events.CANDIDATE_ACTION_UNAVAILABLE,
    events.HUMAN_OWNER_ASSIGNED,
    events.CUSTOMER_SIGNAL_REJECTED,
    events.PAYMENT_STATE_READ_RECORDED,
)
"""Event types that complete nothing, skip nothing and mean nothing to any stage.

Present because a real case's audit trail is mostly these, and a projection that keyed a stage on
"a record exists" rather than on "a record of *this type* exists" would pass every property against
a generator that only produced meaningful records. Six of them, chosen to sit next to a rule they
could plausibly be confused with — ``DIAGNOSIS_ALREADY_RECORDED`` beside ``DIAGNOSIS_RECORDED``,
``PAYMENT_STATE_READ_RECORDED`` beside the two payment-state records that do mean something."""

TIMELINE_EVENT_TYPES: tuple[str, ...] = (
    *COMPLETING_EVENT_TYPES,
    *SKIP_EVENT_TYPES,
    *_IN_PROGRESS_EVENT_TYPES,
    *_NOISE_EVENT_TYPES,
    events.CASE_REVIEWED,
    events.STATE_TRANSITION,
)
"""Every event type this generator can draw. Every one of them is a declared constant.

``STATE_TRANSITION`` is last and is the one type whose *columns* matter as well as its name: it
completes ``EXECUTED`` when its ``new_state`` is ``WAITING_FOR_OUTCOME`` and ``OUTCOME_VERIFIED``
when the state is terminal, so :func:`audit_sequences` draws a state for it rather than leaving the
column null."""

_TRANSITION_STATES: tuple[CaseState, ...] = (
    CaseState.DIAGNOSED,
    CaseState.DECISION_PENDING,
    CaseState.POLICY_CHECK,
    CaseState.ACTION_SCHEDULED,
    CaseState.EXECUTING,
    CaseState.WAITING_FOR_OUTCOME,
    *sorted(TERMINAL_STATES, key=lambda state: state.value),
)
"""States a generated ``STATE_TRANSITION`` may name.

Includes ``WAITING_FOR_OUTCOME`` and every Terminal_State — the two that complete a stage — plus
five that complete nothing, so a transition record is not a guaranteed completion. A generator whose
transitions always landed on a completing state would make ``EXECUTED``'s rule look like "any
transition" and never notice."""


@st.composite
def audit_sequences(
    draw: st.DrawFn,
    *,
    with_gap: bool | None = None,
    min_size: int = 0,
    max_size: int = 14,
) -> tuple[AuditRecordView, ...]:
    """A case's Audit_Records: a subset of the event types, numbered, and shuffled.

    Args:
        with_gap: ``False`` forces a whole sequence numbered ``1..n``. ``True`` forces at least one
            missing number inside the observed range. ``None`` — the default — draws either, which
            is what a property about arbitrary sequences wants.
        min_size: smallest number of records. Zero by default and reachable on purpose: a case whose
            ``CASE_DETECTED`` record has not been read is nine ``UPCOMING`` stages and no gap, and
            that is the input on which an over-eager gap check reports a bypassed allocation on the
            most ordinary page in the system.
        max_size: largest number of records. Fourteen covers every completing type plus a skip, an
            in-progress marker, a review and two transitions, which is more history than any real
            single-cycle case has.

    **Three values rather than a boolean, and that is not tidiness.** A ``with_gap: bool`` would
    make the gap-free case the *default* draw of a two-valued switch, and the two properties that
    need it pinned — a whole sequence must report ``complete``, a gapped one must name its missing
    numbers — would each be asserting over a generator that also produced the other kind. ``None``
    keeps a third option for the properties that genuinely do not care, so no test has to filter.

    **How the gap is made.** Numbers are drawn as ``1..n`` and then a strict, non-empty subset of
    the interior is removed, leaving the first and last in place. Removing an interior number is the
    only kind of gap ``max(seq) - min(seq) + 1`` against the record count can detect: a sequence
    missing its first three records is arithmetically indistinguishable from one that legitimately
    starts at four, which is a real limitation of the check and the reason
    :class:`~revora.timeline.stages.SequenceIntegrity` carries ``starts_at_one`` alongside it. The
    generator makes the detectable kind, because a property asserting that an undetectable gap is
    detected would be asserting something false.

    A gap needs at least three records to exist — two endpoints and one interior number to remove —
    so ``with_gap=True`` raises ``min_size`` to three rather than filtering, which would throw away
    most draws and leave Hypothesis shrinking towards a size it can never use.
    """
    gap = draw(st.booleans()) if with_gap is None else with_gap
    lower = max(min_size, 3) if gap else min_size
    size = draw(st.integers(min_value=lower, max_value=max(lower, max_size)))

    event_types = draw(
        st.lists(st.sampled_from(TIMELINE_EVENT_TYPES), min_size=size, max_size=size)
    )
    numbers = list(range(1, size + 1))

    if gap and size >= 3:
        interior = numbers[1:-1]
        removed = draw(
            st.lists(
                st.sampled_from(interior),
                min_size=1,
                max_size=len(interior),
                unique=True,
            )
        )
        numbers = [number for number in numbers if number not in set(removed)]
        event_types = event_types[: len(numbers)]

    records = [
        AuditRecordView(
            seq=number,
            event_type=event_type,
            occurred_at=_EPOCH + timedelta(minutes=number),
            new_state=(
                draw(st.sampled_from(_TRANSITION_STATES)).value
                if event_type == events.STATE_TRANSITION
                else None
            ),
            review_trigger=(
                draw(st.sampled_from(("SCHEDULED_REVIEW", "EVENT_ATTACHED", "CUSTOMER_SIGNAL")))
                if event_type == events.CASE_REVIEWED
                else None
            ),
            previous_selected_action=(
                draw(st.sampled_from([action.value for action in CandidateAction] + [None]))
                if event_type == events.CASE_REVIEWED
                else None
            ),
            new_selected_action=(
                draw(st.sampled_from([action.value for action in CandidateAction] + [None]))
                if event_type == events.CASE_REVIEWED
                else None
            ),
        )
        for number, event_type in zip(numbers, event_types, strict=True)
    ]

    # Shuffled, so a projection that trusted arrival order fails here rather than in production.
    # `permutations` rather than a `random.shuffle`: the permutation is drawn, so a counterexample
    # records the order that broke it and replays it.
    return tuple(draw(st.permutations(records)))


@st.composite
def case_views(draw: st.DrawFn, *, now: datetime) -> CaseView:
    """A Recovery_Case as the projection sees it, with the review window drawn around ``now``.

    ``next_review_at`` is drawn on both sides of ``now`` and as ``None``, and
    ``decision_cycle_count`` is drawn on both sides of ``max_recovery_attempts``. Those two are the
    only time-dependent and only configuration-dependent inputs the projection has (R30.C14), so
    they are the two the generator has to straddle — a strategy that always produced a future review
    instant would make the ``DECIDED`` stage ``IN_PROGRESS`` in every example and never exercise the
    ``UPCOMING`` branch.

    ``state`` is drawn from all fourteen, terminal and not. ``CUSTOMER_RESPONDED``'s skip condition
    reads it, and so does the ``OUTCOME_VERIFIED`` field set.
    """
    attempts = draw(st.integers(min_value=1, max_value=5))
    return CaseView(
        case_id="c0000000-0000-4000-8000-00000000000f",
        state=draw(st.sampled_from([state.value for state in CaseState])),
        detected_at=_EPOCH,
        decision_cycle_count=draw(st.integers(min_value=0, max_value=attempts + 2)),
        max_recovery_attempts=attempts,
        next_review_at=draw(
            st.one_of(
                st.none(),
                st.just(now - timedelta(hours=1)),
                st.just(now),
                st.just(now + timedelta(hours=1)),
            )
        ),
        terminal_reason=draw(
            st.one_of(st.none(), st.sampled_from([reason.value for reason in TerminalReason]))
        ),
        provider_payment_id="pay_generated",
    )


@st.composite
def signal_views(draw: st.DrawFn) -> tuple[SignalView, ...]:
    """Customer_Signals for a case, possibly none, in submission order.

    ``PAGE_VIEWED`` rows carry no delay reason and are drawn, because R26.C4 asks for *every*
    persisted kind and the page view is the one a filter would quietly drop. The empty tuple is
    reachable, which is what makes ``CUSTOMER_RESPONDED``'s skip condition exercisable.
    """
    return tuple(
        draw(
            st.lists(
                st.builds(
                    SignalView,
                    kind=st.sampled_from([kind.value for kind in CustomerSignalKind]),
                    submitted_at=st.just(_EPOCH + timedelta(hours=1)),
                    delay_reason=st.one_of(
                        st.none(),
                        st.sampled_from([reason.value for reason in DelayReason]),
                    ),
                ),
                max_size=4,
            )
        )
    )


@st.composite
def intent_views(draw: st.DrawFn) -> tuple[IntentView, ...]:
    """Execution_Intents for a case, possibly none.

    The intent state is a *presented* field and never a completion rule, so these are drawn
    independently of whether any record completes the ``EXECUTED`` stage. That independence is the
    property being protected: a generator that produced a ``CONFIRMED`` intent only alongside the
    completing transition would make the two look coupled, and a projection that had started keying
    the stage on the intent would keep passing.
    """
    return tuple(
        draw(
            st.lists(
                st.builds(
                    IntentView,
                    action=st.sampled_from([action.value for action in CandidateAction]),
                    state=st.sampled_from(("ATTEMPTED", "CONFIRMED", "FAILED", "UNCERTAIN")),
                    attempt_started_at=st.just(_EPOCH + timedelta(minutes=30)),
                ),
                max_size=3,
            )
        )
    )


@st.composite
def figure_views(draw: st.DrawFn, *, with_ai_explanation: bool | None = None) -> FigureView:
    """The recorded figures, every currency value already a formatted string.

    Args:
        with_ai_explanation: ``True`` forces a ``DECISION_EXPLANATION`` paragraph to be present,
            ``False`` forces its absence, ``None`` draws either. P58 needs the first two pinned,
            because its claim is that the deterministic sentences are *identical* across the pair —
            which cannot be checked without holding one view against the other.

    **The AI paragraph is constructed here rather than read from anywhere, and that is the honest
    state of the system rather than a shortcut.** ``revora.reasoning`` holds contracts and schemas,
    no module in ``revora`` imports it, and nothing writes ``ai_invocation`` at all — so the present
    branch of R26.C9 has no producer today. Generating the record is what makes P58 cover both
    branches for real instead of asserting a property about the absent case twice.

    Every money field is drawn as a formatted-looking string and every field is independently
    optional, including all of them absent. A view with no figures at all is a case at ``DETECTED``,
    and it is the input on which a sentence would most easily acquire a substituted zero.
    """
    money = st.sampled_from(("₹0.00", "₹1.00", "₹4,120.00", "₹12,34,567.89"))
    optional_money = st.one_of(st.none(), money)
    ai = (
        draw(st.booleans()) if with_ai_explanation is None else with_ai_explanation
    )
    return FigureView(
        payment_amount_formatted=draw(optional_money),
        cause=draw(st.one_of(st.none(), st.sampled_from([c.value for c in RiskCause]))),
        confidence=draw(st.one_of(st.none(), st.sampled_from(("0.000", "0.600", "1.000")))),
        diagnosis_method=draw(
            st.one_of(st.none(), st.sampled_from([m.value for m in DiagnosisMethod]))
        ),
        evidence_source=draw(
            st.one_of(st.none(), st.sampled_from([s.value for s in DiagnosisEvidenceSource]))
        ),
        baseline_probability=draw(
            st.one_of(st.none(), st.sampled_from(("0.0000", "0.3100", "1.0000")))
        ),
        baseline_interval=draw(st.one_of(st.none(), st.just("[0.20, 0.44]"))),
        priced_count=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=9))),
        unavailable_count=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=9))),
        cheapest_total_action_cost_formatted=draw(optional_money),
        selected_action=draw(
            st.one_of(st.none(), st.sampled_from([a.value for a in CandidateAction]))
        ),
        net_recovery_value_formatted=draw(optional_money),
        selection_reason=draw(
            st.one_of(st.none(), st.sampled_from([r.value for r in SelectionReason]))
        ),
        runner_up_action=draw(
            st.one_of(st.none(), st.sampled_from([a.value for a in CandidateAction]))
        ),
        runner_up_value_formatted=draw(optional_money),
        policy_verdict=draw(
            st.one_of(st.none(), st.sampled_from([v.value for v in PolicyVerdict]))
        ),
        policy_primary_reason=draw(
            st.one_of(st.none(), st.sampled_from([c.value for c in PolicyCheck]))
        ),
        recovered_amount_formatted=draw(optional_money),
        outcome_classification=draw(
            st.one_of(st.none(), st.sampled_from([o.value for o in OutcomeClass]))
        ),
        outcome_verified_at=draw(st.one_of(st.none(), st.just(_EPOCH + timedelta(days=1)))),
        ai_explanation_text=(
            "The payment link was chosen because it was worth the most of the four "
            "available options."
            if ai
            else None
        ),
    )


@st.composite
def timeline_inputs(
    draw: st.DrawFn,
    *,
    now: datetime,
    with_gap: bool | None = None,
    with_ai_explanation: bool | None = None,
) -> tuple[
    tuple[AuditRecordView, ...],
    CaseView,
    tuple[SignalView, ...],
    tuple[IntentView, ...],
    FigureView,
]:
    """One complete argument tuple for :func:`revora.timeline.stages.project`, minus ``now``.

    Composed rather than drawn per test so every property here explores the same input space, and
    returned as a plain tuple in the projection's own parameter order so a test reads
    ``project(*inputs, now)`` and cannot pass the views in the wrong order.

    The five parts are drawn **independently**. That is the whole point: a real case's records,
    figures, signals and intents are correlated, and generating them correlated would hide every bug
    in which the projection infers one from another. A recovered case with no ``RECOVERY_RECORDED``
    record and a ``CONFIRMED`` intent alongside no execution transition are both impossible in
    production and both are exactly the shapes P57 has to be checked against, because a projection
    that filled in a missing record from a present figure would pass on every consistent input.
    """
    return (
        draw(audit_sequences(with_gap=with_gap)),
        draw(case_views(now=now)),
        draw(signal_views()),
        draw(intent_views()),
        draw(figure_views(with_ai_explanation=with_ai_explanation)),
    )

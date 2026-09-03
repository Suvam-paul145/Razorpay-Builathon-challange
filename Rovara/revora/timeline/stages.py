"""The projection. It takes frozen views and returns one, and that is the whole safety argument.

R26.C6 says the Timeline_Reader performs no write of any kind, and R26.C7 says a repeated
projection of one unchanged set of Audit_Records is identical. Neither of those is enforced here by
discipline. Both are enforced by what :func:`project` is *able* to do:

**It cannot write, because it has nothing to write with.** The signature takes frozen dataclasses
and returns one. There is no ``Session`` parameter, no repository, no engine, no queue. This is the
same argument the base spec makes for ``policy.evaluate``, and it is worth restating why the
argument is stronger than a test: a test that asserts no write happened is a test about one input,
while a function with no session in scope has no input for which a write is expressible. P56 asserts
it anyway — with a session that raises on flush, handed in nowhere and reachable from nothing — so
that the *absence* of the parameter is what the test is really checking.

**It cannot drift with the clock, because it reads no clock.** Every presented instant comes off a
record. There is exactly one time-dependent decision in the whole projection — whether ``DECIDED``
is ``IN_PROGRESS`` because a further review remains permitted (R30.C14) — and it compares
``case.next_review_at`` and ``case.decision_cycle_count``, both persisted, against a ``now`` passed
in explicitly as an argument. A caller supplying the same ``now`` gets the same timeline. A caller
that read a clock did so above this function, where it is visible.

**Record order affects only which records satisfy which rule.** :data:`STAGE_ORDER` is a
module-level tuple. The presented order of the stages is therefore a property of this file rather
than of the sequence of rows that happened to arrive, so a case whose audit records were read in a
different order cannot render its history in a different order.

**The completion rules are keyed to audit event types and nothing else.** Every ``DONE`` is "a
record of this type exists for this case" (:data:`_COMPLETION_RULES`), which is what makes P57
checkable from an audit sequence alone. R26.C4 also requires some *displayed* fields that are not
audit records — the execution-intent state, the recovered amount — and those arrive in
:class:`FigureView` and :class:`IntentView` as **presented** values. The distinction is
load-bearing: a presented field can be absent without changing any stage's status, whereas a
completion rule that read a mutable row would make a stage's status depend on something the audit
trail does not witness.

**``EXECUTED`` keys on the state transition rather than on the intent row**, deliberately. A
``STATE_TRANSITION`` into ``WAITING_FOR_OUTCOME`` is written on both the fast path
(``handle_execution``) and the reconciliation path (``_advance_confirmed``), so one rule covers
both. Keying on the intent would need two rules and would tie a stage's completion to a row that
reconciliation later updates.

A note on what this module deliberately does not contain: no aggregation and no arithmetic on money.
Every currency figure arrives in :class:`FigureView` as the *server-formatted string* the API's
renderer produced, and this module substitutes it into a sentence. It holds no currency symbol
table, no minor-unit digits and no divisor, so it cannot produce a figure that disagrees with the
one on the rest of the page — it has no way to compute one (R26.C8).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from revora.audit import events
from revora.domain.actions import NULL_ACTIONS
from revora.domain.enums import CaseState, TimelineStage, TimelineStageStatus
from revora.domain.transitions import TERMINAL_STATES
from revora.timeline.templates import (
    ACTION_LABELS,
    CAUSE_LABELS,
    DELAY_REASON_LABELS,
    DIAGNOSIS_METHOD_LABELS,
    EVIDENCE_SOURCE_LABELS,
    OUTCOME_CLASS_LABELS,
    POLICY_REASON_LABELS,
    POLICY_VERDICT_LABELS,
    SELECTION_REASON_LABELS,
    SIGNAL_KIND_LABELS,
    STAGE_TEMPLATES,
    TERMINAL_REASON_LABELS,
    TERMINAL_STATE_LABELS,
    UNCERTAINTY_UNAVAILABLE,
    Labelled,
    labelled,
    render,
)

__all__ = [
    "REVIEW_DECISION_KEYS",
    "STAGE_ORDER",
    "AuditRecordView",
    "CaseTimeline",
    "CaseView",
    "FigureView",
    "IntentView",
    "SequenceIntegrity",
    "SignalView",
    "TimelineStageProjection",
    "project",
    "sequence_integrity",
]


STAGE_ORDER: Final[tuple[TimelineStage, ...]] = (
    TimelineStage.DETECTED,
    TimelineStage.DIAGNOSED,
    TimelineStage.BASELINE_ESTIMATED,
    TimelineStage.ALTERNATIVES_PRICED,
    TimelineStage.DECIDED,
    TimelineStage.POLICY_CHECKED,
    TimelineStage.EXECUTED,
    TimelineStage.CUSTOMER_RESPONDED,
    TimelineStage.OUTCOME_VERIFIED,
)
"""The nine stages in the order R26.C1 names them, as a tuple rather than as record order.

A module-level constant, so the presented order is a fact about this file. The alternative —
ordering stages by the sequence number of the record that completed each — sounds equivalent and is
not: a stage with no completing record has no sequence number to sort by, so an ``UPCOMING`` stage
would have to be placed by a rule anyway, and a ``SKIPPED`` one would land wherever its skip
evidence happened to be written. The pipeline's order is the explanation R26 exists to present, and
it is the same for every case whether or not the case followed it.

Asserted below to cover :class:`~revora.domain.enums.TimelineStage` exactly, so a stage added to the
enumeration without being placed here is an import failure rather than a stage missing from a page.
"""

if tuple(sorted(stage.value for stage in STAGE_ORDER)) != tuple(
    sorted(stage.value for stage in TimelineStage)
) or len(STAGE_ORDER) != len(set(STAGE_ORDER)):  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "STAGE_ORDER must list every TimelineStage exactly once; it is the presented order "
        "(R26.C1) and a stage absent from it would be absent from every timeline"
    )


# ---------------------------------------------------------------------------
# The input views. Frozen, and every one of them is somebody else's read.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditRecordView:
    """One Audit_Record, reduced to the fields a stage rule or a sentence reads.

    Four fields, and the reduction is the point rather than an optimization. An ``AuditRecord`` row
    carries five JSONB documents, a masked-field list and a truncation record; a projection with
    access to all of that could reach a conclusion no completion rule declares, by reading a value
    out of ``decision`` that nobody wrote down as a rule. So the view carries the event type, the
    sequence number, the instant and the two state columns — which is exactly what
    :data:`_COMPLETION_RULES` needs and nothing more.

    ``seq`` is not optional here even though the column is nullable. A record with a null ``seq`` is
    an *unattached* record — a rejected signature, a rate limit — and by construction belongs to no
    case, so it is not part of any case's timeline. The router filters on the case, so a null can
    never arrive; making the field non-optional means the gap check in :func:`sequence_integrity`
    has no ``None`` to skip and therefore no way to skip a real record by accident.
    """

    seq: int
    event_type: str
    occurred_at: datetime
    previous_state: str | None = None
    new_state: str | None = None

    review_trigger: str | None = None
    previous_selected_action: str | None = None
    new_selected_action: str | None = None
    """The three fields R30.C14 requires beside every ``CASE_REVIEWED`` record.

    Named individually rather than carried as the record's ``decision`` document, and that is the
    same decision as the four fields above taken again. A view holding the JSONB would let the
    projection reach a value no completion rule declares — and the review triple is exactly the
    tempting case, because the document also carries ``selection_changed``, ``unresolved_amount``
    and a config version, none of which R30.C14 asks the timeline to present.

    So the router lifts these three by name (:data:`REVIEW_DECISION_KEYS`) and everything else in
    the document stays where it is: readable in the audit trail below the timeline, which is the
    surface for reading a record in full.

    ``None`` on every record that is not a ``CASE_REVIEWED`` one, and ``None`` on a review whose
    previous or new selection was itself absent — a review that reached "no action" records a null
    action rather than a sentinel, and the label tables turn that into a named absence.
    """


REVIEW_DECISION_KEYS: Final[tuple[str, str, str]] = (
    "review_trigger",
    "previous_selected_action",
    "new_selected_action",
)
"""The three keys the router lifts out of a ``CASE_REVIEWED`` record's ``decision`` document.

Declared here, next to the view they populate, so the extraction the router performs and the fields
this projection reads are one declaration rather than two lists that could both be edited. The
spellings are ``revora.jobs.pipeline._record_case_reviewed``'s own; a test asserts a written record
carries all three, because a rename there would otherwise show every review with an unnamed trigger
and the timeline would keep rendering.
"""


@dataclass(frozen=True, slots=True)
class CaseView:
    """The persisted Recovery_Case fields the projection reads, and the one configured bound.

    ``max_recovery_attempts`` is the odd one out and is here for a reason the alternative makes
    clear. R30.C14 asks whether *a further review remains permitted*, which is
    ``decision_cycle_count < MAX_RECOVERY_ATTEMPTS`` — a comparison between a persisted counter and
    a per-merchant configured bound. The projection cannot read configuration (it takes no accessor,
    and ``revora.platform.config`` would be an odd import for a read model), so the caller resolves
    the bound and passes it in beside the counter. That keeps the comparison inside the pure
    function where P56 can see it, instead of the caller pre-computing a boolean and the projection
    trusting a conclusion it cannot check.

    ``terminal_reason`` is a plain ``str | None`` rather than the enum, like every other enumeration
    member in these views. A row can hold a value this build does not know — a case decided before a
    rename — and the label tables handle that by preserving the stored member. Parsing here would
    turn a readable page into an exception.
    """

    case_id: str
    state: str
    detected_at: datetime
    decision_cycle_count: int
    max_recovery_attempts: int
    next_review_at: datetime | None = None
    terminal_reason: str | None = None
    provider_payment_id: str = ""


@dataclass(frozen=True, slots=True)
class SignalView:
    """One persisted Customer_Signal: its kind, its instant, and the reason where it has one.

    R26.C4 asks for *every* persisted Customer_Signal_Kind with its submission instant, so this is a
    sequence in :func:`project` rather than a latest-only field. ``PAGE_VIEWED`` rows are included
    and are not filtered anywhere: "the customer opened the link and said nothing" is the only
    evidence some cases have, and a stage that dropped it would be indistinguishable from a customer
    who never arrived.

    Deliberately no note. ``delay_reason_note`` is free text a stranger typed on a public page. It
    is persisted as evidence and it is subject to the retention redaction of R29.C10, and neither
    fact makes it something a nine-line summary should render — the enumerated reason is the part
    with a declared meaning.
    """

    kind: str
    submitted_at: datetime
    delay_reason: str | None = None


@dataclass(frozen=True, slots=True)
class IntentView:
    """One Execution_Intent, as the two presented fields R26.C4 names for the ``EXECUTED`` stage.

    The intent state is a **presented** field and never a completion rule, which is the distinction
    the module docstring turns on. ``EXECUTED`` is ``DONE`` because a ``STATE_TRANSITION`` into
    ``WAITING_FOR_OUTCOME`` exists; the intent's own state — ``ATTEMPTED``, ``CONFIRMED``,
    ``FAILED``, ``UNCERTAIN`` — is what the sentence reports as the provider's result. Keeping them
    apart is what lets the stage statuses be checked from the audit sequence alone (P57) while the
    sentence still says what actually came back.
    """

    action: str
    state: str
    attempt_started_at: datetime


@dataclass(frozen=True, slots=True)
class FigureView:
    """Every recorded figure a stage sentence substitutes, already rendered as a string.

    This is the view that makes R26.C8 structural rather than procedural. Each money field holds the
    **server-formatted string** — ``₹4,120.00`` — produced by ``revora.api.rendering``, not minor
    units. There is nothing here to do arithmetic on and no currency vocabulary in this package to
    do it with, so a sentence cannot present a figure that disagrees with the same figure elsewhere
    on the page.

    Every field defaults to ``None``, and ``None`` means *the audit trail does not carry this*,
    which is an ordinary state rather than an error: a case at ``DETECTED`` has no baseline, a case
    whose selection was a null action has no runner-up worth naming, a case that ended without
    recovering has no recovered amount. :func:`~revora.timeline.templates.labelled` and
    :data:`_NOT_RECORDED_VALUE` turn each absence into a named absence rather than into a zero.

    The counts are ``int`` and are presented as counts, never compared against each other and never
    summed. ``priced_count`` and ``unavailable_count`` come from the same recorded candidate set, so
    the second is a subset count of the first, and the sentence prints both rather than a difference
    — a difference is arithmetic, and arithmetic in a read model is a figure free to disagree with
    the table it was derived from.
    """

    payment_amount_formatted: str | None = None

    cause: str | None = None
    confidence: str | None = None
    diagnosis_method: str | None = None
    evidence_source: str | None = None

    baseline_probability: str | None = None
    baseline_interval: str | None = None

    priced_count: int | None = None
    unavailable_count: int | None = None
    cheapest_total_action_cost_formatted: str | None = None

    selected_action: str | None = None
    net_recovery_value_formatted: str | None = None
    selection_reason: str | None = None
    runner_up_action: str | None = None
    runner_up_value_formatted: str | None = None

    policy_verdict: str | None = None
    policy_primary_reason: str | None = None

    recovered_amount_formatted: str | None = None
    outcome_classification: str | None = None
    outcome_verified_at: datetime | None = None

    ai_explanation_text: str | None = None
    """The ``DECISION_EXPLANATION`` paragraph, where one was recorded for this decision cycle.

    ``None`` is the normal case and, as of task 50, the *only* case reachable in a running system:
    ``revora.reasoning`` holds contracts and schemas, no module in ``revora`` imports it, and
    nothing writes ``ai_invocation`` at all. The field exists anyway because R26.C9's requirement is
    a claim about both branches — the paragraph is presented **adjacent to** the ``DECIDED`` stage
    and marked ``AI_GENERATED``, and every deterministic sentence of R26.C3 and R26.C4 is identical
    whether or not it exists.

    That second half is the one this design has to earn, and it earns it by construction: no
    sentence template takes this field as a substitution, and :func:`project` never reads it while
    building one. It is attached to :class:`CaseTimeline` beside the stages, so a caller that
    dropped it would lose a paragraph and change no sentence. P58 asserts exactly that, over a
    fixture carrying a constructed record and one carrying none.
    """


_NOT_RECORDED_VALUE: Final[str] = "not recorded"
"""What a sentence prints where a figure it names is absent.

Lower case, because it is substituted mid-sentence — ``Recovered not recorded, classified …`` is
the wrong sentence for that state, which is why :func:`_outcome_variant` chooses the *other*
declared template rather than filling this in. Where it does appear the alternative is worse: a
placeholder left unfilled, or a zero. A zero in place of an absent amount is a false financial
statement, and R26.C10 and R14.C15 both say so in their own terms."""


# ---------------------------------------------------------------------------
# The output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineStageProjection:
    """One stage: its status, its instant where it has one, its two sentences, and its fields.

    ``instant`` is present only where ``status`` is ``DONE``, and it is taken from the completing
    Audit_Record's ``occurred_at`` (R26.C3) — never from ``now()`` and never from the row the figure
    came from. An ``UPCOMING`` stage has no instant because nothing has happened; a ``SKIPPED``
    stage has none because the thing that did not happen has no time.

    ``skip_reason`` is present only where ``status`` is ``SKIPPED``, and R26.C5 requires it to be
    *named from the persisted Audit_Records*. It therefore holds an audit event type, not prose: the
    record that evidences the skip is the reason, and a sentence would be a second account of it.

    ``fields`` carries the R26.C4 field set for the stage as a mapping of already-stringified
    values with their labels where they have them. A mapping rather than a per-stage dataclass
    because the nine field sets have almost nothing in common, and nine result classes would be nine
    places for the wire shape to be declared twice.
    """

    stage: TimelineStage
    status: TimelineStageStatus
    order: int
    instant: datetime | None = None
    decision_sentence: str | None = None
    evidence_sentence: str | None = None
    skip_reason: str | None = None
    fields: Mapping[str, object] = field(default_factory=dict)

    def as_document(self) -> dict[str, object]:
        """The wire form. Fixed keys, so a client branches on ``status`` and never on shape."""
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "order": self.order,
            "instant": None if self.instant is None else self.instant.isoformat(),
            "decision_sentence": self.decision_sentence,
            "evidence_sentence": self.evidence_sentence,
            "skip_reason": self.skip_reason,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True, slots=True)
class SequenceIntegrity:
    """Whether the case's Audit_Record sequence is whole, and which numbers are missing (R26.C11).

    A gap is **not** ordinary wear, and the wording of everything here follows from that. Sequence
    numbers are allocated by incrementing ``recovery_case.audit_seq`` with ``RETURNING`` inside the
    same transaction as the audit insert, while the writer already holds the case row under ``FOR
    UPDATE``; a rolled-back transaction rolls the increment back with it. So a gap does not mean a
    record was slow or a number was skipped — it means the allocation mechanism was bypassed. The
    banner exists to say so rather than to paper over it.

    Detection is ``max(seq) - min(seq) + 1`` against the record count, which is the design's own
    rule. Note what it does and does not catch: it finds a hole *inside* the observed range and it
    cannot find a truncation at either end, because a sequence missing its first three records is
    indistinguishable from one that starts at four. ``starts_at_one`` is carried for that reason —
    it is the other half of the same suspicion, and R28.C15 asserts it elsewhere as a property of
    real sequences.
    """

    complete: bool
    record_count: int
    first_seq: int | None
    last_seq: int | None
    missing: tuple[int, ...]
    starts_at_one: bool

    def as_document(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "record_count": self.record_count,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "missing": list(self.missing),
            "starts_at_one": self.starts_at_one,
            "detail": (
                None
                if self.complete
                else (
                    "This case's audit sequence is incomplete. Sequence numbers are allocated "
                    "inside the transaction that writes the record, so a gap means the "
                    "allocation was bypassed rather than that a record is late. Every stage "
                    "below is projected from the records that are present, and no stage is "
                    "shown as done on the strength of an absent record."
                )
            ),
        }


@dataclass(frozen=True, slots=True)
class CaseTimeline:
    """Nine stages in declared order, the sequence integrity finding, and any AI paragraph.

    ``stages`` is a tuple of exactly nine elements in :data:`STAGE_ORDER`, always. Not "the stages
    that happened" — a timeline that omitted the stages a case has not reached would be shorter for
    an early case, and a reader used to nine rows seeing six does not notice which three went. The
    same argument the policy view makes for always showing twelve checks.

    ``ai_explanation`` sits here rather than inside the ``DECIDED`` stage, which is R26.C9's word
    *adjacent* taken literally. Inside the stage it would be one more field of the stage's field
    set, and a client rendering the field set generically would render advisory prose in the same
    register as a recorded figure. Beside it, a caller can drop it entirely and every sentence is
    unchanged — which is the invariant P58 checks.
    """

    case_id: str
    stages: tuple[TimelineStageProjection, ...]
    integrity: SequenceIntegrity
    ai_explanation: str | None = None
    ai_explanation_label: str = "AI_GENERATED"

    def as_document(self) -> dict[str, object]:
        """The wire form.

        ``ai_explanation_label`` travels even when the paragraph is ``None``, so the key set does
        not change with the presence of a reasoning record. A client whose shape depended on that
        would have two rendering paths for one screen, and the rarely-exercised one would be the one
        that broke.
        """
        return {
            "case_id": self.case_id,
            "stage_count": len(self.stages),
            "stages": [stage.as_document() for stage in self.stages],
            "audit_sequence": self.integrity.as_document(),
            "ai_explanation": self.ai_explanation,
            "ai_explanation_label": self.ai_explanation_label,
        }

    def stage(self, stage: TimelineStage) -> TimelineStageProjection:
        """One stage by name. Raises if it is absent, which the constructor makes impossible."""
        for projection in self.stages:
            if projection.stage is stage:
                return projection
        raise KeyError(stage)  # pragma: no cover - STAGE_ORDER totality makes this unreachable


# ---------------------------------------------------------------------------
# The completion rules, keyed to concrete event types (R26.C2, P57)
# ---------------------------------------------------------------------------

_COMPLETION_RULES: Final[Mapping[TimelineStage, tuple[str, ...]]] = MappingProxyType(
    {
        TimelineStage.DETECTED: (events.CASE_DETECTED,),
        TimelineStage.DIAGNOSED: (events.DIAGNOSIS_RECORDED,),
        TimelineStage.BASELINE_ESTIMATED: (events.BASELINE_ESTIMATE_RECORDED,),
        TimelineStage.ALTERNATIVES_PRICED: (events.CANDIDATE_ESTIMATES_RECORDED,),
        TimelineStage.DECIDED: (events.RECOMMENDATION_RECORDED,),
        TimelineStage.POLICY_CHECKED: (events.POLICY_DECISION_RECORDED,),
        TimelineStage.EXECUTED: (),
        TimelineStage.CUSTOMER_RESPONDED: (events.CUSTOMER_SIGNAL_RECORDED,),
        TimelineStage.OUTCOME_VERIFIED: (
            events.RECOVERY_RECORDED,
            events.RECONCILED_TO_RECOVERED,
            events.DELAYED_RECOVERY_RECONCILED,
        ),
    }
)
"""Which recorded event types complete each stage. Constants, never literals.

Every value is a constant imported from :mod:`revora.audit.events`, which is the one place an event
type string is declared. A literal here would be a second spelling of a vocabulary the writers
already own, and the audit trail would then answer a query for one and miss the other — the exact
failure that module's docstring exists to prevent.

``EXECUTED`` is an empty tuple and is **not** an omission: its rule is not "a record of type X
exists" but "a ``STATE_TRANSITION`` into ``WAITING_FOR_OUTCOME`` exists", which is a predicate over
two columns rather than a membership test on one. It is implemented in :func:`_executed_status` and
the empty tuple here keeps the table total over the nine stages, so a reader can see that every
stage has a declared rule and that one of them is declared elsewhere.

``OUTCOME_VERIFIED`` has three event types plus a transition predicate, because a case can reach a
verified outcome by three routes: the ordinary recovery, a reconciliation of a case that had already
ended, and a delayed capture. Any of them is the stage completing. A rule per route would present
the same stage differently depending on how the money arrived, which is a distinction the merchant
did not ask for."""

_SKIP_RULES: Final[Mapping[TimelineStage, tuple[str, ...]]] = MappingProxyType(
    {
        TimelineStage.BASELINE_ESTIMATED: (events.BASELINE_ESTIMATION_FAILED,),
        TimelineStage.ALTERNATIVES_PRICED: (events.CANDIDATE_MEMORY_UNAVAILABLE,),
        TimelineStage.EXECUTED: (
            events.EXECUTION_ABANDONED_POLICY,
            events.EXECUTION_REFUSED,
            events.ACTION_CANCELLED_PAYMENT_RECEIVED,
            events.ACTION_CANCELLED_CONTACT_SUPPRESSED,
        ),
    }
)
"""Which recorded event types evidence a *skip*, per stage (R26.C5).

A skip has to be named from the audit records, so the reason is the event type of the record that
evidences it — not prose, and not an inference from a later stage having completed.

**Three stages have skip evidence and six do not, and the asymmetry is real rather than
incomplete.** ``DETECTED`` can never be skipped: the case exists because it was detected.
``DIAGNOSED``, ``DECIDED`` and ``POLICY_CHECKED`` are not skippable steps either — every decision
cycle produces all three or the case does not advance — so a case without them has not reached them,
which is ``UPCOMING``. ``CUSTOMER_RESPONDED`` is skipped on a condition rather than on a record: a
terminal case with no signal recorded is a customer who never answered, and there is no audit
record for a thing a person did not do. That case is handled in :func:`_signal_status`.

**``ACTION_CANCELLED_CONTACT_SUPPRESSED`` is now present, and its arrival is worth recording.**
It appears in the design's skip column for ``EXECUTED`` and for several revisions it was absent
from this tuple, because :mod:`revora.audit.events` declared no constant for it and nothing wrote
one — keying a rule on a record nothing can produce would have meant spelling the string as a
literal here, which is the one thing that module forbids. The note left in its place said that
whichever task introduced the writer would add the constant there and the entry here in the same
commit. Task 42 is that task: the writer is
:func:`revora.jobs.pipeline.handle_contact_suppression`, cancelling a scheduled action because
the customer disputed the charge or cancelled the order (R21.C6).

The suppression path also still reaches this stage through ``EXECUTION_ABANDONED_POLICY``, which
policy check 5 produces for any *later* customer-visible action on a suppressed scope. The two
are different occurrences and both belong here: the cancellation is the action that was already
authorized and did not happen, the abandonment is an action that was proposed afterwards and was
refused. A case can show either or both, and neither is a duplicate of the other."""

_STAGE_IN_PROGRESS_RULES: Final[Mapping[TimelineStage, tuple[str, ...]]] = MappingProxyType(
    {
        TimelineStage.DIAGNOSED: (events.DIAGNOSIS_UNMAPPED_REASON,),
        TimelineStage.EXECUTED: (events.EXECUTION_STARTED,),
        TimelineStage.OUTCOME_VERIFIED: (
            events.PAYMENT_STATE_CONFLICT,
            events.PAYMENT_STATE_READ_UNAVAILABLE,
        ),
    }
)
"""Which recorded event types mean a stage is under way but not complete.

Each is a record that says work started and did not finish, and each is checked *after* the
completion rule — so a case that hit an unmapped reason and then recorded a diagnosis is ``DONE``,
not ``IN_PROGRESS``. That ordering is why these can be a plain table: the "no completing record
yet" half of the design's condition is the order of the checks in :func:`_status_for`, not a clause
each entry has to restate.

``DECIDED``'s ``IN_PROGRESS`` condition is not here, because it is the one condition in the whole
projection that is not keyed to a record — R30.C14 asks whether a further review remains permitted,
which reads two persisted case fields against the ``now`` argument. It lives in
:func:`_decided_status`."""


def _by_event_type(records: Sequence[AuditRecordView]) -> Mapping[str, AuditRecordView]:
    """The **first** record of each event type, by type.

    First rather than last, and the difference is visible on any case that has been through more
    than one decision cycle. R26.C3 wants the instant a stage completed, and a case reviewed three
    times has three ``RECOMMENDATION_RECORDED`` records — the stage completed at the first one. The
    later ones are the review history, which the ``DECIDED`` stage's field set reports separately.

    Iterated in ascending sequence order, which the router guarantees by reading in that order and
    :func:`_ordered` re-establishes here so the guarantee does not depend on the caller. Sequence
    order rather than timestamp order because two records can share a millisecond and the sequence
    is what says which came first.
    """
    found: dict[str, AuditRecordView] = {}
    for record in _ordered(records):
        found.setdefault(record.event_type, record)
    return found


def _ordered(records: Sequence[AuditRecordView]) -> tuple[AuditRecordView, ...]:
    """Records in ascending sequence order.

    Sorted here rather than trusted from the caller. R26.C1 says the records are read in ascending
    sequence order, and the router's query does order them — but P57 generates sequences
    deliberately out of order, and a projection whose output depended on the order it received would
    fail that property for a reason that has nothing to do with the completion rules. Sorting makes
    record order affect only *which* records satisfy which rule, which is claim 3 of the purity
    argument.
    """
    return tuple(sorted(records, key=lambda record: record.seq))


def _transition_into(
    records: Sequence[AuditRecordView], state: CaseState
) -> AuditRecordView | None:
    """The first ``STATE_TRANSITION`` record whose ``new_state`` is ``state``.

    The predicate behind ``EXECUTED``'s completion and behind ``OUTCOME_VERIFIED``'s terminal route.
    Compared against ``state.value`` rather than parsed into a ``CaseState``, so a row holding a
    state this build does not know is simply not a match instead of an exception on the way to a
    page somebody needed.
    """
    for record in _ordered(records):
        if record.event_type == events.STATE_TRANSITION and record.new_state == state.value:
            return record
    return None


def _terminal_transition(records: Sequence[AuditRecordView]) -> AuditRecordView | None:
    """The first ``STATE_TRANSITION`` into any Terminal_State.

    ``TERMINAL_STATES`` comes from ``revora.domain.transitions``, so which states are terminal is
    the domain's answer and not a list restated here. A seventh terminal state added there completes
    this stage without an edit in this file, which is the correct direction — a stage that failed to
    notice a new ending would show a finished case as still waiting.
    """
    terminal = {state.value for state in TERMINAL_STATES}
    for record in _ordered(records):
        if record.event_type == events.STATE_TRANSITION and record.new_state in terminal:
            return record
    return None


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def project(
    records: Sequence[AuditRecordView],
    case: CaseView,
    signals: Sequence[SignalView],
    intents: Sequence[IntentView],
    figures: FigureView,
    now: datetime,
) -> CaseTimeline:
    """Project one case's Audit_Records into nine ordered stages (R26.C1 through C7).

    Args:
        records: every Audit_Record of the case. Order is irrelevant — :func:`_ordered` sorts by
            sequence number — which is what lets P57 generate out-of-order sequences.
        case: the persisted Recovery_Case fields, plus the configured attempt bound R30.C14 needs.
        signals: every persisted Customer_Signal for the case, for R26.C4's field set.
        intents: every Execution_Intent, for the ``EXECUTED`` stage's *presented* state.
        figures: the recorded figures, with every currency value already formatted by the server.
        now: the instant the caller is asking about. Read for exactly one decision — whether
            ``DECIDED`` is ``IN_PROGRESS`` — and never presented. Passing it rather than reading a
            clock is what makes two projections of one unchanged input equal (R26.C7, P56).

    Returns:
        A :class:`CaseTimeline` with exactly nine stages in :data:`STAGE_ORDER`.

    The function performs no write. It has no session, no repository and no queue in scope, so there
    is no expression inside it that could perform one — see the module docstring for why that is a
    stronger statement than a test, and P56 for the test anyway.
    """
    ordered = _ordered(records)
    by_type = _by_event_type(ordered)

    return CaseTimeline(
        case_id=case.case_id,
        stages=tuple(
            _project_stage(
                stage,
                order=order,
                records=ordered,
                by_type=by_type,
                case=case,
                signals=signals,
                intents=intents,
                figures=figures,
                now=now,
            )
            for order, stage in enumerate(STAGE_ORDER, start=1)
        ),
        integrity=sequence_integrity(ordered),
        ai_explanation=figures.ai_explanation_text,
    )


def _project_stage(
    stage: TimelineStage,
    *,
    order: int,
    records: Sequence[AuditRecordView],
    by_type: Mapping[str, AuditRecordView],
    case: CaseView,
    signals: Sequence[SignalView],
    intents: Sequence[IntentView],
    figures: FigureView,
    now: datetime,
) -> TimelineStageProjection:
    """One stage: decide its status first, then say only as much as the status permits.

    The ordering inside this function is the honesty rule of the whole module. The status is decided
    from records; *then* the sentences are rendered, and only for a ``DONE`` stage, from the record
    that completed it. A stage that is not ``DONE`` gets no instant and no sentences, so there is no
    code path on which a sentence about what a stage decided exists without the record that decided
    it (R26.C2, R26.C11, P57).

    A ``SKIPPED`` stage still carries its field set. The reason is a specific misreading: a skipped
    ``BASELINE_ESTIMATED`` with no fields at all reads as a stage nobody looked at, when in fact
    somebody looked, the estimation failed, and the failure is recorded — so the fields carry the
    skip evidence and the reader can see there was an attempt.
    """
    status, completing = _status_for(
        stage,
        records=records,
        by_type=by_type,
        case=case,
        signals=signals,
        now=now,
    )

    fields = _fields_for(
        stage, records=records, case=case, signals=signals, intents=intents, figures=figures
    )

    if status is not TimelineStageStatus.DONE or completing is None:
        return TimelineStageProjection(
            stage=stage,
            status=status,
            order=order,
            skip_reason=_skip_reason(stage, status, completing, case),
            fields=fields,
        )

    decision, evidence = _sentences(
        stage,
        completing=completing,
        case=case,
        signals=signals,
        intents=intents,
        figures=figures,
    )
    return TimelineStageProjection(
        stage=stage,
        status=status,
        order=order,
        instant=completing.occurred_at,
        decision_sentence=decision,
        evidence_sentence=evidence,
        fields=fields,
    )


def _skip_reason(
    stage: TimelineStage,
    status: TimelineStageStatus,
    completing: AuditRecordView | None,
    case: CaseView,
) -> str | None:
    """The reason a stage was skipped, named from the persisted Audit_Records (R26.C5).

    An audit event type rather than prose. The record that evidences the skip *is* the reason, and a
    sentence would be a second account of it — one that could disagree with the record it describes
    and one a reader could not grep the audit trail for.

    ``CUSTOMER_RESPONDED`` is the one stage skipped by a condition rather than by a purpose-written
    record: a terminal case with no signal recorded is a customer who never answered, and nothing
    writes an audit record for a thing a person did not do. Its reason is therefore the terminal
    transition qualified by the state it reached — ``STATE_TRANSITION:STOPPED`` — because the bare
    event type would say only that the case moved, and which ending it moved to is the whole of what
    makes the silence explicable.

    ``None`` for every status other than ``SKIPPED``, ``UPCOMING`` included. An ``UPCOMING`` stage
    has no reason because nothing has decided anything about it; inventing one — "not reached" —
    would be prose dressed as evidence, on the field whose entire purpose is to hold evidence.
    """
    if status is not TimelineStageStatus.SKIPPED:
        return None
    if stage is TimelineStage.CUSTOMER_RESPONDED:
        return f"{events.STATE_TRANSITION}:{case.state}"
    return None if completing is None else completing.event_type


def _status_for(
    stage: TimelineStage,
    *,
    records: Sequence[AuditRecordView],
    by_type: Mapping[str, AuditRecordView],
    case: CaseView,
    signals: Sequence[SignalView],
    now: datetime,
) -> tuple[TimelineStageStatus, AuditRecordView | None]:
    """The stage's status and the record that justifies it.

    Returns the record alongside the status rather than only the status, and that pairing is what
    makes P57 checkable: a ``DONE`` returned with ``None`` would be a status with no evidence, and
    the type here makes that expressible only by returning it — which the checks below never do.

    **The order of the checks is the rule.** Completion first, then the stage-specific
    ``IN_PROGRESS``, then skip evidence, then ``UPCOMING``. Completion first is why the
    ``IN_PROGRESS`` table needs no "and no completing record yet" clause per entry, and why a case
    that failed an estimation and then succeeded is ``DONE`` rather than ``SKIPPED`` — the retry
    succeeded, and the failed attempt is history rather than an outcome.
    """
    if stage is TimelineStage.EXECUTED:
        return _executed_status(records=records, by_type=by_type, case=case)
    if stage is TimelineStage.CUSTOMER_RESPONDED:
        return _signal_status(records=records, by_type=by_type, case=case, signals=signals)
    if stage is TimelineStage.OUTCOME_VERIFIED:
        return _outcome_status(records=records, by_type=by_type)

    completing = _first_of(by_type, _COMPLETION_RULES[stage])
    if completing is not None:
        return TimelineStageStatus.DONE, completing

    if stage is TimelineStage.DECIDED:
        return _decided_status(by_type=by_type, case=case, now=now)

    in_progress = _first_of(by_type, _STAGE_IN_PROGRESS_RULES.get(stage, ()))
    if in_progress is not None:
        return TimelineStageStatus.IN_PROGRESS, in_progress

    skipped = _first_of(by_type, _SKIP_RULES.get(stage, ()))
    if skipped is not None:
        return TimelineStageStatus.SKIPPED, skipped

    return TimelineStageStatus.UPCOMING, None


def _first_of(
    by_type: Mapping[str, AuditRecordView], event_types: Sequence[str]
) -> AuditRecordView | None:
    """The earliest record among a set of event types, by sequence number.

    Earliest rather than "the first type in the tuple that matches", because the tuple's order is a
    declaration order and carries no meaning about which happened first. On ``OUTCOME_VERIFIED``'s
    three routes that distinction is reachable: a case can hold both a ``RECOVERY_RECORDED`` and a
    later ``DELAYED_RECOVERY_RECONCILED``, and the stage completed at the first of them.
    """
    candidates = [by_type[event_type] for event_type in event_types if event_type in by_type]
    if not candidates:
        return None
    return min(candidates, key=lambda record: record.seq)


def _decided_status(
    *, by_type: Mapping[str, AuditRecordView], case: CaseView, now: datetime
) -> tuple[TimelineStageStatus, AuditRecordView | None]:
    """``DECIDED`` when there is no ``RECOMMENDATION_RECORDED`` yet (R30.C14).

    The one time-dependent decision in the projection, and the reason ``now`` is a parameter. A case
    resting at ``POLICY_CHECK`` with a persisted ``next_review_at`` in the future and a decision
    cycle counter below the configured bound has a further review permitted, so its ``DECIDED``
    stage is ``IN_PROGRESS`` rather than ``UPCOMING``: something *is* going to happen, at a named
    instant.

    All three conditions are needed and none is redundant, for the same reasons
    ``api.views._waiting_summary`` gives: without the instant check an overdue case claims a future
    appointment, and without the counter check a case at the cap claims a review R30.C10 will
    refuse. The state check is not repeated here because a case not at ``POLICY_CHECK`` has its
    ``next_review_at`` cleared on every edge out of it by ``cases.manager.apply_locked_transition``.

    Returns the ``CASE_REVIEWED`` record as the justifying record where one exists, so the status
    still comes back with evidence. Where no review has happened yet there is no such record and the
    justification is the two case columns — which is why this returns ``None`` there and why
    :func:`_project_stage` renders no sentence for an ``IN_PROGRESS`` stage.
    """
    review_at = case.next_review_at
    if (
        review_at is not None
        and review_at > now
        and case.decision_cycle_count < case.max_recovery_attempts
    ):
        return TimelineStageStatus.IN_PROGRESS, None
    return TimelineStageStatus.UPCOMING, None


def _executed_status(
    *,
    records: Sequence[AuditRecordView],
    by_type: Mapping[str, AuditRecordView],
    case: CaseView,
) -> tuple[TimelineStageStatus, AuditRecordView | None]:
    """``EXECUTED``, keyed on the transition into ``WAITING_FOR_OUTCOME``.

    The transition is written on the fast path and on the reconciliation path alike, so one rule
    covers both and the stage cannot report differently depending on which route confirmed the
    action. The intent row's own state is a presented field (:class:`IntentView`) and takes no part
    in this decision.

    ``EXECUTION_STARTED`` without that transition is ``IN_PROGRESS``: the intent is durable, the
    provider may already have been called, and nothing has come back. That is a real and momentarily
    common state, and it must not read as ``DONE`` — the whole ordering guarantee of
    ``EXECUTION_STARTED`` is that after it exists a call *may* have gone out, which is not the same
    as one having succeeded.
    """
    completing = _transition_into(records, CaseState.WAITING_FOR_OUTCOME)
    if completing is not None:
        return TimelineStageStatus.DONE, completing

    started = _first_of(by_type, _STAGE_IN_PROGRESS_RULES[TimelineStage.EXECUTED])
    if started is not None:
        return TimelineStageStatus.IN_PROGRESS, started

    skipped = _first_of(by_type, _SKIP_RULES[TimelineStage.EXECUTED])
    if skipped is not None:
        return TimelineStageStatus.SKIPPED, skipped

    return TimelineStageStatus.UPCOMING, None


def _signal_status(
    *,
    records: Sequence[AuditRecordView],
    by_type: Mapping[str, AuditRecordView],
    case: CaseView,
    signals: Sequence[SignalView],
) -> tuple[TimelineStageStatus, AuditRecordView | None]:
    """``CUSTOMER_RESPONDED``, keyed on any ``CUSTOMER_SIGNAL_RECORDED`` record.

    On the *record*, not on ``signals`` being non-empty, and the difference matters for P57. Exactly
    one such record is written per accepted signal, in the same transaction as the insert, so the
    record's presence is proof the row exists. Keying on the row instead would make a stage's status
    depend on a table the audit trail does not witness, and the property could no longer be checked
    from an audit sequence alone.

    Skipped where the case is terminal and no signal was recorded: the customer never answered, and
    there is no audit record for something a person did not do. Before the case is terminal the
    absence is ``UPCOMING``, because they still might.
    """
    completing = _first_of(by_type, _COMPLETION_RULES[TimelineStage.CUSTOMER_RESPONDED])
    if completing is not None:
        return TimelineStageStatus.DONE, completing

    if case.state in {state.value for state in TERMINAL_STATES} and not signals:
        return TimelineStageStatus.SKIPPED, _terminal_transition(records)

    return TimelineStageStatus.UPCOMING, None


def _outcome_status(
    *,
    records: Sequence[AuditRecordView],
    by_type: Mapping[str, AuditRecordView],
) -> tuple[TimelineStageStatus, AuditRecordView | None]:
    """``OUTCOME_VERIFIED``: a recovery record, or a transition into any Terminal_State.

    Four completing routes, and the fourth is why this is not a table lookup. A case that ended
    without recovering — stopped, blocked, expired, escalated — has a *verified outcome*: the
    outcome is that it did not recover, and that is as final and as recorded as a capture. Treating
    only the three recovery event types as completion would leave every unrecovered case with a
    permanently ``UPCOMING`` final stage, which reads as "still working on it" on a case nobody is
    working on.

    Earliest of the routes wins, so a case that recovered and was later reconciled shows the instant
    it recovered.
    """
    recovery = _first_of(by_type, _COMPLETION_RULES[TimelineStage.OUTCOME_VERIFIED])
    terminal = _terminal_transition(records)
    candidates = [record for record in (recovery, terminal) if record is not None]
    if candidates:
        return TimelineStageStatus.DONE, min(candidates, key=lambda record: record.seq)

    in_progress = _first_of(by_type, _STAGE_IN_PROGRESS_RULES[TimelineStage.OUTCOME_VERIFIED])
    if in_progress is not None:
        return TimelineStageStatus.IN_PROGRESS, in_progress

    return TimelineStageStatus.UPCOMING, None


# ---------------------------------------------------------------------------
# Sentences and field sets
# ---------------------------------------------------------------------------


def _sentences(
    stage: TimelineStage,
    *,
    completing: AuditRecordView,
    case: CaseView,
    signals: Sequence[SignalView],
    intents: Sequence[IntentView],
    figures: FigureView,
) -> tuple[str, str]:
    """Render a ``DONE`` stage's two sentences from its declared template.

    The variant index is chosen here, at the two stages that declare two, and the choice turns on
    whether a record exists — never on what a value is. Passing the index into
    :func:`~revora.timeline.templates.render` rather than assembling a sentence means the wording
    stays entirely in ``templates.py`` and this function only decides *which declared sentence*
    applies.
    """
    variant = _variant_for(stage, figures)
    template = STAGE_TEMPLATES[stage][variant]
    substitutions = _substitutions(
        stage,
        variant=variant,
        completing=completing,
        case=case,
        signals=signals,
        intents=intents,
        figures=figures,
    )
    return (
        render(template.decision, substitutions[0]),
        render(template.evidence, substitutions[1]),
    )


def _variant_for(stage: TimelineStage, figures: FigureView) -> int:
    """Which of a stage's declared template variants applies.

    Two stages have two, and each choice is a presence test:

    ``BASELINE_ESTIMATED`` — variant 0 where an uncertainty interval was recorded, variant 1 where
    R26.C4's ``UNCERTAINTY_UNAVAILABLE`` applies. An interval that does not exist is not rendered as
    a wide one.

    ``OUTCOME_VERIFIED`` — variant 0 where a recovered amount and a classification were recorded,
    variant 1 where the case ended for another reason. Not a fallback: an ended case has a different
    sentence to say, and saying the recovery sentence with *not recorded* substituted into it would
    be the wrong sentence rather than an incomplete one.
    """
    if stage is TimelineStage.BASELINE_ESTIMATED:
        return 0 if figures.baseline_interval is not None else 1
    if stage is TimelineStage.OUTCOME_VERIFIED:
        return 0 if figures.recovered_amount_formatted is not None else 1
    return 0


def _substitutions(
    stage: TimelineStage,
    *,
    variant: int,
    completing: AuditRecordView,
    case: CaseView,
    signals: Sequence[SignalView],
    intents: Sequence[IntentView],
    figures: FigureView,
) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """The exact substitution sets for a stage's two sentences.

    Every value is a ``str`` before it gets here, so no formatting decision — no rounding, no
    thousands separator, no percentage conversion — can be taken at substitution time. Money is the
    server's formatted string; a probability and a confidence are the ``Decimal``'s own string form,
    which is what the rest of the dashboard renders too.

    Returned as a pair of mappings rather than one shared mapping, so a placeholder that exists only
    in the evidence sentence cannot be quietly satisfied by a value computed for the decision
    sentence — :func:`~revora.timeline.templates.render` refuses a surplus key, and a shared mapping
    would make every key surplus somewhere.
    """
    intent = intents[-1] if intents else None
    latest_signal = signals[-1] if signals else None

    if stage is TimelineStage.DETECTED:
        return (
            {"amount": _text(figures.payment_amount_formatted)},
            {"provider_payment_id": _text(case.provider_payment_id or None)},
        )

    if stage is TimelineStage.DIAGNOSED:
        return (
            {
                "cause": _label(labelled(CAUSE_LABELS, figures.cause)),
                "confidence": _text(figures.confidence),
            },
            {
                "evidence_source": labelled(
                    EVIDENCE_SOURCE_LABELS, figures.evidence_source
                ).label,
                "method": labelled(DIAGNOSIS_METHOD_LABELS, figures.diagnosis_method).label,
            },
        )

    if stage is TimelineStage.BASELINE_ESTIMATED:
        decision = {"probability": _text(figures.baseline_probability)}
        if variant == 0:
            return decision, {"interval": _text(figures.baseline_interval)}
        return decision, {"uncertainty": UNCERTAINTY_UNAVAILABLE}

    if stage is TimelineStage.ALTERNATIVES_PRICED:
        return (
            {
                "priced_count": _count(figures.priced_count),
                "unavailable_count": _count(figures.unavailable_count),
            },
            {
                "cheapest_total_action_cost": _text(
                    figures.cheapest_total_action_cost_formatted
                )
            },
        )

    if stage is TimelineStage.DECIDED:
        return (
            {
                "action": _label(labelled(ACTION_LABELS, figures.selected_action)),
                "net_recovery_value": _text(figures.net_recovery_value_formatted),
                "runner_up": _label(labelled(ACTION_LABELS, figures.runner_up_action)),
                "runner_up_value": _text(figures.runner_up_value_formatted),
            },
            {
                "selection_reason": labelled(
                    SELECTION_REASON_LABELS, figures.selection_reason
                ).label
            },
        )

    if stage is TimelineStage.POLICY_CHECKED:
        return (
            {"verdict": _label(labelled(POLICY_VERDICT_LABELS, figures.policy_verdict))},
            {
                "primary_reason": labelled(
                    POLICY_REASON_LABELS, figures.policy_primary_reason
                ).label
            },
        )

    if stage is TimelineStage.EXECUTED:
        return (
            {
                "action": _label(
                    labelled(ACTION_LABELS, None if intent is None else intent.action)
                ),
                "instant": completing.occurred_at.isoformat(),
            },
            {"intent_state": _text(None if intent is None else intent.state)},
        )

    if stage is TimelineStage.CUSTOMER_RESPONDED:
        return (
            {
                "signal_kind": labelled(
                    SIGNAL_KIND_LABELS, None if latest_signal is None else latest_signal.kind
                ).label,
                "delay_reason": labelled(
                    DELAY_REASON_LABELS,
                    None if latest_signal is None else latest_signal.delay_reason,
                ).label,
            },
            {
                "instant": (
                    completing.occurred_at.isoformat()
                    if latest_signal is None
                    else latest_signal.submitted_at.isoformat()
                )
            },
        )

    if variant == 0:
        return (
            {
                "amount": _text(figures.recovered_amount_formatted),
                "outcome_class": _label(
                    labelled(OUTCOME_CLASS_LABELS, figures.outcome_classification)
                ),
            },
            {
                "instant": (
                    figures.outcome_verified_at or completing.occurred_at
                ).isoformat()
            },
        )
    return (
        {
            "terminal_reason": _label(
                labelled(TERMINAL_REASON_LABELS, case.terminal_reason)
                if case.terminal_reason is not None
                else labelled(TERMINAL_STATE_LABELS, case.state)
            )
        },
        {"instant": completing.occurred_at.isoformat()},
    )


def _label(value: Labelled) -> str:
    """A label with its stored member in brackets, or the label alone where there is none.

    ``Waiting and watching (WAIT)``. Both halves in one string because this is substituted into a
    sentence and a sentence cannot hold a structured pair — the structured pair travels separately
    in the stage's ``fields``, so a client that wants to render them apart can, and a client
    rendering the sentence still shows the reader the stored value (R26.C14).

    The bracket is omitted where the member is empty, which is :data:`NOT_RECORDED`'s case. ``Not
    recorded ()`` would be a worse sentence than ``Not recorded``.
    """
    if not value.member:
        return value.label
    return f"{value.label} ({value.member})"


def _text(value: str | None) -> str:
    """A persisted string, or the named absence. Never an empty string and never a zero."""
    return _NOT_RECORDED_VALUE if value is None or value == "" else value


def _count(value: int | None) -> str:
    """A recorded count as text, or the named absence.

    ``0`` is a real count and passes through as ``"0"``. An absent count becomes the words, because
    "no candidates were priced" and "we have no record of pricing" are different statements and the
    ``ALTERNATIVES_PRICED`` stage is where they are most easily confused.
    """
    return _NOT_RECORDED_VALUE if value is None else str(value)


def _fields_for(
    stage: TimelineStage,
    *,
    records: Sequence[AuditRecordView],
    case: CaseView,
    signals: Sequence[SignalView],
    intents: Sequence[IntentView],
    figures: FigureView,
) -> Mapping[str, object]:
    """The per-stage field set R26.C4 enumerates, as structured values beside the sentences.

    Structured *and* in the sentences, which is deliberate duplication. The sentence is what a
    reviewer with two minutes reads; the field set is what a reviewer who disagrees with the
    sentence checks — each label paired with its stored member, each money figure the server's own
    formatted string, each count a count. A client can render either or both, and neither is derived
    from the other in the browser.

    Every currency value here is a formatted string, never minor units, so there is nothing on this
    wire for a browser to do arithmetic on (R26.C8).
    """
    if stage is TimelineStage.DETECTED:
        return {
            "payment_amount": figures.payment_amount_formatted,
            "provider_payment_id": case.provider_payment_id or None,
            "detected_at": case.detected_at.isoformat(),
        }

    if stage is TimelineStage.DIAGNOSED:
        return {
            "cause": labelled(CAUSE_LABELS, figures.cause).as_document(),
            "confidence": figures.confidence,
            "method": labelled(DIAGNOSIS_METHOD_LABELS, figures.diagnosis_method).as_document(),
            "evidence_source": labelled(
                EVIDENCE_SOURCE_LABELS, figures.evidence_source
            ).as_document(),
        }

    if stage is TimelineStage.BASELINE_ESTIMATED:
        return {
            "baseline_recovery_probability": figures.baseline_probability,
            # R26.C4 names the token as the value where no interval was recorded, so the token is
            # what travels — not `None`, which a client would render as an empty cell and a reader
            # would take for a narrow interval nobody printed.
            "uncertainty_interval": figures.baseline_interval or UNCERTAINTY_UNAVAILABLE,
        }

    if stage is TimelineStage.ALTERNATIVES_PRICED:
        return {
            "priced_count": figures.priced_count,
            "unavailable_count": figures.unavailable_count,
            "cheapest_total_action_cost": figures.cheapest_total_action_cost_formatted,
        }

    if stage is TimelineStage.DECIDED:
        return {
            "selected_action": labelled(ACTION_LABELS, figures.selected_action).as_document(),
            "net_recovery_value": figures.net_recovery_value_formatted,
            "selection_reason": labelled(
                SELECTION_REASON_LABELS, figures.selection_reason
            ).as_document(),
            "runner_up_action": labelled(ACTION_LABELS, figures.runner_up_action).as_document(),
            "runner_up_net_recovery_value": figures.runner_up_value_formatted,
            # R30.C14. Every review, with its trigger and both actions, at this stage.
            "reviews": _reviews(records),
            "decision_cycle_count": case.decision_cycle_count,
            "max_recovery_attempts": case.max_recovery_attempts,
            "next_review_at": (
                None if case.next_review_at is None else case.next_review_at.isoformat()
            ),
            "selected_action_is_null_action": (
                figures.selected_action in {action.value for action in NULL_ACTIONS}
            ),
        }

    if stage is TimelineStage.POLICY_CHECKED:
        return {
            "verdict": labelled(POLICY_VERDICT_LABELS, figures.policy_verdict).as_document(),
            "primary_reason": labelled(
                POLICY_REASON_LABELS, figures.policy_primary_reason
            ).as_document(),
        }

    if stage is TimelineStage.EXECUTED:
        return {
            "attempts": [
                {
                    "action": labelled(ACTION_LABELS, intent.action).as_document(),
                    "intent_state": intent.state,
                    "instant": intent.attempt_started_at.isoformat(),
                }
                for intent in intents
            ]
        }

    if stage is TimelineStage.CUSTOMER_RESPONDED:
        return {
            "signals": [
                {
                    "kind": labelled(SIGNAL_KIND_LABELS, signal.kind).as_document(),
                    "delay_reason": labelled(
                        DELAY_REASON_LABELS, signal.delay_reason
                    ).as_document(),
                    "submitted_at": signal.submitted_at.isoformat(),
                }
                for signal in signals
            ]
        }

    return {
        "recovered_amount": figures.recovered_amount_formatted,
        "outcome_classification": labelled(
            OUTCOME_CLASS_LABELS, figures.outcome_classification
        ).as_document(),
        "terminal_state": (
            None
            if case.state not in {state.value for state in TERMINAL_STATES}
            else labelled(TERMINAL_STATE_LABELS, case.state).as_document()
        ),
        "terminal_reason": (
            None
            if case.terminal_reason is None
            else labelled(TERMINAL_REASON_LABELS, case.terminal_reason).as_document()
        ),
    }


def _reviews(records: Sequence[AuditRecordView]) -> list[dict[str, object]]:
    """Every ``CASE_REVIEWED`` record, at the ``DECIDED`` stage, in sequence order (R30.C14).

    Every one of them, not the latest. A case reviewed three times chose restraint three times, and
    the sequence of those choices is the answer to the question R30 exists to make askable — *how
    often does restraint get revisited* — which a single most-recent row cannot give.

    Both actions travel, and the previous one is what makes the row worth reading. A review that
    re-selected ``WAIT`` produces no state visible anywhere else: same state, same selected action,
    one more decision cycle spent. Presenting only the new selection would render "we re-examined
    and still think waiting is right" identically to "we never looked", and those are the product
    working and the defect R30 was written about.

    Labelled, so the two null actions read as *"Waiting and watching"* with their stored member
    beside them (R26.C14) — a reviewer seeing ``DO_NOTHING`` in a review history and ``WAIT`` in the
    next row would read a change of course where the two mean the same thing.
    """
    return [
        {
            "sequence": record.seq,
            "reviewed_at": record.occurred_at.isoformat(),
            "review_trigger": record.review_trigger,
            "previous_selected_action": labelled(
                ACTION_LABELS, record.previous_selected_action
            ).as_document(),
            "new_selected_action": labelled(
                ACTION_LABELS, record.new_selected_action
            ).as_document(),
        }
        for record in _ordered(records)
        if record.event_type == events.CASE_REVIEWED
    ]


# ---------------------------------------------------------------------------
# Sequence integrity (R26.C11)
# ---------------------------------------------------------------------------


def sequence_integrity(records: Sequence[AuditRecordView]) -> SequenceIntegrity:
    """Detect a gap in the case's Audit_Record sequence and name the missing numbers.

    ``max(seq) - min(seq) + 1`` against the record count, and the missing numbers listed on a
    mismatch. The design's rule verbatim, and it is a cheap check on a set that is already in memory
    — the alternative, asking the database for the expected count, would be a second read whose
    answer could disagree with the rows the timeline was actually built from.

    A duplicate ``seq`` cannot arrive: ``UNIQUE (case_id, seq)`` refuses it in the schema. The count
    is taken over the distinct numbers anyway, because if one ever did arrive, counting it twice
    would mask a real gap — the arithmetic would balance and the banner would not appear, which is
    the one outcome this function must not produce.

    An empty record set is reported as complete. A case with no audit records at all is not a case
    with a gap; it is a case whose ``CASE_DETECTED`` record has not been read, and the nine stages
    are then all ``UPCOMING``, which says that already. Reporting a gap there would put a banner
    about a bypassed allocation on the most ordinary page in the system.
    """
    numbers = sorted({record.seq for record in records})
    if not numbers:
        return SequenceIntegrity(
            complete=True,
            record_count=0,
            first_seq=None,
            last_seq=None,
            missing=(),
            starts_at_one=False,
        )

    first, last = numbers[0], numbers[-1]
    expected = last - first + 1
    missing = tuple(number for number in range(first, last + 1) if number not in set(numbers))
    return SequenceIntegrity(
        complete=len(numbers) == expected,
        record_count=len(numbers),
        first_seq=first,
        last_seq=last,
        missing=missing,
        starts_at_one=first == 1,
    )

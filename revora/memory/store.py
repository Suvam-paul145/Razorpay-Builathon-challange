"""Recovery memory: one observation per case, written when the case ends.

This is the table the baseline estimator learns from, and it is currently empty — which is
why every baseline in the system is the uniform prior with a `[0.025, 0.975]` interval. The
estimator has been reading this table since Phase 2 and finding nothing. Nothing about that is
broken; a system with no history genuinely does not know its own recovery rate, and the wide
interval says so. This module is what starts closing that gap.

**Written inside the terminal transition, not after it.** R15.C1 is explicit, and the reason is
not tidiness. A case reaching a terminal state and an observation recording that outcome are
one fact. Written in a follow-on job they become two, and a crash between them leaves a
resolved case with no observation — permanently, because nothing re-visits a terminal case.
The training set would then be silently biased toward whatever survives crashes, and the bias
would be undetectable because the missing rows leave no trace. Sharing the transaction makes
the two facts inseparable.

**The layering forces the wiring, and the wiring is worth understanding.**
``revora.cases`` sits *below* ``revora.memory``, so the case manager cannot import this
module — the import contract forbids it. That is not an obstacle to work around: the case
manager has no business knowing what a training label is. So the write is injected as the
``on_success`` callback ``apply_locked_transition`` already accepts, supplied by a caller in a
layer above both. :func:`observation_writer` builds that callback.

**The label is the point, not the outcome.** ``intervention_status`` decides whether a row may
be used as a baseline training label at all, and only ``NO_INTERVENTION_CONFIRMED`` may. That
status means "no Revora action and no *recorded* merchant action" — which is weaker than "no
intervention", because Revora cannot see a merchant phoning a customer. The weakness is
labelled rather than solved: ``MERCHANT_INTERVENTION_UNKNOWN`` is a distinct value, its share
per segment is reported, and it is excluded from the posterior. A design that folded unknown
into confirmed would produce a baseline that looked better-evidenced and was quietly wrong.

**The feature document is an interface, and a fragile one.** The five *segment* keys must be
exactly what ``estimation.segments.SegmentFeatures.as_values()`` produces, because the estimator
matches segments by JSONB containment. A renamed key does not raise — it silently stops
matching, every segment collapses to the global prior, and nothing looks broken. So the
document is built by calling that function rather than by assembling a dict here.

**One non-segment observation feature exists, and the distinction matters.** R15.C1 lets an
observation carry features about what happened to a case; R22.C6 requires a Partial_Arrangement_
Request to be one of them. It is added under
:data:`~revora.customer.arrangements.FEATURE_PARTIAL_ARRANGEMENT`, alongside the five and
deliberately not among them: the estimator's backoff levels are truncations of one ordered tuple,
so a sixth *segment* dimension would resegment the whole training set, while a key no containment
probe ever names changes no estimate at all. Its value is a nested object, which a probe built
from string values cannot match even by accident — so "adding this cannot move a baseline" is
structural rather than a promise. The key is present only on the cases that have a request, which
is why the pg test asserting the exact five-key set on an expired case still holds.

**Customer signals live here, and that is what puts them out of the Policy_Engine's reach**
(R25.C1, R25.C6). R15.C6 already forbids the Policy_Engine from deriving any check outcome, any
threshold or any configured bound from Recovery_Memory. So the coverage of a Delay_Reason, a
Promise_Status, an arrangement indication and a signal count is not a second prohibition that
somebody has to remember to write — it follows from where the fields are stored, and the import
contract is what makes it structural: ``policy-isolation`` forbids ``revora.policy`` from
importing ``revora.memory`` *or* ``revora.persistence``, so the policy engine has no session, no
repository and no ORM model with which to read an observation. The one Customer_Signal
consequence policy does read is the persisted ``contact_suppression`` row, which reaches it
through its own declared input (R21.C3, check 5) and not through this table. Stated here rather
than left implied, because "covered automatically" is the kind of claim that is true until an
exemption gets added for convenience.

**Two flat keys and one nested document, and the split is R25.C3 against R15.C1.** R25.C3 asks
for the Delay_Reason and the Promise_Status to be *selectable as distinct segments*, and the only
thing that makes a value selectable in this table is JSONB containment — so those two are flat
string keys beside the five, matchable as ``features @> '{"delay_reason": "..."}'``. They are
still not *segment dimensions*: :data:`~revora.domain.segments.FEATURE_KEYS` is unchanged, so
``backoff_candidates`` never names them and no baseline moves because they exist. Everything else
R25.C1 requires — the promise-to-payment interval, the arrangement indication, the suppression
indication, the signal count — is a fact about what happened rather than a segment anybody
selects on, so it goes in one nested :data:`FEATURE_CUSTOMER_SIGNALS` document on
:data:`FEATURE_PARTIAL_ARRANGEMENT`'s terms: a containment probe built from string values cannot
match a nested object even by accident.

All three keys are absent from a case that holds no signal and no suppression, which keeps the
document of an ordinary case byte-identical to what it was before this feature existed — and
keeps the pg test asserting the exact five-key set on an expired case honest rather than widened.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from revora.audit.events import RECOVERY_OBSERVATION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.customer.arrangements import arrangement_feature, first_arrangement_request
from revora.customer.suppression import suppression_in_force
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    NOT_ESTABLISHED,
    CaseState,
    DecisionSource,
    DelayReason,
    ExperimentGroup,
    IntentState,
    InterventionStatus,
    OutcomeClass,
    PromiseStatus,
    Provenance,
    RiskCause,
)
from revora.domain.money import Minor
from revora.domain.segments import FEATURE_KEYS, SegmentFeatures
from revora.domain.transitions import is_terminal
from revora.persistence.models import MemoryObservation
from revora.persistence.repositories.cases import WebhookEventRepository
from revora.persistence.repositories.customer import (
    CustomerSignalRepository,
    PromiseToPayRepository,
)
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.memory import MemoryObservationRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.platform.clock import ensure_utc
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

    from revora.customer.arrangements import ArrangementRequest
    from revora.persistence.models import ExecutionIntent, PolicyDecision, RecoveryCase
    from revora.platform.config import Configuration

__all__ = [
    "FEATURE_CUSTOMER_SIGNALS",
    "FEATURE_DELAY_REASON",
    "FEATURE_PROMISE_STATUS",
    "SIGNAL_SEGMENT_KEYS",
    "CustomerSignalFacts",
    "ObservationOutcome",
    "classify_intervention_status",
    "observation_writer",
    "read_customer_signal_facts",
    "record_observation",
]

_logger = get_logger(__name__)

_ACTOR = "recovery_memory"


FEATURE_DELAY_REASON: Final[str] = "delay_reason"
"""The flat ``features`` key holding the recorded Delay_Reason (R25.C1, R25.C3).

Flat and string-valued *because* R25.C3 asks for each Delay_Reason value to be selectable as a
distinct segment, and containment against a string subset is the only selection this table
supports. Spelled the same as ``revora.diagnosis.service.EVIDENCE_STATED_REASON`` by coincidence
of the domain word, not by dependency — the two documents are unrelated and neither reads the
other.

Absent, never null, on a case that stated no reason. A present-but-null key is a key a
containment probe can match on, and "the customer said nothing" has no value to record."""

FEATURE_PROMISE_STATUS: Final[str] = "promise_status"
"""The flat ``features`` key holding the Promise_Status (R25.C1, R25.C3). Absent where the case
holds no Promise_To_Pay, on :data:`FEATURE_DELAY_REASON`'s terms."""

FEATURE_CUSTOMER_SIGNALS: Final[str] = "customer_signals"
"""The nested ``features`` key holding the rest of R25.C1.

Nested for the reason :data:`~revora.customer.arrangements.FEATURE_PARTIAL_ARRANGEMENT` is: the
five segment keys share a namespace the estimator probes by containment, and while a probe built
from :data:`~revora.domain.segments.FEATURE_KEYS` would never name a count or a boolean, "would
never" is a promise and a namespace is a fact. A nested object cannot match
``features @> '{"...": "..."}'`` at all, so none of these can become a segment dimension by
accident."""

SIGNAL_SEGMENT_KEYS: Final[tuple[str, ...]] = (
    FEATURE_DELAY_REASON,
    FEATURE_PROMISE_STATUS,
)
"""The two keys R25.C3 makes selectable, as a tuple a caller can iterate.

Exported so the composition report and the cohort report name the same two keys this writer
writes, rather than each spelling them for itself — the same drift argument that has
``SegmentFeatures.as_values()`` build the five."""

_TRAINING_LABEL_SEGMENT_KEYS: Final[frozenset[str]] = frozenset(FEATURE_KEYS)
"""The five keys the estimator's backoff actually probes.

Held here only so :func:`_signal_features` can assert it is not colliding with one of them. A
new signal feature named ``risk_cause`` would silently overwrite the segment value and move every
baseline in the merchant's history, and that failure has no other detector."""

_ACTED_INTENT_STATES: Final[frozenset[IntentState]] = frozenset(
    {IntentState.ATTEMPTED, IntentState.CONFIRMED, IntentState.UNCERTAIN}
)
"""The intent states that mean an external effect may exist — R25.C4's "a Revora action".

``FAILED`` is excluded and the other three are included, and the asymmetry is the same
conservatism :func:`classify_intervention_status` is built on. ``FAILED`` is the one state that
means *definitely nothing landed*: a connect-phase failure or a parseable provider rejection, so a
signal arriving after one cannot have been a response to it. The other three all admit the
possibility that a customer received something — ``CONFIRMED`` because the provider acknowledged
it, ``UNCERTAIN`` because nobody knows, and ``ATTEMPTED`` because the call may be in flight — and
the cost of the two readings is wildly asymmetric. Counting an intervened case as
no-intervention biases the baseline downward and flatters every incremental claim built on it;
declining to count a genuinely untreated case costs one training label out of a set that is
already labelled as too small."""


@dataclass(frozen=True, slots=True)
class CustomerSignalFacts:
    """Everything R25.C1 asks the observation to carry about what a customer said.

    Read once, in the terminal transition, and used for three things that must not be able to
    disagree: the feature document, the audit record, and the ``REVORA_INTERVENED`` decision of
    R25.C4. One read means there is no second query to drift — the alternative, asking each
    consumer to look the facts up for itself, is how an observation ends up recording an
    arrangement request that its own audit record denies.
    """

    signal_count: int
    """The count of persisted Customer_Signals (R25.C1). Counted in SQL rather than taken as the
    length of :attr:`latest_signal_at`'s source list, because the list is read under
    ``MAX_CUSTOMER_SIGNALS_PER_CASE`` and a truncated list would silently report a smaller
    number than the case holds."""

    delay_reason: DelayReason | None
    """The recorded Delay_Reason, or ``None``. The *latest*, matching
    :meth:`~revora.persistence.repositories.customer.CustomerSignalRepository.latest_delay_reason`
    and R20.C4's input — a customer who states a second reason has corrected the first, and an
    observation labelled with the superseded one would train on a correction nobody made."""

    promise_status: PromiseStatus | None
    promise_seconds_to_payment: int | None
    """``promise_to_pay.seconds_promise_to_payment`` where both the Promise_Date and the outcome
    instant exist (R25.C1). Read off the column R23.C10 already writes rather than recomputed
    here: the interval is measured against the *provider-reported* payment timestamp, which this
    module does not hold, and a second derivation from a different instant would produce a
    plausible number that disagrees with the promise row."""

    arrangement: ArrangementRequest | None
    contact_suppressed: bool
    """Whether a Contact_Suppression covers this case's Suppression_Scope (R25.C1).

    Scope-keyed rather than case-keyed, because that is what "covers the Recovery_Case" means
    under R21.C8: a suppression written by a *sibling* case of the same customer and order
    suppresses contact on this one too, and an observation that denied it would describe a case
    Revora was free to chase when it was not."""

    any_synthetic: bool
    """Whether any Customer_Signal on the case carries ``SYNTHETIC`` provenance (R25.C2).

    One is enough, applying R15.C2 unchanged. A generated signal contributing to an observation
    makes that observation synthetic however real the payment behind it was."""

    signal_after_action: bool
    """Whether any Customer_Signal arrived at or after the first Revora action (R25.C4).

    At or after, not strictly after. The comparison is between a submission instant the customer
    caused and an attempt instant Revora caused, and an exact tie is a clock collision rather
    than evidence that the customer moved first — so the tie resolves toward "this is not a
    no-intervention observation", which is the direction that cannot overstate what the baseline
    knows."""

    first_action_at: datetime | None
    latest_signal_at: datetime | None

    @property
    def has_content(self) -> bool:
        """Whether this case has anything to record under R25.C1.

        A suppression with no signal on *this* case is content, for the reason
        :attr:`contact_suppressed` gives. Everything else implies a signal, so the two
        conditions between them cover the requirement — and a case with neither writes none of
        the three keys, which is what keeps an ordinary observation's document unchanged.
        """
        return self.signal_count > 0 or self.contact_suppressed

    def as_document(self) -> dict[str, object]:
        """The nested :data:`FEATURE_CUSTOMER_SIGNALS` value, and the audit record's copy.

        One function for both, so the observation and the audit record explaining it cannot
        describe different cases. Instants are ISO-8601 strings because this lands in JSONB and
        a reader comparing two observations needs a form that sorts.
        """
        return {
            "signal_count": self.signal_count,
            "delay_reason": None if self.delay_reason is None else self.delay_reason.value,
            "promise_status": (
                None if self.promise_status is None else self.promise_status.value
            ),
            "seconds_promise_to_payment": self.promise_seconds_to_payment,
            "arrangement_requested": self.arrangement is not None,
            "contact_suppressed": self.contact_suppressed,
            "signal_after_revora_action": self.signal_after_action,
            "provenance": (
                Provenance.SYNTHETIC.value if self.any_synthetic else Provenance.REAL.value
            ),
            "first_revora_action_at": (
                None if self.first_action_at is None else self.first_action_at.isoformat()
            ),
            "latest_signal_at": (
                None if self.latest_signal_at is None else self.latest_signal_at.isoformat()
            ),
        }


@dataclass(frozen=True, slots=True)
class ObservationOutcome:
    """What one observation write did.

    ``already_recorded`` is not a failure. A terminal transition can be retried — the
    reconciliation path can move a case to ``RECOVERED`` after it ended for another reason —
    and the unique constraint on ``case_id`` is what makes the second attempt harmless.
    """

    observation_id: uuid.UUID | None
    already_recorded: bool = False
    skipped_reason: str | None = None

    @property
    def written(self) -> bool:
        return self.observation_id is not None and not self.already_recorded


def classify_intervention_status(
    *,
    confirmed_actions: int,
    group: ExperimentGroup | None,
    signal_after_action: bool = False,
) -> InterventionStatus:
    """Decide whether this case's outcome may serve as a baseline training label.

    The rule is deliberately conservative in one direction only.

    ``REVORA_INTERVENED`` whenever at least one action was confirmed. Not "was scheduled" and
    not "was attempted" — *confirmed*, meaning the provider acknowledged the effect exists. An
    attempt whose outcome is still unknown must not be counted as an intervention, because if
    it never landed the case really was untreated; equally it must not be counted as
    no-intervention, which is why an unresolved case is not observed at all until it resolves.

    ``REVORA_INTERVENED`` **also** where a Customer_Signal arrived at or after a Revora action,
    even with no confirmed action at all (R25.C4). That clause exists for one situation and it
    is worth naming precisely, because otherwise it looks redundant beside the count above: an
    intent that reached ``ATTEMPTED`` or ``UNCERTAIN`` and never resolved. The confirmed count
    is zero, so the old rule would have let a control-arm case of that shape become a training
    label — while the customer's own submission is direct evidence the message landed. A
    customer who responded to something Revora sent is not an observation of what happens when
    Revora does nothing, and here the response is the strongest evidence available that the
    unresolved attempt did in fact reach a person. See :data:`_ACTED_INTENT_STATES` for which
    states count as "a Revora action" and why ``FAILED`` does not.

    ``NO_INTERVENTION_CONFIRMED`` requires zero confirmed actions, no post-action signal, **and**
    a control-arm assignment. The last condition is the part that gets omitted by mistake, and
    omitting it is what makes a baseline dishonest: a treatment case that happened to receive no
    action — because policy blocked it, or the window closed first — is *not* evidence about what
    happens without intervention. It is evidence about cases Revora declined to treat, which
    is a different and selected population. Treating those as baseline labels is the classic
    selection bias, and it would bias the baseline in the direction that flatters every
    subsequent incremental claim.

    Everything else is ``MERCHANT_INTERVENTION_UNKNOWN``: an unassigned case, where Revora did
    nothing but has no basis to claim nobody else did either.

    Args:
        signal_after_action: defaulted to ``False`` rather than made required, because the
            answer for a case with no signals and no actions is ``False`` and every caller that
            has not been taught to read signals would have to pass it. The default is the
            *permissive* value, which is the one place this signature is not conservative — so
            the pg test that a post-action signal disqualifies a control case is what stands
            behind the clause rather than the type checker.
    """
    if confirmed_actions > 0 or signal_after_action:
        return InterventionStatus.REVORA_INTERVENED
    if group is ExperimentGroup.CONTROL:
        return InterventionStatus.NO_INTERVENTION_CONFIRMED
    return InterventionStatus.MERCHANT_INTERVENTION_UNKNOWN


def record_observation(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
) -> ObservationOutcome:
    """Flatten one resolved case into a training observation, on the caller's session.

    Commits nothing. The caller's transaction is the terminal transition's transaction, which
    is the whole point — see the module docstring.

    Args:
        case: the case row, already held ``FOR UPDATE`` and already moved to its terminal
            state by the caller. Read after the transition so the counters and the terminal
            reason on the row are the final ones.

    Returns:
        An :class:`ObservationOutcome`. A non-terminal case is skipped rather than raising: the
        callback is attached to every transition and only terminal ones produce an
        observation, so "not terminal" is the ordinary case, not an error.
    """
    state = CaseState(case.state)
    if not is_terminal(state):
        return ObservationOutcome(None, skipped_reason="case is not terminal")

    observations = MemoryObservationRepository(session)
    existing = observations.for_case(merchant_id, case.id)
    if existing is not None:
        # A terminal case can transition again — reconciliation moves an EXPIRED case to
        # RECOVERED on a verified capture. The observation belongs to the case, not to the
        # transition, so the first one stands.
        return ObservationOutcome(existing.id, already_recorded=True)

    case_id = case.id
    # Not ``case.decision_cycle_count`` — see ``active_decision_cycle``. The counter is one
    # ahead of the cycle the diagnosis and the policy decision are filed under, and reading it
    # here produced an observation whose cause, confidence, method and verdict were all NULL:
    # a training label that cannot say why the action was taken, on a case where the reason was
    # recorded in full.
    cycle = RecommendationRepository(session).active_decision_cycle(
        merchant_id, case_id, fallback=int(case.decision_cycle_count)
    )

    diagnosis = DiagnosisRepository(session).active_for_cycle(merchant_id, case_id, cycle)
    decisions = PolicyDecisionRepository(session).for_cycle(merchant_id, case_id, cycle)
    intents = list(ExecutionIntentRepository(session).list_for_case(merchant_id, case_id))
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case_id)
    assignment = observations.assignment_for_case(merchant_id, case_id)

    confirmed = [
        intent for intent in intents if IntentState(intent.state) is IntentState.CONFIRMED
    ]
    group = None if assignment is None else ExperimentGroup(str(assignment.group))

    # The five-key feature document, built by the estimator's own function so a key rename
    # cannot drift between writer and reader.
    canonical_method, canonical_error_source = _payment_facts(
        session, merchant_id, case.source_event_id
    )
    features = SegmentFeatures.derive(
        risk_cause=RiskCause(diagnosis.cause) if diagnosis is not None else RiskCause.UNKNOWN,
        amount=Minor(int(case.payment_amount)),
        payment_method=canonical_method,
        # The count *before* any action, which is what the estimate was conditioned on.
        executed_action_count=0,
        error_source=canonical_error_source,
    )
    # R25.C1: read once, in this transaction, and used by the feature document, the audit
    # record and the training-label decision below. See :class:`CustomerSignalFacts`.
    signals = read_customer_signal_facts(
        session, merchant_id, case, intents=intents, config=config
    )

    feature_document: dict[str, object] = dict(features.as_values())
    feature_document.update(_signal_features(signals))

    row = MemoryObservation(
        case_id=case_id,
        features=feature_document,
        cause=None if diagnosis is None else str(diagnosis.cause),
        confidence=None if diagnosis is None else diagnosis.confidence,
        diagnosis_method=None if diagnosis is None else str(diagnosis.method),
        selected_action=_selected_action(confirmed, decisions),
        policy_verdict=None if not decisions else str(decisions[-1].verdict),
        # NOT_ESTABLISHED, never NULL and never a guess. "We did not measure a recovery" and
        # "we measured and there was none" are different statements, and the estimator counts
        # only the recognised outcome classes as successes — so a resolved-but-unrecovered
        # case has to say so explicitly or it silently leaves the denominator too.
        outcome_class=(
            outcome.classification if outcome is not None else NOT_ESTABLISHED
        ),
        realized_cost=_realized_cost(confirmed),
        group=None if group is None else group.value,
        executed_action_count=int(case.executed_action_count),
        customer_message_count=int(case.customer_message_count),
        decision_source=_decision_source(case, group),
        intervention_status=classify_intervention_status(
            confirmed_actions=len(confirmed),
            group=group,
            signal_after_action=signals.signal_after_action,
        ).value,
        # One synthetic contributor is enough to make every figure built on it synthetic
        # (R15.C2), and R25.C2 adds Customer_Signals to the set of contributors. So the case's
        # own provenance propagates verbatim and a synthetic signal can only ever widen it —
        # a real case cannot be made real again by a generated submission arriving on it.
        provenance=_observation_provenance(case, signals),
        trained_into_model_version_id=None,
    )
    observations.add(merchant_id, row)
    session.flush()

    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=RECOVERY_OBSERVATION_RECORDED,
            actor=_ACTOR,
            new_state=state.value,
            action=row.selected_action,
            decision={
                "intervention_status": row.intervention_status,
                "outcome_class": row.outcome_class,
                "usable_as_baseline_label": (
                    row.intervention_status
                    == InterventionStatus.NO_INTERVENTION_CONFIRMED.value
                ),
                "group": row.group,
                "provenance": row.provenance,
                "realized_cost": int(row.realized_cost),
                "segment_features": dict(row.features),
                # R25.C1's fields again, as their own block rather than only inside the
                # feature document. A reader auditing why this observation was or was not a
                # training label needs the post-action-signal answer beside
                # ``intervention_status``, not nested three levels down in a features blob.
                "customer_signals": signals.as_document(),
                "signal_disqualified_training_label": signals.signal_after_action,
            },
        ),
        correlation_id=correlation_id,
    )

    return ObservationOutcome(row.id)


def observation_writer(
    config: Configuration, *, correlation_id: uuid.UUID | None = None
) -> Callable[[Session, RecoveryCase], None]:
    """Build the ``on_success`` callback that records an observation in the transition.

    This function exists because of the import contract, and the contract is right. The case
    manager may not import ``revora.memory`` — it sits a layer below — and giving it a special
    exemption would mean the component that owns state transitions also owns what a training
    label is. Instead a caller in a higher layer composes the two:

    ```python
    apply_transition(..., on_success=observation_writer(config))
    ```

    The callback swallows nothing: an exception inside ``on_success`` rolls the transition back
    with it, which is the correct coupling. If the observation cannot be written, the case has
    not ended — better a retryable transition than a resolved case missing from the training
    set forever.
    """

    def _write(session: Session, case: RecoveryCase) -> None:
        outcome = record_observation(
            session,
            uuid.UUID(str(case.merchant_id)),
            case,
            config=config,
            correlation_id=correlation_id,
        )
        if outcome.skipped_reason is None and not outcome.written:
            _logger.debug(
                "recovery observation already recorded for case",
                case_id=str(case.id),
            )

    return _write


# ---------------------------------------------------------------------------
# The derived fields
# ---------------------------------------------------------------------------


def read_customer_signal_facts(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    intents: Sequence[ExecutionIntent],
    config: Configuration,
) -> CustomerSignalFacts:
    """Every R25.C1 fact about one case's Customer_Signals, in the caller's transaction.

    Six reads in one function, and one function rather than six calls scattered through
    :func:`record_observation` because three consumers use the answers — the feature document,
    the audit record and the ``REVORA_INTERVENED`` decision of R25.C4 — and the one thing that
    must not happen is an observation recording an arrangement request its own audit record
    denies. Reading once makes that impossible rather than unlikely.

    Public for the same reason: "what does an observation say about what a customer did" is a
    question a reviewer should be able to answer by reading one signature, not by tracing which
    repository calls the writer happens to make.

    Note what deliberately does *not* call this. ``revora.metrics.engine`` computes R25.C11's
    cohort counts with its own aggregates, because it is answering a different question — how many
    *cases* held each value — and per-case facts fetched in a loop would be the same answer
    computed once per row. The two do share the rules that could drift: both take the *latest*
    stated reason, ordered identically, and both key the suppression on the Suppression_Scope.

    Args:
        case: the locked case row, already moved to its terminal state. Used for its id and for
            its Suppression_Scope — :func:`~revora.customer.suppression.scope_key_for_case`
            reads the scope off the case, which is what makes the suppression lookup find an
            objection raised on a sibling case of the same customer and order (R21.C8).
        intents: the case's execution intents, already read by the caller. Passed rather than
            re-queried, because the caller needs them for the confirmed count anyway and two
            reads of the same rows inside one transaction is one read too many.
        config: for ``MAX_CUSTOMER_SIGNALS_PER_CASE``. The signal list is bounded by the same
            configured cap R19.C7 enforces on the way in, so reading it whole is a bounded
            read rather than an unbounded one — and the bound is the configured value rather
            than a number written here, because a deployment that raises the cap must not
            silently start truncating what memory records.

    Returns:
        A :class:`CustomerSignalFacts`. Every field is answered even for a case that has no
        signals at all; :attr:`CustomerSignalFacts.has_content` is what the feature builder
        consults before writing anything.
    """
    case_id = uuid.UUID(str(case.id))
    signal_repository = CustomerSignalRepository(session)

    signal_count = signal_repository.count_for_case(merchant_id, case_id)
    rows = signal_repository.list_for_case(
        merchant_id, case_id, limit=max(int(config.MAX_CUSTOMER_SIGNALS_PER_CASE), 1)
    )
    submitted = [ensure_utc(row.submitted_at) for row in rows]
    any_synthetic = any(
        str(row.provenance or Provenance.REAL.value) == Provenance.SYNTHETIC.value
        for row in rows
    )

    latest_reason = signal_repository.latest_delay_reason(merchant_id, case_id)
    delay_reason = (
        None
        if latest_reason is None or latest_reason.delay_reason is None
        else DelayReason(str(latest_reason.delay_reason))
    )

    promise = PromiseToPayRepository(session).for_case(merchant_id, case_id)
    promise_status = None if promise is None else PromiseStatus(str(promise.status))
    promise_seconds = (
        None
        if promise is None or promise.seconds_promise_to_payment is None
        else int(promise.seconds_promise_to_payment)
    )

    first_action_at = _first_action_instant(intents)
    return CustomerSignalFacts(
        signal_count=signal_count,
        delay_reason=delay_reason,
        promise_status=promise_status,
        promise_seconds_to_payment=promise_seconds,
        arrangement=first_arrangement_request(session, merchant_id, case_id),
        contact_suppressed=suppression_in_force(session, merchant_id, case),
        any_synthetic=any_synthetic,
        signal_after_action=(
            first_action_at is not None
            and any(instant >= first_action_at for instant in submitted)
        ),
        first_action_at=first_action_at,
        latest_signal_at=max(submitted) if submitted else None,
    )


def _first_action_instant(intents: Sequence[ExecutionIntent]) -> datetime | None:
    """The earliest instant at which Revora may have reached this customer, or ``None``.

    ``attempt_started_at`` of the earliest intent in one of :data:`_ACTED_INTENT_STATES`.
    ``attempt_started_at`` rather than ``resolved_at`` because the external call is issued after
    the intent row is durably committed and before it resolves (R9.C4), so the attempt instant
    is the earliest instant a message could have been in flight — and a signal submitted between
    the attempt and its confirmation is exactly the response this is looking for.

    Earliest rather than latest, because R25.C4 asks whether a signal arrived after *a* Revora
    action and any one of them disqualifies the observation.
    """
    instants = [
        ensure_utc(intent.attempt_started_at)
        for intent in intents
        if IntentState(intent.state) in _ACTED_INTENT_STATES
    ]
    return min(instants) if instants else None


def _observation_provenance(case: RecoveryCase, signals: CustomerSignalFacts) -> str:
    """The observation's provenance, widened by a synthetic Customer_Signal (R25.C2, R15.C2).

    ``SYNTHETIC`` where the case is synthetic *or* any signal on it is. Never the reverse: there
    is no path here that narrows a synthetic case to ``REAL``, which is what makes "one synthetic
    contributor is enough" a property of the function rather than of the data it happens to see.

    In practice a real case with a synthetic signal should not occur — page submissions are real
    and generated ones arrive with generated cases — and that is exactly why the widening is
    written down. A mixture would otherwise produce an observation labelled ``REAL`` that a
    generator contributed to, and the label is the whole basis on which a merchant-facing figure
    is allowed to claim it describes real money.
    """
    if signals.any_synthetic:
        return Provenance.SYNTHETIC.value
    return str(case.provenance or Provenance.REAL.value)


def _signal_features(signals: CustomerSignalFacts) -> dict[str, object]:
    """The Customer_Signal keys this observation's feature document carries, or none at all.

    Three keys at most: the two flat, containment-selectable ones R25.C3 requires, the nested
    R25.C1 document, and — unchanged from before this feature — the arrangement key of R22.C6.
    An empty mapping for a case with no signal and no suppression, which is what keeps an
    ordinary observation's document identical to what it was and keeps the pg test asserting the
    exact five-key set from having to be widened.

    The collision assert is not defensive clutter. These keys are added to the same document the
    five segment features occupy, and a key that collided with one of them would overwrite a
    segment value — silently moving every baseline computed from this merchant's history, with
    no error and no test failure anywhere else. The check costs a set membership per observation
    and turns that into an immediate, named failure.

    **Absent keys, never null ones**, and this is the rule the arrangement key established and
    the two new flat keys inherit. ``memory_observation.features`` is JSONB and a
    present-but-null key is a key a containment probe can match on, so the absent form keeps the
    document of a case that said nothing byte-identical to what it was before this feature
    existed. It also keeps the honest reading: a document with no ``delay_reason`` key says the
    customer stated no reason, and there is no third state to represent.

    The arrangement key carries the *earliest* request, which is
    :func:`~revora.customer.arrangements.first_arrangement_request`'s contract and the right one
    here — the observation records when this case became a person's problem, and a second
    identical ask does not move that instant. The Delay_Reason goes the other way and carries the
    latest; see :attr:`CustomerSignalFacts.delay_reason` for why the two differ.
    """
    if not signals.has_content:
        return {}

    document: dict[str, object] = {FEATURE_CUSTOMER_SIGNALS: signals.as_document()}
    if signals.delay_reason is not None:
        document[FEATURE_DELAY_REASON] = signals.delay_reason.value
    if signals.promise_status is not None:
        document[FEATURE_PROMISE_STATUS] = signals.promise_status.value
    if signals.arrangement is not None:
        document.update(arrangement_feature(signals.arrangement))

    collisions = sorted(_TRAINING_LABEL_SEGMENT_KEYS.intersection(document))
    if collisions:  # pragma: no cover - a coding error, not a data condition
        raise RuntimeError(
            f"customer-signal feature keys collide with segment features: {collisions}. "
            "Writing one would overwrite the segment value the estimator matches on and move "
            "every baseline in this merchant's history without failing anything."
        )
    return document


def _selected_action(
    confirmed: Sequence[ExecutionIntent], decisions: Sequence[PolicyDecision]
) -> str | None:
    """The action this case actually took, preferring what was confirmed over what was chosen.

    A confirmed intent is evidence; a policy decision is an intention. Where they disagree —
    an approval that never executed — the observation must record what happened, because a
    training row claiming an action was taken when it was not is worse than one claiming
    nothing.
    """
    if confirmed:
        return str(confirmed[0].action)
    if decisions:
        return str(decisions[-1].selected_action)
    return None


def _realized_cost(confirmed: Sequence[ExecutionIntent]) -> int:
    """The cost actually incurred, in integer minor units.

    Zero for now, and honestly zero rather than estimated. There is no ``action_cost`` column
    on ``execution_intent``, so no realized figure exists to sum — and substituting the
    *estimated* cost from the recommendation would put a prediction in a column named
    ``realized``, which is the sort of thing that later gets summed into a revenue report and
    presented as fact. A payment link costs nothing to create; when an action with a real
    per-use charge exists, it gets a column and this reads it.
    """
    return 0


def _decision_source(case: RecoveryCase, group: ExperimentGroup | None) -> str:
    """Who decided what happened to this case.

    Recorded because it is what makes confounding visible. A model trained on rows without it
    would reproduce past human choices and there would be no way to see that it had. A control
    case is ``BASELINE_WORKFLOW`` — its recommendation was recorded and suppressed, so the
    outcome describes the frozen baseline rather than anything Revora chose.
    """
    if group is ExperimentGroup.CONTROL:
        return DecisionSource.BASELINE_WORKFLOW.value
    if case.human_owner_user_id is not None:
        return DecisionSource.HUMAN_OVERRIDE.value
    return DecisionSource.AUTOMATED.value


def _payment_facts(
    session: Session, merchant_id: uuid.UUID, source_event_id: uuid.UUID | None
) -> tuple[str | None, str | None]:
    """The payment method and error source from the case's originating event.

    Read from the canonical column, which is PII-free by construction — the raw payload is
    encrypted and is not touched here. Both are ``None`` for a case with no source event, and
    ``SegmentFeatures.derive`` bands ``None`` into its own explicit bucket rather than guessing.
    """
    if source_event_id is None:
        return None, None
    event = WebhookEventRepository(session).get(merchant_id, source_event_id)
    if event is None:  # pragma: no cover - foreign key guarantees it
        return None, None
    canonical = event.canonical or {}
    method = canonical.get("method")
    error_source = canonical.get("error_source")
    return (
        method if isinstance(method, str) else None,
        error_source if isinstance(error_source, str) else None,
    )


_ = (CandidateAction, OutcomeClass)
"""Imported for the enum-backed columns they constrain; referenced so the import is not
mistaken for dead weight by a reader or removed by a linter."""

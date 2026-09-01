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

**The feature document is an interface, and a fragile one.** The five keys must be exactly
what ``estimation.segments.SegmentFeatures.as_values()`` produces, because the estimator
matches segments by JSONB containment. A renamed key does not raise — it silently stops
matching, every segment collapses to the global prior, and nothing looks broken. So the
document is built by calling that function rather than by assembling a dict here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from revora.audit.events import RECOVERY_OBSERVATION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    NOT_ESTABLISHED,
    CaseState,
    DecisionSource,
    ExperimentGroup,
    IntentState,
    InterventionStatus,
    OutcomeClass,
    Provenance,
    RiskCause,
)
from revora.domain.money import Minor
from revora.domain.segments import SegmentFeatures
from revora.domain.transitions import is_terminal
from revora.persistence.models import MemoryObservation
from revora.persistence.repositories.cases import WebhookEventRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.memory import MemoryObservationRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session

    from revora.persistence.models import ExecutionIntent, PolicyDecision, RecoveryCase
    from revora.platform.config import Configuration

__all__ = [
    "ObservationOutcome",
    "classify_intervention_status",
    "observation_writer",
    "record_observation",
]

_logger = get_logger(__name__)

_ACTOR = "recovery_memory"


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
    *, confirmed_actions: int, group: ExperimentGroup | None
) -> InterventionStatus:
    """Decide whether this case's outcome may serve as a baseline training label.

    The rule is deliberately conservative in one direction only.

    ``REVORA_INTERVENED`` whenever at least one action was confirmed. Not "was scheduled" and
    not "was attempted" — *confirmed*, meaning the provider acknowledged the effect exists. An
    attempt whose outcome is still unknown must not be counted as an intervention, because if
    it never landed the case really was untreated; equally it must not be counted as
    no-intervention, which is why an unresolved case is not observed at all until it resolves.

    ``NO_INTERVENTION_CONFIRMED`` requires zero confirmed actions **and** a control-arm
    assignment. The second condition is the part that gets omitted by mistake, and omitting it
    is what makes a baseline dishonest: a treatment case that happened to receive no action —
    because policy blocked it, or the window closed first — is *not* evidence about what
    happens without intervention. It is evidence about cases Revora declined to treat, which
    is a different and selected population. Treating those as baseline labels is the classic
    selection bias, and it would bias the baseline in the direction that flatters every
    subsequent incremental claim.

    Everything else is ``MERCHANT_INTERVENTION_UNKNOWN``: an unassigned case, where Revora did
    nothing but has no basis to claim nobody else did either.
    """
    if confirmed_actions > 0:
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
    cycle = int(case.decision_cycle_count)

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

    row = MemoryObservation(
        case_id=case_id,
        features=features.as_values(),
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
            confirmed_actions=len(confirmed), group=group
        ).value,
        # One synthetic contributor is enough to make every figure built on it synthetic, so
        # the case's own provenance propagates verbatim rather than being re-derived.
        provenance=str(case.provenance or Provenance.REAL.value),
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

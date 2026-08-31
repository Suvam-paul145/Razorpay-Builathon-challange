"""Persist the recommendation and every rejected alternative.

R7.C8 and R11.C6 together say something stronger than "record the decision": record every
candidate that was considered, with all six of its figures, its exclusion reason and its
rank, so that the single audit query that explains a case carries the whole comparison.
A merchant explaining to a customer why they were sent a payment link should be able to
see what else was on the table and what each alternative was worth — and should not need
a second query to find out.

So this module writes one ``recommendation`` row and one ``recommendation_candidate`` row
per candidate, including the excluded ones. Nothing is filtered on the way to storage.

**The AI boundary is structural here, not procedural.** ``recommendation.ai_explanation_text``
is the only place a model's prose may land, and this module cannot import
``revora.reasoning`` — the import contract forbids it, so there is no code path by which
an AI-produced value could reach a figure. When explanation text is supplied by a caller
that *can* reach the reasoning layer, it is written to that column alone and an audit
note records that it held no influence on the selection. That is Property 2's persistence
half: the ranking read ``net_recovery_value``, the column holding prose is not an input to
anything, and both facts are checkable from this file.

**The substituted diagnosis travels with the recommendation.** R7.C9: where the diagnosis
was replaced by ``UNKNOWN`` because its confidence fell below the floor or its method was
untrusted, the recommendation records the original cause and the substitution reason. A
narrowed candidate set — ``UNKNOWN`` permits nothing customer-visible — otherwise looks
like a failure rather than like the deliberate conservatism it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session

from revora.audit.events import RECOMMENDATION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import CandidateAction
from revora.domain.enums import ActionAvailability, RiskCause, SelectionReason
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.optimizer.arithmetic import CandidateInput
from revora.optimizer.selection import SelectionResult, Thresholds, select
from revora.persistence.models.estimates import CandidateEstimate
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.estimates import (
    BaselineEstimateRepository,
    CandidateEstimateRepository,
)
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = [
    "FAILURE_NO_BASELINE",
    "FAILURE_NO_CANDIDATES",
    "FAILURE_NO_CASE",
    "RecommendationOutcome",
    "run_optimizer",
    "thresholds_from_config",
]

_logger = get_logger(__name__)

_OPTIMIZER_ACTOR: Final = "value_optimizer"

FAILURE_NO_CASE: Final = "CASE_NOT_FOUND"
FAILURE_NO_BASELINE: Final = "NO_BASELINE_FOR_CYCLE"
FAILURE_NO_CANDIDATES: Final = "NO_CANDIDATES_FOR_BASELINE"
"""The three reasons no recommendation is produced.

All three are ordering faults in the job pipeline rather than data conditions: the
optimizer runs after estimation, and estimation guarantees a baseline and a candidate set
or reports its own failure. Distinguished as tokens because "the baseline never got
written" and "the candidates never got written" point at different jobs."""

_AI_NOTE: Final = "ai_explanation_text held no influence on selection"


def thresholds_from_config(config: Configuration) -> Thresholds:
    """The four bounds the optimizer applies, read from configuration.

    Extracted so the pure selection code never touches a ``Configuration`` and the
    properties can vary a bound without constructing one.
    """
    return Thresholds(
        min_net_value=config.MIN_NET_VALUE_THRESHOLD,
        min_incremental_probability=config.MIN_INCREMENTAL_PROBABILITY,
        max_cost_to_value_ratio=config.MAX_COST_TO_VALUE_RATIO,
        high_baseline=config.HIGH_BASELINE_THRESHOLD,
    )


@dataclass(frozen=True, slots=True)
class RecommendationOutcome:
    """What one optimizer run decided, and what the caller must do next.

    The caller is the job handler, and it owns the transition to ``POLICY_CHECK`` — the
    optimizer recommends, and only the policy engine can authorize. That separation is
    the core principle of the whole system, and it shows up here as this service
    deliberately not moving the case forward itself.
    """

    recommendation_id: uuid.UUID | None
    selected_action: CandidateAction | None
    selection_reason: SelectionReason | None
    divergence_reason: str | None
    net_recovery_value: Minor | None
    expected_incremental_revenue: Minor | None
    candidate_count: int
    failure_reason: str | None
    case_version: int | None
    already_recorded: bool = False


def run_optimizer(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
    ai_explanation_text: str | None = None,
) -> RecommendationOutcome:
    """Rank the candidate set and persist the recommendation for the current cycle.

    Must be called inside a transaction; it commits nothing itself. Takes the case row
    under ``FOR UPDATE`` because the audit writer allocates its gap-free sequence from a
    counter on that row, and because the lock serializes a concurrent second optimizer
    job onto the existing-recommendation check rather than onto the unique index beneath
    it.

    Args:
        ai_explanation_text: optional prose from the reasoning layer, supplied by a
            caller that can reach it. Written to ``ai_explanation_text`` and nowhere
            else. It is a parameter rather than something this module fetches precisely
            so that the module cannot import the reasoning layer — which is what makes
            "AI cannot influence the ranking" a structural fact.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        _logger.warning("optimizer for missing case", case_id=str(case_id))
        return _failed(FAILURE_NO_CASE, case_version=None)

    moment = now()
    decision_cycle = case.decision_cycle_count
    recommendations = RecommendationRepository(session)
    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    existing = recommendations.for_cycle(merchant_id, case_id, decision_cycle)
    if existing is not None:
        return RecommendationOutcome(
            recommendation_id=existing.id,
            selected_action=CandidateAction(existing.selected_action),
            selection_reason=SelectionReason(existing.selection_reason),
            divergence_reason=existing.divergence_reason,
            net_recovery_value=None,
            expected_incremental_revenue=None,
            candidate_count=recommendations.candidate_count(merchant_id, existing.id),
            failure_reason=None,
            case_version=None,
            already_recorded=True,
        )

    baseline = BaselineEstimateRepository(session).for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if baseline is None:
        _logger.warning("optimizer with no baseline", case_id=str(case_id))
        return _failed(FAILURE_NO_BASELINE, case_version=case.version)

    rows = CandidateEstimateRepository(session).list_for_baseline(merchant_id, baseline.id)
    if not rows:
        _logger.warning("optimizer with no candidates", case_id=str(case_id))
        return _failed(FAILURE_NO_CANDIDATES, case_version=case.version)

    result = select(
        tuple(_to_input(row) for row in rows),
        baseline=Probability(baseline.probability),
        amount=Minor(int(case.payment_amount)),
        thresholds=thresholds_from_config(config),
    )

    estimate_ids = {CandidateAction(row.action): row.id for row in rows}
    substitution = _substitution(session, merchant_id, case_id, decision_cycle)

    recommendation = recommendations.insert(
        merchant_id,
        values={
            "case_id": case_id,
            "baseline_estimate_id": baseline.id,
            "decision_cycle": decision_cycle,
            "selected_action": result.selected.action.value,
            "selection_reason": result.selection_reason.value,
            "divergence_reason": result.divergence_reason,
            "substituted_risk_cause": substitution[0],
            "substitution_reason": substitution[1],
            # The only column AI prose may occupy, and it is write-only from the
            # ranking's point of view.
            "ai_explanation_text": ai_explanation_text,
        },
    )
    recommendations.insert_candidates(
        merchant_id,
        rows=[
            {
                "recommendation_id": recommendation.id,
                "candidate_estimate_id": estimate_ids[item.action],
                "action": item.action.value,
                "incremental_probability": item.incremental_probability.value,
                "expected_incremental_revenue": int(item.expected_incremental_revenue),
                "action_cost": int(item.action_cost),
                "risk_cost": int(item.risk_cost),
                "customer_cost": int(item.customer_cost),
                "net_recovery_value": int(item.net_recovery_value),
                "excluded": item.excluded,
                "exclusion_reason": _reason_value(item.exclusion_reason),
                "rank": item.rank,
            }
            for item in result.candidates
        ],
    )

    _write_audit(
        writer,
        merchant_id,
        case_id,
        result=result,
        baseline_probability=baseline.probability,
        decision_cycle=decision_cycle,
        config_version=config.version,
        substitution=substitution,
        ai_explanation_present=ai_explanation_text is not None,
        correlation_id=correlation_id,
        moment=moment,
    )

    return RecommendationOutcome(
        recommendation_id=recommendation.id,
        selected_action=result.selected.action,
        selection_reason=result.selection_reason,
        divergence_reason=result.divergence_reason,
        net_recovery_value=result.selected.net_recovery_value,
        expected_incremental_revenue=result.selected.expected_incremental_revenue,
        candidate_count=len(result.candidates),
        failure_reason=None,
        case_version=case.version,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _to_input(row: CandidateEstimate) -> CandidateInput:
    """Turn a persisted candidate estimate into the optimizer's input struct.

    The narrow point at which the ORM stops and the pure arithmetic starts. Only the
    named columns the value chain needs cross this boundary, which is what keeps
    ``arithmetic`` and ``selection`` free of ``persistence`` — and means a column added
    to the table later cannot silently become an input to the ranking.
    """
    return CandidateInput(
        action=CandidateAction(row.action),
        intervention_probability=Probability(row.intervention_probability),
        action_cost=Minor(int(row.action_cost)),
        risk_cost=Minor(int(row.risk_cost)),
        customer_cost=Minor(int(row.customer_cost)),
        availability=ActionAvailability(row.availability),
        unavailable_reason=row.unavailable_reason,
    )


def _reason_value(reason: object | None) -> str | None:
    """The stored form of an exclusion reason.

    ``EvaluatedCandidate.exclusion_reason`` is typed loosely so that ``arithmetic`` need
    not import the exclusion vocabulary it does not use. This is the one place that
    loose type is narrowed, and it narrows to the enum's value or to ``None`` — the
    schema's ``exclusion_reason_iff_excluded`` check refuses anything else.
    """
    if reason is None:
        return None
    value = getattr(reason, "value", None)
    return value if isinstance(value, str) else str(reason)


def _substitution(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_cycle: int,
) -> tuple[str | None, str | None]:
    """The substituted cause and reason from the active diagnosis, if it was substituted.

    R7.C9. Read from the diagnosis row rather than recomputed: the substitution rule
    lives in ``revora.diagnosis`` and applying it a second time here would be a second
    implementation that could disagree with the recorded one.
    """
    diagnosis = DiagnosisRepository(session).active_for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if diagnosis is None or not diagnosis.substituted_to_unknown:
        return None, None
    evidence: Mapping[str, object] = diagnosis.evidence or {}
    original = evidence.get("original_cause")
    reason = evidence.get("substitution_reason")
    cause = original if isinstance(original, str) and original in set(RiskCause) else None
    return cause, reason if isinstance(reason, str) else None


def _write_audit(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    result: SelectionResult,
    baseline_probability: Decimal,
    decision_cycle: int,
    config_version: str,
    substitution: tuple[str | None, str | None],
    ai_explanation_present: bool,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """One audit record carrying the whole comparison.

    The ``decision`` field holds every candidate with its six figures, its exclusion
    reason and its rank, which is what R11.C6 asks for and what makes the single-query
    explanation complete. It is one record rather than one per candidate because the
    comparison is a single decision — splitting it would make the explanation a join.
    """
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=RECOMMENDATION_RECORDED,
            actor=_OPTIMIZER_ACTOR,
            action=result.selected.action.value,
            decision={
                "decision_cycle": decision_cycle,
                "config_version": config_version,
                "baseline_probability": str(baseline_probability),
                "selected_action": result.selected.action.value,
                "selection_reason": result.selection_reason.value,
                "divergence_reason": result.divergence_reason,
                "selected_net_recovery_value": int(result.selected.net_recovery_value),
                "substituted_risk_cause": substitution[0],
                "substitution_reason": substitution[1],
                # Property 2, recorded rather than merely true: the ranking read net
                # recovery value, and the presence of model prose changed nothing.
                "ai_explanation_present": ai_explanation_present,
                "ai_influence": _AI_NOTE,
                "ranked_on": "net_recovery_value",
                "candidates": [
                    {
                        "action": item.action.value,
                        "intervention_probability": str(item.intervention_probability),
                        "incremental_probability": str(item.incremental_probability),
                        "expected_incremental_revenue": int(
                            item.expected_incremental_revenue
                        ),
                        "action_cost": int(item.action_cost),
                        "risk_cost": int(item.risk_cost),
                        "customer_cost": int(item.customer_cost),
                        "net_recovery_value": int(item.net_recovery_value),
                        "cost_ratio": (
                            None if item.cost_ratio is None else str(item.cost_ratio)
                        ),
                        "availability": item.availability.value,
                        "excluded": item.excluded,
                        "exclusion_reason": _reason_value(item.exclusion_reason),
                        "rank": item.rank,
                    }
                    for item in result.candidates
                ],
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _failed(reason: str, *, case_version: int | None) -> RecommendationOutcome:
    """The outcome for a run that produced no recommendation."""
    return RecommendationOutcome(
        recommendation_id=None,
        selected_action=None,
        selection_reason=None,
        divergence_reason=None,
        net_recovery_value=None,
        expected_incremental_revenue=None,
        candidate_count=0,
        failure_reason=reason,
        case_version=case_version,
    )

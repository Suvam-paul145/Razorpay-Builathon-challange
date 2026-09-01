"""Read models for the dashboard. Every figure leaves here already formatted and already labelled.

This module is where a stored row becomes something a browser may render, and it exists as its own
layer rather than inline in the routers for one reason: **the honesty rules are the hard part, and
they belong in one place.** Four of them run through everything below.

**No money leaves as a bare integer.** :func:`revora.api.rendering.money` emits the formatted
string and the minor units together, so a client that renders the string is doing the easy thing
and a client that does arithmetic has to go out of its way (R14.C12).

**An absent value names what is absent and what state the case is in.** Never zero, never a dash.
A case with no recommendation gets a marker saying so and naming ``DETECTED`` or ``BLOCKED``, and
those two mean opposite things — the first is "not yet", the second is "policy stopped it"
(R14.C15).

**A refusal is rendered as fully as an action.** Where the optimizer selected ``DO_NOTHING`` or
``WAIT``, :func:`case_detail` returns the recorded reason *plus* the baseline probability, the
incremental probability, the net value and all three compared thresholds. A refusal shown as an
empty row or a red dot is how a merchant concludes the product is broken when it is working — and
"we decided not to spend your money on this customer" is the single most defensible thing Revora
says.

**Every candidate is returned, excluded ones included** (R6.C9, R7.C8). The case detail *is* the
comparison, not the winner. An excluded action carries its exclusion reason and its figures, so
"a retry was considered and is not available on this account" is visible rather than being an
absence nobody can interpret.

Nothing here computes a figure. Every number is read from a row that some component committed, and
the two derived values that do appear — a cost total and a net value — are integer sums of columns
sitting next to each other. A read model that computed would be a second implementation of the
arithmetic, free to disagree with the stored one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from revora.api.rendering import data_unavailable, money, not_yet_recorded, rate
from revora.domain.actions import CandidateAction
from revora.domain.enums import POLICY_CHECK_ORDER, SelectionReason
from revora.metrics.engine import CohortMetrics
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.estimates import BaselineEstimateRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    PaymentStateReadRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.experiments import (
    ExperimentResultRepository,
)
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.persistence.models import (
        AuditRecord,
        Experiment,
        ExperimentResult,
        PolicyCheckResult,
        PolicyDecision,
        Recommendation,
        RecommendationCandidate,
        RecoveryCase,
    )
    from revora.platform.config import Configuration

__all__ = [
    "NULL_SELECTION_REASONS",
    "audit_document",
    "case_detail",
    "case_summary",
    "experiment_document",
    "metrics_document",
]

NULL_SELECTION_REASONS: frozenset[str] = frozenset(
    {SelectionReason.NO_POSITIVE_VALUE.value, SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value}
)
"""The two reasons that mean Revora deliberately did not act.

Both get the full refusal block. They are not interchangeable and the block says which: one is
"nothing was worth doing", the other is "this customer was going to pay anyway". A merchant shown
the same words for both learns nothing from either."""


def case_summary(
    session: Session, merchant_id: uuid.UUID, case: RecoveryCase
) -> dict[str, object]:
    """One row of the case list (R14.C2).

    Deliberately a per-case read rather than one joined query. The list is bounded by
    ``DASHBOARD_PAGE_SIZE`` — a hundred by default — and a hundred small indexed reads is a
    cost worth paying for a list whose columns come from five tables and where a join would have to
    pick, for each of them, which of several rows is "the" one. That choice is exactly what the
    repository methods below already encode, and duplicating it in SQL is how a list column starts
    disagreeing with the detail page it links to.
    """
    currency = str(case.currency)
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
    diagnosis = DiagnosisRepository(session).active_for_case(merchant_id, case.id)
    intents = ExecutionIntentRepository(session).list_for_case(merchant_id, case.id)
    # Same cycle derivation as `case_detail`, for the same reason: the case counter and the cycle a
    # decision was recorded under are not the same number, and a list column that disagreed with the
    # detail page it links to is worse than either being wrong alone.
    decisions = PolicyDecisionRepository(session).for_cycle(
        merchant_id,
        case.id,
        int(recommendation.decision_cycle)
        if recommendation is not None
        else int(case.decision_cycle_count),
    )
    state = str(case.state)

    return {
        "case_id": str(case.id),
        "state": state,
        "detected_at": case.detected_at.isoformat(),
        "window_end_at": case.window_end_at.isoformat(),
        "payment_amount": money(int(case.payment_amount), currency=currency),
        "provider_payment_id": str(case.provider_payment_id),
        "customer_contact_masked": case.customer_contact_masked,
        "risk_cause": (
            str(diagnosis.cause) if diagnosis is not None else not_yet_recorded(state, "diagnosis")
        ),
        "selected_action": (
            str(recommendation.selected_action)
            if recommendation is not None
            else not_yet_recorded(state, "recommendation")
        ),
        "executed_action": _executed_action_summary(intents, state),
        "policy_decision": (
            str(decisions[-1].verdict)
            if decisions
            else not_yet_recorded(state, "policy decision")
        ),
        "recovered_amount": (
            money(int(outcome.recovered_amount), currency=currency)
            if outcome is not None
            else not_yet_recorded(state, "recovered amount")
        ),
        "outcome_classification": (
            str(outcome.classification)
            if outcome is not None
            else not_yet_recorded(state, "outcome")
        ),
        "human_owner_user_id": (
            None if case.human_owner_user_id is None else str(case.human_owner_user_id)
        ),
        "provenance": str(case.provenance),
    }


def _executed_action_summary(intents: Sequence[object], state: str) -> object:
    """The most recent confirmed action, or a marker.

    "No executed action" is not a failure and must not render as one: on a ``RECOVERED`` case it
    means the customer paid without being contacted, which is the outcome the product most wants
    and the one a naive display would show as a blank.
    """
    confirmed = [intent for intent in intents if str(getattr(intent, "state", "")) == "CONFIRMED"]
    if not confirmed:
        return not_yet_recorded(state, "executed action")
    latest = confirmed[-1]
    return str(getattr(latest, "action", ""))


def case_detail(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    config: Configuration,
) -> dict[str, object]:
    """The whole decision trail for one case (R14.C3 through C6, R14.C14, R11.C5).

    Everything a merchant would need to answer "why did Revora do that", in the order the pipeline
    produced it: what failed, what it was diagnosed as, what the baseline said, every candidate
    that was priced, which one was selected and why, what policy decided across all twelve checks,
    what was actually executed, and what came back.
    """
    currency = str(case.currency)
    state = str(case.state)

    diagnosis = DiagnosisRepository(session).active_for_case(merchant_id, case.id)
    baseline = BaselineEstimateRepository(session).latest_for_case(merchant_id, case.id)
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
    # The cycle to read decisions for comes from the recommendation when there is one, not from the
    # case counter. They diverge: the counter is incremented when a cycle *starts*, and the
    # recommendation and its policy decision are written under the cycle the optimizer ran in. Using
    # the counter showed a fully evaluated case as having no policy decision — a false absence
    # arriving through the read model rather than through a missing row, which is exactly the
    # failure R14.C15 exists to prevent, one level up.
    cycle = (
        int(recommendation.decision_cycle)
        if recommendation is not None
        else int(case.decision_cycle_count)
    )
    decisions = PolicyDecisionRepository(session).for_cycle(merchant_id, case.id, cycle)
    intents = ExecutionIntentRepository(session).list_for_case(merchant_id, case.id)
    reads = PaymentStateReadRepository(session).list_for_case(merchant_id, case.id)
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
    consent = CustomerConsentRepository(session).for_customer(
        merchant_id, str(case.customer_key)
    )

    return {
        "case": case_summary(session, merchant_id, case),
        "counters": {
            "executed_action_count": int(case.executed_action_count),
            "customer_message_count": int(case.customer_message_count),
            "decision_cycle_count": cycle,
            "max_recovery_attempts": int(config.MAX_RECOVERY_ATTEMPTS),
            "max_customer_messages": int(config.MAX_CUSTOMER_MESSAGES),
            "last_outbound_at": (
                None if case.last_outbound_at is None else case.last_outbound_at.isoformat()
            ),
        },
        "diagnosis": (
            {
                "cause": str(diagnosis.cause),
                "confidence": str(diagnosis.confidence),
                "method": str(diagnosis.method),
                "evidence": diagnosis.evidence,
                "substituted_to_unknown": bool(diagnosis.substituted_to_unknown),
                "decision_cycle": int(diagnosis.decision_cycle),
                "recorded_at": diagnosis.created_at.isoformat(),
                # Named explicitly so the dashboard can state it rather than implying it by the
                # absence of an AI badge. R3.C1's claim is that the deterministic path needs no
                # model at all, and a surface that only shows AI when present cannot express
                # "this decision involved none".
                "ai_involved": diagnosis.ai_invocation_id is not None,
            }
            if diagnosis is not None
            else not_yet_recorded(state, "diagnosis")
        ),
        "baseline": (
            {
                "probability": str(baseline.probability),
                "interval": (
                    None
                    if baseline.ci_low is None
                    else f"[{baseline.ci_low}, {baseline.ci_high}]"
                ),
                "uncertainty_available": bool(baseline.uncertainty_available),
                "segment_id": baseline.segment_id,
                "method": str(baseline.method),
                "validation_status": str(baseline.validation_status),
                "provenance": str(baseline.provenance),
            }
            if baseline is not None
            else not_yet_recorded(state, "baseline estimate")
        ),
        "recommendation": _recommendation_document(
            session,
            merchant_id,
            recommendation,
            baseline_probability=None if baseline is None else str(baseline.probability),
            currency=currency,
            state=state,
            config=config,
        ),
        "policy_decisions": [
            _policy_document(session, merchant_id, decision) for decision in decisions
        ]
        or not_yet_recorded(state, "policy decision"),
        "executed_actions": [
            {
                "intent_id": str(intent.id),
                "action": str(intent.action),
                "attempt_ordinal": int(intent.attempt_ordinal),
                "state": str(intent.state),
                "attempt_started_at": intent.attempt_started_at.isoformat(),
                "resolved_at": (
                    None if intent.resolved_at is None else intent.resolved_at.isoformat()
                ),
                "provider_failure_code": intent.provider_failure_code,
                # A bearer capability: whoever holds the URL can pay. Shown here because the
                # dashboard is authenticated, and never written to a log or an audit record.
                "provider_short_url": intent.provider_short_url,
                "is_post_payment": bool(intent.is_post_payment),
                "idempotency_key": str(intent.idempotency_key),
            }
            for intent in intents
        ]
        or not_yet_recorded(state, "executed action"),
        "authoritative_reads": [
            {
                "read_at": read.read_at.isoformat(),
                "status": str(read.status),
                "captured": bool(read.captured),
                "amount": money(int(read.amount), currency=currency),
                "amount_refunded": money(int(read.amount_refunded), currency=currency),
                "attempt_no": int(read.attempt_no),
            }
            for read in reads
        ]
        or not_yet_recorded(state, "authoritative provider read"),
        "outcome": (
            {
                "classification": str(outcome.classification),
                "recovered_amount": money(int(outcome.recovered_amount), currency=currency),
                "recovery_timestamp": outcome.recovery_timestamp.isoformat(),
                "seconds_to_recovery": outcome.seconds_to_recovery,
                # The read that verified it. A recovery with no backing read cannot exist —
                # the column is NOT NULL — and surfacing the id is what makes the figure
                # checkable rather than merely asserted.
                "verified_by_read_id": str(outcome.verified_by_read_id),
                "reconciled_from_terminal_state": outcome.reconciled_from_terminal_state,
            }
            if outcome is not None
            else not_yet_recorded(state, "verified outcome")
        ),
        "consent": (
            {
                "opted_out": bool(consent.opted_out),
                "source": consent.source,
                "effective_at": consent.effective_at.isoformat(),
                "consent_expires_at": (
                    None
                    if consent.consent_expires_at is None
                    else consent.consent_expires_at.isoformat()
                ),
            }
            if consent is not None
            else not_yet_recorded(state, "consent record")
        ),
        "terminal_reason": case.terminal_reason,
    }


def _recommendation_document(
    session: Session,
    merchant_id: uuid.UUID,
    recommendation: Recommendation | None,
    *,
    baseline_probability: str | None,
    currency: str,
    state: str,
    config: Configuration,
) -> dict[str, object]:
    """The recommendation, every candidate, and the refusal block where nothing was selected."""
    if recommendation is None:
        return not_yet_recorded(state, "recommendation")

    candidates = RecommendationRepository(session).candidates_for(
        merchant_id, recommendation.id
    )
    reason = str(recommendation.selection_reason)
    selected = str(recommendation.selected_action)
    selected_candidate = next(
        (item for item in candidates if str(item.action) == selected), None
    )

    document: dict[str, object] = {
        "recommendation_id": str(recommendation.id),
        "decision_cycle": int(recommendation.decision_cycle),
        "selected_action": selected,
        "selection_reason": reason,
        "divergence_reason": recommendation.divergence_reason,
        "substituted_risk_cause": recommendation.substituted_risk_cause,
        "substitution_reason": recommendation.substitution_reason,
        # Prose, and labelled as prose. No figure is derived from it and the dashboard must
        # present it as an explanation rather than as evidence.
        "ai_explanation_text": recommendation.ai_explanation_text,
        "ai_explanation_is_advisory": True,
        "candidates": [
            _candidate_document(item, currency=currency) for item in candidates
        ],
        "candidate_count": len(candidates),
    }

    if reason in NULL_SELECTION_REASONS:
        document["refusal"] = _refusal_block(
            reason=reason,
            baseline_probability=baseline_probability,
            selected=selected_candidate,
            config=config,
        )
    return document


def _candidate_document(
    candidate: RecommendationCandidate, *, currency: str
) -> dict[str, object]:
    """One candidate with all six figures R14.C4 names, plus its cost breakdown.

    Excluded candidates are here too, with their reason. An action that could not be used is
    different evidence from an action that was not worth using, and both are different from an
    action that never appears.
    """
    action = str(candidate.action)
    action_cost = int(candidate.action_cost)
    risk_cost = int(candidate.risk_cost)
    customer_cost = int(candidate.customer_cost)
    return {
        "action": action,
        "is_executable": CandidateAction(action) in _EXECUTABLE,
        "incremental_probability": str(candidate.incremental_probability),
        "expected_incremental_revenue": money(
            int(candidate.expected_incremental_revenue), currency=currency
        ),
        "action_cost": money(action_cost, currency=currency),
        "risk_cost": money(risk_cost, currency=currency),
        "customer_cost": money(customer_cost, currency=currency),
        "total_cost": money(action_cost + risk_cost + customer_cost, currency=currency),
        "net_recovery_value": money(int(candidate.net_recovery_value), currency=currency),
        "excluded": bool(candidate.excluded),
        "exclusion_reason": candidate.exclusion_reason,
        "rank": candidate.rank,
    }


def _refusal_block(
    *,
    reason: str,
    baseline_probability: str | None,
    selected: RecommendationCandidate | None,
    config: Configuration,
) -> dict[str, object]:
    """Why doing nothing was the answer, with every number that decided it (R11.C5).

    The three thresholds are included even though only one of them usually decided, because a
    merchant asking "why not?" is asking about the whole comparison. Showing the single failing
    bound invites the reply "so lower it", and the answer to that is the other two.
    """
    return {
        "reason": reason,
        "explanation": _REFUSAL_EXPLANATIONS[reason],
        "baseline_probability": baseline_probability
        or not_yet_recorded("UNKNOWN", "baseline probability"),
        "incremental_probability": (
            str(selected.incremental_probability) if selected is not None else None
        ),
        "net_recovery_value": (
            money(int(selected.net_recovery_value), currency="INR")
            if selected is not None
            else None
        ),
        "compared_thresholds": {
            "min_net_value_threshold": int(config.MIN_NET_VALUE_THRESHOLD),
            "min_incremental_probability": str(config.MIN_INCREMENTAL_PROBABILITY),
            "max_cost_to_value_ratio": str(config.MAX_COST_TO_VALUE_RATIO),
            "high_baseline_threshold": str(config.HIGH_BASELINE_THRESHOLD),
        },
    }


_REFUSAL_EXPLANATIONS: dict[str, str] = {
    SelectionReason.NO_POSITIVE_VALUE.value: (
        "No action cleared both the minimum net value and the minimum incremental probability, "
        "so acting would have cost more than it was expected to recover."
    ),
    SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value: (
        "This customer was already likely to pay without being contacted. Acting would have "
        "spent money and a customer's patience on a recovery that was going to happen anyway."
    ),
}
"""Plain sentences, server-side. A client that composed these would eventually word one of them
as a failure, and "we chose not to act" being mistaken for "we could not act" is the single
misreading this product can least afford."""

_EXECUTABLE = frozenset(
    {
        CandidateAction.DO_NOTHING,
        CandidateAction.WAIT,
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
        CandidateAction.HUMAN_ESCALATION,
    }
)
"""Mirrors ``domain.actions.EXECUTABLE_ACTIONS``. Referenced rather than imported as a name so a
reader of this file can see what "is_executable" means without following an import — and asserted
equal to the domain set by a test, so the two cannot drift."""


def _policy_document(
    session: Session, merchant_id: uuid.UUID, decision: PolicyDecision
) -> dict[str, object]:
    """One policy decision and all twelve checks, in evaluation order (R14.C5).

    Twelve, not "the ones that ran". The evaluation order is fixed precisely so the stated reason
    cannot be an expensive or case-specific check, and a record showing fewer than twelve rows
    would be indistinguishable from an evaluation that stopped early and approved.
    """
    checks = PolicyDecisionRepository(session).check_results_for(merchant_id, decision.id)
    return {
        "policy_decision_id": str(decision.id),
        "verdict": str(decision.verdict),
        "primary_reason": str(decision.primary_reason),
        "selected_action": str(decision.selected_action),
        "evaluated_at": decision.evaluated_at.isoformat(),
        "expires_at": decision.expires_at.isoformat(),
        "earliest_permitted_at": (
            None
            if decision.earliest_permitted_at is None
            else decision.earliest_permitted_at.isoformat()
        ),
        "rule_set_version": str(decision.rule_set_version),
        "config_version": decision.config_version,
        "case_state_at_evaluation": str(decision.case_state_at_evaluation),
        "decision_cycle": int(decision.decision_cycle),
        "consumed_by_intent_id": (
            None
            if decision.consumed_by_intent_id is None
            else str(decision.consumed_by_intent_id)
        ),
        "checks": _check_documents(checks),
        "expected_check_count": len(POLICY_CHECK_ORDER),
        "recorded_check_count": len(checks),
    }


def _check_documents(checks: Sequence[PolicyCheckResult]) -> list[dict[str, object]]:
    """The twelve check rows, and a placeholder for any that is missing.

    A missing row is rendered as ``NOT_RECORDED`` rather than omitted. Omitting it would shorten
    the list, and a reader used to twelve rows seeing eleven will not notice which one went — the
    same failure this whole surface exists to prevent, one level down.
    """
    by_id = {str(check.check_id): check for check in checks}
    documents: list[dict[str, object]] = []
    for order, check in enumerate(POLICY_CHECK_ORDER, start=1):
        row = by_id.get(check.value)
        if row is None:
            documents.append(
                {
                    "check_order": order,
                    "check_id": check.value,
                    "outcome": "NOT_RECORDED",
                    "detail": "this check has no recorded result for this decision",
                }
            )
            continue
        documents.append(
            {
                "check_order": int(row.check_order),
                "check_id": str(row.check_id),
                "outcome": str(row.outcome),
                "detail": row.detail,
            }
        )
    return documents


# ---------------------------------------------------------------------------
# Metrics, experiments, audit
# ---------------------------------------------------------------------------


def metrics_document(
    metrics: CohortMetrics,
    *,
    currency: str,
    incremental_available: bool = True,
) -> dict[str, object]:
    """A cohort report with every money figure formatted and every label attached.

    ``incremental_available=False`` replaces the incremental figure — and only that figure — with a
    data-unavailable marker (R14.C16). The rest of the report returns with its own timestamps,
    which is the whole point: a slow experiment analysis must not blank a page that also holds
    revenue at risk and a recovery rate.

    Note what does *not* change when the incremental figure is unavailable: the
    ``CAUSALITY_NOT_ESTABLISHED`` label stays on. A figure we could not compute is certainly not a
    causal claim we established, so degrading fails toward the safe statement rather than dropping
    the caveat along with the number.
    """
    return {
        "reporting_period": metrics.period.as_document(),
        "computed_at": metrics.computed_at.isoformat(),
        "segment": metrics.segment.as_document(),
        "case_count": metrics.case_count,
        "recovered_case_count": metrics.recovered_case_count,
        "revenue_at_risk": money(metrics.revenue_at_risk, currency=currency),
        "observed_recovered_revenue": money(
            metrics.observed_recovered_revenue, currency=currency
        ),
        "natural_recovered_revenue": money(
            metrics.natural_recovered_revenue, currency=currency
        ),
        "total_recovery_cost": money(metrics.total_recovery_cost, currency=currency),
        "net_recovered_revenue": money(metrics.net_recovered_revenue, currency=currency),
        "unresolved_revenue": money(metrics.unresolved_revenue, currency=currency),
        "incremental_recovered_revenue": (
            _incremental_document(metrics, currency=currency)
            if incremental_available
            else data_unavailable(
                "incremental_recovered_revenue",
                "the experiment analysis did not complete within "
                "DASHBOARD_METRICS_TIMEOUT; every other figure in this response is current",
            )
        ),
        "recovery_rate": rate(metrics.recovery_rate),
        "intervention_rate": rate(metrics.intervention_rate),
        "action_success_rate": rate(metrics.action_success_rate),
        "escalation_rate": rate(metrics.escalation_rate),
        "average_hours_to_recovery": rate(metrics.average_hours_to_recovery),
        "blocked_case_count": metrics.blocked_case_count,
        "escalated_case_count": metrics.escalated_case_count,
        "intervened_case_count": metrics.intervened_case_count,
        "confirmed_action_count": metrics.confirmed_action_count,
        "successful_action_count": metrics.successful_action_count,
        "unnecessary_action_count": metrics.unnecessary_action_count,
        "cycles_without_action_count": metrics.cycles_without_action_count,
        "labels": list(metrics.labels),
        "causality_established": metrics.causality_established,
        "is_synthetic": metrics.is_synthetic,
    }


def _incremental_document(metrics: CohortMetrics, *, currency: str) -> dict[str, object]:
    """The incremental figure, or the sentinel with its reasons — never a zero.

    ``NOT_ESTABLISHED`` is emitted as the sentinel string rather than as ``null`` or ``0``. Both
    alternatives render as something: an empty cell reads as "nothing recovered" and a zero reads
    as "measured, and it was nothing". The claim being made is neither.
    """
    finding = metrics.incremental
    if not finding.established:
        return {
            "status": "NOT_ESTABLISHED",
            "value": str(finding.value),
            "refusal_codes": list(finding.refusal_codes),
            "detail": (
                "No completed, adequately powered experiment with a lift interval entirely "
                "above zero supports a causal claim for this period. Observed recovery is not "
                "presented as incremental."
            ),
        }
    return {
        "status": "ESTABLISHED",
        "amount": money(int(finding.value), currency=currency),
        "experiment_id": None if finding.experiment_id is None else str(finding.experiment_id),
        "control_case_count": finding.control_case_count,
        "treatment_case_count": finding.treatment_case_count,
        "lift": None if finding.lift is None else str(finding.lift),
        "lift_interval": (
            None
            if finding.lift_ci_low is None
            else f"[{finding.lift_ci_low}, {finding.lift_ci_high}]"
        ),
    }


def experiment_document(
    session: Session,
    merchant_id: uuid.UUID,
    experiment: Experiment,
    *,
    currency: str,
) -> dict[str, object]:
    """An experiment and its newest analysis, with per-arm figures and interval bounds (R14.C8).

    Returns the definition even when there is no analysis yet, and says so. An experiment with no
    result is a normal state — it is running — and an endpoint that 404'd would make a running
    experiment indistinguishable from one that does not exist.
    """
    result = ExperimentResultRepository(session).latest_for_experiment(
        merchant_id, experiment.id
    )
    document: dict[str, object] = {
        "experiment_id": str(experiment.id),
        "name": str(experiment.name),
        "state": str(experiment.state),
        "primary_metric": str(experiment.primary_metric),
        "allocation_ratio": str(experiment.allocation_ratio),
        "assumed_baseline_rate": str(experiment.assumed_baseline_rate),
        "minimum_detectable_effect": str(experiment.minimum_detectable_effect),
        "significance_level": str(experiment.significance_level),
        "power": str(experiment.power),
        "required_sample_size_per_group": int(experiment.required_sample_size_per_group),
        "analysis_method": str(experiment.analysis_method),
        "labels": list(experiment.labels or ()),
        "activated_at": (
            None if experiment.activated_at is None else experiment.activated_at.isoformat()
        ),
        "completed_at": (
            None if experiment.completed_at is None else experiment.completed_at.isoformat()
        ),
    }
    document["result"] = (
        _experiment_result_document(result, currency=currency)
        if result is not None
        else not_yet_recorded(str(experiment.state), "experiment analysis")
    )
    return document


def _experiment_result_document(
    result: ExperimentResult, *, currency: str
) -> dict[str, object]:
    """One analysis. Per-arm counts and the interval, never a lift without its interval."""
    comparison = result.comparison if isinstance(result.comparison, dict) else {}
    return {
        "result_id": str(result.id),
        "computed_at": result.computed_at.isoformat(),
        "analysis_method": str(result.analysis_method),
        "primary_metric": str(result.primary_metric),
        "control": {
            "case_count": int(result.control_case_count),
            "recoveries": int(result.control_recoveries),
            "rate": rate(result.control_rate),
        },
        "treatment": {
            "case_count": int(result.treatment_case_count),
            "recoveries": int(result.treatment_recoveries),
            "rate": rate(result.treatment_rate),
        },
        "lift": None if result.lift is None else str(result.lift),
        # The interval travels with the lift and is emitted as a pair of bounds rather than as a
        # width. A width can be rendered without its centre; bounds cannot be shown without
        # revealing whether zero is inside them, which is the only thing that licenses a claim.
        "lift_ci_low": None if result.lift_ci_low is None else str(result.lift_ci_low),
        "lift_ci_high": None if result.lift_ci_high is None else str(result.lift_ci_high),
        "interval_contains_zero": _contains_zero(result),
        "contaminated_count": int(result.contaminated_count),
        "excluded_count": int(result.excluded_count),
        "labels": list(result.labels or ()),
        "four_way_comparison": comparison,
        "currency": currency,
    }


def _contains_zero(result: ExperimentResult) -> bool | None:
    """Whether the reported interval straddles zero. ``None`` when there is no interval.

    Computed server-side so a client never has to compare two decimal strings — the comparison
    that decides whether a causal claim is permitted is not one to leave to JavaScript number
    coercion.
    """
    if result.lift_ci_low is None or result.lift_ci_high is None:
        return None
    return result.lift_ci_low <= 0 <= result.lift_ci_high


def audit_document(records: Sequence[AuditRecord]) -> list[dict[str, object]]:
    """The ordered audit trail for a case (R11.C5).

    Sequence order, not timestamp order. Two records can share a millisecond and the per-case
    sequence is what actually says which came first — it is allocated inside the transaction that
    wrote the record, so it is gap-free and cannot be reordered by a clock.

    Every field is already masked at write time. Nothing here re-masks, and nothing here decides
    what to hide: a read model that filtered audit fields would be a second, weaker masking policy
    sitting in front of the real one.
    """
    return [
        {
            "seq": None if record.seq is None else int(record.seq),
            "occurred_at": record.occurred_at.isoformat(),
            "event_type": str(record.event_type),
            "actor": str(record.actor),
            "correlation_id": str(record.correlation_id),
            "previous_state": record.previous_state,
            "new_state": record.new_state,
            "action": record.action,
            "action_result": record.action_result,
            "idempotency_key": record.idempotency_key,
            "confidence": None if record.confidence is None else str(record.confidence),
            "diagnosis": record.diagnosis,
            "evidence": record.evidence,
            "decision": record.decision,
            "policy_result": record.policy_result,
        }
        for record in records
    ]


# NOTE. The candidate figures shown here come from ``recommendation_candidate`` — the *ranked*
# record — and not from ``candidate_estimate``, which is the *priced* one. They are not the same
# set: pricing happens for every eligible action, ranking happens after exclusion, and the ranked
# rows carry the exclusion reason and the rank that R7.C8 asks the dashboard to show. Reading the
# priced table here would display figures that were never part of a comparison.

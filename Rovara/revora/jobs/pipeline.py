"""The decision pipeline: detection to authorization, one job per step.

Five steps, each its own job, each advancing the case one state and enqueuing the next in
the same transaction:

``DETECTED`` → diagnose → ``DIAGNOSED`` → estimate → decide → ``DECISION_PENDING``
→ policy → ``POLICY_CHECK``

**Why one job per step rather than one job for the pipeline.** Every step is a durable
state change with its own audit record, and a job boundary is where a crash becomes
resumable. A single job doing all five would, on a crash after estimation, either redo the
diagnosis (harmless but wasteful) or resume from a position it has no durable record of
(not harmless). With a job per step the case's own state *is* the resume point, and the
sweeper can always tell what should happen next from persisted rows alone.

**Why the follow-on enqueue is inside the transition.** ``apply_transition`` takes an
``on_success`` callback that runs in its transaction, so the state change and the job that
acts on it commit together. That is the whole reason the queue is a table rather than a
broker: a broker's enqueue after commit can be lost, and before commit can fire against
state that never committed.

**This module owns the policy decision's persistence**, and that placement is deliberate.
The ``policy-isolation`` import contract forbids ``revora.policy`` from importing
``revora.persistence``, because the engine's purity is what makes R8.C14 and Property 2
checkable. So the pure ``evaluate`` lives in ``revora.policy`` and the row-reading and
row-writing around it live here, in the one layer permitted to see both. The task plan
sketched this as ``revora/policy/service.py``; the contract is the stronger authority and
the behaviour is the same.

**Zero external calls anywhere in this file.** The whole decision pipeline is reads,
arithmetic and writes. Nothing here can reach the provider — ``revora.providers`` is not
imported and the execution engine that will use it is task 20.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session

from revora.audit.events import POLICY_DECISION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.manager import apply_transition
from revora.detection.service import DetectionServiceResult
from revora.diagnosis.service import run_diagnosis
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, PolicyVerdict, RiskCause
from revora.estimation.baseline import run_baseline_estimation
from revora.estimation.candidates import run_candidate_estimation
from revora.optimizer.service import run_optimizer
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger
from revora.policy.engine import PolicyEvaluation, evaluate, idempotency_key_for
from revora.policy.input import PolicyInput
from revora.policy.rules import RuleSet, default_rule_set

__all__ = [
    "CANDIDATE_JOB_KIND",
    "DIAGNOSIS_JOB_KIND",
    "OPTIMIZER_JOB_KIND",
    "POLICY_JOB_KIND",
    "PolicyOutcome",
    "enqueue_next",
    "handle_candidates",
    "handle_diagnosis",
    "handle_optimizer",
    "handle_policy",
    "rule_set_from_config",
    "run_policy_evaluation",
]

_logger = get_logger(__name__)

_POLICY_ACTOR: Final = "policy_engine"

DIAGNOSIS_JOB_KIND: Final[str] = "diagnosis"
CANDIDATE_JOB_KIND: Final[str] = "estimation"
OPTIMIZER_JOB_KIND: Final[str] = "optimization"
POLICY_JOB_KIND: Final[str] = "policy_evaluation"
"""The four decision-pipeline job kinds. Declared here rather than in ``scheduler`` because
these are event-driven follow-ons rather than periodic sweeps — each is enqueued by the step
before it, not by a clock."""


def enqueue_next(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    kind: str,
    case_id: uuid.UUID,
    correlation_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Enqueue the next pipeline step in the caller's transaction.

    Dedupe-keyed on ``(kind, case_id)`` so a retried step cannot enqueue its successor
    twice. The key collides only against *pending* jobs, so a later decision cycle can
    legitimately enqueue the same kind for the same case again once the first has been
    claimed.
    """
    return JobRepository(session).enqueue(
        merchant_id,
        kind=kind,
        payload={
            "case_id": str(case_id),
            "correlation_id": None if correlation_id is None else str(correlation_id),
        },
        run_after=now(),
        dedupe_key=f"{kind}:{case_id}",
        case_id=case_id,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Step 1: diagnosis
# ---------------------------------------------------------------------------


def handle_diagnosis(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Diagnose the cause, move the case to ``DIAGNOSED``, and enqueue estimation.

    The diagnosis is written in its own transaction, then the transition is applied in
    another — because ``apply_transition`` opens its own and holds the case row lock, and
    nesting the two would deadlock on that row. The intermediate state is safe: a crash
    between them leaves a recorded diagnosis and a case still in ``DETECTED``, and the next
    diagnosis job returns the existing row idempotently and retries the transition.
    """
    with tenant_transaction(merchant_id) as session:
        config = _config(session, merchant_id)
        outcome = run_diagnosis(
            session, merchant_id, case_id, config, correlation_id=correlation_id
        )
        version = outcome.case_version

    if version is None:
        version = _current_version(merchant_id, case_id)
        if version is None:
            return

    apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.DIAGNOSED,
        reason="risk cause recorded",
        actor="diagnosis_engine",
        correlation_id=correlation_id,
        on_success=lambda session, case: _after(
            session, merchant_id, case_id, CANDIDATE_JOB_KIND, correlation_id
        ),
    )


# ---------------------------------------------------------------------------
# Step 2: estimation (baseline, then candidates)
# ---------------------------------------------------------------------------


def handle_candidates(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Estimate the baseline and price every candidate action, then enqueue the optimizer.

    A baseline failure stops the pipeline here with the case left in ``DIAGNOSED`` (R5.C11).
    Deliberately no transition and no follow-on job: a missing baseline must never be
    treated as zero, and the honest response to "we could not estimate what happens if we
    do nothing" is to leave the case where a retry or a human can find it.
    """
    with tenant_transaction(merchant_id) as session:
        config = _config(session, merchant_id)
        baseline = run_baseline_estimation(
            session, merchant_id, case_id, config, correlation_id=correlation_id
        )
        if not baseline.requires_candidate_estimation:
            _logger.warning(
                "baseline estimation produced no estimate; pipeline stops at DIAGNOSED",
                case_id=str(case_id),
                failure_reason=baseline.failure_reason,
            )
            return
        run_candidate_estimation(
            session, merchant_id, case_id, config, correlation_id=correlation_id
        )

    with tenant_transaction(merchant_id) as session:
        enqueue_next(
            session,
            merchant_id,
            kind=OPTIMIZER_JOB_KIND,
            case_id=case_id,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# Step 3: the optimizer
# ---------------------------------------------------------------------------


def handle_optimizer(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Rank the candidates, record the recommendation, move to ``DECISION_PENDING``.

    The transition into ``DECISION_PENDING`` increments the decision-cycle counter, which is
    the counter that bounds the only cycle in the state graph. That is why the recommendation
    is written *before* the transition: the recommendation belongs to the cycle that produced
    it, and writing it after the increment would file it under the next one.
    """
    with tenant_transaction(merchant_id) as session:
        config = _config(session, merchant_id)
        outcome = run_optimizer(
            session, merchant_id, case_id, config, correlation_id=correlation_id
        )
        if outcome.recommendation_id is None:
            _logger.warning(
                "optimizer produced no recommendation",
                case_id=str(case_id),
                failure_reason=outcome.failure_reason,
            )
            return
        version = outcome.case_version

    if version is None:
        version = _current_version(merchant_id, case_id)
        if version is None:
            return

    apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.DECISION_PENDING,
        reason="recommendation recorded",
        actor="value_optimizer",
        correlation_id=correlation_id,
        on_success=lambda session, case: _after(
            session, merchant_id, case_id, POLICY_JOB_KIND, correlation_id
        ),
    )


# ---------------------------------------------------------------------------
# Step 4: policy
# ---------------------------------------------------------------------------


def handle_policy(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Evaluate policy, persist the decision, and move the case to ``POLICY_CHECK``.

    The pipeline stops here in this phase. Execution is task 20, and until it exists an
    ``APPROVED`` decision is a durable authorization that nothing consumes — which is the
    correct intermediate state, because the decision record is complete and the effect has
    not happened.
    """
    with tenant_transaction(merchant_id) as session:
        config = _config(session, merchant_id)
        outcome = run_policy_evaluation(
            session, merchant_id, case_id, config, correlation_id=correlation_id
        )
        if outcome.policy_decision_id is None:
            _logger.warning(
                "policy evaluation produced no decision",
                case_id=str(case_id),
                failure_reason=outcome.failure_reason,
            )
            return
        version = outcome.case_version

    if version is None:
        version = _current_version(merchant_id, case_id)
        if version is None:
            return

    apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.POLICY_CHECK,
        reason=f"policy verdict {outcome.verdict.value if outcome.verdict else 'UNKNOWN'}",
        actor=_POLICY_ACTOR,
        action=outcome.selected_action,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Policy evaluation: the row-reading and row-writing around the pure engine
# ---------------------------------------------------------------------------


def rule_set_from_config(config: Configuration) -> RuleSet:
    """Lift the configured bounds into a versioned rule set.

    The boundary between configuration and the pure engine. Every bound the twelve checks
    compare against is read once, here, and packed into an immutable value — which is what
    lets ``evaluate`` stay pure and what makes the recorded ``rule_set_version`` a faithful
    description of what actually ran.
    """
    return default_rule_set(
        max_recovery_attempts=config.MAX_RECOVERY_ATTEMPTS,
        max_customer_messages=config.MAX_CUSTOMER_MESSAGES,
        cooldown_interval=config.COOLDOWN_INTERVAL,
        policy_decision_validity=config.POLICY_DECISION_VALIDITY,
        risk_reason_codes=config.RISK_REASON_CODES,
        min_net_value_threshold=config.MIN_NET_VALUE_THRESHOLD,
        min_incremental_probability=config.MIN_INCREMENTAL_PROBABILITY,
    )


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """What one policy evaluation decided, and what the caller may now do.

    ``authorized`` is the only field an execution path should branch on, and it is true only
    for a persisted ``APPROVED``. Deliberately not left for the caller to derive from
    ``verdict`` — one place decides what "may act" means.
    """

    policy_decision_id: uuid.UUID | None
    verdict: PolicyVerdict | None
    primary_reason: str | None
    selected_action: CandidateAction | None
    idempotency_key: str | None
    expires_at: datetime | None
    earliest_permitted_at: datetime | None
    failed_check_count: int
    failure_reason: str | None
    case_version: int | None

    @property
    def authorized(self) -> bool:
        return (
            self.verdict is PolicyVerdict.APPROVED and self.policy_decision_id is not None
        )


def run_policy_evaluation(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    selected_action: CandidateAction | None = None,
    correlation_id: uuid.UUID | None = None,
) -> PolicyOutcome:
    """Evaluate policy for one case and persist the decision with all twelve outcomes.

    Must be called inside a transaction; it commits nothing itself. The caller's commit is
    what releases the authorization, so "recorded before released" (R8.C12) is enforced by
    the transaction boundary rather than by call ordering.

    Args:
        selected_action: the action to authorize. Defaults to the action the current cycle's
            recommendation selected — policy validates what the optimizer proposed. Supplied
            explicitly for a human-initiated action, which is evaluated on the same twelve
            checks with no exemption.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        _logger.warning("policy evaluation for missing case", case_id=str(case_id))
        return _policy_failed("CASE_NOT_FOUND", case_version=None)

    moment = now()

    # The newest recommendation, not the one filed under the case's *current* cycle
    # counter. The counter advances on the edge into ``DECISION_PENDING``, which happens
    # *after* the optimizer writes its recommendation — so by the time policy runs, the
    # counter is one ahead of the cycle the recommendation belongs to. Reading by the
    # current counter finds nothing and the pipeline stalls in ``DECISION_PENDING``.
    #
    # Every subsequent lookup uses the recommendation's own cycle, so the decision, the
    # recommendation and the diagnosis it was built from all agree about which cycle they
    # describe. Deriving it by arithmetic on the counter would work today and break the
    # moment another transition gained a cycle effect.
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case_id)

    action = selected_action
    if action is None:
        if recommendation is None:
            _logger.warning("policy evaluation with no recommendation", case_id=str(case_id))
            return _policy_failed("NO_RECOMMENDATION_FOR_CYCLE", case_version=case.version)
        action = CandidateAction(recommendation.selected_action)

    decision_cycle = (
        case.decision_cycle_count
        if recommendation is None
        else int(recommendation.decision_cycle)
    )

    diagnosis = DiagnosisRepository(session).active_for_cycle(
        merchant_id, case_id, decision_cycle
    )
    cause = RiskCause(diagnosis.cause) if diagnosis is not None else None
    consent = CustomerConsentRepository(session).for_customer(merchant_id, case.customer_key)
    decisions = PolicyDecisionRepository(session)
    rules = rule_set_from_config(config)

    prospective_key = idempotency_key_for(
        case_id=case_id, action=action, attempt_ordinal=case.executed_action_count + 1
    )
    candidate = PolicyInput.from_persisted(
        case=case,
        consent=consent,
        verified_captured=_verified_captured(case.verified_payment_status),
        verified_status=case.verified_payment_status,
        diagnosed_cause=cause,
        open_intent_exists=decisions.open_intent_exists(merchant_id, case_id),
        intent_exists_for_key=decisions.intent_exists_for_key(merchant_id, prospective_key),
        selected_action=action,
        evaluated_at=moment,
        rules_version=rules.version_label,
        config_version=config.version,
    )
    evaluation = evaluate(candidate, rules)

    decision = decisions.insert(
        merchant_id,
        values={
            "case_id": case_id,
            "recommendation_id": None if recommendation is None else recommendation.id,
            "verdict": evaluation.verdict.value,
            "primary_reason": evaluation.primary_reason,
            "rule_set_id": None,
            "rule_set_version": evaluation.rules_version,
            "config_version": config.version,
            "evaluated_at": evaluation.evaluated_at,
            "expires_at": evaluation.expires_at,
            "earliest_permitted_at": evaluation.earliest_permitted_at,
            "idempotency_key": evaluation.idempotency_key,
            "selected_action": action.value,
            "case_state_at_evaluation": evaluation.case_state_at_evaluation,
            "decision_cycle": decision_cycle,
        },
    )
    decisions.insert_check_results(
        merchant_id,
        rows=[
            {
                "policy_decision_id": decision.id,
                "check_order": check.order,
                "check_id": check.check.value,
                "outcome": check.outcome.value,
                "detail": check.detail,
            }
            for check in evaluation.checks
        ],
    )
    _write_policy_audit(
        session,
        merchant_id,
        case_id,
        config=config,
        evaluation=evaluation,
        decision_cycle=decision_cycle,
        correlation_id=correlation_id,
        moment=moment,
    )

    return PolicyOutcome(
        policy_decision_id=decision.id,
        verdict=evaluation.verdict,
        primary_reason=evaluation.primary_reason,
        selected_action=action,
        idempotency_key=evaluation.idempotency_key,
        expires_at=evaluation.expires_at,
        earliest_permitted_at=evaluation.earliest_permitted_at,
        failed_check_count=len(evaluation.failed_checks),
        failure_reason=None,
        case_version=case.version,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _config(session: Session, merchant_id: uuid.UUID) -> Configuration:
    return ConfigurationRepository(session).load(merchant_id)


def _current_version(merchant_id: uuid.UUID, case_id: uuid.UUID) -> int | None:
    """Re-read the case version after an idempotent early return.

    A step that found its work already done reports no version, because it did not read the
    case under a lock. The transition still has to be attempted — the previous run may have
    crashed between writing its row and transitioning — so the version is fetched here.
    """
    with tenant_transaction(merchant_id) as session:
        case = RecoveryCaseRepository(session).get(merchant_id, case_id)
        return None if case is None else case.version


def _after(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    kind: str,
    correlation_id: uuid.UUID | None,
) -> None:
    """Enqueue the next step inside the transition's transaction."""
    enqueue_next(
        session, merchant_id, kind=kind, case_id=case_id, correlation_id=correlation_id
    )


def _verified_captured(status: str | None) -> bool | None:
    """Whether the last authoritative read says captured.

    ``None`` where no read has happened. ``captured`` is the only status that counts:
    ``authorized`` alone is explicitly not recovery, because the money has not been taken.
    """
    if status is None:
        return None
    return status == "captured"


def _write_policy_audit(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    config: Configuration,
    evaluation: PolicyEvaluation,
    decision_cycle: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """One audit record carrying all twelve ordered outcomes.

    R8.C12 and R11.C6: the record answers what was decided, against which rules, against
    which state, and what every check said — without a join. One record rather than twelve,
    because the evaluation is a single decision and the ordered outcomes are its reasoning.
    """
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=POLICY_DECISION_RECORDED,
            actor=_POLICY_ACTOR,
            action=evaluation.selected_action.value,
            idempotency_key=evaluation.idempotency_key,
            policy_result={
                "verdict": evaluation.verdict.value,
                "primary_reason": evaluation.primary_reason,
                "rule_set_version": evaluation.rules_version,
                "config_version": config.version,
                "evaluated_at": evaluation.evaluated_at.isoformat(),
                "expires_at": evaluation.expires_at.isoformat(),
                "earliest_permitted_at": (
                    None
                    if evaluation.earliest_permitted_at is None
                    else evaluation.earliest_permitted_at.isoformat()
                ),
                "case_state_at_evaluation": evaluation.case_state_at_evaluation,
                "decision_cycle": decision_cycle,
                "provider_requests_issued": 0,
                "checks": [
                    {
                        "order": check.order,
                        "check_id": check.check.value,
                        "outcome": check.outcome.value,
                        "detail": check.detail,
                    }
                    for check in evaluation.checks
                ],
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _policy_failed(reason: str, *, case_version: int | None) -> PolicyOutcome:
    """The outcome for a run that produced no decision.

    Every authorization-bearing field is ``None``, so ``authorized`` is false and a caller
    that ignored ``failure_reason`` still cannot act.
    """
    return PolicyOutcome(
        policy_decision_id=None,
        verdict=None,
        primary_reason=None,
        selected_action=None,
        idempotency_key=None,
        expires_at=None,
        earliest_permitted_at=None,
        failed_check_count=0,
        failure_reason=reason,
        case_version=case_version,
    )


def enqueue_after_detection(
    session: Session,
    merchant_id: uuid.UUID,
    result: DetectionServiceResult,
    *,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Start the pipeline for a newly opened case, in detection's own transaction.

    Only for a case that detection actually created. An event attached to an already-open
    case must not start a second pipeline — that case is already somewhere in the sequence,
    and a second diagnosis job would race the first for the same decision cycle.
    """
    if not result.case_created or result.case_id is None:
        return None
    return enqueue_next(
        session,
        merchant_id,
        kind=DIAGNOSIS_JOB_KIND,
        case_id=result.case_id,
        correlation_id=correlation_id,
    )

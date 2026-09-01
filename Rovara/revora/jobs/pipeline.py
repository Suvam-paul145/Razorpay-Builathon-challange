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

**The decision steps make zero external calls, and the two that can are named.** Diagnosis,
estimation, optimization and policy are reads, arithmetic and writes only — none of them can
reach the provider, and none of them takes a client to reach it with. :func:`handle_execution`
and :func:`handle_outcome` are the only functions here that touch the outside world, and both
require a ``provider`` argument rather than resolving one. That is the structural form of
"which steps can have an effect": it is answerable by reading the signatures.

Both of those delegate entirely. The exactly-once guarantee lives in
:mod:`revora.execution.engine` and the recovery decision lives in :mod:`revora.outcome.monitor`;
the handlers here decide only what to enqueue next. A second opinion about either would be a
second implementation of the guarantee.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

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
from revora.execution.engine import ExecutionOutcome, execute_approved_action
from revora.experiment.control import assign_case
from revora.optimizer.service import run_optimizer
from revora.outcome.monitor import observe_payment_outcome
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
from revora.policy.rules import rule_set_from_config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from revora.providers.razorpay import PaymentProviderClient

__all__ = [
    "CANDIDATE_JOB_KIND",
    "DIAGNOSIS_JOB_KIND",
    "EXECUTION_JOB_KIND",
    "OPTIMIZER_JOB_KIND",
    "OUTCOME_JOB_KIND",
    "POLICY_JOB_KIND",
    "PolicyOutcome",
    "enqueue_next",
    "handle_candidates",
    "handle_diagnosis",
    "handle_execution",
    "handle_optimizer",
    "handle_outcome",
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
EXECUTION_JOB_KIND: Final[str] = "execution"
OUTCOME_JOB_KIND: Final[str] = "outcome_observation"
"""The six decision-pipeline job kinds. Declared here rather than in ``scheduler`` because
these are event-driven follow-ons rather than periodic sweeps — each is enqueued by the step
before it, not by a clock.

``execution`` and ``outcome_observation`` are the two that close the loop, and they were the gap
that made the pipeline stop at ``POLICY_CHECK``: an ``APPROVED`` decision was a durable
authorization that nothing consumed. The two periodic sweeps — execution reconciliation and
payment-state reconciliation — remain the safety nets underneath them, because a case must never
*depend* on a job having run. These are the fast path when one does."""


def enqueue_next(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    kind: str,
    case_id: uuid.UUID,
    correlation_id: uuid.UUID | None,
    extra_payload: dict[str, object] | None = None,
) -> uuid.UUID | None:
    """Enqueue the next pipeline step in the caller's transaction.

    Dedupe-keyed on ``(kind, case_id)`` so a retried step cannot enqueue its successor
    twice. The key collides only against *pending* jobs, so a later decision cycle can
    legitimately enqueue the same kind for the same case again once the first has been
    claimed.

    ``extra_payload`` carries step-specific values — currently only the claimed status on an
    outcome observation. It is merged *under* the two mandatory keys rather than over them, so a
    caller cannot accidentally redirect a job to another case by supplying ``case_id``.
    """
    payload: dict[str, object] = dict(extra_payload or {})
    payload["case_id"] = str(case_id)
    payload["correlation_id"] = None if correlation_id is None else str(correlation_id)
    return JobRepository(session).enqueue(
        merchant_id,
        kind=kind,
        payload=payload,
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
    """Evaluate policy, persist the decision, move to ``POLICY_CHECK``, and schedule if approved.

    Two transitions rather than one, and they are separate states for a reason. ``POLICY_CHECK``
    means "a decision has been recorded"; ``ACTION_SCHEDULED`` means "an authorization exists and
    is waiting to be consumed". A case that stalls between them is a case whose decision is
    durable and whose effect has not happened — which is the safe intermediate state, and the one a
    crash should leave behind.

    Only an ``APPROVED`` verdict schedules. ``BLOCKED``, ``DEFERRED`` and ``ESCALATE`` all stop at
    ``POLICY_CHECK``: the decision is complete, it is on the record with its twelve check outcomes,
    and no effect follows. A blocked case is not a failed case and must not be made to look like one
    — the lifecycle sweeper will terminate it when its window closes, and the dashboard shows the
    reason in the meantime.
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

    recorded = apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.POLICY_CHECK,
        reason=f"policy verdict {outcome.verdict.value if outcome.verdict else 'UNKNOWN'}",
        actor=_POLICY_ACTOR,
        action=outcome.selected_action,
        correlation_id=correlation_id,
    )
    if not outcome.authorized or not recorded.applied:
        return

    # The scheduling edge, and the execution enqueue rides inside its transaction. If the enqueue
    # were a separate commit, a crash between the two would leave a case in ``ACTION_SCHEDULED``
    # with nothing to execute it — recoverable only by a sweep, and invisible until then.
    apply_transition(
        merchant_id,
        case_id,
        expected_version=recorded.version if recorded.version is not None else version + 1,
        target_state=CaseState.ACTION_SCHEDULED,
        reason="approved action scheduled",
        actor=_POLICY_ACTOR,
        action=outcome.selected_action,
        correlation_id=correlation_id,
        on_success=lambda session, case: _after(
            session, merchant_id, case_id, EXECUTION_JOB_KIND, correlation_id
        ),
    )


# ---------------------------------------------------------------------------
# Step 5: execution — the only step that can produce an external effect
# ---------------------------------------------------------------------------


def handle_execution(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Execute the approved action at most once, then wait for the outcome.

    Thin on purpose: :func:`revora.execution.engine.execute_approved_action` owns the whole
    exactly-once guarantee — the reservation, the lock discipline, the transitions and the
    refusals — and this handler must not add a second opinion about any of them. It decides one
    thing the engine does not: whether to enqueue an outcome observation.

    It enqueues one only on ``CONFIRMED`` — a link that demonstrably exists at the provider. On a
    refusal there is nothing to observe. On ``UNCERTAIN`` the *reconciliation* sweep owns the case,
    and enqueuing an observation instead would have the monitor read a payment whose link may not
    exist: a read that cannot answer the question being asked, and one that would move the case out
    of the state reconciliation looks for.
    """
    attempt = execute_approved_action(
        merchant_id, case_id, provider=provider, correlation_id=correlation_id
    )
    _logger.info(
        "execution attempt completed",
        merchant_id=str(merchant_id),
        case_id=str(case_id),
        outcome=attempt.outcome.value,
        made_external_call=attempt.made_external_call,
    )
    if attempt.outcome is not ExecutionOutcome.CONFIRMED:
        return

    with tenant_transaction(merchant_id) as session:
        enqueue_next(
            session,
            merchant_id,
            kind=OUTCOME_JOB_KIND,
            case_id=case_id,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# Step 6: outcome observation — the only place a recovery may be declared
# ---------------------------------------------------------------------------


def handle_outcome(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    signal_status: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Read the provider and let the monitor decide whether this case recovered.

    Idempotent at the monitor: a case already ``RECOVERED`` short-circuits before any read, so a
    duplicate capture signal costs nothing and cannot count the amount twice.

    ``signal_status`` is what the prompting webhook claimed. It is passed through for conflict
    detection only — the recovery decision comes from the authoritative read, never from the
    signal, which is the whole of R10.C1.
    """
    assessment = observe_payment_outcome(
        merchant_id,
        case_id,
        provider=provider,
        signal_status=signal_status,
        correlation_id=correlation_id,
    )
    _logger.info(
        "outcome observed",
        merchant_id=str(merchant_id),
        case_id=str(case_id),
        verdict=assessment.verdict.value,
        declared_recovery=assessment.declared_recovery,
    )


# ---------------------------------------------------------------------------
# Policy evaluation: the row-reading and row-writing around the pure engine
# ---------------------------------------------------------------------------


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
        # `RecoveryCase` satisfies `CaseFacts` at runtime and does not satisfy it structurally to
        # mypy, and the reason is a known limitation rather than a defect on either side: a
        # SQLAlchemy 2.0 mapped attribute is declared `Mapped[UUID]` and resolves to `UUID` through
        # the descriptor protocol on *access*, but protocol matching compares the declared types.
        # Restating the Protocol in terms of `Mapped[...]` would drag `sqlalchemy` into
        # `revora.policy`, which the policy-isolation contract forbids and which is the whole point
        # of the Protocol existing. Suppressed here, at the one call site, rather than weakened
        # there.
        case=case,  # type: ignore[arg-type]
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
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Assign the arm and start the pipeline, in detection's own transaction.

    Only for a case that detection actually created. An event attached to an already-open
    case must not start a second pipeline — that case is already somewhere in the sequence,
    and a second diagnosis job would race the first for the same decision cycle. It must not
    be re-assigned either: the arm is fixed for the life of the case.

    ``config`` is passed positionally because the caller already has it loaded in this same
    transaction, and re-reading it here would be a second query for a value that cannot have
    changed since.
    """
    if not result.case_created or result.case_id is None:
        return None

    # Experiment assignment happens here, and the position is the requirement (R13.C1, C2). This
    # is detection's own transaction — the one that created the case — and it runs before the
    # diagnosis job is even enqueued. So the arm is durable before anything can look at the
    # case's cause, and a case cannot exist without its arm.
    #
    # `assign_case` never raises: an experiment that cannot be assigned leaves the case
    # unassigned on the baseline workflow rather than failing the transaction. Losing a real
    # payment failure because an experiment was misconfigured would be the wrong trade, since
    # the experiment is the optional part.
    assign_case(
        session,
        merchant_id,
        result.case_id,
        config=config,
        correlation_id=correlation_id,
    )

    return enqueue_next(
        session,
        merchant_id,
        kind=DIAGNOSIS_JOB_KIND,
        case_id=result.case_id,
        correlation_id=correlation_id,
    )


def enqueue_after_recovery_signal(
    session: Session,
    merchant_id: uuid.UUID,
    result: DetectionServiceResult,
    *,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Enqueue an outcome observation for a payment-success signal, in detection's transaction.

    A capture is ``NOT_AT_RISK`` — it is not a failure — so it opens no case, and before this
    existed the pipeline did nothing with it. The case still reached ``RECOVERED``, but only when
    the payment-state sweep next ran, up to ``PAYMENT_STATE_RECONCILIATION_INTERVAL`` later. R10.C1
    wants an authoritative read within ``OUTCOME_READ_LATENCY_BOUND``, and a fifteen-minute sweep
    cannot meet a sixty-second bound.

    The sweep stays underneath as the safety net, because a case must never *depend* on a webhook
    arriving. This is the fast path when one does, and the monitor's idempotence is what makes
    having both harmless: whichever gets there first, the second finds the case already recovered
    and issues no read at all.
    """
    if result.recovery_signal_case_id is None:
        return None
    return enqueue_next(
        session,
        merchant_id,
        kind=OUTCOME_JOB_KIND,
        case_id=result.recovery_signal_case_id,
        correlation_id=correlation_id,
        extra_payload={"signal_status": result.signal_status},
    )

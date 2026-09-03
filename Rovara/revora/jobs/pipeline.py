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

:func:`handle_review` is the one step that is not part of that sequence. It re-enters it, from
``POLICY_CHECK`` back to ``DECISION_PENDING``, for a case whose last cycle chose restraint —
and it runs the same four steps rather than any of its own, so a reviewed case is decided on
exactly the terms a new one is.

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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from sqlalchemy.orm import Session

from revora.audit.events import (
    ACTION_CANCELLED_CONTACT_SUPPRESSED,
    CASE_ESCALATED,
    CASE_REVIEWED,
    POLICY_DECISION_RECORDED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.manager import apply_locked_transition, apply_transition
from revora.customer.suppression import suppression_in_force
from revora.detection.service import DetectionServiceResult
from revora.diagnosis.service import run_diagnosis
from revora.domain.actions import NULL_ACTIONS, CandidateAction, needs_provider_call
from revora.domain.enums import (
    CaseState,
    HardStopReason,
    PolicyVerdict,
    ReviewTrigger,
    RiskCause,
    TerminalReason,
)
from revora.domain.transitions import is_terminal
from revora.estimation.baseline import run_baseline_estimation
from revora.estimation.candidates import run_candidate_estimation
from revora.execution.engine import ExecutionOutcome, execute_approved_action
from revora.experiment.control import assign_case
from revora.memory.store import observation_writer
from revora.optimizer.service import run_optimizer
from revora.outcome.monitor import (
    observe_payment_outcome,
    record_post_suppression_actions,
)
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
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
    from revora.persistence.models import RecoveryCase
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
    "handle_review",
    "rule_set_from_config",
    "run_policy_evaluation",
]

_logger = get_logger(__name__)

_POLICY_ACTOR: Final = "policy_engine"
_REVIEW_ACTOR: Final = "review_engine"
_SUPPRESSION_ACTOR: Final = "contact_suppression"

SUPPRESSION_TERMINAL_REASON: Final[Mapping[HardStopReason, TerminalReason]] = (
    MappingProxyType(
        {
            HardStopReason.DISPUTES_THE_CHARGE: TerminalReason.CUSTOMER_DISPUTED_CHARGE,
            HardStopReason.NO_LONGER_WANTS_THE_ORDER: (
                TerminalReason.CUSTOMER_CANCELLED_ORDER
            ),
        }
    )
)
"""R21.C4 and R21.C5 as a table, total over :class:`HardStopReason`.

Two rows and both are the whole content of a requirement clause, which is why they are a table
rather than an ``if``: R21.C4 and R21.C5 differ only in this mapping, and stating it as data
means the two clauses are checkable by reading one expression. The check below keeps it total, so
a third Hard_Stop_Reason fails at import rather than falling through to a ``KeyError`` inside a
worker transaction — the place a missing row would be most expensive to discover.

The two reasons stay distinct rather than collapsing onto one escalation reason because what has
to happen next differs: a dispute implies a possible chargeback, a cancellation implies fulfilment
and refund questions. Both are a person's problem, and not the same person's."""

_unmapped_hard_stops = sorted(
    reason.value for reason in HardStopReason if reason not in SUPPRESSION_TERMINAL_REASON
)
if _unmapped_hard_stops:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "SUPPRESSION_TERMINAL_REASON is not total over HardStopReason; missing "
        f"{_unmapped_hard_stops}"
    )

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

    An approved *null* action — ``DO_NOTHING`` or ``WAIT`` — also stops at ``POLICY_CHECK``, and it
    is the one of these that leaves something behind: a ``next_review_at`` instant, written in the
    transition's own transaction, at which the case will be looked at again (R30.C3). Before that
    instant existed, a case Revora correctly decided not to act on had no route to a second
    decision cycle at all, and waited out its window as though it had been abandoned.
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

    # R30.C3: a selection of ``DO_NOTHING`` or ``WAIT`` gets a review instant, and it is
    # written *inside* the transition into ``POLICY_CHECK`` rather than in a commit after it.
    # A case resting at ``POLICY_CHECK`` with ``next_review_at`` null is invisible to the
    # Review_Sweeper's index predicate, so a crash between two commits would reproduce exactly
    # the defect R30 exists to fix — silently, on whichever cases were unlucky. The callback
    # runs on the row this transition already holds locked, so it reads the immutable
    # ``window_end_at`` it clamps against under that lock.
    #
    # Attached only for a null action, because that is R30.C3's precondition. A ``DEFERRED``,
    # ``BLOCKED`` or ``ESCALATE`` verdict also rests at ``POLICY_CHECK``, and a case that was
    # refused is not a case that chose restraint — giving it a review instant would put the
    # sweeper in charge of retrying decisions the policy engine already declined.
    chose_restraint = outcome.authorized and outcome.selected_action in NULL_ACTIONS
    review_at: datetime | None = None

    def _schedule_review(session: Session, case: RecoveryCase) -> None:
        nonlocal review_at
        review_at = _review_instant(
            moment=now(),
            window_end_at=case.window_end_at,
            interval=config.WAIT_REVIEW_INTERVAL,
        )
        case.next_review_at = review_at

    recorded = apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.POLICY_CHECK,
        reason=f"policy verdict {outcome.verdict.value if outcome.verdict else 'UNKNOWN'}",
        actor=_POLICY_ACTOR,
        action=outcome.selected_action,
        correlation_id=correlation_id,
        on_success=_schedule_review if chose_restraint else None,
    )
    if not outcome.authorized or not recorded.applied:
        return

    action = outcome.selected_action
    next_version = recorded.version if recorded.version is not None else version + 1

    if action is not None and needs_provider_call(action):
        # The scheduling edge, and the execution enqueue rides inside its transaction. If the
        # enqueue were a separate commit, a crash between the two would leave a case in
        # ``ACTION_SCHEDULED`` with nothing to execute it — recoverable only by a sweep, and
        # invisible until then.
        apply_transition(
            merchant_id,
            case_id,
            expected_version=next_version,
            target_state=CaseState.ACTION_SCHEDULED,
            reason="approved action scheduled",
            actor=_POLICY_ACTOR,
            action=action,
            correlation_id=correlation_id,
            on_success=lambda session, case: _after(
                session, merchant_id, case_id, EXECUTION_JOB_KIND, correlation_id
            ),
        )
        return

    if action is CandidateAction.HUMAN_ESCALATION:
        # A human has been asked, so the case leaves automation. Terminal, and terminal is right:
        # ``ESCALATED -> RECOVERED`` stays legal, so a case a person resolves still reconciles when
        # the money arrives. What must not happen is what happened before this branch existed —
        # scheduling an escalation as though it were a provider call, which left the case
        # authorized, unexecuted and waiting for its window to close with no explanation on the
        # record.
        #
        # ``observation_writer`` is attached because this is a *terminal* edge, and every terminal
        # edge owes the training set a row. Nothing revisits a terminal case, so an edge without
        # this callback is a permanent hole — and the hole would be systematically the large-amount
        # cases, teaching the memory layer that expensive failures are ones Revora chose not to act
        # on. It is exactly the wrong thing to be missing.
        apply_transition(
            merchant_id,
            case_id,
            expected_version=next_version,
            target_state=CaseState.ESCALATED,
            reason="approved action requires a human",
            actor=_POLICY_ACTOR,
            action=action,
            terminal_reason=TerminalReason.HUMAN_OWNERSHIP,
            correlation_id=correlation_id,
            on_success=observation_writer(config, correlation_id=correlation_id),
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        return

    # A null action. Nothing to schedule and nothing to execute — Revora decided not to act this
    # cycle, which is a complete and defensible answer. The case stays at ``POLICY_CHECK``
    # carrying the review instant written above (R30.C3, R30.C12), so restraint is a decision to
    # revisit rather than a decision to abandon. The lifecycle sweeper still owns the ending if
    # no review ever changes the answer, so "we chose not to act" does not become "we failed".
    #
    # ``review_scheduled`` false with an authorized null action means the window had already
    # closed when the selection was recorded — see :func:`_review_instant`.
    _logger.info(
        "no action to execute this cycle",
        merchant_id=str(merchant_id),
        case_id=str(case_id),
        selected_action=None if action is None else action.value,
        selection_reason=outcome.primary_reason,
        next_review_at=None if review_at is None else review_at.isoformat(),
        review_scheduled=review_at is not None,
    )


# ---------------------------------------------------------------------------
# Step 4b: review — a second decision cycle for a case that chose restraint
# ---------------------------------------------------------------------------


def handle_review(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    trigger: ReviewTrigger,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Look at a case that chose restraint again, and start it on a fresh decision cycle.

    Assembled from the four steps the forward path already uses — diagnosis, baseline,
    candidates, optimizer — rather than reimplementing any of them. A second implementation
    of "what does this case cost and what is it worth" would be a second answer, and the
    whole claim of R30 is that a reviewed case is decided on *exactly* the terms a new case
    is. So this handler contributes no arithmetic of its own. What it contributes is the
    ordering, the cap, and the record.

    **The decision-cycle numbering.** All four steps read ``case.decision_cycle_count`` off
    the row themselves and file under it; none of them takes a cycle number. Because they run
    *before* the transition into ``DECISION_PENDING`` — which is the edge that increments the
    counter — a case resting at ``POLICY_CHECK`` with the counter at ``n`` produces a
    diagnosis, a baseline, a candidate set and a recommendation all filed under cycle ``n``,
    which is the ``n+1``-th decision cycle of the case's life. Passing a cycle number in, or
    running any step after the transition, would file the recommendation under ``n+1`` where
    every lookup for it searches ``n``. That failure is silent — ``for_cycle`` returns ``None``
    rather than raising — and it presents as a pipeline stalled in ``DECISION_PENDING``, a
    case-detail view with no policy decision, and a training observation with a null cause.
    See ``RecommendationRepository.active_decision_cycle``.

    **The cap is checked here as well as in the sweep's query, and both are load-bearing.**
    The query excludes capped cases so no pointless job is queued; this gate terminates a case
    that reached the cap between that read and this lock. R30.C10 is this one — the sweep
    cannot satisfy it, because a sweep that merely declines to enqueue leaves a capped case
    sitting at ``POLICY_CHECK`` until its window elapses, which is a different ending with a
    different reason on the record.

    **Nothing here reads the trigger except the audit record.** R30.C15 requires the policy
    evaluation to take no input from the Review_Trigger and none from the count of prior
    reviews, and the structure is what enforces it: policy runs in its own job, from
    ``POLICY_JOB_KIND``, whose payload carries a case id and a correlation id and no trigger.
    There is nothing for it to branch on.
    """
    review_at_start = now()
    new_action: CandidateAction | None = None

    with tenant_transaction(merchant_id) as session:
        config = _config(session, merchant_id)
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:
            _logger.warning("review for missing case", case_id=str(case_id))
            return

        # Re-checked under the lock, because the sweep read the due set in a transaction that
        # has since committed and any trigger's enqueue is older than this claim. A case that
        # has been approved and scheduled, terminated, or already reviewed is not an error
        # here: it is a review that arrived after the question stopped being open.
        state = CaseState(case.state)
        if state is not CaseState.POLICY_CHECK:
            _logger.info(
                "review skipped: case is no longer waiting at POLICY_CHECK",
                case_id=str(case_id),
                observed_state=state.value,
                review_trigger=trigger.value,
            )
            return

        version = case.version
        cycles_before = case.decision_cycle_count
        unresolved_amount = int(case.payment_amount)
        at_cap = cycles_before >= config.MAX_RECOVERY_ATTEMPTS
        previous_action = _previously_selected_action(session, merchant_id, case_id)

        if not at_cap:
            run_diagnosis(session, merchant_id, case_id, config, correlation_id=correlation_id)
            baseline = run_baseline_estimation(
                session, merchant_id, case_id, config, correlation_id=correlation_id
            )
            if not baseline.requires_candidate_estimation:
                # Same stop as the forward path: a missing baseline must never be read as a
                # baseline of zero, which would make every intervention look maximally
                # valuable. The case keeps its ``next_review_at``, so the next sweep pass
                # retries — and if the estimate never becomes available, the lifecycle sweep
                # still ends the case when its window closes.
                _logger.warning(
                    "review produced no baseline; case stays at POLICY_CHECK",
                    case_id=str(case_id),
                    failure_reason=baseline.failure_reason,
                )
                return
            run_candidate_estimation(
                session, merchant_id, case_id, config, correlation_id=correlation_id
            )
            recommendation = run_optimizer(
                session, merchant_id, case_id, config, correlation_id=correlation_id
            )
            if recommendation.recommendation_id is None:
                _logger.warning(
                    "review produced no recommendation; case stays at POLICY_CHECK",
                    case_id=str(case_id),
                    failure_reason=recommendation.failure_reason,
                )
                return
            new_action = recommendation.selected_action

    def _record(session: Session, case: RecoveryCase) -> None:
        """Write ``CASE_REVIEWED`` in the transition's own transaction.

        In ``on_success`` rather than a commit of its own so the record exists exactly when
        the review applied — R30.C11 wants one record per completed review, and a record
        written after a separate commit can outlive a rolled-back transition or be lost by a
        crash between the two.

        ``case.decision_cycle_count`` read here is the counter *after* the review, because
        the transition's counter effects have already been applied to this row.
        ``case.next_review_at`` is ``None`` on both paths, and that is the honest value: the
        one writer of ``recovery_case.state`` clears it on every edge out of ``POLICY_CHECK``,
        and the *next* review instant does not exist yet — it is written by
        :func:`handle_policy` if this cycle's selection is a null action too.
        """
        _write_review_audit(
            session,
            merchant_id,
            case_id,
            config=config,
            trigger=trigger,
            previous_action=previous_action,
            new_action=new_action,
            decision_cycle_count=case.decision_cycle_count,
            next_review_at=case.next_review_at,
            unresolved_amount=unresolved_amount,
            correlation_id=correlation_id,
            moment=review_at_start,
        )

    if at_cap:
        # R30.C10. Terminal, so the training set is owed a row: nothing revisits a terminal
        # case, and an edge without ``observation_writer`` is a permanent hole in what the
        # memory layer learns. This hole would be systematically the cases Revora tried
        # hardest on, since reaching the cap takes the maximum number of cycles.
        #
        # ``next_review_at`` needs no clearing at this call site. ``apply_locked_transition``
        # clears it on every edge whose source is ``POLICY_CHECK``, keyed on the source state
        # rather than on a list of edges, so this one inherits it by construction.
        observation = observation_writer(config, correlation_id=correlation_id)

        def _terminate(session: Session, case: RecoveryCase) -> None:
            observation(session, case)
            _record(session, case)

        apply_transition(
            merchant_id,
            case_id,
            expected_version=version,
            target_state=CaseState.STOPPED,
            reason="decision cycle limit reached at review",
            actor=_REVIEW_ACTOR,
            terminal_reason=TerminalReason.DECISION_CYCLE_LIMIT_REACHED,
            correlation_id=correlation_id,
            on_success=_terminate,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        return

    def _advance(session: Session, case: RecoveryCase) -> None:
        _record(session, case)
        _after(session, merchant_id, case_id, POLICY_JOB_KIND, correlation_id)

    apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=CaseState.DECISION_PENDING,
        reason=f"review triggered by {trigger.value}",
        actor=_REVIEW_ACTOR,
        action=new_action,
        correlation_id=correlation_id,
        on_success=_advance,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )


# ---------------------------------------------------------------------------
# Contact_Suppression: the transitional consequences of a hard stop (R21.C4-C7)
# ---------------------------------------------------------------------------


def handle_contact_suppression(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    hard_stop_reason: HardStopReason,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Apply everything a Contact_Suppression causes that the accepting request could not.

    The suppression row and the token revocation were written inside the request that accepted the
    Customer_Signal, because R21.C1 and R21.C10 require them to be atomic with it. Everything in
    this handler is the other four clauses, and every one of them is something R19.C9 forbids that
    request from doing: a state transition (C4, C5), the cancellation of scheduled actions (C6) and
    the treatment of an intent that was already in flight (C7). So the accepting request enqueued
    one ``contact_suppression`` job and the worker is here to run it.

    **One transaction, and the ordering inside it is the whole design.**

    1. **Lock the case.** Everything after this is serialized against the pipeline's own writer,
       which is what makes the intent read and the approval read below a snapshot rather than a
       guess.
    2. **Return early if the case is already terminal.** Not an error and not rare: a customer can
       submit a hard stop on a case that a sweep expired seconds earlier, and a retried job finds
       the case this handler itself escalated. Either way the correct action is none — a case holds
       one terminal reason, and overwriting the first one would rewrite history to say the customer
       objected to a case that had already ended. This early return is also what makes the handler
       idempotent without an ``is_post_payment``-style flag column: a second run writes nothing.
    3. **Record the in-flight intents** (C7), before the transition. Ordered first among the writes
       because these are statements about what was already true when the suppression arrived, and a
       reader walking the audit log in sequence should see *what had gone out* before seeing *what
       we did about it*.
    4. **Record one cancellation per approved-but-unconsumed decision** (C6). "For which no
       execution-intent record exists" is ``consumed_by_intent_id IS NULL``, so the set comes
       straight from the schema rather than from an inference about what was probably scheduled.
    5. **Record ``CASE_ESCALATED``** carrying the unresolved ``payment_amount`` in minor units
       (C4, C5), before the transition, for the reason ``DELAYED_RECOVERY_RECONCILED`` is written
       before its transition in the Outcome_Monitor: the audit sequence is allocated under the case
       row lock this transaction already holds, and the transition is the last thing that happens
       so that a failure anywhere above it leaves the case where it was.
    6. **Transition to ``ESCALATED``** with ``CUSTOMER_DISPUTED_CHARGE`` or
       ``CUSTOMER_CANCELLED_ORDER``, through ``apply_locked_transition`` — the only writer of
       ``recovery_case.state``, reached in its locked form because this transaction already holds
       the row and the wrapper would deadlock against itself.

    **Neither counter moves, and that is structural rather than checked here.** The
    executed-action and customer-message counters have exactly one edge that increments them,
    ``ACTION_SCHEDULED -> EXECUTING``, and every edge into a terminal state carries
    ``NO_EFFECTS``. So R21.C6's "SHALL leave the executed-action counter and the customer-message
    counter unchanged" is a property of the transition table, not of this function remembering.

    **No provider request is issued from anywhere in this path.** This module cannot issue one —
    there is no provider client in this function's arguments — and the queued execution jobs that
    could are neutered by the terminal state, because ``execute_approved_action`` re-requests
    authority against reloaded state and check 2 refuses a terminal case. R21.C12's later
    reconciliation to ``RECOVERED`` is likewise a read rather than a request, and it leaves the
    suppression in force because nothing but :func:`revora.customer.suppression.release_suppression`
    can clear ``released_at`` and only a named Merchant_User can call it.
    """
    with tenant_transaction(merchant_id) as session:
        config = ConfigurationRepository(session).load(merchant_id)
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:  # pragma: no cover - RESTRICT makes a case undeletable
            _logger.warning("suppression consequence on missing case", case_id=str(case_id))
            return

        state = CaseState(case.state)
        if is_terminal(state):
            _logger.info(
                "suppression consequence skipped: case already terminal",
                case_id=str(case_id),
                state=state.value,
                terminal_reason=case.terminal_reason,
            )
            return

        writer = AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        unresolved_amount = int(case.payment_amount)
        terminal_reason = SUPPRESSION_TERMINAL_REASON[hard_stop_reason]

        in_flight = record_post_suppression_actions(
            merchant_id,
            case_id,
            writer,
            ExecutionIntentRepository(session).list_for_case(merchant_id, case_id),
            correlation_id=correlation_id,
        )

        cancelled = PolicyDecisionRepository(session).list_approved_unconsumed(
            merchant_id, case_id
        )
        for decision in cancelled:
            writer.write_for_case(
                merchant_id,
                case_id,
                AuditEntry(
                    event_type=ACTION_CANCELLED_CONTACT_SUPPRESSED,
                    actor=_SUPPRESSION_ACTOR,
                    action=str(decision.selected_action),
                    idempotency_key=decision.idempotency_key,
                    decision={
                        "policy_decision_id": str(decision.id),
                        "hard_stop_reason": hard_stop_reason.value,
                        "detail": "contact suppressed before any external call; action "
                        "cancelled, counters unchanged",
                    },
                ),
                correlation_id=correlation_id,
            )

        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CASE_ESCALATED,
                actor=_SUPPRESSION_ACTOR,
                previous_state=state.value,
                new_state=CaseState.ESCALATED.value,
                decision={
                    "hard_stop_reason": hard_stop_reason.value,
                    "terminal_reason": terminal_reason.value,
                    # R21.C4, R21.C5: the unresolved amount in minor currency units. An
                    # ``int`` from a ``BIGINT`` column, never a float and never re-derived
                    # from a formatted string.
                    "unresolved_amount": unresolved_amount,
                    "cancelled_actions": len(cancelled),
                    "in_flight_actions": in_flight,
                },
            ),
            correlation_id=correlation_id,
        )

        # Terminal, so the training set is owed a row: nothing revisits a terminal case, and an
        # edge without ``observation_writer`` is a permanent hole in what the memory layer
        # learns. Systematically the cases where a customer objected, which is the population a
        # model most needs to stop proposing contact for.
        _, rejection = apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.ESCALATED,
            reason=f"contact suppressed: {hard_stop_reason.value}",
            actor=_SUPPRESSION_ACTOR,
            terminal_reason=terminal_reason,
            correlation_id=correlation_id,
            on_success=observation_writer(config, correlation_id=correlation_id),
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        if rejection is not None:  # pragma: no cover - every non-terminal edge is legal
            _logger.warning(
                "suppression escalation refused",
                case_id=str(case_id),
                outcome=rejection.outcome.value,
                state=state.value,
            )
            return

    _logger.info(
        "contact suppression applied",
        case_id=str(case_id),
        hard_stop_reason=hard_stop_reason.value,
        terminal_reason=terminal_reason.value,
        cancelled_actions=len(cancelled),
        in_flight_actions=in_flight,
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

    # The newest recommendation, not the one filed under the case's *current* cycle counter.
    # ``RecommendationRepository.active_decision_cycle`` documents why those differ; the
    # consequence here is that a lookup by the counter finds nothing and the pipeline stalls in
    # ``DECISION_PENDING``. Every subsequent lookup uses the recommendation's own cycle, so the
    # decision, the recommendation and the diagnosis it was built from all agree about which
    # cycle they describe.
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
        # R21.C3 and R21.C8. Resolved here rather than in the engine because
        # ``policy-isolation`` forbids ``revora.policy`` from importing ``revora.persistence``,
        # and keyed on the Suppression_Scope rather than the case, so a case opened later for a
        # suppressed order finds the suppression on its first cycle.
        contact_suppressed=suppression_in_force(session, merchant_id, case),
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


def _review_instant(
    *, moment: datetime, window_end_at: datetime, interval: timedelta
) -> datetime | None:
    """When a case that chose restraint should be looked at again, or ``None`` for not at all.

    ``min(moment + interval, window_end_at)`` is R30.C3's clamp. The ``review_within_window``
    CHECK on ``recovery_case`` refuses anything past the window end, so the clamp is verified
    below the application rather than trusted here — the arithmetic and the constraint agree,
    and if they ever stop agreeing the write fails loudly instead of scheduling a review for
    after the case is dead.

    ``None`` when the clamped instant lands at or before ``moment``, which is to say when the
    recovery window had already closed by the time the policy job recorded this selection — a
    case whose window elapsed between the optimizer's cycle and this job's run. The clamp
    would then yield an instant in the past, the sweeper would find the case due on its very
    next pass, and the review it triggered would spend a decision cycle on an evaluation whose
    window check refuses every candidate action including the null ones. Writing nothing leaves
    the case exactly where the lifecycle sweep expects to find it, and the lifecycle sweep is
    the component that owns an elapsed window (R2.C12). A review that cannot lead to an action
    is not a review; it is a busy loop that leaves audit records.
    """
    review_at = min(moment + interval, window_end_at)
    return review_at if review_at > moment else None


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


def _previously_selected_action(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> CandidateAction | None:
    """The action the case's most recent completed cycle selected, or ``None``.

    Read *before* the review's own optimizer runs, because the optimizer writes a
    recommendation that would then be the newest one and the record's "previous" and "new"
    fields would both name it — which is the exact comparison R30.C11 asks the record to
    make, collapsed into a tautology.

    ``None`` where the case has no recommendation at all, which is a case that reached
    ``POLICY_CHECK`` some other way. It is not the same as a selection of ``DO_NOTHING``, and
    reporting it as one would put a decision on the record that was never made.
    """
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case_id)
    if recommendation is None:
        return None
    return CandidateAction(recommendation.selected_action)


def _write_review_audit(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    config: Configuration,
    trigger: ReviewTrigger,
    previous_action: CandidateAction | None,
    new_action: CandidateAction | None,
    decision_cycle_count: int,
    next_review_at: datetime | None,
    unresolved_amount: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """One ``CASE_REVIEWED`` record per completed review (R30.C11).

    ``selection_changed`` is computed and stored rather than left for a reader to derive from
    the two action fields. The question the record exists to answer is "was restraint
    re-examined", and a boolean makes that a ``WHERE`` instead of a comparison every consumer
    has to get right — including the one that has to decide what a null previous action means.

    ``unresolved_amount`` is R30.C10's "record the unresolved payment_amount in minor currency
    units", carried on the terminating path and on the continuing one alike: what is still
    owed is the figure that makes both readable without a join back to the case.
    """
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=CASE_REVIEWED,
            actor=_REVIEW_ACTOR,
            action=None if new_action is None else new_action.value,
            decision={
                "review_trigger": trigger.value,
                "reviewed_at": moment.isoformat(),
                "previous_selected_action": (
                    None if previous_action is None else previous_action.value
                ),
                "new_selected_action": None if new_action is None else new_action.value,
                "selection_changed": previous_action is not new_action,
                "decision_cycle_count": decision_cycle_count,
                "next_review_at": None if next_review_at is None else next_review_at.isoformat(),
                "unresolved_amount": unresolved_amount,
                "config_version": config.version,
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
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

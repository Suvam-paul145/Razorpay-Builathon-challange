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
from revora.customer.arrangements import first_arrangement_request, hard_stop_recorded
from revora.customer.promises import follow_up_due_for_case, sweep_due_promises
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
from revora.persistence.repositories.customer import PromiseToPayRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import ensure_utc, now
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
    "handle_partial_arrangement",
    "handle_policy",
    "handle_promise_escalation",
    "handle_promise_sweep",
    "handle_review",
    "rule_set_from_config",
    "run_policy_evaluation",
]

_logger = get_logger(__name__)

_POLICY_ACTOR: Final = "policy_engine"
_REVIEW_ACTOR: Final = "review_engine"
_SUPPRESSION_ACTOR: Final = "contact_suppression"
_ARRANGEMENT_ACTOR: Final = "partial_arrangement"
"""The actor on an arrangement escalation's records.

Distinct from ``_SUPPRESSION_ACTOR`` even though both are this module escalating a case on
something a customer said, because the audit log is where the two are told apart afterwards: a
suppression ended contact permanently and an arrangement did not, and one actor name for both
would make "why did contact stop on this case?" unanswerable from the record."""

_PROMISE_ACTOR: Final = "promise_to_pay"
"""The actor on a beyond-window promise escalation's records.

A third name for a third reason a customer's own words end a case, and separate from the other
two on the same argument. All three escalate; none of them means the same thing afterwards. A
dispute ended contact, an arrangement request asked for different terms, and this one is the case
where the customer *intends to pay* and named a date the window cannot reach — which is the only
one of the three where a person picking the case up may well recover the money."""

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
    """Look at a case again, and start it on a fresh decision cycle.

    **Two source states, and they are two different questions with one answer.** A case that
    chose restraint rests at ``POLICY_CHECK`` and is looked at again under R30.C1. A case whose
    customer promised to pay rests at ``WAITING_FOR_OUTCOME`` — a promise can only be submitted
    with a Customer_Access_Token, and one is minted inside the transition into ``EXECUTING`` — and
    is looked at again under R24.C13 once its Follow_Up_Instant has been reached. The two take
    different edges into ``DECISION_PENDING`` (``REVIEW`` and ``REENTRY``), both already declared
    in :mod:`revora.domain.transitions` and both carrying ``decision_cycle_delta = 1``, so the cap
    below bounds them together and no new edge was needed for the second.

    Assembled from the four steps the forward path already uses — diagnosis, baseline,
    candidates, optimizer — rather than reimplementing any of them. A second implementation
    of "what does this case cost and what is it worth" would be a second answer, and the
    whole claim of R30 is that a reviewed case is decided on *exactly* the terms a new case
    is. So this handler contributes no arithmetic of its own. What it contributes is the
    ordering, the cap, and the record.

    **The decision-cycle numbering.** All four steps read ``case.decision_cycle_count`` off
    the row themselves and file under it; none of them takes a cycle number. Because they run
    *before* the transition into ``DECISION_PENDING`` — which is the edge that increments the
    counter — a case resting with the counter at ``n`` produces a
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
    different reason on the record. R24.C13's last clause is the same gate reached from
    ``WAITING_FOR_OUTCOME``: a due follow-up on a case whose cycle budget is spent stops the case
    with ``DECISION_CYCLE_LIMIT_REACHED`` rather than starting a cycle that cannot finish.

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
        #
        # ``WAITING_FOR_OUTCOME`` is admitted only with a due promise behind it, and the
        # condition is what keeps R24.C13 from widening into a general re-entry. A review job
        # enqueued while the case was at ``POLICY_CHECK`` can be claimed after the case has
        # advanced through execution — the dedupe key is the case, not the state it was in — and
        # admitting that job here would spend a decision cycle on a trigger whose evidence the
        # cycle it caused has already consumed. A due follow-up is different in kind: the promise
        # is the new evidence, it is still unactioned, and the sweep will keep finding it until a
        # cycle considers it.
        state = CaseState(case.state)
        admitted = (
            follow_up_due_for_case(session, merchant_id, case_id, instant=review_at_start)
            if state is CaseState.WAITING_FOR_OUTCOME
            else state is CaseState.POLICY_CHECK
        )
        if not admitted:
            _logger.info(
                "review skipped: the case is not in a state this review can act on",
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
# Partial_Arrangement_Request: escalate, record, and touch no money field (R22.C2, C7, C8, C10)
# ---------------------------------------------------------------------------


def handle_partial_arrangement(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    signal_id: uuid.UUID,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Escalate a case whose customer asked to pay differently. One transaction, no money moved.

    The whole of R22 is a statement about restraint, and this handler is where the restraint has
    to be visible. A customer asking to settle for less or to pay in instalments has asked Revora
    to agree to something, and Revora is structurally unable to: there is no field on the request
    for an amount, no column for one, and nothing on this path that reads one. So the consequence
    is the smallest one that is still an answer — the case ends ``ESCALATED`` with
    ``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT`` and a person picks it up.

    **Deliberately shorter than :func:`handle_contact_suppression`, and every omission is a
    requirement.** That handler cancels scheduled actions, records the in-flight ones, and ends
    contact permanently. This one does none of the three:

    * **Nothing is cancelled** and no ``ACTION_CANCELLED_*`` record is written. There is no
      provider client in this function's arguments, so no cancellation is issuable from here at
      all, and the terminal state is what stops a queued execution — ``execute_approved_action``
      re-requests authority against reloaded state and refuses a terminal case.
    * **No payment link is expired or modified** (R22.C8). A live link stays live for its
      remaining validity, which is the clause that makes the *whole feature* safe rather than
      merely correct: a customer who reads "we have passed this to a person" and then decides to
      pay in full still recovers the case under R10.C14, and the reconciliation edge counts that
      recovery exactly once. Expiring the link to tidy up would take that path away from them.
    * **No Contact_Suppression is written.** Asking about instalments is not an objection to the
      debt. Suppressing contact on it would make an arrangement request indistinguishable from a
      dispute, and it would do so in the direction that silently ends chasing.

    **The three money fields are untouched, and they are untouched by construction** (R22.C7).
    ``payment_amount``, ``currency`` and ``window_end_at`` are never assigned in this function or
    in anything it calls: ``apply_locked_transition`` writes ``state``, ``version``,
    ``terminal_reason``, the three counters, ``last_outbound_at`` and ``next_review_at``, and
    those are the entire set of columns a transition may move. So R22.C7 is a property of the one
    writer of case state rather than a discipline this handler keeps, which is why there is no
    assertion here restating it.

    **The unresolved amount is *recorded*, not changed.** ``CASE_ESCALATED`` carries
    ``int(case.payment_amount)`` — an ``int`` read from a ``BIGINT`` column, minor units, never a
    float and never re-derived from a formatted string. Recording it is what lets the ``ESCALATED``
    grouping name the money at stake (R22.C9) from the audit trail alone.

    **Where a Hard_Stop_Reason is also persisted, this yields** (R22.C10). A case holds one
    Terminal_State reason and the hard stop is the stronger statement, so the arrangement stays
    recorded as a Customer_Signal and applies **no second Terminal_State transition**. The check
    is :func:`~revora.customer.arrangements.hard_stop_recorded` — a read of the case's own
    signals — and it is a read rather than a lock ordering on purpose: resolving the collision by
    "whichever job runs first" would make the recorded reason a function of queue order, so the
    same two submissions would end one case ``CUSTOMER_DISPUTED_CHARGE`` and the next
    ``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT``. Yielding to the hard stop is deterministic in both
    orders: if the suppression handler has already run the case is terminal and the early return
    below covers it, and if it has not, this returns and leaves the case for it.

    The ordering inside the transaction is :func:`handle_contact_suppression`'s, minus the two
    steps it has that this does not:

    1. **Lock the case**, so everything after is serialized against the pipeline's own writer.
    2. **Return early if the case is already terminal** — not an error and not rare. A sweep can
       expire the case seconds after the customer submits, the suppression handler may have got
       there first, and a retried job finds the case this handler itself escalated. In all three
       the correct action is none, and this early return is also what makes the handler idempotent
       without a flag column.
    3. **Return early if a Hard_Stop_Reason is persisted** (R22.C10).
    4. **Write ``CASE_ESCALATED``** carrying the unresolved amount, before the transition, because
       the audit sequence is allocated under the case row lock this transaction already holds and
       the transition must be the last thing that happens — so a failure anywhere above it leaves
       the case exactly where it was.
    5. **Transition to ``ESCALATED``** through ``apply_locked_transition``, in its locked form
       because this transaction already holds the row and the wrapper would deadlock against
       itself.

    Args:
        signal_id: the ``customer_signal`` row that queued this escalation, from the job payload.
            It travels rather than being re-derived, on the same terms the Hard_Stop_Reason
            travels on a suppression job: the record names its cause without a reader having to
            infer which of a case's signals was the trigger. The request *instant* is read back
            instead of travelling, because a retried job must produce the same record as the first
            attempt and a payload copy of a column is a copy that can disagree with it.
    """
    with tenant_transaction(merchant_id) as session:
        config = ConfigurationRepository(session).load(merchant_id)
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:  # pragma: no cover - RESTRICT makes a case undeletable
            _logger.warning("arrangement consequence on missing case", case_id=str(case_id))
            return

        state = CaseState(case.state)
        if is_terminal(state):
            _logger.info(
                "arrangement escalation skipped: case already terminal",
                case_id=str(case_id),
                state=state.value,
                terminal_reason=case.terminal_reason,
            )
            return

        hard_stop = hard_stop_recorded(session, merchant_id, case_id)
        if hard_stop is not None:
            # R22.C10. The signal is already persisted — that happened in the accepting request —
            # so "recorded as a Customer_Signal without a second Terminal_State transition" is
            # satisfied by returning and writing nothing. No audit record either: the escalation
            # that does happen is the suppression's, its CASE_ESCALATED names the Hard_Stop_Reason,
            # and a second record saying "and also this" would put two escalations of one case in
            # the log for one ending.
            _logger.info(
                "arrangement escalation yielded to a hard stop",
                case_id=str(case_id),
                hard_stop_reason=hard_stop.value,
            )
            return

        request = first_arrangement_request(session, merchant_id, case_id)
        writer = AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        unresolved_amount = int(case.payment_amount)

        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CASE_ESCALATED,
                actor=_ARRANGEMENT_ACTOR,
                previous_state=state.value,
                new_state=CaseState.ESCALATED.value,
                decision={
                    "terminal_reason": (
                        TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT.value
                    ),
                    "signal_id": str(signal_id),
                    # R22.C2: the unresolved amount in minor currency units.
                    "unresolved_amount": unresolved_amount,
                    # The currency travels with the amount so a reader never has to guess which
                    # one an integer counts. Read off the case, not defaulted.
                    "currency": str(case.currency),
                    "requested_at": (
                        None if request is None else request.requested_at.isoformat()
                    ),
                    # *Whether* a note was written, never the note. The note is free text a
                    # stranger typed on a public endpoint and the audit log is the one store that
                    # cannot be rewritten, so it is retained on ``customer_signal`` where the
                    # retention sweep can reach it (R29.C10) and presented from there.
                    "note_present": request is not None and request.note is not None,
                    # Named so the record says what did *not* happen. R22.C8 is the clause a
                    # reader is most likely to doubt, and an audit trail that only records
                    # actions taken cannot answer "was the link cancelled?" at all.
                    "detail": (
                        "customer asked to pay differently; escalated to a person. No amount, "
                        "instalment count or schedule was accepted, payment_amount, currency and "
                        "window_end_at are unchanged, and any live payment link is left live and "
                        "unmodified for its remaining validity"
                    ),
                },
            ),
            correlation_id=correlation_id,
        )

        # Terminal, so the training set is owed a row (R15.C1) — and this population especially,
        # because a case where the customer engaged and asked for different terms is evidence a
        # model needs about which contact is worth making.
        _, rejection = apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.ESCALATED,
            reason="customer requested a partial arrangement",
            actor=_ARRANGEMENT_ACTOR,
            terminal_reason=TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
            correlation_id=correlation_id,
            on_success=observation_writer(config, correlation_id=correlation_id),
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        if rejection is not None:  # pragma: no cover - every non-terminal edge is legal
            _logger.warning(
                "arrangement escalation refused",
                case_id=str(case_id),
                outcome=rejection.outcome.value,
                state=state.value,
            )
            return

    _logger.info(
        "partial arrangement escalation applied",
        case_id=str(case_id),
        signal_id=str(signal_id),
        unresolved_amount=unresolved_amount,
    )


# ---------------------------------------------------------------------------
# Promise_To_Pay beyond the window: escalate, and extend nothing (R23.C5, C6)
# ---------------------------------------------------------------------------


def handle_promise_escalation(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    promise_id: uuid.UUID,
    signal_id: uuid.UUID,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Escalate a case whose customer promised a date the Recovery_Window cannot reach.

    **The window is not extended, and the whole requirement is that absence.** R2.C5 makes
    ``window_end_at`` immutable once a case opens, and that immutability is what makes R2.C12's
    termination bound provable — a promise that could extend the window would remove the
    guarantee, not merely bend it. So a Promise_Date at or past the window end is not a scheduling
    problem to be solved by stretching the window; it is a case for a person, and this handler is
    the person's inbox.

    Like :func:`handle_partial_arrangement`, and shorter than
    :func:`handle_contact_suppression`, with every omission deliberate:

    * **Nothing is cancelled** and no ``ACTION_CANCELLED_*`` record is written. There is no
      provider client in this function's arguments, so no cancellation is issuable from here.
    * **No payment link is expired or modified.** This is the escalation where that matters most
      of the three: the customer said they *will* pay, just later than the window allows, so a
      live link left live is exactly the path by which the money still arrives — and R10.C14's
      reconciliation edge counts that recovery once, from a terminal case.
    * **The promise's own status is not touched.** It is already ``BEYOND_WINDOW_ESCALATED``,
      written by the transaction that accepted the submission, and ``escalated_schedules_nothing``
      means it carries no Follow_Up_Instant to cancel. R23.C12's ``VOIDED`` is for a promise still
      ``RECORDED`` when its case ends, which this one never was.

    **The window end is untouched by construction** (R23.C4). ``apply_locked_transition`` writes
    ``state``, ``version``, ``terminal_reason``, the three counters, ``last_outbound_at`` and
    ``next_review_at`` — that is the entire set of columns a transition may move, and
    ``window_end_at`` is not among them. So R23.C4 is a property of the only writer of case state
    rather than a discipline this handler keeps, which is why there is no assertion here restating
    it. Property 41 asserts it from the outside.

    **Where a Hard_Stop_Reason is also persisted, this yields**, on exactly
    :func:`handle_partial_arrangement`'s argument and through the same
    :func:`~revora.customer.arrangements.hard_stop_recorded` read. A case holds one Terminal_State
    reason, the hard stop is the stronger statement — an objection to the debt rather than a date
    Revora cannot reach — and resolving the collision by queue order would make the recorded
    reason a function of which job ran first. R22.C10 says this about an arrangement request; the
    same reasoning is what makes it correct here, and applying it to both is what keeps the
    recorded reason deterministic in every order the two jobs can run in.

    The ordering inside the transaction is :func:`handle_partial_arrangement`'s:

    1. **Lock the case.**
    2. **Return early if the case is already terminal** — a sweep may have expired it, the
       suppression handler may have got there first, and a retried job finds the case this handler
       itself escalated. This early return is what makes the handler idempotent without a flag
       column.
    3. **Return early if a Hard_Stop_Reason is persisted.**
    4. **Write ``CASE_ESCALATED``** carrying the unresolved amount and the submitted Promise_Date,
       both of which R23.C5 names, before the transition — the audit sequence is allocated under
       the case row lock this transaction already holds, and the transition must be last so a
       failure above it leaves the case where it was.
    5. **Transition to ``ESCALATED``** with ``PROMISE_BEYOND_RECOVERY_WINDOW``.

    Args:
        promise_id: the ``promise_to_pay`` row that queued this escalation. The Promise_Date is
            read back through it rather than travelling on the payload, because a retried job must
            produce the same record as the first attempt and a payload copy of a column can
            disagree with it.
        signal_id: the ``customer_signal`` row the promise names, so an audit reader can join the
            escalation to the submission without inferring which of a case's signals caused it.
    """
    with tenant_transaction(merchant_id) as session:
        config = ConfigurationRepository(session).load(merchant_id)
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:  # pragma: no cover - RESTRICT makes a case undeletable
            _logger.warning("promise escalation on missing case", case_id=str(case_id))
            return

        state = CaseState(case.state)
        if is_terminal(state):
            _logger.info(
                "promise escalation skipped: case already terminal",
                case_id=str(case_id),
                state=state.value,
                terminal_reason=case.terminal_reason,
            )
            return

        hard_stop = hard_stop_recorded(session, merchant_id, case_id)
        if hard_stop is not None:
            # The stronger statement wins, and no audit record is written here: the escalation
            # that does happen is the suppression's, its CASE_ESCALATED names the
            # Hard_Stop_Reason, and a second record saying "and also this" would put two
            # escalations of one case in the log for one ending.
            _logger.info(
                "promise escalation yielded to a hard stop",
                case_id=str(case_id),
                hard_stop_reason=hard_stop.value,
            )
            return

        promise = PromiseToPayRepository(session).get(merchant_id, promise_id)
        writer = AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        unresolved_amount = int(case.payment_amount)

        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CASE_ESCALATED,
                actor=_PROMISE_ACTOR,
                previous_state=state.value,
                new_state=CaseState.ESCALATED.value,
                decision={
                    "terminal_reason": TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW.value,
                    "promise_id": str(promise_id),
                    "signal_id": str(signal_id),
                    # R23.C5 names both of these explicitly: the unresolved payment_amount and the
                    # submitted Promise_Date. An int of minor currency units, never a float and
                    # never re-derived from a formatted string.
                    "unresolved_amount": unresolved_amount,
                    "currency": str(case.currency),
                    "promise_date": (
                        None if promise is None else ensure_utc(promise.promise_date).isoformat()
                    ),
                    # The window end the promise was measured against, so a reader can see that
                    # the date was past it without joining — and can see that it is still the
                    # value it was when the case opened.
                    "window_end_at": ensure_utc(case.window_end_at).isoformat(),
                    # Named so the record says what did *not* happen. R23.C4 is the clause a
                    # reader is most likely to doubt, and an audit trail that only recorded
                    # actions taken could not answer "was the window extended?" at all.
                    "detail": (
                        "the customer named a date at or past the recovery window end, or one "
                        "leaving no room for a follow-up inside it; escalated to a person. "
                        "window_end_at is unchanged, no PROMISE_TO_PAY_FOLLOW_UP was scheduled, "
                        "and any live payment link is left live and unmodified for its remaining "
                        "validity"
                    ),
                },
            ),
            correlation_id=correlation_id,
        )

        # Terminal, so the training set is owed a row (R15.C1) — and this population especially:
        # a customer who engaged and named a date is evidence a model needs about which contact is
        # worth making, even though the date was one the window could not reach.
        _, rejection = apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.ESCALATED,
            reason="promised date beyond the recovery window",
            actor=_PROMISE_ACTOR,
            terminal_reason=TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW,
            correlation_id=correlation_id,
            on_success=observation_writer(config, correlation_id=correlation_id),
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        if rejection is not None:  # pragma: no cover - every non-terminal edge is legal
            _logger.warning(
                "promise escalation refused",
                case_id=str(case_id),
                outcome=rejection.outcome.value,
                state=state.value,
            )
            return

    _logger.info(
        "promise beyond window escalation applied",
        case_id=str(case_id),
        promise_id=str(promise_id),
        unresolved_amount=unresolved_amount,
    )


def handle_promise_sweep(
    merchant_id: uuid.UUID, *, correlation_id: uuid.UUID | None = None
) -> None:
    """One pass of the promise sweep (R23.C13). Delegates entirely.

    The body is :func:`revora.customer.promises.sweep_due_promises`, one layer down, and this
    exists only to give ``PROMISE_SWEEP_KIND`` a handler with the signature every other
    sweep handler has — on the same terms :func:`handle_execution` and
    :func:`handle_outcome` delegate to the modules
    that own their guarantees. A second opinion here about which promises are due would be a
    second reading of the partial index the claim is served by.
    """
    sweep_due_promises(merchant_id, correlation_id=correlation_id)


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

"""The review sweeper: the eighth periodic sweep, and the one enqueue every trigger shares.

A case that chose restraint rests at ``POLICY_CHECK`` carrying a ``next_review_at``
instant. Nothing in the base spec could begin a second decision cycle for such a case —
the only re-entry edge into ``DECISION_PENDING`` came from ``WAITING_FOR_OUTCOME``, which
is reachable only after a confirmed intervention, so Revora re-decided exactly the cases
it had already acted on and never the ones where waiting had been the right answer. This
module is the half of the fix that finds those cases; :func:`revora.jobs.pipeline.handle_review`
is the half that decides.

**This module only enqueues.** It applies no transition, evaluates no policy and calls no
provider. Every consequence of a review is applied by the worker through
``apply_transition``, which stays the only writer of ``recovery_case.state``. The same is
true of the other two triggers, which is why they all go through
:func:`enqueue_case_review` rather than each building a job of their own.

Structurally this copies :func:`revora.cases.sweeper.sweep_expired_cases`: read the due
set in one transaction, release it, then act on each case in its own transaction. The read
is not held across the writes, so a backlog does not lock thousands of rows for the length
of a batch, and ``DEFAULT_SWEEP_LIMIT`` — the same bound, imported rather than restated —
turns a backlog after an outage into bounded passes. A case whose version moved between
the read and its own transaction is skipped rather than treated as an error: the sweep runs
every ``REVIEW_SWEEP_INTERVAL``, so "revisited next pass" is a complete answer.

**Restart independence (R30.C6).** Every input is a persisted column on ``recovery_case``:
``state``, ``next_review_at`` and ``decision_cycle_count``. No in-memory schedule and no
queue state is consulted, so a sweep starting with an empty job queue after a process
restart finds exactly the same due set it would have found without one.

**Idempotency (R30.C9, P65) comes from the job table's existing partial unique index.**
``one_pending_job_per_dedupe_key`` is unique over *pending* jobs, so a second enqueue of
``case_review:{case_id}`` while the first is still pending returns ``None`` and no second
decision cycle exists. All three triggers use that one key, which is what makes R30.C9 hold
"irrespective of the Review_Trigger" through one mechanism rather than three.

The rejected alternative was a ``review_enqueued_at`` column on ``recovery_case``. It is a
second copy of queue state, and two records of "is there work pending for this case" drift.
The drift is not benign in either direction: the column set with no job means the case stops
being reviewed until its window closes, and the column clear with a job pending means a
duplicated decision cycle. The index is already the authority on that question, and it is
the authority the worker's claim actually consults.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from revora.cases.sweeper import DEFAULT_SWEEP_LIMIT
from revora.domain.enums import CaseState, ReviewTrigger
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = ["CASE_REVIEW_KIND", "enqueue_case_review", "sweep_due_reviews"]

_logger = get_logger(__name__)

CASE_REVIEW_KIND: str = "case_review"
"""The job kind for both the periodic review sweep and one case's review.

Declared here rather than in ``revora.jobs.scheduler`` for a layering reason, and the
layering reason is the right one: all three triggers enqueue this kind, and two of them
(detection, and the customer response surface) sit below ``revora.jobs`` and cannot import
it. A constant each of them spelled for itself would give three spellings of one dedupe
key, which is precisely the defect the key exists to prevent. ``scheduler`` imports it from
here and appends it to ``PERIODIC_SWEEP_KINDS``.

One kind serves two shapes of work, distinguished by whether the payload carries a
``case_id``: without one it is the sweep, with one it is that case's review. They share a
kind because they are one mechanism — the sweep exists only to enqueue the reviews — and
their dedupe keys are disjoint by construction (``case_review:{merchant}:{bucket}`` for the
sweep, ``case_review:{case_id}`` for a review), so neither can swallow the other."""


def enqueue_case_review(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    trigger: ReviewTrigger,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Enqueue one decision cycle for a case, in the caller's transaction.

    The single entry point for all three Review_Triggers, so the dedupe key and the payload
    shape exist in one place. Returns the job id, or ``None`` when a review for this case is
    already pending — which is R30.C9's guarantee, delivered by the partial unique index on
    ``job`` rather than by a read-then-write here that two callers could interleave.

    In the caller's transaction on purpose. R30.C7 and R30.C8 both require the enqueue to be
    atomic with the thing that caused it — the event attach and its audit record, or the
    accepted customer signal — and a queue that is a table is the whole reason that is
    possible.
    """
    return JobRepository(session).enqueue(
        merchant_id,
        kind=CASE_REVIEW_KIND,
        payload={
            "case_id": str(case_id),
            "correlation_id": None if correlation_id is None else str(correlation_id),
            "review_trigger": trigger.value,
        },
        run_after=now(),
        dedupe_key=f"{CASE_REVIEW_KIND}:{case_id}",
        case_id=case_id,
        correlation_id=correlation_id,
    )


def sweep_due_reviews(
    merchant_id: uuid.UUID,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
    factory: sessionmaker[Session] | None = None,
    correlation_id: uuid.UUID | None = None,
) -> int:
    """Enqueue one decision cycle for every case whose review instant has been reached.

    Returns the number of reviews enqueued, which is at most the number of cases found due:
    a case already holding a pending review contributes nothing, and that is the sweep
    working rather than failing.

    Cases already at ``MAX_RECOVERY_ATTEMPTS`` are excluded by the query, so the sweep does
    not queue work whose only possible outcome is a transition to ``STOPPED``. The bound is
    *also* checked by the handler under the row lock, and both checks are needed for
    different reasons: this one is about not queueing pointless jobs, the handler's is about
    the bound itself, because this read releases before the handler acts and a concurrent
    cycle can reach the cap in between.
    """
    moment = now()
    with tenant_transaction(merchant_id, factory) as session:
        config = ConfigurationRepository(session).load(merchant_id)
        due = [
            (case.id, case.version)
            for case in RecoveryCaseRepository(session).list_due_for_review(
                merchant_id,
                now=moment,
                max_decision_cycles=config.MAX_RECOVERY_ATTEMPTS,
                limit=limit,
            )
        ]

    enqueued = 0
    for case_id, version in due:
        if _enqueue_one(
            merchant_id,
            case_id,
            expected_version=version,
            max_decision_cycles=config.MAX_RECOVERY_ATTEMPTS,
            factory=factory,
            correlation_id=correlation_id,
        ):
            enqueued += 1
    if enqueued:
        _logger.info(
            "review sweep enqueued decision cycles",
            merchant_id=str(merchant_id),
            enqueued=enqueued,
            considered=len(due),
        )
    return enqueued


def _enqueue_one(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    expected_version: int,
    max_decision_cycles: int,
    factory: sessionmaker[Session] | None,
    correlation_id: uuid.UUID | None,
) -> bool:
    """Re-check one case under its row lock and enqueue its review. Its own transaction.

    The re-check is why this takes the lock for an operation that writes no case column.
    The due set was read in a transaction that has since committed, so by now the case may
    have been approved and scheduled, terminated, or already reviewed by a trigger of
    another kind. Enqueuing regardless would be harmless in outcome — the handler re-checks
    the state under its own lock and returns — but it would put a job in the queue for every
    stale row on every pass, and a queue full of jobs that do nothing is how a real backlog
    becomes invisible.

    A version that moved is not an error and is not retried here: the case is revisited on
    the next pass, at most ``REVIEW_SWEEP_INTERVAL`` later.
    """
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:  # pragma: no cover - a case is never deleted
            return False
        if case.version != expected_version:
            _logger.debug(
                "review skipped: case moved since the due set was read",
                case_id=str(case_id),
                expected_version=expected_version,
                observed_version=case.version,
            )
            return False
        if (
            CaseState(case.state) is not CaseState.POLICY_CHECK
            or case.next_review_at is None
            or case.decision_cycle_count >= max_decision_cycles
        ):
            return False
        job_id = enqueue_case_review(
            session,
            merchant_id,
            case_id,
            trigger=ReviewTrigger.SCHEDULED_REVIEW,
            correlation_id=correlation_id,
        )
    return job_id is not None

"""The Postgres job queue: claim, backoff, dead-letter, attempt history.

No broker and no Redis (ADR-3). The decisive reason is transactional: a job is
enqueued in the same transaction as the state change it follows, which a broker
cannot do without an outbox — and an outbox is this table with extra hops.

The claim uses ``FOR UPDATE SKIP LOCKED`` so two workers never wait on each other for
a job either could take. A claimed job is marked ``RUNNING`` and its lock released on
commit; a worker that then dies leaves it ``RUNNING`` with a stale ``locked_at``, and
:func:`reclaim_stale` returns it to ``PENDING`` after the lease. That is why a handler
must be idempotent: a job can run more than once, and correctness never depends on it
running exactly once — every timing rule is also enforced by the sweeper from
persisted timestamps.

Failure is bounded. Each attempt is recorded in ``job_attempt`` — the whole history,
not just a counter, because "failed six times" and "failed six times with the same
provider timeout" call for different responses. Past the attempt cap a job moves to
``DEAD_LETTER`` and an audit record names it, rather than retrying forever.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select

from revora.audit.events import JOB_DEAD_LETTERED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.persistence.models import Job, JobAttempt
from revora.persistence.models.jobs import PENDING_STATE
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import Session, sessionmaker
except ImportError:  # pragma: no cover
    Session = object  # type: ignore[assignment,misc]
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = [
    "BACKOFF_BASE",
    "BACKOFF_CAP",
    "DEAD_LETTER_STATE",
    "DONE_STATE",
    "RUNNING_STATE",
    "ClaimedJob",
    "claim_one",
    "complete",
    "enqueue",
    "fail",
    "reclaim_stale",
]

_logger = get_logger(__name__)

RUNNING_STATE: Final[str] = "RUNNING"
DONE_STATE: Final[str] = "DONE"
DEAD_LETTER_STATE: Final[str] = "DEAD_LETTER"

BACKOFF_BASE: Final[timedelta] = timedelta(seconds=5)
"""First retry delay. Doubles each attempt."""

BACKOFF_CAP: Final[timedelta] = timedelta(minutes=30)
"""Ceiling on the retry delay, so a stuck job retries hourly-ish rather than never."""

_MAX_LAST_ERROR = 2000
"""Truncation bound for the stored error string. A job payload carries no PII, and a
handler error string is truncated rather than masked field-by-field because it is
free text with no declared field kinds."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A detached snapshot of a claimed job.

    Detached on purpose: the claim transaction commits before the handler runs, so
    the handler works from plain values and never touches an expired ORM row. The
    handler reads everything else it needs from the rows the ids point at, in its own
    scoped transaction.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempt_no: int
    max_attempts: int
    case_id: uuid.UUID | None
    correlation_id: uuid.UUID | None


def enqueue(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    kind: str,
    payload: dict[str, Any],
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    case_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    max_attempts: int = 5,
) -> uuid.UUID | None:
    """Enqueue a job in the caller's transaction. See ``JobRepository.enqueue``.

    A thin pass-through so jobs-layer callers do not reach into the persistence
    repository, and so ``run_after`` defaults to now for an immediately-runnable job.
    """
    return JobRepository(session).enqueue(
        merchant_id,
        kind=kind,
        payload=payload,
        run_after=run_after or now(),
        dedupe_key=dedupe_key,
        case_id=case_id,
        correlation_id=correlation_id,
        max_attempts=max_attempts,
    )


def claim_one(
    merchant_id: uuid.UUID,
    *,
    worker_id: str,
    factory: sessionmaker[Session] | None = None,
) -> ClaimedJob | None:
    """Claim the oldest due job for a merchant, or ``None`` if there is none.

    Marks it ``RUNNING`` and opens its ``job_attempt`` row in one transaction, so a
    concurrent worker's ``SKIP LOCKED`` claim passes it over. Returns a detached
    snapshot for the handler to work from.
    """
    moment = now()
    with tenant_transaction(merchant_id, factory) as session:
        jobs = JobRepository(session).claim_pending(merchant_id, now=moment, limit=1)
        if not jobs:
            return None
        job = jobs[0]
        job.state = RUNNING_STATE
        job.locked_by = worker_id
        job.locked_at = moment
        job.attempts += 1
        attempt_no = job.attempts
        session.add(
            JobAttempt(
                merchant_id=merchant_id,
                job_id=job.id,
                attempt_no=attempt_no,
                started_at=moment,
                worker_id=worker_id,
            )
        )
        return ClaimedJob(
            id=job.id,
            merchant_id=merchant_id,
            kind=job.kind,
            payload=dict(job.payload),
            attempt_no=attempt_no,
            max_attempts=job.max_attempts,
            case_id=job.case_id,
            correlation_id=job.correlation_id,
        )


def complete(
    claimed: ClaimedJob,
    *,
    outcome: str = "SUCCESS",
    factory: sessionmaker[Session] | None = None,
) -> None:
    """Mark a claimed job done and close its attempt row."""
    finished = now()
    with tenant_transaction(claimed.merchant_id, factory) as session:
        job = _get_job(session, claimed)
        if job is not None:
            job.state = DONE_STATE
            job.locked_by = None
            job.locked_at = None
        _close_attempt(session, claimed, finished=finished, outcome=outcome)


def fail(
    claimed: ClaimedJob,
    *,
    error_class: str,
    error_detail: str,
    factory: sessionmaker[Session] | None = None,
) -> bool:
    """Record a failed attempt and either reschedule with backoff or dead-letter.

    Returns ``True`` if the job dead-lettered (attempt cap reached), ``False`` if it
    was rescheduled. A dead-letter writes a ``JOB_DEAD_LETTERED`` audit record so a
    poison job is visible rather than silently abandoned.
    """
    finished = now()
    truncated = error_detail[:_MAX_LAST_ERROR]
    dead_lettered = claimed.attempt_no >= claimed.max_attempts
    with tenant_transaction(claimed.merchant_id, factory) as session:
        job = _get_job(session, claimed)
        if job is not None:
            job.locked_by = None
            job.locked_at = None
            job.last_error = truncated
            if dead_lettered:
                job.state = DEAD_LETTER_STATE
            else:
                job.state = PENDING_STATE
                job.run_after = finished + _backoff(claimed.attempt_no)
        _close_attempt(
            session,
            claimed,
            finished=finished,
            outcome="DEAD_LETTER" if dead_lettered else "RETRY",
            error_class=error_class,
            error_detail=truncated,
        )
        if dead_lettered:
            _audit_dead_letter(session, claimed, error_class=error_class)
    if dead_lettered:
        _logger.error("job dead-lettered", job_kind=claimed.kind, attempts=claimed.attempt_no)
    return dead_lettered


def reclaim_stale(
    merchant_id: uuid.UUID,
    *,
    lease: timedelta,
    factory: sessionmaker[Session] | None = None,
) -> int:
    """Return ``RUNNING`` jobs whose lease has expired to ``PENDING``.

    A worker that died mid-job left the row ``RUNNING`` with a stale ``locked_at``.
    After the lease, the job is claimable again — which is safe precisely because
    handlers are idempotent. Returns the number reclaimed.
    """
    moment = now()
    cutoff = moment - lease
    with tenant_transaction(merchant_id, factory) as session:
        stale = (
            session.execute(
                select(Job).where(
                    Job.merchant_id == merchant_id,
                    Job.state == RUNNING_STATE,
                    Job.locked_at.is_not(None),
                    Job.locked_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for job in stale:
            job.state = PENDING_STATE
            job.locked_by = None
            job.locked_at = None
            job.run_after = moment
    if stale:
        _logger.warning("reclaimed stale running jobs", count=len(stale))
    return len(stale)


def _backoff(attempt_no: int) -> timedelta:
    seconds = BACKOFF_BASE.total_seconds() * (2 ** max(0, attempt_no - 1))
    return min(BACKOFF_CAP, timedelta(seconds=seconds))


def _get_job(session: Session, claimed: ClaimedJob) -> Job | None:
    return session.execute(
        select(Job).where(Job.merchant_id == claimed.merchant_id, Job.id == claimed.id)
    ).scalar_one_or_none()


def _close_attempt(
    session: Session,
    claimed: ClaimedJob,
    *,
    finished: datetime,
    outcome: str,
    error_class: str | None = None,
    error_detail: str | None = None,
) -> None:
    attempt = session.execute(
        select(JobAttempt).where(
            JobAttempt.merchant_id == claimed.merchant_id,
            JobAttempt.job_id == claimed.id,
            JobAttempt.attempt_no == claimed.attempt_no,
        )
    ).scalar_one_or_none()
    if attempt is None:  # pragma: no cover - claim always opens one
        return
    attempt.finished_at = finished
    attempt.outcome = outcome
    attempt.error_class = error_class
    attempt.error_detail = error_detail
    if attempt.started_at is not None:
        attempt.duration_ms = max(0, int((finished - attempt.started_at).total_seconds() * 1000))


def _audit_dead_letter(session: Session, claimed: ClaimedJob, *, error_class: str) -> None:
    entry = AuditEntry(
        event_type=JOB_DEAD_LETTERED,
        actor="job_queue",
        action_result=error_class,
        evidence={"kind": claimed.kind, "attempts": claimed.attempt_no},
    )
    writer = AuditWriter(session)
    if claimed.case_id is not None:
        writer.write_for_case(
            claimed.merchant_id, claimed.case_id, entry, correlation_id=claimed.correlation_id
        )
    else:
        writer.write_unattached(
            claimed.merchant_id, entry, correlation_id=claimed.correlation_id
        )

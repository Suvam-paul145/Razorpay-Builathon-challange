"""The job queue and its attempt history.

The queue is a table rather than a broker for one decisive reason: a job must be
enqueued in the same transaction as the state change it follows. A broker cannot do
that without an outbox, and an outbox is this table with extra hops.

``UNIQUE (dedupe_key) WHERE state = 'PENDING'`` is what stops a periodic sweep from
enqueuing itself twice when two schedulers overlap or one restarts mid-tick. Partial
on ``PENDING`` deliberately: once a job has been claimed, the same dedupe key must
be enqueueable again for the next tick, otherwise the sweep runs once and never
again.

``job.payload`` carries ids and the correlation id only — never PII and never
secrets. A queue row is the least protected durable object in the system: it is read
by every worker, it appears in operator queries, and a failed job's payload ends up
in an error report.

``job_attempt`` keeps the per-attempt history rather than only a counter, because
"this job failed six times" and "this job failed six times with the same provider
timeout" call for different responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.persistence.models.base import TIMESTAMPTZ, RowBase

__all__ = ["PENDING_STATE", "Job", "JobAttempt"]

PENDING_STATE: str = "PENDING"
"""The one state the dedupe constraint applies to. Named here so the model, the
migration and the queue implementation cannot spell it differently."""


class Job(RowBase):
    """One unit of deferred work, claimed with ``FOR UPDATE SKIP LOCKED``."""

    __tablename__ = "job"

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """Ids and the correlation id. Anything a handler needs beyond that, it reads
    from the row the id points at, inside its own transaction, under the merchant
    scoping the repository enforces."""

    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=PENDING_STATE)
    run_after: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="5")
    """Per-job rather than global: a provider call and a metrics rollup do not
    deserve the same number of retries."""

    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """Set at claim. A worker that dies leaves these set, and the lease sweep
    returns the job to ``PENDING`` — which is why a handler must be idempotent."""

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    """Truncated, and masked. An error string from a provider call can contain the
    request it was made with."""

    __table_args__ = (
        # Prevents duplicate sweeps. Partial on PENDING so the same key can be
        # enqueued again on the next tick, once this one has been claimed.
        Index(
            "one_pending_job_per_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text(f"state = '{PENDING_STATE}' AND dedupe_key IS NOT NULL"),
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempt_counts_sane"),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'DEAD_LETTER')",
            name="state_known",
        ),
        # Reason: queue claim. The single hottest query in the worker loop —
        # pending jobs whose run_after has passed, oldest first.
        Index("ix_job_state_run_after", "state", "run_after"),
        Index("ix_job_merchant_id_kind", "merchant_id", "kind"),
    )


class JobAttempt(RowBase):
    """One attempt at one job, kept as history."""

    __tablename__ = "job_attempt"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    """``CASCADE`` here and nowhere else: attempt history has no meaning without
    the job, and unlike an audit record it is diagnostic rather than evidential."""

    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    outcome: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_job_id_attempt_no"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
    )

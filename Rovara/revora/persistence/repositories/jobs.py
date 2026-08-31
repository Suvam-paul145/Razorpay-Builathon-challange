"""Job reads and the one deliberate cross-tenant query in the whole package.

The queue itself is task 7's work. What lives here is the merchant-scoped read side,
plus the single exception to the merchant-scoping rule, called out explicitly rather
than hidden inside a worker loop.

**The exception.** A worker has to decide which tenant to serve next before it can
bind a transaction to one. That decision cannot itself be merchant-scoped without
becoming "poll every merchant in turn", which does not survive a tenant list of any
size. So :func:`claimable_merchant_ids` reads ``job`` across tenants and returns
merchant ids and nothing else — no payload, no case id, no row. It is the only
function in this package without a ``merchant_id`` parameter, and it returns no
tenant data by construction, so a bug in it cannot leak one merchant's information
to another. Everything the worker does after that goes through
``tenant_transaction``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from revora.persistence.models import Job, JobAttempt
from revora.persistence.models.jobs import PENDING_STATE
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.session import for_update_skip_locked

__all__ = ["JobAttemptRepository", "JobRepository", "claimable_merchant_ids"]


class JobRepository(MerchantScopedRepository[Job]):
    """Merchant-scoped queue reads and claims."""

    model = Job

    def claim_pending(
        self,
        merchant_id: uuid.UUID,
        *,
        now: datetime,
        limit: int,
    ) -> Sequence[Job]:
        """Claim due pending jobs for one merchant, skipping locked rows.

        ``FOR UPDATE SKIP LOCKED`` rather than plain ``FOR UPDATE``: two workers must
        not wait on each other for a job either could take. Served by
        ``ix_job_state_run_after``, which exists for this query.
        """
        statement = for_update_skip_locked(
            select(Job)
            .where(
                Job.merchant_id == merchant_id,
                Job.state == PENDING_STATE,
                Job.run_after <= now,
            )
            .order_by(Job.run_after)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def pending_count(self, merchant_id: uuid.UUID) -> int:
        """How many jobs are waiting for one merchant. Feeds the queue-depth alarm."""
        statement = self.scoped(merchant_id).where(Job.state == PENDING_STATE)
        return len(list(self.session.execute(statement).scalars()))

    def find_pending_by_dedupe_key(
        self, merchant_id: uuid.UUID, dedupe_key: str
    ) -> Job | None:
        """The pending job holding a dedupe key, if any.

        A read-side convenience only. The guarantee that there is at most one comes
        from ``one_pending_job_per_dedupe_key``, not from checking here first — that
        check would race with the insert it precedes.
        """
        statement = self.scoped(merchant_id).where(
            Job.state == PENDING_STATE, Job.dedupe_key == dedupe_key
        )
        return self.session.execute(statement).scalar_one_or_none()


class JobAttemptRepository(MerchantScopedRepository[JobAttempt]):
    """Per-attempt history, kept so a repeated failure is diagnosable."""

    model = JobAttempt

    def list_for_job(self, merchant_id: uuid.UUID, job_id: uuid.UUID) -> Sequence[JobAttempt]:
        """Every attempt at one job, in order."""
        statement = (
            self.scoped(merchant_id)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_no)
        )
        return list(self.session.execute(statement).scalars())


def claimable_merchant_ids(session: Session, *, now: datetime, limit: int) -> Sequence[uuid.UUID]:
    """Which merchants have work due, oldest work first.

    The one function here without a ``merchant_id`` argument, and the only one. It
    returns merchant ids and nothing else: no payload, no case id, no row. The worker
    calls it to decide which tenant to bind the next transaction to, and every read
    after that point is scoped.
    """
    statement = (
        select(Job.merchant_id)
        .where(Job.state == PENDING_STATE, Job.run_after <= now)
        .group_by(Job.merchant_id)
        .order_by(Job.merchant_id)
        .limit(limit)
    )
    return list(session.execute(statement).scalars())

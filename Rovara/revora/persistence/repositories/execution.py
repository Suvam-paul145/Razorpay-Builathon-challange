"""Execution intent reservation and the reconciliation claim.

:meth:`ExecutionIntentRepository.reserve` is the exactly-once gate. It inserts the
intent *before* the provider is called, with ``ON CONFLICT DO NOTHING`` on
``(merchant_id, idempotency_key)``. A returned id means this caller owns the attempt
and may make the call. ``None`` means an intent for that key already exists, and the
correct response is to reconcile it — never to call the provider again.

That ordering is deliberately pessimistic. A crash between the insert and the call
burns an attempt that never reached the provider. The alternative — call first,
record after — risks a crash loop that issues provider calls while consuming zero
attempts, which can exceed the cap and message a customer twice. Given the choice,
the design under-attempts.

:meth:`ExecutionIntentRepository.claim_unresolved` uses ``FOR UPDATE SKIP LOCKED``
so two reconciliation sweeps running at once take different intents instead of
waiting on each other and then both resolving the same one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import IntentState
from revora.persistence.models import ExecutionIntent, PaymentStateRead, RecoveryOutcome
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.session import for_update_skip_locked

__all__ = [
    "ExecutionIntentRepository",
    "PaymentStateReadRepository",
    "RecoveryOutcomeRepository",
]

_UNRESOLVED: tuple[str, ...] = (IntentState.ATTEMPTED.value, IntentState.UNCERTAIN.value)


class ExecutionIntentRepository(MerchantScopedRepository[ExecutionIntent]):
    """The exactly-once record's repository."""

    model = ExecutionIntent

    def reserve(
        self,
        merchant_id: uuid.UUID,
        *,
        idempotency_key: str,
        values: dict[str, object],
    ) -> uuid.UUID | None:
        """Claim the right to make one external call, or discover it is already claimed.

        Returns the new intent's id, or ``None`` when an intent for this
        idempotency key already exists. ``None`` is a normal outcome on a retry.
        """
        statement = (
            insert(ExecutionIntent)
            .values(merchant_id=merchant_id, idempotency_key=idempotency_key, **values)
            .on_conflict_do_nothing(
                index_elements=[ExecutionIntent.merchant_id, ExecutionIntent.idempotency_key]
            )
            .returning(ExecutionIntent.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get_by_idempotency_key(
        self, merchant_id: uuid.UUID, idempotency_key: str
    ) -> ExecutionIntent | None:
        """The intent for a key, within one merchant."""
        statement = self.scoped(merchant_id).where(
            ExecutionIntent.idempotency_key == idempotency_key
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[ExecutionIntent]:
        """Every attempt on a case, in attempt order."""
        statement = (
            self.scoped(merchant_id)
            .where(ExecutionIntent.case_id == case_id)
            .order_by(ExecutionIntent.attempt_ordinal)
        )
        return list(self.session.execute(statement).scalars())

    def claim_unresolved(
        self,
        merchant_id: uuid.UUID,
        *,
        started_before: datetime,
        limit: int,
    ) -> Sequence[ExecutionIntent]:
        """Claim unresolved intents for reconciliation, skipping locked rows.

        Served by ``ix_execution_intent_unresolved``, the partial index that exists
        for exactly this scan. Oldest first, because an intent that has been
        ``UNCERTAIN`` longest is the one blocking a case from progressing.
        """
        statement = for_update_skip_locked(
            select(ExecutionIntent)
            .where(
                ExecutionIntent.merchant_id == merchant_id,
                ExecutionIntent.state.in_(_UNRESOLVED),
                ExecutionIntent.attempt_started_at <= started_before,
            )
            .order_by(ExecutionIntent.attempt_started_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class PaymentStateReadRepository(MerchantScopedRepository[PaymentStateRead]):
    """Authoritative provider reads, kept as history."""

    model = PaymentStateRead

    def latest_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> PaymentStateRead | None:
        """The newest authoritative read for a case.

        Newest by ``read_at``, not by ``created_at``: a read can be persisted after a
        later one when reconciliation runs concurrently, and the question being asked
        is what the provider said most recently.
        """
        statement = (
            self.scoped(merchant_id)
            .where(PaymentStateRead.case_id == case_id)
            .order_by(PaymentStateRead.read_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[PaymentStateRead]:
        """Every read for a case, oldest first, so a conflict can be reconstructed."""
        statement = (
            self.scoped(merchant_id)
            .where(PaymentStateRead.case_id == case_id)
            .order_by(PaymentStateRead.read_at)
        )
        return list(self.session.execute(statement).scalars())


class RecoveryOutcomeRepository(MerchantScopedRepository[RecoveryOutcome]):
    """The verified outcome, at most one per case."""

    model = RecoveryOutcome

    def for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> RecoveryOutcome | None:
        """The outcome for a case, if it has been verified."""
        statement = self.scoped(merchant_id).where(RecoveryOutcome.case_id == case_id)
        return self.session.execute(statement).scalar_one_or_none()

    def list_in_window(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[RecoveryOutcome]:
        """Outcomes recovered inside a time window, for cohort aggregation.

        The aggregate is computed by summing these integers in the caller rather than
        by a SQL ``SUM`` over a cast, so the reported total is exactly the sum of the
        rows it is built from — see ``domain.money.sum_exact``.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                RecoveryOutcome.recovery_timestamp >= start,
                RecoveryOutcome.recovery_timestamp < end,
            )
            .order_by(RecoveryOutcome.recovery_timestamp)
        )
        return list(self.session.execute(statement).scalars())

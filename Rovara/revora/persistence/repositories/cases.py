"""Recovery case and event reads, plus audit sequence allocation.

Two operations here are the load-bearing ones.

:meth:`WebhookEventRepository.insert_if_new` is the dedup point. It is a single
``INSERT ... ON CONFLICT DO NOTHING ... RETURNING id``, so the uniqueness check and
the insert are one atomic statement. Doing it as ``SELECT`` then ``INSERT`` would be
a race that two concurrent redeliveries of one event win together, and the visible
consequence of losing that race is a customer contacted twice.

:meth:`RecoveryCaseRepository.allocate_audit_seq` is the gap-free sequence. It is an
``UPDATE ... RETURNING`` on the case row, which the caller is already holding under
``FOR UPDATE`` for state reasons. Concurrent writers serialize on that row lock, and
a rolled-back transaction rolls back the allocation with it — which a Postgres
sequence would not do, and ``max(seq)+1`` would not survive.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import CaseState
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models import RecoveryCase, WebhookEvent
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.session import for_update

__all__ = ["RecoveryCaseRepository", "WebhookEventRepository"]

_TERMINAL_VALUES: tuple[str, ...] = tuple(sorted(state.value for state in TERMINAL_STATES))


class WebhookEventRepository(MerchantScopedRepository[WebhookEvent]):
    """Reads and the idempotent insert for inbound events."""

    model = WebhookEvent

    def insert_if_new(
        self,
        merchant_id: uuid.UUID,
        *,
        provider_event_id: str,
        values: dict[str, object],
    ) -> uuid.UUID | None:
        """Insert an event, returning its id, or ``None`` if it was a duplicate.

        ``None`` is not an error. It is the answer "we already have this one", and
        the caller's correct response is to write a ``DUPLICATE_EVENT_DISCARDED``
        audit record and answer the provider 200 — a duplicate delivery is the
        provider working as documented, not a fault.

        The conflict target is the ``(merchant_id, provider_event_id)`` unique
        constraint, which is the constraint the whole dedup guarantee rests on.
        """
        statement = (
            insert(WebhookEvent)
            .values(merchant_id=merchant_id, provider_event_id=provider_event_id, **values)
            .on_conflict_do_nothing(
                index_elements=[WebhookEvent.merchant_id, WebhookEvent.provider_event_id]
            )
            .returning(WebhookEvent.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get_by_provider_event_id(
        self, merchant_id: uuid.UUID, provider_event_id: str
    ) -> WebhookEvent | None:
        """The retained event for a provider event id, within one merchant."""
        statement = self.scoped(merchant_id).where(
            WebhookEvent.provider_event_id == provider_event_id
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_by_correlation_id(
        self, merchant_id: uuid.UUID, correlation_id: uuid.UUID
    ) -> Sequence[WebhookEvent]:
        """Every event sharing a correlation id, within one merchant."""
        statement = self.scoped(merchant_id).where(WebhookEvent.correlation_id == correlation_id)
        return list(self.session.execute(statement).scalars())


class RecoveryCaseRepository(MerchantScopedRepository[RecoveryCase]):
    """Case reads, the row lock, and audit sequence allocation."""

    model = RecoveryCase

    def open_case_for_payment(
        self, merchant_id: uuid.UUID, provider_payment_id: str
    ) -> RecoveryCase | None:
        """The one non-terminal case for a payment, if there is one.

        At most one can exist — the ``one_open_case_per_payment`` partial unique
        index makes a second one uncommittable — so this returns a single row rather
        than a list, and ``scalar_one_or_none`` is allowed to raise if that ever
        stops being true. A silent ``first()`` would hide a broken invariant.
        """
        statement = self._open(merchant_id).where(
            RecoveryCase.provider_payment_id == provider_payment_id
        )
        return self.session.execute(statement).scalar_one_or_none()

    def lock_for_update(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> RecoveryCase | None:
        """Read a case and hold its row for the rest of the transaction.

        Every state transition takes this lock, and audit sequence allocation relies
        on the caller already holding it.
        """
        statement = for_update(
            self.scoped(merchant_id).where(RecoveryCase.id == case_id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def allocate_audit_seq(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> int:
        """Increment and return this case's audit sequence number.

        Strictly increasing, gap-free, no duplicates, and rolled back with the
        transaction that allocated it. Must run in the same transaction as the audit
        insert it is for — allocating in one transaction and inserting in another
        reintroduces the gap this exists to prevent.
        """
        statement = (
            update(RecoveryCase)
            .where(RecoveryCase.merchant_id == merchant_id, RecoveryCase.id == case_id)
            .values(audit_seq=RecoveryCase.audit_seq + 1)
            .returning(RecoveryCase.audit_seq)
        )
        allocated = self.session.execute(statement).scalar_one_or_none()
        if allocated is None:
            raise LookupError(f"case {case_id} not found for merchant {merchant_id}")
        return int(allocated)

    def list_due_for_lifecycle(
        self,
        merchant_id: uuid.UUID,
        *,
        now: datetime,
        limit: int,
    ) -> Sequence[RecoveryCase]:
        """Non-terminal cases whose recovery window has closed.

        Served by ``ix_recovery_case_merchant_id_state_window_end_at``, which exists
        for this query. ``now`` is passed in rather than read here so a test can move
        the clock without the repository consulting a different one.
        """
        statement = (
            self._open(merchant_id)
            .where(RecoveryCase.window_end_at <= now)
            .order_by(RecoveryCase.window_end_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def list_by_state(
        self,
        merchant_id: uuid.UUID,
        state: CaseState,
        *,
        limit: int,
    ) -> Sequence[RecoveryCase]:
        """Cases in one state, newest first, within one merchant."""
        statement = (
            self.scoped(merchant_id)
            .where(RecoveryCase.state == state.value)
            .order_by(RecoveryCase.detected_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def list_by_customer_key(
        self, merchant_id: uuid.UUID, customer_key: str, *, limit: int
    ) -> Sequence[RecoveryCase]:
        """Every case for one customer, within one merchant.

        Used by the opt-out path: an opt-out recorded on one case has to be applied
        to every other case of the same customer, which is what makes consent a
        statement about a person rather than about a payment.
        """
        statement = (
            self.scoped(merchant_id)
            .where(RecoveryCase.customer_key == customer_key)
            .order_by(RecoveryCase.detected_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def _open(self, merchant_id: uuid.UUID) -> Select[tuple[RecoveryCase]]:
        """Scoped select restricted to non-terminal cases.

        The state list comes from ``domain.transitions.TERMINAL_STATES``, the same
        declaration the partial unique index is generated from, so the query and the
        index cannot disagree about what "open" means.
        """
        return select(RecoveryCase).where(
            RecoveryCase.merchant_id == merchant_id,
            RecoveryCase.state.notin_(_TERMINAL_VALUES),
        )

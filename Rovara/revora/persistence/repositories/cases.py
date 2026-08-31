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

from sqlalchemy import Select, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import CaseState
from revora.domain.payment_event import RECOVERY_SIGNAL_EVENTS, PaymentStatus
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models import DetectionVerdictRecord, RecoveryCase, WebhookEvent
from revora.persistence.models.cases import TERMINAL_STATE_SQL
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.session import for_update

__all__ = [
    "DetectionVerdictRepository",
    "RecoveryCaseRepository",
    "WebhookEventRepository",
]

_TERMINAL_VALUES: tuple[str, ...] = tuple(sorted(state.value for state in TERMINAL_STATES))

#: The predicate of the ``one_open_case_per_payment`` partial unique index, rendered
#: from the same declaration the index is generated from. ``ON CONFLICT`` against a
#: partial index must repeat its ``WHERE`` clause, and repeating it from a second
#: source is how the two drift.
_OPEN_CASE_INDEX_WHERE = text(f"state NOT IN ({TERMINAL_STATE_SQL})")
_CAPTURED_STATUS = PaymentStatus.CAPTURED.value
_RECOVERY_SIGNAL_VALUES: tuple[str, ...] = tuple(sorted(RECOVERY_SIGNAL_EVENTS))


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

    def newest_provider_created_at_for_payment(
        self,
        merchant_id: uuid.UUID,
        provider_payment_id: str,
        *,
        exclude_event_id: uuid.UUID | None = None,
    ) -> datetime | None:
        """The newest provider timestamp already seen for a payment, if any.

        The out-of-order guard (task 9.5) compares an arriving event against this. The
        current event is excluded so an event is never judged stale against itself.
        Matched on the canonical ``provider_payment_id`` because that identifier is
        stable across a payment's events, whereas ``provider_event_id`` is per
        delivery.
        """
        statement = self.scoped(merchant_id).where(
            WebhookEvent.canonical["provider_payment_id"].astext == provider_payment_id,
            WebhookEvent.provider_created_at.is_not(None),
        )
        if exclude_event_id is not None:
            statement = statement.where(WebhookEvent.id != exclude_event_id)
        statement = statement.order_by(WebhookEvent.provider_created_at.desc()).limit(1)
        row = self.session.execute(statement).scalars().first()
        return None if row is None else row.provider_created_at

    def has_capture_signal_for_payment(
        self, merchant_id: uuid.UUID, provider_payment_id: str
    ) -> bool:
        """Whether any persisted event for a payment signals it was captured.

        The detection rule "no verified captured state for the payment id" (R1) reads
        persisted rows rather than calling the provider. A prior ``payment.captured``,
        ``order.paid`` or ``payment_link.paid``, or any event whose canonical status
        is ``captured``, means a late ``payment.failed`` must not open a case for a
        payment that already succeeded. Matched on the canonical ``provider_payment_id``
        because that is the stable identifier across a payment's events.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                WebhookEvent.canonical["provider_payment_id"].astext == provider_payment_id,
                or_(
                    WebhookEvent.canonical["status"].astext == _CAPTURED_STATUS,
                    WebhookEvent.event_name.in_(_RECOVERY_SIGNAL_VALUES),
                ),
            )
            .limit(1)
        )
        return self.session.execute(statement).first() is not None

    def has_event_for_payment(
        self, merchant_id: uuid.UUID, provider_payment_id: str
    ) -> bool:
        """Whether any event at all is already persisted for a payment.

        The detection-gap backfill's pre-check. It cannot use the dedup index for this: the
        index keys on ``provider_event_id``, and a payment delivered by webhook carries the
        *provider's* event id while a backfill would mint a synthetic one — so the index sees
        two different keys for one payment and correctly inserts both. Matching on the
        canonical ``provider_payment_id`` instead is what makes backfilling an
        already-known payment a no-op.

        Matched on the canonical column rather than a dedicated one because that identifier
        is stable across every event a payment produces, whichever route each arrived by.
        """
        statement = (
            self.scoped(merchant_id)
            .where(WebhookEvent.canonical["provider_payment_id"].astext == provider_payment_id)
            .limit(1)
        )
        return self.session.execute(statement).first() is not None

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

    def insert_if_absent(
        self, merchant_id: uuid.UUID, *, values: dict[str, object]
    ) -> uuid.UUID | None:
        """Open a case, returning its id, or ``None`` if one is already open.

        A single ``INSERT ... ON CONFLICT DO NOTHING`` against the
        ``one_open_case_per_payment`` partial unique index. ``None`` is not an error:
        it means an open case already exists for this payment, and the caller's
        correct response is to attach the event to it and leave its ``payment_amount``
        and detection timestamp unchanged (R1.C10). Two concurrent detections of one
        failed payment reach this together and exactly one wins — the guarantee is the
        index, not a check-then-insert that would race.

        A *terminal* case on the same payment does not conflict: the partial index
        does not cover it, so a payment that failed again gets a genuinely new case.
        """
        statement = (
            insert(RecoveryCase)
            .values(merchant_id=merchant_id, **values)
            .on_conflict_do_nothing(
                index_elements=[RecoveryCase.merchant_id, RecoveryCase.provider_payment_id],
                index_where=_OPEN_CASE_INDEX_WHERE,
            )
            .returning(RecoveryCase.id)
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


class DetectionVerdictRepository(MerchantScopedRepository[DetectionVerdictRecord]):
    """The one-verdict-per-event record, and the idempotency guard around it."""

    model = DetectionVerdictRecord

    def exists_for_event(self, merchant_id: uuid.UUID, webhook_event_id: uuid.UUID) -> bool:
        """Whether this event already has a verdict.

        Detection is enqueued as a job and a job can be retried, so the service reads
        this before doing anything. The definitive guard is the
        ``uq_detection_verdict_webhook_event_id`` unique constraint — this only lets
        a retry return early cleanly instead of hitting it.
        """
        statement = self.scoped(merchant_id).where(
            DetectionVerdictRecord.webhook_event_id == webhook_event_id
        )
        return self.session.execute(statement).first() is not None

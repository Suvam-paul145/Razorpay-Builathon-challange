"""The authoritative read. A webhook is a claim; this is evidence.

Everything in this module exists to support one sentence: **no recovery is ever declared
from a webhook.** Delivery is verified at-least-once and out-of-order, so a
``payment.captured`` webhook establishes that something was reported at some point, not that
money is currently in the merchant's account. The difference is the whole reason
``recovery_outcome.verified_by_read_id`` is ``NOT NULL`` — a recovery row that no read backs
cannot be inserted.

Two decisions here that look like extra work and are not.

**Every read is persisted, including the ones that change nothing.** A recovery figure is
only defensible if the reads behind it are enumerable, and a read that happened without a
row is a number nobody can check afterwards. The full response is retained too, PII-free, so
a disagreement between two signals is reconstructed from what the provider actually said
rather than argued about from memory.

**``amount_refunded`` is captured on every read, before anything needs it.** MVP recovery
figures are labelled gross of refunds, which is an honest simplification only while the data
to restate them exists. Capturing it from the start means a later restatement is arithmetic;
capturing it when someone asks means the history is gone.

The recovery test itself is deliberately narrow and lives in :func:`is_recovered`.
``authorized`` is not recovery. That is the single most important line in the module: an
authorized payment is a hold on a customer's card, and reporting it as recovered revenue
would count money that may never settle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from revora.domain.payment_event import PaymentStatus
from revora.persistence.models import PaymentStateRead
from revora.persistence.repositories.execution import PaymentStateReadRepository
from revora.platform.clock import now
from revora.providers.classification import ProviderResult, Success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.providers.classification import PaymentEntity

__all__ = [
    "PARTIAL_STATUSES",
    "ReadRecord",
    "is_partial",
    "is_recovered",
    "payment_timestamp",
    "persist_read",
    "read_attempt_number",
]

_NEVER_PERSISTED: frozenset[str] = frozenset({"contact", "email"})
"""Fields of the payment entity that must never reach an unencrypted column.

The two PII fields. ``payment_state_read.raw`` is plain JSONB, and R17.C6 permits cleartext
contact in exactly one place — the encrypted raw event store. Enumerated as a constant so the
exclusion is greppable and testable rather than a keyword argument somebody removes while
tidying."""

PARTIAL_STATUSES: frozenset[str] = frozenset({"partially_paid"})
"""Statuses that mean *some* money arrived. Never recovery (R10.C11).

A frozenset rather than a literal comparison because the payment-link entity and the payment
entity both have a notion of partial payment and both must be excluded by the same rule. A
partial counted as full inflates every figure by the difference."""


def is_recovered(entity: PaymentEntity) -> bool:
    """Whether this read establishes that the money moved. The only recovery test.

    ``captured`` — the status, or the boolean with ``authorized`` — and nothing else.

    **``authorized`` alone is not recovery** (R10.C2). An authorization is a hold against a
    customer's card that may be voided, may expire, may fail to settle. Reporting it as
    recovered revenue would mean reporting money that never arrived, and the whole design
    exists so that the recovery number is one a merchant can take to their finance team.

    ``captured`` is also accepted alongside ``authorized`` because the provider's own
    documentation shows ``captured`` as a boolean on the entity independently of ``status``,
    and a payment whose status lags a true ``captured`` flag has still moved the money.
    """
    if entity.status == PaymentStatus.CAPTURED.value:
        return True
    return entity.status == PaymentStatus.AUTHORIZED.value and entity.captured


def is_partial(entity: PaymentEntity) -> bool:
    """Whether some but not all of the money arrived. Holds the case, never recovers it."""
    if entity.status in PARTIAL_STATUSES:
        return True
    # A capture for less than the amount at risk is partial in substance whatever it is
    # called. Checked explicitly because `accept_partial=False` only constrains the links
    # Revora creates — a customer can still part-pay through another channel.
    return bool(
        is_recovered(entity) and 0 < entity.amount_refunded < entity.amount
    )


def payment_timestamp(entity: PaymentEntity, *, fallback: datetime) -> tuple[datetime, str]:
    """The recovery timestamp, and which source it came from.

    R10.C3 asks for the provider-reported payment timestamp. The verified surface exposes
    ``created_at`` and no ``captured_at``, so the honest answer is a proxy plus a label
    saying so — returned as a pair rather than silently substituted, because a figure whose
    provenance is not recorded is a figure that will later be quoted with more confidence
    than it earned.

    Returns:
        ``(instant, source)`` where source is ``"provider_created_at"`` or ``"read_at"``.
    """
    if entity.created_at is not None and entity.created_at > 0:
        return datetime.fromtimestamp(entity.created_at, tz=UTC), "provider_created_at"
    return fallback, "read_at"


@dataclass(frozen=True, slots=True)
class ReadRecord:
    """A persisted authoritative read, with the verdicts derived from it."""

    row: PaymentStateRead
    entity: PaymentEntity
    attempt_no: int

    @property
    def recovered(self) -> bool:
        return is_recovered(self.entity)

    @property
    def partial(self) -> bool:
        return is_partial(self.entity)


def read_attempt_number(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> int:
    """Which attempt the next read will be, counting from one.

    Derived from the persisted reads rather than stored, so it cannot disagree with them.
    """
    return len(PaymentStateReadRepository(session).list_for_case(merchant_id, case_id)) + 1


def persist_read(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    result: ProviderResult[PaymentEntity],
    provider_payment_id: str,
    moment: datetime | None = None,
) -> ReadRecord | None:
    """Persist a successful authoritative read. ``None`` when the read did not succeed.

    ``None`` rather than a row with a sentinel status. ``payment_state_read.status`` holds the
    provider's own vocabulary deliberately — the column has no ``CHECK`` precisely so an
    unrecognised provider status is stored rather than refused — and writing our own token
    into it would make the table lie about where its contents came from. A failed read is
    recorded in the audit log instead, which is where "we tried and could not" belongs.

    The caller must hold the case row, since it is about to act on the result.
    """
    if not isinstance(result, Success):
        return None

    entity = result.entity
    when = moment or now()
    attempt_no = read_attempt_number(session, merchant_id, case_id)

    row = PaymentStateRead(
        case_id=case_id,
        provider_payment_id=provider_payment_id,
        status=entity.status,
        amount=int(entity.amount),
        amount_refunded=int(entity.amount_refunded),
        captured=bool(entity.captured),
        read_at=when,
        attempt_no=attempt_no,
        # The response is retained so a disagreement between two signals is reconstructed
        # from what the provider said. `contact` and `email` are excluded explicitly, not
        # incidentally: they are the only PII the entity can carry, this column is
        # unencrypted JSONB, and R17.C6 permits cleartext contact in exactly one place —
        # the encrypted raw event store, which is not this. Named rather than filtered by
        # a rule, so adding a PII field later is a visible decision here.
        raw=entity.model_dump(
            mode="json", exclude_none=True, exclude=set(_NEVER_PERSISTED)
        ),
    )
    PaymentStateReadRepository(session).add(merchant_id, row)
    session.flush()

    return ReadRecord(row=row, entity=entity, attempt_no=attempt_no)

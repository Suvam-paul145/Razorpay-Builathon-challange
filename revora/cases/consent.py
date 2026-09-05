"""Recording consent and opt-out. Keyed on the customer, authoritative from an instant.

An opt-out is a statement about a person, not about a payment. R17.C10 is explicit about what that
implies and it is stronger than it first sounds: an opt-out recorded now is authoritative for
**every policy evaluation beginning after its ``effective_at``**, across the cases that already
exist and the ones that do not yet, and it stays authoritative until an explicitly recorded
re-consent replaces it.

Three consequences shape this module.

**Rows are appended, never updated.** A re-consent is a new row with a later ``effective_at``, and
the reader takes the newest. Overwriting would destroy the answer to "when did they ask us to
stop?", which is the question a complaint actually turns on — and it is the one question a mutable
row cannot answer.

**Nothing is applied retroactively.** Cases whose policy evaluation already completed keep their
decisions; the opt-out governs evaluations that *begin* after the instant. That is not a
convenience, it is what makes a past decision explicable: a decision recorded as APPROVED under a
consent state that later changed must not read, months afterwards, as a decision made against an
opt-out that existed.

**The write is unattached to any case.** ``customer_key`` may match no case yet, one case, or
forty. Attaching the record to whichever case happened to be in the request would make the trail
imply the statement was about that payment.

The keyed hash is computed by the caller, not here. ``platform.crypto.customer_key`` needs a
resolved secret, and this module is called from both the API — where a contact arrives as text —
and from a future provider-side unsubscribe hook, where a ``customer_key`` arrives already derived.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from revora.audit.events import CONSENT_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.enums import CaseState
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models.tenancy import CustomerConsent
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.platform.config import Configuration

__all__ = ["ConsentRecord", "record_consent"]

_logger = get_logger(__name__)

_AFFECTED_CASE_LIMIT = 200
"""How many affected open cases to count for the audit record.

A count rather than a list, and a bounded one. The record's job is to say "this opted-out customer
has open cases", which is what makes the consequence of the write visible to whoever reads the
trail later. An unbounded scan on a write path is a query whose cost grows with a tenant's success,
and the exact figure past a couple of hundred changes nothing about the answer."""


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """The row that was appended, and how many open cases it takes effect on."""

    consent_id: uuid.UUID
    customer_key: str
    opted_out: bool
    effective_at: datetime
    affected_open_case_count: int
    supersedes: uuid.UUID | None


def record_consent(
    merchant_id: uuid.UUID,
    *,
    customer_key: str,
    opted_out: bool,
    source: str,
    consent_expires_at: datetime | None = None,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
    factory: sessionmaker[Session] | None = None,
) -> ConsentRecord:
    """Append one consent statement for a customer.

    Args:
        opted_out: ``True`` records an opt-out, ``False`` records consent. Both are appended as
            rows; there is no delete, so an opt-out is never removed, only superseded.
        source: where the statement came from — a dashboard action, a provider unsubscribe, a
            support ticket reference. Required, because a consent record whose provenance is
            unknown cannot be defended and R17.C10 asks for it by name.
        consent_expires_at: when consent lapses, if it does. Ignored in substance for an opt-out —
            an opt-out does not expire, and a caller passing an expiry on one is recorded as
            passing it so the oddity is visible rather than silently dropped.
    """
    when = moment or now()
    with tenant_transaction(merchant_id, factory) as session:
        repository = CustomerConsentRepository(session)
        previous = repository.for_customer(merchant_id, customer_key)

        row = CustomerConsent(
            customer_key=customer_key,
            opted_out=opted_out,
            source=source,
            effective_at=when,
            consent_expires_at=None if opted_out else consent_expires_at,
        )
        repository.add(merchant_id, row)
        session.flush()

        # Non-terminal cases only. A closed case's decisions are already made and this statement
        # does not revisit them, so counting it would overstate what the write changed.
        cases = RecoveryCaseRepository(session).list_by_customer_key(
            merchant_id, customer_key, limit=_AFFECTED_CASE_LIMIT
        )
        affected = sum(
            1 for case in cases if CaseState(str(case.state)) not in TERMINAL_STATES
        )

        AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=CONSENT_RECORDED,
                actor=source,
                decision={
                    "consent_id": str(row.id),
                    # The keyed hash, which is what this table is keyed on. Not the contact: the
                    # whole reason the column is a hash is that a table of real addresses is a
                    # liability, and an audit record holding the cleartext would reintroduce one.
                    "customer_key": customer_key,
                    "opted_out": opted_out,
                    "source": source,
                    "effective_at": when.isoformat(),
                    "supersedes_consent_id": None if previous is None else str(previous.id),
                    "affected_open_case_count": affected,
                    "note": (
                        "authoritative for every policy evaluation beginning after "
                        "effective_at, across existing and future cases of this customer; "
                        "decisions already recorded are not revisited"
                    ),
                },
            ),
            correlation_id=correlation_id,
        )

        _logger.info(
            "consent recorded",
            merchant_id=str(merchant_id),
            opted_out=opted_out,
            source=source,
            affected_open_case_count=affected,
        )
        return ConsentRecord(
            consent_id=row.id,
            customer_key=customer_key,
            opted_out=opted_out,
            effective_at=when,
            affected_open_case_count=affected,
            supersedes=None if previous is None else previous.id,
        )

"""Customer-data retention: delete the contact, keep the numbers.

R17.C11 requires contact data to be deleted or irreversibly masked within 24 hours of
``CUSTOMER_DATA_RETENTION`` elapsing, while retaining the non-identifying fields the metrics need,
and recording the retention configuration version that was applied.

The last clause is the one that is easy to skip and it is load-bearing: "we deleted it on time" is a
claim about the bound that was in force, and the bound is per-merchant configurable. Without the
version on the record, a sweep that ran under a 180-day bound is indistinguishable from one that ran
under a 30-day bound, and the compliance statement is unverifiable in either direction.

**What is destroyed and what survives, stated explicitly because the split is the whole design.**

Destroyed, irreversibly:

* ``recovery_case.customer_contact_masked`` — the partial contact kept for support. Already masked,
  and the disclosed tail is still customer data, so it goes.
* ``webhook_event.raw_payload_ciphertext`` and its ``raw_payload_nonce`` — the AES-GCM ciphertext
  of the raw body, which is the only place a full contact ever existed. Both are cleared, not just
  the ciphertext: a nonce left behind is not itself sensitive, but leaving it makes the row look
  decryptable and the next person to read the schema has to work out why it is not. The row itself
  survives, because its ``provider_event_id`` is what makes duplicate detection work and deleting
  it would let a redelivery from a year ago open a second case.

Survived, deliberately:

* ``recovery_case.customer_key`` — the keyed non-reversible hash. **This is a judgement call and it
  is worth arguing with.** Keeping it preserves the cross-case opt-out join, which is the mechanism
  that stops an opted-out customer being contacted again; destroying it would silently revoke every
  historical opt-out, which is a privacy regression dressed as a privacy measure. It is not
  reversible without the HMAC secret, and it identifies nobody on its own.
* every amount, timestamp, state, cause and outcome. A retention sweep that also destroyed these
  would make every historical figure irreproducible — a different failure from a privacy one, and
  just as real, because a merchant's finance team cannot reconcile a quarter that has been
  partially erased.

**Terminal cases only.** A live case may still need to send a payment link, which needs the
ciphertext. In practice the recovery window is 168 hours and the retention bound is 180 days, so a
non-terminal case older than the bound is a stuck case rather than an active one — but relying on
that would make a privacy sweep depend on a configuration coincidence. Filtering on terminal state
makes it depend on the state machine instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import select, update

from revora.audit.events import CUSTOMER_DATA_REDACTED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models import RecoveryCase, WebhookEvent
from revora.persistence.repositories.base import rows_affected
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.platform.config import Configuration

__all__ = ["RETENTION_BATCH_LIMIT", "RetentionReport", "sweep_customer_data_retention"]

_logger = get_logger(__name__)

RETENTION_BATCH_LIMIT: Final[int] = 500
"""Cases redacted per sweep.

Bounded so one sweep cannot hold a long transaction over a table the pipeline is writing to. The
sweep runs on an interval and picks up where it left off — the ``WHERE`` clause is self-limiting
because a redacted case no longer matches it — so a backlog drains over several runs rather than in
one lock."""

_REDACTED_CIPHERTEXT: Final[bytes] = b""
"""What a redacted payload becomes. Empty, not a placeholder string.

An empty ``BYTEA`` cannot be decrypted into anything, which is what "irreversibly" requires. A
placeholder like ``b'REDACTED'`` would decrypt-fail with an authentication error indistinguishable
from a corrupted row or a rotated key, and the operator investigating would have no way to tell a
compliance action from a data-loss incident."""

_TERMINAL_VALUES: Final[tuple[str, ...]] = tuple(
    sorted(state.value for state in TERMINAL_STATES)
)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What one sweep did. Every figure is a count, and the version is what was applied."""

    cases_redacted: int
    payloads_redacted: int
    retention_seconds: int
    config_version: str
    cutoff: datetime
    more_remaining: bool

    def as_document(self) -> dict[str, object]:
        return {
            "cases_redacted": self.cases_redacted,
            "payloads_redacted": self.payloads_redacted,
            "retention_seconds": self.retention_seconds,
            "retention_config_version": self.config_version,
            "cutoff": self.cutoff.isoformat(),
            "more_remaining": self.more_remaining,
        }


def sweep_customer_data_retention(
    merchant_id: uuid.UUID,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
    limit: int = RETENTION_BATCH_LIMIT,
    factory: sessionmaker[Session] | None = None,
) -> RetentionReport:
    """Redact contact data on terminal cases older than ``CUSTOMER_DATA_RETENTION``.

    Returns a report whose ``more_remaining`` tells the caller a further sweep is due immediately
    rather than at the next interval — which is what keeps the 24-hour bound in R17.C11 satisfiable
    on a merchant with a large backlog, where waiting for the next tick would miss it.

    Must be called outside a transaction; opens its own.
    """
    when = moment or now()
    cutoff = when - config.CUSTOMER_DATA_RETENTION

    with tenant_transaction(merchant_id, factory) as session:
        due = list(
            session.execute(
                select(RecoveryCase.id, RecoveryCase.source_event_id)
                .where(
                    RecoveryCase.merchant_id == merchant_id,
                    RecoveryCase.state.in_(_TERMINAL_VALUES),
                    RecoveryCase.detected_at < cutoff,
                    # The self-limiting condition. A case whose contact is already gone does not
                    # match, so a re-run is cheap and idempotent rather than rewriting the same
                    # rows and writing a second audit record claiming a second redaction.
                    RecoveryCase.customer_contact_masked.is_not(None),
                )
                .order_by(RecoveryCase.detected_at)
                .limit(limit + 1)
            ).all()
        )
        more_remaining = len(due) > limit
        batch = due[:limit]

        if not batch:
            return RetentionReport(
                cases_redacted=0,
                payloads_redacted=0,
                retention_seconds=int(config.CUSTOMER_DATA_RETENTION.total_seconds()),
                config_version=config.version,
                cutoff=cutoff,
                more_remaining=False,
            )

        case_ids = [row.id for row in batch]
        event_ids = [row.source_event_id for row in batch if row.source_event_id is not None]

        session.execute(
            update(RecoveryCase)
            .where(RecoveryCase.merchant_id == merchant_id, RecoveryCase.id.in_(case_ids))
            .values(customer_contact_masked=None)
        )

        payloads = 0
        if event_ids:
            payloads = rows_affected(
                session.execute(
                    update(WebhookEvent)
                    .where(
                        WebhookEvent.merchant_id == merchant_id,
                        WebhookEvent.id.in_(event_ids),
                        WebhookEvent.raw_payload_ciphertext != _REDACTED_CIPHERTEXT,
                    )
                    .values(
                        raw_payload_ciphertext=_REDACTED_CIPHERTEXT,
                        raw_payload_nonce=_REDACTED_CIPHERTEXT,
                    )
                )
            )

        report = RetentionReport(
            cases_redacted=len(case_ids),
            payloads_redacted=payloads,
            retention_seconds=int(config.CUSTOMER_DATA_RETENTION.total_seconds()),
            config_version=config.version,
            cutoff=cutoff,
            more_remaining=more_remaining,
        )

        AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=CUSTOMER_DATA_REDACTED,
                actor="retention_sweep",
                decision={
                    **report.as_document(),
                    "retained_fields": [
                        "customer_key",
                        "payment_amount",
                        "currency",
                        "detected_at",
                        "state",
                        "terminal_reason",
                        "risk_cause",
                        "recovered_amount",
                    ],
                    "note": (
                        "customer_key is retained deliberately: it is a keyed non-reversible "
                        "hash and it is what makes a historical opt-out apply to a future case. "
                        "Destroying it would revoke every recorded opt-out."
                    ),
                },
            ),
            correlation_id=correlation_id,
        )

    _logger.info(
        "customer data retention sweep completed",
        merchant_id=str(merchant_id),
        **report.as_document(),
    )
    return report

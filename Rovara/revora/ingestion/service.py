"""Ingestion orchestration: verify, canonicalize, deduplicate, enqueue — atomically.

This is the request-path work behind ``POST /webhooks/razorpay/{slug}``, kept out of
the FastAPI route so it is testable without an HTTP client. It does no detection work
on the path (R1.C13) — the most it does is one insert and one enqueue in a single
transaction, then it answers.

The order is fixed and each step has a reason:

1. **Rate limit** per source, so a flood cannot exhaust the process before it can
   reject (R17.C12). Rejected without discarding anything already persisted.
2. **Size cap before hashing**, so an oversized body cannot burn CPU on an HMAC it
   was never going to pass.
3. **Signature verify** against the exact bytes, against every active secret. A
   missing secret is a 503 (configuration failure, redeliver) not a 401 (forgery) —
   the two are different and answering 401 would tell a real retry it was forged.
4. **Event-id required**, because it is the dedup key.
5. **Canonicalize** with the round-trip self-check; a failure quarantines and answers
   202 so an unparseable payload is not invited back.
6. **Insert-or-dedup plus enqueue, in one transaction** (R1.C4-C5). The insert is
   ``ON CONFLICT DO NOTHING``; zero rows means duplicate, answered 200 with a
   ``DUPLICATE_EVENT_DISCARDED`` record. A new row enqueues exactly one detection job
   in the same transaction, so the event and the work to process it commit together.

The enqueue goes through the persistence repository rather than the job module,
because ingestion sits below ``jobs`` in the dependency order and a job row is,
mechanically, a persisted row.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique

from revora.audit.events import (
    CREDENTIAL_UNAVAILABLE,
    DUPLICATE_EVENT_DISCARDED,
    EVENT_INGESTED,
    MALFORMED_EVENT,
    OUT_OF_ORDER_EVENT,
    RATE_LIMIT_APPLIED,
    SIGNATURE_REJECTED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.ingestion.canonical import CanonicalizationError, canonicalize
from revora.ingestion.ordering import assess_ordering
from revora.ingestion.quarantine import quarantine_payload
from revora.ingestion.signature import verify_for_merchant
from revora.persistence.repositories.cases import WebhookEventRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.crypto import payload_cipher
from revora.platform.logging import get_logger
from revora.platform.secrets import CredentialUnavailableError

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import Session, sessionmaker
except ImportError:  # pragma: no cover
    Session = object  # type: ignore[assignment,misc]
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = ["DETECTION_JOB_KIND", "IngestionOutcome", "IngestionResult", "ingest_webhook"]

_logger = get_logger(__name__)

DETECTION_JOB_KIND = "detection"
"""The job kind the worker dispatches to ``detection.run_detection``."""

_INGESTION_ACTOR = "event_ingestion"


@unique
class IngestionOutcome(StrEnum):
    """What ingestion decided, mapped to an HTTP status by the route."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    QUARANTINED = "QUARANTINED"
    SIGNATURE_REJECTED = "SIGNATURE_REJECTED"
    MISSING_EVENT_ID = "MISSING_EVENT_ID"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"


_HTTP_STATUS: dict[IngestionOutcome, int] = {
    IngestionOutcome.ACCEPTED: 200,
    IngestionOutcome.DUPLICATE: 200,
    IngestionOutcome.QUARANTINED: 202,
    IngestionOutcome.SIGNATURE_REJECTED: 401,
    IngestionOutcome.MISSING_EVENT_ID: 400,
    IngestionOutcome.PAYLOAD_TOO_LARGE: 413,
    IngestionOutcome.RATE_LIMITED: 429,
    IngestionOutcome.CREDENTIAL_UNAVAILABLE: 503,
}


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The outcome and the HTTP status the route should return.

    ``webhook_event_id`` is set only on ``ACCEPTED``. No field carries any detail a
    failing response should not disclose — the route returns a bare status.
    """

    outcome: IngestionOutcome
    webhook_event_id: uuid.UUID | None = None

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.outcome]


class _RateLimiter:
    """A process-local per-source fixed-window counter.

    Approximate on purpose: a per-process counter over a one-minute window is enough
    to shed a flood without a shared store, and a shared limiter is a scaling problem
    the MVP does not have. The window resets rather than sliding, which can admit up
    to twice the limit across a boundary — acceptable for a coarse abuse guard, and
    documented so it is not mistaken for a precise quota.
    """

    __slots__ = ("_counts", "_lock", "_window")

    def __init__(self) -> None:
        self._counts: dict[str, tuple[datetime, int]] = {}
        self._window = timedelta(minutes=1)
        self._lock = threading.Lock()

    def allow(self, key: str, limit_per_minute: int, moment: datetime) -> bool:
        with self._lock:
            start, count = self._counts.get(key, (moment, 0))
            if moment - start >= self._window:
                start, count = moment, 0
            if count >= limit_per_minute:
                self._counts[key] = (start, count)
                return False
            self._counts[key] = (start, count + 1)
            return True


_rate_limiter = _RateLimiter()


def ingest_webhook(
    merchant_id: uuid.UUID,
    merchant_slug: str,
    *,
    body: bytes,
    provided_signature: str | None,
    provider_event_id: str | None,
    config: Configuration,
    correlation_id: uuid.UUID,
    factory: sessionmaker[Session] | None = None,
) -> IngestionResult:
    """Process one inbound webhook for a resolved merchant.

    ``merchant_id`` and ``merchant_slug`` are passed as plain values rather than an
    ORM row: the route resolves the merchant in its own transaction, which expires
    the row on commit, so nothing here touches a detached instance. Every write is
    scoped to ``merchant_id`` through a merchant-bound transaction.
    """
    slug = merchant_slug
    moment = now()

    if not _rate_limiter.allow(slug, config.INGEST_RATE_LIMIT, moment):
        _audit_unattached(
            merchant_id,
            AuditEntry(event_type=RATE_LIMIT_APPLIED, actor=_INGESTION_ACTOR),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.RATE_LIMITED)

    if len(body) > config.MAX_INBOUND_PAYLOAD_SIZE:
        _audit_unattached(
            merchant_id,
            AuditEntry(
                event_type=MALFORMED_EVENT,
                actor=_INGESTION_ACTOR,
                evidence={"rule": "payload_too_large", "bytes": len(body)},
            ),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.PAYLOAD_TOO_LARGE)

    try:
        verified = verify_for_merchant(body, provided_signature or "", slug)
    except CredentialUnavailableError:
        _audit_unattached(
            merchant_id,
            AuditEntry(
                event_type=CREDENTIAL_UNAVAILABLE,
                actor=_INGESTION_ACTOR,
                evidence={"credential": "webhook_signing_secret"},
            ),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.CREDENTIAL_UNAVAILABLE)
    if not verified:
        _audit_unattached(
            merchant_id,
            AuditEntry(event_type=SIGNATURE_REJECTED, actor=_INGESTION_ACTOR),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.SIGNATURE_REJECTED)

    if not provider_event_id:
        _audit_unattached(
            merchant_id,
            AuditEntry(
                event_type=SIGNATURE_REJECTED,
                actor=_INGESTION_ACTOR,
                evidence={"rule": "missing_event_id"},
            ),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.MISSING_EVENT_ID)

    try:
        canonical_result = canonicalize(body, disclosure_length=config.MASK_DISCLOSURE_LENGTH)
    except CanonicalizationError as exc:
        with tenant_transaction(merchant_id, factory) as session:
            quarantine_payload(
                session,
                merchant_id,
                body=body,
                validation_rule=exc.rule,
                correlation_id=correlation_id,
                received_at=moment,
                retention=config.QUARANTINE_RETENTION,
                provider_event_id=provider_event_id,
            )
            _writer(session, config).write_unattached(
                merchant_id,
                AuditEntry(
                    event_type=MALFORMED_EVENT,
                    actor=_INGESTION_ACTOR,
                    evidence={"rule": exc.rule},
                ),
                correlation_id=correlation_id,
                occurred_at=moment,
            )
        return IngestionResult(IngestionOutcome.QUARANTINED)

    return _persist_and_enqueue(
        merchant_id,
        provider_event_id=provider_event_id,
        body=body,
        canonical_result=canonical_result,
        config=config,
        correlation_id=correlation_id,
        moment=moment,
        factory=factory,
    )


def _persist_and_enqueue(
    merchant_id: uuid.UUID,
    *,
    provider_event_id: str,
    body: bytes,
    canonical_result,
    config: Configuration,
    correlation_id: uuid.UUID,
    moment: datetime,
    factory: sessionmaker[Session] | None,
) -> IngestionResult:
    canonical = canonical_result.canonical
    try:
        encrypted = payload_cipher().encrypt(body)
    except CredentialUnavailableError:
        _audit_unattached(
            merchant_id,
            AuditEntry(
                event_type=CREDENTIAL_UNAVAILABLE,
                actor=_INGESTION_ACTOR,
                evidence={"credential": "payload_encryption_keys"},
            ),
            config,
            correlation_id,
            factory,
        )
        return IngestionResult(IngestionOutcome.CREDENTIAL_UNAVAILABLE)

    with tenant_transaction(merchant_id, factory) as session:
        events = WebhookEventRepository(session)
        new_id = events.insert_if_new(
            merchant_id,
            provider_event_id=provider_event_id,
            values={
                "event_name": canonical.event_name,
                "raw_payload_ciphertext": encrypted.ciphertext,
                "raw_payload_nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "canonical": canonical.to_dict(),
                "provider_created_at": canonical_result.provider_created_at,
                "received_at": moment,
                "correlation_id": correlation_id,
                "signature_verified": True,
                "retain_until": moment + config.EVENT_DEDUP_RETENTION,
            },
        )
        writer = _writer(session, config)

        if new_id is None:
            writer.write_unattached(
                merchant_id,
                AuditEntry(
                    event_type=DUPLICATE_EVENT_DISCARDED,
                    actor=_INGESTION_ACTOR,
                    evidence={"provider_event_id": provider_event_id},
                ),
                correlation_id=correlation_id,
                occurred_at=moment,
            )
            return IngestionResult(IngestionOutcome.DUPLICATE)

        _record_ordering(events, writer, merchant_id, canonical, new_id, canonical_result,
                         correlation_id, moment)

        JobRepository(session).enqueue(
            merchant_id,
            kind=DETECTION_JOB_KIND,
            payload={
                "webhook_event_id": str(new_id),
                "correlation_id": str(correlation_id),
            },
            run_after=moment,
            dedupe_key=f"{DETECTION_JOB_KIND}:{new_id}",
            correlation_id=correlation_id,
        )
        writer.write_unattached(
            merchant_id,
            AuditEntry(
                event_type=EVENT_INGESTED,
                actor=_INGESTION_ACTOR,
                evidence={
                    "provider_event_id": provider_event_id,
                    "event_name": canonical.event_name,
                    "webhook_event_id": str(new_id),
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )
        return IngestionResult(IngestionOutcome.ACCEPTED, webhook_event_id=new_id)


def _record_ordering(
    events: WebhookEventRepository,
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    canonical,
    new_id: uuid.UUID,
    canonical_result,
    correlation_id: uuid.UUID,
    moment: datetime,
) -> None:
    """Audit an out-of-order arrival. Detection still runs; its rules decide the case.

    The event is retained and detection is enqueued regardless — the correctness of
    the capture-before-failure case is enforced by detection's "no verified capture"
    rule. This records the ordering fact with both timestamps (R1.C11) so a case that
    did not open for an out-of-order reason is explicable.
    """
    payment_id = canonical.provider_payment_id
    if payment_id is None:
        return
    newest_prior = events.newest_provider_created_at_for_payment(
        merchant_id, payment_id, exclude_event_id=new_id
    )
    decision = assess_ordering(canonical_result.provider_created_at, newest_prior)
    if decision.in_order:
        return
    writer.write_unattached(
        merchant_id,
        AuditEntry(
            event_type=OUT_OF_ORDER_EVENT,
            actor=_INGESTION_ACTOR,
            evidence={
                "provider_payment_id": payment_id,
                "event_at": decision.event_at.isoformat() if decision.event_at else None,
                "newest_prior_at": (
                    decision.newest_prior_at.isoformat() if decision.newest_prior_at else None
                ),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _writer(session: Session, config: Configuration) -> AuditWriter:
    return AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )


def _audit_unattached(
    merchant_id: uuid.UUID,
    entry: AuditEntry,
    config: Configuration,
    correlation_id: uuid.UUID,
    factory: sessionmaker[Session] | None,
) -> None:
    """Write one unattached audit record in its own transaction.

    Its own transaction because these are the rejection paths — there is no event row
    and no enqueue to be atomic with, and the record must persist regardless.
    """
    with tenant_transaction(merchant_id, factory) as session:
        _writer(session, config).write_unattached(
            merchant_id, entry, correlation_id=correlation_id
        )

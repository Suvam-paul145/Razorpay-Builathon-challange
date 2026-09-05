"""Inbound events: the dedup point, the quarantine, and the detection verdict.

``webhook_event`` is the only table in Revora that holds cleartext customer
contact data, and it holds it encrypted. That is the resolution of the conflict
the design flags as HIGH severity: R1.C3 wants the provider's raw payload
persisted verbatim, R17.C6 wants contact identifiers masked at write time, and the
raw ``payment.failed`` payload contains ``contact`` and ``email`` in clear. So the
raw bytes go into ``raw_payload_ciphertext`` under AES-256-GCM, and everything
downstream reads ``canonical``, which is PII-free by construction.

The ``UNIQUE (merchant_id, provider_event_id)`` constraint on this table is the
single mechanism that makes duplicate webhook delivery safe. The provider retries;
two retries can arrive concurrently; the insert is ``ON CONFLICT DO NOTHING`` in
the same transaction as the detection job row. Zero rows inserted means duplicate.
Without the constraint that check is a race, and the visible symptom is a customer
contacted twice for one failed payment.

Every persisted event gets exactly one ``detection_verdict`` row, including the
ones that are not at risk. Recording the negative is what makes a detection gap
detectable later — an event with no verdict row is a bug, and an event with a
``NOT_AT_RISK`` verdict is a decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.enums import DetectionVerdict
from revora.persistence.models.base import TIMESTAMPTZ, RowBase, enum_check

__all__ = ["DetectionVerdictRecord", "EventQuarantine", "WebhookEvent"]


class WebhookEvent(RowBase):
    """One signature-verified inbound provider event.

    The ciphertext columns are a single logical value split three ways because
    ``platform.crypto.EncryptedPayload`` is a triple: the ciphertext, the nonce
    needed to read it back, and the key version that wrote it. The nonce and the
    version are not secret, and storing the version per row is what lets a key be
    rotated without rewriting history.
    """

    __tablename__ = "webhook_event"

    provider_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The verified ``x-razorpay-event-id`` header. A header, not a payload field —
    the payload has no stable per-delivery identifier, so dedup has to key on
    this."""

    event_name: Mapped[str | None] = mapped_column(Text)

    raw_payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_payload_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int | None] = mapped_column(SmallInteger)

    canonical: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The PII-free normalized view every downstream component reads. If a field
    is needed downstream and is not here, the answer is to derive a masked form of
    it — not to decrypt the payload outside execution."""

    provider_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """The provider's own timestamp. Compared against the newest already-processed
    timestamp for the same payment to detect out-of-order delivery."""

    received_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """Generated at acceptance and carried through every job, audit record and log
    line that follows from this event. The thread that makes one inbound delivery
    traceable end to end."""

    signature_verified: Mapped[bool] = mapped_column(nullable=False)
    processing_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="RECEIVED")
    """Plain ``TEXT`` with no ``CHECK``: unlike the columns below it, this has no
    authoritative enum in ``revora.domain``, and inventing the permitted set here
    would put it somewhere the domain cannot see."""

    retain_until: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """When the dedup record may be dropped, from ``EVENT_DEDUP_RETENTION``.
    Stored rather than computed at read time so a change to the bound does not
    retroactively expire records that were relied on."""

    __table_args__ = (
        # THE dedup constraint. Concurrent deliveries of one provider_event_id
        # yield exactly one persisted row (R1.C4, P4). Correctness, not speed.
        UniqueConstraint(
            "merchant_id",
            "provider_event_id",
            name="uq_webhook_event_merchant_id_provider_event_id",
        ),
        # Reason: trace one inbound event end to end, and find the events a job
        # was scheduled from.
        Index("ix_webhook_event_correlation_id", "correlation_id"),
        # Reason: the detection-gap backfill sweep scans recent events per
        # merchant looking for ones with no verdict row.
        Index("ix_webhook_event_merchant_id_received_at", "merchant_id", "received_at"),
    )


class EventQuarantine(RowBase):
    """A payload that failed schema validation or exceeded the size cap.

    Retained rather than discarded: a malformed payload is either our schema being
    wrong or the provider changing theirs, and both are diagnosed from the bytes.
    Held encrypted for the same reason as ``webhook_event`` — a malformed payload
    is still a payload with a phone number in it.
    """

    __tablename__ = "event_quarantine"

    provider_event_id: Mapped[str | None] = mapped_column(Text)
    """Nullable, because a payload can be malformed in exactly the way that means
    this could not be read."""

    validation_rule: Mapped[str] = mapped_column(Text, nullable=False)
    """Which rule rejected it. Named in the ``MALFORMED_EVENT`` audit record too,
    so the two can be reconciled."""

    raw_payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_payload_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int | None] = mapped_column(SmallInteger)
    payload_bytes: Mapped[int | None] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    """From ``QUARANTINE_RETENTION``. A quarantine with no expiry becomes an
    unbounded store of unparsed customer data."""

    __table_args__ = (
        # Reason: the retention sweep deletes by expiry, per merchant.
        Index("ix_event_quarantine_merchant_id_retain_until", "merchant_id", "retain_until"),
    )


class DetectionVerdictRecord(RowBase):
    """Exactly one verdict per persisted event, negatives included.

    Named ``...Record`` because ``DetectionVerdict`` is the enum in
    ``revora.domain.enums`` and the two must not be confusable at a call site.
    """

    __tablename__ = "detection_verdict"

    webhook_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_event.id", ondelete="RESTRICT"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT")
    )
    """Set only for ``AT_RISK``. A verdict that opened no case still has a row."""

    decided_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    """Persistence to verdict, against ``DETECTION_LATENCY_BOUND``. Stored so the
    bound can be reported on rather than merely alarmed on."""

    __table_args__ = (
        # One verdict per event. A second verdict row would mean the detection
        # engine ran twice and we could not say which answer was used.
        UniqueConstraint("webhook_event_id", name="uq_detection_verdict_webhook_event_id"),
        enum_check("detection_verdict", "verdict", DetectionVerdict),
        # Reason: the detection-gap sweep joins events to verdicts by event id;
        # this covers the case-side lookup instead.
        Index("ix_detection_verdict_merchant_id_decided_at", "merchant_id", "decided_at"),
    )

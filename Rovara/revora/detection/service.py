"""Detection: one verdict per event, at most one open case per payment.

Runs inside the transaction the worker opened for the detection job, so the verdict
row, the case (if any) and the audit record commit together or not at all — a
partially-detected event is not a state this can be left in.

The case-creation guarantee is the database's, not this code's. On an ``AT_RISK``
verdict the service issues ``INSERT ... ON CONFLICT DO NOTHING`` against the
``one_open_case_per_payment`` partial unique index; two concurrent detections of one
failed payment reach that insert together and exactly one wins. The loser gets
``None`` back, finds the open case, and attaches its event to it — leaving the
existing ``payment_amount`` and detection timestamp untouched (R1.C10), because the
first detection's figures are the case's figures.

Detection is idempotent under job retry. Every persisted event carries exactly one
verdict (R1.C14, enforced by ``uq_detection_verdict_webhook_event_id``), so the
service checks for an existing verdict first and returns early rather than writing a
second one.

No provider call and no AI (R1.C12). "No verified captured state for the payment id"
is answered from persisted rows — a prior capture signal or a case already read as
captured — so a late ``payment.failed`` opens no case for a payment that was paid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from revora.audit.events import (
    CASE_DETECTED,
    DETECTION_VERDICT_RECORDED,
    EVENT_ATTACHED_TO_CASE,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.detection.rules import DetectionResult, classify
from revora.domain.enums import CaseState, DetectionVerdict, Provenance
from revora.domain.payment_event import (
    SUPPORTED_CURRENCIES,
    CanonicalPaymentEvent,
    PaymentStatus,
)
from revora.persistence.models import DetectionVerdictRecord
from revora.persistence.repositories.cases import (
    DetectionVerdictRepository,
    RecoveryCaseRepository,
    WebhookEventRepository,
)
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = ["DetectionServiceResult", "run_detection"]

_logger = get_logger(__name__)

_DETECTION_ACTOR = "detection_engine"
_UNKNOWN_CUSTOMER_PREFIX = "unknown:"


@dataclass(frozen=True, slots=True)
class DetectionServiceResult:
    """What one detection run did."""

    verdict: DetectionVerdict
    case_id: uuid.UUID | None
    case_created: bool
    already_processed: bool = False


def run_detection(
    session: Session,
    merchant_id: uuid.UUID,
    webhook_event_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
) -> DetectionServiceResult:
    """Classify one persisted event and, if at risk, open or attach a case.

    Must be called inside a transaction; it commits nothing itself. The worker's job
    handler owns the transaction so the verdict, the case and the audit are atomic.
    """
    verdicts = DetectionVerdictRepository(session)
    if verdicts.exists_for_event(merchant_id, webhook_event_id):
        return DetectionServiceResult(
            DetectionVerdict.NOT_AT_RISK, None, case_created=False, already_processed=True
        )

    events = WebhookEventRepository(session)
    cases = RecoveryCaseRepository(session)
    event = events.get(merchant_id, webhook_event_id)
    if event is None:
        _logger.warning("detection for missing event", webhook_event_id=str(webhook_event_id))
        return DetectionServiceResult(
            DetectionVerdict.NOT_AT_RISK, None, case_created=False, already_processed=True
        )

    canonical = CanonicalPaymentEvent.from_dict(event.canonical)
    already_captured = _already_captured(cases, events, merchant_id, canonical)

    result = classify(
        canonical,
        min_detection_amount=config.MIN_DETECTION_AMOUNT,
        supported_currencies=SUPPORTED_CURRENCIES,
        already_captured=already_captured,
    )

    moment = now()
    latency_ms = max(0, int((moment - event.received_at).total_seconds() * 1000))
    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    case_id: uuid.UUID | None = None
    case_created = False
    if result.verdict is DetectionVerdict.AT_RISK:
        case_id, case_created = _open_or_attach(
            cases, writer, merchant_id, canonical, event, config, result, correlation_id, moment
        )
    else:
        writer.write_unattached(
            merchant_id,
            AuditEntry(
                event_type=DETECTION_VERDICT_RECORDED,
                actor=_DETECTION_ACTOR,
                evidence={
                    "verdict": result.verdict.value,
                    "reason": result.reason,
                    "applied_rules": list(result.applied_rules),
                    "event_name": canonical.event_name,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    verdicts.add(
        merchant_id,
        DetectionVerdictRecord(
            webhook_event_id=webhook_event_id,
            verdict=result.verdict.value,
            reason=result.reason,
            case_id=case_id,
            decided_at=moment,
            latency_ms=latency_ms,
        ),
    )
    return DetectionServiceResult(result.verdict, case_id, case_created=case_created)


def _already_captured(
    cases: RecoveryCaseRepository,
    events: WebhookEventRepository,
    merchant_id: uuid.UUID,
    canonical: CanonicalPaymentEvent,
) -> bool:
    """Whether the payment already has a verified captured state, from persisted rows."""
    payment_id = canonical.provider_payment_id
    if payment_id is None:
        return False
    open_case = cases.open_case_for_payment(merchant_id, payment_id)
    if open_case is not None and open_case.verified_payment_status == PaymentStatus.CAPTURED.value:
        return True
    return events.has_capture_signal_for_payment(merchant_id, payment_id)


def _open_or_attach(
    cases: RecoveryCaseRepository,
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    canonical: CanonicalPaymentEvent,
    event: object,
    config: Configuration,
    result: DetectionResult,
    correlation_id: uuid.UUID | None,
    moment,
) -> tuple[uuid.UUID, bool]:
    """Open a new case, or attach to the one already open. Returns ``(case_id, created)``."""
    payment_id = canonical.provider_payment_id
    assert payment_id is not None  # AT_RISK implies a failed payment, which has an id
    webhook_event_id = event.id  # type: ignore[attr-defined]
    window_end = moment + config.RECOVERY_WINDOW_DURATION
    customer_key = canonical.customer_key or f"{_UNKNOWN_CUSTOMER_PREFIX}{payment_id}"

    new_id = cases.insert_if_absent(
        merchant_id,
        values={
            "state": CaseState.DETECTED.value,
            "provider_payment_id": payment_id,
            "provider_order_id": canonical.provider_order_id,
            "payment_amount": canonical.amount,
            "currency": canonical.currency,
            "customer_key": customer_key,
            "customer_contact_masked": canonical.customer_contact_masked,
            "source_event_id": webhook_event_id,
            "detected_at": moment,
            "window_end_at": window_end,
            "provenance": Provenance.REAL.value,
        },
    )

    if new_id is not None:
        writer.write_for_case(
            merchant_id,
            new_id,
            AuditEntry(
                event_type=CASE_DETECTED,
                actor=_DETECTION_ACTOR,
                new_state=CaseState.DETECTED.value,
                evidence={
                    "applied_rules": list(result.applied_rules),
                    "provider_payment_id": payment_id,
                    "source_event_id": str(webhook_event_id),
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )
        return new_id, True

    # An open case already exists: attach this event to it, changing nothing on it.
    open_case = cases.open_case_for_payment(merchant_id, payment_id)
    if open_case is None:  # pragma: no cover - only under a concurrent terminal race
        _logger.warning("at-risk event with no insertable and no open case",
                        provider_payment_id=payment_id)
        raise RuntimeError("at-risk detection could neither open nor find a case")
    writer.write_for_case(
        merchant_id,
        open_case.id,
        AuditEntry(
            event_type=EVENT_ATTACHED_TO_CASE,
            actor=_DETECTION_ACTOR,
            evidence={
                "applied_rules": list(result.applied_rules),
                "source_event_id": str(webhook_event_id),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )
    return open_case.id, False

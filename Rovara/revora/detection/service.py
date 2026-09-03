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
first detection's figures are the case's figures. An attach to a case resting at
``POLICY_CHECK`` also enqueues one decision cycle for it, in the same transaction (R30.C7):
a second failure on a payment Revora decided to wait on is evidence about that decision, and
before this the attach recorded the event and changed nothing.

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
from datetime import datetime

from sqlalchemy.orm import Session

from revora.audit.events import (
    CASE_DETECTED,
    DETECTION_VERDICT_RECORDED,
    EVENT_ATTACHED_TO_CASE,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.review import enqueue_case_review
from revora.detection.rules import DetectionResult, classify
from revora.domain.enums import CaseState, DetectionVerdict, Provenance, ReviewTrigger
from revora.domain.payment_event import (
    RECOVERY_SIGNAL_EVENTS,
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

    recovery_signal_case_id: uuid.UUID | None = None
    """The case a payment-success signal refers to, when there is one.

    A capture is ``NOT_AT_RISK`` — it is not a failure — so it opens no case and, before this
    field existed, the pipeline did nothing with it. The case still reached ``RECOVERED``, but only
    when the payment-state sweep next ran, up to ``PAYMENT_STATE_RECONCILIATION_INTERVAL`` later.
    R10.C1 wants an authoritative read within ``OUTCOME_READ_LATENCY_BOUND`` of the signal, and a
    fifteen-minute sweep cannot meet a sixty-second bound.

    So this names the case, and the worker enqueues an outcome observation for it. The sweep stays
    as the safety net — a case must never *depend* on a webhook arriving — and this is the fast
    path when one does.
    """

    signal_status: str | None = None
    """The payment status the signal claimed. Passed to the outcome monitor for conflict detection
    only; the recovery decision comes from the authoritative read alone."""


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
    signal_case_id: uuid.UUID | None = None
    if result.verdict is DetectionVerdict.AT_RISK:
        case_id, case_created = _open_or_attach(
            cases, writer, merchant_id, canonical, event, config, result, correlation_id, moment
        )
    else:
        signal_case_id = _recovery_signal_case(cases, merchant_id, canonical)
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
                    # Named on the record so a capture that routed to a case is visible in the
                    # trail. A recovery signal that matched nothing is the more interesting case —
                    # it means money arrived for a payment Revora never opened a case for.
                    "recovery_signal_case_id": (
                        None if signal_case_id is None else str(signal_case_id)
                    ),
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
    return DetectionServiceResult(
        result.verdict,
        case_id,
        case_created=case_created,
        recovery_signal_case_id=signal_case_id,
        signal_status=canonical.status if signal_case_id is not None else None,
    )


def _recovery_signal_case(
    cases: RecoveryCaseRepository,
    merchant_id: uuid.UUID,
    canonical: CanonicalPaymentEvent,
) -> uuid.UUID | None:
    """The case a payment-success signal refers to, or ``None``.

    Uses the *newest* case for the payment rather than the open one, deliberately. A capture that
    arrives after the recovery window closed belongs to a case that is already ``EXPIRED``, and
    R10.C14 requires that capture to reconcile it to ``RECOVERED`` — the open-case read cannot see
    a terminal case, so routing through it would drop the late success silently. That is the
    difference between recovering money and losing track of money the merchant already has.

    The monitor is what decides whether the case is eligible; this only decides which case the
    signal is about. Keeping those separate is why this returns an id and not a verdict.
    """
    if canonical.event_name not in RECOVERY_SIGNAL_EVENTS:
        return None
    payment_id = canonical.provider_payment_id
    if payment_id is None:
        return None
    case = cases.newest_case_for_payment(merchant_id, payment_id)
    return None if case is None else case.id


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
    moment: datetime,
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

    # R30.C7. A second failure on a payment Revora decided to wait on is new evidence about
    # the decision to wait, and until this enqueue existed the attach changed literally
    # nothing: the record was written, the case stayed where it was, and the customer failed
    # again while a case sat open doing nothing about it.
    #
    # In *this* transaction — the one carrying the attach and the audit record — because those
    # three facts have to reach disk together. A commit between them would leave either an
    # attached event with no cycle to consider it, or a queued cycle for an attach that rolled
    # back. The queue being a table is what makes the choice available.
    #
    # Only from ``POLICY_CHECK``. A case anywhere else is already mid-cycle and its own step
    # will see the attached event; the sweep and the two other triggers cover the rest. And the
    # enqueue is idempotent through ``case_review:{case_id}`` on the job table's partial unique
    # index, so a burst of retries on one payment produces one decision cycle (R30.C9).
    #
    # Nothing above touched ``payment_amount`` or ``detected_at``, and no second case was
    # created — the first detection's figures stay the case's figures (R1.C10), and this branch
    # is reached precisely because ``insert_if_absent`` refused to make another one.
    if CaseState(open_case.state) is CaseState.POLICY_CHECK:
        enqueue_case_review(
            cases.session,
            merchant_id,
            open_case.id,
            trigger=ReviewTrigger.EVENT_ATTACHED,
            correlation_id=correlation_id,
        )
    return open_case.id, False

"""The deterministic detection rule set. No AI is reachable from here (R1.C12).

Four predicates decide whether a persisted ``payment.failed`` event is revenue at
risk, evaluated in a fixed order, each one identified so the verdict record can name
exactly which rules were applied and which one, if any, declined the case:

1. ``status_is_failed`` — the payment status is ``failed``.
2. ``amount_meets_minimum`` — the amount is at least ``MIN_DETECTION_AMOUNT``.
3. ``currency_supported`` — the currency is one Revora can value.
4. ``no_verified_capture`` — there is no verified captured state for the payment id.

The order is not cosmetic. It is cheapest-and-most-decisive first, and the first
failing rule is the recorded reason, so a case declined for an unsupported currency
does not also claim to have been declined for its amount.

Everything else is not a detection trigger. A recovery signal (``payment.captured``
and friends) is ``NOT_AT_RISK`` here — a success opens no case, and the
Outcome_Monitor consumes those. A modelled-but-out-of-scope trigger (abandonment,
promise-to-pay, window expiry) is ``DEFERRED_TRIGGER``: retained and visible, but no
case (R1.C15).

This module is pure: it takes a canonical event, the two bounds, and one precomputed
fact — whether the payment already has a verified capture — and returns a verdict. It
reads no database and calls no provider, which is what lets the detection property
test (task 10.3) run in the microsecond tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from revora.domain.enums import DetectionVerdict
from revora.domain.money import Minor
from revora.domain.payment_event import (
    DEFERRED_TRIGGER_EVENTS,
    RECOVERY_SIGNAL_EVENTS,
    CanonicalPaymentEvent,
    EventName,
    PaymentStatus,
)

__all__ = [
    "RULE_AMOUNT_MEETS_MINIMUM",
    "RULE_CURRENCY_SUPPORTED",
    "RULE_NO_VERIFIED_CAPTURE",
    "RULE_STATUS_IS_FAILED",
    "DetectionResult",
    "classify",
]

RULE_STATUS_IS_FAILED = "status_is_failed"
RULE_AMOUNT_MEETS_MINIMUM = "amount_meets_minimum"
RULE_CURRENCY_SUPPORTED = "currency_supported"
RULE_NO_VERIFIED_CAPTURE = "no_verified_capture"


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """One event's verdict, the rules that were applied, and the deciding reason.

    ``applied_rules`` lists every rule evaluated in order, so the verdict record can
    prove the deterministic path ran and which predicates it checked. ``reason`` is
    the identifier of the rule that declined an otherwise-eligible failed payment, or
    a short token for a non-trigger event; ``None`` on ``AT_RISK``.
    """

    verdict: DetectionVerdict
    applied_rules: tuple[str, ...]
    reason: str | None = None


def classify(
    event: CanonicalPaymentEvent,
    *,
    min_detection_amount: Minor,
    supported_currencies: frozenset[str],
    already_captured: bool,
) -> DetectionResult:
    """Classify a persisted canonical event. Deterministic, no I/O.

    Args:
        event: the PII-free canonical event read from ``webhook_event.canonical``.
        min_detection_amount: ``MIN_DETECTION_AMOUNT`` for the merchant, minor units.
        supported_currencies: the currencies Revora can value.
        already_captured: whether the payment already has a verified captured state,
            computed by the service from persisted rows (there is no provider call
            at detection). A ``payment.captured`` that arrived first makes this true,
            so a late ``payment.failed`` opens no case for a payment that was paid.
    """
    name = event.event_name

    if name in DEFERRED_TRIGGER_EVENTS:
        return DetectionResult(DetectionVerdict.DEFERRED_TRIGGER, (), reason=name)

    if name != EventName.PAYMENT_FAILED.value:
        reason = "recovery_signal" if name in RECOVERY_SIGNAL_EVENTS else "not_a_trigger"
        return DetectionResult(DetectionVerdict.NOT_AT_RISK, (), reason=reason)

    applied: list[str] = []

    applied.append(RULE_STATUS_IS_FAILED)
    if event.status != PaymentStatus.FAILED.value:
        return DetectionResult(
            DetectionVerdict.NOT_AT_RISK, tuple(applied), reason=RULE_STATUS_IS_FAILED
        )

    applied.append(RULE_AMOUNT_MEETS_MINIMUM)
    if event.amount is None or event.amount < int(min_detection_amount):
        return DetectionResult(
            DetectionVerdict.NOT_AT_RISK, tuple(applied), reason=RULE_AMOUNT_MEETS_MINIMUM
        )

    applied.append(RULE_CURRENCY_SUPPORTED)
    if event.currency is None or event.currency not in supported_currencies:
        return DetectionResult(
            DetectionVerdict.NOT_AT_RISK, tuple(applied), reason=RULE_CURRENCY_SUPPORTED
        )

    applied.append(RULE_NO_VERIFIED_CAPTURE)
    if already_captured:
        return DetectionResult(
            DetectionVerdict.NOT_AT_RISK, tuple(applied), reason=RULE_NO_VERIFIED_CAPTURE
        )

    return DetectionResult(DetectionVerdict.AT_RISK, tuple(applied), reason=None)

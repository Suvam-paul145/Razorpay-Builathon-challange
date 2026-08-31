"""The canonical payment event: the PII-free vocabulary ingestion and detection share.

Event_Ingestion and Detection_Engine sit in the same architectural layer and may not
import each other, yet one produces exactly what the other consumes. The interface
between them is a persisted row — ``webhook_event.canonical`` — and this module is the
shape of that row's contents, expressed as a standard-library dataclass so it can
live in ``domain`` and be reachable from both sides without either importing the
other.

Two properties make this the right home:

**It is PII-free by construction.** The raw provider payload carries ``contact`` and
``email`` in clear; those never enter this structure. Ingestion derives a keyed,
non-reversible ``customer_key`` and a masked contact from them during
canonicalization and then discards the cleartext, which continues to exist only
inside the AES-GCM ciphertext of the raw payload. Every downstream component reads
this canonical view, so no downstream component can leak a contact it never received.

**It carries no float.** ``amount`` is an integer count of minor units, exactly as it
arrives from the provider ("currency subunits"), so the money discipline holds from
the first moment a value enters the system.

The field names are the verified Razorpay field names (the Payment Failure Taxonomy
and Payment Links findings), not invented ones. The Pydantic model that validates the
raw envelope and the round-trip self-check live in ``ingestion.canonical``; this is
only the normalized result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum, unique
from typing import Final

__all__ = [
    "DEFERRED_TRIGGER_EVENTS",
    "RECOVERY_SIGNAL_EVENTS",
    "SUBSCRIBED_EVENTS",
    "SUPPORTED_CURRENCIES",
    "CanonicalPaymentEvent",
    "EventName",
    "PaymentStatus",
]


@unique
class EventName(StrEnum):
    """The eight webhook events Revora subscribes to (verified provider names)."""

    PAYMENT_FAILED = "payment.failed"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_AUTHORIZED = "payment.authorized"
    ORDER_PAID = "order.paid"
    PAYMENT_LINK_PAID = "payment_link.paid"
    PAYMENT_LINK_PARTIALLY_PAID = "payment_link.partially_paid"
    PAYMENT_LINK_EXPIRED = "payment_link.expired"
    PAYMENT_LINK_CANCELLED = "payment_link.cancelled"


SUBSCRIBED_EVENTS: Final[frozenset[str]] = frozenset(member.value for member in EventName)
"""Every event Revora asks the provider to send. An event outside this set that
somehow arrives is recorded but classified ``NOT_AT_RISK`` — it is not a detection
trigger and Revora did not subscribe to it."""

RECOVERY_SIGNAL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        EventName.PAYMENT_CAPTURED.value,
        EventName.PAYMENT_AUTHORIZED.value,
        EventName.ORDER_PAID.value,
        EventName.PAYMENT_LINK_PAID.value,
    }
)
"""Events that signal a payment may have succeeded. The Outcome_Monitor (task 21)
consumes these to declare recovery from an authoritative read; the Detection_Engine
classifies them ``NOT_AT_RISK`` because a success signal opens no recovery case."""

DEFERRED_TRIGGER_EVENTS: Final[frozenset[str]] = frozenset()
"""Event types modelled but out of scope as detection triggers — checkout
abandonment, missed promise-to-pay, payment-window expiry (R1.C15). Empty in the MVP
because Revora subscribes to none of them; a subscribed deferred trigger is added
here and gets verdict ``DEFERRED_TRIGGER`` with no case, rather than being mistaken
for a failed payment. Kept as an explicit set so the classifier's branch is real
rather than dead."""

SUPPORTED_CURRENCIES: Final[frozenset[str]] = frozenset({"INR"})
"""Currencies Revora values in the MVP. **[ASSUMPTION]** — single-currency, matching
the ``customer_key`` normalization's ten-digit-subscriber assumption. A payment in an
unsupported currency is ``NOT_AT_RISK``: Revora will not act on revenue it cannot
value."""


@unique
class PaymentStatus(StrEnum):
    """The verified Razorpay payment status values Revora reasons about."""

    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CanonicalPaymentEvent:
    """The normalized, PII-free view of one inbound provider event.

    Every field is either a provider identifier, an integer amount in minor units, a
    verified status/error token, or a value already derived to a non-reversible or
    masked form. There is deliberately no ``contact`` and no ``email``.
    """

    event_name: str
    provider_payment_id: str | None = None
    provider_order_id: str | None = None
    payment_link_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    status: str | None = None
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_reason: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    customer_key: str | None = None
    customer_contact_masked: str | None = None
    provider_created_at: str | None = None
    """ISO 8601 UTC. A string rather than a ``datetime`` because this structure is
    serialized to ``JSONB``; the provider's Unix timestamp is converted to a UTC
    instant during canonicalization and rendered here."""

    def to_dict(self) -> dict[str, object]:
        """The ``JSONB`` form stored in ``webhook_event.canonical``.

        Null fields are dropped so the stored document carries only what the event
        actually contained — an absent ``error_reason`` reads as absent, not as a
        recorded ``null`` that a downstream reader might treat as a value.
        """
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CanonicalPaymentEvent:
        """Reconstruct from a persisted canonical document.

        Each field is pulled by name and type-checked rather than splatted in, so a
        value of the wrong shape (a string where an integer belongs, from a corrupt or
        hand-edited row) becomes ``None`` rather than a type error deep in a consumer.
        Unknown keys are ignored, so a document written by a newer build that added a
        field stays readable by this one.
        """

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        def integer(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return cls(
            event_name=text("event_name") or "",
            provider_payment_id=text("provider_payment_id"),
            provider_order_id=text("provider_order_id"),
            payment_link_id=text("payment_link_id"),
            amount=integer("amount"),
            currency=text("currency"),
            status=text("status"),
            method=text("method"),
            error_code=text("error_code"),
            error_description=text("error_description"),
            error_reason=text("error_reason"),
            error_source=text("error_source"),
            error_step=text("error_step"),
            customer_key=text("customer_key"),
            customer_contact_masked=text("customer_contact_masked"),
            provider_created_at=text("provider_created_at"),
        )

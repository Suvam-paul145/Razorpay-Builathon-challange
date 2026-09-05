"""Canonicalize a verified webhook body into the PII-free event downstream reads.

Two things happen here, in order, and the order is the point.

**The round-trip self-check (R16.C11).** The raw body is parsed into a typed model,
serialized back, and re-parsed; if the two parses are not equal the payload is
rejected as malformed rather than accepted. This catches the class of bug where our
interpretation of a provider field is not stable — a number that parsed as a string,
a field that coerced differently the second time — before that instability becomes a
persisted canonical row that lies about what arrived.

**The PII split.** The raw payment entity carries ``contact`` and ``email`` in clear.
Those are used here, once, to derive a keyed non-reversible ``customer_key`` (for the
cross-case opt-out join) and a masked contact (for display), and are then dropped.
The :class:`CanonicalPaymentEvent` this produces has neither field, so nothing
downstream can leak a contact it never received. The cleartext continues to exist
only inside the AES-GCM ciphertext of the raw body, which the ingestion service
encrypts and stores separately.

Field names are the verified Razorpay names. Unknown fields are ignored rather than
rejected, so a provider adding a field does not start quarantining live traffic — the
round-trip check still holds because both parses ignore the same unknowns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from revora.domain.enums import FieldKind
from revora.domain.payment_event import CanonicalPaymentEvent
from revora.platform.crypto import customer_key
from revora.platform.logging import get_logger
from revora.platform.masking import MASK_DISCLOSURE_LENGTH, mask_value
from revora.platform.secrets import CredentialUnavailableError

__all__ = ["CanonicalResult", "CanonicalizationError", "canonicalize"]

_logger = get_logger(__name__)


class CanonicalizationError(ValueError):
    """The payload could not be parsed, or did not survive the round-trip check.

    Carries the name of the rule that rejected it — ``invalid_json``,
    ``schema_invalid`` or ``round_trip_mismatch`` — which is recorded on the
    quarantine row and named in the ``MALFORMED_EVENT`` audit record, so the two can
    be reconciled without re-deriving why.
    """

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        super().__init__(f"{rule}: {detail}" if detail else rule)


class _Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    amount: int | None = None
    currency: str | None = None
    status: str | None = None
    order_id: str | None = None
    method: str | None = None
    contact: str | None = None
    email: str | None = None
    reference_id: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_reason: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    created_at: int | None = None


class _Wrapper(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity: _Entity | None = None


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment: _Wrapper | None = None
    order: _Wrapper | None = None
    payment_link: _Wrapper | None = None


class _Envelope(BaseModel):
    """The verified webhook envelope. ``extra='ignore'`` so a new provider field is
    tolerated rather than fatal, and the round-trip check still holds."""

    model_config = ConfigDict(extra="ignore")

    event: str
    created_at: int | None = None
    payload: _Payload = _Payload()


@dataclass(frozen=True, slots=True)
class CanonicalResult:
    """The canonical event plus the provider instant, for the service to persist."""

    canonical: CanonicalPaymentEvent
    provider_created_at: datetime | None


def canonicalize(
    body: bytes, *, disclosure_length: int = MASK_DISCLOSURE_LENGTH
) -> CanonicalResult:
    """Parse a verified body into a PII-free canonical event.

    Args:
        body: the exact verified request bytes. Signature verification has already
            happened against these bytes; this re-parses them.
        disclosure_length: how many trailing characters the masked contact may show.

    Raises:
        CanonicalizationError: on invalid JSON, a schema violation, or a failed
            round-trip. The caller quarantines and answers 202 — never opens a case
            and never issues an external request (R16.C13).
    """
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CanonicalizationError("invalid_json", str(exc)) from exc
    if not isinstance(raw, dict):
        raise CanonicalizationError("schema_invalid", "top-level payload is not an object")

    try:
        envelope = _Envelope.model_validate(raw)
    except ValidationError as exc:
        raise CanonicalizationError("schema_invalid", _first_error(exc)) from exc

    # Round-trip: parse -> serialize -> parse, and the two parses must agree. An
    # unstable field surfaces here as a mismatch rather than as a wrong stored row.
    try:
        reparsed = _Envelope.model_validate(json.loads(envelope.model_dump_json()))
    except (ValidationError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        raise CanonicalizationError("round_trip_mismatch", _first_error(exc)) from exc
    if reparsed != envelope:
        raise CanonicalizationError("round_trip_mismatch", "re-parsed event differs from original")

    return _to_canonical(envelope, disclosure_length=disclosure_length)


def _to_canonical(envelope: _Envelope, *, disclosure_length: int) -> CanonicalResult:
    payment = envelope.payload.payment.entity if envelope.payload.payment else None
    order = envelope.payload.order.entity if envelope.payload.order else None
    link = envelope.payload.payment_link.entity if envelope.payload.payment_link else None

    unix_ts = (payment.created_at if payment else None) or envelope.created_at
    provider_created_at = (
        datetime.fromtimestamp(unix_ts, tz=UTC) if unix_ts is not None else None
    )

    contact_value = None
    if payment is not None:
        contact_value = payment.contact or payment.email

    canonical = CanonicalPaymentEvent(
        event_name=envelope.event,
        provider_payment_id=payment.id if payment else None,
        provider_order_id=(payment.order_id if payment else None) or (order.id if order else None),
        payment_link_id=link.id if link else None,
        amount=payment.amount if payment else (order.amount if order else None),
        currency=payment.currency if payment else (order.currency if order else None),
        status=payment.status if payment else None,
        method=payment.method if payment else None,
        error_code=payment.error_code if payment else None,
        error_description=payment.error_description if payment else None,
        error_reason=payment.error_reason if payment else None,
        error_source=payment.error_source if payment else None,
        error_step=payment.error_step if payment else None,
        customer_key=_derive_customer_key(contact_value),
        customer_contact_masked=(
            _masked_contact(contact_value, disclosure_length=disclosure_length)
            if contact_value
            else None
        ),
        provider_created_at=provider_created_at.isoformat() if provider_created_at else None,
    )
    return CanonicalResult(canonical=canonical, provider_created_at=provider_created_at)


def _derive_customer_key(contact_value: str | None) -> str | None:
    """The keyed customer key, or ``None`` if there is no contact or no secret.

    A missing key secret degrades the cross-case opt-out join rather than dropping
    the event: the raw payload is still persisted encrypted, and the key can be
    backfilled once the secret is restored. Better a case that opens without the
    join than a real payment failure lost to a configuration gap.
    """
    if not contact_value:
        return None
    try:
        return customer_key(contact_value)
    except CredentialUnavailableError:
        _logger.warning("customer_key secret unavailable; canonical event has no customer_key")
        return None


def _masked_contact(value: str, *, disclosure_length: int) -> str:
    """:func:`mask_value` narrowed to the one field kind this module masks.

    ``mask_value`` is declared as returning ``object`` because it is also the pass-through for
    non-sensitive kinds, where it hands back whatever it was given. ``CONTACT`` is always
    sensitive, so the result is always text. Narrowing here rather than widening
    ``customer_contact_masked`` keeps the canonical model's field typed as the string it is.
    """
    masked = mask_value(value, FieldKind.CONTACT, disclosure_length=disclosure_length)
    if not isinstance(masked, str):  # pragma: no cover - CONTACT always masks to text
        raise CanonicalizationError(
            "schema_invalid", "customer contact did not mask to text"
        )
    return masked


def _first_error(exc: ValidationError | json.JSONDecodeError) -> str:
    """A one-line description of why a payload would not parse.

    Accepts both exception types because the round-trip check can fail either way: pydantic
    rejects a field, or the serialized form is not valid JSON at all. A ``JSONDecodeError`` has no
    ``errors()``, so it is handled separately rather than by hoping the attribute exists.
    """
    if isinstance(exc, json.JSONDecodeError):  # pragma: no cover - defensive
        return f"line {exc.lineno} column {exc.colno}: {exc.msg}"
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always populates
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', 'invalid')}"

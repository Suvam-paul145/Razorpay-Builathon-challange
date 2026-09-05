"""Just-in-time contact decryption. The only place cleartext PII exists in this process.

Three requirements collide here and the design resolves the collision rather than pretending
it away: the raw payload must be persisted verbatim (R1.C3), contact identifiers must be
stored masked (R17.C6), and creating a notifying payment link needs the contact in clear.
All three cannot hold literally.

The resolution: ``webhook_event.raw_payload_ciphertext`` is the single PII holder, encrypted
at rest under AES-256-GCM. Every derived table — ``recovery_case`` included — stores only a
masked form plus ``source_event_id`` pointing back at the encrypted row. This module is the
one place that follows that pointer, decrypts, and hands the result straight to the provider
request builder.

Four rules hold in this module, and each closes a specific leak:

* **Nothing decrypted is returned to a caller that persists.** The engine passes the value
  into ``build_payment_link_request`` and drops the reference; the frame ends and the object
  becomes garbage. No column, no cache, no module global.
* **Nothing decrypted is logged, at any level.** Not the email, not the phone, not the name,
  not a "decrypted contact for X" line with the value in a field. The log lines here carry a
  case id and an outcome word.
* **A decryption failure is not fatal and not silent.** It returns ``None`` with a reason.
  The engine refuses the action, which costs one recovery opportunity — the alternative is
  either crashing the worker or sending a link nobody receives.
* **Nothing here raises.** A missing pointer, a rotated-away key, a payload whose shape
  changed: all become a typed refusal. This code runs inside the transaction that is about
  to authorize an external effect, and an exception there is the least useful outcome.

The masked form on the case row is deliberately *not* used as a fallback. A masked phone
number is not a contactable address, and a provider given ``+91XXXXXX7890`` would either
reject it or, worse, notify someone else.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from revora.persistence.models import WebhookEvent
from revora.platform.crypto import payload_cipher
from revora.platform.logging import get_logger
from revora.providers.payment_link import CustomerContact, PaymentLinkRequestError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.persistence.models import RecoveryCase

__all__ = ["ContactResolution", "resolve_customer_contact"]

_logger = get_logger(__name__)




@dataclass(frozen=True, slots=True)
class ContactResolution:
    """A resolved contact, or the reason there is not one.

    ``contact`` is ``None`` exactly when ``reason`` is set. Callers branch on ``reason``
    rather than on truthiness of the contact, so an empty-but-present contact cannot be
    mistaken for a refusal.
    """

    contact: CustomerContact | None
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.contact is not None


def resolve_customer_contact(
    session: Session, merchant_id: uuid.UUID, case: RecoveryCase
) -> ContactResolution:
    """Decrypt the originating payload and extract the customer's contact details.

    Called by the engine while it still holds the case lock and before the intent is
    committed, so a refusal here happens in a window where nothing has been sent and
    nothing recorded.

    Args:
        case: the case row, already loaded. ``source_event_id`` is followed to the one
            encrypted row that holds cleartext.

    Returns:
        A resolution carrying either the contact or a short machine-readable reason. The
        reason never contains any part of the contact — it names the failure, not the data.
    """
    if case.source_event_id is None:
        return ContactResolution(None, "no_source_event")

    # Read the ciphertext columns only. Selecting the row through the ORM would work too,
    # but naming the three columns makes it obvious to a reviewer that nothing else about
    # the event is pulled into memory here.
    row = session.execute(
        select(
            WebhookEvent.raw_payload_ciphertext,
            WebhookEvent.raw_payload_nonce,
            WebhookEvent.key_version,
        ).where(
            WebhookEvent.id == case.source_event_id,
            WebhookEvent.merchant_id == merchant_id,
        )
    ).one_or_none()

    if row is None:
        return ContactResolution(None, "source_event_missing")

    ciphertext, nonce, key_version = row
    if nonce is None or key_version is None:
        return ContactResolution(None, "payload_not_encrypted")

    try:
        plaintext = payload_cipher().decrypt(bytes(ciphertext), bytes(nonce), int(key_version))
    except Exception as exc:
        # The exception type is logged, never its message: a cryptography error can quote
        # the material it failed on.
        _logger.warning(
            "contact decryption failed",
            case_id=str(case.id),
            failure=type(exc).__name__,
        )
        return ContactResolution(None, "decryption_failed")

    try:
        payload = json.loads(plaintext)
    except (ValueError, UnicodeDecodeError):
        return ContactResolution(None, "payload_not_json")
    finally:
        # Rebinding does not scrub the buffer — CPython gives no way to do that for an
        # immutable bytes object — but it drops the only reference this frame holds, so the
        # plaintext is collectable rather than living as long as the transaction.
        del plaintext

    entity = _payment_entity(payload)
    if entity is None:
        return ContactResolution(None, "payload_shape_unrecognised")

    phone = _clean(entity.get("contact"))
    email = _clean(entity.get("email"))

    # The phone number is required, not merely preferred. It is the channel the provider
    # always has, and ``CustomerContact`` rejects a blank one — a link nobody can be told
    # about is not an action, so refusing here is more honest than creating a link that
    # notifies no one and then reporting the action as taken.
    if phone is None:
        return ContactResolution(None, "no_contact_on_payment")

    try:
        contact = CustomerContact(contact=phone, email=email)
    except PaymentLinkRequestError as exc:
        # The rule name is safe to carry; the value that failed it is not.
        return ContactResolution(None, exc.rule)

    return ContactResolution(contact)


def _payment_entity(payload: object) -> dict[str, Any] | None:
    """Find the payment entity in a webhook payload, tolerating both known shapes.

    ``payload.payment.entity`` is the webhook envelope; a bare entity is what a direct API
    read looks like. Both are accepted because the case's source event may have arrived by
    either route once the detection-gap backfill exists — a backfilled case has no webhook
    behind it at all.
    """
    if not isinstance(payload, dict):
        return None

    nested = payload.get("payload")
    if isinstance(nested, dict):
        payment = nested.get("payment")
        if isinstance(payment, dict):
            entity = payment.get("entity")
            if isinstance(entity, dict):
                return entity

    # A bare entity identifies itself.
    if payload.get("entity") == "payment":
        return payload
    if "email" in payload or "contact" in payload:
        return payload
    return None


def _clean(value: object) -> str | None:
    """A non-empty trimmed string, or ``None``. Never logged by the caller."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None

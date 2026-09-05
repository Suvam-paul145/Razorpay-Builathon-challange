"""Quarantine for payloads that fail validation. Retained, encrypted, and named.

A malformed payload is either our schema being wrong or the provider changing theirs,
and both are diagnosed from the bytes — so the payload is retained rather than
discarded (R16.C13). It is held encrypted for the same reason ``webhook_event`` is: a
malformed payload is still a payload with a phone number in it. The row records which
validation rule rejected it, and that same rule name goes into the ``MALFORMED_EVENT``
audit record, so the quarantine row and the audit trail can be reconciled.

The response to a quarantined payload is 202, not a redelivery-inviting error: an
unparseable payload will not parse on the next attempt either, and asking the
provider to resend it just wastes both sides' retries (R16.C13).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from revora.persistence.models import EventQuarantine
from revora.platform.crypto import payload_cipher
from revora.platform.logging import get_logger
from revora.platform.secrets import CredentialUnavailableError

__all__ = ["quarantine_payload"]

_logger = get_logger(__name__)


def quarantine_payload(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    body: bytes,
    validation_rule: str,
    correlation_id: uuid.UUID,
    received_at: datetime,
    retention: timedelta,
    provider_event_id: str | None = None,
) -> EventQuarantine | None:
    """Persist a malformed payload, encrypted, with its failed rule named.

    Returns the row, or ``None`` if the payload encryption key is unavailable — in
    which case the malformed payload is not stored in clear as a fallback, because a
    quarantine that leaks contact data is worse than a diagnosis we cannot perform.
    The ``MALFORMED_EVENT`` audit record is still written by the caller regardless.
    """
    try:
        encrypted = payload_cipher().encrypt(body)
    except CredentialUnavailableError:
        _logger.warning("payload cipher unavailable; malformed payload not retained")
        return None

    row = EventQuarantine(
        merchant_id=merchant_id,
        provider_event_id=provider_event_id,
        validation_rule=validation_rule,
        raw_payload_ciphertext=encrypted.ciphertext,
        raw_payload_nonce=encrypted.nonce,
        key_version=encrypted.key_version,
        payload_bytes=len(body),
        received_at=received_at,
        correlation_id=correlation_id,
        retain_until=received_at + retention,
    )
    session.add(row)
    return row

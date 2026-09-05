"""Webhook signature verification, over the exact received bytes.

The HMAC itself — SHA-256 over the raw body, constant-time comparison against every
active secret in the merchant's rotation window — lives in ``platform.secrets``,
because the multi-secret window is a secret-store concern and the comparison must not
be re-implemented per caller. This module is the ingestion-facing surface: it names
the two headers, resolves the merchant's active secrets, and answers "is this body
authentic for this merchant" without the route touching the secret store directly.

The load-bearing rule is that the bytes verified here are the exact bytes received.
The provider documentation is explicit that a re-encoded JSON string will not match,
so the route reads ``await request.body()`` and hands those bytes straight through —
no Pydantic model, no middleware, and no proxy may re-serialize the body before this
runs. Canonicalization happens only *after* verification succeeds.

Merchant identity is the URL slug *and* the secret that verified. A body signed under
merchant A's secret cannot be attributed to merchant B, because B's secrets are the
only ones tried for a request to B's slug (R17.C12).
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Final

from revora.platform.logging import get_logger
from revora.platform.secrets import (
    SecretValue,
    get_secret_store,
    verify_webhook_signature,
)

__all__ = [
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "signature_canary",
    "verify_for_merchant",
]

SIGNATURE_HEADER: Final[str] = "X-Razorpay-Signature"
"""The header carrying the provider's HMAC-SHA256 of the body."""

EVENT_ID_HEADER: Final[str] = "x-razorpay-event-id"
"""The unique-per-event header. It is the deduplication key — a header, not a payload
field — so a payload with no stable per-delivery id is still deduplicated."""

_logger = get_logger(__name__)


def verify_for_merchant(body: bytes, provided_signature: str, merchant_slug: str) -> bool:
    """True if ``body`` carries a valid signature under any active secret for the slug.

    Resolves the merchant's ordered active secrets and compares against every one,
    even after a match, so the work done does not reveal which secret verified.

    Raises:
        CredentialUnavailableError: propagated from the secret store when the merchant
            has no configured secret. The caller must treat that as a configuration
            failure (refuse and audit), not as a forged signature (401) — the two
            demand different responses, and answering 401 would tell a legitimate
            provider retry its signature was wrong.
    """
    active = get_secret_store().webhook_signing_secrets(merchant_slug)
    return verify_webhook_signature(body, provided_signature, active)


_CANARY_SECRET: Final[SecretValue] = SecretValue(
    "revora.signature.canary.secret.not.a.real.credential"
)
_CANARY_BODY: Final[bytes] = b'{"event":"revora.signature.canary"}'


def signature_canary() -> bool:
    """Sign a known body and verify it end to end, through the real code path.

    Run at startup. It catches a broken HMAC construction — a wrong digest, a
    changed comparison — before the first real event does, by exercising the exact
    verification function the route uses. It signs the body itself, so a passing
    canary proves the construction; a body-mutating proxy in front of the process is
    a deployment concern the canary cannot see from inside, and is flagged separately
    by real events failing verification uniformly.
    """
    expected = hmac.new(_CANARY_SECRET.reveal_bytes(), _CANARY_BODY, sha256).hexdigest()
    ok = verify_webhook_signature(_CANARY_BODY, expected, (_CANARY_SECRET,))
    if not ok:  # pragma: no cover - only if the HMAC construction is broken
        _logger.error("signature canary failed; webhook verification path is broken")
    return ok

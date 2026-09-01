"""Runtime secret resolution, and the rotation window that makes it safe.

Nothing in this module reads a secret from source control or from a database row.
Environment (or the platform's secret storage projected into it) holds exactly
three things per the design's Configuration and Migrations subsection: connection
strings, secret references, and the process role. The ~50 tunable bounds live in
``app_config`` instead, because R15.C6 requires a recorded approving user for a
policy change and a redeploy cannot supply one.

Two decisions here carry weight.

**The webhook signing secret is a list, not a value.** Razorpay redelivers a failed
event for up to 24 hours. If a rotation replaced the secret with a single new
value, every event still being retried would fail signature verification and be
answered 401 — silently dropping real payment failures for a day, which is exactly
the revenue leak Revora exists to catch. So a merchant has an *ordered* list of
active secrets, verification succeeds if any of them matches, and the retired
secret is only dropped once the redelivery window has closed. The design's
Signature Verification finding recommends this as an addition to
``requirements.md``; it is not in the requirements as written.

**A missing credential is a classifiable outcome, not a crash.** R17.C4 requires
that an unreadable credential refuses the external call, leaves state unchanged,
and audits ``CREDENTIAL_UNAVAILABLE``. A caller that catches a bare ``KeyError``
cannot tell that apart from a bug and might retry, or worse, proceed. So there is
one exception type, it names the credential, and it never names the value.

No secret value appears in a ``repr``, a ``str``, a log record or an exception
message anywhere in this module. ``SecretValue`` exists to make that a property of
the type rather than a habit of the programmer.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Final, Protocol

from revora.platform.clock import ensure_utc

__all__ = [
    "CREDENTIAL_UNAVAILABLE",
    "WEBHOOK_REDELIVERY_WINDOW",
    "CredentialUnavailableError",
    "EnvironmentSecretResolver",
    "SecretResolver",
    "SecretStore",
    "SecretValue",
    "WebhookSecretEntry",
    "active_webhook_secrets",
    "get_secret_store",
    "reset_secret_store",
    "set_secret_store",
    "verify_dashboard_key",
    "verify_webhook_signature",
]

CREDENTIAL_UNAVAILABLE: Final[str] = "CREDENTIAL_UNAVAILABLE"
"""Audit event type recorded when a credential could not be resolved (R17.C4)."""

ENV_RAZORPAY_KEY_ID: Final[str] = "REVORA_RAZORPAY_KEY_ID"
ENV_RAZORPAY_KEY_SECRET: Final[str] = "REVORA_RAZORPAY_KEY_SECRET"
ENV_PAYLOAD_ENCRYPTION_KEYS: Final[str] = "REVORA_PAYLOAD_ENCRYPTION_KEYS"
ENV_CUSTOMER_KEY_SECRET: Final[str] = "REVORA_CUSTOMER_KEY_SECRET"
ENV_LLM_CREDENTIAL: Final[str] = "REVORA_LLM_CREDENTIAL"
ENV_WEBHOOK_SECRETS_PREFIX: Final[str] = "REVORA_WEBHOOK_SECRETS_"
"""Suffixed with the merchant slug, upper-cased, non-alphanumerics as underscores.
Value is a comma-separated list, newest secret first."""

ENV_DASHBOARD_KEYS_PREFIX: Final[str] = "REVORA_DASHBOARD_KEYS_"
"""Suffixed the same way as the webhook prefix. Comma-separated, newest first.

The credential that mints a dashboard session. **[BUILD LATER]** and honest about it:
``merchant_user`` has no password column and never had one, because the design assumes an
external identity provider and states plainly that per-user roles and MFA are deferred for a
single-operator persona. Inventing a password column now would mean inventing a hashing
scheme, a reset flow and a lockout policy — four things to get wrong — for a persona that
does not need them.

So a session is minted by presenting a per-merchant operator key, verified in constant time
against this list, exactly as a webhook signature is verified. It is a shared secret rather
than a user credential, which is why the session it produces still names a specific
``merchant_user``: the audit trail has to say who acted even when the credential does not."""

ENV_SESSION_TOKEN_SECRET: Final[str] = "REVORA_SESSION_TOKEN_SECRET"
"""HMAC secret the stored session token digest is keyed with.

Separate from ``REVORA_CUSTOMER_KEY_SECRET`` on purpose. Rotating the customer key secret
invalidates every stored ``customer_key`` and therefore every cross-case opt-out join;
rotating this one invalidates live dashboard sessions and nothing else. Sharing one secret
would couple "log everybody out" to "lose the opt-out index", which is the kind of coupling
that stops a rotation from ever happening."""

WEBHOOK_REDELIVERY_WINDOW: Final[timedelta] = timedelta(hours=24)
"""How long a retired webhook secret must stay active. Matches the provider's
documented redelivery window: drop the old secret sooner and an in-flight retry of
a real payment failure is rejected as a forgery."""


class CredentialUnavailableError(RuntimeError):
    """A credential is missing, blank, or malformed.

    The caller's contract on catching this: refuse the external call, leave case
    state unchanged, and write a ``CREDENTIAL_UNAVAILABLE`` audit record naming
    ``credential``. Do not retry in the same transaction — nothing about the
    environment will have changed.

    ``credential`` is a name, never a value. ``reason`` is a category, never a
    value; anything constructing this must not interpolate the secret it failed to
    parse into it.
    """

    def __init__(self, credential: str, reason: str = "not configured") -> None:
        self.credential = credential
        self.reason = reason
        self.audit_event_type = CREDENTIAL_UNAVAILABLE
        super().__init__(f"credential {credential!r} unavailable: {reason}")


class SecretValue:
    """A string that will not print itself.

    ``repr``, ``str`` and formatting all yield a redaction, so a secret cannot leak
    through an f-string, a ``%s`` in a log call, a ``pytest`` assertion diff or a
    traceback frame. ``reveal()`` is the only way out and it is deliberately
    conspicuous at the call site.

    Equality is constant-time so this type is usable in a comparison without
    reintroducing the timing leak that ``hmac.compare_digest`` exists to avoid.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def reveal_bytes(self) -> bytes:
        return self._value.encode("utf-8")

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # Length only. Useful for a "is this plausibly a key" check without reveal.
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        # Hashing the value would put it in a dict key's repr on a collision dump.
        return hash(("SecretValue", len(self._value)))


class SecretResolver(Protocol):
    """Where secret values come from. One method, so a test can substitute a dict."""

    def get(self, name: str) -> str | None:
        """The raw value for ``name``, or ``None`` if absent or blank."""
        ...


class EnvironmentSecretResolver:
    """Reads from a mapping, defaulting to ``os.environ``.

    A blank value is treated as absent: an env var set to the empty string is how a
    misconfigured deploy usually presents, and treating it as a valid credential
    would send an unauthenticated request instead of refusing.
    """

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def get(self, name: str) -> str | None:
        raw = self._environ.get(name)
        if raw is None:
            return None
        stripped = raw.strip()
        return stripped or None

    def __repr__(self) -> str:
        return "EnvironmentSecretResolver()"


@dataclass(frozen=True, slots=True)
class WebhookSecretEntry:
    """One webhook signing secret and its rotation timestamps.

    ``retired_at`` set means a rotation has happened and this secret is no longer
    given to the provider — but it stays in the active list until the redelivery
    window closes, because events signed with it may still arrive.
    """

    secret: SecretValue
    activated_at: datetime
    retired_at: datetime | None = None

    def is_active(self, *, now: datetime, window: timedelta = WEBHOOK_REDELIVERY_WINDOW) -> bool:
        moment = ensure_utc(now)
        if ensure_utc(self.activated_at) > moment:
            return False
        if self.retired_at is None:
            return True
        return ensure_utc(self.retired_at) + window >= moment


def active_webhook_secrets(
    entries: Iterable[WebhookSecretEntry],
    *,
    now: datetime,
    window: timedelta = WEBHOOK_REDELIVERY_WINDOW,
) -> tuple[SecretValue, ...]:
    """The ordered active secrets: newest activation first, retired-but-in-window last.

    Newest first because the overwhelming majority of events are signed with the
    current secret, so it is the comparison that should happen first. The retired
    ones are still there, which is the whole point.
    """
    active = [entry for entry in entries if entry.is_active(now=now, window=window)]
    active.sort(key=lambda entry: ensure_utc(entry.activated_at), reverse=True)
    return tuple(entry.secret for entry in active)


def verify_webhook_signature(
    body: bytes,
    provided_signature: str,
    active_secrets: Sequence[SecretValue],
) -> bool:
    """True if ``provided_signature`` is a valid HMAC-SHA256 of ``body`` under any
    active secret.

    ``body`` must be the exact bytes received. The provider documentation is
    explicit that a re-serialized JSON string will not match, so the caller —
    ``revora.ingestion.signature`` (task 9.1), which owns the size cap and the
    parse-after-verify ordering — hands the raw body straight through.

    Every secret is compared even after a match, so the work done does not reveal
    *which* secret verified. Each individual comparison is ``compare_digest``.

    Raises:
        CredentialUnavailableError: if no active secret was supplied. Returning
            ``False`` would be indistinguishable from a forged signature, and the
            two demand different responses: 401 for a forgery, refuse-and-audit for
            a configuration failure.
    """
    if not active_secrets:
        raise CredentialUnavailableError("webhook_signing_secret", "no active secret for merchant")
    matched = False
    for secret in active_secrets:
        expected = hmac.new(secret.reveal_bytes(), body, sha256).hexdigest()
        if hmac.compare_digest(expected, provided_signature):
            matched = True
    return matched


def verify_dashboard_key(presented: str, keys: Sequence[SecretValue]) -> bool:
    """True if ``presented`` equals any configured dashboard key for the merchant.

    Every key is compared even after a match, and each comparison is ``compare_digest``, for
    the same two reasons as the webhook check: a timing signal must not reveal *which* key
    matched, and a byte-by-byte early exit must not reveal how much of a wrong key was right.

    An empty ``presented`` is refused before any comparison. ``compare_digest("", "")`` is
    ``True``, so a merchant whose key list somehow contained a blank entry would otherwise
    authenticate an empty header — and the blank entry is exactly what a misconfigured
    environment variable produces.

    Raises:
        CredentialUnavailableError: if no key was supplied, distinguished from a wrong key
            because one is a configuration failure and the other is an authentication failure.
    """
    if not keys:
        raise CredentialUnavailableError("dashboard_key", "no key configured for merchant")
    if not presented:
        return False
    matched = False
    for key in keys:
        if hmac.compare_digest(key.reveal(), presented):
            matched = True
    return matched


class SecretStore:
    """Named accessors for every credential Revora needs.

    Named rather than a generic ``get(name)`` so that the set of secrets the system
    uses is enumerable by reading one class, and so a typo in a key name is an
    ``AttributeError`` at import rather than a ``None`` at runtime.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver: SecretResolver | None = None) -> None:
        self._resolver: SecretResolver = resolver or EnvironmentSecretResolver()

    def _require(self, name: str, credential: str) -> SecretValue:
        raw = self._resolver.get(name)
        if raw is None:
            raise CredentialUnavailableError(credential)
        return SecretValue(raw)

    def razorpay_key_id(self) -> SecretValue:
        """Provider API key id.

        Not strictly secret, but resolved through the same path so that a partial
        credential set fails as one thing. Operational note from the design: the
        Razorpay key pair is shared between Payment Gateway and RazorpayX, so
        rotating it has blast radius outside Revora.
        """
        return self._require(ENV_RAZORPAY_KEY_ID, "razorpay_key_id")

    def razorpay_key_secret(self) -> SecretValue:
        """Provider API key secret."""
        return self._require(ENV_RAZORPAY_KEY_SECRET, "razorpay_key_secret")

    def llm_credential(self) -> SecretValue:
        """Reasoning-layer provider credential.

        The one credential whose absence is not an incident: every sanctioned AI use
        has a deterministic fallback, so a missing LLM key degrades diagnosis to the
        rule-based path rather than stopping the pipeline.
        """
        return self._require(ENV_LLM_CREDENTIAL, "llm_credential")

    def customer_key_secret(self) -> bytes:
        """HMAC secret behind ``crypto.customer_key``.

        Base64, at least 32 bytes decoded. Rotating it invalidates every stored
        ``customer_key``, so it is a separate credential from the payload key rather
        than a reuse of it.
        """
        value = self._require(ENV_CUSTOMER_KEY_SECRET, "customer_key_secret")
        return _decode_key(value.reveal(), "customer_key_secret", minimum_length=32)

    def payload_encryption_keys(self) -> Mapping[int, bytes]:
        """Versioned AES-256 keys for the raw event store.

        Format: ``1:<base64>,2:<base64>`` — version, colon, key. More than one entry
        is normal, not exceptional: a rotation adds a higher version and keeps the
        previous one so historical rows stay readable. The highest version present
        is the one new rows are written under.

        Raises:
            CredentialUnavailableError: if the variable is absent, malformed, or any
                key is not exactly 32 bytes. Malformed counts as unavailable
                because the caller's correct response is identical.
        """
        value = self._require(ENV_PAYLOAD_ENCRYPTION_KEYS, "payload_encryption_keys")
        keys: dict[int, bytes] = {}
        for chunk in value.reveal().split(","):
            entry = chunk.strip()
            if not entry:
                continue
            version_text, separator, key_text = entry.partition(":")
            if not separator:
                raise CredentialUnavailableError(
                    "payload_encryption_keys", "malformed entry, expected version:base64"
                )
            try:
                version = int(version_text.strip())
            except ValueError as exc:
                raise CredentialUnavailableError(
                    "payload_encryption_keys", "version is not an integer"
                ) from exc
            keys[version] = _decode_key(
                key_text.strip(), "payload_encryption_keys", minimum_length=32, exact=True
            )
        if not keys:
            raise CredentialUnavailableError("payload_encryption_keys", "no key entries")
        return keys

    def webhook_signing_secrets(self, merchant_slug: str) -> tuple[SecretValue, ...]:
        """The ordered active signing secrets for one merchant, newest first.

        Environment-backed for now: the ``webhook_secret`` table (task 5.1) becomes
        the source once persistence exists, at which point ``active_webhook_secrets``
        does the window filtering that a static list cannot. The order is the
        contract either way.

        Raises:
            CredentialUnavailableError: if the merchant has no configured secret.
                Verification cannot proceed, and answering 401 would tell a
                legitimate provider retry that its signature was wrong.
        """
        name = ENV_WEBHOOK_SECRETS_PREFIX + _slug_to_env(merchant_slug)
        raw = self._resolver.get(name)
        if raw is None:
            raise CredentialUnavailableError("webhook_signing_secret", "no secret for merchant")
        values = tuple(SecretValue(part.strip()) for part in raw.split(",") if part.strip())
        if not values:
            raise CredentialUnavailableError("webhook_signing_secret", "no secret for merchant")
        return values

    def dashboard_keys(self, merchant_slug: str) -> tuple[SecretValue, ...]:
        """The operator keys that may mint a dashboard session for one merchant, newest first.

        Same shape as :meth:`webhook_signing_secrets` and for the same reason: more than one
        entry is normal, because a rotation adds the new key and keeps the old one until every
        operator has moved. A tuple rather than a single value is what makes rotation possible
        without a window where nobody can log in.

        Raises:
            CredentialUnavailableError: if the merchant has no configured key. Minting refuses,
                which is the safe direction — a merchant with no dashboard credential should be
                unreachable rather than open.
        """
        name = ENV_DASHBOARD_KEYS_PREFIX + _slug_to_env(merchant_slug)
        raw = self._resolver.get(name)
        if raw is None:
            raise CredentialUnavailableError("dashboard_key", "no key for merchant")
        values = tuple(SecretValue(part.strip()) for part in raw.split(",") if part.strip())
        if not values:
            raise CredentialUnavailableError("dashboard_key", "no key for merchant")
        return values

    def session_token_secret(self) -> bytes:
        """HMAC secret behind the stored session-token digest.

        Base64, at least 32 bytes decoded, like the customer-key secret. The token itself is
        never stored — only its keyed digest — so a database disclosure does not hand over live
        sessions.
        """
        value = self._require(ENV_SESSION_TOKEN_SECRET, "session_token_secret")
        return _decode_key(value.reveal(), "session_token_secret", minimum_length=32)

    def __repr__(self) -> str:
        return f"SecretStore(resolver={self._resolver!r})"


def _slug_to_env(merchant_slug: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in merchant_slug).upper()


def _decode_key(
    encoded: str,
    credential: str,
    *,
    minimum_length: int,
    exact: bool = False,
) -> bytes:
    try:
        material = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialUnavailableError(credential, "not valid base64") from exc
    if exact and len(material) != minimum_length:
        raise CredentialUnavailableError(
            credential, f"decoded key must be exactly {minimum_length} bytes"
        )
    if len(material) < minimum_length:
        raise CredentialUnavailableError(
            credential, f"decoded key must be at least {minimum_length} bytes"
        )
    return material


_store: SecretStore = SecretStore()


def get_secret_store() -> SecretStore:
    """The process-wide store."""
    return _store


def set_secret_store(store: SecretStore) -> SecretStore:
    """Install ``store``, returning the previous one. Bootstrap and tests only."""
    global _store
    previous = _store
    _store = store
    return previous


def reset_secret_store() -> None:
    """Restore the environment-backed store."""
    set_secret_store(SecretStore())

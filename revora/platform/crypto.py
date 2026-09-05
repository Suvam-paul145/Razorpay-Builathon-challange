"""Envelope encryption for raw payloads, and the keyed customer key.

This module is the mechanism that resolves the conflict the design flags as HIGH
severity: R17.C6 wants contact identifiers masked at write time, R1.C3 wants the
provider's raw payload persisted durably, and the raw ``payment.failed`` payload
contains ``contact`` and ``email`` in clear. All three cannot hold literally.

The resolution: ``webhook_event.raw_payload`` is stored as AES-256-GCM ciphertext
and is the *only* holder of cleartext contact data. Every derived table, every
audit record and every log line holds a masked value (see ``masking``) plus a
pointer to that row. The Execution_Engine decrypts just in time, inside the
execution transaction, and never persists or logs what it decrypted.

Two separate keys, deliberately:

- The **payload key** is reversible by design, because the contact has to be
  recoverable to create a notifying payment link.
- The **customer key secret** is used for an HMAC and nothing else. ``customer_key``
  must not be reversible: it is stored in ``recovery_case`` and ``customer_consent``
  on every row, which makes it the widest-spread customer-derived value in the
  database, and a widely-spread reversible identifier is a contact list.

Key versioning exists so a key can be rotated without a migration that rewrites
every historical row. Old rows keep their ``key_version`` and stay readable; new
rows are written under the highest version present.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Final, NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "KEY_LENGTH",
    "NONCE_LENGTH",
    "CustomerKeyHasher",
    "EncryptedPayload",
    "PayloadCipher",
    "PayloadDecryptionError",
    "UnknownKeyVersionError",
    "customer_key",
    "normalize_contact",
    "payload_cipher",
    "reset_cached_material",
]

KEY_LENGTH: Final[int] = 32
"""AES-256. Anything shorter is refused rather than stretched, because a silently
weaker key is worse than a startup failure."""

NONCE_LENGTH: Final[int] = 12
"""96 bits, the GCM-recommended nonce size. Generated fresh per call from
``os.urandom`` — never a counter, never derived from the plaintext. Nonce reuse
under one key is the single failure that breaks GCM completely, so there is no
code path here that can produce a nonce twice."""


class EncryptedPayload(NamedTuple):
    """What a caller stores: ciphertext, the nonce it needs to read it back, and
    which key version wrote it. The nonce and version are not secret."""

    ciphertext: bytes
    nonce: bytes
    key_version: int


class UnknownKeyVersionError(LookupError):
    """A row references a key version this process was not given.

    Means a key was dropped from configuration while rows written under it still
    exist. Distinct from a decryption failure because the remedy is different:
    restore the key, do not investigate tampering.
    """


class PayloadDecryptionError(RuntimeError):
    """Authentication failed. The ciphertext, nonce or key does not match.

    Carries no detail about which — a caller cannot act on the difference, and an
    error message that distinguishes them is an oracle.
    """


class PayloadCipher:
    """AES-256-GCM over raw webhook payloads, with versioned keys.

    Args:
        keys: version number to 32-byte key. More than one so a rotation can add
            a version while the previous one is still needed to read old rows.
        current_version: which version to write under. Defaults to the highest,
            which makes "rotate" mean "add a higher-numbered key" and nothing else.
    """

    __slots__ = ("_current_version", "_keys")

    def __init__(self, keys: Mapping[int, bytes], *, current_version: int | None = None) -> None:
        if not keys:
            raise ValueError("at least one payload encryption key is required")
        for version, key in keys.items():
            if len(key) != KEY_LENGTH:
                raise ValueError(
                    f"payload encryption key version {version} is {len(key)} bytes; "
                    f"AES-256 requires exactly {KEY_LENGTH}"
                )
        self._keys: dict[int, bytes] = dict(keys)
        chosen = max(self._keys) if current_version is None else current_version
        if chosen not in self._keys:
            raise ValueError(f"current key version {chosen} is not among the supplied keys")
        self._current_version: int = chosen

    @property
    def current_version(self) -> int:
        return self._current_version

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def encrypt(self, plaintext: bytes) -> EncryptedPayload:
        """Encrypt under the current key with a fresh nonce.

        The key version is bound in as associated data, so a row whose stored
        ``key_version`` was altered to point at a different key fails
        authentication rather than being decrypted under the wrong key and
        producing something that might parse.
        """
        nonce = os.urandom(NONCE_LENGTH)
        aead = AESGCM(self._keys[self._current_version])
        ciphertext = aead.encrypt(nonce, plaintext, _version_aad(self._current_version))
        return EncryptedPayload(ciphertext, nonce, self._current_version)

    def decrypt(self, ciphertext: bytes, nonce: bytes, key_version: int) -> bytes:
        """Decrypt a stored payload.

        Raises:
            UnknownKeyVersionError: the referenced key is not configured.
            PayloadDecryptionError: authentication failed.
        """
        key = self._keys.get(key_version)
        if key is None:
            raise UnknownKeyVersionError(
                f"no payload encryption key configured for version {key_version}"
            )
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, _version_aad(key_version))
        except InvalidTag as exc:
            raise PayloadDecryptionError("payload authentication failed") from exc

    def __repr__(self) -> str:
        # Versions only. Never the key material.
        return f"PayloadCipher(versions={self.versions}, current={self._current_version})"


def _version_aad(version: int) -> bytes:
    return f"revora.payload.v{version}".encode("ascii")


# ---------------------------------------------------------------------------
# customer_key
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\D")

#: Indian subscriber numbers are ten digits. A contact arriving as
#: ``+919876543210``, ``919876543210``, ``09876543210`` or ``98765 43210`` is one
#: customer, and the opt-out join in the policy hot path has to see one key or a
#: customer who opted out once will be contacted again through the other format.
#: [ASSUMPTION] scoped to INR merchants — see ``normalize_contact``.
_SUBSCRIBER_DIGITS: Final[int] = 10


def normalize_contact(contact: str) -> str:
    """Normalize a contact so two spellings of one customer produce one key.

    The rules, in order:

    1. Strip surrounding whitespace.
    2. If it contains ``@``, treat it as an email and casefold the whole thing.
       Local-part case sensitivity is legal per RFC 5321 and honoured by
       essentially no provider, and treating ``A@x.com`` and ``a@x.com`` as two
       customers would let one of them be contacted after the other opted out.
    3. Otherwise treat it as a phone number: discard every non-digit, then keep
       the last ten digits. That collapses country codes, trunk zeros, spaces,
       dashes and brackets onto one value.

    **The ten-digit rule is an [ASSUMPTION] for INR merchants.** It means two
    numbers from different countries sharing their last ten digits collide. That
    is acceptable here for one specific reason: ``customer_key`` is only ever
    joined within a merchant (``customer_consent (merchant_id, customer_key)``), so
    a collision is confined to one merchant's customer base, and the failure mode
    it produces is a suppressed message rather than an unwanted one. Revisit before
    a merchant transacts in a second country.

    A value with no digits and no ``@`` falls back to casefolded text, so an
    unrecognised identifier still hashes stably instead of collapsing to empty.

    Raises:
        ValueError: on an empty or whitespace-only contact. There is no meaningful
            key for "no contact", and returning one would let every such case join
            to every other.
    """
    stripped = contact.strip()
    if not stripped:
        raise ValueError("contact must not be empty")
    if "@" in stripped:
        return stripped.casefold()
    digits = _DIGITS.sub("", stripped)
    if not digits:
        return stripped.casefold()
    return digits[-_SUBSCRIBER_DIGITS:] if len(digits) > _SUBSCRIBER_DIGITS else digits


class CustomerKeyHasher:
    """HMAC-SHA256 over a normalized contact, under a dedicated secret.

    Keyed rather than a plain digest: an unkeyed hash of a ten-digit phone number
    is enumerable in seconds, which would make the column a reversible contact
    list with extra steps. The secret is used for nothing else, so rotating it does
    not force a payload re-encryption — but it *does* invalidate every stored key,
    so rotation requires recomputing them from the encrypted payloads. Documented
    here because that cost is the reason this secret is separate from the payload
    key rather than shared with it.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if len(secret) < KEY_LENGTH:
            raise ValueError(
                f"customer key secret must be at least {KEY_LENGTH} bytes, got {len(secret)}"
            )
        self._secret = bytes(secret)

    def key(self, contact: str) -> str:
        """The stable, non-reversible key for a contact. Hex, 64 characters."""
        normalized = normalize_contact(contact)
        return hmac.new(self._secret, normalized.encode("utf-8"), sha256).hexdigest()

    def __repr__(self) -> str:
        return "CustomerKeyHasher(<secret redacted>)"


_cached_cipher: PayloadCipher | None = None
_cached_hasher: CustomerKeyHasher | None = None


def payload_cipher() -> PayloadCipher:
    """The process-wide cipher, built from resolved secrets on first use.

    Lazy rather than at import time so that importing this module does not require
    credentials to be present — which matters for the pure test tier, and means a
    missing key surfaces as ``CredentialUnavailableError`` at the call site that
    needed it rather than as an import failure at startup.
    """
    global _cached_cipher
    if _cached_cipher is None:
        from revora.platform.secrets import get_secret_store

        _cached_cipher = PayloadCipher(get_secret_store().payload_encryption_keys())
    return _cached_cipher


def customer_key(contact: str) -> str:
    """Keyed, non-reversible key for a customer contact.

    This is what cross-case opt-out joins on (R17.C10, P8): consent is keyed on the
    customer, not the case, so an opt-out recorded against one failed payment
    suppresses contact on every other case for the same person.
    """
    global _cached_hasher
    if _cached_hasher is None:
        from revora.platform.secrets import get_secret_store

        _cached_hasher = CustomerKeyHasher(get_secret_store().customer_key_secret())
    return _cached_hasher.key(contact)


def reset_cached_material() -> None:
    """Drop the cached cipher and hasher. For tests, and for a secret reload."""
    global _cached_cipher, _cached_hasher
    _cached_cipher = None
    _cached_hasher = None

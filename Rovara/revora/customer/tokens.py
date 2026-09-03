"""Mint, verify, revoke — the Customer_Access_Token and the whole of its authority.

The token is the second credential in a system that otherwise has exactly one, and every
decision in this module is about bounding what holding it is worth. An attacker with a live
token can read one case's amount, currency, merchant display name, plain-language failure
reason, payment link URL and window end, and can write at most
``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` signals to that one case. It reaches no second case, no
audit record, no recommendation, no policy decision, no metric and no configuration value
(R18.C10) — and the reason that list holds is structural rather than diligent: this package
sits below the optimizer, the policy engine and the metrics engine in the layering contract,
so there is nothing here to import that could leak them.

Wire form::

    rvc_<token_id>.<secret>
    token_id = base32(16 random bytes), 26 chars, unpadded, lowercase
    secret   = base64url(16 random bytes), 22 chars, unpadded  -> 128 bits

Four properties of that shape are load-bearing.

**The ``token_id`` is separately random, not derived from the secret.** It is the lookup
handle, it is the only form permitted in a log line or an audit field (R18.C11), and it is
therefore the form that gets copied around. Deriving it from the secret would make every
place it is legitimately written a place the secret is partially disclosed.

**What is stored is ``HMAC-SHA256(signing_key, token_id ‖ secret)``.** Keyed rather than a
plain digest, so a database dump alone does not permit offline verification — an unkeyed
digest of a 128-bit secret is not brute-forceable, but it *is* verifiable against a guess,
and a keyed one is not. No reversible copy of the secret exists anywhere (R18.C3), which is
why the reuse path below can return a token's identity and not its URL.

**Verification does one indexed lookup and then compares against every active signing
secret, accumulating with ``|=`` and never breaking early**, so the time taken is
independent of which secret matched and of whether any did (R18.C4, R29.C6). A missing row
does not skip the loop: it substitutes a per-process decoy hash and does exactly the same
work, so "no such handle", "wrong secret" and "signed by a retired key" fold into one branch
that returns an identical status and an identical body. A malformed token does not return
early either — it becomes a decoy handle of the right shape and takes the same path.

**Expiry is the earlier of ``issued_at + CUSTOMER_TOKEN_LIFETIME`` and the case's
``window_end_at``, and is never extended** (R18.C2). Minting a replacement for an expired
predecessor revokes it with ``EXPIRED_SUPERSEDED`` in the same transaction, before the
insert. That ordering is not stylistic: ``one_live_token_per_case`` is a *partial* unique
index over ``revoked_at IS NULL``, and expiry cannot be in its predicate because expiry
needs ``now()`` — so an expired-but-unrevoked predecessor is still in the index, and an
insert beside it fails. Revoking first removes the old row from the predicate, and doing
both in one transaction is what makes the supersession auditable rather than inferred.

**Rotation (R29.C14).** Minting always uses the highest configured version; verification
tries every configured version. A token whose signing version has been *retired* — removed
from the configured set — therefore matches nothing and takes the same 404 path as a token
that never existed. That is **stronger than R29.C14's 410 and chosen for that reason**:
distinguishing "signed by a retired key" from "not a real token" tells an attacker that
their guess had the right shape. ``CUSTOMER_TOKEN_KEY_RETIRED`` is recorded as the rejection
category so "how many customers did that rotation lock out" stays answerable; the caller
sees 404 and an empty body.

**A missing signing secret mints nothing and verifies nothing** (R29.C13). It resolves
through ``revora.platform.secrets``, whose contract already refuses to invent a credential,
and the answer is a ``CREDENTIAL_UNAVAILABLE`` record and HTTP 503 — not a rejection, which
would be indistinguishable from a forgery, and not an exception escaping into a 500.

**Why ``merchant_id`` is an argument to every function here.** It looks redundant against
R18.C4's "derive the Merchant identifier from the verified token alone", and it is not. The
token's authority still comes only from the persisted row: the ``merchant_id`` on
:class:`VerifiedToken` is read off that row, and the case id is never taken from a request.
What the argument does is keep this module inside the rule the whole persistence package is
built on — every read and write names its tenant, and there is deliberately no
"find this token anywhere" repository function to call by accident. A token presented under
the wrong tenant finds no row and takes the 404 path, which is the correct answer.

Nothing here transitions a case, evaluates policy, schedules an action or calls a provider.
"""

from __future__ import annotations

import base64
import hmac
import secrets as _secrets
import string
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, Protocol

from sqlalchemy.exc import IntegrityError

from revora.audit.events import (
    CREDENTIAL_UNAVAILABLE,
    CUSTOMER_TOKEN_EXPIRED,
    CUSTOMER_TOKEN_ISSUED,
    CUSTOMER_TOKEN_KEY_RETIRED,
    CUSTOMER_TOKEN_REJECTED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import CandidateAction
from revora.domain.enums import FieldKind, TokenRevocationReason
from revora.persistence.models.customer import SECRET_HASH_BYTES, CustomerAccessToken
from revora.persistence.repositories.customer import CustomerAccessTokenRepository
from revora.platform.clock import ensure_utc, now
from revora.platform.logging import get_logger
from revora.platform.masking import sensitive
from revora.platform.secrets import CredentialUnavailableError, get_secret_store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "CUSTOMER_TOKEN_ENTROPY_BITS",
    "ENTROPY_BYTES",
    "REJECTION_STATUS",
    "SECRET_LENGTH",
    "TOKEN_ID_LENGTH",
    "TOKEN_PREFIX",
    "AuditSink",
    "MintOutcome",
    "MintedToken",
    "TokenIssueFailure",
    "TokenRejection",
    "TokenService",
    "TokenStore",
    "VerificationOutcome",
    "VerifiedToken",
    "revoke_tokens_for_case",
    "wire_token",
]

_logger = get_logger(__name__)

_ACTOR: Final[str] = "customer_response_service"
"""The actor on a token-lifecycle audit record.

Not the ``token_id``, on the rejection records specifically. R18.C6 requires a rejection to
record the category and **no part of the presented token**, and a handle that matched nothing
is attacker-supplied text. R29.C9's "token identifier in the actor field" governs an accepted
or rejected *signal write*, where the token verified and the handle is genuinely ours."""

TOKEN_PREFIX: Final[str] = "rvc_"
ENTROPY_BYTES: Final[int] = 16
CUSTOMER_TOKEN_ENTROPY_BITS: Final[int] = ENTROPY_BYTES * 8
"""128 bits in each half, from ``secrets.token_bytes`` — the standard library's CSPRNG.

Named as the requirement names it (R18.C1) rather than left implicit in a byte count, and a
derived value rather than a second literal, so the two cannot disagree."""

TOKEN_ID_LENGTH: Final[int] = 26
"""``len(base32(16 bytes))`` with padding stripped."""

SECRET_LENGTH: Final[int] = 22
"""``len(base64url(16 bytes))`` with padding stripped."""

_TOKEN_ID_ALPHABET: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz234567")
_SECRET_ALPHABET: Final[frozenset[str]] = frozenset(
    string.ascii_letters + string.digits + "-_"
)

_DECOY_TOKEN_ID: Final[str] = "a" * TOKEN_ID_LENGTH
_DECOY_SECRET: Final[str] = "A" * SECRET_LENGTH
"""Substituted for a malformed presentation so it takes the same path as a well-formed one.

A malformed token cannot be looked up, so returning early would make "this is not even a
token" measurably cheaper than "this is a token I do not have" — a distinction an attacker
can use to learn the wire format from timing alone, before guessing anything."""

_ABSENT_ROW_HASH: Final[bytes] = _secrets.token_bytes(SECRET_HASH_BYTES)
"""What a missing row's ``secret_hash`` compares against.

Per process and random, so it cannot be hit by a guess, and 32 bytes so
``compare_digest`` against a real signature does the same work it would against a real row.
Fixed bytes would work equally well cryptographically; random costs nothing and removes the
question."""


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@unique
class TokenIssueFailure(StrEnum):
    """Why a mint did not produce a token. Every member is a reason R18.C13 must name."""

    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    """The signing secret is absent, unreadable or malformed (R29.C13). Nothing is minted,
    and the caller's correct response is to abandon the execution attempt."""

    WINDOW_ALREADY_CLOSED = "WINDOW_ALREADY_CLOSED"
    """The case's ``window_end_at`` is at or before the issuance instant, so the earlier of
    the two bounds in R18.C2 is already past. There is no token to mint: ``expires_at >
    issued_at`` is a database constraint, and a token that expires the moment it is created
    is a URL that reads as broken to the one customer who receives it."""

    PERSISTENCE_CONFLICT = "PERSISTENCE_CONFLICT"
    """``one_live_token_per_case`` refused the insert, which means another transaction
    committed a live token for this case in between. R18.C14 wants one token, and the other
    transaction has it — so this attempt abandons rather than competing."""


@unique
class TokenRejection(StrEnum):
    """Why a presented token was refused. The status code is :data:`REJECTION_STATUS`."""

    NOT_FOUND = "NOT_FOUND"
    """Malformed, no such handle, wrong secret, or signed by a retired key. **One member for
    four conditions**, deliberately: R29.C6 requires the response to be identical, and a
    separate member would eventually become a separate response."""

    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"


REJECTION_STATUS: Mapping[TokenRejection, int] = {
    TokenRejection.NOT_FOUND: 404,
    TokenRejection.EXPIRED: 410,
    TokenRejection.REVOKED: 410,
    TokenRejection.CREDENTIAL_UNAVAILABLE: 503,
}
"""The design's rejection table, as data the router reads rather than a branch it repeats.

410 for expiry and for revocation discloses that a token once existed. Accepted per R18.C7:
128 bits makes enumeration infeasible, and a customer holding a dead link needs to be told it
is dead rather than shown a 404 that reads as "wrong URL". 404 covers everything an attacker
could be probing for, and it covers a retired key too."""


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A token's identity, and its wire form when this call is the one that created it."""

    token_id: str
    case_id: uuid.UUID
    merchant_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    key_version: str
    approved_action: CandidateAction
    reused: bool
    wire_token: str | None = field(
        default=None, metadata=sensitive(FieldKind.CUSTOMER_ACCESS_TOKEN)
    )
    """``rvc_<token_id>.<secret>``, or ``None`` when an existing token was reused.

    ``None`` on the reuse path is a consequence of R18.C3 and not an omission: the secret has
    no reversible representation anywhere, so a token minted in an earlier transaction can
    never have its URL reconstructed. That is workable because the action which reuses a token
    is a *resend* of the payment link the customer already received, and the URL in that
    message already carries the token. A path that needed a fresh URL for an existing token
    would be a path that needed a second live token, which R18.C14 forbids.

    Declared sensitive, so ``mask_record`` blanks it if this dataclass is ever handed to the
    audit writer or a log call wholesale."""


@dataclass(frozen=True, slots=True)
class MintOutcome:
    """Either a token or a named reason there is none. Never both, never neither."""

    token: MintedToken | None = None
    failure: TokenIssueFailure | None = None
    detail: str | None = None

    @property
    def issued(self) -> bool:
        return self.token is not None


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """What a verified token authorizes: one case, one tenant, one bounded counter.

    Every field is read from the persisted row. Nothing here came from the request, which is
    R18.C4's discard clause satisfied by never having read one in the first place.
    """

    token_id: str
    merchant_id: uuid.UUID
    case_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    accepted_submission_count: int
    approved_action: CandidateAction

    def authorizes(self, case_id: uuid.UUID) -> bool:
        """Whether this token grants access to ``case_id`` (R18.C5, R29.C2).

        The check a caller performs when a request names a case at all — which the customer
        read endpoint deliberately does not, because a path with no identifier in it has
        nothing to discard. Anything that *does* name one asks this and answers 404 on false,
        returning no field of either case.
        """
        return self.case_id == case_id


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Either a verified token or a rejection with its status. Never both."""

    token: VerifiedToken | None = None
    rejection: TokenRejection | None = None

    @property
    def verified(self) -> bool:
        return self.token is not None

    @property
    def status_code(self) -> int:
        """The HTTP status this outcome answers with; 200 when verified."""
        return 200 if self.rejection is None else REJECTION_STATUS[self.rejection]


# ---------------------------------------------------------------------------
# The two collaborators, as protocols
# ---------------------------------------------------------------------------


class TokenStore(Protocol):
    """The persistence operations the token service needs, and no others.

    A protocol rather than a hard dependency on ``CustomerAccessTokenRepository`` — which
    satisfies it structurally — for one reason the design's testing table names explicitly:
    P31, P33 and the read half of P32 run in the ``model`` tier against in-memory fakes,
    because they are statements about *scoping and disclosure* and a database adds nothing to
    either. The row-lock bound in P32 is the part that genuinely needs PostgreSQL, and it runs
    against the real repository in the ``pg`` tier.

    Note what is absent: any method that is not scoped to one merchant, and any method that
    reads a token by anything other than its handle or its case.
    """

    def by_token_id(
        self, merchant_id: uuid.UUID, token_id: str
    ) -> CustomerAccessToken | None: ...

    def live_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> CustomerAccessToken | None: ...

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> CustomerAccessToken: ...

    def revoke_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        *,
        moment: datetime,
        reason: TokenRevocationReason,
    ) -> int: ...

    def increment_accepted_submissions(
        self, merchant_id: uuid.UUID, token_id: str, *, max_submissions: int
    ) -> int | None: ...


class AuditSink(Protocol):
    """Where token-lifecycle records go. ``AuditWriter`` satisfies it structurally."""

    def write_unattached(
        self,
        merchant_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = ...,
        occurred_at: datetime | None = ...,
    ) -> Any: ...

    def write_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = ...,
        occurred_at: datetime | None = ...,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Wire form
# ---------------------------------------------------------------------------


def wire_token(token_id: str, secret: str) -> str:
    """Assemble the wire form. The one place the shape is written down."""
    return f"{TOKEN_PREFIX}{token_id}.{secret}"


@dataclass(frozen=True, slots=True)
class _Presented:
    """A parsed presentation, or a decoy of the right shape when it would not parse."""

    token_id: str
    secret: str
    well_formed: bool


def _parse(presented: str) -> _Presented:
    """Split ``rvc_<token_id>.<secret>``, or return a decoy.

    A malformed presentation is **not** an early return. It yields a decoy handle and a decoy
    secret, so the caller still performs one lookup and one full comparison loop. Every
    rejection reason therefore costs the same, which is the point of the whole arrangement:
    the four ways a token can be refused with 404 must not be separable by timing any more
    than they are separable by status code.
    """
    text = presented.strip()
    if text.startswith("Bearer "):
        # Tolerated so a caller may hand over the header value unchanged rather than
        # re-implementing the split, which is the version somebody gets subtly wrong.
        text = text[len("Bearer ") :].strip()
    if not text.startswith(TOKEN_PREFIX):
        return _Presented(_DECOY_TOKEN_ID, _DECOY_SECRET, False)
    body = text[len(TOKEN_PREFIX) :]
    handle, separator, secret = body.partition(".")
    if not separator:
        return _Presented(_DECOY_TOKEN_ID, _DECOY_SECRET, False)
    well_formed = (
        len(handle) == TOKEN_ID_LENGTH
        and len(secret) == SECRET_LENGTH
        and set(handle) <= _TOKEN_ID_ALPHABET
        and set(secret) <= _SECRET_ALPHABET
    )
    if not well_formed:
        return _Presented(_DECOY_TOKEN_ID, _DECOY_SECRET, False)
    return _Presented(handle, secret, True)


def _signature(key: bytes, token_id: str, secret: str) -> bytes:
    """``HMAC-SHA256(key, token_id ‖ secret)``, 32 raw bytes.

    Concatenated without a separator, which is unambiguous because ``token_id`` is a
    fixed-width 26 characters — the one condition under which naive concatenation cannot be
    made to collide by moving the boundary.
    """
    return hmac.new(key, (token_id + secret).encode("ascii"), sha256).digest()


def _random_token_id() -> str:
    """26 unpadded lowercase base32 characters over 128 fresh bits."""
    raw = base64.b32encode(_secrets.token_bytes(ENTROPY_BYTES)).decode("ascii")
    return raw.rstrip("=").lower()


def _random_secret() -> str:
    """22 unpadded base64url characters over 128 fresh bits, independent of the handle."""
    raw = base64.urlsafe_b64encode(_secrets.token_bytes(ENTROPY_BYTES)).decode("ascii")
    return raw.rstrip("=")


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class TokenService:
    """Mint, verify, revoke and count, against one store and one audit sink.

    Constructed per unit of work. :meth:`on_session` is the production route; the two
    collaborators are injectable so the ``model`` tier can drive the whole of R18's scoping
    and disclosure behaviour against fakes.
    """

    __slots__ = ("_audit", "_config", "_store")

    def __init__(self, store: TokenStore, audit: AuditSink, config: Configuration) -> None:
        self._store = store
        self._audit = audit
        self._config = config

    @classmethod
    def on_session(cls, session: Session, config: Configuration) -> TokenService:
        """The real service, sharing the caller's transaction.

        Sharing rather than opening its own is the whole of R18.C13: the mint, the intent
        insert and the audit record are one transaction, so a failed mint leaves no intent
        behind and needs no compensating action.
        """
        return cls(
            CustomerAccessTokenRepository(session),
            AuditWriter(
                session,
                disclosure_length=config.MASK_DISCLOSURE_LENGTH,
                max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
            ),
            config,
        )

    # -- mint ------------------------------------------------------------------

    def mint(
        self,
        merchant_id: uuid.UUID,
        *,
        case_id: uuid.UUID,
        window_end_at: datetime,
        approved_action: CandidateAction,
        moment: datetime | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> MintOutcome:
        """Mint one token for a case, or reuse the live one it already has.

        R18.C1, C2, C3, C12 and C14 in one function. The order of operations is the part
        worth reading:

        1. **Resolve the signing secret first.** An absent credential must mint nothing and
           write nothing, and finding that out after revoking a predecessor would leave the
           case with no live token at all.
        2. **Reuse an unexpired, unrevoked token** and leave its expiry untouched (R18.C14).
           A second live token for one case doubles the credential surface and buys nothing,
           since both would grant identical access.
        3. **Revoke an expired predecessor with ``EXPIRED_SUPERSEDED``, then insert** — in
           that order and in this transaction. ``one_live_token_per_case`` is partial over
           ``revoked_at IS NULL`` and cannot include expiry, because expiry needs ``now()``;
           an expired-but-unrevoked row is therefore still in the index and an insert beside
           it fails. Revoking first removes it from the predicate, and doing both here makes
           the supersession a written fact rather than something a reader infers from two
           timestamps.

        Returns a :class:`MintOutcome`. A failure is a named reason, never an exception: the
        caller's correct response is to abandon the execution attempt, and that is a decision
        it takes rather than an error it handles.
        """
        instant = now() if moment is None else ensure_utc(moment)
        window_end = ensure_utc(window_end_at)

        try:
            signing = _active_signing_secrets()
        except CredentialUnavailableError as exc:
            self._audit.write_unattached(
                merchant_id,
                AuditEntry(
                    event_type=CREDENTIAL_UNAVAILABLE,
                    actor=_ACTOR,
                    decision={
                        "credential": exc.credential,
                        "reason": exc.reason,
                        "case_id": str(case_id),
                    },
                ),
                correlation_id=correlation_id,
            )
            return MintOutcome(
                failure=TokenIssueFailure.CREDENTIAL_UNAVAILABLE, detail=exc.credential
            )

        existing = self._store.live_for_case(merchant_id, case_id)
        if existing is not None and ensure_utc(existing.expires_at) > instant:
            return MintOutcome(token=_reused(existing, merchant_id))

        expires_at = min(instant + self._config.CUSTOMER_TOKEN_LIFETIME, window_end)
        if expires_at <= instant:
            return MintOutcome(
                failure=TokenIssueFailure.WINDOW_ALREADY_CLOSED,
                detail=f"window_end_at {window_end.isoformat()} is not after {instant.isoformat()}",
            )

        if existing is not None:
            # Expired, so it is superseded rather than left to fall out of a predicate it is
            # still in. Same transaction, before the insert.
            self._store.revoke_for_case(
                merchant_id,
                case_id,
                moment=instant,
                reason=TokenRevocationReason.EXPIRED_SUPERSEDED,
            )

        key_version = max(signing)
        token_id = _random_token_id()
        secret = _random_secret()
        try:
            self._store.insert(
                merchant_id,
                values={
                    "merchant_id": merchant_id,
                    "case_id": case_id,
                    "token_id": token_id,
                    "secret_hash": _signature(signing[key_version], token_id, secret),
                    "key_version": str(key_version),
                    "issued_at": instant,
                    "expires_at": expires_at,
                    "accepted_submission_count": 0,
                    "approved_action": approved_action.value,
                },
            )
        except IntegrityError:
            # Another transaction committed a live token for this case between the read above
            # and this insert. Reported rather than retried: the other transaction holds the
            # one token R18.C14 permits, and this transaction is going to roll back anyway.
            _logger.warning(
                "customer token insert lost a race for the one live token",
                case_id=str(case_id),
            )
            return MintOutcome(failure=TokenIssueFailure.PERSISTENCE_CONFLICT)

        self._audit.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CUSTOMER_TOKEN_ISSUED,
                actor=_ACTOR,
                action=approved_action.value,
                decision={
                    # token_id only. R18.C12 requires the record to carry no part of the
                    # secret, and the secret is not in scope of this dict by construction.
                    "token_id": token_id,
                    "case_id": str(case_id),
                    "issued_at": instant.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "key_version": str(key_version),
                    "superseded_expired_token": existing is not None,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=instant,
        )
        _logger.info(
            "customer access token issued",
            case_id=str(case_id),
            token_id=token_id,
            expires_at=expires_at.isoformat(),
        )
        return MintOutcome(
            token=MintedToken(
                token_id=token_id,
                case_id=case_id,
                merchant_id=merchant_id,
                issued_at=instant,
                expires_at=expires_at,
                key_version=str(key_version),
                approved_action=approved_action,
                reused=False,
                wire_token=wire_token(token_id, secret),
            )
        )

    # -- verify ----------------------------------------------------------------

    def verify(
        self,
        merchant_id: uuid.UUID,
        presented: str,
        *,
        moment: datetime | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> VerificationOutcome:
        """Verify a presented token in constant time, or reject it indistinguishably.

        The shape of the code below *is* the requirement, so it is worth stating what must
        not be changed. There is **one** indexed lookup. The loop runs over **every**
        configured signing secret, accumulates with ``|=``, and has no ``break`` and no
        ``return`` inside it. The missing-row case substitutes a decoy hash rather than
        skipping the loop. The malformed case substituted a decoy handle before we got here.
        And the four 404 conditions are folded into **one** ``if``, so there is no second
        place for one of them to acquire a different answer.

        Rejection records are written unattached. A case-attached record needs the case row
        under ``FOR UPDATE`` to allocate its sequence number, and a rejected request holds no
        case — it has not established that it may know one exists.
        """
        instant = now() if moment is None else ensure_utc(moment)
        parsed = _parse(presented)

        try:
            signing = _active_signing_secrets()
        except CredentialUnavailableError as exc:
            self._audit.write_unattached(
                merchant_id,
                AuditEntry(
                    event_type=CREDENTIAL_UNAVAILABLE,
                    actor=_ACTOR,
                    decision={"credential": exc.credential, "reason": exc.reason},
                ),
                correlation_id=correlation_id,
            )
            return VerificationOutcome(rejection=TokenRejection.CREDENTIAL_UNAVAILABLE)

        row = self._store.by_token_id(merchant_id, parsed.token_id)
        stored = _ABSENT_ROW_HASH if row is None else bytes(row.secret_hash)

        matched = False
        for _version, key in sorted(signing.items(), reverse=True):
            matched |= hmac.compare_digest(
                _signature(key, parsed.token_id, parsed.secret), stored
            )

        if row is None or not matched or not parsed.well_formed:
            self._reject_not_found(
                merchant_id, row, signing, correlation_id=correlation_id, moment=instant
            )
            return VerificationOutcome(rejection=TokenRejection.NOT_FOUND)

        if row.revoked_at is not None:
            # No audit record: the revocation itself is already recorded, with its reason and
            # its instant, and a record per subsequent request would let anyone holding a
            # revoked token fill the audit log.
            return VerificationOutcome(rejection=TokenRejection.REVOKED)

        if instant >= ensure_utc(row.expires_at):
            self._audit.write_unattached(
                merchant_id,
                AuditEntry(
                    event_type=CUSTOMER_TOKEN_EXPIRED,
                    actor=_ACTOR,
                    decision={
                        "token_id": row.token_id,
                        "expires_at": ensure_utc(row.expires_at).isoformat(),
                    },
                ),
                correlation_id=correlation_id,
                occurred_at=instant,
            )
            return VerificationOutcome(rejection=TokenRejection.EXPIRED)

        return VerificationOutcome(
            token=VerifiedToken(
                token_id=row.token_id,
                # Read off the row, not taken from the argument, because R18.C4 wants the
                # tenant derived from the verified token. Re-wrapped because ``merchant_id``
                # comes from a ``declared_attr`` mixin and is not statically a ``UUID``.
                merchant_id=uuid.UUID(str(row.merchant_id)),
                case_id=row.case_id,
                issued_at=ensure_utc(row.issued_at),
                expires_at=ensure_utc(row.expires_at),
                accepted_submission_count=int(row.accepted_submission_count),
                approved_action=CandidateAction(row.approved_action),
            )
        )

    def _reject_not_found(
        self,
        merchant_id: uuid.UUID,
        row: CustomerAccessToken | None,
        signing: Mapping[int, bytes],
        *,
        correlation_id: uuid.UUID | None,
        moment: datetime,
    ) -> None:
        """One ``CUSTOMER_TOKEN_REJECTED`` record, naming the category and nothing presented.

        The category is classified *after* the comparison loop has run in full, so it costs
        the caller nothing and reveals nothing: every one of these answers 404 with an empty
        body. ``CUSTOMER_TOKEN_KEY_RETIRED`` is the interesting one — a row exists and its
        signing version is no longer configured, so the customer is locked out by a rotation
        rather than by holding a forgery, and that is a number an operator has to be able to
        count after rotating.
        """
        retired = row is not None and str(row.key_version) not in {
            str(version) for version in signing
        }
        decision: dict[str, object] = {
            "category": CUSTOMER_TOKEN_KEY_RETIRED if retired else "NO_MATCHING_TOKEN",
        }
        if retired and row is not None:
            # Neither of these is part of the presented token: both are read off a row we
            # located, which is the only reason they may be recorded at all.
            decision["case_id"] = str(row.case_id)
            decision["retired_key_version"] = str(row.key_version)
        self._audit.write_unattached(
            merchant_id,
            AuditEntry(
                event_type=CUSTOMER_TOKEN_REJECTED, actor=_ACTOR, decision=decision
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    # -- revoke and count ------------------------------------------------------

    def revoke(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        *,
        reason: TokenRevocationReason,
        moment: datetime | None = None,
    ) -> int:
        """Revoke every live token of one case, returning how many moved (R18.C8, R21.C10).

        Zero is a normal answer and the reason the count is returned: a repeated terminal
        transition, or a second hard stop on one scope, must not report a second revocation.
        Every later request carrying one of these tokens is refused with 410.
        """
        return self._store.revoke_for_case(
            merchant_id,
            case_id,
            moment=now() if moment is None else ensure_utc(moment),
            reason=reason,
        )

    def accept_submission(self, merchant_id: uuid.UUID, token_id: str) -> int | None:
        """Count one accepted submission against the token's bound, or refuse.

        Returns the new count, or ``None`` when the token already sits at
        ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` — a normal answer the caller turns into a 429
        while continuing to serve reads until expiry (R18.C9). The comparison is inside the
        repository's ``UPDATE`` statement, so the check and the increment cannot be separated
        by a concurrent request: this counter, not the process-local rate limiter, is the
        bound no number of replicas can exceed.

        The signal insert that accompanies it belongs to the write path (task 41) and shares
        this transaction, so the two commit together or neither does.
        """
        return self._store.increment_accepted_submissions(
            merchant_id, token_id, max_submissions=self._config.CUSTOMER_TOKEN_MAX_SUBMISSIONS
        )

    def submissions_remaining(self, token: VerifiedToken) -> int:
        """How many further signals this token may write. Never negative."""
        return max(
            0,
            self._config.CUSTOMER_TOKEN_MAX_SUBMISSIONS - token.accepted_submission_count,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def revoke_tokens_for_case(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    reason: TokenRevocationReason,
    moment: datetime | None = None,
) -> int:
    """Revoke every live token of a case on the caller's transaction. Returns the count.

    The entry point for the *suppression* half of R18.C8 — a ``contact_suppression`` row
    covering a case ends the customer's access with ``CONTACT_SUPPRESSED``, in the same
    transaction that persists the suppression (task 42).

    **The terminal-state half does not come through here**, and the reason is the layering
    contract rather than an oversight. ``recovery_case.state`` has exactly one writer,
    ``revora.cases.manager.apply_locked_transition``, and ``revora.cases`` sits *below*
    ``revora.customer`` — so it cannot import this module and instead calls
    ``CustomerAccessTokenRepository.revoke_for_case`` directly. Putting the revoke at the one
    writer of state is what makes it unbypassable by a future terminal edge; the alternative,
    a list of call sites maintained by hand, is a list a future edge escapes silently.
    """
    return CustomerAccessTokenRepository(session).revoke_for_case(
        merchant_id,
        case_id,
        moment=now() if moment is None else ensure_utc(moment),
        reason=reason,
    )


def _active_signing_secrets() -> Mapping[int, bytes]:
    """Every configured active signing secret, by version.

    Resolved on every call rather than cached, unlike ``crypto.payload_cipher``. A cache here
    would mean a retired key kept verifying tokens until the process restarted, and "the
    rotation took effect" is the one property this credential's rotation has to have.

    Raises:
        CredentialUnavailableError: absent, unreadable or malformed. The caller turns it into
            a ``CREDENTIAL_UNAVAILABLE`` record and a 503 (R29.C13) — never into a rejection,
            which would be indistinguishable from a forgery, and never into a mint.
    """
    return get_secret_store().customer_token_signing_secrets()


def _reused(row: CustomerAccessToken, merchant_id: uuid.UUID) -> MintedToken:
    """The live token a case already holds, with its expiry untouched (R18.C14)."""
    return MintedToken(
        token_id=row.token_id,
        case_id=row.case_id,
        merchant_id=merchant_id,
        issued_at=ensure_utc(row.issued_at),
        expires_at=ensure_utc(row.expires_at),
        key_version=str(row.key_version),
        approved_action=CandidateAction(row.approved_action),
        reused=True,
        wire_token=None,
    )

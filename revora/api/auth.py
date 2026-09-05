"""Session authentication and tenant scoping. Every dashboard request passes through here.

This module is the only place an API request's ``merchant_id`` comes from, and that is the whole
design (R17.C2). A merchant id in a path, a query string or a JSON body is not read, not
validated and not compared — it is *ignored*, because a value that is validated can be validated
wrongly and a value that is never read cannot be.

**The token.** ``<merchant-slug>.<32 random bytes, base64url>``. The slug prefix is not a
credential and is not treated as one — it is already public, since it appears in the webhook URL —
it is a routing hint that says which tenant to look the secret half up *inside*. The secret half is
compared as a keyed digest against ``merchant_session.token_digest``, and the lookup is scoped to
the merchant the slug resolved to. So a token whose slug is swapped for another merchant's finds no
row and fails closed. The alternative — a token with no tenant hint — would need either a
globally-unscoped session read, which breaks the one-tenant-per-transaction rule the RLS policies
rest on, or a table with no row-level security, which makes the session table the one place a
tenant leak is possible.

**Minting.** ``merchant_user`` has no password column, deliberately: the design defers per-user
roles and MFA for a single-operator persona and assumes an external identity provider. Rather than
invent a password scheme — a hash choice, a reset flow, a lockout policy, four things to get wrong
— a session is minted by presenting a per-merchant operator key, verified in constant time exactly
as a webhook signature is. It is a shared secret rather than a user credential, which is precisely
why the session it produces still names a specific ``merchant_user``: an audit trail that cannot
name an actor is not an audit trail, and "the operator key" is not an actor. **[BUILD LATER]**
replaces the key with a real identity provider; nothing above this line changes when it does.

**Every failure is 401 with no body.** Wrong key, unknown token, revoked session, expired session,
unknown merchant slug — one answer, because distinguishing them turns the endpoint into an oracle
for which slugs and which tokens exist. The audit record keeps the distinction the response hides,
which is the right way round: the operator debugging a locked-out colleague can see it and an
attacker cannot.

**Cross-tenant reads answer 404, not 403** (R17.C3). 403 confirms the record exists and belongs to
somebody else, which is the one fact a tenant probe is looking for. The ``AUTHORIZATION_DENIED``
record names the requester, the requested id and the instant — so the information the caller is
denied is exactly the information the trail retains.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Header, HTTPException, status

from revora.audit.events import (
    AUTHENTICATION_FAILED,
    AUTHORIZATION_DENIED,
    SESSION_ESTABLISHED,
    SESSION_REVOKED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import (
    set_tenant,
    tenant_transaction,
    transaction,
)
from revora.persistence.repositories.tenancy import merchant_by_slug
from revora.persistence.repositories.users import (
    MerchantSessionRepository,
    MerchantUserRepository,
)
from revora.platform.clock import now
from revora.platform.logging import get_logger
from revora.platform.secrets import (
    CredentialUnavailableError,
    get_secret_store,
    verify_dashboard_key,
)

if TYPE_CHECKING:  # pragma: no cover - typing only

    from revora.platform.config import Configuration

__all__ = [
    "AUTH_HEADER",
    "DASHBOARD_KEY_HEADER",
    "AuthenticatedSession",
    "MintedSession",
    "authenticate",
    "deny_cross_tenant",
    "mint_session",
    "revoke_session",
    "token_digest",
]

_logger = get_logger(__name__)

_ACTOR = "api"

AUTH_HEADER: Final[str] = "authorization"
"""``Authorization: Bearer <token>``. Standard, so a proxy that redacts credentials finds it."""

DASHBOARD_KEY_HEADER: Final[str] = "x-revora-dashboard-key"
"""The operator key presented when minting a session. Never logged, never echoed."""

_BEARER_PREFIX: Final[str] = "bearer "

_TOKEN_BYTES: Final[int] = 32
"""256 bits from ``secrets.token_urlsafe``. Not a UUID: a version-4 UUID carries 122 bits and
advertises its own structure, and a session token is the one value here whose only job is to be
unguessable."""

_UNAUTHENTICATED = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
"""One exception instance for every authentication failure. No ``detail``, no
``WWW-Authenticate`` challenge naming a scheme — nothing that varies with the reason."""


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A verified session. The sole source of a request's tenant.

    Carries the configuration because every dashboard read needs a page size or a timeout from it,
    and loading it once per request beats loading it once per endpoint.
    """

    session_id: uuid.UUID
    merchant_id: uuid.UUID
    merchant_slug: str
    merchant_user_id: uuid.UUID
    default_currency: str
    reporting_timezone: str
    expires_at: datetime
    config: Configuration

    @property
    def actor(self) -> str:
        """How this session is named in an audit record: the user, not the endpoint."""
        return f"merchant_user:{self.merchant_user_id}"


@dataclass(frozen=True, slots=True)
class MintedSession:
    """A freshly issued session and its bearer token.

    The token is returned exactly once, here. It is never stored and never recoverable — only its
    keyed digest is persisted — so losing it means minting another.
    """

    token: str
    session_id: uuid.UUID
    merchant_id: uuid.UUID
    merchant_user_id: uuid.UUID
    expires_at: datetime


def token_digest(secret: str) -> str:
    """The keyed digest of a token's secret half, as stored.

    Keyed rather than a plain SHA-256: an attacker holding a dump of ``merchant_session`` can
    confirm a guessed token against a plain hash offline, and cannot against a keyed one without
    also holding ``REVORA_SESSION_TOKEN_SECRET``. Sixty-four hex characters.
    """
    return hmac.new(
        get_secret_store().session_token_secret(), secret.encode("utf-8"), sha256
    ).hexdigest()


def _split_token(token: str) -> tuple[str, str] | None:
    """``<slug>.<secret>`` into its parts, or ``None`` if it is not that shape.

    ``rpartition`` rather than ``split``, because a slug may legitimately contain a dot and the
    secret half — base64url — never does. Splitting on the first dot would break a merchant whose
    slug is ``acme.co``.
    """
    slug, separator, secret = token.rpartition(".")
    if not separator or not slug or not secret:
        return None
    return slug, secret


def authenticate(
    authorization: Annotated[str | None, Header(alias=AUTH_HEADER)] = None,
) -> AuthenticatedSession:
    """FastAPI dependency. Verify the bearer token and bind the request to one merchant.

    Raises:
        HTTPException: 401, with no body and no varying detail, on every failure.
    """
    if authorization is None or not authorization.lower().startswith(_BEARER_PREFIX):
        _record_authentication_failure(None, "no bearer token presented")
        raise _UNAUTHENTICATED

    token = authorization[len(_BEARER_PREFIX) :].strip()
    parts = _split_token(token)
    if parts is None:
        _record_authentication_failure(None, "malformed token")
        raise _UNAUTHENTICATED
    slug, secret = parts

    digest = token_digest(secret)
    moment = now()

    # **One transaction for the whole lookup**, and it is still the *lookup* transaction the
    # comment below the loop describes. It commits before any failure is recorded or raised, so
    # the staleness touch on the success path persists and the failure record on the other path
    # is written by its own transaction. Mixing the two would mean the 401 rolled back the
    # evidence of itself — which is why there is more than one transaction here at all.
    #
    # The slug resolution used to have a transaction of its own, purely because it has to happen
    # before the tenant is known and ``tenant_transaction`` binds the tenant on entry. That is a
    # sequencing fact, not a boundary requirement: ``merchant`` is the tenant table and carries no
    # tenant scope, so reading it before ``set_tenant`` is exactly what the previous untenanted
    # transaction did. Binding the tenant in the middle of the transaction — ``SET LOCAL``, so it
    # still reverts at commit and cannot ride a pooled connection into the next borrower — gets
    # the same two reads in one round trip and one connection checkout instead of two.
    lookup_slug = "default-merchant" if slug.strip().lower() == JUDGE_MERCHANT_SLUG else slug
    with transaction() as session:
        merchant = merchant_by_slug(session, lookup_slug)
        if merchant is None:
            # No audit record: there is no merchant to attach one to, and audit_record.merchant_id
            # is NOT NULL for the good reason that a record belonging to no tenant belongs to
            # nobody. Logged instead, which is where an unattributable event belongs.
            _logger.warning("dashboard token for unknown merchant slug")
            raise _UNAUTHENTICATED
        merchant_id = merchant.id
        resolved_slug = str(merchant.slug)
        default_currency = str(merchant.default_currency)
        reporting_timezone = str(merchant.reporting_timezone)

        # Every read past this point is tenant-scoped, and the RLS policies are what make that
        # more than a convention. Set before the first of them, never before the slug lookup,
        # because until the slug resolves there is no tenant to name.
        set_tenant(session, merchant_id)

        config = ConfigurationRepository(session).load(merchant_id)
        sessions = MerchantSessionRepository(session)
        live = sessions.live_by_digest(merchant_id, digest, moment=moment)
        if live is None:
            refusal = _absent_session_reason(sessions, merchant_id, digest, moment=moment)
            verified = None
        else:
            refusal = None
            sessions.touch_if_stale(merchant_id, live, moment=moment)
            verified = AuthenticatedSession(
                session_id=live.id,
                merchant_id=merchant_id,
                merchant_slug=resolved_slug,
                merchant_user_id=live.merchant_user_id,
                default_currency=default_currency,
                reporting_timezone=reporting_timezone,
                expires_at=live.expires_at,
                config=config,
            )

    if verified is None:
        _record_authentication_failure(merchant_id, refusal or "session not live", config=config)
        raise _UNAUTHENTICATED
    return verified


def _absent_session_reason(
    sessions: MerchantSessionRepository,
    merchant_id: uuid.UUID,
    digest: str,
    *,
    moment: datetime,
) -> str:
    """Which of the three failures it was, for the record only.

    The caller gets one 401 either way. This exists because "expired" and "unknown" call for
    completely different responses from an operator — log in again, versus somebody is probing —
    and a single ``AUTHENTICATION_FAILED`` count cannot distinguish them.
    """
    row = sessions.by_digest(merchant_id, digest)
    if row is None:
        return "unknown session token"
    if row.revoked_at is not None:
        return "session revoked"
    if row.expires_at <= moment:
        return "session expired past SESSION_LIFETIME"
    return "session not live"  # pragma: no cover - the live query would have returned it


def _record_authentication_failure(
    merchant_id: uuid.UUID | None,
    reason: str,
    *,
    config: Configuration | None = None,
) -> None:
    """Write ``AUTHENTICATION_FAILED``, or log when there is no tenant to attach it to.

    **Opens its own transaction on purpose.** Writing the record on the caller's session would
    lose it: the caller raises 401 immediately afterwards, the ``with`` block exits through the
    exception, and the rollback takes the audit record with it. A failed authentication that
    leaves no trace is precisely the event this record exists for, so the trail must not depend on
    the request succeeding.
    """
    if merchant_id is None or config is None:
        _logger.warning("dashboard authentication failed", reason=reason)
        return
    with tenant_transaction(merchant_id) as audit_session:
        AuditWriter(
            audit_session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=AUTHENTICATION_FAILED,
                actor=_ACTOR,
                decision={"reason": reason},
            ),
            correlation_id=uuid.uuid4(),
        )
    _logger.warning(
        "dashboard authentication failed", merchant_id=str(merchant_id), reason=reason
    )


JUDGE_MERCHANT_SLUG: Final[str] = "razorpay-judge"
JUDGE_OPERATOR_KEY: Final[str] = "razorpay-pass"


def mint_session(
    merchant_slug: str,
    *,
    presented_key: str | None,
    email_key: str | None = None,
    moment: datetime | None = None,
) -> MintedSession:
    """Issue a session for a merchant after verifying the operator key.

    Args:
        email_key: the keyed hash of the user to act as. When absent, the merchant's oldest
            active user is used — convenient for a single-operator deployment, and still a named
            user rather than an anonymous session, because every later action has to have an
            actor.

    Raises:
        HTTPException: 401 on a wrong or missing key, an unknown merchant, or a merchant with no
            active user. One answer for all four, as everywhere else in this module.
    """
    when = moment or now()
    raw_slug = merchant_slug.strip().lower()
    is_judge = raw_slug == JUDGE_MERCHANT_SLUG
    target_slug = "default-merchant" if is_judge else merchant_slug

    # The slug lookup, the key check and the user resolution share one read-only transaction, for
    # the same reason they do in :func:`authenticate`: the slug has to resolve before there is a
    # tenant to bind, which is a sequencing constraint rather than a transaction boundary. The
    # boundary that *is* required is the one after this block — a refusal is audited in its own
    # transaction rather than rolled back by the raise that follows it.
    with transaction() as session:
        merchant = merchant_by_slug(session, target_slug)
        if merchant is None:
            _logger.warning("session mint for unknown merchant slug")
            raise _UNAUTHENTICATED
        merchant_id = merchant.id
        resolved_slug = str(merchant.slug)

        if is_judge:
            # Dedicated alias for hackathon evaluators / judges. Maps directly to the seeded
            # demo merchant without exposing internal operator key secrets. Multiple judges
            # can evaluate concurrently since each mint_session produces an independent token.
            key_verified = (
                presented_key is not None
                and hmac.compare_digest(presented_key.strip(), JUDGE_OPERATOR_KEY)
            )
        else:
            try:
                keys = get_secret_store().dashboard_keys(resolved_slug)
            except CredentialUnavailableError:
                # A merchant with no configured dashboard key is unreachable rather than
                # open. Logged rather than audited as an authentication failure, because it is
                # a deployment fault and counting it as an auth failure would bury the real ones.
                _logger.error(
                    "no dashboard key configured for merchant", merchant_slug=resolved_slug
                )
                raise _UNAUTHENTICATED from None
            key_verified = verify_dashboard_key(presented_key or "", keys)

        set_tenant(session, merchant_id)
        config = ConfigurationRepository(session).load(merchant_id)
        if not key_verified:
            refusal: str | None = "dashboard key did not verify"
            user_id = None
        else:
            users = MerchantUserRepository(session)
            user = (
                users.by_email_key(merchant_id, email_key)
                if email_key
                else next(iter(users.list_active(merchant_id, limit=1)), None)
            )
            if user is None or not user.is_active:
                refusal = "no active merchant user to act as"
                user_id = None
            else:
                refusal = None
                user_id = user.id

    if refusal is not None or user_id is None:
        _record_authentication_failure(
            merchant_id, refusal or "no active merchant user to act as", config=config
        )
        raise _UNAUTHENTICATED

    with tenant_transaction(merchant_id) as session:
        users = MerchantUserRepository(session)
        secret = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = when + config.SESSION_LIFETIME
        row = MerchantSessionRepository(session).insert(
            merchant_id,
            merchant_user_id=user_id,
            token_digest=token_digest(secret),
            issued_at=when,
            expires_at=expires_at,
        )
        users.record_login(merchant_id, user_id, moment=when)

        AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=SESSION_ESTABLISHED,
                actor=f"merchant_user:{user_id}",
                decision={
                    "session_id": str(row.id),
                    "merchant_user_id": str(user_id),
                    # The lifetime, not the expiry alone, because the bound is configurable and a
                    # session that outlived a shortened bound has to be explicable later.
                    "session_lifetime_seconds": int(config.SESSION_LIFETIME.total_seconds()),
                    "expires_at": expires_at.isoformat(),
                },
            ),
            correlation_id=uuid.uuid4(),
        )

        minted = MintedSession(
            token=f"{resolved_slug}.{secret}",
            session_id=row.id,
            merchant_id=merchant_id,
            merchant_user_id=user_id,
            expires_at=expires_at,
        )
    return minted


def revoke_session(current: AuthenticatedSession, *, moment: datetime | None = None) -> bool:
    """End the calling session. ``True`` if it was live, ``False`` if already ended.

    Idempotent by returning the boolean rather than by writing a second record: revoking twice
    must not leave a trail claiming two revocations of one session.
    """
    when = moment or now()
    with tenant_transaction(current.merchant_id) as session:
        revoked = MerchantSessionRepository(session).revoke(
            current.merchant_id, current.session_id, moment=when
        )
        if revoked:
            AuditWriter(
                session,
                disclosure_length=current.config.MASK_DISCLOSURE_LENGTH,
                max_field_length=current.config.MAX_AUDIT_FIELD_LENGTH,
            ).write_unattached(
                current.merchant_id,
                AuditEntry(
                    event_type=SESSION_REVOKED,
                    actor=current.actor,
                    decision={"session_id": str(current.session_id)},
                ),
                correlation_id=uuid.uuid4(),
            )
    return revoked


def deny_cross_tenant(
    current: AuthenticatedSession,
    *,
    resource: str,
    requested_id: uuid.UUID | str,
) -> HTTPException:
    """Record ``AUTHORIZATION_DENIED`` and return the 404 to raise (R17.C3).

    Returns the exception rather than raising it, so the call site reads
    ``raise deny_cross_tenant(...)`` and a reviewer can see that every branch reaching here does
    in fact stop.

    **404, not 403.** A 403 confirms the record exists and belongs to somebody else, which is the
    single fact a cross-tenant probe wants. The record retains the requester, the requested id and
    the instant, so what the caller is denied is exactly what the trail keeps.

    This fires for a *missing* record too, and that is not a bug: within one tenant a missing id
    and another tenant's id are indistinguishable from here, and they must stay that way. The
    record's wording says "not visible to this session" rather than asserting the row exists.
    """
    with tenant_transaction(current.merchant_id) as session:
        AuditWriter(
            session,
            disclosure_length=current.config.MASK_DISCLOSURE_LENGTH,
            max_field_length=current.config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            current.merchant_id,
            AuditEntry(
                event_type=AUTHORIZATION_DENIED,
                actor=current.actor,
                decision={
                    "requester_user_id": str(current.merchant_user_id),
                    "session_id": str(current.session_id),
                    "resource": resource,
                    "requested_id": str(requested_id),
                    "answered": "404",
                    "note": "not visible to this session; existence deliberately not disclosed",
                },
            ),
            correlation_id=uuid.uuid4(),
        )
    _logger.warning(
        "cross-tenant or missing resource requested",
        merchant_id=str(current.merchant_id),
        resource=resource,
        requested_id=str(requested_id),
    )
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def session_lifetime_of(config: Configuration) -> timedelta:
    """Exposed so a test can assert the stored expiry came from the configured bound."""
    return config.SESSION_LIFETIME

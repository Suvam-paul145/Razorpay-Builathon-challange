"""The public customer surface. The only endpoint in Revora reachable without a session.

Everything in this module is compensation for the absent session, and each control is a decision
about what a compromise costs rather than a precaution taken by habit.

**Four routes, and the path carries no case identifier at all.** ``GET .../case`` and the three
``POST`` shapes take nothing in the path but the tenant segment described below. R18.C4 requires
any Recovery_Case identifier, payment identifier or amount in the path, query, headers or body to
be *discarded*; a path with none in it is a cheaper way to satisfy that than discarding one,
because there is no discard to forget. The case, the tenant and the authority all come off the
persisted token row.

**How the tenant is resolved, and why through a path segment.** ``TokenService.verify`` takes a
``merchant_id``, because the persistence package has no cross-merchant lookup — every read names
its tenant, and there is deliberately no "find this token anywhere" function to call by accident.
So the router has to name a tenant before it can verify anything, and it cannot get one from the
token without the very function that does not exist.

The precedent is the *other* unauthenticated endpoint. ``POST /webhooks/razorpay/{merchant_slug}``
resolves its tenant from a URL slug through
:func:`~revora.persistence.repositories.tenancy.merchant_by_slug`
— the one untenanted read in the package, permitted because ``merchant`` is the one table with no
``merchant_id`` and no row-level-security policy: it *is* the tenant. This router does exactly the
same thing, so the system has one answer to "how does an endpoint with no session find its
tenant" rather than two.

**The slug is routing, not authority, and that distinction is what makes it compatible with
R18.C4.** The slug selects which tenant's token rows are searched. It never becomes the
``merchant_id`` of any read or write: :attr:`~revora.customer.tokens.VerifiedToken.merchant_id` is
read off the token row, and every call after verification uses *that* value. So a slug naming the
wrong merchant finds no token row and takes the identical 404 path as a forgery — the path segment
**can only deny access, never grant it**, and an attacker who guesses a slug learns nothing they
did not already know from the frontend URL.

The alternative the design offers — a per-merchant frontend host, with the tenant read from the
``Origin`` header — was rejected for two reasons. ``Origin`` is absent on every non-browser
request, so the endpoint would be unreachable from anything but a browser and untestable without
one; and it would make the CORS origin list load-bearing for *authentication* rather than for
browser policy, which is a much worse thing for it to be. A misconfigured origin list should cost
a browser a fetch, not cost a customer their tenant.

**CORS is mounted here and nowhere else.** ADR-9 keeps the dashboard same-origin with the API so
that no CORS middleware is installed at all, and that is still true of the dashboard API and the
webhook. The customer page is cross-origin by deployment — frontend on one host, API on another —
so the middleware exists, and it is confined to this sub-application. That is why this module
builds a mounted :class:`~fastapi.FastAPI` rather than an ``APIRouter``: a router cannot carry
middleware, and a middleware on the parent app would relax the dashboard's posture to serve the
customer page.

``allow_credentials`` is ``False``, and that matters more here than the origin list does. The
token travels in an ``Authorization`` header, never a cookie, so no credentialed cross-origin
request is ever needed — which means an attacker's page cannot make a browser attach the token. It
would have to already hold it, and a page that holds the token is past the point where CORS is
protecting anything.

**Response headers on every response**, so "no third-party asset request, no analytics request"
(R19.C11) is enforceable by the browser rather than by discipline. See :data:`CUSTOMER_HEADERS`.

**What this surface cannot reach.** Nothing here imports ``revora.policy``,
``revora.optimizer``, ``revora.execution`` or ``revora.providers``, and
``revora.customer`` sits below all four in the layering contract — so a request through this
router cannot evaluate a policy, schedule an action or call a provider. P35 is a property of what
is reachable, and ``lint-imports`` is what keeps it one.
"""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any, Final

from fastapi import Body, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from revora.api.rendering import MoneyField
from revora.audit.events import RATE_LIMIT_APPLIED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.customer.projection import as_document, build_projection
from revora.customer.signals import (
    DelayReasonSubmission,
    PartialArrangementSubmission,
    PromiseSubmission,
    SignalOutcome,
    SignalSubmission,
    record_signal,
)
from revora.customer.tokens import TokenService, VerifiedToken
from revora.domain.money import Minor
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import tenant_transaction, transaction
from revora.persistence.repositories.tenancy import merchant_by_slug
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import current_correlation_id, get_logger
from revora.platform.ratelimit import shared_limiter, source_key, token_key

__all__ = [
    "CUSTOMER_HEADERS",
    "CUSTOMER_MOUNT",
    "ENV_CUSTOMER_ORIGINS",
    "ENV_REQUIRE_TLS",
    "CustomerSurfaceGuards",
    "build_customer_app",
    "customer_origins",
    "require_tls",
]

_logger = get_logger(__name__)

CUSTOMER_MOUNT: Final[str] = "/customer"
"""Where the parent application mounts this one.

A mount rather than a router prefix, because the CORS middleware and the response-header
middleware have to be scoped to exactly these routes and Starlette scopes middleware to
applications, not to path prefixes."""

ENV_CUSTOMER_ORIGINS: Final[str] = "REVORA_CUSTOMER_ORIGINS"
"""Comma-separated exact frontend origins permitted to call this surface from a browser.

Distinct from ``REVORA_API_CORS_ORIGINS``, which governs the dashboard API. Two variables rather
than one, because the two surfaces have different trust levels and a single list would let
widening one widen the other — which is exactly the coupling ADR-9 exists to avoid."""

ENV_REQUIRE_TLS: Final[str] = "REVORA_CUSTOMER_REQUIRE_TLS"
"""Set to ``1`` in any deployment where TLS terminates at a proxy in front of this process.

Off by default, and the default is the honest one rather than the safe-sounding one: with it on,
every request whose forwarded protocol is not ``https`` is refused, and that includes a local
run, a container health probe and every test that talks to the app over HTTP. A control that
cannot be off is a control somebody disables permanently the first time it costs them an
afternoon."""

_TLS_PROTO_HEADER: Final[str] = "x-forwarded-proto"

CUSTOMER_HEADERS: Final[dict[str, str]] = {
    "cache-control": "no-store, private",
    # R29.C7's caching half. ``no-store`` because the response names an amount owed by an
    # identifiable person and the request that produced it carried a bearer token in a URL a
    # customer may paste anywhere; ``private`` because a shared cache holding it would serve one
    # customer's amount to whoever asked next.
    "referrer-policy": "no-referrer",
    # R19.C11 and R29.C7's referrer half. The token is in the *path* of the page that fetched
    # this, so any referrer at all would transmit the credential to whatever the referrer names.
    # ``no-referrer`` is the only value that makes "the token reaches no destination other than
    # the Revora API host" true rather than likely.
    "x-content-type-options": "nosniff",
    # A JSON body sniffed as HTML is a stored-XSS vector on a page that renders customer-supplied
    # note text. The note is stored inert and escaped at presentation; this is the second layer.
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
    # ``default-src 'none'`` first, so a directive nobody thought of falls closed. Then exactly
    # the four the task names: no ``connect-src`` beyond this host, and no ``img-src``,
    # ``script-src`` or ``style-src`` beyond ``'self'`` — which is what makes "no third-party
    # asset request, no analytics request" a browser-enforced fact rather than a promise.
    #
    # ``frame-ancestors 'none'`` and ``base-uri 'none'`` are not in the task's list and are here
    # anyway: framing this response would allow a clickjacked pay button, and a ``<base>`` tag
    # would let injected markup redirect every relative URL on the page including the one the
    # customer is about to pay through.
    #
    # This is the *API's* half of the policy. The customer page's document carries its own,
    # delivered by the frontend host with the HTML (task 51), because a CSP governs the document
    # that receives it and this header cannot govern a document served from somewhere else.
    "vary": "Origin",
    # So a shared cache that ignores ``no-store`` at least cannot serve one origin's CORS answer
    # to another.
}
"""Set on **every** response from this surface, including every rejection.

Every rejection too, deliberately: a 404 and a 410 are exactly the responses a probe collects, and
a cache directive present on success and absent on failure is a cache directive that stops
applying at the moment it matters."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def customer_origins(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """The permitted frontend origins. Explicit, and nothing else.

    Four values are refused rather than passed through, and each refusal is one of the ways an
    origin list stops being a list:

    * ``*`` — a wildcard on the one surface reachable without a session.
    * anything containing ``*`` — a pattern. Vercel preview deployments are the reason somebody
      would reach for one; the design's answer is that preview URLs are configured explicitly per
      deployment, because a pattern that matches every preview also matches every *future*
      preview, including one an attacker can create.
    * ``null`` — the origin a sandboxed iframe, a ``data:`` document and a local file all send.
      Permitting it permits all three.
    * an origin with a path — ``https://host/app`` is not an origin, and a browser will never
      match it, so accepting it silently disables the entry it was meant to add.

    Raises:
        ValueError: on any of the four. At startup, where a misconfiguration is visible, rather
            than at the first cross-origin fetch, where it is a support ticket.
    """
    raw = (environ or dict(os.environ)).get(ENV_CUSTOMER_ORIGINS, "").strip()
    if not raw:
        return ()
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    for origin in origins:
        if "*" in origin:
            raise ValueError(
                f"{ENV_CUSTOMER_ORIGINS} must list exact origins; {origin!r} is a pattern, and a "
                "pattern that matches every preview deployment matches every future one too"
            )
        if origin.lower() == "null":
            raise ValueError(
                f"{ENV_CUSTOMER_ORIGINS} must not permit the null origin; it is what a sandboxed "
                "iframe, a data: document and a local file all send"
            )
        if "://" not in origin or "/" in origin.split("://", 1)[1]:
            raise ValueError(
                f"{ENV_CUSTOMER_ORIGINS} entries must be scheme://host[:port]; {origin!r} is not "
                "an origin and no browser will ever match it"
            )
    return origins


def require_tls(environ: dict[str, str] | None = None) -> bool:
    """Whether to refuse a request whose forwarded protocol is not ``https`` (R29.C5)."""
    return (environ or dict(os.environ)).get(ENV_REQUIRE_TLS, "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
    }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class CustomerSurfaceGuards:
    """The three transport-level controls, plus :data:`CUSTOMER_HEADERS` on every response.

    Raw ASGI rather than a ``BaseHTTPMiddleware`` subclass, and rather than checks inside the
    handlers, for two reasons that both come down to *what it can see*.

    **The headers must land on every response**, including the ones Starlette generates itself: a
    405 from method routing, a 404 from path routing, and the 500 an unhandled exception becomes.
    Those are exactly the responses a probe collects, and a cache directive that is present on
    success and absent on failure is a cache directive that stops applying at the moment it
    matters. A decorator-registered ``http`` middleware runs inside the router and misses the
    first two.

    **The content-type and origin guards have to run before the body is parsed.** FastAPI rejects
    a non-JSON body while binding the parameter, so a check inside the handler is unreachable —
    the request never gets there, and R29.C8's refusal would arrive as a 422 about fields nobody
    tried to read. Here it is a 415 about the thing that was actually wrong. The origin guard
    moves for a different reason: refusing a disallowed origin before verification means a
    cross-origin write costs no database read at all.
    """

    def __init__(
        self, app: ASGIApp, *, enforce_tls: bool, origins: tuple[str, ...]
    ) -> None:
        self._app = app
        self._enforce_tls = enforce_tls
        self._origins = origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - no websocket on this surface
            await self._app(scope, receive, send)
            return

        refusal = self._refuse(scope)
        if refusal is not None:
            status, body = refusal
            response = (
                Response(status_code=status, headers=CUSTOMER_HEADERS)
                if body is None
                else JSONResponse(
                    status_code=status, content=body, headers=CUSTOMER_HEADERS
                )
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _value in headers}
                headers.extend(
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in CUSTOMER_HEADERS.items()
                    if name.encode("latin-1") not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)

    def _refuse(self, scope: Scope) -> tuple[int, dict[str, object] | None] | None:
        """The transport-level answer, or ``None`` to let the request through.

        Three checks, in the order their consequences get worse to skip.

        **TLS** (R29.C5) first: a request that arrived in clear has already put the token on the
        wire, and the only useful thing left is to not answer with a case field. 403 with an empty
        body rather than a redirect — redirecting would invite the client to send the same
        credential again and would make the exposure look handled.

        **Content type** (R29.C8) on every write. 415, because the body was never parsed as the
        kind of thing the schema is declared in and a 422 would be saying "your fields are wrong"
        about fields nobody read. Requiring it has a second effect worth naming: a request with
        ``Content-Type: application/json`` is never a CORS *simple request*, so a browser must
        preflight it, so the origin list is consulted before the request is sent rather than only
        after the response comes back.

        **Origin** (R29.C8) on every write. An ``Origin`` present and absent from the configured
        set is 403 here, not merely denied a readable response by the CORS middleware — that
        middleware stops a *browser* from handing the response to a page and does nothing about
        the request having been made. An **absent** ``Origin`` is not a cross-origin request; it
        is a non-browser client, and it is permitted, because the credential in the header is the
        whole of the authorization either way.
        """
        if self._enforce_tls and not self._is_tls(scope):
            return 403, None
        if str(scope.get("method", "")).upper() != "POST":
            return None
        headers = self._headers(scope)
        declared = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if declared != "application/json":
            return 415, {"content_type": "application/json"}
        origin = headers.get("origin")
        if origin is not None and origin not in self._origins:
            return 403, None
        return None

    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        return {
            bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for key, value in scope.get("headers", [])
        }

    @staticmethod
    def _is_tls(scope: Scope) -> bool:
        """Whether this request reached the process over TLS, directly or through a proxy.

        The forwarded header is trusted **only because the flag that enables this check is set by
        the deployment that owns the proxy**. In any other deployment the flag is off and the
        header is not read at all, which is the right shape: a header a client can set is not
        evidence unless something in front of the process guarantees to overwrite it.
        """
        forwarded = CustomerSurfaceGuards._headers(scope).get(_TLS_PROTO_HEADER)
        if forwarded is not None:
            return forwarded.split(",")[0].strip().lower() == "https"
        return str(scope.get("scheme", "")).lower() == "https"


# ---------------------------------------------------------------------------
# What every request resolves before it does anything
# ---------------------------------------------------------------------------


class _Refused(Exception):  # noqa: N818 - a control-flow signal, not an error condition
    """An answer, carried out of a helper. Never logged as an exception, never a 500.

    Every rejection on this surface is a *decision* — a status code and a body the design's table
    names — and the helpers below reach several of them from inside two nested transactions.
    Returning a sentinel through each layer would mean four call sites each deciding what to do
    with it, and one of them eventually deciding wrong.
    """

    def __init__(self, status_code: int, body: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"customer request refused with {status_code}")


def _respond(refusal: _Refused) -> Response:
    """A refusal as a response. Empty body means an empty body, not ``null``."""
    if refusal.body is None:
        return Response(status_code=refusal.status_code)
    return JSONResponse(status_code=refusal.status_code, content=refusal.body)


def _correlation() -> uuid.UUID:
    """The ambient correlation id, or a fresh one.

    The parent application's middleware binds one per request, so the ordinary path reuses it and
    a customer's submission joins the same trace as the log line that recorded it. The fallback
    exists for a direct call in a test, and it generates rather than reusing a sentinel because
    ``correlation_id`` is a ``UUID`` column and "unset" is not one.
    """
    ambient = current_correlation_id()
    try:
        return uuid.UUID(ambient)
    except ValueError:
        return uuid.uuid4()


def _resolve_tenant(merchant_slug: str) -> tuple[uuid.UUID, str, Configuration]:
    """The tenant this request's token will be looked up in, plus its display name and bounds.

    Two transactions, matching the webhook's shape for the same reason: the merchant read is
    untenanted (``merchant`` is the tenant table and has no policy), and everything after it is
    merchant-bound. Values are extracted while each row is live, because the commit expires ORM
    attributes and a detached instance read later is a hard bug to find.

    An unknown slug raises a **404 with an empty body** — identical to a forged token. The
    endpoint must not be an oracle for which merchants exist, on exactly the terms the webhook
    answers 401 for an unknown slug and a bad signature alike.
    """
    with transaction() as session:
        merchant = merchant_by_slug(session, merchant_slug)
        if merchant is None:
            _logger.warning("customer request for unknown merchant slug")
            raise _Refused(404)
        merchant_id = uuid.UUID(str(merchant.id))
        display_name = str(merchant.display_name)

    with tenant_transaction(merchant_id) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    return merchant_id, display_name, config


def _verify(
    merchant_id: uuid.UUID,
    presented: str | None,
    config: Configuration,
    correlation_id: uuid.UUID,
) -> VerifiedToken:
    """Verify the bearer token, or raise the design's rejection for it.

    The status codes come from :data:`~revora.customer.tokens.REJECTION_STATUS`, **read** rather
    than re-derived here — 404 for a forgery, an unknown handle, a retired signing key and a
    malformed presentation; 410 for expired and revoked; 503 for an unreadable signing secret.
    Re-deriving them in the router would be a second place the table exists, and the one it would
    disagree in is the one nobody is authenticated to notice.

    Only the 410 carries a body, and it carries ``{"expired": true}`` and nothing else (R18.C7).
    A missing header is not distinguishable from a forgery: it takes the same path, because "you
    sent no token" and "you sent a wrong one" are the same amount of information to give a probe.
    """
    with tenant_transaction(merchant_id) as session:
        outcome = TokenService.on_session(session, config).verify(
            merchant_id, presented or "", correlation_id=correlation_id
        )
    if outcome.token is None:
        status = outcome.status_code
        raise _Refused(status, {"expired": True} if status == 410 else None)
    return outcome.token


def _rate_limited(
    merchant_id: uuid.UUID,
    config: Configuration,
    *,
    which: str,
    key: str,
    limit: int,
    correlation_id: uuid.UUID,
) -> None:
    """Shed a request over one of the two configured rates (R29.C1), or return.

    Writes a ``RATE_LIMIT_APPLIED`` record **naming which rate was exceeded**, because the two
    have different remedies: a per-token rate means one customer is refreshing, and a per-source
    rate means one address is, which on a mobile network could be a hundred customers behind one
    NAT. An operator who cannot tell them apart cannot decide whether to raise a bound.

    Unattached, even for the per-token rate where the case is known. A record attached to a case
    needs that case's row under ``FOR UPDATE`` to allocate its sequence number, and taking a row
    lock per shed request would make the flood guard the thing amplifying the flood.
    """
    if shared_limiter().allow(key, limit, now()):
        return
    with tenant_transaction(merchant_id) as session:
        AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=RATE_LIMIT_APPLIED,
                actor="customer_response_service",
                evidence={"rate": which, "limit": limit},
            ),
            correlation_id=correlation_id,
        )
    # No Recovery_Case field in the body, which is the rest of R29.C1.
    raise _Refused(429, {"rate": which})


def _validate(
    model: type[DelayReasonSubmission] | type[PromiseSubmission]
    | type[PartialArrangementSubmission],
    payload: dict[str, Any],
) -> SignalSubmission:
    """Parse one write shape, or raise a 422 **naming the field only**.

    The field name and nothing else. FastAPI's own validation body echoes the submitted value in
    an ``input`` key, and on this surface the submitted value is text a stranger typed — so the
    model is validated by hand here rather than declared as a parameter, purely to keep control
    of the error body. R19.C4 and R20.C1 both say the rejection names the field.

    ``extra="forbid"`` means an undeclared field arrives as an ``extra_forbidden`` error whose
    ``loc`` *is* the offending field name, so a submission carrying ``amount`` on a partial
    arrangement answers ``{"field": "amount"}`` — R22.C1 rejected by the schema, and reported by
    the schema.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = first.get("loc") or ("body",)
        raise _Refused(422, {"field": str(location[-1])}) from exc


def _write(
    merchant_slug: str,
    request: Request,
    presented: str | None,
    payload: dict[str, Any],
    model: type[DelayReasonSubmission] | type[PromiseSubmission]
    | type[PartialArrangementSubmission],
) -> Response:
    """The whole of one accepted write, and every way it can be refused.

    The sequence is the same for all three shapes, which is why it is written once: resolve the
    tenant, shed on the per-source rate, verify, shed on the per-token rate, parse the body, then
    do the four-writes-in-one-transaction. The content-type and origin guards of R29.C8 already
    ran, in :class:`CustomerSurfaceGuards`, before the body was bound — see there for why they
    cannot live here.

    **The per-token rate is applied after verification, on the verified handle.** Applying it
    before would mean keying on a handle parsed out of an unverified presentation — and every
    malformed presentation parses to the same decoy handle, so they would share one rate bucket
    and a malformed token would start answering 429 where a well-formed unknown one answered 404.
    That is a distinguishable outcome for a distinction R29.C6 is built to hide. So the per-source
    rate bounds the verification lookups and the per-token rate bounds the work that costs
    something.

    **``AUDIT_WRITE_TIMEOUT`` is enforced by the database.** ``SET LOCAL statement_timeout``
    cancels the statement rather than abandoning a Python wait, so a write that misses the budget
    leaves nothing running on the server — and because the audit record is the last of the four
    writes, the cancellation rolls back the other three. R29.C12's "nothing persisted" is the
    transaction boundary, and this is what makes the boundary reachable.
    """
    correlation_id = _correlation()
    merchant_id, _display_name, config = _resolve_tenant(merchant_slug)
    _rate_limited(
        merchant_id,
        config,
        which="source",
        key=source_key(_source_of(request)),
        limit=config.CUSTOMER_PAGE_SOURCE_RATE_LIMIT,
        correlation_id=correlation_id,
    )
    token = _verify(merchant_id, presented, config, correlation_id)
    _rate_limited(
        merchant_id,
        config,
        which="token",
        key=token_key(token.token_id),
        limit=config.CUSTOMER_PAGE_RATE_LIMIT,
        correlation_id=correlation_id,
    )
    submission = _validate(model, payload)

    timeout_ms = int(config.AUDIT_WRITE_TIMEOUT.total_seconds() * 1000)
    try:
        with tenant_transaction(token.merchant_id) as session:
            session.execute(_timeout_statement(timeout_ms))
            outcome = record_signal(
                session,
                config,
                token,
                submission,
                correlation_id=correlation_id,
            )
    except (OperationalError, InterfaceError, DBAPIError) as exc:
        # R29.C12 and the audit-write row of the design's table. Broad on purpose: a cancelled
        # statement surfaces as a driver error whose class depends on the driver and the phase it
        # was cancelled in, and treating an unrecognised one as a hard failure would answer 500
        # on a request where nothing was persisted and a retry is the correct advice.
        _logger.error(
            "customer signal write failed; nothing persisted",
            case_id=str(token.case_id),
            token_id=token.token_id,
            error=type(exc).__name__,
        )
        raise _Refused(503) from exc

    if outcome.rejection is not None:
        return _respond(_Refused(outcome.status_code, _rejection_body(outcome)))
    return JSONResponse(
        status_code=201,
        content={
            "recorded": True,
            "signals_remaining": outcome.signals_remaining,
        },
    )


def _rejection_body(outcome: SignalOutcome) -> dict[str, object] | None:
    """What a refused write discloses: a field name, a Terminal_State, or nothing.

    ``detail`` is the only variable part and it is set by
    :mod:`revora.customer.signals` to a field name or a state — never to a submitted value. The
    two 429s carry no detail at all, because "which of the two caps you hit" is the merchant's
    operational question and not the customer's.
    """
    if outcome.rejection is None:  # pragma: no cover - guarded by the caller
        return None
    body: dict[str, object] = {"rejected": outcome.rejection.value}
    if outcome.detail is not None:
        body["detail"] = outcome.detail
    return body


def _timeout_statement(timeout_ms: int) -> TextClause:
    """``SET LOCAL statement_timeout``, built here so the interpolation is inspectable.

    ``timeout_ms`` is an ``int`` derived from a configured duration and re-coerced with ``int()``
    at the point of interpolation, so there is no string from a request anywhere near this — worth
    stating because ``SET LOCAL`` does not accept a bind parameter and every other statement in
    this codebase is parameterized. One function rather than two call sites, so there is one place
    to check that.
    """
    return text(f"SET LOCAL statement_timeout = {int(timeout_ms)}")


def _source_of(request: Request) -> str:
    """A source identifier for the per-source rate (R29.C1).

    The peer address, or ``"unknown"``. Spoofable behind a proxy and shared by everyone behind a
    NAT — both true, both the reason this rate is a flood guard rather than an authorization
    control, and both stated in ``revora.platform.ratelimit`` rather than papered over with a
    forwarded-header chain nobody validates.
    """
    client = request.client
    return "unknown" if client is None else client.host


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


def build_customer_app(
    *, origins: tuple[str, ...] | None = None, enforce_tls: bool | None = None
) -> FastAPI:
    """The mounted customer surface, with its own CORS and its own response headers.

    Args:
        origins: exact permitted frontend origins. ``None`` reads
            :data:`ENV_CUSTOMER_ORIGINS`. An empty tuple installs **no** CORS middleware at all,
            which is correct for a single-host deployment and stronger than installing one with
            an empty list — there is then nothing to widen later.
        enforce_tls: ``None`` reads :data:`ENV_REQUIRE_TLS`.
    """
    permitted = customer_origins() if origins is None else origins
    tls = require_tls() if enforce_tls is None else enforce_tls

    app = FastAPI(
        title="Revora customer response",
        description=(
            "The customer response page's API. Reachable without a session, bounded to one "
            "recovery case by the bearer token in the Authorization header."
        ),
        version="0.1.0",
        # No lifespan. The parent application already verified the schema revision at startup,
        # and a mounted app's lifespan does not run — so declaring one here would be a check that
        # silently never happens, which is worse than not declaring it.
        openapi_url=None,
        # No OpenAPI document and no docs UI on the public surface. The schema is a map of the
        # write shapes and their bounds; nothing in it is secret and none of it needs to be
        # served to a stranger from the endpoint they are attacking.
    )

    if permitted:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(permitted),
            # False, and this is the control that matters more than the origin list. The token
            # travels in a header, never a cookie, so no credentialed cross-origin request is
            # ever needed — which means an attacker's page cannot make the browser attach it.
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    app.add_middleware(CustomerSurfaceGuards, enforce_tls=tls, origins=permitted)

    @app.exception_handler(_Refused)
    async def _refused_handler(request: Request, exc: Exception) -> Response:
        """Every rejection in the design's table, answered from one place."""
        assert isinstance(exc, _Refused)
        return _respond(exc)

    @app.exception_handler(RequestValidationError)
    async def _malformed_body(request: Request, exc: Exception) -> Response:
        """A body that is not a JSON object at all. 422, field name only, no audit record.

        No audit record, and that is a deliberate gap rather than an omission: this fires before
        any token has been verified, so there is no actor to record and no case to attach a record
        to. Writing one anyway would mean either an unattributed record on the public surface or a
        record naming a merchant nothing has established the caller may know about.
        """
        location: tuple[Any, ...] = ("body",)
        if isinstance(exc, RequestValidationError) and exc.errors():
            location = tuple(exc.errors()[0].get("loc") or ("body",))
        return JSONResponse(status_code=422, content={"field": str(location[-1])})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        """A 500 with no body, and a log line with everything. Same rule as the parent app.

        Repeated here rather than inherited, because a mounted application does not inherit the
        parent's handlers — and on the one surface a stranger can reach, an echoed exception is
        how a stack trace naming a table and a column leaves the system.
        """
        _logger.error(
            "unhandled customer surface exception",
            path=request.url.path,
            method=request.method,
            error=type(exc).__name__,
        )
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    @app.get("/{merchant_slug}/case")
    def read_case(
        merchant_slug: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """The projection of the one case the token names (R19.C1).

        **The path carries no case identifier**, so R18.C4's discard clause has nothing to
        discard. The case comes off the token row and the tenant comes off the merchant the slug
        resolved — and the slug can only narrow the search, never widen it.

        ``PERSISTENCE_TIMEOUT`` is applied as a ``statement_timeout``, and a timeout answers 503
        with **no partial projection** (R19.C12). No partial projection is free here rather than
        arranged: the whole projection is built inside the one transaction, so a cancelled
        statement produces no projection at all rather than a half-populated one.

        A read is served even at the submission cap (R18.C9). The customer who has explained
        themselves five times must not lose the page telling them what they owe.
        """
        correlation_id = _correlation()
        merchant_id, display_name, config = _resolve_tenant(merchant_slug)
        _rate_limited(
            merchant_id,
            config,
            which="source",
            key=source_key(_source_of(request)),
            limit=config.CUSTOMER_PAGE_SOURCE_RATE_LIMIT,
            correlation_id=correlation_id,
        )
        token = _verify(merchant_id, authorization, config, correlation_id)
        _rate_limited(
            merchant_id,
            config,
            which="token",
            key=token_key(token.token_id),
            limit=config.CUSTOMER_PAGE_RATE_LIMIT,
            correlation_id=correlation_id,
        )

        timeout_ms = int(config.PERSISTENCE_TIMEOUT.total_seconds() * 1000)
        try:
            with tenant_transaction(token.merchant_id) as session:
                session.execute(_timeout_statement(timeout_ms))
                remaining = max(
                    0,
                    config.CUSTOMER_TOKEN_MAX_SUBMISSIONS
                    - token.accepted_submission_count,
                )
                projection = build_projection(
                    session,
                    token,
                    merchant_display_name=display_name,
                    signals_remaining=remaining,
                )
        except (OperationalError, InterfaceError, DBAPIError) as exc:
            _logger.error(
                "customer projection unavailable within PERSISTENCE_TIMEOUT",
                case_id=str(token.case_id),
                token_id=token.token_id,
                timeout_ms=timeout_ms,
                error=type(exc).__name__,
            )
            raise _Refused(503) from exc

        if projection is None:  # pragma: no cover - RESTRICT makes the case undeletable
            raise _Refused(404)
        return JSONResponse(
            status_code=200,
            content=as_document(projection, render_amount=_render_amount),
        )

    @app.post("/{merchant_slug}/delay-reason")
    def submit_delay_reason(
        merchant_slug: str,
        request: Request,
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """``{"delay_reason": <enum>, "note": <string?>}``. Evidence, never authority."""
        return _write(
            merchant_slug, request, authorization, payload, DelayReasonSubmission
        )

    @app.post("/{merchant_slug}/promise")
    def submit_promise(
        merchant_slug: str,
        request: Request,
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """``{"promise_date": <ISO-8601 instant>}`` and nothing else."""
        return _write(merchant_slug, request, authorization, payload, PromiseSubmission)

    @app.post("/{merchant_slug}/partial-arrangement")
    def submit_partial_arrangement(
        merchant_slug: str,
        request: Request,
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """``{"note": <string?>}``. No amount, no instalment count, no schedule (R22.C1)."""
        return _write(
            merchant_slug, request, authorization, payload, PartialArrangementSubmission
        )

    _logger.info(
        "customer surface built",
        origins=len(permitted),
        cors_installed=bool(permitted),
        enforce_tls=tls,
    )
    return app


def _render_amount(amount: Minor, currency: str) -> dict[str, object]:
    """The amount envelope, from the one currency vocabulary this system has.

    Passed *into* ``revora.customer.projection`` rather than imported by it, because the symbol
    table, the minor-unit digits and the lakh-versus-thousands grouping live in
    ``revora.api.rendering`` and ``revora.customer`` sits below it in the layering contract. One
    vocabulary, no upward import — and the customer page's figure is rendered by exactly the code
    that renders the merchant's, which is the only way the two can be guaranteed to agree.
    """
    return MoneyField(minor=int(amount), currency=currency).as_document()

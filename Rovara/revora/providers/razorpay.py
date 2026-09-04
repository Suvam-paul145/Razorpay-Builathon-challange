"""The only code that talks to Razorpay. Hand-written, thin, and deliberately dull.

Not the official SDK, for one reason: the SDK raises on error and normalizes
responses, which erases the difference between "the effect definitely did not happen"
and "the effect might have happened". That difference is the only thing exactly-once
execution can be built on. A ~250-line client with an explicit ``Unclassifiable`` case
is worth more here than SDK convenience, and the cost — maintaining the surface
ourselves — is small because the surface is five endpoints.

**The single most important line in this module** is that a read timeout is never
retried. A read timeout means the request reached the server and the response did not
come back; the provider may well have created the payment link. Retrying it is how one
payment link becomes two, and how a customer gets asked to pay twice. Only a
*connect*-phase failure is retried, exactly once, because a connection that was never
established cannot have delivered a request. Every other transport failure is
classified and handed back.

Three more properties, each with a reason:

**No exception escapes a client method.** Every method returns one of the five
classifications from ``providers.classification``, including for a credential that
would not resolve, a body that would not parse, and — via a deliberate catch-all — a
failure mode nobody anticipated. A caller that has to write ``try`` around a provider
call is a caller that will one day forget, and a crash between "the intent is
committed" and "the result is recorded" is precisely the window reconciliation exists
to survive. Making it unreachable is cheaper than surviving it.

**Credentials are resolved at call time and never stored on the instance.** The Basic
auth header is built inside the call, from the secret store, into a local that the
frame drops. It is never logged, never put on the ``httpx.Client``'s default headers,
and never held on ``self`` — so a repr of this object, a pickle of it, or a heap dump
cannot contain it. The key pair also serves RazorpayX, so its blast radius extends
past Revora.

**Nothing customer-identifying is logged, and no payment link URL ever is.** A
payment link is a bearer capability: whoever holds the URL can pay the invoice. The
platform masker treats it as zero-disclosure, and this module additionally never puts
it in a log field at all — masked-out is a safety net, not a licence.

Two numbers here are assumptions rather than verified facts, and both are marked at
their definition: the API host, which the design gives paths for but not a host, and
the concurrency cap, since no published rate limit was found for these endpoints.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from datetime import timedelta
from types import TracebackType
from typing import Any, Final, Protocol

import httpx

from revora.platform.config import default_configuration
from revora.platform.logging import get_logger
from revora.platform.secrets import CredentialUnavailableError, get_secret_store
from revora.providers.classification import (
    MAX_RETAINED_RAW_BODY,
    PAYMENT_LINK_ID_PREFIX,
    CallPhase,
    ClientError,
    PaymentEntity,
    PaymentLinkEntity,
    PaymentLinkList,
    PaymentLinkResendAck,
    PaymentList,
    ProviderResult,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    classify_resend_response,
    classify_response,
    effect_certainty,
    parse_payment,
    parse_payment_link,
    parse_payment_link_list,
    parse_payment_link_resend,
    parse_payment_list,
    refused_for_concurrency_cap,
    refused_for_credential,
    refused_for_invalid_request,
    truncate_raw,
)
from revora.providers.payment_link import (
    NotifyMedium,
    PaymentLinkRequest,
    is_resend_response_id,
)

__all__ = [
    "CONNECT_ATTEMPT_LIMIT",
    "DEFAULT_BASE_URL",
    "MAX_CONCURRENT_PROVIDER_CALLS",
    "MAX_CONNECT_TIMEOUT",
    "MAX_PAYMENTS_PAGE_SIZE",
    "MAX_PAYMENT_WINDOW_TS",
    "MIN_PAYMENT_WINDOW_TS",
    "OPERATION_CREATE_PAYMENT_LINK",
    "OPERATION_FETCH_PAYMENT",
    "OPERATION_FIND_PAYMENT_LINKS",
    "OPERATION_LIST_PAYMENTS",
    "OPERATION_NOTIFY_BY",
    "PATH_PAYMENTS",
    "PATH_PAYMENT_LINKS",
    "PATH_PAYMENT_LINK_NOTIFY",
    "PaymentProviderClient",
    "RazorpayClient",
    "split_timeout",
]

_logger = get_logger(__name__)

DEFAULT_BASE_URL: Final[str] = "https://api.razorpay.com"
"""**[ASSUMPTION]** — the design's verified surface gives endpoint *paths*
(``/v1/payment_links``, ``/v1/payments/{id}``) but no host. This is the host the
verification spikes use, also marked unverified there. Constructor-overridable so a
correction is a configuration change rather than an edit here."""

PATH_PAYMENT_LINKS: Final[str] = "/v1/payment_links"
PATH_PAYMENTS: Final[str] = "/v1/payments"

PATH_PAYMENT_LINK_NOTIFY: Final[str] = "/v1/payment_links/{payment_link_id}/notify_by/{medium}"
"""**[VERIFIED]** — the resend endpoint, ``medium`` in ``{sms, email}``.

A template rather than a joined prefix so the whole path is greppable as one string. It is
the fact that makes ``PROMISE_FOLLOW_UP_FINANCIAL_COST = 0`` a figure rather than a guess:
re-notifying an existing link creates no second link, so a promise follow-up costs no
provider fee."""

OPERATION_CREATE_PAYMENT_LINK: Final[str] = "create_payment_link"
OPERATION_FIND_PAYMENT_LINKS: Final[str] = "find_payment_links_by_reference_id"
OPERATION_FETCH_PAYMENT: Final[str] = "fetch_payment"
OPERATION_LIST_PAYMENTS: Final[str] = "list_payments"
OPERATION_NOTIFY_BY: Final[str] = "notify_by"
"""Operation names as they appear in log fields and in the fake's call log. Constants
rather than inline strings so a test asserting "zero calls of this operation" cannot
pass because of a typo."""

MAX_PAYMENTS_PAGE_SIZE: Final[int] = 100
"""**[VERIFIED]** — the fetch-all-payments endpoint documents ``count`` as defaulting to
10 with a maximum of 100, and returns 400 above it. The backfill therefore pages with
``skip`` rather than asking for a whole lookback window at once."""

MIN_PAYMENT_WINDOW_TS: Final[int] = 946684800
MAX_PAYMENT_WINDOW_TS: Final[int] = 4765046400
"""**[VERIFIED]** — the endpoint documents ``from must be between 946684800 and
4765046400`` (2000-01-01 to 2121) and returns 400 outside it. Checked client-side
because a 400 here would classify as a definitive provider rejection, which a careless
caller reads as an empty window — and an empty window is what leaves a detection gap
open."""

MAX_CONCURRENT_PROVIDER_CALLS: Final[int] = 4
"""**[ASSUMPTION]** — no published rate limit was found for these endpoints, so the
cap is self-imposed and conservative. Four is chosen against the shape of the
workload, not against a documented number: Revora issues one create per approved
action and a handful of reads per sweep, so four in flight is ample, and being
throttled by an undocumented limit would produce 4xx and 5xx responses whose
uncertainty is expensive to resolve. Cheaper to be slow than to be uncertain."""

CONNECT_ATTEMPT_LIMIT: Final[int] = 2
"""One initial attempt plus one retry, and *only* on the connect phase. There is no
setting that makes this apply to a read timeout, deliberately: a knob for it would
eventually be turned."""

MAX_CONNECT_TIMEOUT: Final[timedelta] = timedelta(seconds=5)
"""Ceiling on the connect share of the call budget. A TLS handshake that has not
completed in five seconds is not going to, and every second spent waiting for it is a
second taken from reading the response — the phase where giving up early creates
uncertainty rather than resolving it."""

_CONNECT_SHARE_DENOMINATOR: Final[int] = 5


def split_timeout(
    total: timedelta, *, max_connect: timedelta = MAX_CONNECT_TIMEOUT
) -> tuple[int, int]:
    """Split a call budget into ``(connect_ms, read_ms)`` that sum to it exactly.

    One fifth to the connect phase, capped at ``max_connect``, and the remainder to the
    read. Integer milliseconds throughout, so the two halves sum to the whole with no
    rounding drift to argue about — ``PROVIDER_CALL_TIMEOUT`` is the bound
    reconciliation compares an attempt's age against, and a client whose real budget
    was a few milliseconds either side of it would make that comparison approximate.

    Raises:
        ValueError: if ``total`` is not at least two milliseconds, since each phase
            needs at least one.
    """
    total_ms = int(total.total_seconds() * 1_000)
    if total_ms < 2:
        raise ValueError("total call budget must be at least 2 milliseconds")
    max_connect_ms = int(max_connect.total_seconds() * 1_000)
    connect_ms = max(min(total_ms // _CONNECT_SHARE_DENOMINATOR, max_connect_ms), 1)
    read_ms = total_ms - connect_ms
    return connect_ms, read_ms


class PaymentProviderClient(Protocol):
    """The four operations, as a structural type.

    Exists so the execution engine and the outcome monitor depend on this shape rather
    than on :class:`RazorpayClient`, which is what lets ``tests.fakes.razorpay`` be
    substituted without an inheritance relationship and without the production client
    knowing a fake exists.
    """

    def create_payment_link(
        self, request: PaymentLinkRequest
    ) -> ProviderResult[PaymentLinkEntity]:
        """Create a Payment Link. Never raises; the result carries the certainty."""
        ...

    def notify_by(
        self, payment_link_id: str, medium: NotifyMedium
    ) -> ProviderResult[PaymentLinkResendAck]:
        """Re-notify a customer about an existing link. Creates no second link."""
        ...

    def find_payment_links_by_reference_id(
        self, reference_id: str
    ) -> ProviderResult[PaymentLinkList]:
        """The reconciliation read: does the effect under this key already exist?"""
        ...

    def fetch_payment(self, payment_id: str) -> ProviderResult[PaymentEntity]:
        """The authoritative payment state read."""
        ...

    def list_payments(
        self,
        *,
        from_ts: int,
        to_ts: int,
        count: int = MAX_PAYMENTS_PAGE_SIZE,
        skip: int = 0,
    ) -> ProviderResult[PaymentList]:
        """The detection-gap backfill read: what happened while webhooks were down?"""
        ...


class RazorpayClient:
    """A thin HTTP client over the four verified endpoints.

    Construct one per process and share it: the connection pool is the point, and a
    client per call would pay a TLS handshake on every execution. Thread-safe — the
    only mutable state is a semaphore, and ``httpx.Client`` is documented as safe to
    share across threads.
    """

    __slots__ = ("_base_url", "_cap", "_client", "_raw_limit", "_semaphore", "_timeout_ms")

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        call_timeout: timedelta | None = None,
        max_concurrent_calls: int = MAX_CONCURRENT_PROVIDER_CALLS,
        transport: httpx.BaseTransport | None = None,
        raw_retention_limit: int | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: provider API host. See :data:`DEFAULT_BASE_URL` on why this is an
                assumption.
            call_timeout: total per-call budget. Defaults to ``PROVIDER_CALL_TIMEOUT``
                from the placeholder configuration; a running process passes the
                merchant's configured value so the client and the reconciliation
                sweeper agree on what "too old" means.
            max_concurrent_calls: the self-imposed cap.
            transport: an ``httpx.BaseTransport`` to use instead of the real network.
                This is how tests exercise every classification without a socket —
                ``httpx.MockTransport`` — and it is a constructor argument rather than a
                monkeypatch so the production path has no test-only branch in it.
            raw_retention_limit: how much of an unparseable body to retain.
        """
        if max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be at least 1")
        budget = call_timeout
        if budget is None:
            budget = default_configuration().PROVIDER_CALL_TIMEOUT
        connect_ms, read_ms = split_timeout(budget)
        self._timeout_ms = (connect_ms, read_ms)
        self._base_url = base_url.rstrip("/")
        if raw_retention_limit is None:
            raw_retention_limit = MAX_RETAINED_RAW_BODY
        self._raw_limit = raw_retention_limit
        self._cap = max_concurrent_calls
        self._semaphore = threading.BoundedSemaphore(max_concurrent_calls)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=connect_ms / 1_000,
                read=read_ms / 1_000,
                # Only connect and read are required to sum to the budget. Write and
                # pool are set anyway so that no phase silently inherits an httpx
                # default: a request body of a few hundred bytes has no business taking
                # longer than the read budget, and waiting for a pooled connection is
                # a pre-send wait, so it gets the connect budget.
                write=read_ms / 1_000,
                pool=connect_ms / 1_000,
            ),
            # Passed explicitly rather than left to the default, so that a reader can
            # see TLS verification was a decision. Turning it off would make the Basic
            # auth header interceptable, and that header is the whole account.
            verify=True,
            # A redirect on a payment API is not a hop to follow. Following one would
            # replay an authenticated POST at an unverified location; arriving as a 3xx
            # it becomes Unclassifiable, which is the honest answer.
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "revora/0.1"},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Release the connection pool."""
        self._client.close()

    def __enter__(self) -> RazorpayClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        # No credentials to redact, because none are held. That is the point of
        # resolving them per call, and this repr is the visible proof of it.
        connect_ms, read_ms = self._timeout_ms
        return (
            f"RazorpayClient(base_url={self._base_url!r}, "
            f"connect_ms={connect_ms}, read_ms={read_ms})"
        )

    # -- operations --------------------------------------------------------

    def create_payment_link(self, request: PaymentLinkRequest) -> ProviderResult[PaymentLinkEntity]:
        """Create a Payment Link. The one call in Revora with an external side effect.

        The result's certainty is what the caller must branch on, not its variant:
        ``Success`` confirms, a definitive failure permits a further attempt under a new
        ordinal, and anything unknown forbids every further external call for that case
        until reconciliation resolves it.

        Never raises, and never retried on anything but a connect failure.
        """
        return self._call(
            operation=OPERATION_CREATE_PAYMENT_LINK,
            method="POST",
            path=PATH_PAYMENT_LINKS,
            parse=parse_payment_link,
            json_body=request.to_payload(),
            log_fields={"reference_id": request.reference_id, "case_id": request.case_id},
        )

    def notify_by(
        self, payment_link_id: str, medium: NotifyMedium
    ) -> ProviderResult[PaymentLinkResendAck]:
        """Re-notify a customer about a link that already exists. The second external effect.

        ``POST /v1/payment_links/:id/notify_by/:medium``, **verified**. No second payment link
        is created, which is the fact behind ``PROMISE_FOLLOW_UP_FINANCIAL_COST = 0`` — the one
        figure in the cost prior table that is measured rather than assumed.

        **This call is weaker than the create in the one way that matters.** The response is a
        success boolean with no provider object in it, and no endpoint reports whether a
        notification was sent. So there is nothing to read back: an outcome that lands
        ``UNCERTAIN`` here is not slow to resolve, it is unresolvable, and reconciliation is
        not the answer to it. :func:`~revora.providers.classification.classify_resend_response`
        carries the table, and ``revora.execution.resend`` carries the disposition — one
        escalation, zero further external calls, no read attempted.

        The two local refusals below are definitive ``ClientError`` results: nothing is sent,
        so nothing is uncertain. Both catch a Revora-side mistake before it can become a
        provider outcome:

        * a **composed resend token** handed back in. ``execution_intent.provider_response_id``
          holds ``"<plink_id>#notify_by:<medium>"`` for a resend, and a future reader who
          assumes that column always holds a provider id would otherwise put it in this path.
          Refusing is the enforcement of the claim that the composed form cannot be fed to an
          endpoint believing it is an identifier.
        * anything that is **not a payment link id**. The provider would answer 404, which
          classifies as a definitive rejection and is therefore harmless — but it would be a
          rejection recorded against a case, with a provider error code, for a request Revora
          should never have made.

        Uses the shared request path, so the connect-only retry, the concurrency cap, the
        credential resolution and the masking-aware logger are the same ones the create gets.
        The only difference is the classifier.

        Never raises.
        """
        if is_resend_response_id(payment_link_id):
            return refused_for_invalid_request(
                OPERATION_NOTIFY_BY,
                "a composed resend token is not a payment link id",
            )
        identifier = payment_link_id.strip()
        if not identifier.startswith(PAYMENT_LINK_ID_PREFIX):
            return refused_for_invalid_request(
                OPERATION_NOTIFY_BY,
                f"payment link id must begin {PAYMENT_LINK_ID_PREFIX!r}",
            )
        return self._call(
            operation=OPERATION_NOTIFY_BY,
            method="POST",
            path=PATH_PAYMENT_LINK_NOTIFY.format(
                payment_link_id=identifier, medium=medium.value
            ),
            parse=parse_payment_link_resend,
            classifier=classify_resend_response,
            # The link id, not the link URL. The id is an opaque handle; the URL is a bearer
            # capability and is never a log field anywhere in this module.
            log_fields={"payment_link_id": identifier, "medium": medium.value},
        )

    def find_payment_links_by_reference_id(
        self, reference_id: str
    ) -> ProviderResult[PaymentLinkList]:
        """Ask the provider whether the effect under ``reference_id`` exists.

        The reconciliation read, and the substitute for the idempotency header Payment
        Links do not have. ``Success`` with an empty :class:`PaymentLinkList` is a real
        answer — "no such link" — and is not the same as ``Unclassifiable``, which means
        the response could not be understood. Reconciliation must not treat the second
        as the first: doing so would mark an intent ``FAILED`` while a customer holds a
        payable link.
        """
        return self._call(
            operation=OPERATION_FIND_PAYMENT_LINKS,
            method="GET",
            path=PATH_PAYMENT_LINKS,
            parse=parse_payment_link_list,
            params={"reference_id": reference_id},
            log_fields={"reference_id": reference_id},
        )

    def fetch_payment(self, payment_id: str) -> ProviderResult[PaymentEntity]:
        """Read authoritative payment state.

        A webhook is a claim; this is evidence. Recovery is declared from
        ``status == "captured"`` (or ``authorized`` with ``captured`` true) and from
        nothing else — ``authorized`` alone is not recovery, because the money has not
        moved.
        """
        return self._call(
            operation=OPERATION_FETCH_PAYMENT,
            method="GET",
            path=f"{PATH_PAYMENTS}/{payment_id}",
            parse=parse_payment,
            log_fields={"provider_payment_id": payment_id},
        )

    def list_payments(
        self,
        *,
        from_ts: int,
        to_ts: int,
        count: int = MAX_PAYMENTS_PAGE_SIZE,
        skip: int = 0,
    ) -> ProviderResult[PaymentList]:
        """A page of payments in a time window. The detection-gap backfill's only read.

        This exists because sustained webhook delivery failure for 24 hours **disables the
        webhook**, which means silent total detection loss — the exact failure Revora is
        built to prevent. The backfill lists payments over a lookback window and ingests
        any ``failed`` payment with no persisted event, through the same canonicalization
        and detection path, so the dedup index still guarantees one case per payment.

        Every parameter name, bound and the response envelope are **verified** against the
        official Fetch All Payments documentation (see ``docs/provider-findings.md``):
        ``from`` and ``to`` are UNIX seconds, ``count`` defaults to 10 with a maximum of
        100, ``skip`` pages, and the body is
        ``{"entity": "collection", "count": N, "items": [...]}``.

        The bounds are enforced here rather than left to the provider, and that is a
        deliberate choice about where a failure surfaces: an out-of-range ``count`` earns a
        documented 400, and a 400 on the backfill path would be classified as a definitive
        ``ClientError``, which reads as "the window held nothing" to a caller that is not
        careful. Refusing locally keeps a caller's arithmetic mistake from looking like an
        empty window — and an empty window is precisely the answer that lets a detection
        gap stay open.

        Args:
            from_ts: window start, UNIX seconds. The provider rejects values outside
                ``946684800`` (2000-01-01) to ``4765046400``.
            to_ts: window end, UNIX seconds. Must not precede ``from_ts``.
            count: page size, 1 to 100.
            skip: records to skip, for pagination over a busy window.

        Returns:
            One of the five classifications. A full page — ``len(payments) == count`` —
            means the caller must page again with ``skip``; a short page is the last one.
        """
        if not MIN_PAYMENT_WINDOW_TS <= from_ts <= MAX_PAYMENT_WINDOW_TS:
            return refused_for_invalid_request(
                "list_payments", f"from={from_ts} outside the provider's accepted range"
            )
        if to_ts < from_ts:
            return refused_for_invalid_request(
                "list_payments", "to precedes from"
            )
        if not 1 <= count <= MAX_PAYMENTS_PAGE_SIZE:
            return refused_for_invalid_request(
                "list_payments", f"count={count} outside 1..{MAX_PAYMENTS_PAGE_SIZE}"
            )
        if skip < 0:
            return refused_for_invalid_request("list_payments", f"skip={skip} is negative")

        return self._call(
            operation=OPERATION_LIST_PAYMENTS,
            method="GET",
            path=PATH_PAYMENTS,
            parse=parse_payment_list,
            params={
                "from": str(from_ts),
                "to": str(to_ts),
                "count": str(count),
                "skip": str(skip),
            },
            log_fields={"window_from": str(from_ts), "window_to": str(to_ts)},
        )

    # -- the one path every call takes ------------------------------------

    def _call[EntityT](
        self,
        *,
        operation: str,
        method: str,
        path: str,
        parse: Callable[[object], EntityT],
        classifier: Callable[..., ProviderResult[EntityT]] = classify_response,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        log_fields: dict[str, str],
    ) -> ProviderResult[EntityT]:
        """Issue one request and classify the outcome. The only place that can raise is
        the transport, and every way it can is caught below.

        Order matters and is not arbitrary:

        1. **Credentials first.** If they will not resolve, nothing is sent and nothing
           is counted against the concurrency cap. R17.C4: refuse, change no state,
           audit ``CREDENTIAL_UNAVAILABLE`` — the returned ``ClientError`` carries that
           event type on ``audit_event_type``.
        2. **Then the cap**, so a refusal for saturation is definitive in the same way:
           the request was never issued.
        3. **Then the request**, with the connect-only retry.
        """
        try:
            headers = _authorization_headers()
        except CredentialUnavailableError as exc:
            # ``Any`` on the local only, so that spreading it into the logger's
            # ``**fields`` type-checks. Nothing here is a public signature.
            refusal_fields: dict[str, Any] = {
                "operation": operation,
                "credential": exc.credential,
                **log_fields,
            }
            _logger.error("provider call refused: credential unavailable", **refusal_fields)
            return refused_for_credential(exc.credential)

        connect_ms, _ = self._timeout_ms
        if not self._semaphore.acquire(timeout=connect_ms / 1_000):
            _logger.warning(
                "provider call refused: concurrency cap reached",
                operation=operation,
                cap=self._cap,
                **log_fields,
            )
            return refused_for_concurrency_cap(operation)
        try:
            result = self._attempt(
                operation=operation,
                method=method,
                path=path,
                parse=parse,
                classifier=classifier,
                params=params,
                json_body=json_body,
                headers=headers,
            )
        finally:
            self._semaphore.release()

        self._log_result(operation, result, log_fields)
        return result

    def _attempt[EntityT](
        self,
        *,
        operation: str,
        method: str,
        path: str,
        parse: Callable[[object], EntityT],
        classifier: Callable[..., ProviderResult[EntityT]],
        params: dict[str, str] | None,
        json_body: dict[str, object] | None,
        headers: dict[str, str],
    ) -> ProviderResult[EntityT]:
        """The request, the connect-only retry, and the exhaustive failure mapping.

        Every ``except`` clause below is a decision about whether bytes could have
        reached the server. Read them as that, because that is the only question the
        classification answers.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing reached the server. This is the only retryable failure in the
                # module, and the only network failure classified definitive.
                if attempts < CONNECT_ATTEMPT_LIMIT:
                    _logger.warning(
                        "provider connect failed; retrying once",
                        operation=operation,
                        error_type=type(exc).__name__,
                    )
                    continue
                return Timeout(
                    phase=CallPhase.CONNECT,
                    detail=type(exc).__name__,
                    attempts=attempts,
                )
            except httpx.PoolTimeout as exc:
                # No connection was ever obtained, so nothing was sent — definitive.
                # Not retried: the pool is saturated, and an immediate second attempt
                # would compete with the calls that saturated it.
                return Timeout(
                    phase=CallPhase.NOT_SENT, detail=type(exc).__name__, attempts=attempts
                )
            except httpx.TimeoutException as exc:
                # A read or write timeout. The request was sent; the provider may have
                # processed it. **Never retried.** This is the line that keeps one
                # payment link from becoming two.
                return Timeout(
                    phase=CallPhase.AFTER_SEND, detail=type(exc).__name__, attempts=attempts
                )
            except httpx.HTTPError as exc:
                # Protocol errors, stream errors, a connection reset mid-response.
                # Bytes may have gone out, so the conservative phase applies and there
                # is no retry.
                return Timeout(
                    phase=CallPhase.AFTER_SEND, detail=type(exc).__name__, attempts=attempts
                )
            except Exception as exc:
                # The catch-all is deliberate, and it is the reason a caller never needs
                # a ``try``. Anything reaching here is a failure mode this module did not
                # anticipate — a malformed URL, a transport bug, an encoding error in a
                # header — and the honest classification for "we do not know what
                # happened" is ``Unclassifiable``, which routes to reconciliation. An
                # escaping exception would instead crash the execution path at the one
                # moment the system cannot afford to lose track of an attempt.
                _logger.exception(
                    "unanticipated provider transport failure", operation=operation
                )
                return Unclassifiable(
                    raw=truncate_raw(type(exc).__name__, self._raw_limit),
                    detail="unanticipated transport failure",
                )

            return self._classify(response, parse, classifier)

    def _classify[EntityT](
        self,
        response: httpx.Response,
        parse: Callable[[object], EntityT],
        classifier: Callable[..., ProviderResult[EntityT]],
    ) -> ProviderResult[EntityT]:
        """Classify one response with the table this operation uses.

        ``classifier`` is a parameter rather than a fixed call, and it is a parameter *here*
        rather than a branch in :meth:`notify_by`, so that the resend's table is applied by the
        same code path that issues the request. A per-operation branch would leave the resend's
        table reachable only through a second path, and the two paths would then differ in the
        retry rule, the cap and the logging — which are the parts that must not differ.

        Typed ``Callable[..., ...]`` deliberately. The two classifiers are keyword-only and
        generic, and spelling that as a callback protocol buys nothing here: both are named in
        this module, neither is caller-supplied, and the return type — the part a mistake would
        actually escape through — is checked.
        """
        try:
            body_text = response.text
        except Exception:
            # Reading ``.text`` decodes bytes against the declared charset. A body that
            # will not decode is still evidence that a response arrived, so this is
            # unclassifiable rather than a transport failure.
            body_text = ""
        return classifier(
            http_status=response.status_code,
            body_text=body_text,
            parse=parse,
            raw_limit=self._raw_limit,
        )

    def _log_result(
        self, operation: str, result: ProviderResult[object], log_fields: dict[str, str]
    ) -> None:
        """One line per call, values in fields so the masker can see them.

        Deliberately absent: the payment link URL, the customer contact, the request
        body, and the authorization header. The URL is a bearer capability and logs
        travel further than dashboards do.
        """
        certainty = effect_certainty(result)
        fields: dict[str, Any] = {
            "operation": operation,
            "classification": type(result).__name__,
            "effect_certainty": certainty.value,
            **log_fields,
        }
        match result:
            case Success(http_status=status):
                _logger.info("provider call succeeded", http_status=status, **fields)
            case ClientError(code=code, http_status=status):
                _logger.warning(
                    "provider call failed definitively",
                    provider_error_code=code,
                    http_status=status,
                    **fields,
                )
            case ServerError(http_status=status):
                _logger.warning("provider server error", http_status=status, **fields)
            case Timeout(phase=phase, detail=detail, attempts=attempts):
                _logger.warning(
                    "provider call timed out",
                    phase=phase.value,
                    error_type=detail,
                    attempts=attempts,
                    **fields,
                )
            case Unclassifiable(detail=detail, http_status=status):
                # Raw body deliberately not logged: it is provider-controlled text of
                # unknown shape. It travels on the result to the audit record, which is
                # length-bounded and masked.
                _logger.error(
                    "provider response unclassifiable",
                    detail=detail,
                    http_status=status,
                    **fields,
                )


def _authorization_headers() -> dict[str, str]:
    """The Basic auth header, built from the secret store at call time.

    Verified scheme: ``Authorization: Basic base64(key_id:key_secret)``.

    Built per call and returned as a local rather than cached on the client, so the
    credential exists only for the duration of one request and a rotation takes effect
    on the next call without a restart. ``SecretValue.reveal()`` is called at exactly
    these two points in the module and the revealed strings are not bound to any name
    that outlives this frame.

    Raises:
        CredentialUnavailableError: if either half is missing or blank. The caller turns
            this into a refusal; it is never allowed to escape a client method.
    """
    store = get_secret_store()
    key_id = store.razorpay_key_id()
    key_secret = store.razorpay_key_secret()
    token = base64.b64encode(f"{key_id.reveal()}:{key_secret.reveal()}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}

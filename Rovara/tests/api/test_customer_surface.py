"""Task 40.4. The rejection table, the CORS confinement, and the headers, over the real app.

Everything here runs against a live application on a migrated database, because every claim is
about a *response* — a status code, a body, a header — and none of them is observable from below the
router.

**The rejection table is asserted as a table**, in one parametrized test, rather than as a dozen
tests each of which is a sentence. The design's table is the specification for the one endpoint an
attacker can reach without a credential, and a reader checking the implementation against it wants
to see the rows next to each other. A row that had its own test would be a row somebody could
delete without the shape of the table changing.

**Two rows are deferred and named as deferred**: the 422 for a Promise_Date inside
``PROMISE_MIN_LEAD_TIME`` (R23.C2) and the 409 for a second promise on one case (R23.C7). Both are
guarantees about the ``promise_to_pay`` row, which task 44 writes; ``PROMISE_MIN_LEAD_TIME`` is not
in the configuration catalogue yet, and implementing the check against an unconfigured bound would
mean inventing the number. What *is* implemented is the degenerate half a schema constraint already
forces — a Promise_Date at or before the submission instant, refused with 422 and no configured
bound consulted.

**CORS is asserted on both sides of the boundary.** That the customer surface answers a preflight
from a permitted origin, refuses a write from an unpermitted one, and never returns
``allow-credentials``; and that the dashboard API answers no preflight at all. The second is the
half that keeps ADR-9 true, and it is the one that would silently stop being true if somebody moved
the middleware up to the parent application.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.routers.customer import (
    CUSTOMER_HEADERS,
    CustomerSurfaceGuards,
    build_customer_app,
    customer_origins,
    require_tls,
)
from revora.customer.tokens import TokenService, wire_token
from revora.domain.actions import CandidateAction
from revora.domain.enums import TokenRevocationReason
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.platform.ratelimit import shared_limiter
from tests.api.conftest import insert_case
from tests.pg_support import insert_merchant

pytestmark = pytest.mark.pg

_CONFIG = default_configuration()
_ORIGIN = "https://pay.example.test"
_FORGED = wire_token("a" * 26, "A" * 22)


@pytest.fixture(autouse=True)
def _clear_rate_limiter() -> None:
    """Start every test with an empty rate limiter.

    The limiter is process-local by design (see ``revora.platform.ratelimit``), so it is shared
    across tests in one session — and a test that exhausts a rate would otherwise make every later
    one answer 429 for a reason several files away. Clearing it here rather than injecting a
    per-test limiter keeps production code from taking an argument only a test supplies.
    """
    shared_limiter().reset()


@pytest.fixture
def customer_app(installed_engine: Engine, installed_secrets: None) -> FastAPI:
    """The full application with the customer surface's CORS configured explicitly.

    ``origins`` is passed rather than read from the environment, because the environment is shared
    with every other test in the session and a test that had to set a variable would be a test that
    could leak one.
    """
    return create_app(
        customer_origins=[_ORIGIN], verify_schema=False, serve_dashboard=False
    )


@pytest.fixture
def customer_client(customer_app: FastAPI) -> TestClient:
    return TestClient(customer_app)


def _slug(engine: Engine, merchant_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
            ).scalar_one()
        )


def _live_case(engine: Engine, *, state: str = "POLICY_CHECK") -> tuple[str, uuid.UUID, str]:
    """A merchant, a case in ``state``, and a freshly minted wire token for it."""
    merchant_id = insert_merchant(engine, display_name="Customer surface")
    case_id = insert_case(engine, merchant_id, state=state)
    moment = datetime.now(UTC)
    with tenant_transaction(merchant_id) as session:
        minted = TokenService.on_session(session, _CONFIG).mint(
            merchant_id,
            case_id=case_id,
            window_end_at=moment + timedelta(days=6),
            approved_action=CandidateAction.PAYMENT_LINK,
            moment=moment,
        )
    assert minted.token is not None and minted.token.wire_token is not None
    return _slug(engine, merchant_id), case_id, minted.token.wire_token


def _headers(token: str | None, *, json: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    if token is not None:
        out["Authorization"] = f"Bearer {token}"
    if json:
        out["Content-Type"] = "application/json"
    return out


# ---------------------------------------------------------------------------
# Response headers on every response, including every rejection
# ---------------------------------------------------------------------------


def test_every_customer_response_carries_the_declared_headers(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R19.C11, R29.C7, and the CSP — on a 200, a 404, a 410 and a 422 alike.

    The rejections are the point. A 404 and a 410 are exactly what a probe collects, and a
    ``no-store`` present on success and absent on failure is a directive that stops applying at the
    moment it matters. So the same assertion runs over four different outcomes.

    The CSP's contents are checked rather than merely its presence: ``default-src 'none'`` so a
    directive nobody thought of falls closed, and no ``connect-src``, ``img-src``, ``script-src`` or
    ``style-src`` beyond ``'self'`` — which is what makes "no third-party asset request, no
    analytics request" a browser-enforced fact rather than a promise.
    """
    slug, _case_id, token = _live_case(installed_engine)
    responses = {
        "200 read": customer_client.get(f"/customer/{slug}/case", headers=_headers(token)),
        "404 forged": customer_client.get(
            f"/customer/{slug}/case", headers=_headers(_FORGED)
        ),
        "404 unknown slug": customer_client.get(
            "/customer/nope/case", headers=_headers(token)
        ),
        "422 extra field": customer_client.post(
            f"/customer/{slug}/delay-reason",
            headers=_headers(token, json=True),
            json={"delay_reason": "OTHER", "amount": 1},
        ),
    }
    for label, response in responses.items():
        for name, value in CUSTOMER_HEADERS.items():
            assert response.headers.get(name) == value, (
                f"the {label} response is missing {name}. Every response from this surface carries "
                "the same set, and a rejection is the response a probe is most likely to receive"
            )

    policy = responses["200 read"].headers["content-security-policy"]
    assert policy.startswith("default-src 'none'"), (
        "the CSP does not start closed, so a directive nobody thought of falls open"
    )
    for directive in ("connect-src", "img-src", "script-src", "style-src"):
        assert f"{directive} 'self'" in policy, (
            f"{directive} is not confined to 'self', so the page could reach a third party"
        )
    assert "'unsafe-inline'" not in policy and "*" not in policy


# ---------------------------------------------------------------------------
# The rejection table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "method", "suffix", "token", "body", "expected", "expected_body"),
    [
        ("bad signature", "GET", "case", "forged", None, 404, None),
        ("no such token_id", "GET", "case", "absent", None, 404, None),
        ("missing header", "GET", "case", None, None, 404, None),
        ("malformed token", "GET", "case", "garbage", None, 404, None),
        (
            "field outside the schema",
            "POST",
            "delay-reason",
            "live",
            {"delay_reason": "OTHER", "nickname": "bob"},
            422,
            {"field": "nickname"},
        ),
        (
            "amount on a partial arrangement (R22.C1)",
            "POST",
            "partial-arrangement",
            "live",
            {"amount": 50000},
            422,
            {"field": "amount"},
        ),
        (
            "instalment_count on a partial arrangement (R22.C1)",
            "POST",
            "partial-arrangement",
            "live",
            {"instalment_count": 3},
            422,
            {"field": "instalment_count"},
        ),
        (
            "schedule on a partial arrangement (R22.C1)",
            "POST",
            "partial-arrangement",
            "live",
            {"schedule": "monthly"},
            422,
            {"field": "schedule"},
        ),
        (
            "delay reason outside the enumeration (R20.C1)",
            "POST",
            "delay-reason",
            "live",
            {"delay_reason": "I_FORGOT"},
            422,
            {"field": "delay_reason"},
        ),
        (
            "delay reason absent",
            "POST",
            "delay-reason",
            "live",
            {"note": "just a note"},
            422,
            {"field": "delay_reason"},
        ),
    ],
)
def test_the_rejection_table_row_by_row(
    installed_engine: Engine,
    customer_client: TestClient,
    row: str,
    method: str,
    suffix: str,
    token: str | None,
    body: dict[str, object] | None,
    expected: int,
    expected_body: dict[str, object] | None,
) -> None:
    """The design's *Rejection status codes* table, implemented exactly (R18.C6, R19.C4, R20.C1).

    The four 404 rows are the load-bearing ones, and they are four rows rather than one because
    R29.C6 requires them to be **indistinguishable**: a failed signature, an unknown handle, an
    absent header and a token that is not one must produce the same status *and* the same body. The
    parametrization proves the first; ``test_every_404_is_byte_identical`` below proves the second,
    which is the half a status-code assertion cannot see.

    The 422 rows name the field and nothing else. Three of them are ``amount``,
    ``instalment_count`` and ``schedule`` on a partial arrangement — R22.C1 — and none of them is a
    hand-written check: the model declares no such field, ``extra="forbid"`` refuses it, and the
    error's ``loc`` is the field name, so the rejection and the report both come from the schema.
    """
    slug, _case_id, live = _live_case(installed_engine)
    presented = {
        "live": live,
        "forged": _FORGED,
        "absent": wire_token("b" * 26, "B" * 22),
        "garbage": "Bearer not-a-token-at-all",
        None: None,
    }[token]

    if method == "GET":
        response = customer_client.get(
            f"/customer/{slug}/{suffix}", headers=_headers(presented)
        )
    else:
        response = customer_client.post(
            f"/customer/{slug}/{suffix}",
            headers=_headers(presented, json=True),
            json=body,
        )

    assert response.status_code == expected, f"{row}: {response.status_code} {response.text}"
    if expected_body is None:
        assert response.text == "", (
            f"{row} returned a body. Every 404 on this surface is empty, because a body is a "
            "channel for distinguishing the four conditions R29.C6 requires to be identical"
        )
    else:
        assert response.json() == expected_body, f"{row}: {response.text}"


def test_every_404_is_byte_identical(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R29.C6: the four ways of being refused with 404 are indistinguishable, body included.

    Status codes are asserted above. This asserts the *bodies* and the ``content-length``, because
    two empty-looking responses can differ — ``null`` versus ``""`` versus no body at all — and an
    attacker measuring which of the four they hit does not need much. An unknown merchant slug is
    included as a fifth: the endpoint must not be an oracle for which merchants exist either.
    """
    slug, _case_id, _live = _live_case(installed_engine)
    variants = {
        "forged": _headers(_FORGED),
        "unknown handle": _headers(wire_token("c" * 26, "C" * 22)),
        "malformed": _headers("nonsense"),
        "absent": {},
    }
    seen = {
        label: (
            customer_client.get(f"/customer/{slug}/case", headers=headers).status_code,
            customer_client.get(f"/customer/{slug}/case", headers=headers).content,
        )
        for label, headers in variants.items()
    }
    unknown_merchant = customer_client.get(
        "/customer/does-not-exist/case", headers=_headers(_FORGED)
    )
    seen["unknown merchant"] = (unknown_merchant.status_code, unknown_merchant.content)

    assert len(set(seen.values())) == 1, (
        f"the 404 responses are distinguishable: {seen}. R29.C6 requires a failed signature and an "
        "unknown token identifier to be identical in status and in body, and the same reasoning "
        "covers a malformed presentation and an unknown merchant slug"
    )


def test_a_revoked_token_is_410_with_only_the_expiry_indication(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R18.C7 and R18.C8: 410 carrying ``{"expired": true}`` and nothing else.

    "Nothing else" is asserted as an exact equality on the parsed body rather than as an absence of
    named fields, because the requirement is that the response contains **no Recovery_Case field**
    — and a check for specific absent keys would pass a body that had acquired a different one.

    A customer holding a dead link needs to be told it is dead rather than shown a 404 that reads as
    "wrong URL". That disclosure is accepted under R18.C7 because 128 bits of entropy makes
    enumeration infeasible.
    """
    slug, case_id, token = _live_case(installed_engine)
    merchant_id = uuid.UUID(
        str(
            _one(
                installed_engine,
                "SELECT merchant_id FROM recovery_case WHERE id = :c",
                {"c": str(case_id)},
            )
        )
    )
    with tenant_transaction(merchant_id) as session:
        revoked = TokenService.on_session(session, _CONFIG).revoke(
            merchant_id, case_id, reason=TokenRevocationReason.CONTACT_SUPPRESSED
        )
    assert revoked == 1

    for response in (
        customer_client.get(f"/customer/{slug}/case", headers=_headers(token)),
        customer_client.post(
            f"/customer/{slug}/delay-reason",
            headers=_headers(token, json=True),
            json={"delay_reason": "OTHER"},
        ),
    ):
        assert response.status_code == 410
        assert response.json() == {"expired": True}, (
            f"the 410 body is {response.text}; R18.C7 permits the expiry indication and no other "
            "Recovery_Case field"
        )


def test_a_terminal_case_answers_409_naming_the_terminal_state(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R19.C8: 409, the Terminal_State named, and nothing persisted.

    The state is named because it is the one thing the customer is entitled to be told — the case
    ended, and nothing they write now will be read. Everything else about the case stays withheld.

    The case is made terminal *after* the token was minted, which is the real sequence: a case ends
    between the customer's read and their write. A test that seeded a terminal case would also pass
    against an implementation that refused to mint for one, which is a different guarantee.
    """
    slug, case_id, token = _live_case(installed_engine)
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET state = 'STOPPED', "
                "terminal_reason = 'MAX_ATTEMPTS_REACHED' WHERE id = :c"
            ),
            {"c": str(case_id)},
        )

    response = customer_client.post(
        f"/customer/{slug}/partial-arrangement",
        headers=_headers(token, json=True),
        json={"note": "please"},
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"rejected": "CASE_TERMINAL", "detail": "STOPPED"}
    assert (
        _one(
            installed_engine,
            "SELECT count(*) FROM customer_signal WHERE case_id = :c",
            {"c": str(case_id)},
        )
        == 0
    )


def test_a_promise_date_at_or_before_the_submission_instant_is_422(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """The degenerate half of R23.C2, which needs no configured bound.

    ``promise_to_pay`` carries ``CHECK (promise_date > recorded_at)``, so a date in the past is not
    a promise the system *can* hold rather than one it declines to accept — and refusing it here
    costs nothing and consults no bound.

    **``PROMISE_MIN_LEAD_TIME`` is deferred to task 44**, deliberately and not silently: it is not
    in the configuration catalogue, the row it protects is not written yet, and implementing the
    check against a number invented here would put a bound in code that R15.C6 requires to be a
    versioned row with an approving user.
    """
    slug, case_id, token = _live_case(installed_engine)
    past = datetime.now(UTC) - timedelta(hours=1)
    response = customer_client.post(
        f"/customer/{slug}/promise",
        headers=_headers(token, json=True),
        json={"promise_date": past.isoformat()},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "promise_date"
    assert (
        _one(
            installed_engine,
            "SELECT count(*) FROM customer_signal WHERE case_id = :c",
            {"c": str(case_id)},
        )
        == 0
    )


def test_the_read_is_still_served_after_the_submission_cap(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R18.C9's asymmetry: the write stops at the cap and the read does not.

    The customer who has explained themselves five times must not lose the page telling them what
    they owe as a consequence of having explained themselves. This is the one bound in the system
    whose two halves point in opposite directions, so it is asserted as one test rather than two.
    """
    slug, _case_id, token = _live_case(installed_engine)
    accepted = 0
    for _ in range(_CONFIG.CUSTOMER_TOKEN_MAX_SUBMISSIONS + 2):
        response = customer_client.post(
            f"/customer/{slug}/delay-reason",
            headers=_headers(token, json=True),
            json={"delay_reason": "OTHER"},
        )
        if response.status_code == 201:
            accepted += 1
        else:
            assert response.status_code == 429, response.text

    assert accepted == _CONFIG.CUSTOMER_TOKEN_MAX_SUBMISSIONS

    read = customer_client.get(f"/customer/{slug}/case", headers=_headers(token))
    assert read.status_code == 200, (
        "the projection stopped being served once the cap was reached; R18.C9 keeps serving it "
        "until expiry"
    )
    assert read.json()["signals_remaining"] == 0


def test_the_rate_limits_shed_and_name_which_one(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R29.C1: 429 over either rate, no Recovery_Case field, and a record naming which.

    The per-token rate is the one exercised, because it is the lower of the two and therefore the
    one a single client reaches. The response body names the rate rather than the case, which is the
    "returning no Recovery_Case field" half — and the ``RATE_LIMIT_APPLIED`` record names it too, so
    an operator can tell "one customer is refreshing" from "one address is", which have different
    remedies.
    """
    slug, _case_id, token = _live_case(installed_engine)
    statuses = [
        customer_client.get(f"/customer/{slug}/case", headers=_headers(token)).status_code
        for _ in range(_CONFIG.CUSTOMER_PAGE_RATE_LIMIT + 3)
    ]
    assert statuses.count(200) == _CONFIG.CUSTOMER_PAGE_RATE_LIMIT, (
        f"{statuses.count(200)} reads were served against a per-token rate of "
        f"{_CONFIG.CUSTOMER_PAGE_RATE_LIMIT}"
    )
    shed = customer_client.get(f"/customer/{slug}/case", headers=_headers(token))
    assert shed.status_code == 429
    assert shed.json() == {"rate": "token"}, (
        f"the shed response is {shed.text}; R29.C1 requires it to return no Recovery_Case field, "
        "and naming the rate is what makes it actionable without naming the case"
    )

    with installed_engine.connect() as connection:
        rates = connection.execute(
            text(
                "SELECT evidence->>'rate' FROM audit_record WHERE event_type = "
                "'RATE_LIMIT_APPLIED' ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert rates == "token", (
        "the RATE_LIMIT_APPLIED record does not name which rate was exceeded, so an operator "
        "cannot tell a refreshing customer from a shared NAT"
    )


# ---------------------------------------------------------------------------
# CORS, and the confinement that keeps ADR-9 true
# ---------------------------------------------------------------------------


def test_the_customer_surface_answers_a_preflight_without_credentials(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """The permitted origin, ``GET, POST``, and **no** ``allow-credentials``.

    ``allow_credentials: false`` matters more here than the origin list does. The token travels in
    an ``Authorization`` header and never in a cookie, so no credentialed cross-origin request is
    ever needed — which means an attacker's page cannot make the browser attach the token. It would
    have to already hold it, and a page holding the token is past the point where CORS protects
    anything.

    Starlette adds the CORS-safelisted request headers (``Accept``, ``Accept-Language``,
    ``Content-Language``) to whatever is configured. That is not a widening: a browser permits them
    on any cross-origin request regardless, so the effective set is still ``Authorization`` and
    ``Content-Type`` plus what could not have been excluded.
    """
    slug, _case_id, _token = _live_case(installed_engine)
    preflight = customer_client.options(
        f"/customer/{slug}/delay-reason",
        headers={
            "Origin": _ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == _ORIGIN
    assert "access-control-allow-credentials" not in preflight.headers, (
        "the customer surface advertises credentialed cross-origin requests. The token is a "
        "header, never a cookie, so this would only ever enable an attacker's page to have the "
        "browser attach something"
    )
    allowed_methods = preflight.headers["access-control-allow-methods"]
    assert set(allowed_methods.replace(" ", "").split(",")) == {"GET", "POST"}
    allowed_headers = {
        name.strip().lower()
        for name in preflight.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type"} <= allowed_headers


def test_a_write_from_an_unpermitted_origin_is_refused_before_any_read(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R29.C8's origin clause, enforced by the endpoint and not only by the browser.

    CORS stops a *browser* from handing a response to a page; it does nothing about the request
    having been made. So an ``Origin`` present and absent from the configured set is 403 here — and
    the refusal happens in the guard middleware, before the token is verified, so a cross-origin
    write costs no database read at all.

    An **absent** ``Origin`` is permitted, and that is not a hole. It means a non-browser client,
    which is not a cross-origin request in the sense R29.C8 governs, and the bearer token in the
    header is the whole of the authorization either way.
    """
    slug, case_id, token = _live_case(installed_engine)
    refused = customer_client.post(
        f"/customer/{slug}/delay-reason",
        headers={**_headers(token, json=True), "Origin": "https://evil.example"},
        json={"delay_reason": "OTHER"},
    )
    assert refused.status_code == 403
    assert refused.text == ""
    assert (
        _one(
            installed_engine,
            "SELECT count(*) FROM customer_signal WHERE case_id = :c",
            {"c": str(case_id)},
        )
        == 0
    )

    permitted = customer_client.post(
        f"/customer/{slug}/delay-reason",
        headers={**_headers(token, json=True), "Origin": _ORIGIN},
        json={"delay_reason": "OTHER"},
    )
    assert permitted.status_code == 201, permitted.text


def test_a_write_declaring_the_wrong_content_type_is_415(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R29.C8's content-type clause, and why it is 415 rather than 422.

    A 422 would be saying "your fields are wrong" about a body that was never parsed as the right
    kind of thing. The check has to live in the guard middleware for the same reason: FastAPI
    rejects a non-JSON body while binding the parameter, so a check inside the handler is
    unreachable and the refusal would arrive as a 422 about fields nobody tried to read.

    Requiring the declaration has a second effect. A request with ``Content-Type: application/json``
    is never a CORS *simple request*, so a browser must preflight it — which means the origin list
    is consulted before the request is sent rather than only after the response comes back.
    """
    slug, _case_id, token = _live_case(installed_engine)
    response = customer_client.post(
        f"/customer/{slug}/delay-reason",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
        content='{"delay_reason": "OTHER"}',
    )
    assert response.status_code == 415, response.text
    assert response.json() == {"content_type": "application/json"}


def test_the_dashboard_api_and_the_webhook_keep_no_cors(customer_client: TestClient) -> None:
    """ADR-9, still true after the customer surface acquired CORS.

    This is the assertion that would fail if somebody moved the middleware from the mounted
    sub-application up to the parent app — the single change that would relax the dashboard's
    posture to serve one page, and the one that would otherwise leave every other test passing.

    A 405 rather than a 200 with CORS headers is what "no CORS middleware" looks like from outside:
    nothing answered the preflight, so routing did, and routing has no ``OPTIONS`` handler for these
    paths.
    """
    for path in ("/cases", "/metrics/summary", "/webhooks/razorpay/anything"):
        preflight = customer_client.options(
            path,
            headers={
                "Origin": _ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers, (
            f"{path} answered a CORS preflight. ADR-9 keeps the dashboard same-origin with the API "
            "precisely so that no CORS middleware is installed for it, and the customer surface's "
            "middleware is mounted on its own sub-application to keep that true"
        )


def test_no_dashboard_response_carries_the_customer_surface_headers(
    customer_client: TestClient
) -> None:
    """The response headers are confined too, not only the CORS middleware.

    ``no-store`` on every dashboard response would be a defensible choice and it is not the choice
    that was made — the SPA's hashed assets are cached indefinitely on purpose. So the headers are
    asserted *absent* here, which is what proves the middleware is scoped to the mount rather than
    applied globally and coincidentally harmless.
    """
    response = customer_client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("content-security-policy") is None
    assert response.headers.get("referrer-policy") is None


# ---------------------------------------------------------------------------
# Configuration parsing, and the TLS guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://*.vercel.app",
        "null",
        "https://pay.example.test/app",
        "pay.example.test",
    ],
)
def test_customer_origins_refuses_every_way_of_not_being_an_origin_list(value: str) -> None:
    """Five refusals at startup, where a misconfiguration is visible.

    Each is one of the ways an origin list stops being a list, and each would fail *silently* if
    accepted: a wildcard permits every site, a pattern permits every future preview deployment
    including one an attacker can create, ``null`` permits a sandboxed iframe and a local file, an
    entry with a path is something no browser will ever match, and a bare host has no scheme to
    match against.
    """
    with pytest.raises(ValueError, match="REVORA_CUSTOMER_ORIGINS"):
        customer_origins({"REVORA_CUSTOMER_ORIGINS": value})


def test_customer_origins_accepts_an_explicit_list_and_an_absent_variable() -> None:
    """An absent variable installs no middleware at all, which is stronger than an empty list."""
    assert customer_origins({}) == ()
    assert customer_origins(
        {"REVORA_CUSTOMER_ORIGINS": " https://a.example , https://b.example:8443 "}
    ) == ("https://a.example", "https://b.example:8443")
    assert build_customer_app(origins=()).user_middleware, (
        "an empty origin list left the surface with no middleware at all, so the response headers "
        "went missing along with the CORS the deployment did not need"
    )


def test_require_tls_defaults_off_and_refuses_cleartext_when_on() -> None:
    """R29.C5, and why the flag defaults off.

    With it on, every request whose forwarded protocol is not ``https`` is refused — including a
    local run, a container health probe and every test that talks to the app over HTTP. A control
    that cannot be off is a control somebody disables permanently the first time it costs them an
    afternoon, so the default is the honest one and the deployment that terminates TLS at a proxy
    sets it.

    403 with an empty body rather than a redirect: redirecting would invite the client to send the
    same credential again over the same transport, and would make the cleartext exposure look
    handled.
    """
    assert require_tls({}) is False
    assert require_tls({"REVORA_CUSTOMER_REQUIRE_TLS": "1"}) is True

    guarded = build_customer_app(origins=(_ORIGIN,), enforce_tls=True)
    assert any(
        middleware.cls is CustomerSurfaceGuards for middleware in guarded.user_middleware
    )
    with TestClient(guarded) as client:
        cleartext = client.get("/anything/case", headers=_headers(_FORGED))
        assert cleartext.status_code == 403
        assert cleartext.text == ""
        forwarded = client.get(
            "/anything/case",
            headers={**_headers(_FORGED), "x-forwarded-proto": "https"},
        )
        assert forwarded.status_code != 403, (
            "a request forwarded as https was refused, so a deployment behind a TLS terminator "
            "could not serve the page at all"
        )


def _one(engine: Engine, sql: str, params: dict[str, object]) -> object:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


# ---------------------------------------------------------------------------
# R20.C9 — what a stated reason must not touch
# ---------------------------------------------------------------------------


def test_a_delay_reason_leaves_the_window_the_counters_and_the_last_outbound_alone(
    installed_engine: Engine, customer_client: TestClient
) -> None:
    """R20.C9, over the real endpoint.

    Four columns, and each of them would be a different way for a customer to spend something
    on their own behalf. ``window_end_at`` is the termination guarantee — R2.C5 says it never
    moves, so a submission that extended it would make "every case ends" a claim with an
    exception a stranger controls. The two counters are the bounds that stop a person being
    contacted repeatedly, and a write that consumed one would let a customer explaining
    themselves reduce how many times Revora may reply. ``last_outbound_at`` is what the
    cooldown is measured from, so moving it would either delay or unlock the next action.

    The columns are seeded to **non-default values first**, which is the part that makes this
    an assertion rather than a coincidence. Against a fresh case every counter is zero and
    ``last_outbound_at`` is null, so a write that reset them would pass unnoticed.
    """
    slug, case_id, token = _live_case(installed_engine)
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE recovery_case
                   SET executed_action_count = 2,
                       customer_message_count = 1,
                       last_outbound_at = now() - interval '3 hours'
                 WHERE id = :c
                """
            ),
            {"c": str(case_id)},
        )

    columns = (
        "window_end_at",
        "executed_action_count",
        "customer_message_count",
        "last_outbound_at",
    )
    sql = f"SELECT {', '.join(columns)} FROM recovery_case WHERE id = :c"
    with installed_engine.connect() as connection:
        before = dict(
            connection.execute(text(sql), {"c": str(case_id)}).mappings().one()
        )

    response = customer_client.post(
        f"/customer/{slug}/delay-reason",
        headers=_headers(token, json=True),
        json={"delay_reason": "SALARY_OR_CASHFLOW_TIMING"},
    )
    assert response.status_code == 201, response.text

    with installed_engine.connect() as connection:
        after = dict(connection.execute(text(sql), {"c": str(case_id)}).mappings().one())
    assert after == before

    # The signal is there, so the equality above is not the equality of a write that failed.
    assert (
        _one(
            installed_engine,
            "SELECT count(*) FROM customer_signal WHERE case_id = :c AND kind = 'DELAY_REASON'",
            {"c": str(case_id)},
        )
        == 1
    )

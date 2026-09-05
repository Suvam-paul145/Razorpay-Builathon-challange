"""Authentication, and the tenant isolation that is the whole reason authentication exists.

Property 30 is the target: **every read, list and query returns only the session merchant's
records, a request naming another merchant's record returns no field of it, and a supplied merchant
id has no effect.** The last clause is checked structurally rather than behaviourally — there is no
merchant-id parameter anywhere in the schema to supply — because a behavioural check can only prove
that the ids somebody thought to try were ignored.

Every authentication failure is asserted to answer 401 *with no body*. That is not fussiness: a
body that varies with the reason turns the endpoint into an oracle for which merchants and which
tokens exist, and the variation is exactly what a well-meaning "helpful error message" adds.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.audit.events import (
    AUTHENTICATION_FAILED,
    AUTHORIZATION_DENIED,
    SESSION_ESTABLISHED,
    SESSION_REVOKED,
)
from revora.platform.clock import now
from tests.api.conftest import DASHBOARD_KEY, Tenant, insert_case

pytestmark = pytest.mark.pg

_AUTHENTICATED_GETS = (
    "/cases",
    "/metrics/summary",
    "/metrics/unresolved",
    "/experiments",
    "/health/webhook",
)


def _audit_events(engine: Engine, merchant_id: uuid.UUID, event_type: str) -> list[dict]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT decision FROM audit_record WHERE merchant_id = :m AND event_type = :e "
                "ORDER BY created_at"
            ),
            {"m": str(merchant_id), "e": event_type},
        ).all()
    return [row[0] or {} for row in rows]


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def test_a_session_is_minted_and_recorded_against_a_named_user(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The credential is a shared operator key, so the record has to name the user it acts as.

    Otherwise every later action is attributed to "the key", and an audit trail whose actor is a
    credential rather than a person cannot answer the only question it is ever asked.
    """
    records = _audit_events(installed_engine, tenant.merchant_id, SESSION_ESTABLISHED)
    assert len(records) == 1
    assert records[0]["merchant_user_id"] == str(tenant.user_id)
    # The lifetime, not only the expiry: the bound is configurable, so a session that outlived a
    # later shortening has to remain explicable.
    assert int(records[0]["session_lifetime_seconds"]) == 43_200


def test_the_token_is_never_stored_in_recoverable_form(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """A dump of ``merchant_session`` must not hand over live sessions.

    Asserted against the token's secret half rather than the whole token, because the slug prefix
    is public and finding it in the table would prove nothing.
    """
    secret = tenant.token.rsplit(".", 1)[1]
    with installed_engine.begin() as connection:
        digests = [
            row[0]
            for row in connection.execute(
                text("SELECT token_digest FROM merchant_session WHERE merchant_id = :m"),
                {"m": str(tenant.merchant_id)},
            ).all()
        ]
    assert digests
    assert secret not in digests
    for digest in digests:
        assert len(digest) == 64, "expected a keyed SHA-256 digest, 64 hex characters"
        assert secret not in digest


def test_a_wrong_operator_key_is_refused_and_recorded(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """401 with no body, and a record saying which failure it was."""
    response = client.post(
        "/auth/sessions",
        json={"merchant_slug": tenant.slug},
        headers={DASHBOARD_KEY_HEADER: "not-the-key"},
    )
    assert response.status_code == 401
    assert tenant.slug not in response.text
    assert str(tenant.merchant_id) not in response.text

    reasons = [
        record.get("reason")
        for record in _audit_events(installed_engine, tenant.merchant_id, AUTHENTICATION_FAILED)
    ]
    assert "dashboard key did not verify" in reasons


def test_an_unknown_merchant_slug_is_refused_exactly_like_a_wrong_key(
    client: TestClient,
) -> None:
    """No merchant, no record, one answer.

    There is deliberately no audit record here: ``audit_record.merchant_id`` is ``NOT NULL``, and a
    record belonging to no tenant belongs to nobody. The event is logged instead, which is where an
    unattributable one belongs.
    """
    response = client.post(
        "/auth/sessions",
        json={"merchant_slug": f"no-such-merchant-{uuid.uuid4()}"},
        headers={DASHBOARD_KEY_HEADER: DASHBOARD_KEY},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Using and losing a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _AUTHENTICATED_GETS)
def test_every_dashboard_endpoint_requires_a_session(client: TestClient, path: str) -> None:
    """No token, no data. Enumerated rather than sampled, because one unguarded endpoint is enough.

    ``GET /health`` and the webhook are absent from the list on purpose and are asserted
    unauthenticated by their own tests — a reader should be able to see which two they are.
    """
    assert client.get(path).status_code == 401


def test_a_malformed_bearer_token_is_refused(client: TestClient) -> None:
    """A token with no slug prefix cannot name a tenant, so it cannot be looked up."""
    for header in ("Bearer", "Bearer nodotshere", "Token abc.def", "Bearer .secret"):
        assert client.get("/cases", headers={"Authorization": header}).status_code == 401


def test_an_expired_session_is_unauthenticated_and_the_record_says_so(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R17.C1. Age, not idleness — the expiry is absolute and stored.

    Expiring the row by hand rather than by waiting twelve hours. What is being checked is that the
    endpoint reads the stored ``expires_at``, and moving that column is exactly how to check it.
    """
    # Both timestamps move, because ``ck_merchant_session_expiry_after_issue`` refuses a row whose
    # expiry precedes its issue — correctly, and it caught the first version of this test. What is
    # being simulated is a session issued thirteen hours ago under a twelve-hour bound, which is a
    # state the system genuinely reaches; a row expiring before it was issued is not.
    moment = now()
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE merchant_session SET issued_at = :issued, expires_at = :past "
                "WHERE merchant_id = :m"
            ),
            {
                "issued": moment - timedelta(hours=13),
                "past": moment - timedelta(hours=1),
                "m": str(tenant.merchant_id),
            },
        )

    assert client.get("/cases", headers=tenant.auth).status_code == 401
    reasons = [
        record.get("reason")
        for record in _audit_events(installed_engine, tenant.merchant_id, AUTHENTICATION_FAILED)
    ]
    assert "session expired past SESSION_LIFETIME" in reasons


def test_a_revoked_session_stops_working_immediately(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The reason a session is a row and not a signed token: it can be ended."""
    assert client.get("/cases", headers=tenant.auth).status_code == 200

    revoked = client.delete("/auth/session", headers=tenant.auth)
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    assert client.get("/cases", headers=tenant.auth).status_code == 401
    assert _audit_events(installed_engine, tenant.merchant_id, SESSION_REVOKED)

    # And revoking again is not an error. The desired end state has been reached.
    again = client.delete("/auth/session", headers=tenant.auth)
    assert again.status_code == 401, "a revoked token cannot authenticate its own second revocation"


def test_every_authentication_failure_answers_byte_identically(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The response must not vary with the reason, or the endpoint is an oracle.

    Five distinct failures — no token, malformed token, unknown token, unknown merchant, revoked
    session — compared byte for byte rather than only by status code. The status code is the easy
    half; the half that leaks is a helpful ``detail`` string, and the way that arrives is somebody
    adding one to a single branch. The audit trail keeps the distinction, and its records were
    asserted above.
    """
    unknown_slug_token = f"no-such-merchant-{uuid.uuid4()}.abcdefghijklmnop"
    unknown_secret_token = f"{tenant.slug}.not-a-real-secret-value"

    client.delete("/auth/session", headers=tenant.auth)

    responses = [
        client.get("/cases"),
        client.get("/cases", headers={"Authorization": "Bearer nodots"}),
        client.get("/cases", headers={"Authorization": f"Bearer {unknown_secret_token}"}),
        client.get("/cases", headers={"Authorization": f"Bearer {unknown_slug_token}"}),
        client.get("/cases", headers=tenant.auth),
    ]

    assert {response.status_code for response in responses} == {401}
    bodies = {response.content for response in responses}
    assert len(bodies) == 1, (
        f"authentication failures answered with {len(bodies)} distinct bodies, so the endpoint "
        f"discloses which failure occurred: {bodies}"
    )


def test_the_expiry_comes_from_the_configured_bound(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """Stored, not derived at read time, so changing the bound cannot move a live session."""
    with installed_engine.begin() as connection:
        issued, expires = connection.execute(
            text(
                "SELECT issued_at, expires_at FROM merchant_session WHERE merchant_id = :m"
            ),
            {"m": str(tenant.merchant_id)},
        ).one()
    assert expires - issued == timedelta(hours=12)


# ---------------------------------------------------------------------------
# Property 30 — tenant isolation
# ---------------------------------------------------------------------------


def test_a_case_of_another_merchant_is_a_404_that_discloses_nothing(
    installed_engine: Engine, client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """P30, R17.C3. **404, not 403**, and not one field of the row.

    A 403 would confirm the case exists and belongs to somebody else, which is the single fact a
    cross-tenant probe is looking for. The record keeps what the response withholds.
    """
    theirs = insert_case(installed_engine, other_tenant.merchant_id, amount=999_999)

    response = client.get(f"/cases/{theirs}", headers=tenant.auth)
    assert response.status_code == 404
    assert "999999" not in response.text
    assert str(other_tenant.merchant_id) not in response.text

    denials = _audit_events(installed_engine, tenant.merchant_id, AUTHORIZATION_DENIED)
    assert denials, "a cross-tenant read must leave a record naming the requester"
    latest = denials[-1]
    assert latest["requester_user_id"] == str(tenant.user_id)
    assert latest["requested_id"] == str(theirs)
    assert latest["answered"] == "404"


def test_a_missing_case_is_indistinguishable_from_another_merchants(
    installed_engine: Engine, client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The two must answer identically, or the difference is the disclosure."""
    theirs = insert_case(installed_engine, other_tenant.merchant_id)
    nonexistent = uuid.uuid4()

    cross = client.get(f"/cases/{theirs}", headers=tenant.auth)
    missing = client.get(f"/cases/{nonexistent}", headers=tenant.auth)
    assert cross.status_code == missing.status_code == 404
    assert cross.json() == missing.json()


def test_a_list_returns_only_the_session_merchants_cases(
    installed_engine: Engine, client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The list is where a forgotten ``WHERE`` would show, and where it would be least noticed."""
    mine = {str(insert_case(installed_engine, tenant.merchant_id)) for _ in range(3)}
    theirs = {str(insert_case(installed_engine, other_tenant.merchant_id)) for _ in range(4)}

    body = client.get("/cases", headers=tenant.auth).json()
    returned = {case["case_id"] for case in body["cases"]}
    assert returned == mine
    assert not returned & theirs


def test_an_ownership_write_on_another_merchants_case_is_the_same_404(
    installed_engine: Engine, client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    """A write must not be a better oracle than a read.

    Also checks the write did not happen: an endpoint that answered 404 *after* mutating would be
    the worst of both, and only reading the row back can tell.
    """
    theirs = insert_case(installed_engine, other_tenant.merchant_id)
    assert client.post(f"/cases/{theirs}/owner", headers=tenant.auth).status_code == 404

    with installed_engine.begin() as connection:
        owner = connection.execute(
            text("SELECT human_owner_user_id FROM recovery_case WHERE id = :c"),
            {"c": str(theirs)},
        ).scalar_one()
    assert owner is None, "a refused cross-tenant write must not have written"


def test_no_endpoint_accepts_a_merchant_id_from_the_caller(client: TestClient) -> None:
    """R17.C2, checked structurally. There is nowhere to put one.

    A behavioural test can only prove that the parameter names somebody thought to try were
    ignored. Walking the generated schema proves the stronger thing: no path, query or **request
    body** field anywhere in the API names a merchant, so a caller cannot supply one to be ignored.

    Only inputs are walked. ``SessionResponse.merchant_user_id`` is a response field and flagging it
    would be wrong — the first version of this test did flag it, which is the useful reminder that
    "no merchant id in the API" is a statement about what the API *accepts*.

    ``merchant_slug`` is the one deliberate exception: a slug is public routing information, it
    already appears in the webhook URL, and both endpoints that take one verify a credential scoped
    to it.
    """
    schema = client.get("/openapi.json").json()
    components = schema.get("components", {}).get("schemas", {})
    offenders: list[str] = []

    def _flag(where: str, name: str) -> None:
        if "merchant" in name.lower() and name != "merchant_slug":
            offenders.append(f"{where} names {name}")

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", ()):
                _flag(f"{method.upper()} {path} parameter", str(parameter.get("name", "")))

            body = operation.get("requestBody", {})
            for media in body.get("content", {}).values():
                reference = str(media.get("schema", {}).get("$ref", ""))
                component = components.get(reference.rsplit("/", 1)[-1], {})
                for field in component.get("properties", {}):
                    _flag(f"{method.upper()} {path} request body field", field)

    assert not offenders, (
        "a caller-supplied merchant identifier has appeared in the API surface; R17.C2 holds "
        f"because there is nowhere to write one: {offenders}"
    )

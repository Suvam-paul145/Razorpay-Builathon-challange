"""A live API on a migrated database, with two merchants that must never see each other.

Two merchants rather than one, in every test, because the property this tier exists to check is
tenant isolation and a single-tenant fixture makes every isolation test pass by having nothing to
leak. The second merchant is not a special case here — it is the baseline.

The engine, the secret store and the app are installed process-wide and torn down, in that order.
They have to be process-wide rather than injected: ``tenant_transaction`` reaches for the installed
session factory, and threading a factory through every endpoint would change production code to
suit a test.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.platform import crypto
from revora.platform.secrets import SecretStore, set_secret_store
from tests.pg_support import insert_merchant

DASHBOARD_KEY = "dashboard-operator-key-for-tests"
"""The operator key every test merchant shares. Shared deliberately: the resolver below answers for
any slug, so a test can create a merchant without registering a secret for it — the thing under
test is what the key *authorises*, not how it is stored."""

WEBHOOK_SECRET = "api-tier-webhook-secret"


class _Resolver:
    """Answers the per-merchant prefixed names for any slug, plus the fixed crypto keys."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        if name.startswith("REVORA_DASHBOARD_KEYS_"):
            return DASHBOARD_KEY
        if name.startswith("REVORA_WEBHOOK_SECRETS_"):
            return WEBHOOK_SECRET
        return self._values.get(name)


@pytest.fixture
def installed_engine(migrated_url: str) -> Iterator[Engine]:
    """Install a process-wide engine on the migrated database, disposed on teardown."""
    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield engine
    finally:
        dispose_engine()


@pytest.fixture
def installed_secrets() -> Iterator[None]:
    """Install a secret store holding the dashboard key, the session key and the crypto keys."""
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"E" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"F" * 32).decode(),
            "REVORA_SESSION_TOKEN_SECRET": base64.b64encode(b"G" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


@pytest.fixture
def app(installed_engine: Engine, installed_secrets: None) -> FastAPI:
    """The real application. Schema verification is on, because the fixture migrated the database.

    ``verify_schema=True`` is not incidental: it means this fixture fails loudly if
    ``EXPECTED_REVISION`` was not bumped alongside a migration, which is the mistake that has
    already happened once in this project.
    """
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client that runs the app's lifespan, so the startup schema check actually executes."""
    with TestClient(app) as test_client:
        yield test_client


@dataclass(frozen=True, slots=True)
class Tenant:
    """One merchant, one user, and a live session token for it."""

    merchant_id: uuid.UUID
    slug: str
    user_id: uuid.UUID
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def make_tenant(engine: Engine, client: TestClient, *, label: str) -> Tenant:
    """Create a merchant with one active user and mint a session through the real endpoint.

    Through the endpoint rather than by inserting a session row, because the token is only
    derivable from the minting path — the digest is keyed and the plaintext is never stored, so a
    hand-built row would be a session no test could use.
    """
    merchant_id = insert_merchant(engine, display_name=f"API tier {label}")
    with engine.begin() as connection:
        slug = connection.execute(
            text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
        ).scalar_one()
        user_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO merchant_user (
                    id, merchant_id, email_masked, email_key, role, is_active, created_at
                ) VALUES (:id, :m, :masked, :key, 'operator', true, now())
                """
            ),
            {
                "id": str(user_id),
                "m": str(merchant_id),
                "masked": "****ator@example.invalid",
                "key": f"emailkey-{user_id}",
            },
        )

    response = client.post(
        "/auth/sessions",
        json={"merchant_slug": str(slug)},
        headers={DASHBOARD_KEY_HEADER: DASHBOARD_KEY},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["merchant_user_id"] == str(user_id)
    return Tenant(
        merchant_id=merchant_id,
        slug=str(slug),
        user_id=user_id,
        token=body["token"],
    )


@pytest.fixture
def tenant(installed_engine: Engine, client: TestClient) -> Tenant:
    """The merchant under test."""
    return make_tenant(installed_engine, client, label="primary")


@pytest.fixture
def other_tenant(installed_engine: Engine, client: TestClient) -> Tenant:
    """A second merchant whose data the first must never be able to reach."""
    return make_tenant(installed_engine, client, label="other")


def insert_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    state: str = "DETECTED",
    amount: int = 250_000,
    customer_key: str | None = None,
) -> uuid.UUID:
    """A minimal recovery case, enough for the read endpoints to render one."""
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, customer_contact_masked, detected_at, window_end_at,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, :state, :pid, :amount, 'INR', :ck, '******3210',
                    now() - interval '1 hour', now() + interval '167 hours', 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state,
                "pid": f"pay_{case_id.hex[:16]}",
                "amount": amount,
                "ck": customer_key or f"ck-{case_id}",
            },
        )
    return case_id

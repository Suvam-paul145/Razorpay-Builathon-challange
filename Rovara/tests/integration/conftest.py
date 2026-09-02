"""A whole Revora, driven the way production drives it.

Nothing in this tier reaches into a module to make something happen. A signed webhook goes in at the
HTTP boundary, the worker is told to drain its queue, and every assertion afterwards is a read of a
persisted row or an API response. That is the only way to test a claim like "exactly one external
effect per approval": a test that called the execution engine directly would be testing the engine,
which its own tests already do, and would say nothing about whether the pipeline ever gets there.

The provider is the scriptable fake, and it is the *only* substitution. It records every call, which
is what makes "zero external calls" checkable — a negative needs a log, not an absence of evidence.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.domain.payment_event import PaymentStatus
from revora.jobs.pipeline import EXECUTION_JOB_KIND
from revora.jobs.worker import Handler, build_registry, run_once
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.platform import crypto
from revora.platform.crypto import customer_key
from revora.platform.secrets import SecretStore, set_secret_store
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour, as_provider_client
from tests.pg_support import insert_merchant

DASHBOARD_KEY = "integration-operator-key"
WEBHOOK_SECRET = "integration-webhook-secret"

_CONTACT = "+919876543210"
"""The one contact these helpers use, so consent can be recorded against its derived key before a
case exists. A constant rather than a parameter because every test that needs a different contact
needs a different consent decision too, and passing one without the other is the mistake this
module already made once."""

LINK_PATH_AMOUNT = 100_000
"""₹1,000. The amount at which the optimizer selects ``PAYMENT_LINK``.

Derived from the priors rather than picked for convenience, and the arithmetic decides which branch
these tests exercise. ``UPLIFT_PRIORS`` gives a payment link 0.08 and a human escalation 0.10;
``COST_PRIORS`` gives the link ₹10 of customer cost and the escalation ₹250 of staff time. So
escalation overtakes the link on net value at ₹12,000, and above that Revora asks a human — a real
and defensible product behaviour, not a defect.

At ₹1,000 the link nets ₹70 and clears ``MIN_NET_VALUE_THRESHOLD``, while the escalation's cost
ratio of 2.5 blows through ``MAX_COST_TO_VALUE_RATIO`` and excludes it. That makes the automated
path the one under test, and ``ESCALATION_PATH_AMOUNT`` covers the other branch. Tuning the priors
to make one test convenient would be exactly the dishonesty the labels on those priors exist to
prevent."""

ESCALATION_PATH_AMOUNT = 2_000_000
"""₹20,000. Above the crossover, so a human is asked and no provider call is made."""

MAX_WORKER_PASSES = 24
"""A bound rather than ``while True``. A pipeline that fails to advance fails the test instead of
hanging it, and twenty-four passes drains a seven-step chain with room for the retries a legitimate
optimistic-concurrency loss produces."""


class _Resolver:
    """Answers the per-merchant prefixed credentials for any slug, plus the fixed keys."""

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
    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield engine
    finally:
        dispose_engine()


@pytest.fixture
def installed_secrets() -> Iterator[None]:
    """Every credential the pipeline needs, and deliberately **not** an LLM one.

    ``REVORA_LLM_CREDENTIAL`` is absent on purpose. Task 14 was dropped, so there is no reasoning
    layer to credential — and a fixture that supplied a key for a component that does not exist
    would make the "runs fully with the model unavailable" claim untestable by making the
    unavailability invisible.
    """
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"I" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"J" * 32).decode(),
            "REVORA_SESSION_TOKEN_SECRET": base64.b64encode(b"K" * 32).decode(),
            "REVORA_RAZORPAY_KEY_ID": "rzp_test_integration",
            "REVORA_RAZORPAY_KEY_SECRET": "integration-secret",
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
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@dataclass(frozen=True, slots=True)
class Tenant:
    merchant_id: uuid.UUID
    slug: str
    user_id: uuid.UUID
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def tenant(installed_engine: Engine, client: TestClient) -> Tenant:
    """A merchant with one active user and a live dashboard session."""
    merchant_id = insert_merchant(installed_engine, display_name="Integration merchant")
    with installed_engine.begin() as connection:
        slug = connection.execute(
            text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
        ).scalar_one()
        user_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO merchant_user (
                    id, merchant_id, email_masked, email_key, role, is_active, created_at
                ) VALUES (:id, :m, '****ops@example.invalid', :key, 'operator', true, now())
                """
            ),
            {"id": str(user_id), "m": str(merchant_id), "key": f"emailkey-{user_id}"},
        )

    response = client.post(
        "/auth/sessions",
        json={"merchant_slug": str(slug)},
        headers={DASHBOARD_KEY_HEADER: DASHBOARD_KEY},
    )
    assert response.status_code == 201, response.text
    return Tenant(
        merchant_id=merchant_id,
        slug=str(slug),
        user_id=user_id,
        token=response.json()["token"],
    )


# ---------------------------------------------------------------------------
# Driving the system
# ---------------------------------------------------------------------------


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, sha256).hexdigest()


def failed_payment_body(
    payment_id: str,
    event_id: str,
    *,
    amount: int = 2_000_000,
    contact: str = "+919876543210",
    reason: str = "insufficient_funds",
) -> bytes:
    """A verified-shape ``payment.failed`` envelope.

    ``insufficient_funds`` maps deterministically to ``INSUFFICIENT_FUNDS``, whose eligibility row
    permits ``PAYMENT_LINK`` — so the optimizer has a real action to weigh against the null ones
    rather than falling through for lack of a candidate.
    """
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "created_at": 1_700_000_500,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": f"order_{event_id}",
                    "method": "card",
                    "contact": contact,
                    "email": "buyer@example.invalid",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "insufficient balance",
                    "error_reason": reason,
                    "error_source": "issuer_bank",
                    "error_step": "payment_authorization",
                    "created_at": 1_700_000_500,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def delayed_capture(amount: int = LINK_PATH_AMOUNT) -> ProviderBehaviour:
    """Failed on the first read, captured afterwards, with the read amount pinned to the case.

    Two reads rather than one because the ordering is the claim under test: the read taken right
    after the link is created must find the payment still failed and decline to recover, and only
    the read taken after the capture webhook may recover it. A fake answering captured immediately
    would recover the case before the customer had paid, and the test could then not tell a
    read-driven recovery from a webhook-driven one.

    ``payment_amount`` is pinned explicitly even though the fake's default happens to equal
    ``LINK_PATH_AMOUNT``. A read whose amount differs from the case's is a *partial payment* to the
    outcome monitor, so leaving the two to agree by coincidence would mean any future change to
    either constant silently turned these tests into partial-recovery tests.
    """
    return ProviderBehaviour(
        payment_statuses=(PaymentStatus.FAILED, PaymentStatus.CAPTURED),
        payment_amount=amount,
    )


def captured_payment_body(
    payment_id: str, event_id: str, *, amount: int = LINK_PATH_AMOUNT
) -> bytes:
    """A ``payment.captured`` envelope — the success signal, not the proof of success.

    Revora never declares a recovery from this. It triggers an authoritative read, and the read is
    what decides. That distinction is R10.C1 and it is why this body's ``status`` is passed to the
    monitor as a *claim* to be checked against the read rather than as an input to the decision.
    """
    payload = {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "created_at": 1_700_003_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "amount_refunded": 0,
                    "captured": True,
                    "currency": "INR",
                    "status": "captured",
                    "method": "card",
                    "created_at": 1_700_003_000,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def deliver(client: TestClient, slug: str, body: bytes, event_id: str) -> int:
    """POST one signed webhook the way Razorpay would. Returns the status code."""
    return client.post(
        f"/webhooks/razorpay/{slug}",
        content=body,
        headers={
            "X-Razorpay-Signature": sign(body),
            "X-Razorpay-Event-Id": event_id,
            "content-type": "application/json",
        },
    ).status_code


def registry_for(fake: FakeRazorpay) -> dict[str, Handler]:
    """The real worker registry with the scriptable fake substituted for the provider."""
    return build_registry(provider=as_provider_client(fake))


def registry_without_executor(fake: FakeRazorpay) -> dict[str, Handler]:
    """The real registry with the execution handler replaced by a no-op.

    How a test parks a case at ``ACTION_SCHEDULED``. ``run_once`` drains a merchant's whole queue
    in one pass, so there is no "stop after policy" — the only way to hold a case at the scheduling
    edge is for the component that consumes the authorization to be unavailable, which is also
    exactly the fault the degradation tests are staging. The authorization stays durable and
    unconsumed, no provider call is made, and no outcome observation is enqueued, because none of
    those happen without the executor.

    A no-op rather than a deleted key: an unregistered kind is ``fail``-ed and rescheduled with
    backoff, which would push the job's ``run_after`` into the future and make it unreachable to a
    later drain in the same test.
    """
    handlers = registry_for(fake)
    handlers[EXECUTION_JOB_KIND] = lambda claimed: None
    return handlers


def drain(fake: FakeRazorpay, worker_id: str = "integration-worker") -> int:
    """Work the queue to empty. Returns passes used.

    The registry is rebuilt from the fake rather than captured once, so a behaviour scripted between
    drains takes effect — which is how the delayed-capture and crash scenarios are staged.
    """
    handlers = registry_for(fake)
    for used in range(1, MAX_WORKER_PASSES + 1):
        if run_once(worker_id, registry=handlers) == 0:
            return used
    return MAX_WORKER_PASSES


def case_state(engine: Engine, case_id: uuid.UUID) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT state FROM recovery_case WHERE id = :c"), {"c": str(case_id)}
            ).scalar_one()
        )


def case_for_payment(engine: Engine, merchant_id: uuid.UUID, payment_id: str) -> uuid.UUID | None:
    with engine.begin() as connection:
        found = connection.execute(
            text(
                "SELECT id FROM recovery_case WHERE merchant_id = :m AND provider_payment_id = :p"
            ),
            {"m": str(merchant_id), "p": payment_id},
        ).scalar_one_or_none()
    return None if found is None else uuid.UUID(str(found))


def grant_consent(engine: Engine, merchant_id: uuid.UUID, customer_key: str) -> None:
    """Record consent effective a minute ago, so it governs the evaluation that follows."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    merchant_id, customer_key, opted_out, source, effective_at, created_at
                ) VALUES (:m, :ck, false, 'integration', now() - interval '1 minute', now())
                """
            ),
            {"m": str(merchant_id), "ck": customer_key},
        )


def record_opt_out(engine: Engine, merchant_id: uuid.UUID, customer_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    merchant_id, customer_key, opted_out, source, effective_at, created_at
                ) VALUES (:m, :ck, true, 'integration-opt-out', now() - interval '1 minute', now())
                """
            ),
            {"m": str(merchant_id), "ck": customer_key},
        )


def customer_key_of(engine: Engine, case_id: uuid.UUID) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT customer_key FROM recovery_case WHERE id = :c"), {"c": str(case_id)}
            ).scalar_one()
        )


def drive_to_case(
    engine: Engine,
    client: TestClient,
    tenant: Tenant,
    fake: FakeRazorpay,
    *,
    amount: int = LINK_PATH_AMOUNT,
    opted_out: bool = False,
    registry: dict[str, Handler] | None = None,
) -> tuple[uuid.UUID, str]:
    """Record the consent decision, deliver a failure, and let detection open the case.

    Returns ``(case_id, provider_payment_id)``.

    **This does not stop at the case.** ``run_once`` drains a merchant's whole queue in one pass and
    each step enqueues its successor inside its own transaction, so the first pass runs detection,
    diagnosis, estimation, optimization, policy and execution. The loop below exists only to reach a
    state where the case row is readable; it is not a way to inspect the midpoint.

    ``registry`` is how a test controls how far the pass gets — pass
    :func:`registry_without_executor` to hold the case at ``ACTION_SCHEDULED``. Without it the
    degradation tests would each be asserting against a case that had already recovered, which is
    how three of them failed on first run.

    **Consent is recorded before the webhook, and the ordering is not cosmetic.** ``run_once``
    drains a merchant's whole queue in one pass, and each pipeline step enqueues its successor
    inside its own transaction — so the first call runs detection, diagnosis, estimation,
    optimization *and* policy. Recording consent after "the case exists" therefore records it after
    the decision it was meant to govern, and the first version of this helper did exactly that: the
    opt-out test passed for the wrong reason, because the selected action happened not to be
    customer-visible.

    The key is derived here the same way the system derives it — ``crypto.customer_key`` over the
    contact — which is also how ``POST /consent`` does it. That is what makes recording consent
    before the case exists possible at all, and it matches reality: a merchant records consent at
    checkout, not after a failure.
    """
    key = customer_key(_CONTACT)
    if opted_out:
        record_opt_out(engine, tenant.merchant_id, key)
    else:
        grant_consent(engine, tenant.merchant_id, key)

    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = failed_payment_body(payment_id, event_id, amount=amount, contact=_CONTACT)
    assert deliver(client, tenant.slug, body, event_id) == 200

    handlers = registry_for(fake) if registry is None else registry
    case_id = None
    for _ in range(MAX_WORKER_PASSES):
        case_id = case_for_payment(engine, tenant.merchant_id, payment_id)
        if case_id is not None:
            break
        if run_once("integration-worker", registry=handlers) == 0:
            break
    assert case_id is not None, "detection never opened a case for the delivered failure"
    assert customer_key_of(engine, case_id) == key, (
        "the case's customer key must match the one consent was recorded against, or the consent "
        "checks are being answered about a different person"
    )
    return case_id, payment_id


__all__ = [
    "DASHBOARD_KEY",
    "ESCALATION_PATH_AMOUNT",
    "LINK_PATH_AMOUNT",
    "MAX_WORKER_PASSES",
    "WEBHOOK_SECRET",
    "FakeRazorpay",
    "ProviderBehaviour",
    "Tenant",
    "captured_payment_body",
    "case_for_payment",
    "case_state",
    "customer_key_of",
    "delayed_capture",
    "deliver",
    "drain",
    "drive_to_case",
    "failed_payment_body",
    "grant_consent",
    "record_opt_out",
    "registry_for",
    "registry_without_executor",
    "sign",
]

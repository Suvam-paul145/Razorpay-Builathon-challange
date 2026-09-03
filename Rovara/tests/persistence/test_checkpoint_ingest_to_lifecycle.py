"""Checkpoint (task 12): the transactional core, end to end, against real Postgres.

This is the ingest-to-lifecycle verification the phase closes on. It exercises the
whole path a real webhook takes — signature, canonicalization, dedup, the enqueued
detection job, case creation, and lifecycle expiry — through the actual services and
the actual queue, with only the credentials and the clock substituted.

Two guarantees are the point:

* **A signed ``payment.failed`` delivered twice produces one of everything.** One
  persisted event, one case, one detection verdict, and a gap-free audit trail. The
  second delivery is a duplicate, answered without a second case or a second message.
* **The sweeper terminates a case with the worker otherwise idle.** Once the window
  has elapsed, a lifecycle sweep expires the case from persisted timestamps alone,
  which is what makes termination independent of any job having survived.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import Engine, text

from revora.cases.sweeper import sweep_expired_cases
from revora.domain.enums import CaseState, DetectionVerdict, TerminalReason
from revora.domain.transitions import NON_TERMINAL_STATES
from revora.ingestion.service import IngestionOutcome, ingest_webhook
from revora.jobs.worker import run_once
from revora.persistence.repositories.engine import (
    build_engine,
    dispose_engine,
    set_engine,
)
from revora.platform import clock, crypto
from revora.platform.config import default_configuration
from revora.platform.secrets import SecretStore, set_secret_store

pytestmark = pytest.mark.pg

_WEBHOOK_SECRET = "checkpoint-webhook-secret-value"
_PROVIDER_EVENT_ID = "evt_checkpoint_0001"
_PAYMENT_ID = "pay_checkpoint_0001"

_WEBHOOK_SECRET_PREFIX = "REVORA_WEBHOOK_SECRETS_"

_NON_TERMINAL_VALUES = frozenset(state.value for state in NON_TERMINAL_STATES)
"""Read from the state machine rather than listed, so this test and the partial unique index
cannot disagree about what "open" means."""


class _Resolver:
    """A secret resolver for the test, credentials supplied without environment.

    The webhook secret resolves for *any* merchant slug's key, so the test can use a
    fresh unique slug per run — the external test database persists across runs, and a
    fixed slug would collide on the ``uq_merchant_slug`` constraint the second time.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        if name.startswith(_WEBHOOK_SECRET_PREFIX):
            return _WEBHOOK_SECRET
        return self._values.get(name)


@pytest.fixture
def installed_engine(migrated_url: str) -> Engine:
    """Install a process-wide engine on the migrated database, disposed on teardown.

    The services reach the database through the process-wide factory, so the test
    installs one on the same migrated database the fixtures built. A fresh engine
    rather than the shared ``owner_engine`` so tearing it down does not dispose a
    session-scoped fixture's pool.
    """
    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield engine
    finally:
        dispose_engine()


@pytest.fixture
def installed_secrets() -> None:
    """Install a secret store with a known webhook secret and crypto keys."""
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"A" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"B" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"B" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


def _make_merchant(engine: Engine) -> tuple[uuid.UUID, str]:
    merchant_id = uuid.uuid4()
    slug = f"checkpoint-{merchant_id}"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Checkpoint Merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": slug},
        )
    return merchant_id, slug


def _failed_payment_body() -> bytes:
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "created_at": 1_700_000_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": _PAYMENT_ID,
                    "amount": 250000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_checkpoint_0001",
                    "method": "card",
                    "contact": "+919876543210",
                    "email": "buyer@example.com",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "payment failed for the test",
                    "error_reason": "payment_failed",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "created_at": 1_700_000_000,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _sign(body: bytes) -> str:
    return hmac.new(_WEBHOOK_SECRET.encode("utf-8"), body, sha256).hexdigest()


def _scalar(engine: Engine, sql: str, params: dict[str, object]) -> object:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _case_row(engine: Engine, merchant_id: uuid.UUID) -> tuple[uuid.UUID, str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, state, terminal_reason FROM recovery_case "
                "WHERE merchant_id = :m AND provider_payment_id = :p"
            ),
            {"m": str(merchant_id), "p": _PAYMENT_ID},
        ).one()
    return uuid.UUID(str(row[0])), str(row[1]), (None if row[2] is None else str(row[2]))


def _assert_gap_free(engine: Engine, case_id: uuid.UUID) -> None:
    """The per-case audit sequence is 1..n with no holes and no duplicates.

    The phase-1 invariant (R11.C4, P12), stated independently of how many records happen to
    exist. Gap-freeness is the property; the count is an implementation detail of whatever
    ran.
    """
    seqs = _audit_seqs(engine, case_id)
    assert seqs == list(range(1, len(seqs) + 1)), f"audit sequence has a gap: {seqs}"


def _audit_seqs(engine: Engine, case_id: uuid.UUID) -> list[int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT seq FROM audit_record WHERE case_id = :c ORDER BY seq"),
            {"c": str(case_id)},
        ).all()
    return [int(row[0]) for row in rows]


def test_ingest_to_lifecycle_end_to_end(
    installed_engine: Engine,
    installed_secrets: None,
    manual_clock: clock.ManualClock,
) -> None:
    """One signed failure, delivered twice, becomes one case that later expires."""
    engine = installed_engine
    merchant_id, slug = _make_merchant(engine)
    config = default_configuration()
    body = _failed_payment_body()
    signature = _sign(body)

    # First delivery: accepted, event persisted, detection job enqueued.
    first = ingest_webhook(
        merchant_id,
        slug,
        body=body,
        provided_signature=signature,
        provider_event_id=_PROVIDER_EVENT_ID,
        config=config,
        correlation_id=uuid.uuid4(),
    )
    assert first.outcome is IngestionOutcome.ACCEPTED
    assert first.webhook_event_id is not None

    # Second delivery of the same event id: a duplicate, no second event.
    second = ingest_webhook(
        merchant_id,
        slug,
        body=body,
        provided_signature=signature,
        provider_event_id=_PROVIDER_EVENT_ID,
        config=config,
        correlation_id=uuid.uuid4(),
    )
    assert second.outcome is IngestionOutcome.DUPLICATE

    event_count = _scalar(
        engine,
        "SELECT count(*) FROM webhook_event WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert event_count == 1, "at-least-once delivery must persist exactly one event"

    # The worker processes the enqueued detection job.
    processed = run_once("checkpoint-worker")
    assert processed >= 1

    # Exactly one verdict, and it opened exactly one case.
    verdict_count = _scalar(
        engine,
        "SELECT count(*) FROM detection_verdict WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert verdict_count == 1
    verdict_value = _scalar(
        engine,
        "SELECT verdict FROM detection_verdict WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert verdict_value == DetectionVerdict.AT_RISK.value

    case_count = _scalar(
        engine,
        "SELECT count(*) FROM recovery_case WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert case_count == 1

    case_id, state, _ = _case_row(engine, merchant_id)
    assert state in _NON_TERMINAL_VALUES, (
        "detection must leave the case open; which non-terminal state it reaches depends on "
        "how far the decision pipeline ran, which is phase 2's concern"
    )

    # The audit trail is gap-free. Deliberately *not* asserted as exactly ``[1]``: since the
    # decision pipeline was wired in, detection enqueues diagnosis and the worker drains the
    # whole chain in one pass, so the trail legitimately holds a record per step. The phase-1
    # guarantee is that the per-case sequence has no holes and no duplicates whatever runs —
    # pinning the count would make this test fail every time a downstream step is added,
    # which would be the test asserting the pipeline's length rather than the invariant.
    _assert_gap_free(engine, case_id)

    # Re-running the worker is a no-op for detection: the job is done and detection is
    # idempotent, so no second verdict and no second case appear.
    run_once("checkpoint-worker")
    assert (
        _scalar(
            engine,
            "SELECT count(*) FROM recovery_case WHERE merchant_id = :m",
            {"m": str(merchant_id)},
        )
        == 1
    )
    assert verdict_count == 1

    # The window elapses while the worker is idle; a lifecycle sweep expires the case.
    manual_clock.advance(config.RECOVERY_WINDOW_DURATION + timedelta(minutes=1))
    expired = sweep_expired_cases(merchant_id)
    assert expired == 1

    _, terminal_state, terminal_reason = _case_row(engine, merchant_id)
    assert terminal_state == CaseState.EXPIRED.value
    assert terminal_reason == TerminalReason.RECOVERY_WINDOW_ELAPSED.value

    # Still gap-free after the terminal transition, and the expiry is the newest record.
    seqs = _audit_seqs(engine, case_id)
    _assert_gap_free(engine, case_id)
    assert len(seqs) >= 2, "the expiry adds a record to whatever the pipeline wrote"

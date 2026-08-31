"""The three provider-touching sweeps are wired to real handlers, not to the no-op stub.

Task 7.3 registered every periodic sweep against ``_handle_not_yet_implemented`` so a
scheduled sweep would complete rather than dead-letter before its owner existed. That stub is
exactly the kind of scaffolding that survives into production unnoticed: a sweep pointed at it
runs, succeeds, logs at debug, and does nothing — so execution reconciliation silently stops
resolving intents and the detection-gap backfill silently stops closing gaps, while the queue
reports a clean bill of health.

Nothing else in the suite would catch that. The reconciliation and backfill functions are
tested directly, so they pass; the queue is tested directly, so it passes; only the *edge
between them* is untested, and it is an edge whose failure mode is silence.

So these tests drive the sweeps the way the worker does — enqueue, ``run_once``, assert the
provider was actually consulted — and one of them asserts the stub's absence directly, because
a handler could be replaced by a different do-nothing and the behavioural tests would not
notice.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, IntentState
from revora.domain.keys import execution_key
from revora.domain.payment_event import PaymentStatus
from revora.jobs import worker as worker_module
from revora.jobs.scheduler import (
    DETECTION_GAP_BACKFILL_KIND,
    EXECUTION_RECONCILIATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
    PERIODIC_SWEEP_KINDS,
)
from revora.jobs.worker import build_registry, run_once
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store
from revora.providers.razorpay import (
    OPERATION_FETCH_PAYMENT,
    OPERATION_FIND_PAYMENT_LINKS,
    OPERATION_LIST_PAYMENTS,
)
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour, as_provider_client

pytestmark = pytest.mark.pg


class _Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"W" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"K" * 32).decode(),
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
def installed_engine(migrated_url: str) -> Iterator[Engine]:
    """A process-wide engine, because the sweep handlers take no factory argument.

    That is not an oversight in the handlers — a worker resolves its own session factory from
    the process, and threading a factory through every handler signature would exist only for
    tests. Installing the engine is how a test participates in the real arrangement instead of
    changing it.
    """
    from revora.persistence.repositories.engine import (
        build_engine,
        dispose_engine,
        set_engine,
    )

    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield engine
    finally:
        dispose_engine()


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


def _seed_merchant(engine: Engine) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Sweep merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"sweep-{merchant_id}"},
        )
    return merchant_id


def _seed_case(
    engine: Engine, merchant_id: uuid.UUID, *, state: CaseState
) -> tuple[uuid.UUID, str]:
    case_id = uuid.uuid4()
    event_id = uuid.uuid4()
    payment_id = f"pay_{case_id.hex[:14]}"
    moment = now()
    encrypted = payload_cipher().encrypt(
        json.dumps(
            {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": payment_id,
                            "entity": "payment",
                            "status": "failed",
                            "contact": "+919000090000",
                        }
                    }
                },
            }
        ).encode()
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, raw_payload_nonce, key_version,
                    canonical, correlation_id, signature_verified, received_at, created_at
                ) VALUES (
                    :id, :merchant_id, :eid, 'payment.failed',
                    :ciphertext, :nonce, :key_version, :canonical, :corr, true, :received, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "eid": f"evt_{case_id.hex[:16]}",
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "canonical": json.dumps({"provider_payment_id": payment_id}),
                "corr": str(uuid.uuid4()),
                "received": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, source_event_id, detected_at, window_end_at, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 250000, 'INR',
                    :customer_key, :source_event_id, :detected_at, :window_end, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "payment_id": payment_id,
                "customer_key": f"ck-{case_id}",
                "source_event_id": str(event_id),
                "detected_at": moment - timedelta(hours=1),
                "window_end": moment + timedelta(hours=168),
            },
        )
    return case_id, payment_id


def _seed_uncertain_intent(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> str:
    """An intent the reconciliation sweep must pick up and resolve by reading."""
    key = execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1)
    decision_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, idempotency_key, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, :action, 'ACTION_SCHEDULED', 1, :key, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
                "action": CandidateAction.PAYMENT_LINK.value,
                "key": key,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, is_post_payment,
                    reconciliation_attempts, counter_applied, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :decision_id, :key, :action,
                    1, :state, :started_at, false, 0, true, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "decision_id": str(decision_id),
                "key": key,
                "action": CandidateAction.PAYMENT_LINK.value,
                "state": IntentState.UNCERTAIN.value,
                "started_at": moment - timedelta(minutes=30),
            },
        )
    return key


def _enqueue(merchant_id: uuid.UUID, kind: str, factory: sessionmaker[Session]) -> None:
    with tenant_transaction(merchant_id, factory) as session:
        JobRepository(session).enqueue(
            merchant_id,
            kind=kind,
            payload={},
            run_after=now() - timedelta(seconds=1),
            dedupe_key=f"{kind}:{uuid.uuid4()}",
            correlation_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# The stub must be gone from the three sweeps that now have owners
# ---------------------------------------------------------------------------


def test_no_provider_touching_sweep_is_still_registered_as_a_no_op() -> None:
    """The stub is a silent-failure hazard, so its absence is asserted directly.

    A behavioural test proves a handler does something; this proves it is not the *specific*
    do-nothing that shipped as scaffolding. Both matter, because the stub completes
    successfully — a sweep pointed at it looks healthy in every queue metric.

    ``calibration_report`` is deliberately still a stub: its owner is task 15.5, in a later
    phase. Naming it here rather than excluding it silently means this test starts failing the
    moment that stops being true.
    """
    registry = build_registry(provider=as_provider_client(FakeRazorpay()))
    stub = worker_module._handle_not_yet_implemented

    still_stubbed = {kind for kind in PERIODIC_SWEEP_KINDS if registry.get(kind) is stub}

    assert still_stubbed == {"calibration_report"}, (
        f"unexpected no-op sweeps: {still_stubbed - {'calibration_report'}}"
    )
    assert set(PERIODIC_SWEEP_KINDS) <= registry.keys(), "a sweep kind has no handler at all"


# ---------------------------------------------------------------------------
# Each sweep reaches its provider operation through the worker
# ---------------------------------------------------------------------------


def test_execution_reconciliation_sweep_reads_the_provider_through_the_worker(
    owner_engine: Engine,
    installed_engine: Engine,
    factory: sessionmaker[Session],
) -> None:
    """The sweep resolves an UNCERTAIN intent by reading, dispatched by the real registry.

    Asserts the *read* operation specifically, and that no create was issued — the sweep must
    never repeat an external effect, and dispatching through the worker is a new path to that
    guarantee that the direct reconciliation tests do not cover.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id, _ = _seed_case(owner_engine, merchant_id, state=CaseState.EXECUTING)
    key = _seed_uncertain_intent(owner_engine, merchant_id, case_id)

    fake = FakeRazorpay(ProviderBehaviour.timeout_with_effect_created())
    # Put the link on the provider side without the database knowing about it — the exact state
    # a crash between the call and the result commit leaves behind, and the state reconciliation
    # exists to resolve. The create is pre-spent here so the assertion below is about the
    # sweep's behaviour, not this setup's.
    fake.create_payment_link(_link_request(key))
    creates_before = fake.create_call_count_for(key)
    assert creates_before == 1, "the fixture did not create the orphaned link"

    _enqueue(merchant_id, EXECUTION_RECONCILIATION_KIND, factory)
    processed = run_once(
        "sweep-test", registry=build_registry(provider=as_provider_client(fake))
    )

    assert processed >= 1, "the sweep job was not claimed"
    assert fake.calls_for(OPERATION_FIND_PAYMENT_LINKS), (
        "the reconciliation sweep issued no listing read — it is still a no-op in practice"
    )
    assert fake.create_call_count_for(key) == creates_before, (
        "the reconciliation sweep issued a create"
    )


def test_payment_state_sweep_reads_every_waiting_case_through_the_worker(
    owner_engine: Engine,
    installed_engine: Engine,
    factory: sessionmaker[Session],
) -> None:
    """A case waiting on an outcome is re-read without any webhook arriving.

    This is what stops a case from depending on delivery to reach its ending — a recovery that
    happened while webhooks were broken is still a recovery.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id, _ = _seed_case(owner_engine, merchant_id, state=CaseState.WAITING_FOR_OUTCOME)

    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    _enqueue(merchant_id, PAYMENT_STATE_RECONCILIATION_KIND, factory)
    processed = run_once(
        "sweep-test", registry=build_registry(provider=as_provider_client(fake))
    )

    assert processed >= 1
    assert fake.calls_for(OPERATION_FETCH_PAYMENT), (
        "the payment-state sweep issued no authoritative read"
    )

    with owner_engine.begin() as connection:
        state = connection.execute(
            text("SELECT state FROM recovery_case WHERE id = :id"), {"id": str(case_id)}
        ).scalar_one()
        outcome = connection.execute(
            text("SELECT count(*) FROM recovery_outcome WHERE case_id = :id"),
            {"id": str(case_id)},
        ).scalar_one()

    assert str(state) == CaseState.RECOVERED.value
    assert int(outcome) == 1, "recovery was verified but not recorded exactly once"


def test_detection_gap_backfill_sweep_lists_payments_through_the_worker(
    owner_engine: Engine,
    installed_engine: Engine,
    factory: sessionmaker[Session],
) -> None:
    """The backfill runs from the scheduler, which is the only way it ever runs.

    A backfill that works when called directly but is wired to a stub is worse than no backfill
    at all: the failure it exists to catch is a *silent* one, and so is this.
    """
    merchant_id = _seed_merchant(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.missed_failures(2))

    _enqueue(merchant_id, DETECTION_GAP_BACKFILL_KIND, factory)
    processed = run_once(
        "sweep-test", registry=build_registry(provider=as_provider_client(fake))
    )

    assert processed >= 1
    assert fake.calls_for(OPERATION_LIST_PAYMENTS), (
        "the backfill sweep issued no listing call"
    )

    with owner_engine.begin() as connection:
        events = connection.execute(
            text(
                """
                SELECT count(*) FROM webhook_event
                WHERE merchant_id = :m AND provider_event_id LIKE 'backfill:%'
                """
            ),
            {"m": str(merchant_id)},
        ).scalar_one()
    assert int(events) == 2, "the backfill sweep ingested nothing"


def _link_request(reference_id: str):
    """A payment-link request carrying a chosen reference id, for pre-seeding the fake."""
    from datetime import UTC, datetime

    from revora.domain.money import Minor
    from revora.providers.payment_link import CustomerContact, PaymentLinkRequest

    return PaymentLinkRequest(
        amount=Minor(250_000),
        currency="INR",
        description="sweep wiring fixture",
        reference_id=reference_id,
        customer=CustomerContact(contact="+919000090000"),
        expire_by=int(datetime(2030, 1, 1, tzinfo=UTC).timestamp()),
        case_id=str(uuid.uuid4()),
    )

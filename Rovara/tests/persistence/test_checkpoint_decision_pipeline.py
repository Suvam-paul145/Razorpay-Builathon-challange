"""Checkpoint (task 18): webhook to authorized decision, against real Postgres.

The phase-2 verification. A signed ``payment.failed`` arrives and the worker drives the
whole decision pipeline — detection, diagnosis, baseline, candidates, optimizer, policy —
producing a persisted recommendation and a persisted policy decision with twelve recorded
check outcomes, and issuing **zero external calls** on the way.

Three things are asserted that the pure tests cannot reach:

* **The pipeline actually chains.** Each step enqueues the next in the transaction that
  committed its own work, so the case walks ``DETECTED → DIAGNOSED → DECISION_PENDING →
  POLICY_CHECK`` under a worker that is only told to drain the queue.
* **Twelve check rows exist, in order.** Not "up to twelve". A partially recorded
  evaluation would look, in the record, exactly like one that ran fewer checks and
  approved.
* **Zero provider requests.** Verified structurally — no provider client exists in the
  decision path — and recorded in the audit trail, so the claim is queryable rather than
  merely true.

The recommendation and the decision are also checked for the honesty labels the design
insists on: an interval on the baseline, ``UNCALIBRATED`` on the uplift figures, and a
recorded reason whichever way the decision goes.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from hashlib import sha256

import pytest
from sqlalchemy import Engine, text

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, PolicyVerdict, SelectionReason
from revora.ingestion.service import IngestionOutcome, ingest_webhook
from revora.jobs.pipeline import EXECUTION_JOB_KIND
from revora.jobs.worker import Handler, build_registry, run_once
from revora.persistence.repositories.engine import (
    build_engine,
    dispose_engine,
    set_engine,
)
from revora.platform import crypto
from revora.platform.config import default_configuration
from revora.platform.secrets import SecretStore, set_secret_store

pytestmark = pytest.mark.pg

_WEBHOOK_SECRET = "decision-pipeline-secret-value"
_WEBHOOK_SECRET_PREFIX = "REVORA_WEBHOOK_SECRETS_"

_MAX_WORKER_PASSES = 12
"""Enough passes to drain a five-step pipeline with room to spare. A bound rather than a
``while True`` so a pipeline that fails to advance fails the test instead of hanging it."""


class _Resolver:
    """Supplies credentials without touching the environment.

    The webhook secret resolves for any merchant slug, so each run can use a fresh unique
    slug — the external test database persists across runs and a fixed slug would collide
    on ``uq_merchant_slug`` the second time.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        if name.startswith(_WEBHOOK_SECRET_PREFIX):
            return _WEBHOOK_SECRET
        return self._values.get(name)


@pytest.fixture
def installed_engine(migrated_url: str) -> Engine:
    """Install a process-wide engine on the migrated database, disposed on teardown."""
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
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"C" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"D" * 32).decode(),
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
    slug = f"decision-{merchant_id}"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Decision Pipeline', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": slug},
        )
    return merchant_id, slug


_CONTACT = "+919876543210"
"""The contact in the envelope below. Named so consent can be recorded against its derived key
before any case exists, which is the only ordering that works — see :func:`_grant_consent`."""


def _grant_consent(engine: Engine, merchant_id: uuid.UUID, customer_key: str) -> None:
    """Record consent so the customer-visible path is reachable.

    Without it the policy engine correctly blocks on ``CONSENT_MISSING``, which is a valid
    outcome but not the one this checkpoint is exercising — the point here is that the
    pipeline reaches a decision, and a consent block would prove the engine works while
    saying nothing about the optimizer having run.

    **Must be called before the webhook is delivered.** ``run_once`` drains a merchant's whole
    queue in one pass and every pipeline step enqueues its successor inside its own transaction,
    so the first pass runs detection *through policy*. Recording consent after "the case exists"
    records it after the decision it was meant to govern. The key is therefore derived from the
    contact the way the system derives it, rather than read back off the case row.

    ``effective_at`` is backdated a minute because a consent record takes effect at its own
    effective instant, and one written at ``now()`` races the evaluation reading it.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    merchant_id, customer_key, opted_out, source, effective_at, created_at
                ) VALUES (
                    :merchant_id, :customer_key, false, 'test',
                    now() - interval '1 minute', now()
                )
                """
            ),
            {"merchant_id": str(merchant_id), "customer_key": customer_key},
        )


def _failed_payment_body(payment_id: str, event_id: str) -> bytes:
    """A verified-shape ``payment.failed`` envelope with a mappable failure reason.

    ``insufficient_funds`` is chosen deliberately: it is in the design's verified reason
    table, it maps deterministically to ``INSUFFICIENT_FUNDS``, and that cause's
    eligibility row permits ``PAYMENT_LINK`` — so the pipeline has a real action to
    consider rather than falling through to the null actions for lack of one.

    **The amount is load-bearing and it is below the escalation crossover.** The priors put
    ``HUMAN_ESCALATION`` above ``PAYMENT_LINK`` on net value from about ₹12,000 up, and this
    checkpoint's whole claim is that the decision pipeline runs to completion and then stops at
    the first step that needs the outside world. An escalation is *terminal* — it never reaches
    that step — so above the crossover the test would still pass its later assertions while no
    longer demonstrating the thing it exists to demonstrate. It sat at ₹20,000 for exactly that
    reason and asserted the wrong terminal state until the integration tier made the crossover
    explicit.
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
                    "amount": 100_000,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": f"order_{event_id}",
                    "method": "card",
                    "contact": _CONTACT,
                    "email": "buyer@example.com",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "insufficient balance",
                    "error_reason": "insufficient_funds",
                    "error_source": "issuer_bank",
                    "error_step": "payment_authorization",
                    "created_at": 1_700_000_500,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _sign(body: bytes) -> str:
    return hmac.new(_WEBHOOK_SECRET.encode("utf-8"), body, sha256).hexdigest()


def _one(engine: Engine, sql: str, params: dict[str, object]) -> object:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _decision_only_registry() -> dict[str, Handler]:
    """The real worker registry with the executor replaced by a no-op.

    This is what makes "zero external calls" a *structural* claim here rather than a lucky one.
    The registry is otherwise the production one — every decision step is the real handler, so
    the chaining this checkpoint verifies is the real chaining — and the single kind that can
    reach outside the process is stubbed out. A reader can see from this function alone that no
    provider request is possible, which is a stronger statement than any assertion about counts.

    It also stops the checkpoint from silently changing subject. When execution was wired into
    the worker, this test began running the executor, which reserved an intent and then died on
    an unresolvable credential — leaving the case in ``EXECUTING`` and breaking the very
    "no external effect" assertion below. The decision pipeline is what this file is about;
    execution has its own tests and its own integration coverage.

    A no-op rather than a missing key: an unregistered kind is ``fail``-ed and rescheduled with
    backoff, which is noise in a test that asserts on the audit trail.
    """
    registry = build_registry(provider=None)
    registry[EXECUTION_JOB_KIND] = lambda claimed: None
    return registry


def _drain(worker_id: str) -> int:
    """Run the worker until the queue stops producing work. Returns passes used."""
    registry = _decision_only_registry()
    for used in range(1, _MAX_WORKER_PASSES + 1):
        if run_once(worker_id, registry=registry) == 0:
            return used
    return _MAX_WORKER_PASSES


def test_webhook_to_policy_decision_end_to_end(
    installed_engine: Engine, installed_secrets: None
) -> None:
    """A signed failed payment becomes a recommendation and an authorized decision."""
    engine = installed_engine
    merchant_id, slug = _make_merchant(engine)
    config = default_configuration()

    # Consent first. See `_grant_consent` — the first worker pass runs the pipeline through
    # policy, so consent recorded afterwards is recorded after the decision it governs.
    _grant_consent(engine, merchant_id, crypto.customer_key(_CONTACT))

    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = _failed_payment_body(payment_id, event_id)

    accepted = ingest_webhook(
        merchant_id,
        slug,
        body=body,
        provided_signature=_sign(body),
        provider_event_id=event_id,
        config=config,
        correlation_id=uuid.uuid4(),
    )
    assert accepted.outcome is IngestionOutcome.ACCEPTED

    assert run_once("decision-worker", registry=_decision_only_registry()) >= 1
    _drain("decision-worker")

    # The consent recorded before the case existed is the consent this case's decision saw.
    # Asserted rather than assumed: if the derivation ever diverged, every consent check would be
    # answered about a different person and every one of them would answer "missing".
    assert (
        _one(
            engine,
            "SELECT customer_key FROM recovery_case WHERE merchant_id = :m",
            {"m": str(merchant_id)},
        )
        == crypto.customer_key(_CONTACT)
    )

    # -- the case walked the whole decision pipeline -------------------------
    state = _one(
        engine,
        "SELECT state FROM recovery_case WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    # ``ACTION_SCHEDULED``, not ``POLICY_CHECK``. This expectation moved when execution was
    # wired into the worker, and the move is the point rather than an accommodation: an
    # ``APPROVED`` decision now schedules the action it authorized, where previously it was a
    # durable authorization that nothing consumed. ``POLICY_CHECK`` means "a decision is
    # recorded"; ``ACTION_SCHEDULED`` means "an authorization exists and is waiting to be
    # consumed", and reaching the second proves the policy step enqueued its successor.
    #
    # It stops here because the executor is stubbed out — see `_decision_only_registry`. That is
    # the property this checkpoint exists to assert: the whole decision pipeline runs to
    # completion with zero external calls, and the boundary where the outside world begins is
    # visible in the registry rather than inferred from a count.
    assert state == CaseState.ACTION_SCHEDULED.value, (
        "the decision pipeline must reach ACTION_SCHEDULED; a case stuck earlier means a step "
        "did not enqueue its successor"
    )

    # -- diagnosis: deterministic, no AI ------------------------------------
    cause, method, ai_invocation = _row(
        engine,
        "SELECT cause, method, ai_invocation_id FROM diagnosis "
        "WHERE merchant_id = :m AND is_active",
        {"m": str(merchant_id)},
    )
    assert cause == "INSUFFICIENT_FUNDS"
    assert method == "DETERMINISTIC"
    assert ai_invocation is None, "the deterministic path must record no AI invocation"
    assert (
        _one(
            engine,
            "SELECT count(*) FROM ai_invocation WHERE merchant_id = :m",
            {"m": str(merchant_id)},
        )
        == 0
    ), "zero AI invocations on the deterministic path"

    # -- baseline: present, with an honest interval -------------------------
    probability, ci_low, ci_high, uncertainty, validation = _row(
        engine,
        "SELECT probability, ci_low, ci_high, uncertainty_available, validation_status "
        "FROM baseline_estimate WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert uncertainty is True
    assert ci_low is not None and ci_high is not None
    assert ci_low <= probability <= ci_high
    assert validation in ("UNVALIDATED_BASELINE", "CALIBRATION_UNVERIFIED"), (
        "a baseline nothing has been checked against must never claim VALIDATED"
    )

    # -- candidates: the null actions present, unavailable ones retained ----
    actions = {
        row[0]
        for row in _rows(
            engine,
            "SELECT action FROM candidate_estimate WHERE merchant_id = :m",
            {"m": str(merchant_id)},
        )
    }
    assert CandidateAction.DO_NOTHING.value in actions
    assert CandidateAction.WAIT.value in actions
    assert (
        _one(
            engine,
            "SELECT count(*) FROM candidate_estimate "
            "WHERE merchant_id = :m AND availability = 'UNAVAILABLE' "
            "AND unavailable_reason IS NULL",
            {"m": str(merchant_id)},
        )
        == 0
    ), "an unavailable candidate must always say why"

    # -- recommendation: a reason either way, every candidate recorded ------
    recommendation_id, selected_action, selection_reason = _row(
        engine,
        "SELECT id, selected_action, selection_reason FROM recommendation "
        "WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert selection_reason in {reason.value for reason in SelectionReason}
    assert selected_action in actions, (
        "the selected action must be one of the candidates that was actually priced"
    )
    recorded_candidates = _one(
        engine,
        "SELECT count(*) FROM recommendation_candidate WHERE recommendation_id = :r",
        {"r": str(recommendation_id)},
    )
    assert recorded_candidates == len(actions), (
        "every candidate considered must be recorded, excluded ones included"
    )
    assert (
        _one(
            engine,
            "SELECT count(*) FROM recommendation_candidate "
            "WHERE recommendation_id = :r AND excluded <> (exclusion_reason IS NOT NULL)",
            {"r": str(recommendation_id)},
        )
        == 0
    ), "an exclusion always has a reason and a reason implies an exclusion"

    # DO_NOTHING is definitionally neutral, end to end and not just in the unit test.
    do_nothing_net, do_nothing_expected = _row(
        engine,
        "SELECT net_recovery_value, expected_incremental_revenue "
        "FROM recommendation_candidate WHERE recommendation_id = :r AND action = 'DO_NOTHING'",
        {"r": str(recommendation_id)},
    )
    assert do_nothing_net == 0
    assert do_nothing_expected == 0

    # -- policy: one decision, twelve ordered check rows --------------------
    decision_id, verdict, primary_reason, rule_version, idempotency_key = _row(
        engine,
        "SELECT id, verdict, primary_reason, rule_set_version, idempotency_key "
        "FROM policy_decision WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    assert verdict in {v.value for v in PolicyVerdict}
    assert primary_reason
    assert rule_version, "a decision must name the rules it was judged against"

    orders = [
        row[0]
        for row in _rows(
            engine,
            "SELECT check_order FROM policy_check_result "
            "WHERE policy_decision_id = :d ORDER BY check_order",
            {"d": str(decision_id)},
        )
    ]
    assert orders == list(range(1, 13)), "all twelve checks, in the fixed order, always"

    # An idempotency key names an external effect, so only an approval carries one.
    if verdict == PolicyVerdict.APPROVED.value:
        assert idempotency_key is not None
        assert len(idempotency_key) <= 40
    else:
        assert idempotency_key is None

    # -- zero external effects exist ---------------------------------------
    assert (
        _one(
            engine,
            "SELECT count(*) FROM execution_intent WHERE merchant_id = :m",
            {"m": str(merchant_id)},
        )
        == 0
    ), "the decision pipeline must issue no external effect"

    # -- the audit trail is gap-free across every step ---------------------
    seqs = [
        row[0]
        for row in _rows(
            engine,
            "SELECT seq FROM audit_record WHERE merchant_id = :m AND case_id IS NOT NULL "
            "ORDER BY seq",
            {"m": str(merchant_id)},
        )
    ]
    assert seqs == list(range(1, len(seqs) + 1)), "per-case audit sequence must be gap-free"
    assert (
        _one(
            engine,
            "SELECT count(*) FROM audit_record WHERE merchant_id = :m "
            "AND event_type = 'POLICY_DECISION_RECORDED'",
            {"m": str(merchant_id)},
        )
        == 1
    )


def test_pipeline_is_idempotent_under_a_replayed_drain(
    installed_engine: Engine, installed_secrets: None
) -> None:
    """Draining the queue again produces no second recommendation and no second decision.

    Every pipeline step can be retried by the queue after a worker crash, so each one has
    to be idempotent. This checks the property that matters at the end: a second full drain
    changes no counts.
    """
    engine = installed_engine
    merchant_id, slug = _make_merchant(engine)
    config = default_configuration()

    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = _failed_payment_body(payment_id, event_id)
    ingest_webhook(
        merchant_id,
        slug,
        body=body,
        provided_signature=_sign(body),
        provider_event_id=event_id,
        config=config,
        correlation_id=uuid.uuid4(),
    )
    run_once("replay-worker")
    customer_key = _one(
        engine,
        "SELECT customer_key FROM recovery_case WHERE merchant_id = :m",
        {"m": str(merchant_id)},
    )
    _grant_consent(engine, merchant_id, str(customer_key))
    _drain("replay-worker")

    counts_before = _pipeline_counts(engine, merchant_id)
    _drain("replay-worker-second")
    assert _pipeline_counts(engine, merchant_id) == counts_before


def _pipeline_counts(engine: Engine, merchant_id: uuid.UUID) -> dict[str, int]:
    """Row counts for every table the pipeline writes, for an idempotency comparison."""
    tables = (
        "recovery_case",
        "diagnosis",
        "baseline_estimate",
        "candidate_estimate",
        "recommendation",
        "recommendation_candidate",
        "policy_decision",
        "policy_check_result",
    )
    return {
        table: int(
            _one(  # type: ignore[arg-type]
                engine,
                f"SELECT count(*) FROM {table} WHERE merchant_id = :m",
                {"m": str(merchant_id)},
            )
        )
        for table in tables
    }


def _row(engine: Engine, sql: str, params: dict[str, object]) -> tuple[object, ...]:
    with engine.connect() as connection:
        return tuple(connection.execute(text(sql), params).one())


def _rows(
    engine: Engine, sql: str, params: dict[str, object]
) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params).all()]

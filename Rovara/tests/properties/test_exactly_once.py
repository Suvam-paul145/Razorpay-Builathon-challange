"""Feature: Execution_Engine. Property 3 — at most one external effect per key.

The property, stated so it can fail: **for any sequence of crashes, restarts and
reconciliation runs, the provider receives at most one create call per
``Idempotency_Key``, every subsequent request for that key returns the same recorded
result, and the case counters move at most once.**

This is the property the whole design is arranged around, and it is the one whose failure
is worst: a duplicate create is a real customer being asked to pay the same invoice twice.
It is written before the execution engine exists, which is the point — an exactly-once
guarantee retro-fitted to code that already passes its own tests tends to be a guarantee
about the tests.

Three things make this test able to fail honestly.

**Real Postgres.** The guarantee is ultimately
``uq_execution_intent_merchant_id_idempotency_key`` plus an ``ON CONFLICT DO NOTHING``
insert that commits before the call. Both are database behaviours. Against a fake session
this file would assert that the code calls the functions it calls.

**A ground-truth oracle the engine cannot see.** ``FakeRazorpay.created_link_exists`` says
whether the effect really exists, independent of what the engine managed to record. Without
it the test could only compare the engine against itself, and an engine that confidently
records ``FAILED`` for a link that exists would pass.

**The indistinguishable pair.** ``TIMEOUT_EFFECT_CREATED`` and ``TIMEOUT_NO_EFFECT`` return
byte-identical results to the caller and differ only in whether the link exists. Any
implementation that decides whether to retry by looking at the result alone gets one of the
two wrong, and the generator will find it.

A note on what is *not* asserted: the test does not require that an effect is eventually
created. Refusing to act is always permitted — a policy check may block, reconciliation may
give up and escalate. The property is one-sided by design, because "at least once" and "at
most once" have opposite failure modes and only one of them charges a customer twice.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, DiagnosisMethod, IntentState, RiskCause
from revora.domain.keys import execution_key
from revora.execution.engine import ExecutionOutcome, execute_approved_action
from revora.execution.reconcile import promote_stale_intents, reconcile_intents
from revora.platform import crypto
from revora.platform.clock import ManualClock, now
from revora.platform.config import default_configuration
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour
from tests.strategies.crashes import (
    CrashingProvider,
    CrashInjected,
    CrashPlan,
    CrashPoint,
    crash_on_statement,
    crash_plan,
)

pytestmark = pytest.mark.pg

_ATTEMPT_ORDINAL = 1
"""Property 3 is about one key, so one ordinal. A second ordinal is a *different* key and
is permitted a second effect — that is what "advancing only on a further APPROVED decision"
means. Testing both in one property would blur the two.
"""

_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
"""Forty examples, not the profile's hundred: each one seeds a merchant, a case and a
decision, then drives up to seven transactions against a real database. Forty covers every
crash point against every interesting provider outcome several times over, and the
generator is deterministic enough that raising it mostly re-runs shapes already seen.

``function_scoped_fixture`` is suppressed for one reason and it is worth naming: the root
conftest installs an autouse function-scoped ``_restore_real_clock``. Hypothesis cannot
know that it only resets a module-level global, so it warns. Nothing in this file depends
on per-example fixture setup — every row is created inside the example body.
"""


# ---------------------------------------------------------------------------
# Seeding. Fresh rows per example, so one example cannot contaminate another.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One example's universe: its own merchant, case, approved decision and key."""

    merchant_id: uuid.UUID
    case_id: uuid.UUID
    decision_id: uuid.UUID
    idempotency_key: str


def _seed(engine: Engine) -> _Scenario:
    """A complete, executable scenario: everything the twelve policy checks need to pass.

    The case is seeded in ``ACTION_SCHEDULED`` because that is the only state with an edge
    into ``EXECUTING``, and that edge is the one carrying the counter effects the property
    checks. Seeding anywhere else would make the transition illegal and the test would pass
    for the wrong reason — no execution, therefore no duplicate.

    Every row here is load-bearing, and the reason is worth recording because the first
    version of this seed silently made the whole property vacuous. The engine re-evaluates
    policy against reloaded state before it acts, and a policy check with no data to read
    returns ``UNAVAILABLE``, which becomes ``BLOCKED`` — there is deliberately no
    assume-fine branch. So a scenario missing a diagnosis or a consent row never reaches the
    provider at all, and every "at most one create call" assertion passes with zero calls.
    ``test_the_crash_harness_actually_crashes`` exists to catch exactly that.

    * **consent**, ``opted_out`` false, no expiry — check 5 refuses an absent consent record
      rather than assuming permission.
    * **diagnosis**, active, for the decision's cycle — checks 4 and 10 both read the cause,
      and ``UNKNOWN`` is a legitimate value but a missing row is not.
    * **webhook_event** with an encrypted payload — the engine decrypts the contact
      just-in-time from this row, and refuses if it cannot. A case with no source event has
      nobody to send a link to.
    * **decision** carrying ``idempotency_key`` — the design mints the key at decision time
      and consumes it at execution; letting execution invent one would test a different
      mechanism than the one that ships.
    """
    merchant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    key = execution_key(case_id, CandidateAction.PAYMENT_LINK.value, _ATTEMPT_ORDINAL)
    moment = now()

    customer_key = f"ck-{case_id}"
    event_id = uuid.uuid4()

    # The contact the engine will decrypt. A documentation-range number and an example.com
    # address: this is test data that must never be a way to reach a real person.
    encrypted = payload_cipher().encrypt(
        json.dumps(
            {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_{case_id.hex[:14]}",
                            "entity": "payment",
                            "status": "failed",
                            "contact": "+919000090000",
                            "email": "p3-test@example.com",
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
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'P3 merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"p3-{merchant_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, raw_payload_nonce, key_version,
                    canonical, correlation_id, signature_verified, received_at, created_at
                ) VALUES (
                    :id, :merchant_id, :provider_event_id, 'payment.failed',
                    :ciphertext, :nonce, :key_version,
                    '{}'::jsonb, :correlation_id, true, :received_at, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "provider_event_id": f"evt_{case_id.hex[:16]}",
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "correlation_id": str(uuid.uuid4()),
                "received_at": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, source_event_id, detected_at, window_end_at,
                    created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 250000,
                    'INR', :customer_key, :source_event_id, :detected_at, :window_end_at,
                    now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": CaseState.ACTION_SCHEDULED.value,
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": customer_key,
                "source_event_id": str(event_id),
                "detected_at": moment,
                "window_end_at": moment + timedelta(hours=168),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    id, merchant_id, customer_key, opted_out, source, effective_at,
                    created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :customer_key, false, 'test',
                    :effective_at, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "customer_key": customer_key,
                "effective_at": moment - timedelta(days=1),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :cause, 0.90,
                    :method, 1, true, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "cause": RiskCause.INSUFFICIENT_FUNDS.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, idempotency_key, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, :action, :state, 1, :key, now()
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
                "state": CaseState.ACTION_SCHEDULED.value,
                "key": key,
            },
        )

    return _Scenario(merchant_id, case_id, decision_id, key)


# ---------------------------------------------------------------------------
# Reading the durable record back. Raw SQL on purpose: the ORM identity map can
# report a value that was never committed, which is the one thing this must not do.
# ---------------------------------------------------------------------------


def _intent_rows(engine: Engine, scenario: _Scenario) -> Sequence[dict[str, object]]:
    """Every intent for the scenario's key, as committed."""
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, state, provider_response_id, provider_failure_code,
                       counter_applied, attempt_ordinal, resolved_at
                FROM execution_intent
                WHERE merchant_id = :merchant_id AND idempotency_key = :key
                ORDER BY created_at
                """
            ),
            {"merchant_id": str(scenario.merchant_id), "key": scenario.idempotency_key},
        )
        return [dict(row._mapping) for row in rows]


def _case_counters(engine: Engine, scenario: _Scenario) -> dict[str, object]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT state, executed_action_count, customer_message_count, version
                FROM recovery_case WHERE id = :case_id
                """
            ),
            {"case_id": str(scenario.case_id)},
        ).one()
        return dict(row._mapping)


# ---------------------------------------------------------------------------
# Driving one plan to completion.
# ---------------------------------------------------------------------------


def _run_plan(
    engine: Engine,
    factory: sessionmaker[Session],
    scenario: _Scenario,
    plan: CrashPlan,
    fake: FakeRazorpay,
) -> None:
    """The crashing attempt, then the restarts, then the reconciliation runs.

    ``CrashInjected`` is caught here and nowhere else. Catching it is what makes this a
    restart rather than a test failure: the worker died, the harness notices, and the next
    iteration is a fresh worker picking the job back up. It derives from ``BaseException``
    so nothing inside the engine can catch it first.
    """
    crashing = CrashingProvider(fake, plan.point)

    # The attempt that dies. A transaction-boundary crash is enacted by the SQL
    # listener; a call-adjacent one by the provider wrapper. Both are one-shot.
    with crash_on_statement(engine, plan.point), suppress(CrashInjected):
        execute_approved_action(
            scenario.merchant_id,
            scenario.case_id,
            provider=crashing,
            factory=factory,
        )

    # Restarts. The crash is spent, so these run against an honest provider and must
    # reach the same conclusion no matter how many times they run.
    for _ in range(plan.restarts):
        with suppress(CrashInjected):  # pragma: no cover - the crash is one-shot
            execute_approved_action(
                scenario.merchant_id,
                scenario.case_id,
                provider=fake,
                factory=factory,
            )

    # Reconciliation, which is the only path allowed to resolve an unresolved intent.
    for _ in range(plan.reconciliation_runs):
        reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """Sessions on the migrated database. Session-scoped engine, so no per-example setup."""
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


class _Resolver:
    """A secret resolver over a fixed mapping. No environment, no file."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    """A payload encryption key, so the seed can encrypt and the engine can decrypt.

    Autouse, because every test in this module seeds a webhook payload and the engine
    decrypts the contact from it before it will act. Without a key the engine refuses with
    ``CONTACT_UNAVAILABLE`` and every "at most one create" assertion passes having made zero
    calls — vacuously true, which is the failure mode this whole file is arranged against.

    A fixed test key, never the real one from ``.env``: this test writes ciphertext into a
    database that persists across runs, and encrypting throwaway fixtures under the
    production key would put real key material to work on data nobody is tracking.
    """
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"P" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"3" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


# ---------------------------------------------------------------------------
# The property.
# ---------------------------------------------------------------------------


@_SETTINGS
@given(plan=crash_plan())
def test_at_most_one_external_effect_per_idempotency_key(
    owner_engine: Engine, factory: sessionmaker[Session], plan: CrashPlan
) -> None:
    """Property 3. No crash, restart or reconciliation sequence produces a second effect.

    Five assertions, each one a way the guarantee could break in production:

    1. **At most one create call per key.** The headline. Two calls is a customer asked to
       pay twice.
    2. **At most one intent row per key.** The database-level guarantee behind it. If this
       fails the unique constraint is not doing its job and assertion 1 passed by luck.
    3. **An effect that exists is never recorded as FAILED.** ``FAILED`` licenses a further
       attempt under a new key, so recording it while a payable link is live is how one
       link becomes two by a legitimate-looking route.
    4. **A confirmed intent carries a provider id.** The design permits presenting an
       action as successful only while the intent holds ``CONFIRMED`` with a persisted
       provider id. A ``CONFIRMED`` row without one would let the dashboard claim a link
       exists that nothing can produce.
    5. **Counters move at most once.** The bounds that cap customer contact are enforced by
       these counters; a crash that double-counts silently tightens them, and one that
       under-counts silently loosens them past the point a merchant agreed to.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(plan.behaviour)

    _run_plan(owner_engine, factory, scenario, plan, fake)

    key = scenario.idempotency_key
    creates = fake.create_call_count_for(key)
    rows = _intent_rows(owner_engine, scenario)
    counters = _case_counters(owner_engine, scenario)

    assert creates <= 1, (
        f"{creates} create calls for one idempotency key under {plan.point.value}; "
        "a customer would be asked to pay twice"
    )
    assert len(rows) <= 1, (
        f"{len(rows)} intent rows for one key — the unique constraint did not hold"
    )

    if rows:
        row = rows[0]
        state = IntentState(str(row["state"]))

        if fake.created_link_exists(key):
            assert state is not IntentState.FAILED, (
                "recorded FAILED while a payable link exists on the provider; "
                "this licenses a second attempt against a live link"
            )

        if state is IntentState.CONFIRMED:
            assert row["provider_response_id"] is not None, (
                "CONFIRMED without a provider id — nothing may be presented as successful"
            )
            assert row["resolved_at"] is not None, "CONFIRMED without a resolution time"

    assert int(counters["executed_action_count"]) <= 1, (
        f"executed_action_count reached {counters['executed_action_count']} for one "
        "attempt; the bounds that cap customer contact are computed from this"
    )
    assert int(counters["customer_message_count"]) <= 1, (
        f"customer_message_count reached {counters['customer_message_count']} for one "
        "attempt"
    )


@_SETTINGS
@given(plan=crash_plan())
def test_the_recorded_result_for_a_key_never_changes(
    owner_engine: Engine, factory: sessionmaker[Session], plan: CrashPlan
) -> None:
    """Every later request for a resolved key returns the result already recorded.

    Separate from the count assertion because it catches a different bug. An engine could
    hold the create count at one and still let a later run overwrite ``CONFIRMED`` with
    ``FAILED`` — at which point the case is eligible for a fresh attempt, the dashboard
    stops showing a link that is still live, and the guarantee is gone without a second
    call ever being made. Stability of the record *is* the guarantee; the call count is
    only its most visible symptom.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(plan.behaviour)

    _run_plan(owner_engine, factory, scenario, plan, fake)

    before = _intent_rows(owner_engine, scenario)
    creates_before = fake.create_call_count_for(scenario.idempotency_key)

    # Drive it again, hard. Nothing about the durable record may move.
    for _ in range(2):
        execute_approved_action(
            scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
        )
        reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)

    after = _intent_rows(owner_engine, scenario)
    creates_after = fake.create_call_count_for(scenario.idempotency_key)

    assert creates_after == creates_before, (
        f"re-running after resolution issued {creates_after - creates_before} further "
        "create call(s)"
    )

    resolved = {IntentState.CONFIRMED, IntentState.FAILED}
    if before and IntentState(str(before[0]["state"])) in resolved:
        assert after == before, (
            "the recorded result for a resolved key changed on a later run: "
            f"{before[0]} became {after[0] if after else None}"
        )


@_SETTINGS
@given(plan=crash_plan())
def test_an_effect_created_during_a_crash_is_found_rather_than_repeated(
    owner_engine: Engine, factory: sessionmaker[Session], plan: CrashPlan
) -> None:
    """Where the link exists but the engine never heard so, it is discovered by reading.

    This is the scenario the reconciliation read exists for, and the one where the naive
    implementation is most tempting: the engine has a durable ``ATTEMPTED`` intent, no
    provider id, and a customer waiting. Calling create again would resolve the case
    immediately and would be wrong.

    Only asserted where the link actually exists, and note the asymmetry — the assertion is
    "no second create", never "must reach CONFIRMED". Reconciliation is allowed to run out
    of attempts and escalate to a human with the intent still ``UNCERTAIN``; a link it could
    not verify is a bad outcome but an honest one. A second link is neither.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(plan.behaviour)

    _run_plan(owner_engine, factory, scenario, plan, fake)

    key = scenario.idempotency_key
    if not fake.created_link_exists(key):
        return

    # However many further reconciliation passes run, the link is read, never remade.
    for _ in range(3):
        reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)

    assert fake.create_call_count_for(key) <= 1, (
        "reconciliation issued a second create for a link that already existed"
    )

    rows = _intent_rows(owner_engine, scenario)
    if rows:
        assert IntentState(str(rows[0]["state"])) is not IntentState.FAILED, (
            "reconciliation concluded FAILED against a link that exists"
        )


@_SETTINGS
@given(plan=crash_plan())
def test_no_crash_point_leaves_an_effect_the_record_denies(
    owner_engine: Engine, factory: sessionmaker[Session], plan: CrashPlan
) -> None:
    """The durable record and the external world agree, or the record admits uncertainty.

    The invariant that ties the other three together. For every key there are exactly three
    honest end states:

    * no effect and no resolved intent, or an intent resolved ``FAILED``;
    * an effect and an intent resolved ``CONFIRMED``;
    * an effect whose existence is not yet established, and an intent that says so —
      ``ATTEMPTED`` or ``UNCERTAIN``.

    What is forbidden is a *confident* record that contradicts reality. A system that says
    ``FAILED`` over a live link will act again; a system that says ``CONFIRMED`` over
    nothing will tell a merchant money is on its way that no customer can pay.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(plan.behaviour)

    _run_plan(owner_engine, factory, scenario, plan, fake)

    rows = _intent_rows(owner_engine, scenario)
    if not rows:
        # No durable intent means no call was permitted to happen.
        assert fake.create_call_count_for(scenario.idempotency_key) == 0, (
            "a create call was issued with no committed intent behind it — the intent "
            "must be durable before the call, or a crash loses the record of it"
        )
        return

    state = IntentState(str(rows[0]["state"]))
    effect_exists = fake.created_link_exists(scenario.idempotency_key)

    if state is IntentState.CONFIRMED:
        assert effect_exists, "CONFIRMED with no effect on the provider side"
    elif state is IntentState.FAILED:
        assert not effect_exists, "FAILED with a live effect on the provider side"
    else:
        assert state in (IntentState.ATTEMPTED, IntentState.UNCERTAIN), (
            f"unresolved intent in unexpected state {state}"
        )


def test_the_uncrashed_path_produces_exactly_one_effect(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The complementary check the properties deliberately do not make.

    Every property above is one-sided: *at most* one effect. That asymmetry is correct —
    refusing to act is always permitted, and "at least once" would be a wrong requirement for
    a system whose policy engine is allowed to say no. But one-sidedness means all four would
    still pass against an engine that never acted at all, and an engine that never acts
    recovers no revenue.

    So this pins the other side once, on the happy path, with no crash: given a case with
    everything policy needs, the engine issues exactly one create, records a provider id, and
    moves the case to ``WAITING_FOR_OUTCOME``. Example-based rather than generated, because
    there is one happy path and nothing to explore.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay()

    attempt = execute_approved_action(
        scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
    )

    assert attempt.outcome is ExecutionOutcome.CONFIRMED, (
        f"the happy path did not confirm: {attempt.outcome.value} / {attempt.detail}"
    )
    assert attempt.made_external_call
    assert attempt.provider_response_id is not None
    assert fake.create_call_count_for(scenario.idempotency_key) == 1
    assert fake.created_link_exists(scenario.idempotency_key)

    rows = _intent_rows(owner_engine, scenario)
    assert len(rows) == 1
    assert IntentState(str(rows[0]["state"])) is IntentState.CONFIRMED
    assert rows[0]["counter_applied"] is True

    counters = _case_counters(owner_engine, scenario)
    assert counters["state"] == CaseState.WAITING_FOR_OUTCOME.value
    assert int(counters["executed_action_count"]) == 1
    # PAYMENT_LINK is customer-visible, so it consumes one of the message allowance too.
    assert int(counters["customer_message_count"]) == 1

    # Running it again changes nothing: the key is resolved, so the recorded result is
    # returned and no second link is created.
    again = execute_approved_action(
        scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
    )
    assert not again.made_external_call
    assert fake.create_call_count_for(scenario.idempotency_key) == 1


def test_a_timeout_with_the_effect_created_reconciles_without_a_second_link(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Checkpoint 23's named scenario, pinned as an example rather than left to the generator.

    The provider creates the link and the response never arrives. To the engine this is
    byte-identical to a timeout where nothing was created — the two cannot be told apart from
    the result — so the only correct move is to read, and the read must confirm.

    Asserted explicitly because it is the single scenario the whole package exists for, and a
    generated property that happens to cover it is weaker evidence than a test that names it.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.timeout_with_effect_created())

    attempt = execute_approved_action(
        scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
    )
    assert attempt.outcome is ExecutionOutcome.UNCERTAIN, attempt.outcome.value
    assert fake.created_link_exists(scenario.idempotency_key), (
        "the fake was asked to create the effect and did not"
    )
    assert fake.create_call_count_for(scenario.idempotency_key) == 1

    # Reconciliation reads and confirms. It must not create.
    results = reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)

    assert fake.create_call_count_for(scenario.idempotency_key) == 1, (
        "reconciliation issued a second create for a link that already existed"
    )
    rows = _intent_rows(owner_engine, scenario)
    assert len(rows) == 1
    assert IntentState(str(rows[0]["state"])) is IntentState.CONFIRMED, (
        f"reconciliation left the intent at {rows[0]['state']} despite the link existing; "
        f"sweep returned {[r.outcome.value for r in results]}"
    )
    assert rows[0]["provider_response_id"] is not None
    assert int(_case_counters(owner_engine, scenario)["executed_action_count"]) == 1


def test_startup_promotion_routes_an_interrupted_attempt_to_a_read(
    owner_engine: Engine, factory: sessionmaker[Session], manual_clock: ManualClock
) -> None:
    """Task 20.6: a restart resolves an interrupted attempt by reading, never by calling.

    The scenario is a worker killed after committing its intent and issuing its call. On
    restart the intent is still ``ATTEMPTED``, and the tempting fix — "no provider id, so try
    again" — is the duplicate. Promotion to ``UNCERTAIN`` is what routes it to a read instead.

    The clock is advanced past ``PROVIDER_CALL_TIMEOUT`` rather than the timeout being
    shortened, because staleness is the actual precondition and a test that reached the same
    branch by another route would not prove the precondition is checked.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.timeout_with_effect_created())

    crashing = CrashingProvider(fake, CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT)
    with pytest.raises(CrashInjected):
        execute_approved_action(
            scenario.merchant_id, scenario.case_id, provider=crashing, factory=factory
        )

    rows = _intent_rows(owner_engine, scenario)
    assert len(rows) == 1
    assert IntentState(str(rows[0]["state"])) is IntentState.ATTEMPTED, (
        "a crash after the call must leave the intent ATTEMPTED, not resolved"
    )
    creates_before = fake.create_call_count_for(scenario.idempotency_key)

    # Restart, once the attempt is unambiguously abandoned.
    manual_clock.advance(default_configuration().PROVIDER_CALL_TIMEOUT + timedelta(seconds=1))
    promoted = promote_stale_intents(scenario.merchant_id, factory=factory)

    assert len(promoted) == 1, "the interrupted intent was not promoted"
    assert fake.create_call_count_for(scenario.idempotency_key) == creates_before, (
        "startup promotion issued a provider call; it must only change state"
    )
    after = _intent_rows(owner_engine, scenario)
    assert IntentState(str(after[0]["state"])) is IntentState.UNCERTAIN
    assert int(_case_counters(owner_engine, scenario)["executed_action_count"]) == 1, (
        "promotion moved a counter; counters stay put until the resolution persists"
    )

    # And the read that follows finds the effect the interrupted attempt created.
    reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)
    assert fake.create_call_count_for(scenario.idempotency_key) == creates_before
    settled = _intent_rows(owner_engine, scenario)
    assert IntentState(str(settled[0]["state"])) is IntentState.CONFIRMED


def test_the_crash_harness_actually_crashes(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The harness's own sanity check, and not optional.

    Every assertion above is of the form "no second effect". All of them pass trivially
    against a crash that never fired — and a listener whose SQL pattern stopped matching, or
    a provider wrapper that stopped being consulted, would fail silently and leave four
    green tests asserting nothing. This pins both mechanisms to observable behaviour.
    """
    scenario = _seed(owner_engine)
    fake = FakeRazorpay()

    # The SQL-boundary mechanism fires on the intent insert.
    with (
        crash_on_statement(owner_engine, CrashPoint.BEFORE_INTENT_COMMIT) as fired,
        pytest.raises(CrashInjected),
    ):
        execute_approved_action(
            scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
        )
    assert fired, "the crash listener never matched an insert into execution_intent"
    assert _intent_rows(owner_engine, scenario) == [], (
        "an intent survived a crash before its transaction committed"
    )
    assert fake.create_call_count_for(scenario.idempotency_key) == 0, (
        "a create call was issued even though the intent never committed"
    )

    # The provider mechanism fires around the call.
    other = _seed(owner_engine)
    crashing = CrashingProvider(fake, CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT)
    with pytest.raises(CrashInjected):
        execute_approved_action(
            other.merchant_id, other.case_id, provider=crashing, factory=factory
        )
    assert crashing.fired, "the crashing provider never fired"

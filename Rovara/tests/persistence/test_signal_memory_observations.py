"""Task 48 against a real PostgreSQL: what a signal writes, and what it must not move.

``tests/persistence/test_recovery_memory.py`` covers the observation writer's existing contract
and this file covers what task 48 added to it. They are separate files because the questions are
separate — that one asks "is the row written atomically and is the label right", this one asks
"do the Customer_Signal fields land, are they selectable, and did adding them move a baseline".

Four things are checked and each needs a real server for a different reason.

**The training label.** R25.C4's clause fires on a case whose intent never resolved, so the test
has to build an ``ATTEMPTED`` intent, a signal submitted after it, and a control-arm assignment,
and then read what the writer decided. The classification rule itself is checked in the pure tier;
what is checked here is that the writer reads the right instants off the right rows.

**The consequence for the estimator.** This is the assertion that makes R25.C4 worth anything:
``SegmentObservationRepository.segment_counts`` must not count the disqualified observation. It
communicates with the writer through nothing but ``intervention_status`` and JSONB containment, so
a rule applied at write time and a filter applied at read time can disagree and neither will
raise.

**Selectability.** R25.C3 asks for each Delay_Reason value to be selectable as a distinct segment,
and "selectable" is a claim about a ``@>`` operator. Only a real server can answer it, and a
Python-side dictionary comparison would pass while the containment query returned nothing.

**The two reports.** ``training_set_composition`` and ``compute_metrics`` are ``GROUP BY``\\ s over
JSONB extraction and a ``DISTINCT ON``, none of which a fake session evaluates.
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

from revora.cases.manager import apply_transition
from revora.customer.suppression import suppression_scope_key
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    DiagnosisMethod,
    ExperimentGroup,
    IntentState,
    InterventionStatus,
    PromiseStatus,
    Provenance,
    RiskCause,
    TerminalReason,
)
from revora.domain.keys import execution_key
from revora.memory.store import (
    FEATURE_CUSTOMER_SIGNALS,
    FEATURE_DELAY_REASON,
    FEATURE_PROMISE_STATUS,
    observation_writer,
)
from revora.memory.versions import training_set_composition
from revora.metrics.engine import ReportingPeriod, compute_metrics
from revora.persistence.repositories.estimates import SegmentObservationRepository
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store

pytestmark = pytest.mark.pg


class _Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    """The payload cipher and the customer-key hasher, both needed to seed a case."""
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"M" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"E" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"E" * 32).decode(),
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
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_merchant(engine: Engine) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Signal memory merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"sig-{merchant_id}"},
        )
    return merchant_id


def _seed_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    cause: RiskCause = RiskCause.INSUFFICIENT_FUNDS,
    amount: int = 250_000,
    customer_key: str | None = None,
    order_id: str | None = None,
) -> uuid.UUID:
    """One diagnosed case in ``WAITING_FOR_OUTCOME``, ready to be moved to a terminal state.

    ``customer_key`` and ``order_id`` are parameters because the Suppression_Scope is derived
    from them (R21.C8) and one test needs two cases that share a scope — which is the only way to
    exercise "a suppression covers a case that never held a signal of its own".
    """
    case_id = uuid.uuid4()
    event_id = uuid.uuid4()
    moment = now()
    encrypted = payload_cipher().encrypt(
        json.dumps({"event": "payment.failed", "payload": {}}).encode()
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
                    :ct, :nonce, :kv, :canonical, :corr, true, :received, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "eid": f"evt_{case_id.hex[:16]}",
                "ct": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "kv": encrypted.key_version,
                "canonical": json.dumps(
                    {
                        "provider_payment_id": f"pay_{case_id.hex[:14]}",
                        "method": "card",
                        "error_source": "issuer",
                    }
                ),
                "corr": str(uuid.uuid4()),
                "received": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, provider_order_id,
                    payment_amount, currency, customer_key, source_event_id, detected_at,
                    window_end_at, provenance, decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :pid, :oid, :amount, 'INR',
                    :ck, :sid, :detected, :window_end, :prov, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": CaseState.WAITING_FOR_OUTCOME.value,
                "pid": f"pay_{case_id.hex[:14]}",
                "oid": order_id,
                "amount": amount,
                "ck": customer_key or f"ck-{case_id}",
                "sid": str(event_id),
                "detected": moment - timedelta(hours=3),
                "window_end": moment + timedelta(hours=168),
                "prov": Provenance.REAL.value,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :cause, 0.90, :method, 1,
                    true, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "cause": cause.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
    return case_id


def _assign(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID, group: ExperimentGroup
) -> uuid.UUID:
    experiment_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO experiment (
                    id, merchant_id, name, state, primary_metric, allocation_ratio,
                    significance_level, power, required_sample_size_per_group, created_at
                ) VALUES (
                    :id, :merchant_id, :name, 'ACTIVE', 'recovery_rate', '1:1',
                    0.05, 0.80, 100, now()
                )
                """
            ),
            {
                "id": str(experiment_id),
                "merchant_id": str(merchant_id),
                "name": f"exp-{experiment_id}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO experiment_assignment (
                    id, merchant_id, experiment_id, case_id, "group", assigned_at,
                    contaminated, excluded, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :eid, :case_id, :grp, now(),
                    false, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "eid": str(experiment_id),
                "case_id": str(case_id),
                "grp": group.value,
            },
        )
    return experiment_id


def _unresolved_intent(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID, *, started_at: object
) -> None:
    """An intent that reached ``ATTEMPTED`` and never resolved — R25.C4's whole motivation.

    Not ``CONFIRMED``: a confirmed action already disqualifies the observation through the
    pre-existing count, so a test built on one would pass whether R25.C4 was implemented or not.
    ``ATTEMPTED`` is the state where the old rule was silently wrong.
    """
    decision_id = uuid.uuid4()
    key = execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1)
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
                    :ev, :exp, :action, 'ACTION_SCHEDULED', 1, :key, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "ev": started_at,
                "exp": now() + timedelta(minutes=15),
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
                    gen_random_uuid(), :merchant_id, :case_id, :did, :key, :action,
                    1, :state, :started, false, 0, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "did": str(decision_id),
                "key": key,
                "action": CandidateAction.PAYMENT_LINK.value,
                "state": IntentState.ATTEMPTED.value,
                "started": started_at,
            },
        )


def _signal(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    kind: CustomerSignalKind,
    submitted_at: object,
    delay_reason: DelayReason | None = None,
    provenance: Provenance = Provenance.REAL,
) -> uuid.UUID:
    signal_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_signal (
                    id, merchant_id, case_id, token_id, kind, delay_reason,
                    note_truncated, provenance, submitted_at, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, :token, :kind, :reason,
                    false, :prov, :submitted, now()
                )
                """
            ),
            {
                "id": str(signal_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "token": f"tok_{signal_id.hex[:12]}",
                "kind": kind.value,
                "reason": None if delay_reason is None else delay_reason.value,
                "prov": provenance.value,
                "submitted": submitted_at,
            },
        )
    return signal_id


def _promise(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    signal_id: uuid.UUID,
    status: PromiseStatus,
    seconds_to_payment: int | None,
) -> None:
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO promise_to_pay (
                    id, merchant_id, case_id, customer_signal_id, promise_date,
                    received_representation, status, window_end_at_snapshot, recorded_at,
                    kept_at, seconds_promise_to_payment, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :sid, :promise_date,
                    :repr, :status, :window_end, :recorded, :kept, :seconds, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "sid": str(signal_id),
                "promise_date": moment + timedelta(hours=48),
                "repr": (moment + timedelta(hours=48)).isoformat(),
                "status": status.value,
                "window_end": moment + timedelta(hours=168),
                "recorded": moment,
                "kept": moment + timedelta(hours=49) if status is PromiseStatus.KEPT else None,
                "seconds": seconds_to_payment,
            },
        )


def _suppress(
    engine: Engine,
    merchant_id: uuid.UUID,
    origin_case_id: uuid.UUID,
    *,
    signal_id: uuid.UUID,
    scope_key: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO contact_suppression (
                    id, merchant_id, scope_key, origin_case_id, customer_signal_id,
                    hard_stop_reason, suppressed_at, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :scope, :case_id, :sid,
                    :reason, now(), now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "scope": scope_key,
                "case_id": str(origin_case_id),
                "sid": str(signal_id),
                "reason": DelayReason.DISPUTES_THE_CHARGE.value,
            },
        )


def _terminate(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    factory: sessionmaker[Session],
    engine: Engine,
    *,
    target: CaseState = CaseState.EXPIRED,
    terminal_reason: TerminalReason = TerminalReason.RECOVERY_WINDOW_ELAPSED,
) -> None:
    with engine.begin() as connection:
        version = int(
            connection.execute(
                text("SELECT version FROM recovery_case WHERE id = :id"),
                {"id": str(case_id)},
            ).scalar_one()
        )
    result = apply_transition(
        merchant_id,
        case_id,
        expected_version=version,
        target_state=target,
        reason="test termination",
        actor="test",
        terminal_reason=terminal_reason,
        factory=factory,
        on_success=observation_writer(default_configuration()),
    )
    assert result.applied, f"transition not applied: {result.outcome.value}"


def _observation(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT features, intervention_status, provenance, "group"
                FROM memory_observation WHERE case_id = :case_id
                """
            ),
            {"case_id": str(case_id)},
        ).one()
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# R25.C4 — the training label, and the estimator's agreement with it
# ---------------------------------------------------------------------------


def test_a_signal_after_an_unresolved_attempt_leaves_the_baseline_unable_to_learn_from_it(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C4 end to end, on the one case shape where the old rule was wrong.

    A control-arm case, an intent stranded in ``ATTEMPTED``, and a Delay_Reason submitted an hour
    after that attempt. Zero *confirmed* actions, so the pre-existing rule would have called this
    a no-intervention observation and handed the baseline a case whose customer demonstrably
    received a message Revora sent.

    Both halves are asserted, and the second is the one with teeth. The label being
    ``REVORA_INTERVENED`` is a fact about a column; the segment aggregate not counting it is the
    fact that decides whether any of this reached the estimator. The two communicate through
    nothing but that column's value and a containment match, so a rule applied at write time and
    a filter applied at read time can disagree without anything raising.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)

    attempt_at = now() - timedelta(hours=4)
    _unresolved_intent(owner_engine, merchant_id, case_id, started_at=attempt_at)
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
        submitted_at=attempt_at + timedelta(hours=1),
    )

    _terminate(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row["intervention_status"] == InterventionStatus.REVORA_INTERVENED.value, (
        "a control case whose customer answered a message Revora sent was recorded as a "
        "no-intervention observation; the baseline would learn the recovery rate of answered "
        "messages and report it as the rate with no intervention at all"
    )
    features = row["features"]
    assert isinstance(features, dict)
    body = features[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["signal_after_revora_action"] is True
    assert body["first_revora_action_at"] is not None

    with factory() as session:
        counts = SegmentObservationRepository(session).segment_counts(
            merchant_id, features={}
        )
    assert counts.observations == 0, (
        "the disqualified observation still counts toward the posterior's n, so R25.C4 changed a "
        "column and nothing else"
    )
    assert counts.resolved_control == 1, (
        "the observation vanished from the control-arm count too, which would report the segment "
        "UNVALIDATED_BASELINE when a control outcome does exist"
    )


def test_a_signal_before_any_action_still_trains_the_baseline(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The clause is about responses, and this is the boundary that proves it.

    A control case whose customer opened the page and stated a reason with no Revora action at
    all is still the one usable training label. R25.C4 must not have quietly become "any signal
    disqualifies" — that would throw away exactly the observations a control arm exists to
    collect, since a control-arm customer who visits the page is behaving like an untreated
    customer and is one.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.BANK_OR_CARD_PROBLEM,
        submitted_at=now() - timedelta(hours=2),
    )

    _terminate(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row["intervention_status"] == InterventionStatus.NO_INTERVENTION_CONFIRMED.value
    features = row["features"]
    assert isinstance(features, dict)
    body = features[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["signal_after_revora_action"] is False

    with factory() as session:
        counts = SegmentObservationRepository(session).segment_counts(
            merchant_id, features={}
        )
    assert counts.observations == 1


# ---------------------------------------------------------------------------
# R25.C1, R25.C2, R25.C3 — the fields, and their selectability
# ---------------------------------------------------------------------------


def test_the_observation_carries_every_r25c1_field_and_each_reason_is_selectable(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C1's six fields, and R25.C3's containment claim answered by the server.

    The containment probe is the load-bearing assertion. A Python-side dictionary comparison
    would pass while ``features @> '{"delay_reason": "..."}'`` matched nothing, and "selectable as
    a distinct segment" is a statement about that operator rather than about the document. It is
    run against the same repository the estimator uses, so what is proved is that the estimator
    *could* segment on a stated reason — while :data:`~revora.domain.segments.FEATURE_KEYS` means
    it does not, which is the separation the pure tier asserts.

    Also asserted: the latest reason wins. The customer states a bank problem and then corrects
    it to a cashflow problem, and the observation records the correction — matching
    ``latest_delay_reason``, which is what R20.C4 diagnoses on. Recording the superseded reason
    would train the baseline on a statement the customer withdrew.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)

    base = now() - timedelta(hours=6)
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.PAGE_VIEWED,
        submitted_at=base,
    )
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.BANK_OR_CARD_PROBLEM,
        submitted_at=base + timedelta(minutes=1),
    )
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
        submitted_at=base + timedelta(minutes=2),
    )
    promise_signal = _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.PROMISE_TO_PAY,
        submitted_at=base + timedelta(minutes=3),
    )
    _promise(
        owner_engine,
        merchant_id,
        case_id,
        signal_id=promise_signal,
        status=PromiseStatus.KEPT,
        seconds_to_payment=3_600,
    )
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST,
        submitted_at=base + timedelta(minutes=4),
    )

    _terminate(merchant_id, case_id, factory, owner_engine)

    features = _observation(owner_engine, case_id)["features"]
    assert isinstance(features, dict)
    assert features[FEATURE_DELAY_REASON] == DelayReason.SALARY_OR_CASHFLOW_TIMING.value, (
        "the observation recorded a Delay_Reason the customer had already corrected"
    )
    assert features[FEATURE_PROMISE_STATUS] == PromiseStatus.KEPT.value

    body = features[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["signal_count"] == 5
    assert body["seconds_promise_to_payment"] == 3_600
    assert body["arrangement_requested"] is True
    assert body["contact_suppressed"] is False

    with factory() as session:
        repository = SegmentObservationRepository(session)
        matched = repository.segment_counts(
            merchant_id,
            features={FEATURE_DELAY_REASON: DelayReason.SALARY_OR_CASHFLOW_TIMING.value},
        )
        unmatched = repository.segment_counts(
            merchant_id,
            features={FEATURE_DELAY_REASON: DelayReason.BANK_OR_CARD_PROBLEM.value},
        )
    assert matched.observations == 1, (
        "a containment probe naming the stated reason matched nothing, so the value is stored "
        "and unselectable — which is the one outcome R25.C3 forbids"
    )
    assert unmatched.observations == 0, (
        "a probe naming a different Delay_Reason matched this observation, so the reasons are "
        "not distinct segments"
    )


def test_a_synthetic_signal_makes_the_observation_synthetic(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C2 applying R15.C2 unchanged, on a case that is otherwise entirely real.

    One generated contributor is enough. The label is the whole basis on which a merchant-facing
    figure may claim to describe real money, so an observation a generator contributed to must
    not be able to carry ``REAL`` however real the payment behind it was.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
    _signal(
        owner_engine,
        merchant_id,
        case_id,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.OTHER,
        submitted_at=now() - timedelta(hours=2),
        provenance=Provenance.SYNTHETIC,
    )

    _terminate(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row["provenance"] == Provenance.SYNTHETIC.value, (
        "a generated Customer_Signal contributed to an observation still labelled REAL"
    )
    features = row["features"]
    assert isinstance(features, dict)
    body = features[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["provenance"] == Provenance.SYNTHETIC.value

    with factory() as session:
        counts = SegmentObservationRepository(session).segment_counts(
            merchant_id, features={}
        )
    assert counts.all_synthetic_free is False, (
        "the estimator would produce a REAL baseline from a segment a generator contributed to"
    )


def test_a_case_with_no_signal_carries_exactly_the_five_segment_features(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The absent form, asserted against the real column rather than against the builder.

    This is what keeps every baseline in an existing deployment unmoved by task 48: an
    observation of a case whose customer never opened the page has the same document it had
    before the feature existed, so no historical segment changed shape and no stored estimate
    became unreproducible.
    """
    merchant_id = _seed_merchant(owner_engine)
    case_id = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)

    _terminate(merchant_id, case_id, factory, owner_engine)

    features = _observation(owner_engine, case_id)["features"]
    assert isinstance(features, dict)
    assert FEATURE_DELAY_REASON not in features
    assert FEATURE_PROMISE_STATUS not in features
    assert FEATURE_CUSTOMER_SIGNALS not in features
    assert all(isinstance(value, str) for value in features.values()), (
        "a non-string feature value entered the document of a case with no signals, which a "
        "containment probe built from string values cannot match"
    )


def test_a_suppression_inherited_from_a_sibling_case_is_recorded(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C1's suppression indication is scope-wide, which R21.C8 makes the only correct reading.

    Two cases of one customer and one order share a Suppression_Scope. The first case's customer
    disputes the charge; the second case never holds a signal of its own and is suppressed
    anyway. An observation that recorded ``contact_suppressed`` as false for the second would
    describe a case Revora was free to chase when it was not — and it would do it on the case
    where a reader is least likely to go looking.
    """
    merchant_id = _seed_merchant(owner_engine)
    customer_key = f"ck-shared-{uuid.uuid4()}"
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    origin_case = _seed_case(
        owner_engine, merchant_id, customer_key=customer_key, order_id=order_id
    )
    sibling_case = _seed_case(
        owner_engine, merchant_id, customer_key=customer_key, order_id=order_id
    )
    signal_id = _signal(
        owner_engine,
        merchant_id,
        origin_case,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.DISPUTES_THE_CHARGE,
        submitted_at=now() - timedelta(hours=2),
    )
    _suppress(
        owner_engine,
        merchant_id,
        origin_case,
        signal_id=signal_id,
        scope_key=suppression_scope_key(
            customer_key=customer_key, order_id=order_id, case_id=origin_case
        ),
    )

    _terminate(
        merchant_id,
        sibling_case,
        factory,
        owner_engine,
        target=CaseState.ESCALATED,
        terminal_reason=TerminalReason.CUSTOMER_DISPUTED_CHARGE,
    )

    features = _observation(owner_engine, sibling_case)["features"]
    assert isinstance(features, dict)
    body = features[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["contact_suppressed"] is True, (
        "a case covered by a suppression its sibling wrote recorded no suppression, so the "
        "training set says Revora was free to contact a customer who had objected"
    )
    assert body["signal_count"] == 0
    assert FEATURE_DELAY_REASON not in features, (
        "the sibling case inherited a Delay_Reason it never held; the suppression is scope-wide "
        "and the stated reason is not"
    )


# ---------------------------------------------------------------------------
# R25.C8 — the composition report
# ---------------------------------------------------------------------------


def test_the_composition_report_counts_reasons_and_names_the_ones_with_none(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C8, and the assertion that matters is the tuple of *absent* values.

    Two cases state two different reasons and a third states nothing. The report counts the two
    and names the four Delay_Reason values nothing has been observed for — which is what an
    approver needs before promoting a model that will estimate those populations from a prior it
    has never checked against an outcome.

    The zero-count case is also why ``by_delay_reason`` sums to less than
    ``observation_count`` rather than carrying a ``NOT_RECORDED`` bucket: the writer omits the key
    entirely for a case that said nothing, so there is no stored value to group.
    """
    merchant_id = _seed_merchant(owner_engine)
    stated = {
        DelayReason.SALARY_OR_CASHFLOW_TIMING: 2,
        DelayReason.BANK_OR_CARD_PROBLEM: 1,
    }
    for reason, count in stated.items():
        for _ in range(count):
            case_id = _seed_case(owner_engine, merchant_id)
            _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
            _signal(
                owner_engine,
                merchant_id,
                case_id,
                kind=CustomerSignalKind.DELAY_REASON,
                delay_reason=reason,
                submitted_at=now() - timedelta(hours=2),
            )
            _terminate(merchant_id, case_id, factory, owner_engine)

    silent_case = _seed_case(owner_engine, merchant_id)
    _assign(owner_engine, merchant_id, silent_case, ExperimentGroup.CONTROL)
    _terminate(merchant_id, silent_case, factory, owner_engine)

    with factory() as session:
        composition = training_set_composition(session, merchant_id)

    assert composition.observation_count == 4
    assert composition.usable_label_count == 4
    assert composition.by_delay_reason == {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: 2,
        DelayReason.BANK_OR_CARD_PROBLEM.value: 1,
    }
    assert sum(composition.by_delay_reason.values()) == 3, (
        "the case that stated nothing was counted under a Delay_Reason it never held"
    )

    expected_missing = tuple(
        sorted(member.value for member in DelayReason if member not in stated)
    )
    assert composition.unobserved_delay_reasons == expected_missing, (
        "the report did not name every Delay_Reason value holding zero observations, which is "
        "the half of R25.C8 an approver actually needs"
    )
    # R15.C12's existing groupings still work, and the null bucket keeps the sums checkable.
    assert sum(composition.by_selected_action.values()) == composition.observation_count
    assert CandidateAction.PAYMENT_LINK.value in composition.unobserved_actions
    assert composition.scoped_to_version is False
    assert composition.as_document()["unobserved_delay_reasons"] == list(expected_missing)


def test_the_composition_report_is_scoped_to_one_merchant(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Tenant isolation on an aggregate, which is where it is easiest to lose.

    A leaked ``GROUP BY`` would put one merchant's recovery history in another's promotion
    review, and it would do it in a report nobody would think to check for that — every number
    would simply be a little larger than it should be.
    """
    first = _seed_merchant(owner_engine)
    second = _seed_merchant(owner_engine)
    for merchant_id, reason in (
        (first, DelayReason.SALARY_OR_CASHFLOW_TIMING),
        (second, DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW),
    ):
        case_id = _seed_case(owner_engine, merchant_id)
        _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
        _signal(
            owner_engine,
            merchant_id,
            case_id,
            kind=CustomerSignalKind.DELAY_REASON,
            delay_reason=reason,
            submitted_at=now() - timedelta(hours=2),
        )
        _terminate(merchant_id, case_id, factory, owner_engine)

    with factory() as session:
        composition = training_set_composition(session, first)

    assert composition.observation_count == 1
    assert composition.by_delay_reason == {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: 1
    }
    assert DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW.value in (
        composition.unobserved_delay_reasons
    ), "another merchant's Delay_Reason was counted as observed for this one"


# ---------------------------------------------------------------------------
# R25.C11 — the cohort report
# ---------------------------------------------------------------------------


def test_the_cohort_report_counts_reasons_promises_arrangements_and_suppressions(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C11's four counts over a reporting period, plus the correction rule.

    The correction case is the one worth stating. One customer states a bank problem and then
    corrects it to a cashflow problem, and the cohort counts the correction — once. The literal
    reading of "cases holding each Delay_Reason value" would count that case twice and the
    column would sum to more cases than the cohort contains, which is a report nobody can put
    beside a money total.

    The suppression count is scope-keyed, so it is computed by the same
    :func:`~revora.customer.suppression.scope_key_for_case` every other caller uses rather than
    by a digest re-expressed in SQL. A second derivation of a scope is a suppression a write and
    a read disagree about.
    """
    merchant_id = _seed_merchant(owner_engine)
    base = now() - timedelta(hours=2)

    corrected = _seed_case(owner_engine, merchant_id)
    _signal(
        owner_engine,
        merchant_id,
        corrected,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.BANK_OR_CARD_PROBLEM,
        submitted_at=base,
    )
    _signal(
        owner_engine,
        merchant_id,
        corrected,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
        submitted_at=base + timedelta(minutes=5),
    )

    promising = _seed_case(owner_engine, merchant_id)
    promise_signal = _signal(
        owner_engine,
        merchant_id,
        promising,
        kind=CustomerSignalKind.PROMISE_TO_PAY,
        submitted_at=base,
    )
    _promise(
        owner_engine,
        merchant_id,
        promising,
        signal_id=promise_signal,
        status=PromiseStatus.MISSED,
        seconds_to_payment=None,
    )

    asking = _seed_case(owner_engine, merchant_id)
    _signal(
        owner_engine,
        merchant_id,
        asking,
        kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST,
        submitted_at=base,
    )

    customer_key = f"ck-supp-{uuid.uuid4()}"
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    disputing = _seed_case(
        owner_engine, merchant_id, customer_key=customer_key, order_id=order_id
    )
    dispute_signal = _signal(
        owner_engine,
        merchant_id,
        disputing,
        kind=CustomerSignalKind.DELAY_REASON,
        delay_reason=DelayReason.DISPUTES_THE_CHARGE,
        submitted_at=base,
    )
    _suppress(
        owner_engine,
        merchant_id,
        disputing,
        signal_id=dispute_signal,
        scope_key=suppression_scope_key(
            customer_key=customer_key, order_id=order_id, case_id=disputing
        ),
    )

    # A case nobody said anything about, so the counts are not simply the cohort size.
    _seed_case(owner_engine, merchant_id)

    period = ReportingPeriod(start=now() - timedelta(days=2), end=now() + timedelta(days=1))
    with factory() as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.case_count == 5
    signals = metrics.signals
    assert signals.by_delay_reason == {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: 1,
        DelayReason.DISPUTES_THE_CHARGE.value: 1,
    }, (
        "the corrected case was counted under both of its stated reasons, so the column sums to "
        "more cases than the cohort holds"
    )
    assert signals.by_promise_status == {PromiseStatus.MISSED.value: 1}
    assert signals.arrangement_request_count == 1
    assert signals.contact_suppression_count == 1
    assert signals.signalling_case_count == 4
    assert sum(signals.by_delay_reason.values()) <= signals.signalling_case_count

    document = metrics.as_document()
    assert document["customer_signal_cohorts"] == signals.as_document()
    # The money figures are untouched by any of this, which is the point of the counts being
    # counts: nothing here feeds a revenue figure.
    assert document["observed_recovered_revenue"] == 0


def test_the_cohort_report_is_segmented_on_the_same_terms_as_every_other_figure(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C11's "segmented on the same terms R12.C10 requires", checked rather than asserted.

    Two cases with different Risk_Causes each state a reason. A report restricted to one cause
    counts one of them, because the counts are computed from the same ``case_ids`` the money
    figures are computed from — so a segment's signal counts and the aggregate's cannot be
    defined differently, which is the failure a second filter would have introduced.
    """
    from revora.metrics.engine import SegmentKey

    merchant_id = _seed_merchant(owner_engine)
    base = now() - timedelta(hours=2)
    for cause, reason in (
        (RiskCause.INSUFFICIENT_FUNDS, DelayReason.SALARY_OR_CASHFLOW_TIMING),
        (RiskCause.BANK_OR_NETWORK_FAILURE, DelayReason.BANK_OR_CARD_PROBLEM),
    ):
        case_id = _seed_case(owner_engine, merchant_id, cause=cause)
        _signal(
            owner_engine,
            merchant_id,
            case_id,
            kind=CustomerSignalKind.DELAY_REASON,
            delay_reason=reason,
            submitted_at=base,
        )

    period = ReportingPeriod(start=now() - timedelta(days=2), end=now() + timedelta(days=1))
    with factory() as session:
        aggregate = compute_metrics(session, merchant_id, period)
        segmented = compute_metrics(
            session,
            merchant_id,
            period,
            segment=SegmentKey(risk_cause=RiskCause.INSUFFICIENT_FUNDS),
        )

    assert aggregate.signals.signalling_case_count == 2
    assert segmented.case_count == 1
    assert segmented.signals.by_delay_reason == {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: 1
    }, "the segment's signal counts describe a different population from its money figures"
    assert segmented.signals.signalling_case_count == 1

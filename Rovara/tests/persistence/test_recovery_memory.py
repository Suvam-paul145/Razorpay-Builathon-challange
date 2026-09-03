"""Task 26: the observation exists because the case ended, in the same transaction.

Two things are being checked, and only one of them is about writing a row.

**The row is written atomically with the terminal transition.** Not "shortly after", not "by a
follow-on job". A case that ends without an observation never gets one, because nothing revisits
a terminal case — so the training set would be biased toward whatever survives crashes, and the
bias would be invisible. The test for this is the one that rolls the transition back: if the
observation write fails, the case must still be non-terminal afterwards.

**The label is right, which matters more than the row existing.** Only
``NO_INTERVENTION_CONFIRMED`` observations may train the baseline, and earning that label needs
a control-arm assignment *and* zero confirmed actions. The most valuable test here is the one
asserting that a *treatment* case which happened to receive no action is **not** labelled
usable — because that is the mistake that would bias the baseline in the direction flattering
every incremental claim built on it, and it is a mistake that produces no error and no
suspicious-looking data.

The final test closes the loop the whole phase exists for: with observations written, the
baseline estimator stops returning the uniform prior. That is the one assertion proving the
writer and reader actually agree on the feature document, which they communicate through JSONB
containment and would otherwise disagree about silently.
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
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    DecisionSource,
    DiagnosisMethod,
    ExperimentGroup,
    IntentState,
    InterventionStatus,
    OutcomeClass,
    Provenance,
    RiskCause,
    TerminalReason,
)
from revora.domain.keys import execution_key
from revora.domain.money import Minor
from revora.domain.segments import SegmentFeatures
from revora.memory.store import classify_intervention_status, observation_writer
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


def _seed(
    engine: Engine,
    *,
    state: CaseState = CaseState.WAITING_FOR_OUTCOME,
    cause: RiskCause = RiskCause.INSUFFICIENT_FUNDS,
    amount: int = 250_000,
    provenance: Provenance = Provenance.REAL,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A merchant and one diagnosed case, ready to be moved to a terminal state."""
    merchant_id = uuid.uuid4()
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
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Memory merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"mem-{merchant_id}"},
        )
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
                # The canonical row is what the observation reads its banding inputs from.
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
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, source_event_id, detected_at, window_end_at, provenance,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :pid, :amount, 'INR',
                    :ck, :sid, :detected, :window_end, :prov, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "pid": f"pay_{case_id.hex[:14]}",
                "amount": amount,
                "ck": f"ck-{case_id}",
                "sid": str(event_id),
                "detected": moment - timedelta(hours=3),
                "window_end": moment + timedelta(hours=168),
                "prov": provenance.value,
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
    return merchant_id, case_id


def _assign(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID, group: ExperimentGroup
) -> uuid.UUID:
    """Put the case in an experiment arm, which is what makes a label usable or not."""
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


def _confirm_action(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """A confirmed execution intent — the thing that makes a case 'intervened'."""
    decision_id = uuid.uuid4()
    key = execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1)
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
                    :ev, :exp, :action, 'ACTION_SCHEDULED', 1, :key, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "ev": moment,
                "exp": moment + timedelta(minutes=15),
                "action": CandidateAction.PAYMENT_LINK.value,
                "key": key,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, resolved_at,
                    provider_response_id, is_post_payment, reconciliation_attempts,
                    counter_applied, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :did, :key, :action,
                    1, :state, :started, :started, 'plink_x', false, 0, true, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "did": str(decision_id),
                "key": key,
                "action": CandidateAction.PAYMENT_LINK.value,
                "state": IntentState.CONFIRMED.value,
                "started": moment,
            },
        )


def _observation(engine: Engine, case_id: uuid.UUID) -> dict[str, object] | None:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT features, cause, confidence, diagnosis_method, selected_action,
                       policy_verdict, outcome_class, realized_cost, "group",
                       executed_action_count, customer_message_count, decision_source,
                       intervention_status, provenance
                FROM memory_observation WHERE case_id = :case_id
                """
            ),
            {"case_id": str(case_id)},
        ).one_or_none()
        return None if row is None else dict(row._mapping)


def _case_state(engine: Engine, case_id: uuid.UUID) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT state FROM recovery_case WHERE id = :id"), {"id": str(case_id)}
            ).scalar_one()
        )


def _expire(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    """Move the case to EXPIRED with the observation writer attached, as the sweeper does."""
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
        target_state=CaseState.EXPIRED,
        reason="recovery window elapsed",
        actor="test",
        terminal_reason=TerminalReason.RECOVERY_WINDOW_ELAPSED,
        factory=factory,
        on_success=observation_writer(default_configuration()),
    )
    assert result.applied, f"transition not applied: {result.outcome.value}"


# ---------------------------------------------------------------------------
# The label rules — the part that decides whether the baseline is honest
# ---------------------------------------------------------------------------


def test_a_control_case_with_no_action_is_the_only_usable_training_label() -> None:
    """The classification rule, in isolation. Pure, so every combination is cheap to assert.

    The third case is the one that matters. A *treatment* case that received no action looks
    identical to a control case in the outcome data — same zero actions, same terminal state —
    but it is not evidence about what happens without intervention. It is evidence about cases
    Revora declined to treat, which is a selected population: policy blocked them, or the
    window closed, and both correlate with the case being unpromising. Counting them as
    baseline labels is textbook selection bias, and it biases the baseline in the direction
    that makes every incremental claim look better.
    """
    assert (
        classify_intervention_status(confirmed_actions=0, group=ExperimentGroup.CONTROL)
        is InterventionStatus.NO_INTERVENTION_CONFIRMED
    )
    assert (
        classify_intervention_status(confirmed_actions=1, group=ExperimentGroup.CONTROL)
        is InterventionStatus.REVORA_INTERVENED
    ), "a contaminated control case is not a no-intervention label"
    assert (
        classify_intervention_status(confirmed_actions=0, group=ExperimentGroup.TREATMENT)
        is InterventionStatus.MERCHANT_INTERVENTION_UNKNOWN
    ), (
        "an untreated treatment case was accepted as a baseline label; this is the "
        "selection bias that flatters every incremental claim"
    )
    assert (
        classify_intervention_status(confirmed_actions=0, group=None)
        is InterventionStatus.MERCHANT_INTERVENTION_UNKNOWN
    ), "an unassigned case cannot claim nobody intervened"


def test_a_control_case_produces_a_usable_observation(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The happy path for the baseline: a control case that ran its window untreated."""
    merchant_id, case_id = _seed(owner_engine)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)

    _expire(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row is not None, "no observation was written for a terminal case"
    assert row["intervention_status"] == InterventionStatus.NO_INTERVENTION_CONFIRMED.value
    assert row["group"] == ExperimentGroup.CONTROL.value
    assert row["decision_source"] == DecisionSource.BASELINE_WORKFLOW.value
    assert row["cause"] == RiskCause.INSUFFICIENT_FUNDS.value
    assert row["diagnosis_method"] == DiagnosisMethod.DETERMINISTIC.value
    assert row["provenance"] == Provenance.REAL.value
    # No recovery row, so the outcome is explicitly "not established" rather than NULL.
    assert row["outcome_class"] == "NOT_ESTABLISHED"


def test_a_treated_case_is_recorded_but_not_as_a_baseline_label(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """A treated case is still observed — it just cannot teach the baseline anything."""
    merchant_id, case_id = _seed(owner_engine)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.TREATMENT)
    _confirm_action(owner_engine, merchant_id, case_id)

    _expire(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row is not None
    assert row["intervention_status"] == InterventionStatus.REVORA_INTERVENED.value
    assert row["selected_action"] == CandidateAction.PAYMENT_LINK.value
    assert row["decision_source"] == DecisionSource.AUTOMATED.value


def test_the_feature_document_is_exactly_what_the_estimator_matches_on(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The writer and reader agree on five keys, communicated only through JSONB containment.

    This is the interface with no compiler behind it. A renamed or missing key does not raise —
    containment simply stops matching, every segment collapses to the global prior, and the
    system looks fine while learning nothing. Asserting the exact key set is the only thing
    standing between that and a silent regression.
    """
    merchant_id, case_id = _seed(owner_engine)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
    _expire(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row is not None
    features = row["features"]
    assert isinstance(features, dict)

    expected = SegmentFeatures.derive(
        risk_cause=RiskCause.INSUFFICIENT_FUNDS,
        amount=Minor(250_000),
        payment_method="card",
        executed_action_count=0,
        error_source="issuer",
    ).as_values()

    assert set(features) == set(expected), (
        f"feature keys drifted: wrote {sorted(features)}, estimator matches on "
        f"{sorted(expected)}"
    )
    assert features == expected
    assert all(isinstance(value, str) for value in features.values()), (
        "a non-string feature value will not match containment against a string subset"
    )


# ---------------------------------------------------------------------------
# Atomicity — the reason this is written in the transition at all
# ---------------------------------------------------------------------------


def test_a_failing_observation_write_rolls_the_terminal_transition_back(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """If the observation cannot be written, the case has not ended.

    The coupling is deliberate and it is the whole justification for R15.C1. The alternative —
    swallow the failure and let the case terminate — produces a resolved case with no
    observation, permanently, because nothing revisits a terminal case. That hole is invisible:
    the training set simply has fewer rows than cases, biased toward whichever writes happened
    to succeed. A rolled-back transition is retryable and loud; a missing observation is
    neither.
    """
    merchant_id, case_id = _seed(owner_engine)

    def _explode(session: Session, case: object) -> None:
        raise RuntimeError("observation store unavailable")

    with owner_engine.begin() as connection:
        version = int(
            connection.execute(
                text("SELECT version FROM recovery_case WHERE id = :id"),
                {"id": str(case_id)},
            ).scalar_one()
        )

    with pytest.raises(RuntimeError, match="observation store unavailable"):
        apply_transition(
            merchant_id,
            case_id,
            expected_version=version,
            target_state=CaseState.EXPIRED,
            reason="recovery window elapsed",
            actor="test",
            terminal_reason=TerminalReason.RECOVERY_WINDOW_ELAPSED,
            factory=factory,
            on_success=_explode,
        )

    assert _case_state(owner_engine, case_id) != CaseState.EXPIRED.value, (
        "the case terminated even though its observation could not be written"
    )
    assert _observation(owner_engine, case_id) is None


def test_a_second_terminal_transition_does_not_write_a_second_observation(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Reconciliation can move an expired case to RECOVERED. The observation is still one.

    A second row would weight that case twice in training and twice in every calibration band
    it falls into. ``UNIQUE (case_id)`` would prevent it, but relying on the constraint alone
    would surface as an ``IntegrityError`` inside the transition and roll back a legitimate
    reconciliation — so the writer reads first and treats an existing row as a no-op.
    """
    merchant_id, case_id = _seed(owner_engine)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
    _expire(merchant_id, case_id, factory, owner_engine)

    first = _observation(owner_engine, case_id)
    assert first is not None

    # Now reconcile to RECOVERED, as a delayed capture would.
    with owner_engine.begin() as connection:
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
        target_state=CaseState.RECOVERED,
        reason="delayed capture verified",
        actor="test",
        verified_capture=True,
        factory=factory,
        on_success=observation_writer(default_configuration()),
    )
    assert result.applied, result.outcome.value

    with owner_engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM memory_observation WHERE case_id = :id"),
            {"id": str(case_id)},
        ).scalar_one()
    assert int(count) == 1, "a second terminal transition wrote a second observation"
    assert _observation(owner_engine, case_id) == first, "the observation was rewritten"


def test_synthetic_provenance_propagates_from_the_case(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """One synthetic contributor makes every figure built on it synthetic.

    The propagation has to start here, because the estimator marks a whole baseline
    ``SYNTHETIC`` if any contributing observation is — and it can only see the observation's
    label, not the case's.
    """
    merchant_id, case_id = _seed(owner_engine, provenance=Provenance.SYNTHETIC)
    _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)
    _expire(merchant_id, case_id, factory, owner_engine)

    row = _observation(owner_engine, case_id)
    assert row is not None
    assert row["provenance"] == Provenance.SYNTHETIC.value


# ---------------------------------------------------------------------------
# The loop closes: the estimator stops returning the uniform prior
# ---------------------------------------------------------------------------


def test_written_observations_move_the_baseline_off_the_uniform_prior(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The point of the whole task, asserted end to end.

    Before task 26 this table was empty, so every baseline was Beta(1,1): probability 0.500 with
    a `[0.025, 0.975]` interval. That was honest — a system with no history does not know its
    recovery rate. This test writes real observations through the real terminal-transition path
    and then reads them back through the estimator's own aggregate, which is the only way to
    prove writer and reader agree on the feature document.

    A tightened interval is the observable signal. If the containment match were broken, every
    read would return zeros and the posterior would stay Beta(1,1) with nothing looking wrong.
    """
    from revora.persistence.repositories.estimates import SegmentObservationRepository

    merchant_id = None
    features_subset: dict[str, str] = {}
    recovered_case_ids: list[uuid.UUID] = []

    # Eight control cases in one segment: four recovered, four not.
    for index in range(8):
        merchant_for_case, case_id = _seed(owner_engine)
        if merchant_id is None:
            merchant_id = merchant_for_case
        else:
            # Reuse one merchant so the observations land in one tenant's segment.
            with owner_engine.begin() as connection:
                connection.execute(
                    text("UPDATE recovery_case SET merchant_id = :m WHERE id = :c"),
                    {"m": str(merchant_id), "c": str(case_id)},
                )
                connection.execute(
                    text("UPDATE webhook_event SET merchant_id = :m WHERE id = ("
                         "SELECT source_event_id FROM recovery_case WHERE id = :c)"),
                    {"m": str(merchant_id), "c": str(case_id)},
                )
                connection.execute(
                    text("UPDATE diagnosis SET merchant_id = :m WHERE case_id = :c"),
                    {"m": str(merchant_id), "c": str(case_id)},
                )
        _assign(owner_engine, merchant_id, case_id, ExperimentGroup.CONTROL)

        if index < 4:
            _record_recovery(owner_engine, merchant_id, case_id)
            recovered_case_ids.append(case_id)
        _expire(merchant_id, case_id, factory, owner_engine)

    assert merchant_id is not None
    features_subset = {"risk_cause": RiskCause.INSUFFICIENT_FUNDS.value}

    with factory() as session, session.begin():
        counts = SegmentObservationRepository(session).segment_counts(
            merchant_id, features=features_subset
        )

    assert counts.observations == 8, (
        f"the estimator saw {counts.observations} of 8 observations — the writer and the "
        "segment aggregate disagree about the feature document"
    )
    assert counts.recoveries == 4, f"expected 4 recoveries, aggregate saw {counts.recoveries}"
    assert counts.resolved_control == 8, (
        "no control observations visible, so every baseline stays UNVALIDATED_BASELINE"
    )
    assert counts.synthetic_contributions == 0
    assert counts.unknown_intervention == 0

    # And the posterior actually moves: Beta(1+4, 1+4) has mean 0.5 but a much tighter
    # interval than Beta(1,1). The mean is uninformative here by construction; the interval
    # is what shows the estimator learned something.
    from revora.estimation.beta import UNIFORM_PRIOR

    posterior = UNIFORM_PRIOR.posterior(
        successes=counts.recoveries, trials=counts.observations
    )
    low, high = posterior.interval()
    prior_low, prior_high = UNIFORM_PRIOR.posterior(successes=0, trials=0).interval()
    assert (high - low) < (prior_high - prior_low), (
        "the interval did not tighten, so the observations taught the estimator nothing"
    )


def _record_recovery(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """A verified recovery, so the observation's outcome class is a real recovery."""
    read_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO payment_state_read (
                    id, merchant_id, case_id, provider_payment_id, status, amount,
                    amount_refunded, captured, read_at, attempt_no, created_at
                ) VALUES (
                    :id, :m, :c, :pid, 'captured', 250000, 0, true, :read_at, 1, now()
                )
                """
            ),
            {
                "id": str(read_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "pid": f"pay_{case_id.hex[:14]}",
                "read_at": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_outcome (
                    id, merchant_id, case_id, classification, recovered_amount,
                    recovery_timestamp, seconds_to_recovery, verified_by_read_id, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :c, :cls, 250000, :ts, 3600, :rid, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "cls": OutcomeClass.NATURAL.value,
                "ts": moment,
                "rid": str(read_id),
            },
        )

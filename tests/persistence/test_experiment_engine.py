"""The experiment lifecycle against real Postgres: define, freeze, assign, suppress, analyse.

The pure tests cover the arithmetic and the gate. These cover the things only a real database
can show:

* ``UNIQUE (case_id)`` genuinely prevents a case sitting in two arms — the constraint the whole
  comparison rests on;
* a control case's action is actually suppressed by the execution engine, with the recommendation
  and the approval left standing so the counterfactual survives;
* a contaminated control case is excluded from the arm counts *and* counted separately;
* an analysis persists a row even when it establishes nothing, so the history of what was
  concluded when is reconstructable;
* version freeze drift is detected and invalidates the experiment.

The last one is worth stating plainly: a mid-experiment model promotion changes what the
treatment arm *is*, so the measured difference stops meaning anything. There is no way to salvage
that comparison, which is why the response is a label and a stop rather than a correction.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    SUPPRESSED_BY_CONTROL_ARM,
    CaseState,
    DiagnosisMethod,
    ExperimentGroup,
    ExperimentLabel,
    ExperimentState,
    OutcomeClass,
    RiskCause,
)
from revora.domain.keys import execution_key
from revora.execution.engine import ExecutionOutcome, execute_approved_action
from revora.experiment.analysis import analyse_experiment
from revora.experiment.control import assign_case, mark_contaminated
from revora.experiment.design import (
    activate_experiment,
    define_experiment,
    detect_freeze_drift,
    invalidate_experiment,
)
from revora.memory.versions import (
    COMPONENT_BASELINE_MODEL,
    COMPONENT_BASELINE_WORKFLOW,
    COMPONENT_CANDIDATE_PRIORS,
    COMPONENT_POLICY_RULE_SET,
)
from revora.persistence.repositories.experiments import (
    ExperimentAssignmentRepository,
    ExperimentRepository,
    ExperimentResultRepository,
)
from revora.persistence.repositories.session import tenant_transaction
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store
from tests.fakes.razorpay import FakeRazorpay

pytestmark = pytest.mark.pg

_LIVE_VERSIONS = {
    COMPONENT_BASELINE_WORKFLOW: "no-automated-action-v1",
    COMPONENT_POLICY_RULE_SET: "v1-assumption-baseline",
    COMPONENT_BASELINE_MODEL: "beta-binomial-1",
    COMPONENT_CANDIDATE_PRIORS: "priors-v1",
}


class _Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"X" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"Y" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"Y" * 32).decode(),
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


def _merchant(engine: Engine) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Experiment merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"exp-{merchant_id}"},
        )
    return merchant_id


def _case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    state: CaseState = CaseState.ACTION_SCHEDULED,
    recovered: bool = False,
) -> uuid.UUID:
    """A fully diagnosed case with consent, ready to execute."""
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
                            "email": "exp@example.com",
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
                    :id, :m, :eid, 'payment.failed', :ct, :nonce, :kv,
                    :canonical, :corr, true, :received, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "m": str(merchant_id),
                "eid": f"evt_{case_id.hex[:16]}",
                "ct": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "kv": encrypted.key_version,
                "canonical": json.dumps(
                    {"provider_payment_id": payment_id, "method": "card"}
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
                    customer_key, source_event_id, detected_at, window_end_at,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, :state, :pid, 250000, 'INR', :ck, :sid, :detected, :we, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state.value,
                "pid": payment_id,
                "ck": f"ck-{case_id}",
                "sid": str(event_id),
                "detected": moment - timedelta(hours=2),
                "we": moment + timedelta(hours=168),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    id, merchant_id, customer_key, opted_out, source, effective_at, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :ck, false, 'test', :eff, now()
                )
                """
            ),
            {"m": str(merchant_id), "ck": f"ck-{case_id}", "eff": moment - timedelta(days=1)},
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :c, :cause, 0.90, :method, 1, true, false, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "cause": RiskCause.INSUFFICIENT_FUNDS.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
        if recovered:
            read_id = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO payment_state_read (
                        id, merchant_id, case_id, provider_payment_id, status, amount,
                        amount_refunded, captured, read_at, attempt_no, created_at
                    ) VALUES (
                        :id, :m, :c, :pid, 'captured', 250000, 0, true, :ra, 1, now()
                    )
                    """
                ),
                {
                    "id": str(read_id),
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "pid": payment_id,
                    "ra": moment,
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
                    "cls": OutcomeClass.OBSERVED.value,
                    "ts": moment,
                    "rid": str(read_id),
                },
            )
    return case_id


def _approve(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """An approved, unconsumed decision so the engine has something to execute."""
    moment = now()
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
                    gen_random_uuid(), :m, :c, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :ev, :exp, :action, 'ACTION_SCHEDULED', 1, :key, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "ev": moment,
                "exp": moment + timedelta(minutes=15),
                "action": CandidateAction.PAYMENT_LINK.value,
                "key": key,
            },
        )


def _define_and_activate(
    merchant_id: uuid.UUID,
    factory: sessionmaker[Session],
    *,
    sample_size_effect: str = "0.20",
) -> uuid.UUID:
    """A defined, activated experiment with all four components frozen."""
    with tenant_transaction(merchant_id, factory) as session:
        definition = define_experiment(
            session,
            merchant_id,
            name=f"exp-{uuid.uuid4()}",
            config=default_configuration(),
            assumed_baseline_rate=Decimal("0.20"),
            minimum_detectable_effect=Decimal(sample_size_effect),
        )
        activate_experiment(
            session,
            merchant_id,
            definition.experiment_id,
            live_versions=_LIVE_VERSIONS,
        )
    return definition.experiment_id


# ---------------------------------------------------------------------------
# Definition and freezing
# ---------------------------------------------------------------------------


def test_definition_stores_a_computed_sample_size_and_starts_in_draft(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The sample size is computed at definition time and the experiment does not self-activate.

    Both halves matter. A stored sample size is a threshold; one computed at analysis time is a
    description of the data that arrived. And activation being separate means the thing that
    starts assigning real cases to arms is an explicit act.
    """
    merchant_id = _merchant(owner_engine)
    with tenant_transaction(merchant_id, factory) as session:
        definition = define_experiment(
            session,
            merchant_id,
            name="power-check",
            config=default_configuration(),
            assumed_baseline_rate=Decimal("0.20"),
            minimum_detectable_effect=Decimal("0.10"),
        )

    assert definition.required_sample_size_per_group == 294, (
        f"expected the design's worked example, got {definition.required_sample_size_per_group}"
    )

    with tenant_transaction(merchant_id, factory) as session:
        row = ExperimentRepository(session).get(merchant_id, definition.experiment_id)
        assert row is not None
        assert ExperimentState(str(row.state)) is ExperimentState.DRAFT
        assert int(row.required_sample_size_per_group) == 294
        assert row.activated_at is None


def test_activation_freezes_all_four_components(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Every frozen component gets a row, and the experiment becomes ``ACTIVE``."""
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)

    with owner_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT component, version_id FROM experiment_version_freeze "
                "WHERE experiment_id = :e ORDER BY component"
            ),
            {"e": str(experiment_id)},
        ).all()
    frozen = {str(row[0]): str(row[1]) for row in rows}
    assert frozen == _LIVE_VERSIONS

    with tenant_transaction(merchant_id, factory) as session:
        row = ExperimentRepository(session).get(merchant_id, experiment_id)
        assert row is not None
        assert ExperimentState(str(row.state)) is ExperimentState.ACTIVE
        assert row.activated_at is not None


def test_a_changed_frozen_component_is_detected_and_invalidates_the_experiment(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R13.C16. A mid-experiment promotion changes what the treatment arm is.

    There is nothing to salvage: the comparison would be control against two different
    treatments, weighted by whenever the promotion happened. So the experiment is labelled and
    stops assigning, which is a row an operator can find — the alternative is a number nobody can
    check.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)

    promoted = dict(_LIVE_VERSIONS)
    promoted[COMPONENT_BASELINE_MODEL] = "beta-binomial-2"

    with tenant_transaction(merchant_id, factory) as session:
        drift = detect_freeze_drift(
            session, merchant_id, experiment_id, live_versions=promoted
        )
        assert len(drift) == 1, drift
        assert drift[0].component == COMPONENT_BASELINE_MODEL
        assert drift[0].frozen_version == "beta-binomial-1"
        assert drift[0].live_version == "beta-binomial-2"

        assert invalidate_experiment(
            session,
            merchant_id,
            experiment_id,
            config=default_configuration(),
            drift=drift,
        )

    with tenant_transaction(merchant_id, factory) as session:
        row = ExperimentRepository(session).get(merchant_id, experiment_id)
        assert row is not None
        assert ExperimentLabel.INVALIDATED.value in (row.labels or [])
        assert ExperimentState(str(row.state)) is ExperimentState.ABANDONED
        # And it is no longer offered for assignment.
        assert ExperimentRepository(session).active(merchant_id) is None


def test_no_drift_when_nothing_moved(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The control for the drift test: identical versions produce no drift.

    Without it, a `detect_freeze_drift` that always reported drift would pass the test above and
    invalidate every experiment the moment it was checked.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)
    with tenant_transaction(merchant_id, factory) as session:
        assert (
            detect_freeze_drift(
                session, merchant_id, experiment_id, live_versions=_LIVE_VERSIONS
            )
            == ()
        )


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_assignment_is_recorded_once_per_case(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """``UNIQUE (case_id)`` is the constraint the comparison rests on.

    A second assignment attempt must be a no-op, not an error and not a second row. Two rows would
    put the case in two arms and every figure derived from either becomes indefensible.
    """
    merchant_id = _merchant(owner_engine)
    _define_and_activate(merchant_id, factory)
    case_id = _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        first = assign_case(
            session, merchant_id, case_id, config=default_configuration()
        )
    assert first.assigned
    assert first.group in tuple(ExperimentGroup)

    with tenant_transaction(merchant_id, factory) as session:
        second = assign_case(
            session, merchant_id, case_id, config=default_configuration()
        )
    # Same arm, and no second row.
    assert second.group is first.group
    assert second.reason == "already assigned"

    with owner_engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM experiment_assignment WHERE case_id = :c"),
            {"c": str(case_id)},
        ).scalar_one()
    assert int(count) == 1


def test_a_case_is_unassigned_when_no_experiment_is_active(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R13.C14. An unassigned case runs the baseline workflow and is in neither arm.

    Critically it is *not* treated as treatment. Doing so would put cases into the treatment arm
    that the randomization never selected, which is the same contamination as moving one there by
    hand.
    """
    merchant_id = _merchant(owner_engine)
    case_id = _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        outcome = assign_case(
            session, merchant_id, case_id, config=default_configuration()
        )

    assert not outcome.assigned
    assert outcome.group is None
    assert outcome.reason == "no active experiment"
    with owner_engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM experiment_assignment WHERE case_id = :c"),
            {"c": str(case_id)},
        ).scalar_one()
    assert int(count) == 0


# ---------------------------------------------------------------------------
# Suppression — the control arm's actual effect
# ---------------------------------------------------------------------------


def test_a_control_case_action_is_suppressed_with_the_approval_left_standing(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R13.C3. No external call, no intent — and the approval is *not* consumed.

    The last part is what makes the arm a counterfactual rather than an absence. The
    recommendation and the approved decision both survive, so for this case we know exactly what
    Revora would have done. Consuming the approval would destroy that record and make the case
    indistinguishable from one that executed and failed.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)
    case_id = _case(owner_engine, merchant_id)
    _approve(owner_engine, merchant_id, case_id)

    # Force the control arm rather than hoping for it: the arm is deterministic, so a test that
    # generated cases until one landed in control would be slow and occasionally unlucky.
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO experiment_assignment (
                    id, merchant_id, experiment_id, case_id, "group", assigned_at,
                    contaminated, excluded, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :e, :c, :g, now(), false, false, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "e": str(experiment_id),
                "c": str(case_id),
                "g": ExperimentGroup.CONTROL.value,
            },
        )

    fake = FakeRazorpay()
    attempt = execute_approved_action(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert attempt.outcome is ExecutionOutcome.CONTROL_ARM_SUPPRESSED, attempt.outcome.value
    assert attempt.detail == SUPPRESSED_BY_CONTROL_ARM
    assert not attempt.made_external_call
    assert fake.call_count == 0, "a control case reached the provider"

    with owner_engine.begin() as connection:
        intents = connection.execute(
            text("SELECT count(*) FROM execution_intent WHERE case_id = :c"),
            {"c": str(case_id)},
        ).scalar_one()
        consumed = connection.execute(
            text(
                "SELECT count(*) FROM policy_decision WHERE case_id = :c "
                "AND consumed_by_intent_id IS NOT NULL"
            ),
            {"c": str(case_id)},
        ).scalar_one()
        state = connection.execute(
            text("SELECT state FROM recovery_case WHERE id = :c"), {"c": str(case_id)}
        ).scalar_one()
        events = connection.execute(
            text("SELECT event_type FROM audit_record WHERE case_id = :c ORDER BY seq"),
            {"c": str(case_id)},
        ).scalars().all()

    assert int(intents) == 0, "an execution intent was created for a control case"
    assert int(consumed) == 0, "the control case consumed its approval"
    assert str(state) == CaseState.ACTION_SCHEDULED.value, "the case moved despite suppression"
    assert "CONTROL_ACTION_SUPPRESSED" in [str(e) for e in events]


def test_a_treatment_case_is_not_suppressed(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The control for the suppression test.

    Without it, a suppression check that matched every case would pass the test above and quietly
    disable execution for the whole system.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)
    case_id = _case(owner_engine, merchant_id)
    _approve(owner_engine, merchant_id, case_id)

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO experiment_assignment (
                    id, merchant_id, experiment_id, case_id, "group", assigned_at,
                    contaminated, excluded, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :e, :c, :g, now(), false, false, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "e": str(experiment_id),
                "c": str(case_id),
                "g": ExperimentGroup.TREATMENT.value,
            },
        )

    fake = FakeRazorpay()
    attempt = execute_approved_action(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert attempt.outcome is ExecutionOutcome.CONFIRMED, (
        f"{attempt.outcome.value} / {attempt.detail}"
    )
    assert attempt.made_external_call


# ---------------------------------------------------------------------------
# Contamination and analysis
# ---------------------------------------------------------------------------


def test_a_contaminated_case_leaves_the_arm_counts_but_is_still_counted(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R13.C15. Excluded from the comparison, reported alongside it.

    Both halves. Excluding it keeps the control arm a control arm; reporting the count means a
    lift computed after discarding a third of the arm is visibly different from one that discarded
    none. And the arm itself is never changed — the randomization did not select this case for
    treatment, so moving it there would bias the treatment arm toward cases somebody acted on by
    hand.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)
    case_id = _case(owner_engine, merchant_id)

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO experiment_assignment (
                    id, merchant_id, experiment_id, case_id, "group", assigned_at,
                    contaminated, excluded, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :e, :c, :g, now(), false, false, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "e": str(experiment_id),
                "c": str(case_id),
                "g": ExperimentGroup.CONTROL.value,
            },
        )

    with tenant_transaction(merchant_id, factory) as session:
        before = ExperimentAssignmentRepository(session).arm_counts(merchant_id, experiment_id)
        assert before.control == 1
        assert before.contaminated == 0

        assert mark_contaminated(
            session,
            merchant_id,
            case_id,
            config=default_configuration(),
            detail="a confirmed action reached a control case",
        )

    with tenant_transaction(merchant_id, factory) as session:
        after = ExperimentAssignmentRepository(session).arm_counts(merchant_id, experiment_id)
        assignment = ExperimentAssignmentRepository(session).for_case(merchant_id, case_id)

    assert after.control == 0, "a contaminated case still counted toward the control arm"
    assert after.contaminated == 1, "the contamination was not counted"
    assert assignment is not None
    assert ExperimentGroup(str(assignment.group)) is ExperimentGroup.CONTROL, (
        "contamination changed the arm; it must only set the flag"
    )


def test_an_analysis_that_establishes_nothing_is_still_persisted(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Every analysis writes a row, including the ones that find nothing.

    An interval containing zero is a real finding and the most likely one. A system that recorded
    only the flattering analyses would have a history that could not be checked — and would make
    "we ran it and it showed nothing" indistinguishable from "we never ran it".
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)

    with tenant_transaction(merchant_id, factory) as session:
        analysis = analyse_experiment(
            session, merchant_id, experiment_id, config=default_configuration()
        )

    assert analysis is not None
    assert not analysis.attribution_permitted
    assert analysis.incremental_recovered_revenue == "NOT_ESTABLISHED", (
        "an empty experiment produced a numeric incremental figure"
    )
    codes = {refusal.code for refusal in analysis.refusals}
    assert "EXPERIMENT_NOT_COMPLETED" in codes
    assert "NO_LIFT_INTERVAL" in codes, codes

    with tenant_transaction(merchant_id, factory) as session:
        stored = ExperimentResultRepository(session).latest_for_experiment(
            merchant_id, experiment_id
        )
    assert stored is not None, "the analysis was not persisted"
    assert stored.lift is None
    assert stored.lift_ci_low is None


def test_an_analysis_records_the_four_way_comparison(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R13.C13. The comparison names all four figures per arm plus the difference.

    A lift alone answers the wrong question. The stored document has to let a reader see that a
    lift bought with three times the customer messages is not the same result as the same lift
    bought with none.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)

    # Two control cases (one recovered), two treatment (both recovered).
    for group, recovered in (
        (ExperimentGroup.CONTROL, True),
        (ExperimentGroup.CONTROL, False),
        (ExperimentGroup.TREATMENT, True),
        (ExperimentGroup.TREATMENT, True),
    ):
        case_id = _case(owner_engine, merchant_id, recovered=recovered)
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO experiment_assignment (
                        id, merchant_id, experiment_id, case_id, "group", assigned_at,
                        contaminated, excluded, created_at
                    ) VALUES (
                        gen_random_uuid(), :m, :e, :c, :g, now(), false, false, now()
                    )
                    """
                ),
                {
                    "m": str(merchant_id),
                    "e": str(experiment_id),
                    "c": str(case_id),
                    "g": group.value,
                },
            )

    with tenant_transaction(merchant_id, factory) as session:
        analysis = analyse_experiment(
            session, merchant_id, experiment_id, config=default_configuration()
        )

    assert analysis is not None
    assert analysis.control.cases == 2
    assert analysis.treatment.cases == 2
    assert analysis.control.recoveries == 1
    assert analysis.treatment.recoveries == 2
    assert analysis.lift == Decimal("0.5000")

    comparison = analysis.comparison_document()
    for arm in ("control", "treatment"):
        figures = comparison[arm]
        assert isinstance(figures, dict)
        for key in (
            "net_recovered_revenue",
            "intervention_rate",
            "messages_per_case",
            "blocked_cases",
        ):
            assert key in figures, f"{arm} is missing {key}"
    difference = comparison["difference"]
    assert isinstance(difference, dict)
    assert set(difference) == {
        "net_recovered_revenue",
        "intervention_rate",
        "messages_per_case",
        "blocked_cases",
    }

    # Still not attributable: the experiment is ACTIVE and far below its sample size.
    assert not analysis.attribution_permitted
    assert analysis.incremental_recovered_revenue == "NOT_ESTABLISHED"


def test_repeated_analyses_accumulate_rather_than_overwrite(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Interim looks are visible rather than prevented.

    Repeated analysis is a real inferential hazard, and the mitigation chosen here is that the
    history is on disk: three analyses leave three rows, so anyone can see how many times the
    question was asked before an answer was reported.
    """
    merchant_id = _merchant(owner_engine)
    experiment_id = _define_and_activate(merchant_id, factory)

    for _ in range(3):
        with tenant_transaction(merchant_id, factory) as session:
            analyse_experiment(
                session, merchant_id, experiment_id, config=default_configuration()
            )

    with tenant_transaction(merchant_id, factory) as session:
        history = ExperimentResultRepository(session).list_for_experiment(
            merchant_id, experiment_id
        )
    assert len(history) == 3, f"expected three retained analyses, found {len(history)}"

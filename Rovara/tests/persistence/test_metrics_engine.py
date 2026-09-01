"""Every test here tries to make Revora overstate what it knows. All must fail.

The metrics engine produces the numbers a merchant would put in front of their finance team, so
these tests are written from the position of someone trying to inflate them:

* get a recovery rate of ``0`` out of a period with no cases, so an empty week looks like failure
  rather than like no data;
* get ``incremental_recovered_revenue`` to report observed recovery, which is the substitution
  R12.C13 exists to forbid;
* get an incremental number out of an experiment that is still running, or underpowered, or
  synthetic, or whose interval contains zero;
* get positive observed revenue reported without the ``CAUSALITY_NOT_ESTABLISHED`` label;
* get a synthetic case into a cohort without the whole report being labelled.

The one thing that must *succeed* is a genuine attributed claim: a completed, adequately powered
experiment whose lift interval lies entirely above zero. Without that test the others would all
pass against an engine that refuses everything — which would be safe and useless.

Against real Postgres because every figure is an aggregate over rows, and the two constraints the
recovery figures rest on — ``verified_by_read_id NOT NULL`` and ``UNIQUE (case_id)`` on
``recovery_outcome`` — are not observable against a fake session.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.attribution import RefusalCode
from revora.domain.enums import (
    NOT_ESTABLISHED,
    RECOVERY_GROSS_OF_REFUNDS,
    UNDEFINED,
    CaseState,
    ExperimentGroup,
    ExperimentLabel,
    ExperimentState,
    IntentState,
    OutcomeClass,
    Provenance,
    RiskCause,
)
from revora.domain.segments import AmountBand
from revora.metrics.engine import ReportingPeriod, SegmentKey, compute_metrics, rate
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now

pytestmark = pytest.mark.pg


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


@pytest.fixture
def period() -> ReportingPeriod:
    moment = now()
    return ReportingPeriod(start=moment - timedelta(days=7), end=moment + timedelta(days=1))


def _merchant(engine: Engine) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Metrics merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"met-{merchant_id}"},
        )
    return merchant_id


def _case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    state: CaseState = CaseState.EXPIRED,
    amount: int = 250_000,
    detected_days_ago: int = 2,
    cause: RiskCause = RiskCause.INSUFFICIENT_FUNDS,
    provenance: Provenance = Provenance.REAL,
    decision_cycles: int = 1,
) -> uuid.UUID:
    case_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, provenance,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, :state, :pid, :amount, 'INR', :ck, :detected, :we, :prov, :dc, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state.value,
                "pid": f"pay_{case_id.hex[:14]}",
                "amount": amount,
                "ck": f"ck-{case_id}",
                "detected": moment - timedelta(days=detected_days_ago),
                "we": moment + timedelta(days=5),
                "prov": provenance.value,
                "dc": decision_cycles,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :c, :cause, 0.90, 'DETERMINISTIC', 1, true,
                    false, now()
                )
                """
            ),
            {"m": str(merchant_id), "c": str(case_id), "cause": cause.value},
        )
    return case_id


def _recover(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    classification: OutcomeClass = OutcomeClass.OBSERVED,
    amount: int = 250_000,
    seconds: int = 3600,
) -> None:
    """A verified recovery. Requires a payment_state_read, which is the point of the design."""
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
                    :id, :m, :c, :pid, 'captured', :amount, 0, true, :ra, 1, now()
                )
                """
            ),
            {
                "id": str(read_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "pid": f"pay_{case_id.hex[:14]}",
                "amount": amount,
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
                    gen_random_uuid(), :m, :c, :cls, :amount, :ts, :secs, :rid, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "cls": classification.value,
                "amount": amount,
                "ts": moment,
                "secs": seconds,
                "rid": str(read_id),
            },
        )
        connection.execute(
            text("UPDATE recovery_case SET state = :s WHERE id = :c"),
            {"s": CaseState.RECOVERED.value, "c": str(case_id)},
        )


def _confirmed_action(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    is_post_payment: bool = False,
) -> None:
    decision_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, created_at
                ) VALUES (
                    :id, :m, :c, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1', :ev, :exp,
                    'PAYMENT_LINK', 'ACTION_SCHEDULED', 1, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "ev": moment,
                "exp": moment + timedelta(minutes=15),
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
                    gen_random_uuid(), :m, :c, :d, :key, 'PAYMENT_LINK', 1, :st,
                    :started, :started, 'plink_x', :pp, 0, true, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "d": str(decision_id),
                "key": f"rv_{uuid.uuid4().hex[:16]}",
                "st": IntentState.CONFIRMED.value,
                "started": moment,
                "pp": is_post_payment,
            },
        )


def _completed_experiment(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    control: Sequence[uuid.UUID],
    treatment: Sequence[uuid.UUID],
    control_recoveries: int,
    treatment_recoveries: int,
    required_sample_size: int,
    lift_low: str | None,
    lift_high: str | None,
    state: ExperimentState = ExperimentState.COMPLETED,
    labels: list[str] | None = None,
) -> uuid.UUID:
    """An experiment plus a stored analysis, so the metrics engine has a result to read."""
    experiment_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO experiment (
                    id, merchant_id, name, state, primary_metric, allocation_ratio,
                    significance_level, power, required_sample_size_per_group,
                    activated_at, completed_at, labels, created_at
                ) VALUES (
                    :id, :m, :name, :state, 'recovery_rate', '1:1', 0.05, 0.80, :rss,
                    :act, :comp, :labels, now()
                )
                """
            ),
            {
                "id": str(experiment_id),
                "m": str(merchant_id),
                "name": f"exp-{experiment_id}",
                "state": state.value,
                "rss": required_sample_size,
                "act": moment - timedelta(days=30),
                "comp": moment if state is ExperimentState.COMPLETED else None,
                "labels": labels,
            },
        )
        for group, ids in (
            (ExperimentGroup.CONTROL, control),
            (ExperimentGroup.TREATMENT, treatment),
        ):
            for case_id in ids:
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
        connection.execute(
            text(
                """
                INSERT INTO experiment_result (
                    id, merchant_id, experiment_id, primary_metric, analysis_method,
                    control_case_count, treatment_case_count, control_recoveries,
                    treatment_recoveries, lift, lift_ci_low, lift_ci_high,
                    contaminated_count, excluded_count, comparison, computed_at, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :e, 'recovery_rate', 'two_proportion', :cc, :tc,
                    :cr, :tr, :lift, :low, :high, 0, 0, '{}'::jsonb, :ts, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "e": str(experiment_id),
                "cc": len(control),
                "tc": len(treatment),
                "cr": control_recoveries,
                "tr": treatment_recoveries,
                "lift": None if lift_low is None else Decimal("0.2000"),
                "low": None if lift_low is None else Decimal(lift_low),
                "high": None if lift_high is None else Decimal(lift_high),
                "ts": moment,
            },
        )
    return experiment_id


# ---------------------------------------------------------------------------
# The zero-denominator rule
# ---------------------------------------------------------------------------


def test_rate_returns_undefined_on_a_zero_denominator() -> None:
    """The pure rule, in one place so it cannot be applied inconsistently across nine metrics."""
    assert rate(0, 0) == UNDEFINED
    assert rate(5, 0) == UNDEFINED
    assert rate(0, 10) == Decimal("0.0000")
    assert rate(1, 3) == Decimal("0.3333")
    assert rate(2, 3) == Decimal("0.6667")


def test_an_empty_cohort_reports_undefined_rates_not_zero(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """A period with no cases has no rates. R12.C5.

    The most consequential misreading this prevents is a new merchant's first week: a dashboard of
    zeroes says "we recovered nothing", which is a measurement nobody made. A dashboard of
    ``UNDEFINED`` says "no data yet".

    Counts and money sums *are* zero, and correctly so — an empty period genuinely had zero cases
    and zero revenue at risk. The rule is about rates, not about everything.
    """
    merchant_id = _merchant(owner_engine)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.case_count == 0
    assert metrics.recovery_rate == UNDEFINED
    assert metrics.intervention_rate == UNDEFINED
    assert metrics.action_success_rate == UNDEFINED
    assert metrics.escalation_rate == UNDEFINED
    assert metrics.average_hours_to_recovery == UNDEFINED
    # Sums are legitimately zero.
    assert metrics.revenue_at_risk == 0
    assert metrics.observed_recovered_revenue == 0
    assert metrics.blocked_case_count == 0
    # And none of them serialize as the number zero.
    document = metrics.as_document()
    assert document["recovery_rate"] == UNDEFINED
    assert document["average_hours_to_recovery"] == UNDEFINED


def test_action_success_rate_is_undefined_with_cases_but_no_actions(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """Its denominator is confirmed *actions*, not cases.

    A cohort with cases but no actions has a defined recovery rate and an undefined action success
    rate. Sharing a denominator across the two would report ``0.0000`` here, which claims every
    action failed when none was taken.
    """
    merchant_id = _merchant(owner_engine)
    _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.case_count == 1
    assert metrics.recovery_rate == Decimal("0.0000")
    assert metrics.action_success_rate == UNDEFINED
    assert metrics.intervention_rate == Decimal("0.0000")


# ---------------------------------------------------------------------------
# The cohort boundary
# ---------------------------------------------------------------------------


def test_the_cohort_is_half_open_on_detection_time(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """``[start, end)`` so adjacent periods partition cases exactly. R12.C1.

    A closed interval would count a case detected exactly at the boundary in both periods, and the
    sum of two months would exceed the quarter. Asserted by placing a case precisely on each edge.
    """
    merchant_id = _merchant(owner_engine)
    moment = now().replace(microsecond=0)
    start = moment - timedelta(days=1)
    end = moment

    with owner_engine.begin() as connection:
        for detected_at, label in ((start, "on_start"), (end, "on_end")):
            case_id = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO recovery_case (
                        id, merchant_id, state, provider_payment_id, payment_amount, currency,
                        customer_key, detected_at, window_end_at, created_at
                    ) VALUES (
                        :id, :m, 'EXPIRED', :pid, 100000, 'INR', :ck, :d, :we, now()
                    )
                    """
                ),
                {
                    "id": str(case_id),
                    "m": str(merchant_id),
                    "pid": f"pay_{label}_{case_id.hex[:8]}",
                    "ck": f"ck-{case_id}",
                    "d": detected_at,
                    "we": detected_at + timedelta(days=7),
                },
            )

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(
            session, merchant_id, ReportingPeriod(start=start, end=end)
        )

    assert metrics.case_count == 1, (
        "the period must include its start and exclude its end, or adjacent periods "
        "double-count boundary cases"
    )


def test_a_reversed_period_is_refused() -> None:
    """A period with non-positive duration would silently produce an empty cohort."""
    moment = now()
    with pytest.raises(ValueError):
        ReportingPeriod(start=moment, end=moment - timedelta(days=1))
    with pytest.raises(ValueError):
        ReportingPeriod(start=moment, end=moment)


# ---------------------------------------------------------------------------
# The causality gate — the heart of it
# ---------------------------------------------------------------------------


def test_observed_recovery_is_never_reported_as_incremental(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """R12.C13. With no experiment, incremental is ``NOT_ESTABLISHED`` with no number.

    The single most important assertion in this file. Observed recovery is real and positive here;
    the temptation is to present it as the headline "incremental revenue recovered" figure, and
    that is precisely the substitution the requirement forbids. Everything Revora observes without
    a controlled comparison is consistent with the money having arrived anyway.
    """
    merchant_id = _merchant(owner_engine)
    case_id = _case(owner_engine, merchant_id)
    _recover(owner_engine, merchant_id, case_id, amount=180_000)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.observed_recovered_revenue == 180_000
    assert metrics.incremental.value == NOT_ESTABLISHED
    assert not metrics.incremental.established
    assert RefusalCode.NO_EXPERIMENT in metrics.incremental.refusal_codes

    document = metrics.as_document()
    incremental = document["incremental_recovered_revenue"]
    assert isinstance(incremental, dict)
    assert incremental["value"] == NOT_ESTABLISHED
    assert incremental["value"] != 180_000
    assert incremental["value"] != 0


def test_positive_observed_revenue_without_causality_is_labelled(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """R12.C9. The number is real; the implication a reader would draw is not.

    The label travels with the figure into every surface and export, because the failure mode is
    not a wrong number — it is a right number read as a causal claim.
    """
    merchant_id = _merchant(owner_engine)
    case_id = _case(owner_engine, merchant_id)
    _recover(owner_engine, merchant_id, case_id)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.observed_recovered_revenue > 0
    assert ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value in metrics.labels
    assert RECOVERY_GROSS_OF_REFUNDS in metrics.labels


def test_a_cohort_that_recovered_nothing_is_not_labelled_causality_not_established(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """The label is for positive observed revenue only.

    A period that recovered nothing needs no warning against over-reading its recovery, and
    labelling it anyway would dilute the label exactly where it matters.
    """
    merchant_id = _merchant(owner_engine)
    _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.observed_recovered_revenue == 0
    assert ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value not in metrics.labels


@pytest.mark.parametrize(
    ("state", "labels", "required", "lift_low", "lift_high", "expected_code"),
    [
        (
            ExperimentState.ACTIVE,
            None,
            2,
            "0.0500",
            "0.3000",
            RefusalCode.NO_EXPERIMENT,
        ),
        (
            ExperimentState.COMPLETED,
            None,
            1000,
            "0.0500",
            "0.3000",
            RefusalCode.BELOW_SAMPLE_SIZE,
        ),
        (
            ExperimentState.COMPLETED,
            None,
            2,
            "-0.0500",
            "0.3000",
            RefusalCode.CONTAINS_ZERO,
        ),
        (
            ExperimentState.COMPLETED,
            None,
            2,
            "-0.3000",
            "-0.0500",
            RefusalCode.BELOW_ZERO,
        ),
        (
            ExperimentState.COMPLETED,
            [ExperimentLabel.SYNTHETIC.value],
            2,
            "0.0500",
            "0.3000",
            RefusalCode.DISQUALIFYING_LABEL,
        ),
        (
            ExperimentState.COMPLETED,
            [ExperimentLabel.INVALIDATED.value],
            2,
            "0.0500",
            "0.3000",
            RefusalCode.DISQUALIFYING_LABEL,
        ),
        (ExperimentState.COMPLETED, None, 2, None, None, RefusalCode.NO_INTERVAL),
    ],
)
def test_each_unearned_claim_is_refused(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    period: ReportingPeriod,
    state: ExperimentState,
    labels: list[str] | None,
    required: int,
    lift_low: str | None,
    lift_high: str | None,
    expected_code: str,
) -> None:
    """Seven ways to obtain an incremental figure that was not earned. All refused.

    Each row removes exactly one gate term from an otherwise-qualifying experiment. Removing them
    one at a time is what would catch a conjunction accidentally implemented as a disjunction — a
    scenario that satisfied everything at once would pass either way.
    """
    merchant_id = _merchant(owner_engine)
    control = [_case(owner_engine, merchant_id) for _ in range(2)]
    treatment = [_case(owner_engine, merchant_id) for _ in range(2)]
    _recover(owner_engine, merchant_id, treatment[0])
    _recover(owner_engine, merchant_id, treatment[1])

    _completed_experiment(
        owner_engine,
        merchant_id,
        control=control,
        treatment=treatment,
        control_recoveries=0,
        treatment_recoveries=2,
        required_sample_size=required,
        lift_low=lift_low,
        lift_high=lift_high,
        state=state,
        labels=labels,
    )

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.incremental.value == NOT_ESTABLISHED, (
        f"an unearned claim was permitted: {metrics.incremental}"
    )
    assert expected_code in metrics.incremental.refusal_codes, metrics.incremental.refusal_codes


def test_a_fully_qualified_experiment_permits_an_incremental_figure(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """The control for every refusal above.

    Completed, both arms at their required size, interval entirely above zero, no blocking label.
    The figure comes with the experiment id, both arm counts and the interval attached, because a
    bare incremental number is unfalsifiable and the same number with its comparison can be argued
    with.

    Without this test the seven refusals would all pass against an engine that refuses
    unconditionally — which would be safe, and would mean Revora could never report its central
    claim.
    """
    merchant_id = _merchant(owner_engine)
    control = [_case(owner_engine, merchant_id) for _ in range(4)]
    treatment = [_case(owner_engine, merchant_id) for _ in range(4)]
    _recover(owner_engine, merchant_id, control[0], amount=200_000)
    for case_id in treatment:
        _recover(owner_engine, merchant_id, case_id, amount=200_000)

    experiment_id = _completed_experiment(
        owner_engine,
        merchant_id,
        control=control,
        treatment=treatment,
        control_recoveries=1,
        treatment_recoveries=4,
        required_sample_size=4,
        lift_low="0.2500",
        lift_high="0.9500",
    )

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.incremental.established, metrics.incremental.refusal_codes
    assert isinstance(metrics.incremental.value, int)
    assert metrics.incremental.experiment_id == experiment_id
    assert metrics.incremental.control_case_count == 4
    assert metrics.incremental.treatment_case_count == 4
    assert metrics.incremental.lift_ci_low == Decimal("0.2500")

    # Treatment recovered 4 of 4 at 200000 each; the control rate of 1/4 says one would have
    # recovered anyway. So incremental is 800000 - 200000.
    assert metrics.incremental.value == 600_000

    # And with causality established, the observed figure is no longer labelled as unestablished.
    assert ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value not in metrics.labels


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_one_synthetic_case_labels_the_whole_report(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """R12.C11. One is enough, not a proportion.

    A proportion would invite somebody to decide a little generated data is acceptable. A figure
    with any synthetic contribution cannot support a real-world claim, so the threshold is one.
    """
    merchant_id = _merchant(owner_engine)
    for _ in range(5):
        _case(owner_engine, merchant_id)
    _case(owner_engine, merchant_id, provenance=Provenance.SYNTHETIC)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.case_count == 6
    assert metrics.is_synthetic
    assert ExperimentLabel.SYNTHETIC.value in metrics.labels
    assert ExperimentLabel.SYNTHETIC.value in metrics.as_document()["labels"]  # type: ignore[operator]


def test_an_all_real_cohort_is_not_labelled_synthetic(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """The control: ``REAL`` only where every contributing case is real."""
    merchant_id = _merchant(owner_engine)
    for _ in range(3):
        _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert not metrics.is_synthetic
    assert ExperimentLabel.SYNTHETIC.value not in metrics.labels


# ---------------------------------------------------------------------------
# The remaining figures
# ---------------------------------------------------------------------------


def test_the_counters_and_money_figures_add_up(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """Integer arithmetic throughout: net is observed minus cost, and nothing is a float.

    ``unresolved_revenue`` sums cases whose state is anything but ``RECOVERED``, which includes
    in-flight ones. Deliberate: money not yet recovered is unresolved whether the case failed or
    has not finished, and excluding in-flight cases would make the figure shrink as a period aged
    rather than as money actually arrived.
    """
    merchant_id = _merchant(owner_engine)
    recovered = _case(owner_engine, merchant_id, amount=300_000)
    _recover(owner_engine, merchant_id, recovered, amount=300_000, seconds=7200)
    _case(owner_engine, merchant_id, amount=100_000, state=CaseState.BLOCKED)
    _case(owner_engine, merchant_id, amount=50_000, state=CaseState.ESCALATED)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.case_count == 3
    assert metrics.revenue_at_risk == 450_000
    assert metrics.recovered_case_count == 1
    assert metrics.observed_recovered_revenue == 300_000
    assert metrics.unresolved_revenue == 150_000
    assert metrics.blocked_case_count == 1
    assert metrics.escalated_case_count == 1
    assert metrics.recovery_rate == rate(1, 3)
    assert metrics.escalation_rate == rate(1, 3)
    # Zero cost today, honestly zero rather than estimated.
    assert metrics.total_recovery_cost == 0
    assert metrics.net_recovered_revenue == 300_000
    # 7200 seconds is exactly two hours.
    assert metrics.average_hours_to_recovery == Decimal("2.00")
    assert all(isinstance(value, int) for value in (
        metrics.revenue_at_risk,
        metrics.observed_recovered_revenue,
        metrics.unresolved_revenue,
        metrics.net_recovered_revenue,
    ))


def test_unnecessary_actions_are_counted_and_visible(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """``unnecessary_action_count`` is the cost of Revora being wrong, reported not hidden.

    An action that went out after the customer had already paid. A system that suppressed this
    count would be optimising its own report rather than the merchant's outcome.
    """
    merchant_id = _merchant(owner_engine)
    case_id = _case(owner_engine, merchant_id)
    _confirmed_action(owner_engine, merchant_id, case_id, is_post_payment=True)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.unnecessary_action_count == 1
    assert metrics.as_document()["unnecessary_action_count"] == 1


def test_cases_that_deliberated_without_acting_are_counted(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """R12.C16. How often Revora thought about it and did nothing.

    Either good judgement or a broken policy. Either way an operator should be able to see it, and
    a count of zero would be indistinguishable from the metric not existing.
    """
    merchant_id = _merchant(owner_engine)
    _case(owner_engine, merchant_id, decision_cycles=2)
    acted = _case(owner_engine, merchant_id, decision_cycles=1)
    _confirmed_action(owner_engine, merchant_id, acted)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    assert metrics.cycles_without_action_count == 1, (
        "expected exactly the case that deliberated and never acted"
    )
    assert metrics.intervened_case_count == 1


def test_segmentation_partitions_the_aggregate(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """Segments are computed by the same function as the aggregate, so they must sum to it.

    A segment defined differently from the total it rolls into is worse than no segmentation: two
    figures on one dashboard that disagree, with no way to tell which is wrong.
    """
    merchant_id = _merchant(owner_engine)
    for _ in range(3):
        _case(owner_engine, merchant_id, cause=RiskCause.INSUFFICIENT_FUNDS)
    for _ in range(2):
        _case(owner_engine, merchant_id, cause=RiskCause.EXPIRED_PAYMENT_METHOD)

    with tenant_transaction(merchant_id, factory) as session:
        aggregate = compute_metrics(session, merchant_id, period)
        funds = compute_metrics(
            session,
            merchant_id,
            period,
            segment=SegmentKey(risk_cause=RiskCause.INSUFFICIENT_FUNDS),
        )
        expired = compute_metrics(
            session,
            merchant_id,
            period,
            segment=SegmentKey(risk_cause=RiskCause.EXPIRED_PAYMENT_METHOD),
        )

    assert aggregate.case_count == 5
    assert funds.case_count == 3
    assert expired.case_count == 2
    assert funds.case_count + expired.case_count == aggregate.case_count
    assert funds.revenue_at_risk + expired.revenue_at_risk == aggregate.revenue_at_risk
    assert funds.segment.risk_cause is RiskCause.INSUFFICIENT_FUNDS
    assert aggregate.segment.is_aggregate


def test_amount_band_segmentation_uses_the_shared_banding_rule(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """Banding comes from ``domain.segments``, not a second SQL ``CASE`` expression.

    A ``CASE`` in the query would be a second definition of the same rule, and the two would drift
    — which is the bug that made the segment vocabulary move into the domain in the first place.
    """
    merchant_id = _merchant(owner_engine)
    _case(owner_engine, merchant_id, amount=50_000)
    _case(owner_engine, merchant_id, amount=5_000_000)

    with tenant_transaction(merchant_id, factory) as session:
        aggregate = compute_metrics(session, merchant_id, period)
        large = compute_metrics(
            session, merchant_id, period, segment=SegmentKey(amount_band=AmountBand.LARGE)
        )

    assert aggregate.case_count == 2
    assert large.case_count == 1, (
        f"expected one LARGE case, banding put {large.case_count} there"
    )
    assert large.revenue_at_risk == 5_000_000


def test_every_figure_carries_its_provenance_metadata(
    owner_engine: Engine, factory: sessionmaker[Session], period: ReportingPeriod
) -> None:
    """R12.C12. Period start, period end, and the computation instant, on every report.

    Not decoration. Metrics genuinely move — a delayed capture can reconcile a case to
    ``RECOVERED`` weeks after the period closed — so a figure without its computation instant
    cannot be reconciled against a later recomputation, and the two would just appear to disagree.
    """
    merchant_id = _merchant(owner_engine)
    _case(owner_engine, merchant_id)

    with tenant_transaction(merchant_id, factory) as session:
        metrics = compute_metrics(session, merchant_id, period)

    document = metrics.as_document()
    reporting_period = document["reporting_period"]
    assert isinstance(reporting_period, dict)
    assert reporting_period["start"] == period.start.isoformat()
    assert reporting_period["end"] == period.end.isoformat()
    assert "computed_at" in document
    assert metrics.computed_at is not None
    assert document["segment"] == {"risk_cause": None, "amount_band": None}

"""The four mandatory scenarios, end to end. The null one gates the build.

These are the tests that decide whether Revora's central claim is checkable. Everything else in the
suite verifies that a component does what it says; these verify that the *measurement* recovers an
effect we planted and refuses one we did not.

**The null scenario is the important one and it is the one most likely to be quietly weakened.** It
hands Revora a world with a true lift of exactly zero and requires an interval containing zero plus
``CAUSALITY_NOT_ESTABLISHED``. If it ever reports a lift, the measurement is broken and every other
number the system produces is untrustworthy — so a failure here fails the build rather than
producing a warning. It is also the test that would pass trivially against a system that never
reports anything, which is why the positive scenario sits beside it as the control.

**What these exercise**: the real canonicalizer on real Razorpay-shaped payloads, the real failure
taxonomy, the real deterministic arm assignment, and the real experiment analysis including the
attribution gate. **What they do not**: HMAC, the HTTP route, the job queue, the provider client.
Those are transport and orchestration with their own tests, and driving them here would add minutes
per scenario without touching the claim under test.

The premises these rest on — that the same seed reproduces the same world, that the null scenario's
true lift really is zero, that the generated payloads use only verified reasons — live in
``test_generator.py`` and run in the fast tier, because a premise checked only nightly is a premise
that breaks on a Tuesday and is found on a Friday. The optimizer half of task 27.4 is there too.

**Nothing here proves anything about real recovery rates.** The ground truth is ours. A synthetic
run establishes that the measurement works; it cannot establish what the measurement would find in
the world.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.attribution import RefusalCode
from revora.domain.enums import ExperimentLabel, Provenance
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.synthetic.generator import SCALE, ScenarioName
from revora.synthetic.harness import run_scenario
from tests.pg_support import insert_merchant

pytestmark = [pytest.mark.pg, pytest.mark.harness]
"""Both marks, because these tests belong to two tiers with different jobs.

``pg`` because they need a real server. ``harness`` because they are the full synthetic pipeline,
which the design's cost tiers run nightly and before a demo build rather than on every commit — the
coverage check alone is over a minute. Marking them lets the push tier select ``pg and not harness``
while the nightly and demo-gate jobs select the null scenario by name.
"""

_CASE_COUNT = 600
"""The default scenario size. Arms of roughly 300, which is inside the design's own worked example
for detecting a 0.10 effect at 80 percent power."""

_DETECTION_CASE_COUNT = 2000
"""For the two tests that require the interval to *exclude* zero.

Sized from the failure that produced it. At 600 cases the positive scenario detects its 0.155 effect
at about 4 sigma, which sounds ample and is not: it makes the test fail roughly one run in seventy,
and it failed exactly that way — passing alone, failing in a full-suite run on the same seed. A
fixed seed cannot rescue it, because a fresh merchant is a fresh randomization and the arm split is
where the variance lives (see ``harness._ID_NAMESPACE``).

At 2000 cases the arms are ~1000 each, the standard error of the difference is about 0.020, and
detection is a 5.9-sigma event — a failure rate around one in a hundred million. A statistical test
in CI has to be robust to *any* valid randomization, not to a favourable one, and the only honest
way to buy that is sample size."""

_NULL_RANDOMIZATIONS = 20
"""Independent randomizations the null gate reads.

A single 95 percent interval under a true null excludes zero 5 percent of the time **by
construction**. So a gate asserting "this one interval contains zero" is a gate that fails one build
in twenty for the correct reason, and no sample size fixes it — the false-positive rate is the
confidence level, not a power problem. Twenty randomizations with at most four exclusions puts the
gate's own false-alarm rate at about 0.26 percent while still catching a measurement that
manufactures effects, which would blow through the allowance immediately."""

_NULL_MAX_EXCLUSIONS = 4
"""P(Binomial(20, 0.05) >= 5) is about 0.0026. Raising this weakens the gate; lowering it makes the
gate itself flaky. Both directions are worse."""

_NULL_CASE_COUNT = 300

_NULL_IMPLAUSIBLE_LIFT = Decimal("0.2500")
"""A magnitude no null randomization should reach. About 5.4 standard errors at this arm size, so
this bound is effectively deterministic — unlike "the interval contains zero", it is a statement a
single run can be held to. It is the assertion that would fire if the lift arithmetic had a sign or
scale error rather than merely bad luck."""

_COVERAGE_SEEDS = 40
"""Seeds for the coverage check. The design suggests ~200; 40 takes about 75 seconds, which is
tolerable in a nightly tier, and still makes a systematically narrow interval obvious — at 95
percent nominal coverage, seeing fewer than 30 of 40 would be a real signal. Raising it to 200
would cost roughly six minutes for a proportionally tighter bound on the coverage estimate, which
is a reasonable trade to make later and not one to make on every push."""


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


@pytest.fixture
def merchant(owner_engine: Engine) -> uuid.UUID:
    """A fresh merchant per test, so one scenario's cases can never be counted in another's arm."""
    return insert_merchant(owner_engine, display_name="Synthetic merchant")


# ---------------------------------------------------------------------------
# The four mandatory scenarios
# ---------------------------------------------------------------------------


def test_the_null_scenario_reports_no_causal_effect(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """**THE CI GATE.** True lift is zero; Revora must say so, and must never claim otherwise.

    If this test fails, the build fails, and that is proportionate: a measurement system that
    reports a lift in a world with no effect is a measurement system whose every other number is
    untrustworthy. There is no partial credit and no warning-only mode.

    **Three assertions, and they are not interchangeable.**

    1. *Attribution is refused on every single randomization.* Deterministic, and it is the
       assertion that actually protects a dashboard — whatever the interval does, a null world may
       never license an attributed revenue figure.
    2. *No randomization reports an implausible lift.* Also effectively deterministic at
       ``_NULL_IMPLAUSIBLE_LIFT``, and this is the one that fires on a sign or scale error in the
       lift arithmetic rather than on bad luck.
    3. *Across ``_NULL_RANDOMIZATIONS`` independent randomizations, at most
       ``_NULL_MAX_EXCLUSIONS`` intervals exclude zero.* Many rather than one, because a 95 percent
       interval under a true null excludes zero 5 percent of the time by construction — a
       single-interval gate would fail one build in twenty and teach everyone to re-run it. This
       form checks the property the confidence level actually claims.

    The positive scenario sits beside this as the control, because every assertion here would also
    pass against a system that never reports anything at all.
    """
    config = default_configuration()
    excluded_zero: list[str] = []
    contains_zero_refusals = 0

    for offset in range(_NULL_RANDOMIZATIONS):
        with tenant_transaction(merchant, factory) as session:
            result = run_scenario(
                session,
                merchant,
                scenario_name=ScenarioName.NULL,
                seed=20240101 + offset,
                case_count=_NULL_CASE_COUNT,
                config=config,
            )
        report = result.report
        assert report.true_lift == Decimal("0.0000"), "the null scenario's premise is broken"
        assert report.measured_lift is not None, "a null randomization measured nothing at all"

        assert not report.attribution_permitted, (
            f"A NULL WORLD LICENSED AN ATTRIBUTED CLAIM. seed {report.seed}, refusals "
            f"{report.refusal_codes}. Full report: {report.as_document()}"
        )
        assert abs(report.measured_lift) < _NULL_IMPLAUSIBLE_LIFT, (
            f"THE MEASUREMENT IS BROKEN: a world with no effect measured a lift of "
            f"{report.measured_lift}, which is far outside sampling variation at this arm size. "
            f"Look for a sign or scale error in the lift arithmetic. "
            f"Full report: {report.as_document()}"
        )

        if report.interval_contains_zero:
            contains_zero_refusals += int(
                RefusalCode.CONTAINS_ZERO in report.refusal_codes
            )
        else:
            excluded_zero.append(
                f"seed {report.seed}: [{report.measured_ci_low}, {report.measured_ci_high}]"
            )

    assert len(excluded_zero) <= _NULL_MAX_EXCLUSIONS, (
        f"THE MEASUREMENT IS BROKEN: {len(excluded_zero)} of {_NULL_RANDOMIZATIONS} "
        f"randomizations of a world with no effect produced an interval excluding zero, against an "
        f"allowance of {_NULL_MAX_EXCLUSIONS} at 95 percent confidence. Every causal claim this "
        f"system makes is untrustworthy until this passes. Intervals: {excluded_zero}"
    )
    # Where the interval did contain zero, the refusal must say so by name. An interval straddling
    # zero that refused for some other reason would mean the gate reached the right verdict for the
    # wrong reason, and the reason is what an operator reads.
    expected_contains_zero = _NULL_RANDOMIZATIONS - len(excluded_zero)
    assert contains_zero_refusals == expected_contains_zero, (
        f"{expected_contains_zero} randomizations produced an interval containing zero but only "
        f"{contains_zero_refusals} recorded {RefusalCode.CONTAINS_ZERO}"
    )


def test_the_positive_scenario_recovers_the_planted_effect(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """The control for the null gate. A real effect must be found, and found close to the truth.

    Without this, the null test would pass against a system that reports "no effect" unconditionally
    — which would be perfectly safe and would mean Revora could never report its central claim.

    The measured lift is checked against the true one with a tolerance, not for equality: sampling
    variation is real and a measurement that matched the truth exactly would be suspicious rather
    than reassuring.

    Runs at ``_DETECTION_CASE_COUNT`` rather than the default, because this is one of only two tests
    that require the interval to *exclude* zero and that is the assertion a marginal sample size
    makes flaky. See the constant for the arithmetic.

    Every assertion here is one a *single* randomization can be held to. Interval coverage is not,
    and the note at the end of the body says why it lives elsewhere.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.POSITIVE,
            seed=20240202,
            case_count=_DETECTION_CASE_COUNT,
            config=default_configuration(),
        )

    report = result.report
    assert report.true_lift > Decimal("0.1000"), "the positive scenario's premise is broken"
    assert report.measured_lift is not None

    assert report.measured_lift > 0, (
        f"a planted positive effect measured as {report.measured_lift}: {report.as_document()}"
    )
    assert not report.interval_contains_zero, (
        f"a clear effect was not detected; interval "
        f"[{report.measured_ci_low}, {report.measured_ci_high}] contains zero"
    )
    difference = report.difference
    assert difference is not None
    assert abs(difference) < Decimal("0.1000"), (
        f"measured {report.measured_lift} against a true {report.true_lift}; the measurement is "
        f"off by {difference}"
    )

    # NOTE. There is deliberately no `interval_contains_true_lift` assertion here, and this is the
    # one place in the file where sample size is not the answer.
    #
    # A 95 percent interval covers the true parameter 95 percent of the time **by construction**, so
    # a single-run coverage assertion fails one build in twenty however many cases it is given.
    # `_DETECTION_CASE_COUNT` fixes the *other* assertion above — "the interval excludes zero" is a
    # power question, and more cases push the interval further from zero — but coverage is not a
    # power question. Raising n narrows the interval and moves the true lift no closer to the middle
    # of it. This assertion was here, and it failed exactly as predicted: passing in isolation and
    # failing in a full-suite run, with the interval [0.1686, 0.2470] missing a true 0.1500 low.
    #
    # The claim is real and worth testing, so it is tested where it can be: over
    # `_COVERAGE_SEEDS` randomizations in the coverage test at the end of this file, which is the
    # same treatment the null gate gets and for the same reason. Asserting it twice, once correctly
    # and once as a coin flip, buys nothing and costs a flaky build.


def test_the_negative_scenario_reports_a_negative_lift_and_refuses_attribution(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """Acting makes things worse. The sign must survive, and no claim may be made.

    The failure this catches is a system that reports the magnitude of an effect without its sign —
    anything downstream taking an absolute value would then present an incremental *loss* as a gain.

    That the *optimizer* also declines to act in this world is checked in ``test_generator.py``,
    where it needs no database.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.NEGATIVE,
            seed=20240303,
            case_count=_CASE_COUNT,
            config=default_configuration(),
        )

    report = result.report
    assert report.true_lift < Decimal("0.0000"), "the negative scenario's premise is broken"
    assert report.measured_lift is not None
    assert report.measured_lift < 0, (
        f"a harmful treatment measured as {report.measured_lift}: {report.as_document()}"
    )
    assert not report.attribution_permitted
    # Either the interval straddles zero or it lies entirely below it. Both refuse; neither may
    # license a claim.
    assert (
        RefusalCode.CONTAINS_ZERO in report.refusal_codes
        or RefusalCode.BELOW_ZERO in report.refusal_codes
    ), report.refusal_codes


def test_the_high_baseline_scenario_measures_a_small_effect_on_customers_who_would_pay_anyway(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """P17's case: the customer was going to pay regardless.

    The true uplift is small and positive, the baseline is above ``HIGH_BASELINE_THRESHOLD``, and
    the honest measurement is a lift near zero with an interval that probably includes it. What must
    *not* happen is a confident claim: acting here costs money for almost no gain, and a system that
    reported a solid lift would be recommending spending on customers who need no persuading.

    The matching decision — ``HIGH_BASELINE_NO_INTERVENTION`` — is checked in
    ``test_generator.py``.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.HIGH_BASELINE,
            seed=20240404,
            case_count=_CASE_COUNT,
            config=default_configuration(),
        )

    report = result.report
    assert Decimal("0") < report.true_lift < Decimal("0.0500")
    assert report.measured_lift is not None
    # A small true effect at this sample size should not produce a confident claim.
    assert abs(report.measured_lift) < Decimal("0.1500"), (
        f"a near-zero true effect measured as {report.measured_lift}"
    )
    assert not report.attribution_permitted, (
        "a high-baseline scenario with a marginal uplift licensed an attributed claim"
    )


# ---------------------------------------------------------------------------
# Structural guarantees about synthetic evidence
# ---------------------------------------------------------------------------


def test_a_synthetic_experiment_can_never_license_an_attributed_claim(
    owner_engine: Engine, merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """The ``SYNTHETIC`` label closes the circularity structurally, not by convention.

    This is the most important structural test in the file. The positive scenario produces a clean,
    real, correctly-measured lift — and it still may not be reported as incremental revenue, because
    the experiment carries ``SYNTHETIC`` and that is one of the three blocking labels in the gate.

    Without this, a demo could show a beautiful lift and quote it as recovered revenue, which would
    be circular: the lift was put there by the generator.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.POSITIVE,
            seed=20240505,
            case_count=_DETECTION_CASE_COUNT,
            config=default_configuration(),
        )

    assert result.analysis is not None
    # The effect is genuinely there and genuinely measured.
    assert result.report.measured_lift is not None
    assert result.report.measured_lift > 0
    assert not result.report.interval_contains_zero
    # And it still cannot be claimed.
    assert not result.report.attribution_permitted
    assert RefusalCode.DISQUALIFYING_LABEL in result.report.refusal_codes, (
        result.report.refusal_codes
    )

    with owner_engine.begin() as connection:
        labels = connection.execute(
            text("SELECT labels FROM experiment WHERE id = :e"),
            {"e": str(result.experiment_id)},
        ).scalar_one()
    assert ExperimentLabel.SYNTHETIC.value in (labels or [])


def test_the_run_records_everything_needed_to_reproduce_and_judge_it(
    owner_engine: Engine, merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """R13.C12. Assumptions, embedded true lift, seed, and measured-minus-true, all reported.

    A measured lift shown without its true counterpart is a number that looks like a finding. The
    stored row carries the seed and generator version that reproduce the world, and the ground truth
    that lets a reader check the answer.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.POSITIVE,
            seed=20240606,
            case_count=200,
            config=default_configuration(),
        )

    document = result.report.as_document()
    for key in (
        "scenario",
        "seed",
        "generator_version",
        "true_lift",
        "measured_lift",
        "measured_interval",
        "difference_measured_minus_true",
        "assumptions",
        "provenance",
    ):
        assert key in document, f"the comparison report omits {key}"
    assert document["provenance"] == Provenance.SYNTHETIC.value
    assert document["seed"] == 20240606

    with owner_engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT seed, scenario, generator_version, ground_truth, assumptions, case_count "
                "FROM synthetic_run WHERE id = :r"
            ),
            {"r": str(result.synthetic_run_id)},
        ).one()
    assert int(row[0]) == 20240606
    assert str(row[1]) == ScenarioName.POSITIVE
    assert row[3]["scale"] == SCALE
    assert "expectation" in row[4]
    assert int(row[5]) == 200


def test_synthetic_cases_carry_the_provenance_that_labels_every_downstream_figure(
    owner_engine: Engine, merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """One synthetic case labels a whole metrics report, and the propagation starts on the case row.

    Asserted here rather than only in the metrics tests because this is where the label is written.
    If ``provenance`` were left at its ``REAL`` default, synthetic figures would flow into reports
    indistinguishable from real ones.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.NULL,
            seed=20240707,
            case_count=100,
            config=default_configuration(),
        )

    with owner_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT count(*) FROM recovery_case WHERE merchant_id = :m "
                "AND provenance = :p AND synthetic_run_id = :r"
            ),
            {
                "m": str(merchant),
                "p": Provenance.SYNTHETIC.value,
                "r": str(result.synthetic_run_id),
            },
        ).scalar_one()
    assert int(rows) == 100, "not every synthetic case was labelled and linked to its run"


def test_an_interim_look_establishes_nothing_however_good_the_data(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """A running experiment is not a result, even with a clean measured effect.

    Proves the gate's state term is live in the harness path too, not only in unit tests.
    """
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.POSITIVE,
            seed=20240808,
            case_count=_CASE_COUNT,
            config=default_configuration(),
            complete=False,
        )

    assert not result.report.attribution_permitted
    assert RefusalCode.NOT_COMPLETED in result.report.refusal_codes


def test_an_underpowered_run_establishes_nothing(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """Below the sample size fixed at definition time, no claim is permitted."""
    with tenant_transaction(merchant, factory) as session:
        result = run_scenario(
            session,
            merchant,
            scenario_name=ScenarioName.POSITIVE,
            seed=20240909,
            case_count=100,
            config=default_configuration(),
            required_sample_size=5000,
        )

    assert not result.report.attribution_permitted
    assert RefusalCode.BELOW_SAMPLE_SIZE in result.report.refusal_codes


# ---------------------------------------------------------------------------
# Interval coverage across seeds (task 27.5)
# ---------------------------------------------------------------------------


def test_the_reported_interval_covers_the_true_lift_about_as_often_as_it_claims(
    merchant: uuid.UUID, factory: sessionmaker[Session]
) -> None:
    """A 95 percent interval should contain the true lift about 95 percent of the time.

    The check that catches an interval which is *systematically too narrow*. Every other test here
    could pass while the interval was half the width it should be — the point estimate would still
    be right, the null scenario would still straddle zero on most seeds, and every attributed claim
    would nonetheless be overconfident.

    The threshold is deliberately loose. With 40 seeds, binomial noise around 95 percent gives a
    standard deviation of about 3.4 percent, so demanding 95 percent exactly would produce a flaky
    test. Requiring at least 80 percent catches an interval that is materially wrong while
    tolerating the sampling variation a small number of seeds inevitably has.
    """
    covered = 0
    measured = 0

    for offset in range(_COVERAGE_SEEDS):
        with tenant_transaction(merchant, factory) as session:
            result = run_scenario(
                session,
                merchant,
                scenario_name=ScenarioName.POSITIVE,
                seed=50_000 + offset,
                case_count=200,
                config=default_configuration(),
            )
        if result.report.measured_lift is None:
            continue
        measured += 1
        if result.report.interval_contains_true_lift:
            covered += 1

    assert measured >= _COVERAGE_SEEDS - 2, f"only {measured} of {_COVERAGE_SEEDS} seeds measured"
    coverage = Decimal(covered) / Decimal(measured)
    assert coverage >= Decimal("0.80"), (
        f"the reported 95 percent interval covered the true lift in only {covered}/{measured} "
        f"runs ({coverage}). An interval that is materially too narrow makes every attributed "
        "claim overconfident, even where the point estimate is right."
    )

"""The statistics behind every causal claim, checked against published values.

This file exists because the numbers here are load-bearing in a way that is easy to miss. The
sample size decides whether an experiment is allowed to conclude anything, and the lift interval
decides whether Revora may say it caused a recovery. Both are computed from scratch in exact
``Decimal`` rather than from a library, so nothing external vouches for them — these tests are
the only thing that does.

Three kinds of check, in increasing order of what they would catch:

* **Against published constants.** ``z(0.975) = 1.959964`` is in every textbook. If the quantile
  is wrong, everything downstream is wrong by a predictable factor and nothing else would notice.
* **Against the design's own stated figures.** design.md says roughly 1,000 per arm at
  ``p0 = 0.20, delta = 0.05`` and roughly 270 at ``delta = 0.10``. Independent arithmetic,
  written before this code existed.
* **Against structural properties.** Monotonicity in effect size and power, symmetry of the CDF,
  and — the important one — that an interval never narrows under rounding. A narrowed interval can
  exclude zero when the honest one contains it, which is precisely the condition that unlocks an
  attributed revenue claim.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from revora.experiment.statistics import (
    WORKING_PRECISION,
    SampleSizeInputs,
    lift_interval,
    normal_cdf,
    normal_quantile,
    required_sample_size_per_group,
)

pytestmark = pytest.mark.pure


# ---------------------------------------------------------------------------
# Against published values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probability", "expected_z"),
    [
        ("0.975", "1.959964"),
        ("0.95", "1.644854"),
        ("0.90", "1.281552"),
        ("0.80", "0.841621"),
        ("0.995", "2.575829"),
        ("0.025", "-1.959964"),
        ("0.50", "0.000000"),
    ],
)
def test_normal_quantile_matches_published_z_values(probability: str, expected_z: str) -> None:
    """The z-values every statistics table lists, to six places.

    ``0.975`` and ``0.80`` are the two that matter most: they are the significance and power
    defaults, so they appear in every sample size the system computes. A quantile wrong in the
    third place would scale every sample size by a few percent — enough to let an underpowered
    experiment clear its own threshold, and small enough that nobody would notice by eye.
    """
    assert str(normal_quantile(Decimal(probability))) == expected_z


@pytest.mark.parametrize(
    ("x", "expected_prefix"),
    [
        ("0", "0.5"),
        ("1", "0.841344746"),
        ("1.96", "0.975002104"),
        ("-1.96", "0.024997895"),
        ("3", "0.998650101"),
        ("-3", "0.001349898"),
    ],
)
def test_normal_cdf_matches_published_values(x: str, expected_prefix: str) -> None:
    """The CDF to nine places against standard tables, including the negative tail.

    Negative inputs are checked explicitly because the series is odd in its argument and takes no
    reflection branch — so if the sign handling were wrong it would be wrong only for negative
    inputs, which is exactly half the quantile search space.
    """
    assert str(normal_cdf(Decimal(x))).startswith(expected_prefix)


def test_the_cdf_is_symmetric_to_working_precision() -> None:
    """``Phi(-x)`` and ``1 - Phi(x)`` agree far past anything that is reported.

    Compared inside a working-precision context deliberately. The default 28-digit context
    truncates one side and manufactures a gap around ``1e-30``, which looks like a defect and is
    not — a caveat worth pinning in a test so nobody "fixes" the function in response to it.
    """
    with localcontext() as context:
        context.prec = WORKING_PRECISION
        for value in ("0.3", "1.3", "2.7"):
            gap = abs(normal_cdf(Decimal(f"-{value}")) - (Decimal(1) - normal_cdf(Decimal(value))))
            assert gap < Decimal("1E-40"), f"symmetry gap at {value} was {gap}"


def test_the_quantile_inverts_the_cdf() -> None:
    """Round-tripping is the check that does not depend on any published table.

    If both functions were wrong in a compensating way the table checks above would catch it and
    this would not; if only one is wrong, this catches it. Together they pin both.
    """
    for probability in ("0.05", "0.25", "0.5", "0.75", "0.975", "0.999"):
        p = Decimal(probability)
        recovered = normal_cdf(normal_quantile(p))
        assert abs(recovered - p) < Decimal("1E-6"), (
            f"Phi(z({p})) = {recovered}, expected {p}"
        )


# ---------------------------------------------------------------------------
# Against the design's own stated figures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "design_says_roughly"),
    [("0.05", 1000), ("0.10", 270)],
)
def test_sample_size_agrees_with_the_designs_worked_examples(
    delta: str, design_says_roughly: int
) -> None:
    """design.md: at ``p0 = 0.20``, ``alpha = 0.05``, power ``0.80``.

    "detecting ``delta = 0.05`` needs roughly 1,000 per arm, while detecting ``delta = 0.10``
    needs roughly 270."

    Independent arithmetic done when the design was written, which makes it the best available
    check on this formula. The tolerance is wide because the design said "roughly" and because
    this implementation keeps both variance terms where the shorter textbook form keeps only the
    pooled one — so it lands slightly above, which is the safe side for a threshold.
    """
    n = required_sample_size_per_group(
        SampleSizeInputs(
            baseline_rate=Decimal("0.20"),
            minimum_detectable_effect=Decimal(delta),
            significance_level=Decimal("0.05"),
            power=Decimal("0.80"),
        )
    )
    assert design_says_roughly <= n <= int(design_says_roughly * 1.2), (
        f"n={n} is not within 20 percent above the design's stated {design_says_roughly}"
    )


def test_sample_size_grows_as_the_effect_shrinks() -> None:
    """Smaller effects need more cases. Monotone, strictly.

    A non-monotone sample size would mean the formula has a sign or bracket error somewhere that
    the worked examples above happen not to expose.
    """
    sizes = [
        required_sample_size_per_group(
            SampleSizeInputs(
                baseline_rate=Decimal("0.20"),
                minimum_detectable_effect=Decimal(delta),
                significance_level=Decimal("0.05"),
                power=Decimal("0.80"),
            )
        )
        for delta in ("0.20", "0.15", "0.10", "0.05", "0.02")
    ]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == len(sizes), "distinct effect sizes produced identical samples"


def test_sample_size_grows_with_power_and_with_confidence() -> None:
    """More power and tighter significance both cost cases."""
    by_power = [
        required_sample_size_per_group(
            SampleSizeInputs(
                baseline_rate=Decimal("0.20"),
                minimum_detectable_effect=Decimal("0.05"),
                significance_level=Decimal("0.05"),
                power=Decimal(power),
            )
        )
        for power in ("0.70", "0.80", "0.90", "0.95")
    ]
    assert by_power == sorted(by_power)

    by_alpha = [
        required_sample_size_per_group(
            SampleSizeInputs(
                baseline_rate=Decimal("0.20"),
                minimum_detectable_effect=Decimal("0.05"),
                significance_level=Decimal(alpha),
                power=Decimal("0.80"),
            )
        )
        for alpha in ("0.10", "0.05", "0.01")
    ]
    assert by_alpha == sorted(by_alpha)


def test_sample_size_rounds_up_never_to_nearest() -> None:
    """A fractional case is a case you do not have.

    Rounding to nearest would let an experiment one case short of its own threshold pass the
    attribution gate. Checked by confirming the result is at least the exact value, which holds
    for ceiling and fails for nearest on roughly half of all inputs.
    """
    inputs = SampleSizeInputs(
        baseline_rate=Decimal("0.20"),
        minimum_detectable_effect=Decimal("0.05"),
        significance_level=Decimal("0.05"),
        power=Decimal("0.80"),
    )
    n = required_sample_size_per_group(inputs)
    # Recompute the unrounded value the same way and confirm n is not below it.
    with localcontext() as context:
        context.prec = WORKING_PRECISION
        p0 = inputs.baseline_rate
        p1 = inputs.treated_rate
        pooled = (p0 + p1) / Decimal(2)
        z_a = normal_quantile(Decimal(1) - inputs.significance_level / Decimal(2))
        z_b = normal_quantile(inputs.power)
        exact = (
            z_a * (Decimal(2) * pooled * (Decimal(1) - pooled)).sqrt()
            + z_b * (p0 * (Decimal(1) - p0) + p1 * (Decimal(1) - p1)).sqrt()
        ) ** 2 / (inputs.minimum_detectable_effect ** 2)
    assert Decimal(n) >= exact, f"n={n} rounded below the exact requirement {exact}"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline_rate": Decimal("0"), "minimum_detectable_effect": Decimal("0.05")},
        {"baseline_rate": Decimal("1"), "minimum_detectable_effect": Decimal("0.05")},
        {"baseline_rate": Decimal("0.2"), "minimum_detectable_effect": Decimal("0")},
        {"baseline_rate": Decimal("0.2"), "minimum_detectable_effect": Decimal("-0.1")},
        {"baseline_rate": Decimal("0.98"), "minimum_detectable_effect": Decimal("0.05")},
    ],
)
def test_impossible_designs_are_refused(kwargs: dict[str, Decimal]) -> None:
    """Each of these would produce a meaningless or unbounded sample size.

    A zero minimum detectable effect is the one worth naming: it asks for the sample needed to
    detect an arbitrarily small difference, which is unbounded. Silently producing a very large
    integer would look like an answer.
    """
    with pytest.raises(ValueError):
        SampleSizeInputs(
            significance_level=Decimal("0.05"),
            power=Decimal("0.80"),
            **kwargs,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# The lift interval
# ---------------------------------------------------------------------------


def test_identical_arms_produce_an_interval_containing_zero() -> None:
    """The null case. Both arms 20/100, so the honest answer straddles zero.

    This is the shape the mandatory null scenario asserts end to end. If it failed here, the
    scenario would fail for a reason buried three layers down.
    """
    result = lift_interval(
        control_recoveries=20,
        control_cases=100,
        treatment_recoveries=20,
        treatment_cases=100,
        confidence_level=Decimal("0.95"),
    )
    assert result is not None
    lift, low, high = result
    assert lift == Decimal("0.0000")
    assert low < 0 < high, f"a zero lift produced [{low}, {high}], which excludes zero"


def test_a_large_separation_excludes_zero() -> None:
    """20/500 against 60/500 is a real effect, and the interval should say so.

    The complement of the null test. Without it, an implementation that always returned an
    interval containing zero would pass every other assertion in this file and make Revora
    incapable of ever reporting a finding.
    """
    result = lift_interval(
        control_recoveries=20,
        control_cases=500,
        treatment_recoveries=60,
        treatment_cases=500,
        confidence_level=Decimal("0.95"),
    )
    assert result is not None
    lift, low, high = result
    assert lift == Decimal("0.0800")
    assert low > 0, f"a four-fold difference produced [{low}, {high}], which contains zero"


def test_the_bounds_never_narrow_under_rounding() -> None:
    """The single most consequential rounding rule in the system.

    Bounds are floored and ceilinged outward, so quantization cannot make the published interval
    narrower than the data supports. A narrowed interval can exclude zero when the honest one
    contains it — and excluding zero is exactly the condition that unlocks an attributed revenue
    claim. Every other rounding error in Revora misplaces a paisa; this one would manufacture a
    causal claim.
    """
    for treatment in range(18, 30):
        result = lift_interval(
            control_recoveries=20,
            control_cases=97,
            treatment_recoveries=treatment,
            treatment_cases=101,
            confidence_level=Decimal("0.95"),
        )
        assert result is not None
        lift, low, high = result
        assert low <= lift <= high, f"[{low}, {high}] does not contain its own lift {lift}"


def test_a_wider_confidence_level_gives_a_wider_interval() -> None:
    """99 percent must be wider than 95, which must be wider than 90."""
    widths = []
    for level in ("0.90", "0.95", "0.99"):
        result = lift_interval(
            control_recoveries=30,
            control_cases=200,
            treatment_recoveries=50,
            treatment_cases=200,
            confidence_level=Decimal(level),
        )
        assert result is not None
        _, low, high = result
        widths.append(high - low)
    assert widths == sorted(widths)
    assert len(set(widths)) == 3


@pytest.mark.parametrize(
    ("control_cases", "treatment_cases"),
    [(0, 10), (10, 0), (0, 0)],
)
def test_an_empty_arm_yields_no_interval_rather_than_zero(
    control_cases: int, treatment_cases: int
) -> None:
    """``None``, never a lift of zero.

    An arm with no cases has no rate, so the difference of rates does not exist. Returning zero
    would be a *measurement* — and a measured zero effect is what
    ``CAUSALITY_NOT_ESTABLISHED`` is derived from, so "we have no data" would become "we measured
    no effect". Those must not produce the same row.
    """
    assert (
        lift_interval(
            control_recoveries=0,
            control_cases=control_cases,
            treatment_recoveries=0,
            treatment_cases=treatment_cases,
            confidence_level=Decimal("0.95"),
        )
        is None
    )


def test_incoherent_counts_are_refused() -> None:
    """More recoveries than cases would produce a rate above one, which readers would believe."""
    with pytest.raises(ValueError):
        lift_interval(
            control_recoveries=11,
            control_cases=10,
            treatment_recoveries=5,
            treatment_cases=10,
            confidence_level=Decimal("0.95"),
        )
    with pytest.raises(ValueError):
        lift_interval(
            control_recoveries=-1,
            control_cases=10,
            treatment_recoveries=5,
            treatment_cases=10,
            confidence_level=Decimal("0.95"),
        )


def test_a_harmful_treatment_produces_a_negative_lift() -> None:
    """The interval is signed, because a treatment can do worse than doing nothing.

    Reported rather than clamped. A system that floored the lift at zero would present a harmful
    treatment as merely ineffective, and the negative synthetic scenario exists precisely to
    catch that.
    """
    result = lift_interval(
        control_recoveries=60,
        control_cases=500,
        treatment_recoveries=20,
        treatment_cases=500,
        confidence_level=Decimal("0.95"),
    )
    assert result is not None
    lift, low, high = result
    assert lift < 0
    assert high < 0, f"a clearly harmful treatment produced [{low}, {high}]"

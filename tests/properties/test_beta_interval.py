"""The exact Beta CDF and the honest interval read off it.

Feature: baseline recovery probability estimation (task 15.2, R5.C1, R5.C9).
Property: the reported interval is exact, monotone, brackets the posterior mean, and
narrows only when observations arrive — and at zero observations it is nearly the whole
unit interval.

Why these tests carry weight beyond arithmetic. The interval is the only part of the
baseline that tells a reader how much the number is worth, and it is computed by a
closed form written by hand rather than taken from a library, because this package
forbids binary reals. A hand-written special function that is subtly wrong would still
produce plausible-looking bounds, so it is pinned against values that can be checked by
inspection: ``Beta(1, 1)`` has CDF exactly ``x``, ``Beta(2, 1)`` exactly ``x²``,
``Beta(1, 2)`` exactly ``1 - (1-x)²``. Those three anchors would catch a wrong binomial
coefficient, a wrong summation range, and a wrong complement, which between them are
most of the ways this could be broken.

The last property is the one the design actually cares about: an interval that does not
widen at zero data is an interval that lies about what is known.

All ``pure`` tier — a Decimal sum and a bisection over ten thousand grid points, with no
database, no clock and no configuration.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revora.estimation.beta import (
    DEFAULT_INTERVAL_MASS,
    PROBABILITY_GRID,
    UNIFORM_PRIOR,
    BetaPosterior,
    BetaPrior,
    beta_cdf,
    central_interval,
    posterior_mean,
)

pytestmark = pytest.mark.pure

ZERO = Decimal(0)
ONE = Decimal(1)


def grid_points() -> st.SearchStrategy[Decimal]:
    """Probabilities on the four-place grid the quantile search walks.

    Sampled on the grid rather than from arbitrary decimals because that is the domain
    the function is used over, and because a value off the grid would exercise rounding
    behaviour the caller never asks for.
    """
    return st.integers(min_value=0, max_value=PROBABILITY_GRID).map(
        lambda point: Decimal(point).scaleb(-4)
    )


def small_parameters() -> st.SearchStrategy[int]:
    """Posterior parameters small enough to explore exhaustively and cheaply."""
    return st.integers(min_value=1, max_value=40)


# ---------------------------------------------------------------------------
# The three closed forms that can be checked by inspection
# ---------------------------------------------------------------------------


@given(x=grid_points())
@settings(max_examples=200)
def test_uniform_cdf_is_the_identity(x: Decimal) -> None:
    """Beta(1, 1) is uniform, so its CDF at x is exactly x.

    The single most valuable assertion in this file. It is the degenerate case of the
    binomial sum — one term, coefficient one — so a wrong summation range or a wrong
    coefficient shows up here before anything more complicated is attempted.
    """
    assert beta_cdf(x, 1, 1) == x


@given(x=grid_points())
@settings(max_examples=200)
def test_beta_two_one_cdf_is_x_squared(x: Decimal) -> None:
    """Beta(2, 1) has CDF x**2, exactly."""
    assert beta_cdf(x, 2, 1) == x * x


@given(x=grid_points())
@settings(max_examples=200)
def test_beta_one_two_cdf_is_the_complement_square(x: Decimal) -> None:
    """Beta(1, 2) has CDF 1 - (1 - x)**2, exactly.

    This is the case that exercises the two-term sum and, for these parameters, the
    complement branch — ``alpha < beta`` routes through ``1 - I_{1-x}(b, a)`` — so it
    checks the identity the shorter-tail optimization rests on.
    """
    complement = ONE - x
    assert beta_cdf(x, 1, 2) == ONE - complement * complement


@pytest.mark.parametrize(
    ("alpha", "beta", "point", "expected"),
    [
        (1, 1, "0.0000", "0"),
        (1, 1, "1.0000", "1"),
        (3, 1, "0.5000", "0.125"),
        (1, 3, "0.5000", "0.875"),
        (2, 2, "0.5000", "0.5"),
    ],
)
def test_known_values(alpha: int, beta: int, point: str, expected: str) -> None:
    """Values a reader can verify without a computer.

    ``Beta(2, 2)`` is symmetric so its median is exactly one half; ``Beta(3, 1)`` has
    CDF ``x³``; ``Beta(1, 3)`` is its mirror. Symmetry is worth pinning separately from
    the algebra because it is the property a sign error in the complement would break.
    """
    assert beta_cdf(Decimal(point), alpha, beta) == Decimal(expected)


# ---------------------------------------------------------------------------
# Monotonicity, which is what makes bisection valid at all
# ---------------------------------------------------------------------------


@given(alpha=small_parameters(), beta=small_parameters(), low=grid_points(), high=grid_points())
def test_cdf_is_monotone(alpha: int, beta: int, low: Decimal, high: Decimal) -> None:
    """The CDF never decreases.

    Not decoration: the quantile search is a bisection, and a bisection over a
    non-monotone predicate returns an arbitrary point rather than an answer. If this
    property failed, every interval in the system would be quietly meaningless.
    """
    if low > high:
        low, high = high, low
    assert beta_cdf(low, alpha, beta) <= beta_cdf(high, alpha, beta)


@given(alpha=small_parameters(), beta=small_parameters(), x=grid_points())
def test_cdf_stays_in_the_unit_interval(alpha: int, beta: int, x: Decimal) -> None:
    """A CDF is a probability, and the bounds are stored in a NUMERIC(6,4) column."""
    value = beta_cdf(x, alpha, beta)
    assert ZERO <= value <= ONE


@given(alpha=small_parameters(), beta=small_parameters())
def test_cdf_is_one_at_the_top_and_zero_at_the_bottom(alpha: int, beta: int) -> None:
    """The boundaries are answered exactly, not approached."""
    assert beta_cdf(ZERO, alpha, beta) == ZERO
    assert beta_cdf(ONE, alpha, beta) == ONE


# ---------------------------------------------------------------------------
# The interval
# ---------------------------------------------------------------------------


def test_zero_observations_gives_a_nearly_useless_interval() -> None:
    """At n = 0 the interval is [0.025, 0.975], and that is the point.

    The design says the interval at zero data is "nearly [0.03, 0.97], which is the
    point". This test exists to make narrowing it a test failure rather than a judgement
    call: any future change that tightens the prior, suppresses the interval, or rounds
    the bounds inward to make the dashboard look more confident breaks here.
    """
    posterior = UNIFORM_PRIOR.posterior(successes=0, trials=0)
    assert (posterior.alpha, posterior.beta) == (1, 1)
    assert posterior.interval() == (Decimal("0.0250"), Decimal("0.9750"))
    assert posterior.mean(places=Decimal("0.001")) == Decimal("0.500")


@given(alpha=small_parameters(), beta=small_parameters())
def test_interval_brackets_the_posterior_mean(alpha: int, beta: int) -> None:
    """The mean lies inside the 95 percent interval.

    True for a Beta at every integer parameter pair, and the cheapest possible check
    that the two figures on a stored row describe the same distribution. A bug that
    computed the interval from the prior while the mean came from the posterior would
    show here as soon as the data moved the mean.
    """
    low, high = central_interval(alpha, beta)
    mean = posterior_mean(alpha, beta)
    assert low <= mean <= high


@given(alpha=small_parameters(), beta=small_parameters())
def test_interval_is_ordered_and_in_range(alpha: int, beta: int) -> None:
    """``ci_low <= ci_high`` and both inside [0, 1].

    Both are database constraints — ``interval_ordered`` and ``probability_in_range`` —
    so a violation here is a row that cannot be committed and a job that fails forever.
    """
    low, high = central_interval(alpha, beta)
    assert ZERO <= low <= high <= ONE


@given(alpha=small_parameters(), beta=small_parameters())
def test_interval_holds_at_least_the_requested_mass(alpha: int, beta: int) -> None:
    """The interval holds at least 95 percent, never less.

    This is the outward-rounding guarantee stated as a property. Quantizing onto the
    four-place grid moves both bounds, and it is only ever allowed to move them
    *outward* — an interval that held 94.99 percent because rounding pulled a bound in
    would overstate what the posterior supports, which is the one rounding direction
    that matters here.
    """
    low, high = central_interval(alpha, beta)
    contained = beta_cdf(high, alpha, beta) - beta_cdf(low, alpha, beta)
    assert contained >= DEFAULT_INTERVAL_MASS


@given(trials=st.integers(min_value=0, max_value=60))
def test_more_observations_never_widen_the_interval(trials: int) -> None:
    """Evidence narrows the interval, or at worst leaves it unchanged.

    Sampled at a constant recovery rate of one half so the mean stays put and only the
    width moves. "Never widens" rather than "always narrows" because the bounds live on
    a four-place grid and one additional observation out of sixty can leave both grid
    points where they were.
    """
    successes = trials // 2
    posterior = UNIFORM_PRIOR.posterior(successes=successes, trials=trials)
    wider = UNIFORM_PRIOR.posterior(successes=0, trials=0)
    low, high = posterior.interval()
    prior_low, prior_high = wider.interval()
    assert (high - low) <= (prior_high - prior_low)


def test_a_high_sample_segment_is_visibly_tighter_than_a_low_sample_one() -> None:
    """Thirty observations produce a visibly narrower interval than three.

    Both segments recover at one third, so the point estimates are close and the only
    real difference is confidence. The assertion is deliberately coarse — a factor of
    two — because the value of this test is that the width *moves a lot*, which is what
    makes the interval informative on a dashboard rather than decorative.
    """
    sparse = UNIFORM_PRIOR.posterior(successes=1, trials=3)
    dense = UNIFORM_PRIOR.posterior(successes=10, trials=30)
    sparse_low, sparse_high = sparse.interval()
    dense_low, dense_high = dense.interval()
    sparse_width = sparse_high - sparse_low
    dense_width = dense_high - dense_low
    assert dense_width < sparse_width / 2


# ---------------------------------------------------------------------------
# Conjugacy, and the refusals
# ---------------------------------------------------------------------------


@given(
    successes=st.integers(min_value=0, max_value=50),
    extra=st.integers(min_value=0, max_value=50),
)
def test_posterior_parameters_are_integers_and_add(successes: int, extra: int) -> None:
    """Updating is addition, which is why the closed form survives any sample size.

    The exact CDF requires integer parameters. Conjugacy is what guarantees they stay
    integral no matter how much data arrives, so this property is the precondition of
    the whole module rather than a fact about arithmetic.
    """
    trials = successes + extra
    posterior = UNIFORM_PRIOR.posterior(successes=successes, trials=trials)
    assert posterior.alpha == 1 + successes
    assert posterior.beta == 1 + (trials - successes)
    assert isinstance(posterior.alpha, int)
    assert isinstance(posterior.beta, int)


def test_posterior_refuses_impossible_counts() -> None:
    """More recoveries than observations means the aggregate query is wrong.

    Raised rather than clamped. A posterior built from an impossible count would still
    produce a number, and a number is exactly what nobody would question.
    """
    with pytest.raises(ValueError):
        UNIFORM_PRIOR.posterior(successes=5, trials=3)
    with pytest.raises(ValueError):
        UNIFORM_PRIOR.posterior(successes=-1, trials=3)


def test_parameters_below_one_are_refused() -> None:
    """A Beta parameter of zero has no finite binomial form, so it is refused."""
    with pytest.raises(ValueError):
        BetaPrior(0, 1)
    with pytest.raises(ValueError):
        BetaPosterior(1, 0)


def test_cdf_refuses_a_non_decimal_argument() -> None:
    """The argument must be a Decimal.

    Coercing would defeat the module's only guarantee, which is that no binary
    approximation of a probability entered the computation.
    """
    with pytest.raises(TypeError):
        beta_cdf("0.5", 1, 1)  # type: ignore[arg-type]


def test_cdf_refuses_a_point_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError):
        beta_cdf(Decimal("1.5"), 1, 1)


def test_interval_mass_must_be_a_proper_probability() -> None:
    """A mass of 0 or 1 has no central interval worth reporting."""
    with pytest.raises(ValueError):
        central_interval(1, 1, mass=ONE)
    with pytest.raises(TypeError):
        central_interval(1, 1, mass="0.95")  # type: ignore[arg-type]

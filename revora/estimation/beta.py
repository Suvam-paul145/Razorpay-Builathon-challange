"""The Beta posterior, computed exactly, with no binary approximation anywhere.

This module exists because of a collision between two hard constraints, and the way
out of it is a piece of undergraduate mathematics rather than a library.

**Constraint one.** Requirement 5 wants a 95 percent interval on every baseline
probability, and the design wants that interval to come from the Beta posterior
quantiles precisely because at zero observations it comes out at roughly
``[0.03, 0.97]`` and says so. A near-useless interval at zero data is the honest
answer; narrowing it, or omitting it, would let a number invented from a uniform
prior read like a measurement.

**Constraint two.** ``scripts/check_no_float.py`` treats this whole package as
currency-bearing, so the token is forbidden here as an annotation, a call, or a
literal. That rules out ``scipy.stats.beta.ppf`` and every other numerical library:
they all hand back binary approximations, and a binary approximation of a bound that
gets stored in a ``NUMERIC(6,4)`` column and multiplied into money is exactly the
class of value this project refuses to hold.

The resolution is that we never need the general case. A Beta-Binomial posterior
built on the design's ``alpha = beta = 1`` prior has

    alpha_post = 1 + successes,   beta_post = 1 + failures

and both are **positive integers**, always. For integer parameters the regularized
incomplete beta function — which is the Beta CDF — is not a transcendental integral
at all. It is a finite binomial sum::

    I_x(a, b) = Σ_{j=a}^{a+b-1} C(a+b-1, j) · x^j · (1-x)^(a+b-1-j)

Every factor of that is a rational operation on a decimal quantity, so it evaluates
in :class:`decimal.Decimal` with :func:`math.comb` supplying an exact integer
binomial coefficient. Sanity anchors, both checked in
``tests/properties/test_beta_interval.py``: for ``Beta(1, 1)`` the sum collapses to
``x``, and for ``Beta(2, 1)`` it collapses to ``x²``.

**Why the working precision is 50 and why that is not hand-waving.** Every term of
the sum is non-negative and the total is bounded by 1, so there is no cancellation
and no growth — the error of a 50-digit context accumulates additively across at
most ``min(alpha, beta)`` terms, which puts it around ``10⁻⁴⁵`` for any input this system
can produce. We round to four decimal places. Forty-five orders of magnitude of
headroom is not a close call.

**Quantiles come from bisection, not from an inverse.** There is no closed form for
the inverse, but the CDF is strictly increasing on ``(0, 1)``, and a probability is
recorded to four decimal places, so the answer lives on a grid of 10 001 candidate
values. Bisecting that grid takes fourteen CDF evaluations and lands on the exact
grid point rather than near it.

**The rounding is deliberately outward.** The lower bound is the largest grid point
whose CDF does not exceed the tail mass and the upper bound is the smallest grid
point whose CDF reaches its target, so quantization can only ever widen the reported
interval. Rounding to nearest would sometimes narrow it by a ten-thousandth, and an
interval that is narrower than the posterior actually supports is the one error mode
that matters here — the whole point of publishing the interval is that it must not
overstate what is known.

**Cost.** One CDF evaluation is ``O(min(alpha, beta))`` exact multiplications, and the
shorter of the two tails is always the one summed, via
``I_x(a, b) = 1 - I_{1-x}(b, a)``. An interval is two bisections, so about
``28 · min(alpha, beta)`` terms. At the sample sizes this system sees — ``MIN_SEGMENT_SAMPLE_SIZE``
is 30 — that is microseconds. The baseline service still applies
``BASELINE_ESTIMATION_TIMEOUT`` around it, because a segment that has accumulated an
enormous number of observations should degrade into a recorded failure rather than
into a slow job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

__all__ = [
    "DEFAULT_INTERVAL_MASS",
    "PROBABILITY_GRID",
    "PROBABILITY_PLACES",
    "UNIFORM_PRIOR",
    "WORKING_PRECISION",
    "BetaPosterior",
    "BetaPrior",
    "beta_cdf",
    "central_interval",
    "posterior_mean",
]

WORKING_PRECISION: Final[int] = 50
"""Digits of working precision for the sum. See the module docstring: all terms are
non-negative, so there is nothing to cancel and the headroom over the four places we
report is around forty-five orders of magnitude."""

PROBABILITY_PLACES: Final[Decimal] = Decimal("0.0001")
"""Four places, matching ``domain.probability.Probability`` and the
``NUMERIC(6,4)`` columns the bounds are stored in."""

PROBABILITY_GRID: Final[int] = 10_000
"""The number of intervals in the four-place grid a quantile is searched over. A
grid point ``k`` denotes the probability ``k / 10000`` exactly."""

DEFAULT_INTERVAL_MASS: Final[Decimal] = Decimal("0.95")
"""R5.C9 asks for a 95 percent interval. Central rather than highest-density: a
central interval is defined by two quantiles of a monotone function, which is what
makes it computable exactly by the method above, and it is the interval the design
names."""

_ZERO: Final[Decimal] = Decimal(0)
_ONE: Final[Decimal] = Decimal(1)
_TWO: Final[Decimal] = Decimal(2)


# ---------------------------------------------------------------------------
# The prior and the posterior
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BetaPrior:
    """A Beta prior with integer parameters.

    Integers are not a simplification of convenience — they are the precondition
    for the closed form in this module. A merchant-supplied prior therefore has to
    arrive as a pair of positive integers, which is also the form that can be stated
    honestly: ``BetaPrior(3, 7)`` means "before seeing anything, behave as though ten
    comparable payments had been observed and three of them recovered". A pair of
    decimals would say nothing a reader could check.
    """

    alpha: int
    beta: int

    def __post_init__(self) -> None:
        if not isinstance(self.alpha, int) or not isinstance(self.beta, int):
            raise TypeError("Beta parameters must be integers; the exact CDF requires it")
        if self.alpha < 1 or self.beta < 1:
            raise ValueError(
                f"Beta parameters must be at least 1, got ({self.alpha}, {self.beta})"
            )

    @property
    def pseudo_observations(self) -> int:
        """How many observations this prior is worth. ``UNIFORM_PRIOR`` is worth two."""
        return self.alpha + self.beta

    def posterior(self, *, successes: int, trials: int) -> BetaPosterior:
        """Update on ``successes`` recoveries out of ``trials`` observations.

        Conjugacy makes this addition, which is why the posterior parameters stay
        integral no matter how much data arrives — and therefore why the interval
        stays exactly computable at every sample size rather than only at zero.

        Raises:
            ValueError: if the counts are negative or ``successes`` exceeds
                ``trials``. Both would mean the caller's aggregate query is wrong,
                and a posterior built from an impossible count is worse than a
                failed estimate because it looks like a number.
        """
        if trials < 0 or successes < 0:
            raise ValueError(f"counts must not be negative, got {successes}/{trials}")
        if successes > trials:
            raise ValueError(f"successes {successes} exceeds trials {trials}")
        return BetaPosterior(self.alpha + successes, self.beta + (trials - successes))


UNIFORM_PRIOR: Final[BetaPrior] = BetaPrior(1, 1)
"""``alpha = beta = 1``, the design's weak prior, marked ``[ASSUMPTION]`` there and here.

Uniform over ``[0, 1]``: it asserts that every recovery rate is equally plausible
before any observation. That is a strong claim to make quietly, which is why the
interval it produces at zero data is nearly the whole unit interval — the prior is
not pretending to know anything, and the recorded interval is what says so."""


@dataclass(frozen=True, slots=True)
class BetaPosterior:
    """A Beta posterior with integer parameters, and the two figures read off it."""

    alpha: int
    beta: int

    def __post_init__(self) -> None:
        if not isinstance(self.alpha, int) or not isinstance(self.beta, int):
            raise TypeError("Beta parameters must be integers; the exact CDF requires it")
        if self.alpha < 1 or self.beta < 1:
            raise ValueError(
                f"Beta parameters must be at least 1, got ({self.alpha}, {self.beta})"
            )

    def mean(self, *, places: Decimal = PROBABILITY_PLACES) -> Decimal:
        """The posterior mean ``alpha / (alpha + beta)``, half-up at ``places``."""
        return posterior_mean(self.alpha, self.beta, places=places)

    def cdf(self, x: Decimal) -> Decimal:
        """``P(θ ≤ x)`` under this posterior, exactly."""
        return beta_cdf(x, self.alpha, self.beta)

    def interval(self, *, mass: Decimal = DEFAULT_INTERVAL_MASS) -> tuple[Decimal, Decimal]:
        """The central credible interval holding ``mass``, rounded outward."""
        return central_interval(self.alpha, self.beta, mass=mass)


# ---------------------------------------------------------------------------
# The exact CDF
# ---------------------------------------------------------------------------


def beta_cdf(x: Decimal, alpha: int, beta: int) -> Decimal:
    """The regularized incomplete beta function ``I_x(alpha, beta)``, exactly.

    Args:
        x: the point to evaluate at, as a ``Decimal`` in ``[0, 1]``. A ``Decimal``
            rather than any binary type, both because this package forbids binary
            reals outright and because the grid the quantile search walks is a
            decimal grid — a binary argument could not name its own grid points.
        alpha: first shape parameter, a positive integer.
        beta: second shape parameter, a positive integer.

    Returns:
        ``P(θ ≤ x)`` as a ``Decimal``, unquantized so a caller comparing it against
        a tail mass compares the full computed value rather than a rounded one.

    Raises:
        TypeError: if ``x`` is not a ``Decimal``, or the parameters are not integers.
            Not coerced: the whole guarantee of this module is that no binary
            approximation entered it, and silently accepting one would void that
            while still returning a plausible answer.
        ValueError: if ``x`` lies outside ``[0, 1]`` or a parameter is below 1.
    """
    if not isinstance(x, Decimal):
        raise TypeError(
            f"x must be a Decimal, got {type(x).__name__}; "
            "this module holds no binary approximation of a probability"
        )
    if not isinstance(alpha, int) or not isinstance(beta, int):
        raise TypeError("Beta parameters must be integers; the closed form requires it")
    if alpha < 1 or beta < 1:
        raise ValueError(f"Beta parameters must be at least 1, got ({alpha}, {beta})")
    if x < _ZERO or x > _ONE:
        raise ValueError(f"x must lie in [0, 1], got {x}")

    # The boundaries are answered before the sum rather than by it. At x = 0 every
    # term carries a factor x^j with j >= alpha >= 1, and at x = 1 the complement
    # factor vanishes; evaluating either through the general path would mean raising
    # zero to a non-negative power inside the loop for no benefit.
    if x == _ZERO:
        return _ZERO
    if x == _ONE:
        return _ONE

    trials = alpha + beta - 1
    with localcontext() as context:
        context.prec = WORKING_PRECISION
        # Sum whichever tail has fewer terms. The lower form sums beta terms and
        # the complement sums alpha, and they are equal identically, so taking the
        # shorter one is free accuracy-wise and halves the worst case.
        if beta <= alpha:
            return +_upper_binomial_tail(x, trials=trials, first=alpha)
        return +(_ONE - _upper_binomial_tail(_ONE - x, trials=trials, first=beta))


def _upper_binomial_tail(x: Decimal, *, trials: int, first: int) -> Decimal:
    """``Σ_{j=first}^{trials} C(trials, j) · x^j · (1-x)^(trials-j)``.

    The upper tail of a binomial mass function, which is the identity behind the
    closed form: ``I_x(a, b)`` is the probability that at least ``a`` of ``a+b-1``
    independent trials succeed at rate ``x``.

    Must be called inside a context whose precision is :data:`WORKING_PRECISION`.
    The coefficient comes from :func:`math.comb`, which is exact integer arithmetic,
    so the only inexact steps are the two powers and the multiplication — all of
    them on non-negative quantities, so nothing cancels.
    """
    complement = _ONE - x
    total = _ZERO
    for j in range(first, trials + 1):
        coefficient = Decimal(math.comb(trials, j))
        total += coefficient * (x**j) * (complement ** (trials - j))
    return total


def posterior_mean(alpha: int, beta: int, *, places: Decimal = PROBABILITY_PLACES) -> Decimal:
    """``alpha / (alpha + beta)``, half-up at ``places``.

    With ``alpha = 1 + successes`` and ``beta = 1 + failures`` this is the design's
    ``(alpha + s) / (alpha + beta + n)`` written in terms of the posterior rather than the
    prior. Same quantity, and expressing it this way means the mean and the interval
    are read off one object and cannot drift apart.

    The division runs at :data:`WORKING_PRECISION` before the single rounding, so the
    quantization happens exactly once, on the way out — the same discipline
    ``domain.money.multiply_probability`` applies to money.
    """
    if not isinstance(alpha, int) or not isinstance(beta, int):
        raise TypeError("Beta parameters must be integers")
    if alpha < 1 or beta < 1:
        raise ValueError(f"Beta parameters must be at least 1, got ({alpha}, {beta})")
    with localcontext() as context:
        context.prec = WORKING_PRECISION
        exact = Decimal(alpha) / Decimal(alpha + beta)
    return exact.quantize(places)


# ---------------------------------------------------------------------------
# Quantiles by bisection on the four-place grid
# ---------------------------------------------------------------------------


def central_interval(
    alpha: int, beta: int, *, mass: Decimal = DEFAULT_INTERVAL_MASS
) -> tuple[Decimal, Decimal]:
    """The central credible interval of ``Beta(alpha, beta)`` holding ``mass``.

    Both bounds are grid points of the four-place grid, and both are rounded
    **outward** — the lower bound down, the upper bound up — so the reported interval
    is never narrower than the posterior supports. See the module docstring.

    At ``Beta(1, 1)`` this returns ``(0.0250, 0.9750)``, which is the design's
    "nearly ``[0.03, 0.97]`` at ``n = 0``, which is the point". Nothing in this
    module or above it narrows or suppresses that.

    Args:
        alpha: posterior ``alpha``, a positive integer.
        beta: posterior ``beta``, a positive integer.
        mass: the probability the interval must hold, as a ``Decimal`` strictly
            between 0 and 1.

    Returns:
        ``(lower, upper)``, each a ``Decimal`` quantized to four places, with
        ``lower <= upper``.
    """
    if not isinstance(mass, Decimal):
        raise TypeError(f"mass must be a Decimal, got {type(mass).__name__}")
    if mass <= _ZERO or mass >= _ONE:
        raise ValueError(f"interval mass must lie strictly inside (0, 1), got {mass}")

    with localcontext() as context:
        context.prec = WORKING_PRECISION
        tail = (_ONE - mass) / _TWO

    lower_point = _grid_point_below(tail, alpha, beta)
    upper_point = _grid_point_at_least(_ONE - tail, alpha, beta)
    return _from_grid(lower_point), _from_grid(upper_point)


def _grid_point_at_least(target: Decimal, alpha: int, beta: int) -> int:
    """The smallest grid point whose CDF reaches ``target``.

    Plain bisection over the integer grid. It terminates because the CDF is
    non-decreasing and reaches 1 at the top of the grid, so the predicate "CDF here
    is at least the target" is monotone in the index and true at the last index for
    any target at or below 1.
    """
    low = 0
    high = PROBABILITY_GRID
    while low < high:
        middle = (low + high) // 2
        if beta_cdf(_from_grid(middle), alpha, beta) >= target:
            high = middle
        else:
            low = middle + 1
    return low


def _grid_point_below(target: Decimal, alpha: int, beta: int) -> int:
    """The largest grid point whose CDF does not exceed ``target``.

    Derived from the search above rather than by a second bisection: the first point
    that reaches the target is either exactly on it — in which case it is also the
    largest point that does not exceed it — or one step past it. Zero is clamped to
    itself, which is the honest answer for a tail mass below the smallest
    representable step.
    """
    first = _grid_point_at_least(target, alpha, beta)
    if beta_cdf(_from_grid(first), alpha, beta) == target:
        return first
    return max(0, first - 1)


def _from_grid(point: int) -> Decimal:
    """Grid index to probability. ``scaleb`` rather than division, so it is exact."""
    return Decimal(point).scaleb(-4)

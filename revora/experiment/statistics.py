"""The closed-form statistics an experiment needs, in exact ``Decimal`` arithmetic.

Two quantities, and the whole module exists to compute them honestly:

* **the required sample size per arm**, computed at *definition* time so it is a threshold
  the experiment must reach rather than a description of whatever data happened to arrive;
* **the interval around a measured lift**, because a lift without one invites a causal claim
  it cannot support, and an interval containing zero is the most likely honest answer.

**Why no scipy or statsmodels.** The design offers either as an option, and the project
already has exact ``Decimal`` machinery in ``estimation.beta`` — a regularized incomplete beta
by integer-parameter summation, with quantiles by bisection. Adding a numerical dependency to
compute two textbook formulas would put floating-point arithmetic into the one calculation
whose output decides whether Revora is allowed to claim it recovered money. It would also make
the sample size depend on a library version, which is exactly the kind of thing that changes a
threshold without anyone editing a threshold.

**Why the arithmetic is exact but the formulas are approximations, and why that is fine.**
``normal_cdf`` sums a convergent series at fifty digits, so its *arithmetic* introduces nothing.
The normal approximation to a difference of two binomial proportions is itself an
approximation — it is the standard one, it is what "analysis_method" records, and its error is
a property of the statistics rather than of the code. Being exact about an approximate formula
is not pedantry: it means two runs on two machines produce the same sample size, so a threshold
cannot move underneath a running experiment.

**Rounding runs outward, always.** The sample size rounds *up* — a fractional case is a case
you do not have, and rounding down would let an underpowered experiment call itself powered.
The interval rounds *outward* — a narrower published interval than the data supports is the one
rounding error in this system that overstates knowledge rather than misplacing a paisa, and it
is the error that would let a lift interval exclude zero when the honest one does not.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Final

__all__ = [
    "PI",
    "WORKING_PRECISION",
    "SampleSizeInputs",
    "lift_interval",
    "normal_cdf",
    "normal_quantile",
    "required_sample_size_per_group",
]

WORKING_PRECISION: Final[int] = 50
"""Digits of working precision. Matches ``estimation.beta`` so the two agree.

Fifty is far more than the four places anything is reported at. The headroom matters because
the series for the normal CDF alternates — terms cancel — and cancellation is precisely where
precision is consumed."""

PI: Final[Decimal] = Decimal(
    "3.14159265358979323846264338327950288419716939937510"
)
"""Pi to fifty digits.

Hardcoded rather than computed. A series for pi would be more code and no more correct, and a
literal is auditable against any reference in a way an iteration is not."""

_ZERO: Final[Decimal] = Decimal(0)
_ONE: Final[Decimal] = Decimal(1)
_TWO: Final[Decimal] = Decimal(2)
_HALF: Final[Decimal] = Decimal("0.5")

_QUANTILE_PLACES: Final[Decimal] = Decimal("0.000001")
"""Six places on a z-value. The sample size is proportional to the square of a z, so a
coarser quantile would move the threshold by whole cases."""

_INTERVAL_PLACES: Final[Decimal] = Decimal("0.0001")
"""Four places, matching ``SIGNED_INCREMENT`` — the ``NUMERIC(7,4)`` column a lift and its
bounds are stored in."""

_MAX_ABS_Z: Final[Decimal] = Decimal(12)
"""Bisection bracket for the quantile. Beyond twelve standard deviations the normal CDF is
indistinguishable from zero or one at this precision, so a wider bracket would only cost
iterations. A caller asking for a quantile past it is asking for a significance level no sample
size could ever satisfy."""

_SERIES_TERM_LIMIT: Final[int] = 400
"""Iteration cap on the erf series.

Not a tolerance — the loop stops when a term no longer moves the sum at fifty digits, which
happens well before this for every |z| the bracket permits. The cap exists so a caller who
somehow reaches it gets a bounded wrong answer rather than an unbounded loop, and the
convergence check below is what actually terminates it."""


def _erf(z: Decimal) -> Decimal:
    """The error function, by its Maclaurin series. Must run inside a working-precision context.

    ``erf(z) = 2/sqrt(pi) * sum_{n>=0} (-1)^n z^(2n+1) / (n! (2n+1))``

    Alternating, which is the reason for fifty digits of headroom: consecutive terms are close
    in magnitude near the middle of the sum and cancel, so the working precision has to absorb
    the cancellation to leave six good digits behind.

    Terms are built by recurrence rather than by recomputing ``z**(2n+1)`` and ``n!`` each
    iteration. Not for speed — for accuracy: a factorial of four hundred is an enormous integer
    and dividing by it directly wastes precision that the recurrence never spends.
    """
    if z == _ZERO:
        return _ZERO

    z_squared = z * z
    term = z  # n = 0: z**1 / (0! * 1)
    total = term
    for n in range(1, _SERIES_TERM_LIMIT):
        # term_n = -term_{n-1} * z^2 * (2n-1) / (n * (2n+1))
        term = -term * z_squared * Decimal(2 * n - 1) / (Decimal(n) * Decimal(2 * n + 1))
        previous = total
        total += term
        if total == previous:
            # The term no longer registers at this precision. Everything after it is smaller.
            break
    return _TWO * total / PI.sqrt()


def normal_cdf(x: Decimal) -> Decimal:
    """The standard normal cumulative distribution, exactly to working precision.

    ``Phi(x) = (1 + erf(x / sqrt(2))) / 2``

    Symmetric by construction rather than by a separate branch: the series is odd in its
    argument, so ``erf(-z)`` is the exact negation of ``erf(z)`` — every term simply flips
    sign — and no reflection branch is needed for negative input.

    ``normal_cdf(-x)`` and ``1 - normal_cdf(x)`` are therefore algebraically identical, and
    measured they agree to one unit in the last place at :data:`WORKING_PRECISION` — the two
    expressions apply the same operations in a different order, so bit-identity is not
    guaranteed even though the mathematics is.

    One caveat for anyone checking that themselves: the comparison has to run inside a matching
    :data:`WORKING_PRECISION` context. Subtracting in the default 28-digit context truncates one
    side and manufactures a gap around ``1e-30``, which looks like a defect in this function and
    is not.
    """
    with localcontext() as context:
        context.prec = WORKING_PRECISION
        return (_ONE + _erf(x / _TWO.sqrt())) * _HALF


def normal_quantile(p: Decimal) -> Decimal:
    """The inverse standard normal CDF, by bisection on :func:`normal_cdf`.

    Bisection rather than a rational approximation. The published approximations (Acklam,
    Wichura) are fast and accurate to about a part in a billion, and they are also a table of
    magic coefficients nobody in this repository can verify. Bisection on a function whose own
    series is right is verifiable end to end: the only claim it makes is that the CDF is
    monotone, which it is.

    Around fifty iterations, each a couple of series evaluations. It is called once per
    experiment definition and once per analysis, so the cost is irrelevant and the
    auditability is not.

    Args:
        p: the cumulative probability, strictly inside ``(0, 1)``.

    Raises:
        ValueError: if ``p`` is not strictly inside ``(0, 1)``. Neither bound has a finite
            quantile, and returning a large sentinel would silently produce a sample size that
            looks merely demanding rather than impossible.
    """
    if not (_ZERO < p < _ONE):
        raise ValueError(f"probability must lie strictly inside (0, 1), got {p}")

    with localcontext() as context:
        context.prec = WORKING_PRECISION
        if p == _HALF:
            return _ZERO.quantize(_QUANTILE_PLACES)

        low, high = -_MAX_ABS_Z, _MAX_ABS_Z
        # Fixed iteration count rather than a convergence test on the interval width: the
        # bracket halves every step, so 200 steps take 24 down below 1e-58, past the working
        # precision. A `while high - low > tol` loop would depend on tol being reachable.
        for _ in range(200):
            middle = (low + high) * _HALF
            if normal_cdf(middle) < p:
                low = middle
            else:
                high = middle
        # Round to nearest here, not outward. This is a distributional constant, not a
        # published bound, and the outward rounding that protects an interval is applied to
        # the interval itself in `lift_interval`.
        return ((low + high) * _HALF).quantize(_QUANTILE_PLACES)


class SampleSizeInputs:
    """The four inputs to the power calculation, validated once.

    A class rather than four loose arguments so the validation lives in one place and the error
    names the offending quantity. An experiment definition that silently accepted
    ``minimum_detectable_effect = 0`` would compute an infinite sample size and, depending on
    how the division was written, either raise somewhere unhelpful or produce a number.
    """

    __slots__ = ("baseline_rate", "minimum_detectable_effect", "power", "significance_level")

    def __init__(
        self,
        *,
        baseline_rate: Decimal,
        minimum_detectable_effect: Decimal,
        significance_level: Decimal,
        power: Decimal,
    ) -> None:
        if not (_ZERO < baseline_rate < _ONE):
            raise ValueError(
                f"assumed baseline rate must lie strictly inside (0, 1), got {baseline_rate}"
            )
        if minimum_detectable_effect <= _ZERO:
            raise ValueError(
                "minimum detectable effect must be positive; a zero effect needs an "
                f"unbounded sample, got {minimum_detectable_effect}"
            )
        if baseline_rate + minimum_detectable_effect >= _ONE:
            raise ValueError(
                "baseline rate plus minimum detectable effect must stay below 1, got "
                f"{baseline_rate} + {minimum_detectable_effect}"
            )
        if not (_ZERO < significance_level < _ONE):
            raise ValueError(
                f"significance level must lie strictly inside (0, 1), got {significance_level}"
            )
        if not (_ZERO < power < _ONE):
            raise ValueError(f"power must lie strictly inside (0, 1), got {power}")

        self.baseline_rate = baseline_rate
        self.minimum_detectable_effect = minimum_detectable_effect
        self.significance_level = significance_level
        self.power = power

    @property
    def treated_rate(self) -> Decimal:
        """The rate the treatment arm is assumed to reach if the effect is real."""
        return self.baseline_rate + self.minimum_detectable_effect

    def __repr__(self) -> str:
        return (
            f"SampleSizeInputs(baseline_rate={self.baseline_rate}, "
            f"minimum_detectable_effect={self.minimum_detectable_effect}, "
            f"significance_level={self.significance_level}, power={self.power})"
        )


def required_sample_size_per_group(inputs: SampleSizeInputs) -> int:
    """Cases needed per arm to detect the stated effect at the stated power.

    The standard two-proportion formula the design specifies::

        null = z_{1-a/2} * sqrt(2 * pbar * (1 - pbar))
        alt  = z_{1-b}   * sqrt(p0*(1-p0) + p1*(1-p1))
        n    = ceil((null + alt)^2 / d^2)

    with ``p1 = p0 + d`` and ``pbar = (p0 + p1) / 2``.

    Both terms are kept. The shorter textbook form drops the second and uses only the pooled
    variance, which understates the requirement — this version is a little more demanding, and
    more demanding is the safe direction for a threshold whose job is to stop an underpowered
    experiment being reported as a finding.

    Computed at definition time and stored, which is the entire point. Computing it afterwards
    is how an underpowered experiment gets reported as a finding — the number stops being a
    threshold the data has to clear and becomes a description of the data that arrived. The
    column is ``NOT NULL`` for the same reason.

    Rounds **up**. A fractional case is a case you do not have, and rounding to nearest would
    let an experiment one case short of its own threshold pass the check in
    ``analysis.attribution_permitted``.

    Returns:
        The per-arm case count, at least one. Two-sided on the significance level and one-sided
        on the power, which is the convention the formula above assumes — worth stating because
        halving or doubling either tail is the most common way this calculation goes wrong.
    """
    with localcontext() as context:
        context.prec = WORKING_PRECISION

        p0 = inputs.baseline_rate
        p1 = inputs.treated_rate
        delta = inputs.minimum_detectable_effect
        pooled = (p0 + p1) * _HALF

        z_alpha = normal_quantile(_ONE - inputs.significance_level * _HALF)
        z_beta = normal_quantile(inputs.power)

        null_term = z_alpha * (_TWO * pooled * (_ONE - pooled)).sqrt()
        alt_term = z_beta * (p0 * (_ONE - p0) + p1 * (_ONE - p1)).sqrt()
        numerator = (null_term + alt_term) ** 2

        exact = numerator / (delta * delta)
        rounded = exact.quantize(_ONE, rounding=ROUND_CEILING)

    return max(int(rounded), 1)


def lift_interval(
    *,
    control_recoveries: int,
    control_cases: int,
    treatment_recoveries: int,
    treatment_cases: int,
    confidence_level: Decimal,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """The measured lift and its interval, or ``None`` where neither is defined.

    ``(lift, low, high)`` as ``treatment_rate - control_rate`` with a Wald interval:

    ``lift +/- z_{(1+c)/2} * sqrt( p_c(1-p_c)/n_c + p_t(1-p_t)/n_t )``

    **``None`` on an empty arm, never a lift of zero.** An arm with no cases has no rate, so
    the difference of rates does not exist. Reporting zero would be a measurement — and a
    measurement of exactly no effect is the single most consequential thing this system can
    say, because it is what ``CAUSALITY_NOT_ESTABLISHED`` is derived from. "We have no data"
    and "we measured no effect" must not produce the same row.

    **Bounds round outward**, low floored and high ceilinged. Quantization must never be able
    to narrow the published interval, because a narrowed interval can exclude zero when the
    honest one contains it — and excluding zero is precisely the condition that unlocks an
    attributed revenue claim.

    Args:
        confidence_level: two-sided, e.g. ``0.95``. Converted to a tail internally rather than
            taking a tail directly, because every caller has a confidence level to hand and
            the halving is the step people get wrong.
    """
    if control_cases <= 0 or treatment_cases <= 0:
        return None
    if not (_ZERO < confidence_level < _ONE):
        raise ValueError(
            f"confidence level must lie strictly inside (0, 1), got {confidence_level}"
        )
    if control_recoveries < 0 or treatment_recoveries < 0:
        raise ValueError("recovery counts cannot be negative")
    if control_recoveries > control_cases or treatment_recoveries > treatment_cases:
        raise ValueError("recoveries cannot exceed cases")

    with localcontext() as context:
        context.prec = WORKING_PRECISION

        n_c, n_t = Decimal(control_cases), Decimal(treatment_cases)
        p_c = Decimal(control_recoveries) / n_c
        p_t = Decimal(treatment_recoveries) / n_t

        lift = p_t - p_c
        z = normal_quantile((_ONE + confidence_level) * _HALF)
        variance = p_c * (_ONE - p_c) / n_c + p_t * (_ONE - p_t) / n_t
        half_width = z * variance.sqrt()

        low = lift - half_width
        high = lift + half_width

        return (
            lift.quantize(_INTERVAL_PLACES),
            low.quantize(_INTERVAL_PLACES, rounding=ROUND_FLOOR),
            high.quantize(_INTERVAL_PLACES, rounding=ROUND_CEILING),
        )

"""Integer money in minor currency units.

Every currency figure in Revora is an integer count of minor units — paise for
rupees — so 20,000 rupees is 2_000_000. There is no ``float`` in this module and
there must never be one, because this is the single place where a rounding error
becomes a false revenue claim.

Probabilities are the only non-integer quantities in the system. They are
multiplied into money exactly once, here, at which point rounding happens and the
integer result is stored. See Requirement 7 in requirements.md.

Rounding is half-up, applied away from zero on a tie: 2.5 becomes 3 and -2.5
becomes -3. Incremental values can legitimately be negative when an action is
estimated to make recovery less likely, so the negative case is not theoretical.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import NewType

__all__ = [
    "ZERO",
    "Minor",
    "add",
    "format_minor",
    "multiply_probability",
    "negate",
    "ratio",
    "subtract",
    "sum_exact",
]

Minor = NewType("Minor", int)
"""A signed integer count of minor currency units."""

ZERO: Minor = Minor(0)

_ONE = Decimal(1)


def add(*values: Minor) -> Minor:
    """Add minor-unit amounts. Exact — integer addition cannot drift."""
    return Minor(sum(int(v) for v in values))


def subtract(left: Minor, *others: Minor) -> Minor:
    """Subtract every ``others`` value from ``left``."""
    total = int(left)
    for other in others:
        total -= int(other)
    return Minor(total)


def negate(value: Minor) -> Minor:
    """Flip the sign."""
    return Minor(-int(value))


def multiply_probability(amount: Minor, probability: Decimal) -> Minor:
    """Multiply a minor-unit amount by a probability and round half-up to an integer.

    This is the *only* function in Revora permitted to turn a non-integer into a
    currency figure. Callers must not pre-round the probability and must not round
    the result again — rounding twice is how a total stops matching the sum of its
    rows.

    ``probability`` may be negative, which is how a negative incremental
    probability produces a negative expected incremental revenue.

    Raises:
        TypeError: if ``probability`` is not a ``Decimal``. A ``float`` here would
            silently reintroduce binary rounding error, so it is refused rather
            than coerced.
    """
    if not isinstance(probability, Decimal):
        raise TypeError(
            "probability must be a Decimal, "
            f"got {type(probability).__name__}; floats are not permitted in money arithmetic"
        )
    product = Decimal(int(amount)) * probability
    return Minor(int(product.quantize(_ONE, rounding=ROUND_HALF_UP)))


def sum_exact(values: Iterable[Minor]) -> Minor:
    """Sum stored minor-unit figures.

    Reported aggregates are computed this way so that the aggregate always equals
    the exact sum of the per-case integers it is built from. There is no
    intermediate non-integer representation to lose precision in.
    """
    return Minor(sum(int(v) for v in values))


def ratio(numerator: Minor, denominator: Minor) -> Decimal:
    """Exact decimal ratio of two minor-unit amounts.

    Used for the cost-to-value comparison. Returns a ``Decimal`` rather than a
    float so the comparison against a configured threshold is exact.

    Raises:
        ZeroDivisionError: if ``denominator`` is zero. Callers must exclude
            non-positive expected revenue *before* asking for a cost ratio — see
            Requirement 7, which forbids performing the division in that case.
    """
    if int(denominator) == 0:
        raise ZeroDivisionError(
            "cost ratio is undefined for zero expected incremental revenue; "
            "exclude the candidate before computing a ratio"
        )
    return Decimal(int(numerator)) / Decimal(int(denominator))


def format_minor(value: Minor, *, symbol: str = "₹", minor_digits: int = 2) -> str:
    """Render a minor-unit amount for display.

    The server formats money and the browser renders the string it is given —
    Requirement 14 forbids arithmetic on recovery figures in the client, and
    shipping a pre-formatted string makes violating that take deliberate effort.
    """
    if minor_digits < 0:
        raise ValueError("minor_digits must not be negative")
    divisor = 10**minor_digits
    negative = int(value) < 0
    magnitude = abs(int(value))
    major, minor = divmod(magnitude, divisor)
    sign = "-" if negative else ""
    if minor_digits == 0:
        return f"{sign}{symbol}{major:,}"
    return f"{sign}{symbol}{major:,}.{minor:0{minor_digits}d}"

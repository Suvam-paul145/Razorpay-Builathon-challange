"""Property tests for the money and probability arithmetic.

These are the cheapest tests in the suite and the most valuable, which is why they
run at 500 examples on every commit. The design's reasoning: this is the one place
where a rounding bug becomes a false revenue claim, so the code lives in a
zero-dependency module and gets the heaviest generative coverage.

The full incremental-value chain properties (P14 through P19) belong with the value
optimizer in task 16. What is here are the primitives that chain is built from.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from revora.domain import money
from revora.domain.money import Minor
from revora.domain.probability import (
    AI_CONFIDENCE_CEILING,
    CERTAIN,
    Confidence,
    Probability,
    SignedIncrement,
    increment,
)
from tests.strategies.primitives import (
    confidences,
    positive_money,
    probabilities,
    signed_increments,
)
from tests.strategies.primitives import money as money_strategy

PURE = pytest.mark.pure


# ---------------------------------------------------------------------------
# Money is exact
# ---------------------------------------------------------------------------


@PURE
@settings(max_examples=500)
@given(st.lists(money_strategy(), min_size=0, max_size=40))
def test_sum_exact_equals_python_sum(values: list[Minor]) -> None:
    """An aggregate equals the exact sum of the integers it was built from.

    This is the claim the dashboard rests on: a reported total is the sum of the
    per-case figures, with no intermediate representation to lose precision in.
    """
    assert int(money.sum_exact(values)) == sum(int(v) for v in values)


@PURE
@settings(max_examples=500)
@given(money_strategy(), money_strategy(), money_strategy())
def test_addition_is_associative_and_commutative(a: Minor, b: Minor, c: Minor) -> None:
    """Integer addition, so no reordering surprises."""
    assert money.add(a, b) == money.add(b, a)
    assert money.add(money.add(a, b), c) == money.add(a, money.add(b, c))


@PURE
@settings(max_examples=500)
@given(money_strategy(), money_strategy())
def test_subtract_is_add_of_negation(a: Minor, b: Minor) -> None:
    assert money.subtract(a, b) == money.add(a, money.negate(b))


# ---------------------------------------------------------------------------
# multiply_probability: rounded once, half-up, never a float
# ---------------------------------------------------------------------------


@PURE
@settings(max_examples=500)
@given(positive_money(), probabilities())
def test_multiply_probability_matches_decimal_half_up(
    amount: Minor, probability: Probability
) -> None:
    """The result is the half-up rounding of the exact decimal product.

    Stated against an independently computed expectation rather than against the
    implementation, so a change to how rounding is applied fails here.
    """
    expected = (Decimal(int(amount)) * probability.value).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    assert int(money.multiply_probability(amount, probability.value)) == int(expected)


@PURE
@settings(max_examples=500)
@given(positive_money())
def test_multiply_by_zero_and_one(amount: Minor) -> None:
    """The two boundary probabilities behave exactly.

    Probability zero is the DO_NOTHING case and must produce exactly zero, not a
    rounding artefact near it.
    """
    assert int(money.multiply_probability(amount, Decimal("0.0000"))) == 0
    assert int(money.multiply_probability(amount, Decimal("1.0000"))) == int(amount)


@PURE
@settings(max_examples=500)
@given(positive_money(), signed_increments())
def test_negative_increment_yields_non_positive_revenue(
    amount: Minor, delta: SignedIncrement
) -> None:
    """A negative incremental probability produces non-positive expected revenue.

    Negatives are retained rather than clipped, because an action estimated to make
    recovery less likely has to be excluded for a stated reason — not quietly
    flattened to zero and then treated as merely worthless.
    """
    assume(delta.value < 0)
    assert int(money.multiply_probability(amount, delta.value)) <= 0


@PURE
def test_half_up_rounds_away_from_zero_on_a_tie() -> None:
    """A .5 tie rounds away from zero in both directions.

    Pinned as an example rather than a property because it is the specific
    convention the design names, and a future change to ROUND_HALF_EVEN would still
    satisfy every generative test above.
    """
    # 5 paise at 50% is 2.5, which rounds to 3.
    assert int(money.multiply_probability(Minor(5), Decimal("0.5000"))) == 3
    # And -2.5 rounds to -3, not -2.
    assert int(money.multiply_probability(Minor(-5), Decimal("0.5000"))) == -3


@PURE
@settings(max_examples=200)
@given(positive_money(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_multiply_probability_refuses_a_float(amount: Minor, raw: object) -> None:
    """A float argument is refused rather than coerced.

    Coercing would work, produce plausible numbers, and silently reintroduce binary
    rounding error into a revenue figure. Refusing turns that into a stack trace at
    the call site that made the mistake.
    """
    with pytest.raises(TypeError):
        money.multiply_probability(amount, raw)  # type: ignore[arg-type]


@PURE
@settings(max_examples=200)
@given(money_strategy())
def test_ratio_refuses_a_zero_denominator(numerator: Minor) -> None:
    """The cost ratio is undefined at zero expected revenue, and says so.

    Requirement 7 forbids performing the division when expected incremental revenue
    is non-positive. Raising here means a caller that forgets the exclusion gets an
    error instead of a misleading ratio.
    """
    with pytest.raises(ZeroDivisionError):
        money.ratio(numerator, Minor(0))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@PURE
def test_format_minor_renders_major_and_minor_units() -> None:
    """Server-side formatting, since the browser is forbidden from doing arithmetic."""
    assert money.format_minor(Minor(2_000_000)) == "₹20,000.00"
    assert money.format_minor(Minor(45_050)) == "₹450.50"
    assert money.format_minor(Minor(1)) == "₹0.01"
    assert money.format_minor(Minor(-2_500)) == "-₹25.00"
    assert money.format_minor(Minor(0)) == "₹0.00"


# ---------------------------------------------------------------------------
# Probability types enforce their ranges at construction
# ---------------------------------------------------------------------------


@PURE
@settings(max_examples=500)
@given(probabilities())
def test_probability_stays_in_range_and_at_four_places(value: Probability) -> None:
    assert Decimal("0.0000") <= value.value <= Decimal("1.0000")
    assert -value.value.as_tuple().exponent <= 4


@PURE
@given(st.sampled_from([Decimal("-0.0001"), Decimal("1.0001"), Decimal("2")]))
def test_probability_refuses_out_of_range(raw: Decimal) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        Probability(raw)


@PURE
@given(st.sampled_from([Decimal("-1.0001"), Decimal("1.0001")]))
def test_signed_increment_refuses_out_of_range(raw: Decimal) -> None:
    with pytest.raises(ValueError, match="between -1 and 1"):
        SignedIncrement(raw)


@PURE
@settings(max_examples=500)
@given(probabilities(), probabilities())
def test_increment_is_the_difference(
    intervention: Probability, baseline: Probability
) -> None:
    """``increment`` is exactly intervention minus baseline, sign preserved."""
    delta = increment(intervention, baseline)
    assert delta.value == intervention.value - baseline.value
    assert delta.is_positive == (intervention.value > baseline.value)


@PURE
@settings(max_examples=500)
@given(confidences())
def test_confidence_stays_in_range_and_at_three_places(value: Confidence) -> None:
    assert Decimal("0.000") <= value.value <= Decimal("1.000")
    assert -value.value.as_tuple().exponent <= 3


@PURE
def test_certainty_is_reserved_above_the_ai_ceiling() -> None:
    """Only a deterministic mapping match may claim 1.000.

    The gap between the AI ceiling and certainty is what makes "this came from the
    provider's own error field" distinguishable from "a model guessed well".
    """
    assert AI_CONFIDENCE_CEILING.value < CERTAIN.value
    assert CERTAIN.value == Decimal("1.000")

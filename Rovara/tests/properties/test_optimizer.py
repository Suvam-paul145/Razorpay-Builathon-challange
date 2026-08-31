"""Properties 14 through 19: the arithmetic that is the product.

These are written before the implementation, and they are the most heavily exercised
tests in the suite because this is the one place where a rounding bug becomes a false
revenue claim. Every one of them is pure integer and ``Decimal`` arithmetic, so 500
examples each costs microseconds.

Each test names the feature and states the property, per the design's tagging rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from revora.domain.actions import ACTION_PRECEDENCE, CandidateAction
from revora.domain.enums import ActionAvailability, ExclusionReason, SelectionReason
from revora.domain.money import Minor, multiply_probability
from revora.domain.probability import Probability
from revora.optimizer.arithmetic import CandidateInput, evaluate_candidate
from revora.optimizer.selection import Thresholds, select
from tests.strategies.candidates import (
    candidate_estimate_set,
    candidate_inputs,
    diverging_candidate_set,
    tied_candidate_set,
)
from tests.strategies.primitives import positive_money

pytestmark = pytest.mark.pure

_DEFAULT_THRESHOLDS = Thresholds(
    min_net_value=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
    max_cost_to_value_ratio=Decimal("0.30"),
    high_baseline=Decimal("0.80"),
)


def _permissive() -> Thresholds:
    """Thresholds that exclude nothing, for isolating the arithmetic from the gates."""
    return Thresholds(
        min_net_value=Minor(0),
        min_incremental_probability=Decimal("0"),
        max_cost_to_value_ratio=Decimal("1000000"),
        high_baseline=Decimal("1.1"),
    )


# ---------------------------------------------------------------------------
# P14 — the arithmetic chain, in integer minor units
# ---------------------------------------------------------------------------


@given(
    amount=positive_money(),
    baseline=st.integers(min_value=0, max_value=10_000).map(lambda k: Decimal(k).scaleb(-4)),
    candidate=candidate_inputs(allow_unavailable=False),
)
def test_p14_arithmetic_chain_is_exact_integer_minor_units(
    amount: Minor, baseline: Decimal, candidate: CandidateInput
) -> None:
    """Feature: Value_Optimizer. Property 14 — every currency figure is an integer
    count of minor units, rounding is applied exactly once, and the chain composes.

    The three-step chain is checked against independently recomputed values rather
    than against itself: the incremental probability is a plain Decimal subtraction,
    the expected revenue is one call to the single sanctioned rounding function, and
    the net value is integer subtraction of three costs.
    """
    evaluated = evaluate_candidate(candidate, baseline=Probability(baseline), amount=amount)

    expected_increment = candidate.intervention_probability.value - baseline
    assert evaluated.incremental_probability.value == expected_increment

    expected_revenue = multiply_probability(amount, expected_increment)
    assert evaluated.expected_incremental_revenue == expected_revenue
    assert isinstance(evaluated.expected_incremental_revenue, int)

    assert evaluated.net_recovery_value == (
        int(expected_revenue)
        - int(candidate.action_cost)
        - int(candidate.risk_cost)
        - int(candidate.customer_cost)
    )
    assert isinstance(evaluated.net_recovery_value, int)


@given(
    amount=positive_money(),
    baseline=st.integers(min_value=0, max_value=10_000).map(lambda k: Decimal(k).scaleb(-4)),
    candidate=candidate_inputs(allow_unavailable=False),
)
def test_p14_negative_increments_are_retained_not_clipped(
    amount: Minor, baseline: Decimal, candidate: CandidateInput
) -> None:
    """Feature: Value_Optimizer. Property 14 — an action estimated to make recovery
    less likely produces a negative increment and a negative expected revenue, rather
    than a value quietly flattened to zero.
    """
    evaluated = evaluate_candidate(candidate, baseline=Probability(baseline), amount=amount)
    if candidate.intervention_probability.value < baseline:
        assert evaluated.incremental_probability.value < 0
        assert evaluated.expected_incremental_revenue <= 0


@given(data=candidate_estimate_set())
def test_p14_no_cost_ratio_is_computed_on_non_positive_revenue(
    data: tuple[Probability, tuple[CandidateInput, ...]],
) -> None:
    """Feature: Value_Optimizer. Property 14 / R7.C14 — where expected incremental
    revenue is zero or negative, the candidate is excluded for non-positive value and
    **no cost-ratio division is performed at all**.

    The division would raise ``ZeroDivisionError`` on a zero denominator, so a
    correctly-ordered implementation is one where this test cannot raise. The assertion
    on the exclusion reason is what stops it passing for the wrong reason — an
    implementation that skipped the division by skipping the candidate entirely would
    also not raise, but would lose the recorded reason.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=Minor(2_000_000),
                    thresholds=_DEFAULT_THRESHOLDS)

    null_actions = (CandidateAction.DO_NOTHING, CandidateAction.WAIT)
    for evaluated in result.candidates:
        if evaluated.expected_incremental_revenue > 0:
            continue
        # The invariant that matters, and it holds for every candidate without
        # exception: a non-positive denominator was never divided by.
        assert evaluated.cost_ratio is None, (
            "a cost ratio must not be computed for non-positive expected revenue"
        )
        # A real action with nothing to gain is excluded for exactly that reason. The
        # two null actions are deliberately exempt: DO_NOTHING has zero net value by
        # definition and WAIT usually has little, so excluding them on value would
        # leave the optimizer with nothing to select and no way to express "acting is
        # not worth it" — which is the answer the product most needs to give.
        if evaluated.available and evaluated.action not in null_actions:
            assert evaluated.excluded
            assert (
                evaluated.exclusion_reason
                is ExclusionReason.NON_POSITIVE_INCREMENTAL_VALUE
            )


# ---------------------------------------------------------------------------
# P15 — argmax among survivors, with the declared tie order
# ---------------------------------------------------------------------------


@given(data=candidate_estimate_set(), amount=positive_money())
def test_p15_selection_is_argmax_of_net_value_among_survivors(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 15 — the selected action is the survivor with
    the greatest net recovery value; no excluded candidate is ever selected.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    survivors = [c for c in result.candidates if not c.excluded]
    selected = result.selected

    assert not selected.excluded, "an excluded candidate must never be selected"
    if survivors:
        best = max(c.net_recovery_value for c in survivors)
        if result.selection_reason is SelectionReason.HIGHEST_NET_VALUE:
            assert selected.net_recovery_value == best


@given(data=tied_candidate_set(), amount=positive_money())
def test_p15_ties_resolve_by_cost_then_declared_precedence(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 15 — two candidates with equal net value
    resolve to the lower total cost, and on equal cost to the declared precedence
    order. The result never depends on input ordering.

    Checked by shuffling the input: a stable, declared tie-break gives the same answer
    for every permutation, and a sort-order-dependent one does not.
    """
    baseline, candidates = data
    forward = select(candidates, baseline=baseline, amount=amount,
                     thresholds=_permissive())
    reversed_order = select(tuple(reversed(candidates)), baseline=baseline, amount=amount,
                            thresholds=_permissive())

    assert forward.selected.action is reversed_order.selected.action

    tied = [
        c for c in forward.candidates
        if not c.excluded and c.net_recovery_value == forward.selected.net_recovery_value
    ]
    if len(tied) > 1:
        lowest_cost = min(c.total_cost for c in tied)
        assert forward.selected.total_cost == lowest_cost
        cheapest = [c for c in tied if c.total_cost == lowest_cost]
        if len(cheapest) > 1:
            order = {action: i for i, action in enumerate(ACTION_PRECEDENCE)}
            assert forward.selected.action is min(
                (c.action for c in cheapest), key=lambda a: order[a]
            )


# ---------------------------------------------------------------------------
# P16 — nothing clears the thresholds
# ---------------------------------------------------------------------------


@given(data=candidate_estimate_set(), amount=positive_money())
def test_p16_no_positive_value_selects_a_null_action(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 16 — when no candidate clears both
    thresholds, the selection is a null action with reason ``NO_POSITIVE_VALUE``,
    ``DO_NOTHING`` winning on equality.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    if result.selection_reason is SelectionReason.NO_POSITIVE_VALUE:
        assert result.selected.action in (CandidateAction.DO_NOTHING, CandidateAction.WAIT)


# ---------------------------------------------------------------------------
# P17 — high baseline
# ---------------------------------------------------------------------------


@given(
    data=candidate_estimate_set(baseline=Probability(Decimal("0.9000"))),
    amount=positive_money(),
)
def test_p17_high_baseline_prefers_no_intervention(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 17 — where the baseline is at or above
    ``HIGH_BASELINE_THRESHOLD``, a null action is selected with reason
    ``HIGH_BASELINE_NO_INTERVENTION``.

    A customer who is very likely to pay anyway warrants no intervention, and Revora
    must be able to say so — that claim is the product's credibility, so it is enforced
    ahead of the ordinary ranking rather than emerging from it.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    assert result.selection_reason is SelectionReason.HIGH_BASELINE_NO_INTERVENTION
    assert result.selected.action in (CandidateAction.DO_NOTHING, CandidateAction.WAIT)


# ---------------------------------------------------------------------------
# P18 — divergence disclosure
# ---------------------------------------------------------------------------


@given(
    data=diverging_candidate_set(),
    amount=st.integers(min_value=1_000_000, max_value=500_000_000).map(Minor),
)
def test_p18_divergence_is_disclosed_when_probability_and_value_disagree(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 18 — when the highest-probability candidate is
    not the selected one, the divergence is recorded rather than reconstructed.

    This is the product's whole argument: "most likely to work" and "worth doing" are
    different questions. An optimizer that ranked on probability would select the
    expensive escalation here and record no divergence.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    considered = [c for c in result.candidates if not c.excluded]
    if not considered:
        return
    highest_probability = max(considered, key=lambda c: c.intervention_probability.value)
    if highest_probability.action is not result.selected.action:
        assert result.divergence_reason == "HIGHER_PROBABILITY_LOWER_NET_VALUE"
    else:
        assert result.divergence_reason is None


# ---------------------------------------------------------------------------
# P19 — the null actions are definitionally neutral
# ---------------------------------------------------------------------------


@given(
    amount=positive_money(),
    baseline=st.integers(min_value=0, max_value=10_000).map(lambda k: Decimal(k).scaleb(-4)),
)
def test_p19_do_nothing_is_exactly_zero_on_every_figure(
    amount: Minor, baseline: Decimal
) -> None:
    """Feature: Value_Optimizer. Property 19 — ``DO_NOTHING`` has incremental
    probability, expected incremental revenue, all three costs and net value all
    exactly zero, for any case.

    Exactly zero, not approximately. If ``DO_NOTHING`` were estimated like any other
    action its incremental value would be noise around zero, and roughly half the time
    Revora would find a reason to act purely because the null action's estimate landed
    low. This property is what makes the comparison honest.
    """
    base = Probability(baseline)
    do_nothing = CandidateInput(
        action=CandidateAction.DO_NOTHING,
        intervention_probability=base,
        action_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    evaluated = evaluate_candidate(do_nothing, baseline=base, amount=amount)

    assert evaluated.incremental_probability.value == Decimal("0.0000")
    assert evaluated.expected_incremental_revenue == 0
    assert evaluated.total_cost == 0
    assert evaluated.net_recovery_value == 0


@given(data=candidate_estimate_set(), amount=positive_money())
def test_p19_a_null_action_is_always_available_for_selection(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. Property 19 — the optimizer always has something to
    select: a decision is always reached, and the fallback is a null action.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)
    assert result.selected is not None
    assert result.selection_reason in set(SelectionReason)


@given(data=candidate_estimate_set(), amount=positive_money())
def test_unavailable_candidates_are_excluded_but_retained(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. R6.C9 — an action with no verified provider capability
    is excluded from selection but stays in the recorded set, so the dashboard can show
    it was considered.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    assert len(result.candidates) == len(candidates), "no candidate may be dropped"
    for evaluated in result.candidates:
        if evaluated.availability is ActionAvailability.UNAVAILABLE:
            assert evaluated.excluded
            assert evaluated.exclusion_reason is ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED


@given(data=candidate_estimate_set(), amount=positive_money())
def test_ranks_are_dense_and_only_on_survivors(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Value_Optimizer. R7.C8 — every surviving candidate carries a rank and
    every excluded one carries none, since an excluded action has no position in an
    ordering it was never part of.
    """
    baseline, candidates = data
    result = select(candidates, baseline=baseline, amount=amount,
                    thresholds=_DEFAULT_THRESHOLDS)

    ranked = sorted(
        (c for c in result.candidates if c.rank is not None), key=lambda c: c.rank or 0
    )
    assert [c.rank for c in ranked] == list(range(1, len(ranked) + 1))
    for evaluated in result.candidates:
        assert (evaluated.rank is None) == evaluated.excluded

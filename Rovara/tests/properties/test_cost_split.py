"""Splitting the blended action cost is a presentation change, not a decision change.

Feature: Four-Term Cost Decomposition (task 35.3/35.4, R31.C1, R31.C2, R31.C3, R31.C4,
R31.C6, R31.C8, R31.C12).

Property: P67 — for any Candidate_Action set in which each Candidate_Action's
``financial_cost`` plus ``communication_cost`` equals its pre-split ``action_cost`` and
its ``risk_cost`` and ``customer_cost`` are unchanged, the selected Candidate_Action, the
recorded exclusion reason of every excluded Candidate_Action, and the
``net_recovery_value`` of every Candidate_Action are identical to those the pre-split
computation produced.

Property: P68 — for every ``financial_cost``, ``communication_cost``, ``risk_cost``,
``customer_cost``, ``total_action_cost`` and ``net_recovery_value`` produced, the value is
an integer count of minor currency units, no fractional or binary currency value occurs at
any step, rounding occurs exactly once per estimate at the single multiplication of a
probability into money, and every one of the four cost terms of ``DO_NOTHING`` is zero
with a ``net_recovery_value`` of exactly zero.

**Why this file is a differential test and what the oracle is.** R31.C8 does not say the
four-term arithmetic is self-consistent; it says it agrees with the *three-term*
arithmetic that came before it. A test that recomputed the four-term formula and compared
it against :func:`select` would assert that a function equals itself, and it would pass on
the day somebody made both copies wrong in the same way.

The oracle is therefore the pre-split computation — and the useful observation is that no
second implementation is needed to obtain it. The four-term chain reduces to the
three-term chain exactly when ``communication_cost`` is zero, because the only thing the
optimizer ever does with the split is add it up. So the partition point ``(total, 0)`` is
the old formula, run by the new code, and :func:`cost_partitions` puts it first for that
reason. P67 then asserts invariance of the whole decision across every other partition of
the same total, which is R31.C8's quantifier ("for any input whose ``financial_cost`` plus
``communication_cost`` equals the pre-split ``action_cost``") stated executably rather
than as an intention.

A locally-maintained three-term reference implementation was the alternative, and it was
rejected: it would be a second copy of the arithmetic that has to be kept in step with
the first, and when it drifted the test would go green while the claim it exists to
defend was broken. An oracle that is an element of the generated family cannot drift.

``pure`` tier. Every test here is integer and ``Decimal`` arithmetic over frozen
dataclasses — no database, no I/O, and no fixtures beyond the strategies. The one thing
that would ordinarily want a fixture is the call counter P68 needs, and
:func:`_recorded_rounding` is a plain context manager for exactly that reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from revora.domain.actions import ACTION_PRECEDENCE, CandidateAction
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.optimizer import arithmetic
from revora.optimizer.arithmetic import CandidateInput, evaluate_candidate
from revora.optimizer.selection import SelectionResult, Thresholds, select
from tests.strategies.candidates import (
    candidate_estimate_set,
    cost_partitions,
    diverging_candidate_set,
    tied_candidate_set,
)
from tests.strategies.primitives import positive_money

pytestmark = pytest.mark.pure

_THRESHOLDS = Thresholds(
    min_net_value=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
    max_cost_to_value_ratio=Decimal("0.30"),
    high_baseline=Decimal("0.80"),
)
"""The seeded defaults, deliberately *not* a permissive set.

P67 is a claim about exclusion reasons and ranks as much as about net values, and
thresholds that exclude nothing would leave ``exclusion_reason`` uniformly ``None`` and
every rank assigned — so the two most breakable halves of the property would be compared
as constants. These bounds put ``COST_RATIO_EXCEEDED`` and
``BELOW_NET_VALUE_THRESHOLD`` in reach, and the cost-ratio rule is the one rule in the
system whose numerator is the four-term sum (R31.C4)."""


# ---------------------------------------------------------------------------
# P67 — the split changes presentation, not selection
# ---------------------------------------------------------------------------


def _decision(result: SelectionResult) -> tuple[object, ...]:
    """Everything about a decision that R31.C8 requires the split to leave alone.

    Deliberately not ``result`` itself: ``financial_cost`` and ``communication_cost``
    *do* differ between two partitions of the same total, and they are supposed to. That
    is the presentation change. Comparing whole ``SelectionResult`` objects would fail on
    the one difference the requirement asks for, so the projection names the fields the
    requirement names — and names them explicitly rather than by exclusion, so a field
    added to the result later does not silently join the comparison without anybody
    deciding it should.

    ``total_cost``, ``expected_incremental_revenue`` and ``cost_ratio`` are included
    beyond the requirement's own list because they are the *mechanism* of the claim: the
    total is where the split is consumed, and the ratio is the only figure derived from
    it by division. If the property ever fails, having them in the fingerprint is the
    difference between "the decision moved" and knowing which term moved it.
    """
    return (
        result.selected.action,
        result.selection_reason,
        result.divergence_reason,
        result.qualifying_actions,
        tuple(
            (
                candidate.action,
                int(candidate.net_recovery_value),
                int(candidate.total_cost),
                int(candidate.expected_incremental_revenue),
                candidate.excluded,
                candidate.exclusion_reason,
                candidate.rank,
                candidate.cost_ratio,
                candidate.availability,
            )
            for candidate in result.candidates
        ),
    )


_BASE_SETS = st.one_of(
    candidate_estimate_set(),
    tied_candidate_set(),
    diverging_candidate_set(),
)
"""All three candidate-set shapes, and the tied one is not optional.

This started as ``candidate_estimate_set`` alone and that made P67 substantially weaker
than it reads, which a mutation caught: rewriting ``_ranking_key`` to sort on
``financial_cost`` instead of ``total_cost`` — precisely the "somebody read one of the two
new terms on its own" failure this property exists to detect — left the test green.

The reason is that cost enters the ranking key only as a **tie-break** behind net recovery
value, and net value is identical across partitions by construction. So the cost component
decides nothing unless two candidates are already equal on net value, and a set drawn from
uniform-ish costs and probabilities essentially never produces that. The property was
asserting rank equality over a set of ranks that only net value had determined.

:func:`tied_candidate_set` is built to produce exactly that equality, and it also splits
its shared total across *different* cost columns for the two tied members — so
repartitioning one of them moves the individual terms while leaving both totals equal, and
a ranking key reading either split term alone reorders. With it in the pool the mutation
dies. :func:`diverging_candidate_set` is included for the other half of the ranking rule,
where probability and net value disagree."""


@st.composite
def _repartitionings(
    draw: st.DrawFn,
) -> tuple[Probability, tuple[tuple[CandidateInput, ...], ...]]:
    """A baseline and the same candidate set re-expressed at every partition point.

    Every generator in :data:`_BASE_SETS` produces candidates with
    ``communication_cost`` fixed at zero, so each candidate's ``financial_cost`` *is* its
    pre-split blended ``action_cost``. Each is then repartitioned independently, and
    variant 0 is the original set — the pre-split form, the oracle.

    The number of variants is the largest partition family in the set, and a candidate
    with a shorter family cycles through its own points. That way every partition point
    of every candidate is exercised at least once, and the variants also mix partition
    positions across candidates rather than moving them in lockstep — a bug that only
    showed up when two candidates were split differently would survive a lockstep sweep.
    """
    baseline, candidates = draw(_BASE_SETS)

    for candidate in candidates:
        assert int(candidate.communication_cost) == 0, (
            "the P67 oracle depends on candidate_estimate_set carrying the whole blended "
            "action_cost in financial_cost; a non-zero communication_cost here means the "
            "generated set is no longer the pre-split form and the differential is void"
        )

    families = tuple(
        draw(cost_partitions(total=int(candidate.financial_cost)))
        for candidate in candidates
    )
    variant_count = max(len(family) for family in families)

    variants = tuple(
        tuple(
            replace(
                candidate,
                financial_cost=families[index][position % len(families[index])][0],
                communication_cost=families[index][position % len(families[index])][1],
            )
            for index, candidate in enumerate(candidates)
        )
        for position in range(variant_count)
    )
    return baseline, variants


@given(data=_repartitionings(), amount=positive_money())
def test_p67_repartitioning_the_blended_cost_moves_no_decision(
    data: tuple[Probability, tuple[tuple[CandidateInput, ...], ...]],
    amount: Minor,
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 67 — for any Candidate_Action set
    in which each Candidate_Action's ``financial_cost`` plus ``communication_cost``
    equals its pre-split ``action_cost`` and its ``risk_cost`` and ``customer_cost`` are
    unchanged, the selected Candidate_Action, the recorded exclusion reason of every
    excluded Candidate_Action, and the ``net_recovery_value`` of every Candidate_Action
    are identical to those the pre-split computation produced.

    Variant 0 is the pre-split arithmetic, so ``expected`` is the oracle rather than a
    restatement of the code under test. Every other variant holds the same four-term
    totals split at a different point.
    """
    baseline, variants = data
    oracle = variants[0]
    assert all(int(candidate.communication_cost) == 0 for candidate in oracle)

    expected = _decision(
        select(oracle, baseline=baseline, amount=amount, thresholds=_THRESHOLDS)
    )

    for variant in variants[1:]:
        assert len(variant) == len(oracle)
        for split, blended in zip(variant, oracle, strict=True):
            assert split.action is blended.action
            assert int(split.financial_cost) + int(split.communication_cost) == int(
                blended.financial_cost
            )
            assert int(split.risk_cost) == int(blended.risk_cost)
            assert int(split.customer_cost) == int(blended.customer_cost)

        actual = _decision(
            select(variant, baseline=baseline, amount=amount, thresholds=_THRESHOLDS)
        )
        assert actual == expected


@given(data=_repartitionings(), amount=positive_money())
def test_p67_the_repartitioning_actually_varies_the_two_split_terms(
    data: tuple[Probability, tuple[tuple[CandidateInput, ...], ...]],
    amount: Minor,
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 67, anti-vacuity — wherever a
    candidate carries a positive blended cost, the generated family really does move the
    ``financial_cost``/``communication_cost`` boundary, so the invariance asserted above
    is invariance over more than one input.

    Without this, a generator that quietly produced one variant per example would leave
    P67 passing while testing nothing, and the failure would be invisible: a property
    that iterates over an empty tail is a property that always holds. The candidate set
    always contains the two zero-cost null actions, so the guard is conditional on some
    candidate having something to split.
    """
    _, variants = data
    splittable = any(int(candidate.financial_cost) > 0 for candidate in variants[0])
    if not splittable:
        return

    observed = {
        tuple(
            (int(candidate.financial_cost), int(candidate.communication_cost))
            for candidate in variant
        )
        for variant in variants
    }
    assert len(observed) > 1
    assert any(
        int(candidate.communication_cost) > 0
        for variant in variants
        for candidate in variant
    )


# ---------------------------------------------------------------------------
# P68 — integer money discipline across four terms
# ---------------------------------------------------------------------------


@contextmanager
def _recorded_rounding() -> Iterator[list[tuple[int, Decimal]]]:
    """Record every call to the one function permitted to round money.

    ``domain.money.multiply_probability`` is the single rounding site in the system, and
    :func:`evaluate_candidate` resolves it as a module global of
    ``revora.optimizer.arithmetic`` at call time — so rebinding that name here observes
    the real call without changing what it computes. The recorded call is delegated to
    the original, so this is a probe rather than a stub and the arithmetic under test is
    the production arithmetic.

    A context manager rather than pytest's ``monkeypatch`` because the ``pure`` tier
    takes no fixtures, and restoring in ``finally`` means a failing assertion inside the
    block cannot leave the counter installed for the rest of the session.
    """
    calls: list[tuple[int, Decimal]] = []
    original = arithmetic.multiply_probability

    def recording(amount: Minor, probability: Decimal) -> Minor:
        calls.append((int(amount), probability))
        return original(amount, probability)

    arithmetic.multiply_probability = recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        arithmetic.multiply_probability = original  # type: ignore[assignment]


@given(data=candidate_estimate_set(), amount=positive_money())
def test_p68_every_money_figure_is_an_integer_count_of_minor_units(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 68 — every one of the four cost
    terms, the total action cost, the expected incremental revenue and the net recovery
    value is an integer count of minor currency units, and no fractional or binary
    currency value occurs at any step.

    ``type(value) is int`` rather than ``isinstance``, because ``bool`` is a subclass of
    ``int`` and a figure that arrived as ``True`` would satisfy an ``isinstance`` check
    while being nonsense as money. ``Minor`` is a ``NewType`` over ``int``, so the exact
    type is what the system really stores.

    The probability side is asserted too, as ``Decimal``. It is the other half of the
    same claim: money stays exact only because the one quantity that is not an integer
    is never a binary approximation of one.
    """
    baseline, candidates = data
    result = select(
        candidates, baseline=baseline, amount=amount, thresholds=_THRESHOLDS
    )

    for candidate in result.candidates:
        for figure in (
            candidate.financial_cost,
            candidate.communication_cost,
            candidate.risk_cost,
            candidate.customer_cost,
            candidate.total_cost,
            candidate.expected_incremental_revenue,
            candidate.net_recovery_value,
        ):
            assert type(figure) is int, (candidate.action, figure, type(figure))

        assert int(candidate.total_cost) == (
            int(candidate.financial_cost)
            + int(candidate.communication_cost)
            + int(candidate.risk_cost)
            + int(candidate.customer_cost)
        )
        assert int(candidate.net_recovery_value) == (
            int(candidate.expected_incremental_revenue) - int(candidate.total_cost)
        )

        assert type(candidate.incremental_probability.value) is Decimal
        assert type(candidate.intervention_probability.value) is Decimal
        if candidate.cost_ratio is not None:
            assert type(candidate.cost_ratio) is Decimal


@given(data=candidate_estimate_set(), amount=positive_money())
def test_p68_rounding_occurs_exactly_once_per_estimate(
    data: tuple[Probability, tuple[CandidateInput, ...]], amount: Minor
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 68 — rounding occurs exactly once
    per estimate, at the single multiplication of a probability into money, and no cost
    term is passed through the rounding site.

    Counted rather than reasoned about. The assertion is threefold and each part closes a
    hole the other two leave open:

    1. **One call per estimate, exactly.** The count equals the number of candidates, so
       neither a second rounding of the product nor a pre-rounding of the increment can
       hide inside a chain that still returns the right answer for the generated input.
    2. **Every multiplicand is the payment amount.** That is what "no cost term is
       multiplied by anything" means operationally — a fourth cost term could only
       introduce a new rounding site by being passed through here, and none is.
    3. **The multiplier of call ``i`` is candidate ``i``'s own increment.** Without this,
       a chain that made the right number of calls with the wrong arguments would pass.
       ``select`` evaluates in input order, so the pairing is positional.

    One thing this does *not* prove, stated plainly rather than left implied: it shows no
    cost term reaches the sanctioned rounding function, not that no rounding happens
    anywhere by some other means. A hand-rolled ``quantize`` on a cost would be invisible
    here. That case is covered lexically instead, by ``scripts/check_no_float.py`` over
    ``revora/optimizer`` and by the integer-type assertions above — a cost that had been
    through a rounding step and come back as an integer is a cost that never left the
    integers.
    """
    baseline, candidates = data

    with _recorded_rounding() as calls:
        result = select(
            candidates, baseline=baseline, amount=amount, thresholds=_THRESHOLDS
        )

    assert len(calls) == len(candidates)
    assert {multiplicand for multiplicand, _ in calls} == {int(amount)}

    for (multiplicand, multiplier), candidate in zip(calls, candidates, strict=True):
        assert multiplicand == int(amount)
        assert type(multiplier) is Decimal
        assert multiplier == (
            candidate.intervention_probability.value - baseline.value
        )

    cost_terms = {
        int(term)
        for candidate in candidates
        for term in (
            candidate.financial_cost,
            candidate.communication_cost,
            candidate.risk_cost,
            candidate.customer_cost,
        )
    }
    assert cost_terms - {int(amount)} == cost_terms - {
        multiplicand for multiplicand, _ in calls
    }
    assert len(result.candidates) == len(candidates)


@given(amount=positive_money())
def test_p68_a_single_estimate_rounds_exactly_once(amount: Minor) -> None:
    """Feature: Four-Term Cost Decomposition. Property 68 — one estimate is one rounding.

    The set-level count above divided by the set size, asserted directly, so the clause
    "exactly once **per estimate**" is checked on a single estimate rather than only in
    aggregate. Four non-zero, mutually distinct cost terms, so a stray call that happened
    to pass one of them through would be visible in the recorded multiplicands.
    """
    candidate = CandidateInput(
        action=CandidateAction.PAYMENT_LINK,
        intervention_probability=Probability(Decimal("0.4000")),
        financial_cost=Minor(300),
        communication_cost=Minor(25),
        risk_cost=Minor(7),
        customer_cost=Minor(1_000),
    )
    baseline = Probability(Decimal("0.1000"))

    with _recorded_rounding() as calls:
        evaluated = evaluate_candidate(candidate, baseline=baseline, amount=amount)

    assert len(calls) == 1
    assert calls[0] == (int(amount), Decimal("0.3000"))
    assert int(evaluated.total_cost) == 1_332
    assert int(evaluated.net_recovery_value) == (
        int(evaluated.expected_incremental_revenue) - 1_332
    )


@given(
    amount=positive_money(),
    baseline=st.integers(min_value=0, max_value=10_000).map(
        lambda scaled: Decimal(scaled).scaleb(-4)
    ),
)
def test_p68_do_nothing_is_four_zero_terms_and_exactly_zero_net(
    amount: Minor, baseline: Decimal
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 68 / R31.C6 — every one of the
    four cost terms of ``DO_NOTHING`` is zero and its ``net_recovery_value`` is exactly
    zero, for any payment amount and any baseline.

    Exactly zero, not approximately. If the null action's net value were noise around
    zero, roughly half of all cases would find a reason to act purely because the
    comparator's own estimate landed low, and the product's central claim would be an
    artefact of rounding.
    """
    base = Probability(baseline)
    do_nothing = CandidateInput(
        action=CandidateAction.DO_NOTHING,
        intervention_probability=base,
        financial_cost=Minor(0),
        communication_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )

    evaluated = evaluate_candidate(do_nothing, baseline=base, amount=amount)

    assert int(evaluated.financial_cost) == 0
    assert int(evaluated.communication_cost) == 0
    assert int(evaluated.risk_cost) == 0
    assert int(evaluated.customer_cost) == 0
    assert int(evaluated.total_cost) == 0
    assert evaluated.incremental_probability.value == Decimal("0.0000")
    assert int(evaluated.expected_incremental_revenue) == 0
    assert int(evaluated.net_recovery_value) == 0


@given(
    action=st.sampled_from(ACTION_PRECEDENCE),
    amount=positive_money(),
    baseline=st.integers(min_value=0, max_value=10_000).map(
        lambda scaled: Decimal(scaled).scaleb(-4)
    ),
)
def test_p68_the_zero_is_definitional_not_a_special_case_for_do_nothing(
    action: CandidateAction, amount: Minor, baseline: Decimal
) -> None:
    """Feature: Four-Term Cost Decomposition. Property 68 / R31.C6 — ``DO_NOTHING``'s
    exact zero is a consequence of its definition rather than a branch in the optimizer.

    The same input shape is given to **every** action in ``ACTION_PRECEDENCE``: four zero
    cost terms and an intervention probability equal to the baseline. Each one comes back
    with a net recovery value of exactly zero. That is the distinction R31.C6 and the
    design both insist on — there is deliberately no ``if action is DO_NOTHING`` in
    :func:`evaluate_candidate`, and a special case would make the neutrality a behaviour
    of the optimizer rather than a property of the definition. If one were added, this
    test would keep passing for ``DO_NOTHING`` and start failing for the other nine,
    which is precisely the signal wanted.
    """
    base = Probability(baseline)
    candidate = CandidateInput(
        action=action,
        intervention_probability=base,
        financial_cost=Minor(0),
        communication_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )

    evaluated = evaluate_candidate(candidate, baseline=base, amount=amount)

    assert int(evaluated.total_cost) == 0
    assert int(evaluated.net_recovery_value) == 0
    assert type(evaluated.net_recovery_value) is int

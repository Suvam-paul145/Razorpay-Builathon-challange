"""The two null actions are neutral by definition, for every case.

Feature: candidate prior lookup (task 15.3/15.4, R6.C1, R6.C4, R6.C9, R6.C10, R6.C11).
Property: P19 — ``DO_NOTHING`` has an intervention probability exactly equal to the
baseline and all three costs exactly zero, with method ``DEFINITIONAL``; ``WAIT`` has
zero action cost and zero customer cost.

Why "exactly" is the whole test. The optimizer computes
``incremental = intervention - baseline`` and then multiplies it by the payment amount.
If ``DO_NOTHING``'s probability were *estimated* — even by an excellent estimator — its
incremental value would be noise scattered around zero, and roughly half of all cases
would show a positive net value for doing nothing while the other half showed a negative
one. Either is absurd, and the second is worse: a negative net value for the null action
makes every real action look better than it is. So the identity has to hold at the level
of the stored value, not to four decimal places, and that is what these properties check.

The set-membership and retention properties are here too, because they are the other half
of the same idea: a comparison is only honest if the things it declined to do are visible.
An unavailable action retained with a reason is a recorded decision; an omitted one is an
absence nobody can question.

All ``pure`` tier: :func:`build_candidate_set` takes a cause, a probability and two
durations and returns a frozen dataclass.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revora.domain.actions import (
    NULL_ACTIONS,
    UNAVAILABLE_IN_MVP,
    CandidateAction,
    candidate_set_for,
)
from revora.domain.enums import ActionAvailability, EstimationMethod, RiskCause
from revora.domain.money import ZERO
from revora.domain.probability import Probability
from revora.estimation.candidates import (
    CandidateSet,
    build_candidate_set,
    wait_probability,
    weakest_method,
)
from tests.strategies.primitives import probabilities, risk_causes

pytestmark = pytest.mark.pure

WINDOW = timedelta(days=7)
"""``RECOVERY_WINDOW_DURATION``'s seeded default. A literal rather than a configuration
read, for the reason ``tests/strategies/primitives`` gives: a property test that explores
different values on different machines is not a property test."""


def remaining_windows() -> st.SearchStrategy[timedelta]:
    """Time left in the recovery window, including the degenerate ends.

    Negative values are generated deliberately. A lifecycle sweep can evaluate a case
    moments after its window closed, and "the window is already over" has to produce a
    zero probability rather than an exponent of a negative fraction.
    """
    return st.one_of(
        st.sampled_from(
            [
                timedelta(0),
                timedelta(seconds=-1),
                timedelta(days=-3),
                timedelta(microseconds=1),
                timedelta(hours=1),
                WINDOW,
                WINDOW + timedelta(days=1),
            ]
        ),
        st.integers(min_value=-604_800, max_value=1_209_600).map(
            lambda seconds: timedelta(seconds=seconds)
        ),
    )


def candidate_sets() -> st.SearchStrategy[CandidateSet]:
    """A candidate set for an arbitrary case."""
    return st.builds(
        build_candidate_set,
        risk_causes(),
        baseline=probabilities(),
        remaining=remaining_windows(),
        window=st.just(WINDOW),
        memory_available=st.booleans(),
    )


# ---------------------------------------------------------------------------
# P19: DO_NOTHING is definitionally neutral
# ---------------------------------------------------------------------------


@given(
    cause=risk_causes(),
    baseline=probabilities(),
    remaining=remaining_windows(),
    memory_available=st.booleans(),
)
@settings(max_examples=500)
def test_do_nothing_probability_equals_the_baseline_exactly(
    cause: RiskCause,
    baseline: Probability,
    remaining: timedelta,
    memory_available: bool,
) -> None:
    """``DO_NOTHING``'s probability is the baseline, identically.

    Asserted on the ``Decimal`` value and on the ``Probability`` object, so neither a
    requantization nor a reconstruction that merely agrees to four places would pass.
    """
    candidates = build_candidate_set(
        cause,
        baseline=baseline,
        remaining=remaining,
        window=WINDOW,
        memory_available=memory_available,
    )
    figure = candidates.figure_for(CandidateAction.DO_NOTHING)
    assert figure is not None
    assert figure.intervention_probability == baseline
    assert figure.intervention_probability.value == baseline.value


@given(
    cause=risk_causes(),
    baseline=probabilities(),
    remaining=remaining_windows(),
    memory_available=st.booleans(),
)
@settings(max_examples=500)
def test_do_nothing_costs_are_all_exactly_zero_and_definitional(
    cause: RiskCause,
    baseline: Probability,
    remaining: timedelta,
    memory_available: bool,
) -> None:
    """All three costs zero, all four methods ``DEFINITIONAL``, whatever else is true.

    Including when memory is unavailable. R6.C11 degrades every *estimated* figure to
    ``UNCALIBRATED``, and ``DO_NOTHING``'s figures are not estimated — a definitional
    zero cannot become uncalibrated, and letting it would make the null action's
    neutrality depend on the health of a database.
    """
    candidates = build_candidate_set(
        cause,
        baseline=baseline,
        remaining=remaining,
        window=WINDOW,
        memory_available=memory_available,
    )
    figure = candidates.figure_for(CandidateAction.DO_NOTHING)
    assert figure is not None
    assert figure.action_cost == ZERO
    assert figure.risk_cost == ZERO
    assert figure.customer_cost == ZERO
    assert figure.total_cost == ZERO
    assert figure.probability_method is EstimationMethod.DEFINITIONAL
    assert figure.action_cost_method is EstimationMethod.DEFINITIONAL
    assert figure.risk_cost_method is EstimationMethod.DEFINITIONAL
    assert figure.customer_cost_method is EstimationMethod.DEFINITIONAL
    assert figure.recorded_method is EstimationMethod.DEFINITIONAL
    assert figure.availability is ActionAvailability.AVAILABLE


@given(candidates=candidate_sets())
@settings(max_examples=500)
def test_wait_has_zero_action_and_customer_cost(candidates: CandidateSet) -> None:
    """R6.C10 fixes two of ``WAIT``'s three costs at zero, and they are definitional.

    Waiting issues no request and reaches no customer, so both figures are zero by what
    the action *is* rather than by a configured value that could be retuned upward.
    """
    figure = candidates.figure_for(CandidateAction.WAIT)
    assert figure is not None
    assert figure.action_cost == ZERO
    assert figure.customer_cost == ZERO
    assert figure.action_cost_method is EstimationMethod.DEFINITIONAL
    assert figure.customer_cost_method is EstimationMethod.DEFINITIONAL


# ---------------------------------------------------------------------------
# Set membership, and retention of what was ruled out
# ---------------------------------------------------------------------------


@given(candidates=candidate_sets())
@settings(max_examples=500)
def test_the_set_always_holds_both_null_actions_and_between_two_and_nine_members(
    candidates: CandidateSet,
) -> None:
    """R6.C1: 2 to 9 members, always including ``DO_NOTHING`` and ``WAIT``.

    The lower bound is the load-bearing one. A set that could come back with one member
    would let a cause with no eligible actions produce a recommendation with nothing to
    compare against, and "the only option was to act" is a conclusion nobody should be
    able to reach by an empty eligibility row.
    """
    actions = {figure.action for figure in candidates.figures}
    assert actions >= NULL_ACTIONS
    assert 2 <= len(candidates.figures) <= len(CandidateAction)
    assert len(actions) == len(candidates.figures)


@given(candidates=candidate_sets())
@settings(max_examples=500)
def test_every_member_has_all_four_figures_in_range(candidates: CandidateSet) -> None:
    """R6.C3 and R6.C7: four figures set, probability in [0, 1], costs non-negative ints.

    These are also the database's ``CHECK`` constraints, so a violation is an
    uncommittable row rather than a wrong number — which means a job that retries
    forever on a case nobody is watching.
    """
    for figure in candidates.figures:
        assert Decimal(0) <= figure.intervention_probability.value <= Decimal(1)
        for cost in (figure.action_cost, figure.risk_cost, figure.customer_cost):
            assert isinstance(cost, int)
            assert cost >= 0


@given(candidates=candidate_sets())
@settings(max_examples=500)
def test_unavailable_members_are_retained_and_carry_a_reason(
    candidates: CandidateSet,
) -> None:
    """R6.C9: an unavailable action stays in the recorded set, with a stated reason.

    Retention is the requirement, and the reason is what makes retention useful: the
    database's ``unavailable_requires_reason`` check refuses the alternative, because
    "unavailable" with no reason is what a dashboard renders as an unexplained absence.
    """
    for figure in candidates.figures:
        if figure.availability is ActionAvailability.UNAVAILABLE:
            assert figure.unavailable_reason
        else:
            assert figure.unavailable_reason is None
    for action in candidates.figures:
        if action.action in UNAVAILABLE_IN_MVP:
            assert action.availability is ActionAvailability.UNAVAILABLE


@given(cause=risk_causes(), baseline=probabilities())
@settings(max_examples=200)
def test_mvp_unavailable_actions_that_the_cause_permits_are_present_and_marked(
    cause: RiskCause, baseline: Probability
) -> None:
    """An eligible-but-unexecutable action appears in the set rather than vanishing.

    ``DELAYED_RETRY`` for an insufficient-funds failure is the concrete case: the
    eligibility table permits it, no verified provider capability supports it, and the
    dashboard is supposed to be able to say both of those things at once.
    """
    candidates = build_candidate_set(
        cause, baseline=baseline, remaining=timedelta(days=1), window=WINDOW
    )
    eligible = candidate_set_for(cause)
    for action in eligible & UNAVAILABLE_IN_MVP:
        figure = candidates.figure_for(action)
        assert figure is not None
        assert figure.availability is ActionAvailability.UNAVAILABLE
        assert figure.intervention_probability == baseline


@given(candidates=candidate_sets())
@settings(max_examples=500)
def test_cause_ineligible_actions_are_recorded_as_excluded(
    candidates: CandidateSet,
) -> None:
    """Every action outside the set is accounted for, none twice.

    R6.C2 requires every excluded action to be recorded with ``CAUSE_NOT_ELIGIBLE``.
    Membership plus exclusions must therefore partition the whole enumeration: an action
    that is in neither list is one the record silently forgot.
    """
    members = {figure.action for figure in candidates.figures}
    excluded = set(candidates.excluded_by_cause)
    assert not (members & excluded)
    assert members | excluded == set(CandidateAction)
    assert CandidateAction.PROMISE_TO_PAY_FOLLOW_UP in excluded


# ---------------------------------------------------------------------------
# R6.C11: a memory error degrades labels, never figures
# ---------------------------------------------------------------------------


@given(cause=risk_causes(), baseline=probabilities(), remaining=remaining_windows())
@settings(max_examples=300)
def test_memory_error_marks_every_estimated_figure_uncalibrated(
    cause: RiskCause, baseline: Probability, remaining: timedelta
) -> None:
    """With memory unreachable, no figure claims to be anything but uncalibrated.

    Except the definitional ones, which are not estimates. So the assertion is that
    every recorded method is one of those two, and specifically that nothing is left
    claiming ``PRIOR_FALLBACK`` — a prior applied deliberately is a stronger claim than
    the code is entitled to make when it could not read the segment at all.
    """
    candidates = build_candidate_set(
        cause,
        baseline=baseline,
        remaining=remaining,
        window=WINDOW,
        memory_available=False,
    )
    for figure in candidates.figures:
        assert figure.recorded_method in {
            EstimationMethod.UNCALIBRATED,
            EstimationMethod.DEFINITIONAL,
        }
        if figure.action is not CandidateAction.DO_NOTHING:
            assert figure.probability_method is EstimationMethod.UNCALIBRATED


def test_weakest_method_prefers_the_least_calibrated_claim() -> None:
    """The row's single method is the weakest of its four figures.

    Checked directly because it is the rule that stops a definitional zero cost from
    making an uncalibrated probability look checked.
    """
    assert (
        weakest_method(EstimationMethod.DEFINITIONAL, EstimationMethod.UNCALIBRATED)
        is EstimationMethod.UNCALIBRATED
    )
    assert (
        weakest_method(EstimationMethod.DEFINITIONAL, EstimationMethod.PRIOR_FALLBACK)
        is EstimationMethod.PRIOR_FALLBACK
    )
    assert (
        weakest_method(EstimationMethod.DEFINITIONAL, EstimationMethod.DEFINITIONAL)
        is EstimationMethod.DEFINITIONAL
    )


# ---------------------------------------------------------------------------
# WAIT's hazard
# ---------------------------------------------------------------------------


@given(baseline=probabilities(), remaining=remaining_windows())
@settings(max_examples=500)
def test_wait_probability_never_exceeds_the_baseline_within_the_window(
    baseline: Probability, remaining: timedelta
) -> None:
    """Waiting over part of the window cannot beat waiting over all of it.

    The monotonicity that makes ``WAIT`` meaningful: it is the baseline restricted to
    the time that is actually left, so it is bounded above by the baseline and below by
    zero. A violation would let ``WAIT`` show a positive incremental probability against
    the baseline, which would be the null action claiming an uplift.
    """
    derived = wait_probability(baseline, remaining=remaining, window=WINDOW)
    if remaining <= WINDOW:
        assert derived.value <= baseline.value
    assert Decimal(0) <= derived.value <= Decimal(1)


@given(baseline=probabilities())
@settings(max_examples=200)
def test_wait_equals_the_baseline_over_the_whole_window(baseline: Probability) -> None:
    """At the start of the window the two quantities are the same thing."""
    assert wait_probability(baseline, remaining=WINDOW, window=WINDOW) == baseline


@given(baseline=probabilities())
@settings(max_examples=200)
def test_wait_is_zero_once_the_window_has_closed(baseline: Probability) -> None:
    """No time left means no chance of recovering in it, whatever the baseline was."""
    assert wait_probability(
        baseline, remaining=timedelta(0), window=WINDOW
    ) == Probability(Decimal(0))
    assert wait_probability(
        baseline, remaining=timedelta(hours=-1), window=WINDOW
    ) == Probability(Decimal(0))


@given(
    baseline=probabilities(),
    shorter=st.integers(min_value=1, max_value=604_800),
    longer=st.integers(min_value=1, max_value=604_800),
)
@settings(max_examples=300)
def test_wait_probability_increases_with_time_remaining(
    baseline: Probability, shorter: int, longer: int
) -> None:
    """More time left is never worse.

    The constant-hazard assumption's one observable consequence, and the one a
    front-loaded hazard would also satisfy — so this property survives the assumption
    being replaced by a fitted one later.
    """
    if shorter > longer:
        shorter, longer = longer, shorter
    less = wait_probability(baseline, remaining=timedelta(seconds=shorter), window=WINDOW)
    more = wait_probability(baseline, remaining=timedelta(seconds=longer), window=WINDOW)
    assert less.value <= more.value

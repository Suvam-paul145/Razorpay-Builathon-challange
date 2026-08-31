"""Exclusion, ranking and selection. Ranking reads net recovery value and nothing else.

The order of operations here is the substance of Requirement 7, and two parts of it are
easy to get subtly wrong in ways no test would catch unless the test was written for it.

**The cost ratio must not be computed on non-positive expected revenue (R7.C14).** The
ratio is ``total_cost / expected_incremental_revenue``, and where the denominator is zero
that is a ``ZeroDivisionError``; where it is negative the ratio's sign flips and a large
cost starts looking like a small ratio. Either way the number is meaningless. So the
non-positive check comes *first* and returns, and the ratio is computed only for
candidates that got past it. ``cost_ratio`` stays ``None`` on the excluded ones, which is
the recorded evidence that the division was skipped rather than performed and discarded.

**High baseline short-circuits the ranking (R7.C6).** Where the baseline is at or above
``HIGH_BASELINE_THRESHOLD``, a null action is selected with
``HIGH_BASELINE_NO_INTERVENTION`` regardless of what the candidates say. This is checked
before the argmax rather than emerging from it, because it is a different claim: not
"nothing was worth doing" but "this customer was very likely to pay anyway". A merchant
being told Revora declined to act on a customer with a 0.9 recovery probability is the
product's credibility, and it should not depend on the arithmetic happening to agree.

**Ranking never reads probability magnitude and never reads an AI field.** It reads
``net_recovery_value``. The most likely action to work is frequently not the one worth
doing, and where those disagree the divergence is recorded (P18) rather than resolved in
favour of the more impressive-looking number. There is no AI field to read here in any
case: this module cannot import ``revora.reasoning``, and ``CandidateInput`` has nowhere
to put one.

**Ties resolve by a declared order, not by sort stability.** Equal net value goes to the
lower total cost; equal cost goes to the domain's ``ACTION_PRECEDENCE``, which runs from
cheapest and least intrusive to most. So a tie resolves toward doing less, and it
resolves the same way regardless of the order the candidates arrived in — a selection
that depended on input ordering would change between two callers that built the same set
differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from revora.domain.actions import ACTION_PRECEDENCE, CandidateAction
from revora.domain.enums import (
    DIVERGENCE_HIGHER_PROBABILITY_LOWER_NET_VALUE,
    ExclusionReason,
    SelectionReason,
)
from revora.domain.money import Minor, ratio
from revora.domain.probability import Probability
from revora.optimizer.arithmetic import (
    CandidateInput,
    EvaluatedCandidate,
    evaluate_candidate,
)

__all__ = ["SelectionResult", "Thresholds", "select"]

_PRECEDENCE_INDEX: dict[CandidateAction, int] = {
    action: index for index, action in enumerate(ACTION_PRECEDENCE)
}


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The four configured bounds the optimizer applies.

    Passed in rather than read from configuration, so this module needs no session and
    the properties can explore either side of each bound without a database. The caller
    — :mod:`revora.optimizer.service` — reads them from ``app_config`` and records the
    configuration version on the recommendation, which is what makes a past decision
    explicable after a bound has changed.
    """

    min_net_value: Minor
    min_incremental_probability: Decimal
    max_cost_to_value_ratio: Decimal
    high_baseline: Decimal


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """What the optimizer decided, and everything it decided against.

    ``candidates`` holds **every** input candidate, excluded ones included, in the input
    order. Nothing is dropped: R6.C9 keeps an unavailable action in the record so the
    dashboard can show a retry was considered, and R7.C8 wants every rejected
    alternative with its figures and its reason.
    """

    selected: EvaluatedCandidate
    selection_reason: SelectionReason
    candidates: tuple[EvaluatedCandidate, ...]
    divergence_reason: str | None = None

    @property
    def survivors(self) -> tuple[EvaluatedCandidate, ...]:
        """Candidates that cleared every exclusion, in rank order."""
        ranked = [c for c in self.candidates if c.rank is not None]
        return tuple(sorted(ranked, key=lambda c: c.rank or 0))


def select(
    candidates: tuple[CandidateInput, ...] | list[CandidateInput],
    *,
    baseline: Probability,
    amount: Minor,
    thresholds: Thresholds,
) -> SelectionResult:
    """Evaluate, exclude, rank and select. Pure.

    Args:
        candidates: the candidate set, 2 to 9 members, always including the two null
            actions. Order is irrelevant to the outcome by design.
        baseline: recovery probability with no intervention.
        amount: the payment amount in integer minor units.
        thresholds: the four configured bounds.

    Returns:
        A :class:`SelectionResult`. A decision is always reached — the null actions are
        never excluded for value reasons, so there is always something to select.
    """
    evaluated = [
        evaluate_candidate(candidate, baseline=baseline, amount=amount)
        for candidate in candidates
    ]
    screened = tuple(_apply_exclusions(item, thresholds) for item in evaluated)

    # R7.C6: a high baseline is decided before the ranking, not by it.
    if baseline.value >= thresholds.high_baseline:
        null_choice = _best_null_action(screened)
        return SelectionResult(
            selected=null_choice,
            selection_reason=SelectionReason.HIGH_BASELINE_NO_INTERVENTION,
            candidates=_with_ranks(screened),
            divergence_reason=None,
        )

    survivors = [item for item in screened if not item.excluded]
    ranked = _with_ranks(screened)

    if not survivors:
        return SelectionResult(
            selected=_best_null_action(screened),
            selection_reason=SelectionReason.NO_POSITIVE_VALUE,
            candidates=ranked,
            divergence_reason=None,
        )

    winner = min(survivors, key=_ranking_key)
    selected = next(
        item for item in ranked if item.action is winner.action
    )
    return SelectionResult(
        selected=selected,
        selection_reason=SelectionReason.HIGHEST_NET_VALUE,
        candidates=ranked,
        divergence_reason=_divergence(selected, survivors),
    )


# ---------------------------------------------------------------------------
# Exclusion
# ---------------------------------------------------------------------------


def _apply_exclusions(
    item: EvaluatedCandidate, thresholds: Thresholds
) -> EvaluatedCandidate:
    """Apply the exclusion rules in the one order that is safe.

    The order is not a preference. Each rule below is a precondition for the next one
    being meaningful:

    1. **Unavailable** — an action with no verified provider capability cannot be
       performed, so its figures describe an act that cannot occur. Excluded first
       because ranking it would be ranking a fiction.
    2. **Invalid estimate input** — a figure the estimation layer already rejected.
       Excluded before any arithmetic is trusted.
    3. **Non-positive incremental value** — and this is where the ratio is *not*
       computed. Returns immediately, leaving ``cost_ratio`` as ``None``.
    4. **Cost ratio exceeded** — safe to compute only now that the denominator is known
       positive.
    5. **Below the incremental-probability floor**, then **below the net-value floor** —
       the two configured thresholds a survivor must clear.

    The two null actions are never excluded by rules 3 through 6. ``DO_NOTHING`` has
    exactly zero net value by definition and ``WAIT`` frequently has little, so applying
    a positive-value floor to them would leave the optimizer with nothing to select and
    no way to express "acting is not worth it" — which is the answer the product most
    needs to be able to give.
    """
    if not item.available:
        return item.excluded_for(
            _reason_for_unavailable(item.unavailable_reason)
        )

    is_null = item.action in (CandidateAction.DO_NOTHING, CandidateAction.WAIT)
    if is_null:
        return item

    if item.expected_incremental_revenue <= 0:
        # R7.C14: no division. The ratio stays None as the evidence of that.
        return item.excluded_for(ExclusionReason.NON_POSITIVE_INCREMENTAL_VALUE)

    computed = item.with_ratio(
        ratio(item.total_cost, item.expected_incremental_revenue)
    )
    if computed.cost_ratio is not None and computed.cost_ratio > thresholds.max_cost_to_value_ratio:
        return computed.excluded_for(ExclusionReason.COST_RATIO_EXCEEDED)

    if computed.incremental_probability.value < thresholds.min_incremental_probability:
        return computed.excluded_for(ExclusionReason.BELOW_INCREMENTAL_PROBABILITY)

    if computed.net_recovery_value < int(thresholds.min_net_value):
        return computed.excluded_for(ExclusionReason.BELOW_NET_VALUE_THRESHOLD)

    return computed


def _reason_for_unavailable(recorded: str | None) -> ExclusionReason:
    """Carry the estimation layer's own reason through rather than re-deriving it.

    ``candidates.py`` already decided whether an action is unavailable because the
    provider capability is unverified or because its figures were rejected, and it
    records that as an ``ExclusionReason`` value. Re-deriving it here would be a second
    opinion that could disagree with the stored one.
    """
    if recorded is None:
        return ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED
    try:
        return ExclusionReason(recorded)
    except ValueError:
        return ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED


# ---------------------------------------------------------------------------
# Ranking and selection
# ---------------------------------------------------------------------------


def _ranking_key(item: EvaluatedCandidate) -> tuple[int, int, int]:
    """The total order survivors are ranked by. Lower is better.

    Three components, applied in order: net recovery value descending (negated, so a
    single ``min`` expresses it), then total cost ascending, then the declared precedence
    index ascending. Every component is an integer, so the comparison is exact and there
    is no tie the ordering cannot break — which is what makes the selection independent
    of input order.

    Probability magnitude appears nowhere in this key, and that is the point.
    """
    return (-int(item.net_recovery_value), int(item.total_cost), _PRECEDENCE_INDEX[item.action])


def _with_ranks(items: tuple[EvaluatedCandidate, ...]) -> tuple[EvaluatedCandidate, ...]:
    """Assign 1-based ranks to survivors, leaving excluded candidates unranked.

    Returned in the original input order, not in rank order, so a caller persisting the
    set writes rows in a stable order and the ranks are a column rather than a position.
    """
    survivors = sorted((item for item in items if not item.excluded), key=_ranking_key)
    ranks = {item.action: index for index, item in enumerate(survivors, start=1)}
    return tuple(
        item.ranked(ranks[item.action]) if item.action in ranks else item
        for item in items
    )


def _best_null_action(items: tuple[EvaluatedCandidate, ...]) -> EvaluatedCandidate:
    """The better of ``DO_NOTHING`` and ``WAIT``, with ``DO_NOTHING`` winning on equality.

    R7.C5 fixes the tie toward ``DO_NOTHING``. It is the more conservative of the two in
    a way that is easy to miss: ``WAIT`` consumes recovery window, and a case that waits
    until its window closes has spent the whole opportunity on a decision to do nothing
    slowly. On an exact tie, doing nothing at least leaves the window intact.

    Falls back to the first candidate if neither null action is present, which the
    estimation layer guarantees cannot happen — but a ``StopIteration`` escaping from
    here would be a crash in the one path that exists to always have an answer.
    """
    null_actions = [
        item
        for item in items
        if item.action in (CandidateAction.DO_NOTHING, CandidateAction.WAIT)
    ]
    if not null_actions:  # pragma: no cover - the candidate set always holds both
        return items[0]
    return min(
        null_actions,
        key=lambda item: (
            -int(item.net_recovery_value),
            _PRECEDENCE_INDEX[item.action],
        ),
    )


def _divergence(
    selected: EvaluatedCandidate, survivors: list[EvaluatedCandidate]
) -> str | None:
    """Record when the highest-probability survivor is not the selected one.

    This is Property 18 and the product's central argument: "most likely to work" and
    "worth doing" are different questions, and where they disagree a merchant is owed
    both numbers rather than the one that happened to win. Recorded at decision time
    rather than reconstructed later, because reconstructing it would need the full
    candidate set *and* the ranking rule as they were at the time.
    """
    if not survivors:
        return None
    highest = max(survivors, key=lambda item: item.intervention_probability.value)
    if highest.action is selected.action:
        return None
    return DIVERGENCE_HIGHER_PROBABILITY_LOWER_NET_VALUE

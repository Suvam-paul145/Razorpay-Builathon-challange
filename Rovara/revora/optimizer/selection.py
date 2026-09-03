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

R31.C4 restates that ordering over the four-term cost and asks for it **unchanged**, and
it is unchanged: the numerator moved from a three-term sum to a four-term one, and the
``NON_POSITIVE_INCREMENTAL_VALUE`` return still happens before any division exists to be
performed. Nothing in this module reads ``financial_cost`` or ``communication_cost``
individually — every rule here reads ``total_cost``, which sums all four — so for any
input whose two split terms add up to the pre-split blended figure, every exclusion
reason, every rank and the selected action are identical to the three-term form. That is
Property 67, and it is why the split is a presentation change rather than a decision
change.

**The pool that competes is not the same as the pool that survived exclusion (R7.C4, R7.C5).**
The two null actions are deliberately never excluded, so "everything unexcluded" is never empty —
and while the pool was defined that way, ``NO_POSITIVE_VALUE`` was unreachable code and a world
where acting made things worse selected ``DO_NOTHING`` while recording ``HIGHEST_NET_VALUE``. The
action was right and the sentence was wrong: a merchant was told doing nothing won a comparison
when the truth was that there was nothing to compare. :func:`_qualifies` is the requirement's own
four conditions, asked in one place, and it is what makes the two "did nothing" reasons distinct.

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
    qualifying_actions: tuple[CandidateAction, ...] = ()
    """The actions that actually competed: unexcluded *and* clearing both configured floors.

    Recorded rather than left for a caller to re-derive, because re-deriving it needs the
    thresholds as they were at the time and a second copy of the rule. It is also the field
    that makes the difference between the two "did nothing" reasons legible: empty means
    nothing qualified, which is ``NO_POSITIVE_VALUE``.
    """

    @property
    def survivors(self) -> tuple[EvaluatedCandidate, ...]:
        """Candidates that cleared every exclusion, in rank order.

        Not the same set as :attr:`qualifying_actions`. A null action is never *excluded* — it
        has no exclusion reason and it carries a rank — but ``DO_NOTHING`` has exactly zero net
        value by definition, so it does not clear ``MIN_NET_VALUE_THRESHOLD`` and it is not
        competing. Conflating the two is what made ``NO_POSITIVE_VALUE`` unreachable.
        """
        ranked = [c for c in self.candidates if c.rank is not None]
        return tuple(sorted(ranked, key=lambda c: c.rank or 0))

    @property
    def qualifying(self) -> tuple[EvaluatedCandidate, ...]:
        """The competing candidates themselves, in input order."""
        pool = set(self.qualifying_actions)
        return tuple(item for item in self.candidates if item.action in pool)


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
    ranked = _with_ranks(screened)
    qualifying = [item for item in screened if _qualifies(item, thresholds)]
    pool = tuple(item.action for item in qualifying)

    # R7.C6: a high baseline is decided before the ranking, not by it.
    if baseline.value >= thresholds.high_baseline:
        return SelectionResult(
            selected=_best_null_action(screened),
            selection_reason=SelectionReason.HIGH_BASELINE_NO_INTERVENTION,
            candidates=ranked,
            divergence_reason=None,
            qualifying_actions=pool,
        )

    if not qualifying:
        return SelectionResult(
            selected=_best_null_action(screened),
            selection_reason=SelectionReason.NO_POSITIVE_VALUE,
            candidates=ranked,
            divergence_reason=None,
            qualifying_actions=pool,
        )

    winner = min(qualifying, key=_ranking_key)
    selected = next(
        item for item in ranked if item.action is winner.action
    )
    return SelectionResult(
        selected=selected,
        selection_reason=SelectionReason.HIGHEST_NET_VALUE,
        candidates=ranked,
        divergence_reason=_divergence(selected, qualifying),
        qualifying_actions=pool,
    )


def _qualifies(item: EvaluatedCandidate, thresholds: Thresholds) -> bool:
    """Whether a candidate is actually competing for selection. R7.C4's pool, exactly.

    The requirement names four conditions: no exclusion reason, not ``UNAVAILABLE``, net value at
    or above ``MIN_NET_VALUE_THRESHOLD``, and incremental probability at or above
    ``MIN_INCREMENTAL_PROBABILITY``. The first two are already settled by
    :func:`_apply_exclusions`; the last two have to be re-asked here, and that is the whole point
    of this function.

    **This is the fix for a real bug.** The pool used to be "everything not excluded", and the two
    null actions are deliberately never excluded — so the pool was never empty, and
    ``NO_POSITIVE_VALUE`` was unreachable code. A world where acting reduces recovery therefore
    selected ``DO_NOTHING`` (correctly) and recorded ``HIGHEST_NET_VALUE`` (misleadingly): a
    merchant reading that would be told doing nothing *won* a comparison, when the truth is there
    was nothing to compare. R7.C5 asks for the other sentence, and the two are not interchangeable.
    Found by the negative synthetic scenario, which is exactly what it exists for.

    Note that this is not "drop the null actions". ``WAIT`` genuinely can clear both floors — its
    probability comes from the no-intervention hazard over the time left in the window — and when
    it does it competes and can win on merit, with ``HIGHEST_NET_VALUE`` recorded honestly.
    ``DO_NOTHING`` never qualifies, because its net value is definitionally zero.
    """
    if item.excluded:
        return False
    if int(item.net_recovery_value) < int(thresholds.min_net_value):
        return False
    return item.incremental_probability.value >= thresholds.min_incremental_probability


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
       positive. Its numerator is ``total_cost``, the four-term sum, per R31.C4.
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

    The key is unchanged by the four-term split. Both of its first two components are
    integer sums over the same cost terms, so a candidate whose blended total is unchanged
    lands at exactly the same position — see the module docstring and Property 67.

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

    **An excluded null action is never chosen while an unexcluded one exists.** ``WAIT``
    can arrive marked ``UNAVAILABLE``, and it can still hold the greater net value — so
    ranking the nulls on net value alone would select an action that cannot be performed
    and record it as the decision. That is worse than choosing the weaker option: it is a
    decision to do something that will not happen. Found by P15 once
    ``NO_POSITIVE_VALUE`` became reachable and this function started being reached with a
    real candidate set; before that it was latent under the high-baseline branch, where no
    property asserted the selected candidate was performable.

    Falls back to the whole null set if both are excluded, preferring ``DO_NOTHING``,
    because doing nothing cannot fail at a provider and an input claiming otherwise is
    malformed rather than informative. The estimation layer never produces that input; the
    fallback exists because a ``StopIteration`` escaping the one path that must always have
    an answer would be an outage.
    """
    null_actions = [
        item
        for item in items
        if item.action in (CandidateAction.DO_NOTHING, CandidateAction.WAIT)
    ]
    if not null_actions:  # pragma: no cover - the candidate set always holds both
        return items[0]
    performable = [item for item in null_actions if not item.excluded]
    return min(
        performable or null_actions,
        key=lambda item: (
            -int(item.net_recovery_value),
            _PRECEDENCE_INDEX[item.action],
        ),
    )


def _divergence(
    selected: EvaluatedCandidate, qualifying: list[EvaluatedCandidate]
) -> str | None:
    """Record when the highest-probability *competing* candidate is not the selected one.

    This is Property 18 and the product's central argument: "most likely to work" and
    "worth doing" are different questions, and where they disagree a merchant is owed
    both numbers rather than the one that happened to win. Recorded at decision time
    rather than reconstructed later, because reconstructing it would need the full
    candidate set *and* the ranking rule as they were at the time.

    Computed over the qualifying pool rather than over everything unexcluded, because a
    divergence between two candidates that were never in contention is not a finding. The
    previous version could report one between ``WAIT`` and ``DO_NOTHING`` in a case where nothing
    qualified at all, which reads as "we chose the less likely option" when nothing was chosen.
    """
    if not qualifying:
        return None
    highest = max(qualifying, key=lambda item: item.intervention_probability.value)
    if highest.action is selected.action:
        return None
    return DIVERGENCE_HIGHER_PROBABILITY_LOWER_NET_VALUE

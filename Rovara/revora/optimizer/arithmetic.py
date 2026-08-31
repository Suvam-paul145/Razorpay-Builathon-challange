"""The three-step value chain, in integer minor units. This is the product.

Requirement 7 in one paragraph: for each candidate action, subtract the baseline
probability from the intervention probability to get a signed increment; multiply that
increment by the payment amount to get expected incremental revenue in integer minor
units; subtract the three costs to get net recovery value. Rank on that last number and
nothing else.

**Why this module has almost no dependencies.** It imports ``revora.domain`` and the
standard library, and that is all. It cannot reach a database, a provider, a clock, a
logger, or the reasoning layer — the import contracts make the last one structural
rather than a promise, which is half of Property 2. What that buys is that every rule
below is a pure function of its arguments, so the six properties covering it run at 500
examples in microseconds with no fixtures. This is the code most likely to be quietly
wrong and least likely to announce it, so it is the code that gets the cheapest and
densest test coverage in the system.

**Rounding happens exactly once, and not here.** The single multiplication of a
probability into money is delegated to ``domain.money.multiply_probability``, which is
the only function in Revora permitted to turn a non-integer into a currency figure. This
module never pre-rounds the increment and never re-rounds the product. Rounding twice is
how a reported total stops matching the sum of the rows it was computed from, and a
recovery figure that does not reconcile is a recovery figure nobody should believe.

**Negatives survive.** An action estimated to make recovery *less* likely produces a
negative increment and a negative expected revenue, and both are retained. The candidate
is then excluded by a stated reason in ``selection``, not by having been quietly
flattened to zero on the way in. The distinction matters because the two look identical
in a stored row and completely different in an explanation.

**No ``float``, checked mechanically.** ``scripts/check_no_float.py`` treats this whole
package as currency-bearing, so the token cannot appear here as an annotation, a call or
a literal. Probabilities are ``Decimal``; money is ``int``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from revora.domain.actions import CandidateAction, is_customer_visible
from revora.domain.enums import ActionAvailability
from revora.domain.money import Minor, multiply_probability
from revora.domain.probability import Probability, SignedIncrement, increment

__all__ = ["CandidateInput", "EvaluatedCandidate", "evaluate_candidate"]


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """One candidate action's estimate, as the optimizer reads it.

    A plain frozen struct rather than the ORM row, deliberately. Two reasons, and the
    second is the load-bearing one:

    1. It keeps this module free of ``persistence``, which is what lets the properties
       run with no database.
    2. **There is nowhere for an AI-produced value to sit.** The fields are enumerated
       and typed; there is no ``dict``, no ``Any``, no ``**kwargs`` and no ``extra``. A
       caller that wanted to smuggle a model's opinion into the ranking would have to
       add a field to this class, in a module that cannot import the reasoning layer,
       and every reader of the diff would see it. That is the structural half of
       Property 2 — the optimizer's inability to be influenced by AI output is a fact
       about its type signature rather than a rule someone has to remember.

    Costs are three separate figures rather than a total because they answer different
    questions and are owned by different people. ``customer_cost`` in particular prices
    the intrusion on the customer, and a value model that cannot see the customer's
    interest will spend it.
    """

    action: CandidateAction
    intervention_probability: Probability
    action_cost: Minor
    risk_cost: Minor
    customer_cost: Minor
    availability: ActionAvailability = ActionAvailability.AVAILABLE
    unavailable_reason: str | None = None

    @property
    def total_cost(self) -> Minor:
        """The three costs summed. Integer addition, so exact."""
        return Minor(int(self.action_cost) + int(self.risk_cost) + int(self.customer_cost))

    @property
    def available(self) -> bool:
        return self.availability is ActionAvailability.AVAILABLE


@dataclass(frozen=True, slots=True)
class EvaluatedCandidate:
    """A candidate with its full arithmetic, its exclusion state and its rank.

    Every figure the selection was made from is carried, not just the winner's. That is
    what lets the dashboard show the comparison, what lets a merchant challenge a
    decision, and what makes the exclusion reasons auditable. ``exclusion_reason`` is
    typed as ``object`` rather than as the enum only because this module must not import
    ``domain.enums``' exclusion vocabulary into its own arithmetic concerns — see
    :mod:`revora.optimizer.selection`, which owns exclusion and fills these in.
    """

    action: CandidateAction
    intervention_probability: Probability
    incremental_probability: SignedIncrement
    expected_incremental_revenue: Minor
    action_cost: Minor
    risk_cost: Minor
    customer_cost: Minor
    net_recovery_value: Minor
    availability: ActionAvailability
    unavailable_reason: str | None = None
    excluded: bool = False
    exclusion_reason: object | None = None
    cost_ratio: Decimal | None = None
    """The cost-to-value ratio, or ``None`` when it was **not computed**.

    ``None`` is a statement about what happened rather than a missing value: R7.C14
    forbids performing the division at all where expected incremental revenue is zero or
    negative, so a ``None`` here on such a candidate is the evidence that the division
    was correctly skipped. A zero would be indistinguishable from a ratio that came out
    at zero."""

    rank: int | None = None
    """Position among survivors, 1-based. ``None`` for an excluded candidate, because an
    excluded action has no position in an ordering it was never part of."""

    @property
    def total_cost(self) -> Minor:
        return Minor(int(self.action_cost) + int(self.risk_cost) + int(self.customer_cost))

    @property
    def available(self) -> bool:
        return self.availability is ActionAvailability.AVAILABLE

    @property
    def is_customer_visible(self) -> bool:
        """Whether the customer perceives this action, so it consumes the message cap."""
        return is_customer_visible(self.action)

    def excluded_for(self, reason: object) -> EvaluatedCandidate:
        """This candidate, marked excluded for ``reason`` and stripped of its rank."""
        return EvaluatedCandidate(
            action=self.action,
            intervention_probability=self.intervention_probability,
            incremental_probability=self.incremental_probability,
            expected_incremental_revenue=self.expected_incremental_revenue,
            action_cost=self.action_cost,
            risk_cost=self.risk_cost,
            customer_cost=self.customer_cost,
            net_recovery_value=self.net_recovery_value,
            availability=self.availability,
            unavailable_reason=self.unavailable_reason,
            excluded=True,
            exclusion_reason=reason,
            cost_ratio=self.cost_ratio,
            rank=None,
        )

    def with_ratio(self, ratio: Decimal) -> EvaluatedCandidate:
        """This candidate, carrying a computed cost ratio."""
        return EvaluatedCandidate(
            action=self.action,
            intervention_probability=self.intervention_probability,
            incremental_probability=self.incremental_probability,
            expected_incremental_revenue=self.expected_incremental_revenue,
            action_cost=self.action_cost,
            risk_cost=self.risk_cost,
            customer_cost=self.customer_cost,
            net_recovery_value=self.net_recovery_value,
            availability=self.availability,
            unavailable_reason=self.unavailable_reason,
            excluded=self.excluded,
            exclusion_reason=self.exclusion_reason,
            cost_ratio=ratio,
            rank=self.rank,
        )

    def ranked(self, rank: int) -> EvaluatedCandidate:
        """This candidate, carrying its position among the survivors."""
        return EvaluatedCandidate(
            action=self.action,
            intervention_probability=self.intervention_probability,
            incremental_probability=self.incremental_probability,
            expected_incremental_revenue=self.expected_incremental_revenue,
            action_cost=self.action_cost,
            risk_cost=self.risk_cost,
            customer_cost=self.customer_cost,
            net_recovery_value=self.net_recovery_value,
            availability=self.availability,
            unavailable_reason=self.unavailable_reason,
            excluded=self.excluded,
            exclusion_reason=self.exclusion_reason,
            cost_ratio=self.cost_ratio,
            rank=rank,
        )


def evaluate_candidate(
    candidate: CandidateInput, *, baseline: Probability, amount: Minor
) -> EvaluatedCandidate:
    """Run the three-step chain for one candidate. Pure, exact, total.

    Args:
        candidate: the action's estimate.
        baseline: the probability of recovery with no intervention. The subtrahend of
            every incremental claim in the system.
        amount: the payment amount in integer minor units.

    Returns:
        An :class:`EvaluatedCandidate` carrying all six figures. Not yet excluded and
        not yet ranked — exclusion and ranking are :mod:`revora.optimizer.selection`'s
        job, and keeping them apart is what makes the arithmetic testable without the
        thresholds.

    The chain:

    1. ``incremental_probability = intervention - baseline``, via
       ``domain.probability.increment``, which keeps the sign.
    2. ``expected_incremental_revenue = amount * incremental_probability``, via
       ``domain.money.multiply_probability``, which applies half-up rounding exactly
       once and returns an integer.
    3. ``net_recovery_value = expected - action_cost - risk_cost - customer_cost``, pure
       integer subtraction.

    For ``DO_NOTHING`` the estimation layer passes the baseline through unchanged, so
    step 1 yields exactly ``0.0000``, step 2 yields exactly ``0``, and with three zero
    costs step 3 yields exactly ``0``. That is Property 19, and it holds by construction
    rather than by a special case here — there is no ``if action is DO_NOTHING`` in this
    function, and there must not be, because a special case would make the neutrality a
    behaviour of the optimizer rather than a consequence of the definition.
    """
    incremental = increment(candidate.intervention_probability, baseline)
    expected = multiply_probability(amount, incremental.value)
    net = Minor(
        int(expected)
        - int(candidate.action_cost)
        - int(candidate.risk_cost)
        - int(candidate.customer_cost)
    )
    return EvaluatedCandidate(
        action=candidate.action,
        intervention_probability=candidate.intervention_probability,
        incremental_probability=incremental,
        expected_incremental_revenue=expected,
        action_cost=candidate.action_cost,
        risk_cost=candidate.risk_cost,
        customer_cost=candidate.customer_cost,
        net_recovery_value=net,
        availability=candidate.availability,
        unavailable_reason=candidate.unavailable_reason,
    )

"""Candidate-set strategies for the value optimizer's property tests.

The optimizer is the product, and the four situations it must handle correctly are
exactly the ones a uniform random sample almost never produces:

* **Ties on net value.** Two candidates with identical net value have to resolve by a
  declared order rather than by whichever the sort happened to put first, because a
  selection that depends on dictionary ordering is a selection that changes between
  Python versions. So ties are generated deliberately, not hoped for.
* **High-probability, high-cost divergence.** The candidate most likely to work is not
  always the one worth doing, and that divergence is the entire argument for the
  product. If the generator never produces it, P18 passes without ever having been
  tested.
* **Negative incremental values.** An action estimated to make recovery *less* likely
  is a legitimate estimate, and the arithmetic must keep the sign rather than clipping
  it. Generated explicitly because a plausible-looking prior table produces them
  rarely.
* **Zero or negative expected revenue.** The one case where R7.C14 forbids performing
  the cost-ratio division at all. A generator that never produces a zero denominator
  cannot catch a division that should not have happened.

Every amount is an integer in minor units and every probability is a ``Decimal``. There
is no ``float`` in this module, for the same reason there is none in the optimizer: a
strategy that generated a binary value would be testing arithmetic the system never
performs.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import strategies as st

from revora.domain.actions import ACTION_PRECEDENCE, CandidateAction
from revora.domain.enums import ActionAvailability
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.optimizer.arithmetic import CandidateInput

__all__ = [
    "candidate_estimate_set",
    "candidate_inputs",
    "diverging_candidate_set",
    "tied_candidate_set",
]

_EPSILON = Decimal("0.0001")

#: Costs chosen around the configured thresholds rather than uniformly. The interesting
#: costs are the ones that push a cost ratio just either side of the bound and the ones
#: that make two candidates tie on total cost after tying on net value.
_INTERESTING_COSTS: tuple[int, ...] = (0, 1, 1_000, 2_000, 5_000, 25_000, 50_000)


def _costs() -> st.SearchStrategy[int]:
    """A non-negative cost in minor units, biased to the interesting values."""
    return st.one_of(
        st.sampled_from(_INTERESTING_COSTS),
        st.integers(min_value=0, max_value=200_000),
    )


def _probability_values() -> st.SearchStrategy[Decimal]:
    """A probability as an exact four-place ``Decimal`` in ``[0, 1]``.

    Built from an integer count of ten-thousandths rather than from a decimal string,
    so every generated value is exactly representable at the stored precision and
    Hypothesis shrinks toward the boundaries rather than toward a shorter string.
    """
    return st.integers(min_value=0, max_value=10_000).map(lambda k: Decimal(k).scaleb(-4))


@st.composite
def candidate_inputs(
    draw: st.DrawFn,
    *,
    action: CandidateAction | None = None,
    baseline: Probability | None = None,
    allow_unavailable: bool = True,
) -> CandidateInput:
    """One candidate, with the availability and figures the optimizer reads.

    ``DO_NOTHING`` is special-cased to its definitional form — probability exactly the
    baseline, all costs zero — because that is the only form the estimation layer ever
    produces for it, and a generator that produced any other would be testing the
    optimizer against an input that cannot occur while failing P19 for the wrong reason.
    """
    chosen = action if action is not None else draw(st.sampled_from(ACTION_PRECEDENCE))
    base = baseline if baseline is not None else Probability(draw(_probability_values()))

    if chosen is CandidateAction.DO_NOTHING:
        return CandidateInput(
            action=chosen,
            intervention_probability=base,
            action_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        )

    availability = ActionAvailability.AVAILABLE
    unavailable_reason: str | None = None
    if allow_unavailable and draw(st.booleans()):
        availability = ActionAvailability.UNAVAILABLE
        unavailable_reason = "PROVIDER_CAPABILITY_UNVERIFIED"

    return CandidateInput(
        action=chosen,
        intervention_probability=Probability(draw(_probability_values())),
        action_cost=Minor(draw(_costs())),
        risk_cost=Minor(draw(_costs())),
        customer_cost=Minor(draw(_costs())),
        availability=availability,
        unavailable_reason=unavailable_reason,
    )


@st.composite
def candidate_estimate_set(
    draw: st.DrawFn,
    *,
    min_size: int = 2,
    max_size: int = 9,
    baseline: Probability | None = None,
) -> tuple[Probability, tuple[CandidateInput, ...]]:
    """A baseline and a candidate set of 2 to 9 members with no duplicate action.

    ``DO_NOTHING`` and ``WAIT`` are always present, exactly as the estimation layer
    guarantees, so a property about the null actions is never vacuously true because the
    generator omitted them.

    Returns the baseline alongside the set because every figure the optimizer computes
    is a difference against it — a candidate set without its baseline is not a testable
    input.
    """
    base = baseline if baseline is not None else Probability(draw(_probability_values()))

    members: list[CandidateAction] = [CandidateAction.DO_NOTHING, CandidateAction.WAIT]
    remaining = [
        action for action in ACTION_PRECEDENCE if action not in members
    ]
    extra_count = draw(st.integers(min_value=max(0, min_size - 2), max_value=max_size - 2))
    if extra_count:
        members.extend(draw(st.permutations(remaining))[:extra_count])

    ordered = [action for action in ACTION_PRECEDENCE if action in members]
    candidates = [
        draw(candidate_inputs(action=action, baseline=base)) for action in ordered
    ]
    return base, tuple(candidates)


@st.composite
def tied_candidate_set(
    draw: st.DrawFn,
) -> tuple[Probability, tuple[CandidateInput, ...]]:
    """A set where two available candidates have identical net value.

    Constructed rather than searched for. Two candidates share a probability and a total
    cost, so their net values are equal by construction and the tie-break has to decide
    between them — which is what P15's declared ordering exists for. The two costs are
    split differently across the three cost columns so the tie survives the total-cost
    tie-break as well and falls through to precedence.
    """
    base = Probability(draw(st.integers(min_value=0, max_value=5_000).map(
        lambda k: Decimal(k).scaleb(-4)
    )))
    uplift = draw(st.integers(min_value=1_000, max_value=4_000)).__index__()
    shared_probability = Probability(base.value + Decimal(uplift).scaleb(-4))
    total = draw(st.sampled_from((0, 1_000, 5_000)))

    first = CandidateInput(
        action=CandidateAction.PAYMENT_LINK,
        intervention_probability=shared_probability,
        action_cost=Minor(total),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    second = CandidateInput(
        action=CandidateAction.CUSTOMER_MESSAGE,
        intervention_probability=shared_probability,
        action_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(total),
    )
    do_nothing = CandidateInput(
        action=CandidateAction.DO_NOTHING,
        intervention_probability=base,
        action_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    wait = CandidateInput(
        action=CandidateAction.WAIT,
        intervention_probability=base,
        action_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    return base, (do_nothing, wait, first, second)


@st.composite
def diverging_candidate_set(
    draw: st.DrawFn,
) -> tuple[Probability, tuple[CandidateInput, ...]]:
    """A set where the highest-probability candidate is NOT the highest-net-value one.

    The product's whole argument in one input. ``HUMAN_ESCALATION`` gets the larger
    uplift and a cost large enough to sink its net value below ``PAYMENT_LINK``'s, so a
    correct optimizer selects the link and records
    ``HIGHER_PROBABILITY_LOWER_NET_VALUE``. An optimizer that ranked on probability —
    which is the plausible bug — selects the escalation and fails P18.
    """
    base = Probability(draw(st.integers(min_value=0, max_value=3_000).map(
        lambda k: Decimal(k).scaleb(-4)
    )))
    modest = Probability(base.value + Decimal("0.0500"))
    large = Probability(base.value + Decimal("0.2000"))

    return base, (
        CandidateInput(
            action=CandidateAction.DO_NOTHING,
            intervention_probability=base,
            action_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
        CandidateInput(
            action=CandidateAction.WAIT,
            intervention_probability=base,
            action_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
        CandidateInput(
            action=CandidateAction.PAYMENT_LINK,
            intervention_probability=modest,
            action_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(1_000),
        ),
        CandidateInput(
            action=CandidateAction.HUMAN_ESCALATION,
            intervention_probability=large,
            action_cost=Minor(10_000_000),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
    )

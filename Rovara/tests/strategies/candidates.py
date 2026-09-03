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
    "cost_partitions",
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

    ``WAIT`` is constrained too, and that was a gap: it was being generated ``UNAVAILABLE``
    and with arbitrary action and customer costs, neither of which the estimation layer can
    produce. ``WAIT`` is in ``EXECUTABLE_ACTIONS`` — waiting cannot fail at a provider — and
    R6.C10 fixes its financial, communication and customer costs at zero, since it spends
    the recovery window rather than money. Its ``risk_cost`` is still drawn, because that
    figure is a real estimate. The unconstrained version was not harmless: it made P15 fail
    for an input the system cannot reach, which hides whether the real bug it also found was
    fixed.

    **``communication_cost`` is fixed at zero here, and that is deliberate rather than
    lazy.** R31.C1 split the blended ``action_cost`` into ``financial_cost`` plus
    ``communication_cost``; this generator carries the whole blended figure in
    ``financial_cost`` so that every property already written against the three-term form
    sees an input whose four-term total is identical to the three-term total it was
    validated against. Any other choice would change the distribution these properties
    explore at the same time as the arithmetic changed under them, and a verdict that moved
    would then be unattributable. Exploring the partition itself is
    :func:`cost_partitions`' job and Property 67's subject: P67 establishes that the
    partition point is behaviourally inert, which is what makes the zero here a
    generalisation rather than a coverage hole.
    """
    chosen = action if action is not None else draw(st.sampled_from(ACTION_PRECEDENCE))
    base = baseline if baseline is not None else Probability(draw(_probability_values()))

    if chosen is CandidateAction.DO_NOTHING:
        return CandidateInput(
            action=chosen,
            intervention_probability=base,
            financial_cost=Minor(0),
            communication_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        )

    if chosen is CandidateAction.WAIT:
        return CandidateInput(
            action=chosen,
            intervention_probability=Probability(draw(_probability_values())),
            financial_cost=Minor(0),
            communication_cost=Minor(0),
            risk_cost=Minor(draw(_costs())),
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
        financial_cost=Minor(draw(_costs())),
        communication_cost=Minor(0),
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
    split differently across the four cost columns so the tie survives the total-cost
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
        financial_cost=Minor(total),
        communication_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    second = CandidateInput(
        action=CandidateAction.CUSTOMER_MESSAGE,
        intervention_probability=shared_probability,
        financial_cost=Minor(0),
        communication_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(total),
    )
    do_nothing = CandidateInput(
        action=CandidateAction.DO_NOTHING,
        intervention_probability=base,
        financial_cost=Minor(0),
        communication_cost=Minor(0),
        risk_cost=Minor(0),
        customer_cost=Minor(0),
    )
    wait = CandidateInput(
        action=CandidateAction.WAIT,
        intervention_probability=base,
        financial_cost=Minor(0),
        communication_cost=Minor(0),
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
            financial_cost=Minor(0),
            communication_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
        CandidateInput(
            action=CandidateAction.WAIT,
            intervention_probability=base,
            financial_cost=Minor(0),
            communication_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
        CandidateInput(
            action=CandidateAction.PAYMENT_LINK,
            intervention_probability=modest,
            financial_cost=Minor(0),
            communication_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(1_000),
        ),
        CandidateInput(
            action=CandidateAction.HUMAN_ESCALATION,
            intervention_probability=large,
            financial_cost=Minor(10_000_000),
            communication_cost=Minor(0),
            risk_cost=Minor(0),
            customer_cost=Minor(0),
        ),
    )


# ---------------------------------------------------------------------------
# Repartitioning a blended cost — the generator Property 67 is built on
# ---------------------------------------------------------------------------

PARTITION_EXHAUSTIVE_LIMIT = 32
"""Totals at or below this are partitioned **exhaustively**, every point.

Above it the partition set is sampled, because R31.C1 puts no bound on a cost figure and
``_costs()`` draws up to 200 000 — enumerating 200 001 partition points and running a
selection at each would turn a microsecond property into a minute of arithmetic for no
new information. 32 is chosen so that the exhaustive branch is reached often rather than
rarely: ``_INTERESTING_COSTS`` contributes 0 and 1, and the design's own zero-cost null
actions land here on every example, so the fully-enumerated case is the common one and
the sampled case is the tail."""

PARTITION_SAMPLE_SIZE = 12
"""Interior partition points drawn for a total too large to enumerate.

Twelve rather than three because the cost of one extra point is one ``select()`` over a
nine-member set of pure integer arithmetic, and the failure this property is guarding
against — something downstream reading ``financial_cost`` on its own — would show up at
*some* interior point rather than reliably at a boundary."""


@st.composite
def cost_partitions(
    draw: st.DrawFn,
    *,
    total: int,
    exhaustive_limit: int = PARTITION_EXHAUSTIVE_LIMIT,
    sample_size: int = PARTITION_SAMPLE_SIZE,
) -> tuple[tuple[Minor, Minor], ...]:
    """Every way to split ``total`` into ``(financial_cost, communication_cost)``.

    R31.C8 is an obligation about a *set* of inputs rather than about one input: the
    selection must be identical for **any** pair of cost figures adding up to the
    pre-split blended ``action_cost``. So this returns the whole family of partition
    points at once, and Property 67 runs a selection at each of them and compares.

    Args:
        total: the blended pre-split ``action_cost``, in minor units. Must not be
            negative — R31.C1 makes both terms counts, and a negative cost is not an
            input the estimation layer can produce.
        exhaustive_limit: totals at or below this are enumerated completely.
        sample_size: interior points drawn when ``total`` exceeds that limit.

    Returns:
        A tuple of ``(financial_cost, communication_cost)`` pairs, each summing to
        ``total``, with no duplicates. **The first element is always ``(total, 0)``**,
        and that ordering is load-bearing rather than cosmetic — see below.

    **Why ``(total, 0)`` comes first: it is the pre-split computation.** The three-term
    arithmetic this requirement replaced had one blended ``action_cost``, and the
    four-term arithmetic reduces to it exactly when ``communication_cost`` is zero. So
    the partition point ``(total, 0)`` is not a restatement of the post-split formula —
    it is the *old* formula, evaluated by the new code. That is what makes Property 67 a
    genuine differential test with no second copy of the arithmetic to maintain: the
    oracle is an element of the generated family, and a reference implementation kept
    beside the real one could drift out of sync and let the test pass while the claim
    broke.

    ``(0, total)`` is placed second so the two extremes are always adjacent to the
    oracle and are always present even when the interior is sampled. A bug that read
    ``financial_cost`` alone would survive at ``(total, 0)`` and die at ``(0, total)``.
    """
    if total < 0:
        raise ValueError(f"a blended action_cost cannot be negative, got {total}")

    if total == 0:
        return ((Minor(0), Minor(0)),)

    if total <= exhaustive_limit:
        interior = list(range(total - 1, 0, -1))
    else:
        drawn = draw(
            st.lists(
                st.integers(min_value=1, max_value=total - 1),
                min_size=min(sample_size, total - 1),
                max_size=min(sample_size, total - 1),
                unique=True,
            )
        )
        # The midpoint is included unconditionally: an implementation that happened to
        # be correct at every drawn point but wrong on an even split would be a strange
        # bug, and it costs one more selection to rule out.
        drawn.append(total // 2)
        interior = sorted(set(drawn) - {0, total})

    ordered = [total, 0, *interior]
    return tuple(
        (Minor(financial), Minor(total - financial)) for financial in ordered
    )

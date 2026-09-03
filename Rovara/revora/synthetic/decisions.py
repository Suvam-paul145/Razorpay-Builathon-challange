"""What the real optimizer does when handed a *correct* estimate of a synthetic world.

The measurement harness next door answers "does Revora recover an effect we planted". This module
answers a different and equally load-bearing question: **given a correct estimate of this world,
does the optimizer decide the right thing?** Two of the four mandatory scenarios are defined by
their required decision rather than by their measured lift:

* the **negative** scenario — acting reduces recovery, so the optimizer must select a null action
  on every case;
* the **high-baseline** scenario — the customer was going to pay anyway, so the optimizer must
  select a null action *and* say ``HIGH_BASELINE_NO_INTERVENTION``. A different sentence about a
  different fact, and conflating the two would tell a merchant "nothing was worth doing" when the
  truth is "this customer needed no persuading".

**This module found a real bug on its first run, which is the argument for having it.** The
negative scenario selected ``DO_NOTHING`` on every case — correct — and recorded the reason
``HIGHEST_NET_VALUE``, which told a merchant that doing nothing *won a comparison* when the truth
was that nothing was in contention. The cause was in :func:`revora.optimizer.selection.select`: the
ranking pool was "everything not excluded", the two null actions are deliberately never excluded,
so the pool was never empty and ``NO_POSITIVE_VALUE`` was unreachable code. R7.C4 defines the pool
as the candidates clearing *both* configured floors, which is now what
:func:`revora.optimizer.selection._qualifies` asks. No unit test caught it because the property
covering it was written as a conditional — "if the reason is ``NO_POSITIVE_VALUE`` then ..." — which
a system that never produces the reason satisfies trivially.

**The ground truth is used as an input here, and that is the opposite of how the measurement
harness uses it.** Stated plainly because it is the thing that could look like cheating: the
comparison reporter reads ground truth to *grade* a measurement, and letting it near the decision
path would make the result circular. This module reads it to *feed* the decision, which makes the
claim conditional and narrower — "if estimation were perfect, the optimizer would do the right
thing". That is a real property and it is separable from estimator quality on purpose. If instead
the optimizer were fed Revora's own priors, a failure would be ambiguous between a bad prior and a
bad decision rule, and the decision rule is what these two scenarios exist to pin down.

**Everything below the ground truth is the real thing**: the real eligibility table, the real cost
priors, the real ``Thresholds`` read from configuration, and the real
:func:`revora.optimizer.selection.select`. Nothing is reimplemented, so a change to the selection
rule shows up here rather than passing because a copy of the old rule still agreed with it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from revora.domain.actions import (
    UNAVAILABLE_IN_MVP,
    CandidateAction,
    candidate_set_for,
)
from revora.domain.enums import ActionAvailability, ExclusionReason, SelectionReason
from revora.domain.probability import Probability
from revora.estimation.candidates import cost_prior_for
from revora.optimizer.arithmetic import CandidateInput
from revora.optimizer.selection import SelectionResult, Thresholds, select
from revora.optimizer.service import thresholds_from_config
from revora.synthetic.generator import SCALE, GeneratedCase, GeneratedDataset

if TYPE_CHECKING:  # pragma: no cover - typing only
    from revora.platform.config import Configuration

__all__ = [
    "DecisionTally",
    "decide",
    "tally_decisions",
    "thresholds_for",
    "true_probability",
]

_PLACES = Decimal("0.0001")


def true_probability(millionths: int) -> Probability:
    """An integer probability in millionths as the four-place ``Probability`` the optimizer takes.

    Half-up to four places, matching every other probability in the system. The generator carries
    six digits of resolution so that a uniform draw comparison is exact; the optimizer's contract
    is four, so the conversion is lossy in the last two digits and deliberately so — feeding the
    optimizer more precision than its own columns can store would test a system that does not
    exist.
    """
    return Probability(
        (Decimal(millionths) / Decimal(SCALE)).quantize(_PLACES, rounding=ROUND_HALF_UP)
    )


def thresholds_for(config: Configuration) -> Thresholds:
    """The optimizer's four configured bounds.

    Delegates to :func:`revora.optimizer.service.thresholds_from_config` rather than rebuilding
    the struct, so a bound added to the optimizer cannot be silently left at a default here.
    """
    return thresholds_from_config(config)


def decide(case: GeneratedCase, *, config: Configuration) -> SelectionResult:
    """Run the real optimizer over one generated case, using that case's true probabilities.

    The candidate set is the real one for the case's cause, so the null actions are always present
    and an action the cause does not permit is never offered. The three MVP-unavailable actions are
    marked ``UNAVAILABLE`` with the reason the estimation layer would have given, because R6.C9
    keeps them in the recorded set — an action excluded for being unperformable is different
    evidence from an action excluded for being a bad idea.
    """
    baseline = true_probability(case.p_natural)
    candidates: list[CandidateInput] = []

    for action in sorted(candidate_set_for(case.cause), key=lambda item: item.value):
        costs = cost_prior_for(action, config)
        unavailable = action in UNAVAILABLE_IN_MVP
        candidates.append(
            CandidateInput(
                action=action,
                # DO_NOTHING's probability *is* the baseline, definitionally, which the real
                # estimation layer also guarantees. Taking the treated probability here would
                # give the null action a spurious non-zero increment from rounding alone.
                intervention_probability=(
                    baseline
                    if action is CandidateAction.DO_NOTHING
                    else true_probability(case.p_treated.get(action, case.p_natural))
                ),
                # All four cost terms pass through untouched, and they come from the same
                # accessor the estimator uses — so the two R31.C11 configured rows apply
                # here too. Nothing here blends, splits or re-derives a term, so a scenario
                # prices an action exactly as the real estimation layer would, including
                # under a merchant's changed cost row.
                financial_cost=costs.financial_cost,
                communication_cost=costs.communication_cost,
                risk_cost=costs.risk_cost,
                customer_cost=costs.customer_cost,
                availability=(
                    ActionAvailability.UNAVAILABLE
                    if unavailable
                    else ActionAvailability.AVAILABLE
                ),
                unavailable_reason=(
                    ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED.value
                    if unavailable
                    else None
                ),
            )
        )

    return select(
        tuple(candidates),
        baseline=baseline,
        amount=case.amount,
        thresholds=thresholds_for(config),
    )


@dataclass(frozen=True, slots=True)
class DecisionTally:
    """How a whole scenario's cases were decided, counted.

    Counts rather than a pass/fail, because "the optimizer never acted" and "the optimizer acted
    on four cases out of six hundred" are different findings and a boolean would render them the
    same. A test asserting the second is zero can say so; a test asserting it is small can say
    that instead.
    """

    by_action: Counter[str]
    by_reason: Counter[str]
    case_count: int

    @property
    def acted_count(self) -> int:
        """Cases where the optimizer chose something other than a null action."""
        return self.case_count - (
            self.by_action[CandidateAction.DO_NOTHING.value]
            + self.by_action[CandidateAction.WAIT.value]
        )

    def reason_share(self, reason: SelectionReason) -> int:
        return self.by_reason[reason.value]

    def as_document(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "selected_actions": dict(sorted(self.by_action.items())),
            "selection_reasons": dict(sorted(self.by_reason.items())),
            "acted_count": self.acted_count,
        }


def tally_decisions(dataset: GeneratedDataset, *, config: Configuration) -> DecisionTally:
    """Decide every case in a dataset and count the outcomes. Pure; touches no database."""
    by_action: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    for case in dataset.cases:
        result = decide(case, config=config)
        by_action[result.selected.action.value] += 1
        by_reason[result.selection_reason.value] += 1
    return DecisionTally(
        by_action=by_action, by_reason=by_reason, case_count=dataset.case_count
    )

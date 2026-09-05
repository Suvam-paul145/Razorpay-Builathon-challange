"""The experiment engine: the only thing that can license a causal claim.

Revora's central claim is that it recovers *incremental* revenue — money that would not have
arrived otherwise. Nothing about observing recoveries can establish that. A recovery rate of
thirty percent is consistent with Revora causing all of it, none of it, or anything between,
because the counterfactual is unobservable for any individual case. Only a controlled comparison
gets at it, and this package is that comparison.

Four ideas, each load-bearing:

**Assignment before knowledge.** The arm is computed from ``HMAC-SHA256(experiment_id, case_id)``
and written in the transaction that created the case, before diagnosis runs. Deterministic and
stateless, so a retry computes the same arm and two workers cannot disagree. An arm chosen after
the cause is known would measure which cases we sorted, not what we did to them.

**A control arm that records rather than abstains.** Control cases run the whole pipeline and
produce a real recommendation, which is then withheld. That gives a counterfactual record: for
every control case we know what Revora wanted to do and what happened without it. Skipping the
pipeline would be cheaper and would throw away the only thing that makes the arm informative.

**A sample size fixed before any case arrives.** Computed from the assumed baseline, the smallest
effect worth detecting, the significance level and the power — at definition time, stored, and
``NOT NULL``. Computed afterwards it stops being a threshold and becomes a description of
whatever data turned up.

**A gate that refuses by default.** An attributed claim needs a completed experiment, both arms
at their required size, a lift interval entirely above zero, and no disqualifying label. Miss any
one and the answer is ``NOT_ESTABLISHED`` with no numeric value — not zero, because "we did not
measure this" and "we measured nothing" are different statements and only one of them is usually
true.

The four-way comparison exists because a lift alone answers the wrong question. Net recovered
revenue, intervention rate, messages per case and blocked cases are reported together, so a lift
bought with three times the customer contact is visibly not the same result as the same lift
bought with none.
"""

from __future__ import annotations

from revora.domain.attribution import AttributionRefusal, attribution_refusals
from revora.experiment.analysis import (
    ANALYSIS_METHOD,
    ArmFigures,
    ExperimentAnalysis,
    analyse_experiment,
    refusals_for,
)
from revora.experiment.assignment import (
    AllocationRatio,
    assign_group,
    parse_allocation_ratio,
)
from revora.experiment.control import (
    AssignmentOutcome,
    assign_case,
    mark_contaminated,
)
from revora.experiment.design import (
    ExperimentDefinition,
    FreezeDrift,
    activate_experiment,
    define_experiment,
    detect_freeze_drift,
    invalidate_experiment,
)
from revora.experiment.statistics import (
    SampleSizeInputs,
    lift_interval,
    normal_cdf,
    normal_quantile,
    required_sample_size_per_group,
)

__all__ = [
    "ANALYSIS_METHOD",
    "AllocationRatio",
    "ArmFigures",
    "AssignmentOutcome",
    "AttributionRefusal",
    "ExperimentAnalysis",
    "ExperimentDefinition",
    "FreezeDrift",
    "SampleSizeInputs",
    "activate_experiment",
    "analyse_experiment",
    "assign_case",
    "assign_group",
    "attribution_refusals",
    "define_experiment",
    "detect_freeze_drift",
    "invalidate_experiment",
    "lift_interval",
    "mark_contaminated",
    "normal_cdf",
    "normal_quantile",
    "parse_allocation_ratio",
    "refusals_for",
    "required_sample_size_per_group",
]

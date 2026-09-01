"""Synthetic data with embedded ground truth, for checking Revora's *measurement*.

**What this establishes, and what it does not.** It establishes that Revora's measurement machinery
recovers an effect we planted and correctly refuses to claim one we did not. It establishes nothing
about real recovery rates or real uplift, because the ground truth lives in a table we wrote. A
claim of the form "Revora recovers X% more revenue" derived from synthetic data would be circular.
The demo narrative this supports is "here is a system whose measurement you can trust", never "here
is a system that recovers X%".

Two structural guarantees make that more than a promise:

**Every synthetic experiment carries the ``SYNTHETIC`` label**, which is one of the three blocking
labels in the attribution gate. So a synthetic run can prove the measurement works while remaining
*incapable* of reporting synthetic money as recovered revenue. The circularity is closed by the
gate, not by remembering not to quote the number.

**The ground truth is read in exactly one function** —
:func:`~revora.synthetic.harness.compare_to_truth`, at the end of a run. Nothing that decides
anything can reach it, and the import contracts enforce that rather than trusting it.

The four mandatory scenarios, in order of what they catch:

* **null** — true uplift zero for every action. Revora must report an interval containing zero and
  refuse attribution. A measurement system that reports a lift here is broken, and this is the only
  way to find that out. It gates CI.
* **negative** — acting makes things worse. The measured lift must be negative and no attributed
  claim may be permitted.
* **high baseline** — customers who were going to pay anyway. Acting costs money for almost no gain.
* **positive** — a known lift, which the measurement should land close to.

Plus an interval-coverage check across many seeds: a 95 percent interval should contain the true
lift about 95 percent of the time. Materially less means the interval is too narrow and every
causal claim built on it is overconfident.

:mod:`~revora.synthetic.decisions` answers the other half of the question the scenarios pose. The
harness asks whether the measurement recovers a planted effect; ``decisions`` asks whether the real
optimizer, handed a *correct* estimate of the same world, declines to act where acting would hurt or
where the customer was going to pay anyway. The ground truth is an input there rather than an answer
sheet, which is what keeps the two uses of it distinguishable.

No floats and no numpy. Every probability is an integer count of millionths and every draw is an
integer compared against one, so a case is never assigned to the wrong side of its own truth by a
rounding error. Contacts use reserved documentation ranges that cannot reach a real person.
"""

from __future__ import annotations

from revora.synthetic.decisions import DecisionTally, decide, tally_decisions
from revora.synthetic.generator import (
    GENERATOR_VERSION,
    SCALE,
    SCENARIOS,
    GeneratedCase,
    GeneratedDataset,
    GroundTruth,
    ScenarioName,
    ScenarioSpec,
    generate,
    scenario,
    true_average_lift,
)
from revora.synthetic.harness import (
    TREATED_ACTION,
    ComparisonReport,
    HarnessResult,
    compare_to_truth,
    persist_run,
    realize_outcomes,
    run_scenario,
    seed_cases,
)

__all__ = [
    "GENERATOR_VERSION",
    "SCALE",
    "SCENARIOS",
    "TREATED_ACTION",
    "ComparisonReport",
    "DecisionTally",
    "GeneratedCase",
    "GeneratedDataset",
    "GroundTruth",
    "HarnessResult",
    "ScenarioName",
    "ScenarioSpec",
    "compare_to_truth",
    "decide",
    "generate",
    "persist_run",
    "realize_outcomes",
    "run_scenario",
    "scenario",
    "seed_cases",
    "tally_decisions",
    "true_average_lift",
]

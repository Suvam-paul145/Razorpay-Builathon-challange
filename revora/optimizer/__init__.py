"""The value optimizer: the arithmetic that is the product.

Three modules, split by what can be wrong in them:

* :mod:`revora.optimizer.arithmetic` — the three-step chain, in integer minor units.
  Pure, zero dependencies beyond ``domain``, rounding applied exactly once.
* :mod:`revora.optimizer.selection` — exclusion, ranking and selection. Also pure.
  Ranking reads ``net_recovery_value`` and nothing else: never probability magnitude,
  never an AI-produced field.
* :mod:`revora.optimizer.service` — persistence of the recommendation and every rejected
  alternative, with its figures, its exclusion reason and its rank.

The first two are pure on purpose. They hold the code where a rounding error becomes a
false revenue claim, and purity is what lets Properties 14 through 19 exercise them at
500 examples each in microseconds with no fixtures.

This package cannot import ``revora.reasoning``. The import contract enforces it, and
``CandidateInput`` has no field an AI-produced value could occupy — so "AI cannot
influence the ranking" is a fact about the type signatures rather than a rule someone has
to remember. That is Property 2's structural half.
"""

from __future__ import annotations

from revora.optimizer.arithmetic import (
    CandidateInput,
    EvaluatedCandidate,
    evaluate_candidate,
)
from revora.optimizer.selection import SelectionResult, Thresholds, select
from revora.optimizer.service import (
    RecommendationOutcome,
    run_optimizer,
    thresholds_from_config,
)

__all__ = [
    "CandidateInput",
    "EvaluatedCandidate",
    "RecommendationOutcome",
    "SelectionResult",
    "Thresholds",
    "evaluate_candidate",
    "run_optimizer",
    "select",
    "thresholds_from_config",
]

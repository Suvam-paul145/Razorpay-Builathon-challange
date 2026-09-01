"""Recovery memory: what Revora has learned, and what it is honest about not knowing.

This package writes the table the baseline estimator learns from. Until it ran, that table was
empty and every baseline in the system was the uniform prior — a 0.500 probability with a
`[0.025, 0.975]` interval. That was not a bug: a system with no history genuinely does not know
its own recovery rate, and a wide interval is what saying so looks like.

Two ideas do most of the work here.

**One observation per case, written inside the terminal transition.** A case ending and an
observation recording that ending are one fact. Split across two transactions they become two,
and a crash between them loses the observation permanently — nothing revisits a terminal case.
The training set would then be biased toward whatever survives crashes, invisibly. Sharing the
transaction makes that impossible rather than unlikely.

**The label matters more than the outcome.** Only ``NO_INTERVENTION_CONFIRMED`` observations may
train the baseline, and earning that label needs a control-arm assignment *and* zero confirmed
actions. A treatment case that happened to receive no action — policy blocked it, the window
closed — is not evidence about what happens without intervention; it is evidence about cases
Revora declined to treat, which is a selected population. Counting those would bias the
baseline in the direction that flatters every incremental claim built on it.

And one limit stated rather than solved: ``NO_INTERVENTION_CONFIRMED`` means no *Revora* action
and no *recorded* merchant action. Revora cannot see a merchant phoning a customer. So the
label is weaker than "nobody intervened", the unknown share per segment is reported, and
nothing here pretends otherwise.

Model versions live here too, and the rule is that activation is a decision a person takes:
recording a trained version produces an ``INACTIVE`` row, and promotion requires a named
approving user the schema will not let be null.
"""

from __future__ import annotations

from revora.memory.store import (
    ObservationOutcome,
    classify_intervention_status,
    observation_writer,
    record_observation,
)
from revora.memory.versions import (
    FROZEN_COMPONENTS,
    ModelState,
    PromotionRefused,
    PromotionResult,
    promote_model_version,
    record_model_version,
)

__all__ = [
    "FROZEN_COMPONENTS",
    "ModelState",
    "ObservationOutcome",
    "PromotionRefused",
    "PromotionResult",
    "classify_intervention_status",
    "observation_writer",
    "promote_model_version",
    "record_model_version",
    "record_observation",
]

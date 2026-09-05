"""The attribution gate: the rule that decides whether Revora may claim it caused a recovery.

This is the most consequential rule in the system, and it lives in the domain for two reasons.

**Two packages need it and neither may reach the other.** ``revora.experiment`` applies it when it
analyses a comparison; ``revora.metrics`` applies it when deciding whether ``incremental_recovered_
revenue`` has a number or the ``NOT_ESTABLISHED`` sentinel. They are siblings in the layering
contract. A copy in each would be two implementations of the one rule that must never disagree —
and the way they would disagree is that one of them would be more permissive, which is the
direction that produces a claim nobody earned.

**It has no dependencies worth having.** The gate reads four things: an experiment's state, its
required sample size, its labels, and the arms' case counts and lift interval. All primitives. So
it takes primitives rather than an ORM row, which makes it exhaustively testable without a
database and impossible to accidentally couple to a schema.

**The gate is a conjunction and refuses by default** (R13.C8). An attributed claim needs all of:

* the experiment ``COMPLETED`` — an interim look is not a result;
* both arms at or above the sample size fixed at definition time;
* a lift interval that excludes zero **and lies entirely above zero**;
* none of ``UNDERPOWERED``, ``INVALIDATED`` or ``SYNTHETIC``.

Every refusal is returned, not just the first, because the responses differ: "wait for more data",
"your version freeze broke", and "the treatment does not work" call for completely different
actions and all three otherwise render as the same empty cell.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from revora.domain.enums import ExperimentLabel, ExperimentState

__all__ = [
    "BLOCKING_LABELS",
    "AttributionRefusal",
    "RefusalCode",
    "attribution_refusals",
    "label_set",
]

BLOCKING_LABELS: frozenset[str] = frozenset(
    {
        ExperimentLabel.UNDERPOWERED.value,
        ExperimentLabel.INVALIDATED.value,
        ExperimentLabel.SYNTHETIC.value,
    }
)
"""Labels that disqualify a claim on their own, however good the data looks.

``SYNTHETIC`` is the one that matters for a demo: a generated experiment can show a beautiful lift,
and reporting it as recovered revenue would be circular — the lift was put there by the generator.

``CONTAMINATED`` and ``EXPLORATORY`` are deliberately absent. Contamination is handled by
*excluding* the affected cases from the arm counts, so what remains is still a valid comparison and
the excluded count is reported beside it. Treating the label as fatal would discard a usable
experiment; ignoring the exclusion would keep a broken one."""


class RefusalCode:
    """The refusal codes, as constants.

    Not a ``StrEnum`` because two of them are also ``ExperimentLabel`` members —
    ``CAUSALITY_NOT_ESTABLISHED`` is both a reason the gate refused and a label that goes on the
    figure. Defining it twice in two enums would invite the two spellings to drift; referencing the
    label here keeps one string.
    """

    NOT_COMPLETED = "EXPERIMENT_NOT_COMPLETED"
    BELOW_SAMPLE_SIZE = "BELOW_REQUIRED_SAMPLE_SIZE"
    NO_INTERVAL = "NO_LIFT_INTERVAL"
    CONTAINS_ZERO = ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value
    BELOW_ZERO = "LIFT_INTERVAL_BELOW_ZERO"
    DISQUALIFYING_LABEL = "DISQUALIFYING_LABEL"
    NOT_ANALYSED = "EXPERIMENT_NOT_ANALYSED"
    NO_EXPERIMENT = "NO_COMPLETED_EXPERIMENT"


@dataclass(frozen=True, slots=True)
class AttributionRefusal:
    """One reason an attributed claim is not permitted.

    A code and a detail rather than a bare boolean, because "why not" is the actionable part. An
    operator staring at ``NOT_ESTABLISHED`` needs to know whether to wait, to fix something, or to
    accept a negative result.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def label_set(labels: Sequence[str] | None) -> frozenset[str]:
    """Labels as a frozenset, treating ``None`` as empty.

    ``experiment.labels`` is a nullable ``TEXT[]``, so callers see either ``None`` or a list.
    Normalizing here stops ``None`` being iterated somewhere and stops an absent array reading as
    anything other than "no labels".
    """
    return frozenset(labels or ())


def attribution_refusals(
    *,
    state: str,
    required_sample_size_per_group: int,
    labels: Sequence[str] | None,
    control_cases: int,
    treatment_cases: int,
    lift_ci_low: Decimal | None,
    lift_ci_high: Decimal | None,
) -> tuple[AttributionRefusal, ...]:
    """Every reason this result may not support a causal claim. Empty means it may.

    Ordered as an operator would act on them: state first (wait), then power (wait longer), then
    the interval (the actual finding), then labels (something is wrong).

    Args:
        state: the experiment's state as a string, compared against ``COMPLETED``. An unrecognised
            value refuses rather than raising — a state nobody recognises is certainly not
            ``COMPLETED``, and raising here would turn a data oddity into an outage on the metrics
            endpoint.
        lift_ci_low: ``None`` together with ``lift_ci_high`` when an arm was empty and the
            difference of rates does not exist. Not zero, and the distinction is the point.
    """
    refusals: list[AttributionRefusal] = []

    if state != ExperimentState.COMPLETED.value:
        refusals.append(
            AttributionRefusal(
                RefusalCode.NOT_COMPLETED,
                f"state is {state}; an interim look is not a result",
            )
        )

    if (
        control_cases < required_sample_size_per_group
        or treatment_cases < required_sample_size_per_group
    ):
        refusals.append(
            AttributionRefusal(
                RefusalCode.BELOW_SAMPLE_SIZE,
                f"control {control_cases}, treatment {treatment_cases}, "
                f"required {required_sample_size_per_group} per arm",
            )
        )

    if lift_ci_low is None or lift_ci_high is None:
        refusals.append(
            AttributionRefusal(
                RefusalCode.NO_INTERVAL,
                "an arm has no cases, so the difference of rates does not exist",
            )
        )
    elif lift_ci_low <= 0 <= lift_ci_high:
        # Inclusive on both sides deliberately. An interval whose bound is exactly zero *contains*
        # zero, and a strict comparison would let [0.0000, 0.2000] through — claiming an effect
        # from data consistent with none.
        refusals.append(
            AttributionRefusal(
                RefusalCode.CONTAINS_ZERO,
                f"interval [{lift_ci_low}, {lift_ci_high}] contains zero",
            )
        )
    elif lift_ci_high < 0:
        # Excludes zero, on the wrong side. A real finding — the treatment did harm — but not an
        # attribution. Reporting it as one would let anything taking an absolute value present an
        # incremental loss as a gain.
        refusals.append(
            AttributionRefusal(
                RefusalCode.BELOW_ZERO,
                f"interval [{lift_ci_low}, {lift_ci_high}] lies entirely below zero; "
                "the treatment reduced recovery",
            )
        )

    present = sorted(BLOCKING_LABELS.intersection(label_set(labels)))
    if present:
        refusals.append(
            AttributionRefusal(
                RefusalCode.DISQUALIFYING_LABEL, f"experiment carries {present}"
            )
        )

    return tuple(refusals)

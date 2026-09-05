"""Analysing an experiment: the lift, its interval, the four-way cost comparison, and the gate.

This module decides whether Revora is allowed to say it caused anything. That is the most
consequential thing in the codebase, so it is written to refuse by default.

**The gate is a conjunction and every term is necessary** (R13.C8). An attributed claim needs
all of:

* the experiment ``COMPLETED`` — an interim look on a running experiment is not a result;
* both arms at or above ``required_sample_size_per_group`` — the threshold computed at
  definition time, not a count observed afterwards;
* a lift interval that **excludes zero and lies entirely above zero** — excluding zero on the
  wrong side means the treatment did harm, which is a finding but not an attribution;
* none of ``UNDERPOWERED``, ``INVALIDATED`` or ``SYNTHETIC``.

Miss any one and the answer is ``NOT_ESTABLISHED`` with no numeric value. Not zero.
``NOT_ESTABLISHED`` and zero are different claims: one says we did not measure this, the other
says we measured it and there was nothing. Reporting the second when the first is true is the
specific dishonesty this whole phase exists to prevent, and it is the easy mistake because a
zero renders nicely in a dashboard cell.

**The four-way comparison exists because a lift alone is not an answer** (R13.C13). The question
is not "did recovery go up" but "did recovery go up without added customer or operational cost".
So net recovered revenue, intervention rate, customer messages per case and blocked case count
are all reported per arm with the direction and size of treatment minus control. A lift bought
with three times the messages is a different result from the same lift bought with the same
messages, and the report says which one happened.

**Secondary metrics are labelled ``EXPLORATORY`` and never support attribution** (R13.C11). A
secondary metric that reaches significance is the classic multiple-comparisons artefact: run
enough of them and one will.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from revora.audit.events import EXPERIMENT_ANALYSED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.attribution import (
    AttributionRefusal,
    RefusalCode,
    attribution_refusals,
)
from revora.domain.enums import (
    NOT_ESTABLISHED,
    CaseState,
    ExperimentGroup,
    ExperimentLabel,
    IntentState,
)
from revora.experiment.statistics import lift_interval
from revora.persistence.models import (
    ExecutionIntent,
    MemoryObservation,
    RecoveryCase,
    RecoveryOutcome,
)
from revora.persistence.repositories.experiments import (
    ExperimentAssignmentRepository,
    ExperimentRepository,
    ExperimentResultRepository,
)
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from revora.persistence.models import Experiment
    from revora.platform.config import Configuration

__all__ = [
    "ANALYSIS_METHOD",
    "ArmFigures",
    "ExperimentAnalysis",
    "analyse_experiment",
    "refusals_for",
]

_logger = get_logger(__name__)

_ACTOR = "experiment_engine"

ANALYSIS_METHOD = "two_proportion_normal_approximation"
"""Recorded on every result, because the method is part of the claim.

Two results computed by different methods are not comparable, and a reader who cannot tell which
was used cannot tell whether a difference between two reports is a change in the data or a
change in the arithmetic."""

_RATE_PLACES = Decimal("0.0001")
"""Four places, matching the ``NUMERIC(6,4)`` columns the arm rates are stored in."""


@dataclass(frozen=True, slots=True)
class ArmFigures:
    """One arm's figures. Every count is an integer and every money figure is minor units.

    ``rate`` is ``None`` on an empty arm rather than zero. An arm with no cases has no recovery
    rate; reporting ``0.0000`` would make an empty arm look like a total failure to recover,
    which is a measurement nobody made.
    """

    cases: int
    recoveries: int
    recovered_revenue: int
    total_cost: int
    intervened_cases: int
    customer_messages: int
    blocked_cases: int

    @property
    def rate(self) -> Decimal | None:
        """Recovery rate, or ``None`` where the denominator is zero."""
        if self.cases <= 0:
            return None
        return (Decimal(self.recoveries) / Decimal(self.cases)).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP
        )

    @property
    def net_recovered_revenue(self) -> int:
        """Recovered revenue less the cost of recovering it, in minor units.

        Integer subtraction of two integers. This is the figure the four-way comparison leads
        with, because a lift that cost more than it recovered is not a win and a gross-revenue
        comparison would hide that.
        """
        return self.recovered_revenue - self.total_cost

    @property
    def intervention_rate(self) -> Decimal | None:
        if self.cases <= 0:
            return None
        return (Decimal(self.intervened_cases) / Decimal(self.cases)).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP
        )

    @property
    def messages_per_case(self) -> Decimal | None:
        """Customer messages per case — the cost the merchant's customers actually feel.

        In the four-way comparison because it is the one cost that does not appear in any money
        figure. Three times the messages for the same recovery is a worse outcome even though it
        is free.
        """
        if self.cases <= 0:
            return None
        return (Decimal(self.customer_messages) / Decimal(self.cases)).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP
        )

    def as_document(self) -> dict[str, object]:
        """The arm's figures as JSON-safe values, for the stored comparison."""
        return {
            "cases": self.cases,
            "recoveries": self.recoveries,
            "recovery_rate": None if self.rate is None else str(self.rate),
            "recovered_revenue": self.recovered_revenue,
            "total_cost": self.total_cost,
            "net_recovered_revenue": self.net_recovered_revenue,
            "intervened_cases": self.intervened_cases,
            "intervention_rate": (
                None if self.intervention_rate is None else str(self.intervention_rate)
            ),
            "customer_messages": self.customer_messages,
            "messages_per_case": (
                None if self.messages_per_case is None else str(self.messages_per_case)
            ),
            "blocked_cases": self.blocked_cases,
        }


@dataclass(frozen=True, slots=True)
class ExperimentAnalysis:
    """The full result of one analysis."""

    experiment_id: uuid.UUID
    result_id: uuid.UUID | None
    primary_metric: str
    control: ArmFigures
    treatment: ArmFigures
    lift: Decimal | None
    lift_ci_low: Decimal | None
    lift_ci_high: Decimal | None
    contaminated_count: int
    excluded_count: int
    labels: tuple[str, ...]
    refusals: tuple[AttributionRefusal, ...] = field(default_factory=tuple)

    @property
    def attribution_permitted(self) -> bool:
        """Whether this result may support an attributed recovery claim.

        True only when every gate term passes, which is the same as saying there are no
        refusals. Derived rather than stored so it cannot disagree with the reasons.
        """
        return not self.refusals

    @property
    def incremental_recovered_revenue(self) -> int | str:
        """The incremental figure, or the ``NOT_ESTABLISHED`` sentinel.

        Returns the string sentinel, not ``None`` and not zero, and the type union is
        deliberately awkward: a caller has to look at what it got. ``None`` would be silently
        formattable as an empty cell and zero would be silently formattable as a number, and both
        would let the most important caveat in the system disappear into a dashboard.

        When attribution *is* permitted, the figure is the treatment arm's net recovered revenue
        minus what the control arm's rate says would have been recovered anyway — an integer
        count of minor units throughout.
        """
        if not self.attribution_permitted:
            return NOT_ESTABLISHED
        control_rate = self.control.rate
        if control_rate is None:  # pragma: no cover - gate requires a non-empty control arm
            return NOT_ESTABLISHED
        # What the treatment arm would have recovered at the control arm's rate, in minor units.
        # Integer arithmetic: multiply then divide, so no intermediate is a fraction of a paisa.
        counterfactual_recoveries = (
            Decimal(self.treatment.cases) * control_rate
        ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if self.treatment.recoveries <= 0:  # pragma: no cover - gate requires a positive lift
            return NOT_ESTABLISHED
        per_recovery = self.treatment.recovered_revenue // self.treatment.recoveries
        counterfactual_revenue = int(counterfactual_recoveries) * per_recovery
        return self.treatment.recovered_revenue - counterfactual_revenue - self.treatment.total_cost

    def comparison_document(self) -> dict[str, object]:
        """The four-way comparison, per arm and as a difference (R13.C13)."""
        return {
            "control": self.control.as_document(),
            "treatment": self.treatment.as_document(),
            "difference": {
                "net_recovered_revenue": (
                    self.treatment.net_recovered_revenue - self.control.net_recovered_revenue
                ),
                "intervention_rate": _difference(
                    self.treatment.intervention_rate, self.control.intervention_rate
                ),
                "messages_per_case": _difference(
                    self.treatment.messages_per_case, self.control.messages_per_case
                ),
                "blocked_cases": self.treatment.blocked_cases - self.control.blocked_cases,
            },
            "interpretation_note": (
                "A lift in recovery bought with more customer messages is a different result "
                "from the same lift bought with the same messages. Read the difference block "
                "together, not one figure from it."
            ),
        }


def _difference(treatment: Decimal | None, control: Decimal | None) -> str | None:
    """Treatment minus control, or ``None`` if either side is undefined.

    ``None`` propagates rather than being treated as zero. A difference against an arm that has
    no rate is not a difference of zero, and rendering it as one would claim the arms matched.
    """
    if treatment is None or control is None:
        return None
    return str(treatment - control)


def refusals_for(
    experiment: Experiment,
    *,
    control: ArmFigures,
    treatment: ArmFigures,
    lift_ci_low: Decimal | None,
    lift_ci_high: Decimal | None,
) -> tuple[AttributionRefusal, ...]:
    """Apply the domain's attribution gate to an experiment row and its two arms.

    A thin adapter, and thin on purpose. The rule itself lives in
    :func:`revora.domain.attribution.attribution_refusals` because the metrics engine needs the
    same rule and cannot import this package — they are siblings in the layering contract. Two
    copies of the gate would be two chances for one of them to be more permissive, which is the
    direction that produces a claim nobody earned.

    All this function does is pull the four primitives the rule reads off the ORM row.
    """
    return attribution_refusals(
        state=str(experiment.state),
        required_sample_size_per_group=int(experiment.required_sample_size_per_group),
        labels=experiment.labels,
        control_cases=control.cases,
        treatment_cases=treatment.cases,
        lift_ci_low=lift_ci_low,
        lift_ci_high=lift_ci_high,
    )


def analyse_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
) -> ExperimentAnalysis | None:
    """Compute and persist one analysis of an experiment.

    Must be called inside a transaction; commits nothing.

    Returns ``None`` only if the experiment does not exist. Every other path produces a result
    row — including the ones that establish nothing. Writing only the analyses that found
    something would make the history unauditable, and an interval containing zero is both a real
    finding and the most likely one.
    """
    experiments = ExperimentRepository(session)
    experiment = experiments.get(merchant_id, experiment_id)
    if experiment is None:
        return None

    assignments = ExperimentAssignmentRepository(session)
    counts = assignments.arm_counts(merchant_id, experiment_id)

    control = _arm_figures(
        session,
        merchant_id,
        assignments.case_ids_in_arm(merchant_id, experiment_id, ExperimentGroup.CONTROL),
    )
    treatment = _arm_figures(
        session,
        merchant_id,
        assignments.case_ids_in_arm(merchant_id, experiment_id, ExperimentGroup.TREATMENT),
    )

    interval = lift_interval(
        control_recoveries=control.recoveries,
        control_cases=control.cases,
        treatment_recoveries=treatment.recoveries,
        treatment_cases=treatment.cases,
        confidence_level=config.EXPERIMENT_CONFIDENCE_LEVEL,
    )
    lift, low, high = interval if interval is not None else (None, None, None)

    refusals = refusals_for(
        experiment,
        control=control,
        treatment=treatment,
        lift_ci_low=low,
        lift_ci_high=high,
    )

    # Result labels are the analysis's own, distinct from the experiment's. An experiment can be
    # soundly designed while a particular analysis of it establishes nothing.
    codes = {refusal.code for refusal in refusals}
    result_labels = sorted(
        (codes & {RefusalCode.CONTAINS_ZERO})
        | (
            {ExperimentLabel.UNDERPOWERED.value}
            if RefusalCode.BELOW_SAMPLE_SIZE in codes
            else set()
        )
    )

    analysis = ExperimentAnalysis(
        experiment_id=experiment_id,
        result_id=None,
        primary_metric=str(experiment.primary_metric),
        control=control,
        treatment=treatment,
        lift=lift,
        lift_ci_low=low,
        lift_ci_high=high,
        contaminated_count=counts.contaminated,
        excluded_count=counts.excluded,
        labels=tuple(result_labels),
        refusals=refusals,
    )

    moment = now()
    row = ExperimentResultRepository(session).insert(
        merchant_id,
        values={
            "experiment_id": experiment_id,
            "primary_metric": analysis.primary_metric,
            "analysis_method": ANALYSIS_METHOD,
            "control_case_count": control.cases,
            "treatment_case_count": treatment.cases,
            "control_recoveries": control.recoveries,
            "treatment_recoveries": treatment.recoveries,
            "control_rate": control.rate,
            "treatment_rate": treatment.rate,
            "lift": lift,
            "lift_ci_low": low,
            "lift_ci_high": high,
            "contaminated_count": counts.contaminated,
            "excluded_count": counts.excluded,
            "labels": result_labels or None,
            "comparison": analysis.comparison_document(),
            "computed_at": moment,
        },
    )

    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_unattached(
        merchant_id,
        AuditEntry(
            event_type=EXPERIMENT_ANALYSED,
            actor=_ACTOR,
            decision={
                "experiment_id": str(experiment_id),
                "result_id": str(row.id),
                "analysis_method": ANALYSIS_METHOD,
                "control_cases": control.cases,
                "treatment_cases": treatment.cases,
                "lift": None if lift is None else str(lift),
                "lift_interval": None if low is None else f"[{low}, {high}]",
                "attribution_permitted": analysis.attribution_permitted,
                "refusals": [str(refusal) for refusal in refusals],
                "contaminated_count": counts.contaminated,
                "excluded_count": counts.excluded,
            },
        ),
        correlation_id=correlation_id,
    )

    _logger.info(
        "experiment analysed",
        merchant_id=str(merchant_id),
        experiment_id=str(experiment_id),
        attribution_permitted=analysis.attribution_permitted,
        refusals=[refusal.code for refusal in refusals],
    )

    return ExperimentAnalysis(
        experiment_id=experiment_id,
        result_id=row.id,
        primary_metric=analysis.primary_metric,
        control=control,
        treatment=treatment,
        lift=lift,
        lift_ci_low=low,
        lift_ci_high=high,
        contaminated_count=counts.contaminated,
        excluded_count=counts.excluded,
        labels=tuple(result_labels),
        refusals=refusals,
    )


def _arm_figures(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> ArmFigures:
    """Aggregate one arm's cases into the figures the comparison needs.

    Every figure comes from a persisted row. Recoveries come from ``recovery_outcome``, which
    exists only where an authoritative provider read verified a capture — so an arm's recovery
    count cannot be inflated by a webhook. Costs come from ``memory_observation.realized_cost``,
    written at the terminal transition, because there is no per-action cost column and an
    *estimated* cost summed into a revenue comparison would be a prediction presented as a fact.
    """
    if not case_ids:
        return ArmFigures(0, 0, 0, 0, 0, 0, 0)

    ids = list(case_ids)

    case_row = session.execute(
        select(
            func.count().label("cases"),
            func.coalesce(func.sum(RecoveryCase.customer_message_count), 0).label("messages"),
            func.count()
            .filter(RecoveryCase.state == CaseState.BLOCKED.value)
            .label("blocked"),
        )
        .select_from(RecoveryCase)
        .where(RecoveryCase.merchant_id == merchant_id, RecoveryCase.id.in_(ids))
    ).one()

    outcome_row = session.execute(
        select(
            func.count().label("recoveries"),
            func.coalesce(func.sum(RecoveryOutcome.recovered_amount), 0).label("revenue"),
        )
        .select_from(RecoveryOutcome)
        .where(
            RecoveryOutcome.merchant_id == merchant_id,
            RecoveryOutcome.case_id.in_(ids),
        )
    ).one()

    intervened = session.execute(
        select(func.count(func.distinct(ExecutionIntent.case_id)))
        .select_from(ExecutionIntent)
        .where(
            ExecutionIntent.merchant_id == merchant_id,
            ExecutionIntent.case_id.in_(ids),
            ExecutionIntent.state == IntentState.CONFIRMED.value,
        )
    ).scalar_one()

    cost = session.execute(
        select(func.coalesce(func.sum(MemoryObservation.realized_cost), 0))
        .select_from(MemoryObservation)
        .where(
            MemoryObservation.merchant_id == merchant_id,
            MemoryObservation.case_id.in_(ids),
        )
    ).scalar_one()

    return ArmFigures(
        cases=int(case_row.cases),
        recoveries=int(outcome_row.recoveries),
        recovered_revenue=int(outcome_row.revenue),
        total_cost=int(cost),
        intervened_cases=int(intervened),
        customer_messages=int(case_row.messages),
        blocked_cases=int(case_row.blocked),
    )

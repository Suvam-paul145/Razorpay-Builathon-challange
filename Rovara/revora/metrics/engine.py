"""The metrics engine. Every figure it produces has to survive being questioned.

This module computes the numbers a merchant would put in front of their finance team, so it is
written around four refusals rather than around the arithmetic.

**A rate with a zero denominator is ``UNDEFINED``, never zero** (R12.C5). A reporting period with
no cases has no recovery rate. Reporting ``0`` claims the period recovered nothing, which is a
measurement, and a false one. This matters most exactly when it arises: a new merchant's first
week, where a dashboard of zeroes reads as total failure and a dashboard of ``UNDEFINED`` reads as
"no data yet".

**Incremental revenue is ``NOT_ESTABLISHED`` unless a completed experiment earned it**
(R12.C13). Observed recovery is never presented as incremental. This is the single most important
line in the module: "we recovered ₹X" and "we caused ₹X to be recovered" are different claims, and
only a controlled comparison whose lift interval lies entirely above zero supports the second.
Everything Revora observes without an experiment is consistent with the money having arrived
anyway.

**Observed recovery is labelled when causality is not established** (R12.C9). If observed revenue
is positive while the experiment's interval contains zero, the figure carries
``CAUSALITY_NOT_ESTABLISHED`` on every surface. The number is real; the implication a reader would
draw from it is not.

**Any synthetic contribution labels the whole figure ``SYNTHETIC``** (R12.C11). One synthetic case
in a cohort is enough. A figure derived partly from generated data cannot support a real-world
claim, and the label travels with it into every export.

**All money is integer minor units, all rates are ``Decimal``.** Sums are ``BIGINT`` in the
database and ``int`` here. The only division is a rate, computed in ``Decimal`` at four places, so
a total always equals the sum of its rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from revora.domain.attribution import attribution_refusals
from revora.domain.enums import (
    NOT_ESTABLISHED,
    RECOVERY_GROSS_OF_REFUNDS,
    UNDEFINED,
    CaseState,
    ExperimentGroup,
    ExperimentLabel,
    ExperimentState,
    IntentState,
    OutcomeClass,
    Provenance,
    RiskCause,
)
from revora.domain.money import Minor
from revora.domain.segments import AmountBand, amount_band_for
from revora.persistence.models import (
    Diagnosis,
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

__all__ = [
    "RATE_PLACES",
    "CohortMetrics",
    "IncrementalFinding",
    "ReportingPeriod",
    "SegmentKey",
    "compute_metrics",
    "rate",
]

_logger = get_logger(__name__)

RATE_PLACES = Decimal("0.0001")
"""Four places on every rate, matching the probability discipline elsewhere.

Not more: a rate over a few hundred cases has no information in its fifth decimal, and publishing
one invites a reader to compare two periods on a digit that is noise."""

_HOUR_PLACES = Decimal("0.01")
"""Two places on hours. R12.C5 asks for average time to recovery in hours; hundredths of an hour
is thirty-six seconds, which is finer than the provider's own timestamps justify."""

_RECOVERED_CLASSES = frozenset(
    {OutcomeClass.NATURAL.value, OutcomeClass.OBSERVED.value, OutcomeClass.ATTRIBUTED.value}
)
"""Every classification that means the money arrived. They differ in what causal claim the arrival
supports, not in whether it happened."""


def rate(numerator: int, denominator: int) -> Decimal | str:
    """A rate, or :data:`UNDEFINED` when the denominator is zero.

    The single place the zero-denominator rule is implemented, so it cannot be applied
    inconsistently across nine metrics. Returns ``Decimal | str``, and the awkward union is the
    point — a caller has to look at what it got rather than formatting it blindly.

    Rounds half-up to four places. Nearest rather than outward, unlike an interval bound: a rate is
    a descriptive figure, not a claim about what the data can exclude, so there is no direction in
    which rounding would overstate knowledge.
    """
    if denominator <= 0:
        return UNDEFINED
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATE_PLACES, rounding=ROUND_HALF_UP
    )


@dataclass(frozen=True, slots=True)
class ReportingPeriod:
    """A half-open interval over case detection timestamps.

    Half-open — ``[start, end)`` — because adjacent periods must partition cases exactly. A closed
    interval would count a case detected precisely at midnight in both the day that ended and the
    day that began, so the sum of two months would exceed the quarter.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"reporting period must have positive duration, got {self.start} to {self.end}"
            )

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def as_document(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class SegmentKey:
    """A segmentation key. ``None`` on both fields means the aggregate.

    MVP segments by risk cause and amount band only. The design names four dimensions; the other
    two — selected action and policy outcome — are deliberately omitted rather than half-built,
    because a segmentation nobody reads still costs a query per dimension per period.
    """

    risk_cause: RiskCause | None = None
    amount_band: AmountBand | None = None

    @property
    def is_aggregate(self) -> bool:
        return self.risk_cause is None and self.amount_band is None

    def as_document(self) -> dict[str, str | None]:
        return {
            "risk_cause": None if self.risk_cause is None else self.risk_cause.value,
            "amount_band": None if self.amount_band is None else self.amount_band.value,
        }

    def __str__(self) -> str:
        if self.is_aggregate:
            return "aggregate"
        cause = "*" if self.risk_cause is None else self.risk_cause.value
        band = "*" if self.amount_band is None else self.amount_band.value
        return f"{cause}/{band}"


@dataclass(frozen=True, slots=True)
class IncrementalFinding:
    """What, if anything, may be claimed as incremental — and the evidence for it.

    ``value`` is either an integer of minor units or the :data:`NOT_ESTABLISHED` sentinel. When it
    is the sentinel, ``refusal_codes`` says why, because "wait for more data" and "the treatment
    does not work" are different situations that both render as ``NOT_ESTABLISHED``.

    The experiment id, per-group counts and interval travel with the figure whenever there is one
    (R12.C4). A bare incremental number is unfalsifiable; the same number with the comparison
    behind it can be argued with, which is the point.
    """

    value: int | str
    experiment_id: uuid.UUID | None = None
    control_case_count: int | None = None
    treatment_case_count: int | None = None
    lift: Decimal | None = None
    lift_ci_low: Decimal | None = None
    lift_ci_high: Decimal | None = None
    refusal_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def established(self) -> bool:
        return not isinstance(self.value, str)

    def as_document(self) -> dict[str, object]:
        return {
            "value": self.value,
            "established": self.established,
            "experiment_id": None if self.experiment_id is None else str(self.experiment_id),
            "control_case_count": self.control_case_count,
            "treatment_case_count": self.treatment_case_count,
            "lift": None if self.lift is None else str(self.lift),
            "lift_interval": (
                None
                if self.lift_ci_low is None
                else f"[{self.lift_ci_low}, {self.lift_ci_high}]"
            ),
            "refusal_codes": list(self.refusal_codes),
        }


@dataclass(frozen=True, slots=True)
class CohortMetrics:
    """Every figure for one cohort, with the labels that qualify them.

    Frozen, and every field is either an integer, a ``Decimal``, or a sentinel string. Nothing is
    computed lazily from mutable state, so a figure cannot change between being computed and being
    rendered.
    """

    period: ReportingPeriod
    computed_at: datetime
    segment: SegmentKey

    case_count: int
    revenue_at_risk: int
    recovered_case_count: int
    observed_recovered_revenue: int
    natural_recovered_revenue: int
    total_recovery_cost: int
    unresolved_revenue: int

    blocked_case_count: int
    escalated_case_count: int
    unnecessary_action_count: int
    intervened_case_count: int
    confirmed_action_count: int
    successful_action_count: int
    cycles_without_action_count: int

    recovery_rate: Decimal | str
    intervention_rate: Decimal | str
    action_success_rate: Decimal | str
    escalation_rate: Decimal | str
    average_hours_to_recovery: Decimal | str

    incremental: IncrementalFinding
    labels: tuple[str, ...]

    @property
    def net_recovered_revenue(self) -> int:
        """Observed recovery less the cost of recovering it (R12.C6). Integer arithmetic."""
        return self.observed_recovered_revenue - self.total_recovery_cost

    @property
    def is_synthetic(self) -> bool:
        return ExperimentLabel.SYNTHETIC.value in self.labels

    @property
    def causality_established(self) -> bool:
        return self.incremental.established

    def as_document(self) -> dict[str, object]:
        """Every figure with its provenance metadata attached (R12.C12).

        The three timestamps are not decoration. A figure without its period and computation
        instant cannot be reconciled against a later recomputation — and metrics *do* move, because
        a delayed capture can reconcile a case to ``RECOVERED`` weeks after the period closed.
        """
        return {
            "reporting_period": self.period.as_document(),
            "computed_at": self.computed_at.isoformat(),
            "segment": self.segment.as_document(),
            "case_count": self.case_count,
            "revenue_at_risk": self.revenue_at_risk,
            "recovered_case_count": self.recovered_case_count,
            "observed_recovered_revenue": self.observed_recovered_revenue,
            "natural_recovered_revenue": self.natural_recovered_revenue,
            "incremental_recovered_revenue": self.incremental.as_document(),
            "total_recovery_cost": self.total_recovery_cost,
            "net_recovered_revenue": self.net_recovered_revenue,
            "unresolved_revenue": self.unresolved_revenue,
            "recovery_rate": _render(self.recovery_rate),
            "intervention_rate": _render(self.intervention_rate),
            "action_success_rate": _render(self.action_success_rate),
            "escalation_rate": _render(self.escalation_rate),
            "average_hours_to_recovery": _render(self.average_hours_to_recovery),
            "blocked_case_count": self.blocked_case_count,
            "escalated_case_count": self.escalated_case_count,
            "unnecessary_action_count": self.unnecessary_action_count,
            "cycles_without_action_count": self.cycles_without_action_count,
            "labels": list(self.labels),
        }


def _render(value: Decimal | str) -> str:
    """A rate as a string, so ``UNDEFINED`` and ``0.0000`` are never confusable in JSON.

    Both become strings. If rates were emitted as numbers with ``UNDEFINED`` as a string, a
    consumer doing arithmetic would coerce or crash on the sentinel — and the coercion would
    silently produce zero, which is the exact confusion the sentinel exists to prevent.
    """
    return str(value)


def incremental_finding(session: Session, merchant_id: uuid.UUID) -> IncrementalFinding:
    """The causality gate on its own. Public so a caller can time-bound it separately.

    :func:`compute_metrics` computes this as part of a full report, which is the normal path. The
    dashboard computes it separately because it is the expensive figure — it reads experiment
    results and both arms' outcomes — and R14.C16 requires a figure that could not be produced in
    time to degrade *alone*, with the rest of the report still returning. That is only possible if
    the expensive figure can be asked for by itself.
    """
    return _incremental_finding(session, merchant_id)


def compute_metrics(
    session: Session,
    merchant_id: uuid.UUID,
    period: ReportingPeriod,
    *,
    segment: SegmentKey | None = None,
    moment: datetime | None = None,
    incremental: IncrementalFinding | None = None,
) -> CohortMetrics:
    """Compute every figure for one cohort. Reads only; commits nothing.

    The cohort is every case whose ``detected_at`` falls in ``[period.start, period.end)``
    (R12.C1). Detection time rather than recovery time, so a case belongs to the period in which
    the problem happened — which is the only definition under which "revenue at risk" and
    "recovered revenue" describe the same population.

    Args:
        segment: restrict to a risk cause, an amount band, or both. ``None`` computes the
            aggregate. Segments and the aggregate are computed by the same function so a segment
            can never be defined differently from the total it rolls into.
        incremental: a finding already computed by :func:`incremental_finding`, so a caller that
            time-bounds the expensive figure separately does not compute it twice. Passing one
            that was *not* produced by this module's gate would be a way to smuggle a causal
            claim past it, which is why there is no way to pass a bare value here — only a
            :class:`IncrementalFinding`, which cannot be constructed with a number and no
            experiment behind it without that being visible in a diff.
    """
    key = segment or SegmentKey()
    computed_at = moment or now()

    case_ids, provenances = _cohort(session, merchant_id, period, key)
    counters = _case_counters(session, merchant_id, case_ids)
    outcomes = _outcome_figures(session, merchant_id, case_ids)
    actions = _action_figures(session, merchant_id, case_ids)
    cost = _realized_cost(session, merchant_id, case_ids)

    finding = (
        incremental if incremental is not None else _incremental_finding(session, merchant_id)
    )

    labels = _labels(
        provenances=provenances,
        observed_revenue=outcomes.observed_revenue,
        incremental=finding,
    )

    metrics = CohortMetrics(
        period=period,
        computed_at=computed_at,
        segment=key,
        case_count=counters.case_count,
        revenue_at_risk=counters.revenue_at_risk,
        recovered_case_count=outcomes.recovered_cases,
        observed_recovered_revenue=outcomes.observed_revenue,
        natural_recovered_revenue=outcomes.natural_revenue,
        total_recovery_cost=cost,
        unresolved_revenue=counters.unresolved_revenue,
        blocked_case_count=counters.blocked,
        escalated_case_count=counters.escalated,
        unnecessary_action_count=actions.post_payment_actions,
        intervened_case_count=actions.intervened_cases,
        confirmed_action_count=actions.confirmed_actions,
        successful_action_count=actions.successful_actions,
        cycles_without_action_count=counters.cycles_without_action,
        recovery_rate=rate(outcomes.recovered_cases, counters.case_count),
        intervention_rate=rate(actions.intervened_cases, counters.case_count),
        # Denominator is confirmed *actions*, not cases — a case can carry more than one.
        action_success_rate=rate(actions.successful_actions, actions.confirmed_actions),
        escalation_rate=rate(counters.escalated, counters.case_count),
        average_hours_to_recovery=_average_hours(
            outcomes.total_seconds_to_recovery, outcomes.recovered_cases
        ),
        incremental=finding,
        labels=labels,
    )

    _logger.info(
        "cohort metrics computed",
        merchant_id=str(merchant_id),
        segment=str(key),
        case_count=metrics.case_count,
        causality_established=metrics.causality_established,
        labels=list(labels),
    )
    return metrics


# ---------------------------------------------------------------------------
# The cohort
# ---------------------------------------------------------------------------


def _cohort(
    session: Session,
    merchant_id: uuid.UUID,
    period: ReportingPeriod,
    key: SegmentKey,
) -> tuple[list[uuid.UUID], set[str]]:
    """The cohort's case ids and the set of provenance values they carry.

    Provenances are collected here rather than counted later because one synthetic case is enough
    to label every figure ``SYNTHETIC`` — so what matters is the *set*, not a count.

    Amount-band filtering happens in Python rather than SQL. The banding rule lives in
    ``domain.segments`` as integer boundary comparisons, and re-expressing it as a ``CASE``
    expression would be a second definition of the same rule — the drift bug this project keeps
    avoiding. Cohorts are bounded by a reporting period, so the row count is manageable.
    """
    statement = (
        select(RecoveryCase.id, RecoveryCase.provenance, RecoveryCase.payment_amount)
        .where(
            RecoveryCase.merchant_id == merchant_id,
            RecoveryCase.detected_at >= period.start,
            RecoveryCase.detected_at < period.end,
        )
        .order_by(RecoveryCase.detected_at)
    )
    if key.risk_cause is not None:
        # The cause lives on the active diagnosis, not the case. An EXISTS rather than a join so a
        # case with diagnosis rows across several cycles is still counted once — a join would
        # multiply the case into the cohort once per matching row and inflate every count.
        statement = statement.where(
            select(Diagnosis.id)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.case_id == RecoveryCase.id,
                Diagnosis.cause == key.risk_cause.value,
                Diagnosis.is_active,
            )
            .exists()
        )

    case_ids: list[uuid.UUID] = []
    provenances: set[str] = set()
    for row in session.execute(statement):
        if key.amount_band is not None and (
            amount_band_for(Minor(int(row.payment_amount))) is not key.amount_band
        ):
            continue
        case_ids.append(row.id)
        provenances.add(str(row.provenance or Provenance.REAL.value))
    return case_ids, provenances


@dataclass(frozen=True, slots=True)
class _CaseCounters:
    case_count: int
    revenue_at_risk: int
    unresolved_revenue: int
    blocked: int
    escalated: int
    cycles_without_action: int


def _case_counters(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> _CaseCounters:
    """Counts and money sums straight off the case rows.

    ``unresolved_revenue`` sums ``payment_amount`` over cases whose terminal state is anything but
    ``RECOVERED`` (R12.C5) — which includes cases still in flight. That is deliberate: money not
    yet recovered is unresolved whether the case failed or has not finished, and excluding
    in-flight cases would make the figure shrink as a period aged rather than as money arrived.

    ``cycles_without_action`` is R12.C16: cases that consumed a decision cycle and executed no
    confirmed action. The number that says how often Revora deliberated and then did nothing —
    which is either good judgement or a broken policy, and either way is worth being able to see.
    """
    if not case_ids:
        return _CaseCounters(0, 0, 0, 0, 0, 0)

    ids = list(case_ids)
    confirmed_case = (
        select(ExecutionIntent.id)
        .where(
            ExecutionIntent.merchant_id == merchant_id,
            ExecutionIntent.case_id == RecoveryCase.id,
            ExecutionIntent.state == IntentState.CONFIRMED.value,
        )
        .exists()
    )

    row = session.execute(
        select(
            func.count().label("cases"),
            func.coalesce(func.sum(RecoveryCase.payment_amount), 0).label("at_risk"),
            func.coalesce(
                func.sum(RecoveryCase.payment_amount).filter(
                    RecoveryCase.state != CaseState.RECOVERED.value
                ),
                0,
            ).label("unresolved"),
            func.count().filter(RecoveryCase.state == CaseState.BLOCKED.value).label("blocked"),
            func.count()
            .filter(RecoveryCase.state == CaseState.ESCALATED.value)
            .label("escalated"),
            func.count()
            .filter((RecoveryCase.decision_cycle_count >= 1) & ~confirmed_case)
            .label("cycles_without_action"),
        )
        .select_from(RecoveryCase)
        .where(RecoveryCase.merchant_id == merchant_id, RecoveryCase.id.in_(ids))
    ).one()

    return _CaseCounters(
        case_count=int(row.cases),
        revenue_at_risk=int(row.at_risk),
        unresolved_revenue=int(row.unresolved),
        blocked=int(row.blocked),
        escalated=int(row.escalated),
        cycles_without_action=int(row.cycles_without_action),
    )


@dataclass(frozen=True, slots=True)
class _OutcomeFigures:
    recovered_cases: int
    observed_revenue: int
    natural_revenue: int
    total_seconds_to_recovery: int


def _outcome_figures(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> _OutcomeFigures:
    """Recovery figures from ``recovery_outcome`` — the only table that can establish one.

    Every row there required an authoritative provider read (``verified_by_read_id`` is ``NOT
    NULL``) and there is at most one per case (``UNIQUE (case_id)``). So a recovery count taken
    from here cannot be inflated by a webhook and cannot double-count a case, which is both halves
    of Property 20 and the reason no figure in this module reads a webhook.

    Observed and natural are summed separately, never added together and never presented as
    incremental. ``ATTRIBUTED`` is counted as a recovery for the rate but contributes to neither
    revenue sum: it is licensed only by an experiment, and the experiment's own analysis is what
    reports it.
    """
    if not case_ids:
        return _OutcomeFigures(0, 0, 0, 0)

    ids = list(case_ids)
    row = session.execute(
        select(
            func.count()
            .filter(RecoveryOutcome.classification.in_(sorted(_RECOVERED_CLASSES)))
            .label("recovered"),
            func.coalesce(
                func.sum(RecoveryOutcome.recovered_amount).filter(
                    RecoveryOutcome.classification == OutcomeClass.OBSERVED.value
                ),
                0,
            ).label("observed"),
            func.coalesce(
                func.sum(RecoveryOutcome.recovered_amount).filter(
                    RecoveryOutcome.classification == OutcomeClass.NATURAL.value
                ),
                0,
            ).label("natural"),
            func.coalesce(func.sum(RecoveryOutcome.seconds_to_recovery), 0).label("seconds"),
        )
        .select_from(RecoveryOutcome)
        .where(
            RecoveryOutcome.merchant_id == merchant_id,
            RecoveryOutcome.case_id.in_(ids),
        )
    ).one()

    return _OutcomeFigures(
        recovered_cases=int(row.recovered),
        observed_revenue=int(row.observed),
        natural_revenue=int(row.natural),
        total_seconds_to_recovery=int(row.seconds),
    )


@dataclass(frozen=True, slots=True)
class _ActionFigures:
    intervened_cases: int
    confirmed_actions: int
    successful_actions: int
    post_payment_actions: int


def _action_figures(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> _ActionFigures:
    """Action counts from ``execution_intent``.

    ``successful_actions`` counts confirmed actions on a case that went on to recover — a
    *correlation*, and named ``action_success_rate`` rather than anything causal for that reason.
    It says how often an action was followed by recovery, not how often it caused one. The
    experiment is the only thing that speaks to the second.

    ``post_payment_actions`` is ``unnecessary_action_count``: actions that went out after the
    customer had already paid. Deliberately visible. It is the cost of Revora being wrong, and a
    system that hid it would be optimising its own report rather than the merchant's outcome.
    """
    if not case_ids:
        return _ActionFigures(0, 0, 0, 0)

    ids = list(case_ids)
    recovered_case = (
        select(RecoveryOutcome.id)
        .where(
            RecoveryOutcome.merchant_id == merchant_id,
            RecoveryOutcome.case_id == ExecutionIntent.case_id,
        )
        .exists()
    )
    confirmed = ExecutionIntent.state == IntentState.CONFIRMED.value

    row = session.execute(
        select(
            func.count(func.distinct(ExecutionIntent.case_id))
            .filter(confirmed)
            .label("intervened"),
            func.count().filter(confirmed).label("confirmed"),
            func.count().filter(confirmed & recovered_case).label("successful"),
            func.count().filter(ExecutionIntent.is_post_payment).label("post_payment"),
        )
        .select_from(ExecutionIntent)
        .where(
            ExecutionIntent.merchant_id == merchant_id,
            ExecutionIntent.case_id.in_(ids),
        )
    ).one()

    return _ActionFigures(
        intervened_cases=int(row.intervened),
        confirmed_actions=int(row.confirmed),
        successful_actions=int(row.successful),
        post_payment_actions=int(row.post_payment),
    )


def _realized_cost(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> int:
    """Total realized action cost, in minor units (R12.C6).

    From ``memory_observation.realized_cost``, written at the terminal transition. There is no
    per-action cost column on ``execution_intent``, and the alternative source would be the
    *estimated* cost on the recommendation — a prediction. Summing a prediction into a column
    called ``total_recovery_cost`` and subtracting it from revenue would put a guess inside a
    money figure, which is how a net revenue number stops meaning anything.

    Zero today, because a payment link costs nothing to create. Honestly zero rather than
    estimated.
    """
    if not case_ids:
        return 0
    return int(
        session.execute(
            select(func.coalesce(func.sum(MemoryObservation.realized_cost), 0))
            .select_from(MemoryObservation)
            .where(
                MemoryObservation.merchant_id == merchant_id,
                MemoryObservation.case_id.in_(list(case_ids)),
            )
        ).scalar_one()
    )


def _average_hours(total_seconds: int, recovered_cases: int) -> Decimal | str:
    """Mean hours from detection to verified recovery, or ``UNDEFINED`` with no recoveries.

    A mean over an empty set is undefined, not zero — and zero here would read as "recovery is
    instant", which is the most flattering possible misreading of no data at all.
    """
    if recovered_cases <= 0:
        return UNDEFINED
    return (Decimal(total_seconds) / Decimal(3600) / Decimal(recovered_cases)).quantize(
        _HOUR_PLACES, rounding=ROUND_HALF_UP
    )


# ---------------------------------------------------------------------------
# The causality gate
# ---------------------------------------------------------------------------


def _incremental_finding(session: Session, merchant_id: uuid.UUID) -> IncrementalFinding:
    """The incremental figure, or ``NOT_ESTABLISHED`` with the reasons (R12.C13).

    Looks for a ``COMPLETED`` experiment whose newest analysis clears the whole attribution gate.
    Every other path returns the sentinel with refusal codes attached, because the codes are what
    tell an operator whether to wait for data or accept that the treatment does not work.

    Deliberately conservative in one specific way: with no experiment at all, this returns the
    sentinel rather than falling back to observed recovery. Presenting observed as incremental is
    the exact substitution R12.C13 forbids, and it is the substitution a dashboard would make on
    its own if handed a null.
    """
    experiments = ExperimentRepository(session)
    completed = experiments.list_by_state(merchant_id, ExperimentState.COMPLETED, limit=10)
    if not completed:
        return IncrementalFinding(
            value=NOT_ESTABLISHED,
            refusal_codes=("NO_COMPLETED_EXPERIMENT",),
        )

    results = ExperimentResultRepository(session)
    all_refusals: list[str] = []

    for experiment in completed:
        result = results.latest_for_experiment(merchant_id, experiment.id)
        if result is None:
            all_refusals.append("EXPERIMENT_NOT_ANALYSED")
            continue

        # Read from the *stored* result, not recomputed from live tables. That is the point of
        # persisting the counts: a result that was underpowered when it was concluded must stay
        # underpowered, and recomputing the arms now would let it quietly become adequate as
        # later cases arrived.
        refusals = attribution_refusals(
            state=str(experiment.state),
            required_sample_size_per_group=int(experiment.required_sample_size_per_group),
            labels=experiment.labels,
            control_cases=int(result.control_case_count),
            treatment_cases=int(result.treatment_case_count),
            lift_ci_low=result.lift_ci_low,
            lift_ci_high=result.lift_ci_high,
        )
        if refusals:
            all_refusals.extend(refusal.code for refusal in refusals)
            continue

        # The gate is clear. The incremental figure is the treatment arm's recovered revenue less
        # what the control arm's rate says would have arrived anyway — integer arithmetic on
        # minor units throughout.
        value = _incremental_value(session, merchant_id, experiment.id)
        return IncrementalFinding(
            value=value,
            experiment_id=experiment.id,
            control_case_count=int(result.control_case_count),
            treatment_case_count=int(result.treatment_case_count),
            lift=result.lift,
            lift_ci_low=result.lift_ci_low,
            lift_ci_high=result.lift_ci_high,
        )

    return IncrementalFinding(
        value=NOT_ESTABLISHED,
        refusal_codes=tuple(dict.fromkeys(all_refusals)),
    )


def _incremental_value(
    session: Session, merchant_id: uuid.UUID, experiment_id: uuid.UUID
) -> int:
    """Treatment revenue less the control-rate counterfactual, in minor units.

    Computed from the arms' own cases rather than from the cohort, because the claim belongs to the
    experiment. A cohort-scoped incremental figure would mix cases the experiment never assigned
    into a number the experiment is being asked to license.
    """
    assignments = ExperimentAssignmentRepository(session)
    control_ids = assignments.case_ids_in_arm(
        merchant_id, experiment_id, ExperimentGroup.CONTROL
    )
    treatment_ids = assignments.case_ids_in_arm(
        merchant_id, experiment_id, ExperimentGroup.TREATMENT
    )

    control = _outcome_figures(session, merchant_id, control_ids)
    treatment = _outcome_figures(session, merchant_id, treatment_ids)

    if not control_ids or not treatment_ids or treatment.recovered_cases <= 0:
        return 0

    treatment_revenue = treatment.observed_revenue + treatment.natural_revenue
    per_recovery = treatment_revenue // treatment.recovered_cases
    control_rate = Decimal(control.recovered_cases) / Decimal(len(control_ids))
    counterfactual_recoveries = int(
        (Decimal(len(treatment_ids)) * control_rate).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )
    return treatment_revenue - counterfactual_recoveries * per_recovery


def _labels(
    *,
    provenances: set[str],
    observed_revenue: int,
    incremental: IncrementalFinding,
) -> tuple[str, ...]:
    """The labels that qualify every figure in the report.

    ``SYNTHETIC`` (R12.C11) if *any* contributing case is synthetic. One is enough — a figure
    derived partly from generated data cannot support a real-world claim, and a proportion would
    invite someone to decide that a little synthetic data is acceptable.

    ``CAUSALITY_NOT_ESTABLISHED`` (R12.C9) when observed recovery is positive and no experiment
    licenses a causal claim. The condition is specifically *positive* observed revenue: a period
    that recovered nothing needs no warning against over-reading its recovery, and labelling it
    would dilute the label where it matters.

    ``RECOVERY_GROSS_OF_REFUNDS`` always, for now. Refunds are captured on every authoritative
    read but not netted out, so every recovery figure is gross — and saying so on every surface is
    the difference between a simplification and a misstatement.
    """
    labels: list[str] = [RECOVERY_GROSS_OF_REFUNDS]

    if Provenance.SYNTHETIC.value in provenances:
        labels.append(ExperimentLabel.SYNTHETIC.value)

    if observed_revenue > 0 and not incremental.established:
        labels.append(ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value)

    return tuple(labels)

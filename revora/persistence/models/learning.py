"""Experiments, version freezes, model versions, promotions, and memory.

These tables exist so that a causal claim can be defended, and so that a model
cannot be changed underneath a running comparison.

**Assignment is immutable.** ``UNIQUE (case_id)`` plus no ``UPDATE`` grant on the
group column. A case assigned to control that later gets moved to treatment
destroys the comparison it was part of, and the move would be invisible in the
result. Assignment happens before any diagnosis runs, so the arm cannot be chosen
on the strength of the case.

**Versions are frozen per experiment.** ``experiment_version_freeze`` pins the
baseline workflow, the policy rule set, the baseline model and the simulator for
the life of the experiment. Without it, a mid-experiment model promotion silently
changes what the treatment arm *is*, and the measured difference stops meaning
anything.

**A promotion names a person.** ``model_promotion.approving_user_id`` is ``NOT
NULL`` (R15.C6). This is the same reason the tunable bounds are database rows and
not environment variables: a redeploy cannot supply an approving user.

**Memory holds the label, not just the outcome.** ``intervention_status`` records
whether an observation is usable as a baseline training label. Only
``NO_INTERVENTION_CONFIRMED`` is, and even that means "no Revora action and no
*recorded* merchant action" — Revora cannot see a merchant phoning a customer. The
weakness is labelled rather than solved, and ``MAX_UNKNOWN_INTERVENTION_SHARE``
turns it into a reported risk rather than a silent bias.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    NOT_ESTABLISHED,
    DecisionSource,
    DiagnosisMethod,
    ExperimentGroup,
    ExperimentState,
    InterventionStatus,
    OutcomeClass,
    PolicyVerdict,
    Provenance,
    RiskCause,
)
from revora.persistence.models.base import (
    CONFIDENCE,
    MONEY,
    PROBABILITY,
    SIGNED_INCREMENT,
    TIMESTAMPTZ,
    RowBase,
    enum_check,
)

__all__ = [
    "Experiment",
    "ExperimentAssignment",
    "ExperimentResult",
    "ExperimentVersionFreeze",
    "MemoryObservation",
    "ModelPromotion",
    "ModelVersion",
]


class Experiment(RowBase):
    """A controlled comparison, with its power calculation stored up front.

    ``required_sample_size_per_group`` is ``NOT NULL`` and computed at design time,
    before any case is assigned. Computing it afterwards is how an underpowered
    experiment gets reported as a finding: the sample size stops being a threshold
    and becomes a description of whatever data arrived.
    """

    __tablename__ = "experiment"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")
    primary_metric: Mapped[str] = mapped_column(Text, nullable=False)
    secondary_metrics: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    eligibility: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    allocation_ratio: Mapped[str] = mapped_column(Text, nullable=False, server_default="1:1")
    """Stored as text, not a float. ``1:1`` is a ratio, and rendering it as 0.5
    invites arithmetic on a value that is really a pair of integers."""

    assumed_baseline_rate: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    minimum_detectable_effect: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    significance_level: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    power: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    analysis_method: Mapped[str | None] = mapped_column(Text)
    required_sample_size_per_group: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    """Any of ``UNDERPOWERED``, ``INVALIDATED``, ``SYNTHETIC``, ``CONTAMINATED``,
    ``EXPLORATORY`` or ``CAUSALITY_NOT_ESTABLISHED`` disqualifies the result from
    supporting a causal claim. Stored as an array because more than one can apply,
    and dropping one of them would upgrade the claim."""

    __table_args__ = (
        UniqueConstraint("merchant_id", "name", name="uq_experiment_merchant_id_name"),
        CheckConstraint(
            "required_sample_size_per_group > 0", name="required_sample_size_positive"
        ),
        CheckConstraint(
            "significance_level > 0 AND significance_level < 1", name="significance_level_in_range"
        ),
        CheckConstraint("power > 0 AND power < 1", name="power_in_range"),
        enum_check("experiment", "state", ExperimentState),
        Index("ix_experiment_merchant_id_state", "merchant_id", "state"),
    )


class ExperimentAssignment(RowBase):
    """Which arm a case was assigned to, decided before diagnosis and never changed."""

    __tablename__ = "experiment_assignment"

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("experiment.id", ondelete="RESTRICT"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    group: Mapped[str] = mapped_column("group", Text, nullable=False)
    """Column name kept as the design writes it. ``GROUP`` is a reserved word, so
    every reference to it is quoted — worth the friction to keep the schema
    readable against the design document."""

    assigned_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    contaminated: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    """Set when a control case received an intervention anyway — a merchant acting
    manually, for instance. Contamination invalidates the comparison, and hiding it
    would invalidate the claim instead."""

    excluded: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    exclusion_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # One assignment per case. Two would mean the case sits in two arms, and
        # every figure derived from either arm becomes indefensible.
        UniqueConstraint("case_id", name="uq_experiment_assignment_case_id"),
        CheckConstraint(
            "excluded = (exclusion_reason IS NOT NULL)", name="exclusion_reason_iff_excluded"
        ),
        enum_check("experiment_assignment", "group", ExperimentGroup),
        Index("ix_experiment_assignment_experiment_id_group", "experiment_id", "group"),
    )


class ExperimentResult(RowBase):
    """One analysis of one experiment: the lift, its interval, and the honest labels.

    **The interval is the whole row.** A lift without one is a number that invites a causal
    claim it cannot support, so ``lift_ci_low`` and ``lift_ci_high`` are constrained to be
    present or absent together and ordered — a half-populated interval is uncommittable, and
    an inverted one is caught before anything reads it.

    **Rows accumulate; nothing is overwritten.** An experiment may be analysed more than once,
    and re-analysis after more data arrives is legitimate. What is *not* legitimate is quietly
    replacing an earlier result with a later, more flattering one — so every analysis is its
    own row with its own ``computed_at``, and the history of what was concluded when is
    reconstructable. Repeated interim looks are a real inferential hazard, and the mitigation
    is that they are visible rather than that they are prevented.

    **Counts are stored, not derived.** ``control_case_count`` and ``treatment_case_count`` are
    compared against the experiment's ``required_sample_size_per_group`` to decide whether the
    result may support attribution (R13.C8). Recomputing them at read time against a live
    assignment table would let a result that was underpowered when concluded silently become
    adequately powered later.
    """

    __tablename__ = "experiment_result"

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("experiment.id", ondelete="RESTRICT"), nullable=False
    )
    primary_metric: Mapped[str] = mapped_column(Text, nullable=False)
    """Copied from the experiment rather than joined, because the experiment's own
    ``primary_metric`` could in principle be edited and a result must describe what was
    actually analysed."""

    analysis_method: Mapped[str] = mapped_column(Text, nullable=False)

    control_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    treatment_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    control_recoveries: Mapped[int] = mapped_column(Integer, nullable=False)
    treatment_recoveries: Mapped[int] = mapped_column(Integer, nullable=False)

    control_rate: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    treatment_rate: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    """``NULL`` on a zero denominator, never zero. An arm with no cases has no rate, and
    reporting 0.0000 would make an empty arm look like a total failure to recover."""

    lift: Mapped[Decimal | None] = mapped_column(SIGNED_INCREMENT)
    lift_ci_low: Mapped[Decimal | None] = mapped_column(SIGNED_INCREMENT)
    lift_ci_high: Mapped[Decimal | None] = mapped_column(SIGNED_INCREMENT)
    """Signed, because a treatment can do worse than doing nothing and the design refuses to
    hide that. ``SIGNED_INCREMENT`` rather than ``PROBABILITY`` for exactly that reason — a
    difference of two probabilities lives in [-1, 1]."""

    contaminated_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Reported alongside every result (R13.C15). A lift computed after discarding a third of
    the control arm is a different claim from one that discarded none, and the reader is
    entitled to see which happened."""

    labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    """The result's own labels, distinct from the experiment's. An experiment can be sound
    while a particular analysis of it is ``UNDERPOWERED`` or
    ``CAUSALITY_NOT_ESTABLISHED`` — the interval containing zero is a property of the
    analysis, not of the design."""

    comparison: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The four-way per-arm comparison R13.C13 asks for: net recovered revenue, intervention
    rate, customer messages per case and blocked case count, each with the direction and size
    of treatment minus control.

    JSONB rather than twelve typed columns because these four are *reported together and
    reasoned about together* — the question is "what did the lift cost", and splitting the
    answer across a dozen columns invites reading one without the others. Nothing gates a
    claim on these, so none of them needs a ``CHECK``."""

    computed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "control_case_count >= 0 AND treatment_case_count >= 0 "
            "AND control_recoveries >= 0 AND treatment_recoveries >= 0 "
            "AND contaminated_count >= 0 AND excluded_count >= 0",
            name="result_counts_nonnegative",
        ),
        # A recovery count above the case count is arithmetically impossible and would
        # produce a rate above one, which every downstream reader would take at face value.
        CheckConstraint(
            "control_recoveries <= control_case_count "
            "AND treatment_recoveries <= treatment_case_count",
            name="recoveries_within_case_counts",
        ),
        # Both bounds or neither. A single bound is not an interval, and a reader shown one
        # would reasonably assume the other end was zero.
        CheckConstraint(
            "(lift_ci_low IS NULL) = (lift_ci_high IS NULL)",
            name="interval_bounds_present_together",
        ),
        CheckConstraint(
            "lift_ci_low IS NULL OR lift_ci_low <= lift_ci_high",
            name="interval_bounds_ordered",
        ),
        # Reason: the metrics engine asks for the newest analysis of an experiment, and the
        # dashboard lists an experiment's analysis history.
        Index(
            "ix_experiment_result_experiment_id_computed_at",
            "experiment_id",
            "computed_at",
        ),
    )


class ExperimentVersionFreeze(RowBase):
    """One component pinned to one version for the life of an experiment."""

    __tablename__ = "experiment_version_freeze"

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("experiment.id", ondelete="RESTRICT"), nullable=False
    )
    component: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # One freeze per component per experiment. A second row would mean the
        # component changed mid-experiment, which is the thing this table prevents.
        UniqueConstraint(
            "experiment_id",
            "component",
            name="uq_experiment_version_freeze_experiment_id_component",
        ),
    )


class ModelVersion(RowBase):
    """A trained artefact and the counts behind it.

    ``synthetic_observation_count`` is separate from
    ``training_observation_count`` rather than added to it, because a model trained
    partly on generated data cannot support a real-world claim and the split is the
    only way to see that from the row.
    """

    __tablename__ = "model_version"

    component: Mapped[str] = mapped_column(Text, nullable=False)
    version_label: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="INACTIVE")
    training_observation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    synthetic_observation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    artifact: Mapped[bytes | None] = mapped_column(LargeBinary)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    training_snapshot_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "component",
            "version_label",
            name="uq_model_version_merchant_component_label",
        ),
        CheckConstraint(
            "training_observation_count >= 0 AND synthetic_observation_count >= 0",
            name="observation_counts_nonnegative",
        ),
        CheckConstraint(
            "state IN ('INACTIVE', 'ACTIVE', 'RETIRED')",
            name="state_known",
        ),
        # At most one active version per component per merchant. Two active
        # versions means two different baselines and no way to say which produced
        # a stored estimate.
        Index(
            "one_active_model_version_per_component",
            "merchant_id",
            "component",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )


class ModelPromotion(RowBase):
    """A record that a person promoted a model version.

    ``approving_user_id`` is ``NOT NULL`` because R15.C6 requires the approving
    user to be recorded. An automatic promotion would satisfy neither the
    requirement nor anyone asking why the numbers moved.
    """

    __tablename__ = "model_promotion"

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id", ondelete="RESTRICT"), nullable=False
    )
    prior_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id", ondelete="RESTRICT")
    )
    approving_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT"), nullable=False
    )
    promoted_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    evaluation_report: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # A version cannot be promoted onto itself; that is a no-op dressed as an
        # approval.
        CheckConstraint(
            "prior_version_id IS NULL OR prior_version_id <> model_version_id",
            name="promotion_changes_version",
        ),
        Index("ix_model_promotion_model_version_id", "model_version_id"),
    )


class MemoryObservation(RowBase):
    """One resolved case, flattened into a training observation.

    Written once per case when it reaches a terminal state, which is why
    ``UNIQUE (case_id)`` holds. Rewriting an observation would change the training
    set underneath a model that was already evaluated against it.

    ``decision_source`` is retained so a model trained on this data cannot silently
    reproduce past human choices without the skew being visible in the data it
    learned from.
    """

    __tablename__ = "memory_observation"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    features: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    cause: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    diagnosis_method: Mapped[str | None] = mapped_column(Text)
    """How the cause was arrived at. R15.C1 names it alongside cause and confidence.

    Stored because it decides whether the label is trustworthy, not for completeness. A cause
    reached by ``FALLBACK_UNKNOWN`` is a cause nobody established, and a model trained on
    those rows as though they were diagnoses would learn the shape of our own ignorance. A
    future trainer filters on this column; today nothing does, and recording it anyway is what
    makes that filtering possible later rather than a reason to discard the history."""

    selected_action: Mapped[str | None] = mapped_column(Text)
    policy_verdict: Mapped[str | None] = mapped_column(Text)
    outcome_class: Mapped[str | None] = mapped_column(Text)
    """Nullable and constrained to the outcome classes plus ``NOT_ESTABLISHED``,
    which is not an outcome class on purpose: "we have not measured this" and "we
    measured this and it was nothing" are different statements."""

    realized_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    group: Mapped[str | None] = mapped_column("group", Text)
    executed_action_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    customer_message_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    decision_source: Mapped[str | None] = mapped_column(Text)
    intervention_status: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="REAL")
    trained_into_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id", ondelete="RESTRICT")
    )
    """Which model version consumed this observation, so a training set is
    reconstructable from rows rather than from a snapshot nobody kept."""

    __table_args__ = (
        # One observation per case. A second would weight that case twice in
        # training and twice in every calibration band it falls into.
        UniqueConstraint("case_id", name="uq_memory_observation_case_id"),
        CheckConstraint("realized_cost >= 0", name="realized_cost_nonnegative"),
        enum_check("memory_observation", "cause", RiskCause),
        enum_check("memory_observation", "diagnosis_method", DiagnosisMethod),
        enum_check("memory_observation", "selected_action", CandidateAction),
        enum_check("memory_observation", "policy_verdict", PolicyVerdict),
        enum_check("memory_observation", "outcome_class", OutcomeClass, extra=(NOT_ESTABLISHED,)),
        enum_check("memory_observation", "group", ExperimentGroup),
        enum_check("memory_observation", "decision_source", DecisionSource),
        enum_check("memory_observation", "intervention_status", InterventionStatus),
        enum_check("memory_observation", "provenance", Provenance),
        # Reason: the baseline trainer selects usable labels per merchant, and only
        # NO_INTERVENTION_CONFIRMED observations are usable.
        Index(
            "ix_memory_observation_merchant_id_intervention_status",
            "merchant_id",
            "intervention_status",
        ),
    )

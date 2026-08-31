"""Baselines, per-action candidates, and the recommendation that ranks them.

This is where the value chain becomes durable, so the column types matter more
here than anywhere else. Probabilities are ``NUMERIC(6,4)``, the incremental
probability is ``NUMERIC(7,4)`` because it is signed, and every money column is
``BIGINT`` minor units. There is no float in the chain at any point: the single
multiplication of a probability by an amount happens in ``domain.money`` and the
integer result is what lands here.

Four things are stored that a simpler schema would drop, each because dropping it
would let a number be presented without its caveat:

* ``uncertainty_available`` — false means the interval columns are absent, not
  narrow. A missing interval must not read as a confident one.
* ``validation_status`` — ``UNVALIDATED_BASELINE`` propagates to the dashboard.
  A baseline nothing has been checked against is still a baseline, but the claim
  it supports is weaker and the merchant is entitled to know that.
* ``availability`` and ``unavailable_reason`` — an action that cannot be executed
  appears in the recommendation marked unavailable rather than being silently
  omitted, so the merchant can see that a retry *was* considered.
* ``divergence_reason`` on the recommendation — when the highest-probability
  candidate is not the selected one, that divergence is the product's whole
  argument and it is recorded rather than reconstructed.

``recommendation.ai_explanation_text`` is named the way it is on purpose. It is
prose, it is read by exactly one code path (the dashboard serializer), and no
figure is ever derived from it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    EstimationMethod,
    ExclusionReason,
    Provenance,
    RiskCause,
    SelectionReason,
    ValidationStatus,
)
from revora.persistence.models.base import (
    MONEY,
    PROBABILITY,
    SIGNED_INCREMENT,
    RowBase,
    enum_check,
    nonnegative_money_check,
)

__all__ = [
    "BaselineEstimate",
    "CandidateEstimate",
    "Recommendation",
    "RecommendationCandidate",
]


class BaselineEstimate(RowBase):
    """The probability this payment recovers with no intervention at all.

    Every incremental claim in the system is a difference against this number, so
    an unvalidated or uncalibrated baseline weakens everything downstream — hence
    ``validation_status`` and ``method`` are stored on the row rather than inferred
    from which model happened to be active.
    """

    __tablename__ = "baseline_estimate"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    decision_cycle: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    probability: Mapped[Decimal] = mapped_column(PROBABILITY, nullable=False)
    ci_low: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    ci_high: Mapped[Decimal | None] = mapped_column(PROBABILITY)
    uncertainty_available: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    segment_id: Mapped[str | None] = mapped_column(Text)
    features: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """The feature vector, stored so a later calibration report can be recomputed
    against exactly what the model saw."""

    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id", ondelete="RESTRICT")
    )
    method: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="REAL")
    validation_status: Mapped[str] = mapped_column(Text, nullable=False)
    training_snapshot_id: Mapped[str | None] = mapped_column(Text)
    """Which frozen training set produced the model. Without it, "the model said
    0.42" is not reproducible."""

    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 1", name="probability_in_range"),
        CheckConstraint(
            "ci_low IS NULL OR ci_high IS NULL OR ci_low <= ci_high",
            name="interval_ordered",
        ),
        # An interval is either present and complete or absent. Half an interval
        # is worse than none, because a chart will draw it.
        CheckConstraint(
            "(uncertainty_available AND ci_low IS NOT NULL AND ci_high IS NOT NULL) "
            "OR (NOT uncertainty_available AND ci_low IS NULL AND ci_high IS NULL)",
            name="interval_present_iff_available",
        ),
        enum_check("baseline_estimate", "method", EstimationMethod),
        enum_check("baseline_estimate", "provenance", Provenance),
        enum_check("baseline_estimate", "validation_status", ValidationStatus),
        Index("ix_baseline_estimate_case_id_decision_cycle", "case_id", "decision_cycle"),
        # Reason: the calibration report groups control-group observations by
        # segment and predicted band.
        Index("ix_baseline_estimate_merchant_id_segment_id", "merchant_id", "segment_id"),
    )


class CandidateEstimate(RowBase):
    """One action's simulated intervention probability and its three costs.

    Costs are split three ways rather than summed because they answer different
    questions and are configured separately: ``action_cost`` is what the action
    costs to perform, ``risk_cost`` is the expected cost of it going wrong, and
    ``customer_cost`` prices the intrusion on the customer. A single ``cost``
    column would make the customer's interest invisible in the arithmetic.
    """

    __tablename__ = "candidate_estimate"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    baseline_estimate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("baseline_estimate.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    intervention_probability: Mapped[Decimal] = mapped_column(PROBABILITY, nullable=False)
    action_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    risk_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    customer_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    method: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="REAL")
    availability: Mapped[str] = mapped_column(Text, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint(
            "intervention_probability >= 0 AND intervention_probability <= 1",
            name="intervention_probability_in_range",
        ),
        nonnegative_money_check("action_cost"),
        nonnegative_money_check("risk_cost"),
        nonnegative_money_check("customer_cost"),
        # An unavailable action must say why. "Unavailable" with no reason is what
        # a dashboard renders as an unexplained absence.
        CheckConstraint(
            "availability <> 'UNAVAILABLE' OR unavailable_reason IS NOT NULL",
            name="unavailable_requires_reason",
        ),
        # One estimate per action per baseline. A second would mean two different
        # numbers for the same action in one decision.
        UniqueConstraint(
            "baseline_estimate_id",
            "action",
            name="uq_candidate_estimate_baseline_estimate_id_action",
        ),
        enum_check("candidate_estimate", "action", CandidateAction),
        enum_check("candidate_estimate", "method", EstimationMethod),
        enum_check("candidate_estimate", "provenance", Provenance),
        enum_check("candidate_estimate", "availability", ActionAvailability),
        Index("ix_candidate_estimate_case_id", "case_id"),
    )


class Recommendation(RowBase):
    """What the optimizer selected, and why — including why it selected nothing.

    ``DO_NOTHING`` is a real selection with a real reason. A merchant who cannot
    see why Revora did nothing will conclude it is broken, so
    ``NO_POSITIVE_VALUE`` and ``HIGH_BASELINE_NO_INTERVENTION`` are recorded with
    the same weight as a positive choice.
    """

    __tablename__ = "recommendation"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    baseline_estimate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("baseline_estimate.id", ondelete="RESTRICT"), nullable=False
    )
    decision_cycle: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    selected_action: Mapped[str] = mapped_column(Text, nullable=False)
    selection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    divergence_reason: Mapped[str | None] = mapped_column(Text)
    """Set when the highest-probability candidate was not selected. The dashboard
    shows both figures side by side, because "more likely to work" and "worth
    doing" being different is the entire argument for the product."""

    substituted_risk_cause: Mapped[str | None] = mapped_column(Text)
    substitution_reason: Mapped[str | None] = mapped_column(Text)
    """Set when a low-confidence or rejected diagnosis was replaced by
    ``UNKNOWN``, which narrows the candidate set. Recorded so a conservative
    recommendation is explicable rather than looking like a failure."""

    ai_explanation_text: Mapped[str | None] = mapped_column(Text)
    """AI-generated prose. Named to make its status unmissable. Read by the
    dashboard serializer and nothing else; no figure is derived from it, and it is
    absent whenever the AI layer was unavailable or its output was rejected."""

    __table_args__ = (
        # One recommendation per decision cycle. A second would leave two answers
        # and no record of which one the policy decision was evaluated against.
        UniqueConstraint(
            "case_id", "decision_cycle", name="uq_recommendation_case_id_decision_cycle"
        ),
        enum_check("recommendation", "selected_action", CandidateAction),
        enum_check("recommendation", "selection_reason", SelectionReason),
        enum_check("recommendation", "substituted_risk_cause", RiskCause),
    )


class RecommendationCandidate(RowBase):
    """One ranked candidate inside a recommendation, with its full arithmetic.

    Every figure the selection was made from is stored, not just the winner's.
    That is what lets the dashboard show the comparison, what lets a merchant
    challenge a decision, and what makes the exclusion reasons auditable.

    ``incremental_probability`` is signed ``NUMERIC(7,4)`` and
    ``expected_incremental_revenue`` and ``net_recovery_value`` are signed
    ``BIGINT``. Negative values are retained rather than clipped: an action
    estimated to make recovery less likely is excluded for a stated reason, not
    because the number was flattened to zero on the way in.
    """

    __tablename__ = "recommendation_candidate"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recommendation.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_estimate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_estimate.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    incremental_probability: Mapped[Decimal] = mapped_column(SIGNED_INCREMENT, nullable=False)
    expected_incremental_revenue: Mapped[int] = mapped_column(MONEY, nullable=False)
    action_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    risk_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    customer_cost: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    net_recovery_value: Mapped[int] = mapped_column(MONEY, nullable=False)
    excluded: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(SmallInteger)
    """Null for an excluded candidate: an excluded action has no position in an
    ordering it was never part of."""

    __table_args__ = (
        CheckConstraint(
            "incremental_probability >= -1 AND incremental_probability <= 1",
            name="incremental_probability_in_range",
        ),
        nonnegative_money_check("action_cost"),
        nonnegative_money_check("risk_cost"),
        nonnegative_money_check("customer_cost"),
        # An exclusion always has a reason, and a reason implies an exclusion.
        # Either half alone produces a candidate nobody can explain.
        CheckConstraint(
            "excluded = (exclusion_reason IS NOT NULL)",
            name="exclusion_reason_iff_excluded",
        ),
        # One row per action per recommendation. Two rows for one action would
        # make the ranking ambiguous.
        UniqueConstraint(
            "recommendation_id", "action", name="uq_recommendation_candidate_recommendation_action"
        ),
        enum_check("recommendation_candidate", "action", CandidateAction),
        enum_check("recommendation_candidate", "exclusion_reason", ExclusionReason),
    )

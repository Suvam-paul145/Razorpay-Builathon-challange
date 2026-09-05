"""The policy decision, its twelve checks, and the versioned rule set.

Policy is the only authority that can permit an external effect, so its record has
to answer three questions long after the fact: what was decided, against which
rules, and against which state.

* ``rule_set_version`` and ``rule_set_id`` pin the rules. A decision reviewed six
  months later must be judged against the rules that were live when it was made,
  not the ones live when it is read.
* ``case_state_at_evaluation`` pins the state. A decision made while the case was
  ``DECISION_PENDING`` is not valid against a case that has since moved on.
* ``expires_at`` pins the time. An ``APPROVED`` decision older than
  ``POLICY_DECISION_VALIDITY`` is refused by execution, because the world it was
  evaluated against has had time to change — the customer may have paid.

``consumed_by_intent_id`` with a unique constraint is what makes one approval
authorize at most one external effect (R8.C15). It carries no foreign key
deliberately: ``execution_intent`` already references ``policy_decision``, and a
key in both directions is a circular dependency that every schema-creation order
has to work around. The uniqueness is the invariant; the pointer is a convenience.

``policy_check_result`` always has twelve rows. Not "up to twelve" — a check that
could not be evaluated is recorded as ``UNAVAILABLE`` and forces a block, because
a policy engine that silently skips a check it could not run is a policy engine
that approves on missing data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    POLICY_CHECK_ORDER,
    CaseState,
    CheckOutcome,
    PolicyCheck,
    PolicyVerdict,
)
from revora.persistence.models.base import TIMESTAMPTZ, RowBase, enum_check

__all__ = ["POLICY_CHECK_COUNT", "PolicyCheckResult", "PolicyDecision", "PolicyRuleSet"]

POLICY_CHECK_COUNT: int = len(POLICY_CHECK_ORDER)
"""Twelve, read from the domain's own ordering rather than written down again."""


class PolicyRuleSet(RowBase):
    """A versioned, approved set of policy rules.

    Versioned and approved because R15.C6 requires a recorded approving user for a
    policy change. That is also why the tunable bounds live in ``app_config``
    rather than in environment variables: a redeploy cannot name a person.
    """

    __tablename__ = "policy_rule_set"

    version_label: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    approving_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="false")

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "version_label", name="uq_policy_rule_set_merchant_id_version_label"
        ),
        # At most one active rule set per merchant. Two would mean a decision could
        # cite either, and reviewing it later would be guesswork.
        Index(
            "one_active_rule_set_per_merchant",
            "merchant_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class PolicyDecision(RowBase):
    """One evaluation of the twelve checks against one case and one action."""

    __tablename__ = "policy_decision"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recommendation.id", ondelete="RESTRICT")
    )
    """Nullable: policy also evaluates actions that no recommendation proposed,
    such as a human-initiated one."""

    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    primary_reason: Mapped[str] = mapped_column(Text, nullable=False)
    """The first failing check, in the fixed evaluation order. Fixed order is why
    an expensive or case-specific check can never end up being the stated reason a
    paid or opted-out customer was contacted."""

    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_rule_set.id", ondelete="RESTRICT")
    )
    rule_set_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_version: Mapped[str | None] = mapped_column(Text)
    """Which ``app_config`` version supplied the bounds. Without it, a decision
    made under a bound of 3 is indistinguishable from one made under a bound of 5."""

    evaluated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    earliest_permitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """Set on a ``DEFERRED`` verdict: when the cooldown will have elapsed. Stored
    so the scheduler does not have to re-derive it and possibly disagree."""

    idempotency_key: Mapped[str | None] = mapped_column(Text)
    """Minted here, consumed by execution. Deriving it at decision time is what
    makes a retried execution reuse the same key rather than mint a second one."""

    selected_action: Mapped[str] = mapped_column(Text, nullable=False)
    case_state_at_evaluation: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_by_intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decision_cycle: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        # One approval authorizes at most one external effect (R8.C15). Partial, so
        # the many unconsumed decisions do not collide on NULL.
        Index(
            "one_intent_per_decision",
            "consumed_by_intent_id",
            unique=True,
            postgresql_where=text("consumed_by_intent_id IS NOT NULL"),
        ),
        CheckConstraint("expires_at > evaluated_at", name="validity_window_positive"),
        enum_check("policy_decision", "verdict", PolicyVerdict),
        enum_check("policy_decision", "selected_action", CandidateAction),
        enum_check("policy_decision", "case_state_at_evaluation", CaseState),
        Index("ix_policy_decision_case_id_evaluated_at", "case_id", "evaluated_at"),
        # Reason: the deferred-decision sweep looks for decisions whose cooldown
        # has since elapsed, per merchant.
        Index(
            "ix_policy_decision_merchant_id_earliest_permitted_at",
            "merchant_id",
            "earliest_permitted_at",
        ),
    )


class PolicyCheckResult(RowBase):
    """One of the twelve checks, with its position in the fixed order.

    ``UNIQUE (policy_decision_id, check_order)`` prevents the same position being
    filled twice, which is how a partially-recorded evaluation would otherwise
    look complete.
    """

    __tablename__ = "policy_check_result"

    policy_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_decision.id", ondelete="RESTRICT"), nullable=False
    )
    check_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    """Masked. A check that failed on a contact identifier records the masked
    form, never the identifier."""

    __table_args__ = (
        UniqueConstraint(
            "policy_decision_id",
            "check_order",
            name="uq_policy_check_result_policy_decision_id_check_order",
        ),
        # The order is the domain's ORDER tuple, so a row outside 1..12 means the
        # writer invented a position.
        CheckConstraint(
            f"check_order >= 1 AND check_order <= {POLICY_CHECK_COUNT}",
            name="check_order_within_fixed_order",
        ),
        enum_check("policy_check_result", "check_id", PolicyCheck),
        enum_check("policy_check_result", "outcome", CheckOutcome),
    )

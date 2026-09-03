"""Execution intents, authoritative payment reads, and the verified outcome.

``execution_intent`` is the exactly-once record. ``UNIQUE (merchant_id,
idempotency_key)`` is the constraint the whole execution design rests on: the row
is inserted *before* the provider is called, in its own committed transaction, so a
crash between insert and call leaves evidence that the call may have happened. A
retry computes the same key, hits the constraint, and reconciles instead of calling
again. Without the constraint, "exactly once" is a code promise; with it, a second
external effect for one authorization is impossible to commit.

``UNCERTAIN`` is a real state and not an error state. It means the provider was
called and we do not know the outcome. While an intent sits there, no further
external call is issued for that case. That fails safe but it fails silently, which
is why the partial index below exists and why the count in that state needs an
alarm.

``payment_state_read`` keeps every authoritative read rather than only the latest.
When a webhook and a provider read disagree — which they do, because reads lag —
the disagreement can then be reconstructed from rows instead of argued about from
memory.

``recovery_outcome`` has ``UNIQUE (case_id)``. Recovery is counted once per case by
construction, which is half of the guarantee that a reported recovery figure is not
double-counted. ``verified_by_read_id`` is ``NOT NULL``: an outcome that is not
backed by an authoritative read cannot be written at all, which is the other half.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, ExecutionEffectKind, IntentState, OutcomeClass
from revora.persistence.models.base import MONEY, TIMESTAMPTZ, RowBase, enum_check

__all__ = [
    "RECONCILABLE_EFFECT_KIND_SQL",
    "UNRESOLVED_INTENT_STATES_SQL",
    "ExecutionIntent",
    "PaymentStateRead",
    "RecoveryOutcome",
]

UNRESOLVED_INTENT_STATES_SQL: str = ", ".join(
    f"'{state.value}'" for state in (IntentState.ATTEMPTED, IntentState.UNCERTAIN)
)
"""The two states the reconciliation sweeper looks for. Rendered from the enum so
the index predicate cannot drift from the state names."""

RECONCILABLE_EFFECT_KIND_SQL: str = (
    f"effect_kind = '{ExecutionEffectKind.PAYMENT_LINK_CREATE.value}'"
)
"""The one effect a provider read can answer a question about.

A resend response carries only a success boolean and no notification identifier, so a
resend is re-readable by nothing and an ``UNCERTAIN`` resend intent is *permanently*
unresolvable rather than slow to resolve. This clause in
``ix_execution_intent_unresolved`` is what keeps such a row out of the set the
reconciliation sweep scans — absent from the read, not skipped by a branch someone can
delete. Rendered from the enum for the same reason the state list is."""


class ExecutionIntent(RowBase):
    """One attempt to produce an external effect, recorded before it is attempted."""

    __tablename__ = "execution_intent"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    policy_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_decision.id", ondelete="RESTRICT"), nullable=False
    )
    """``NOT NULL``. There is no path to an external effect that does not carry the
    authorization that permitted it."""

    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    """Derived deterministically from the case, the action and the attempt ordinal.
    Also sent to the provider as ``reference_id``, so their side rejects a duplicate
    too — two independent mechanisms for one guarantee."""

    action: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    provider_response_id: Mapped[str | None] = mapped_column(Text)
    provider_short_url: Mapped[str | None] = mapped_column(Text)
    """A payment link is a bearer capability: whoever holds the URL can pay. Shown
    in the dashboard, never written to a log line or an audit record in clear."""

    provider_failure_code: Mapped[str | None] = mapped_column(Text)
    is_post_payment: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    """True when reconciliation discovered the payment had already succeeded before
    this action landed. Distinguishes "we recovered it" from "it was already paid",
    which is the difference between an honest metric and a flattering one."""

    reconciliation_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    counter_applied: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    """Whether this intent's attempt was counted against the case bounds. Recorded
    so a reconciliation that runs twice cannot count it twice."""

    effect_kind: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=ExecutionEffectKind.PAYMENT_LINK_CREATE.value,
    )
    """Which external effect was attempted, and the reason the index below is narrow.

    The server default stays on the column after ``0008``'s backfill. It is what made
    every pre-``0008`` row correct — every intent written before it was a link creation
    — and dropping it afterwards would show as a permanent autogenerate diff, which is
    how genuine drift stops being visible among the noise. The resend path passes this
    column explicitly rather than relying on the default being wrong for it."""

    __table_args__ = (
        # THE exactly-once constraint (P3). A second intent for one idempotency key
        # cannot be committed, so a second external effect cannot happen.
        UniqueConstraint(
            "merchant_id",
            "idempotency_key",
            name="uq_execution_intent_merchant_id_idempotency_key",
        ),
        CheckConstraint("attempt_ordinal >= 1", name="attempt_ordinal_positive"),
        CheckConstraint("reconciliation_attempts >= 0", name="reconciliation_attempts_nonnegative"),
        # A resolved intent has a resolution time; an unresolved one does not claim
        # to have been resolved.
        CheckConstraint(
            "(state IN ('CONFIRMED', 'FAILED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_iff_resolved",
        ),
        enum_check("execution_intent", "state", IntentState),
        enum_check("execution_intent", "action", CandidateAction),
        enum_check("execution_intent", "effect_kind", ExecutionEffectKind),
        # Reason: the reconciliation sweeper claims unresolved intents oldest
        # first. Partial, because the resolved ones are the overwhelming majority
        # and indexing them would make this scan read them.
        #
        # The effect-kind clause is the mechanism, not an optimization. Both
        # reconcile_intents and promote_stale_intents read their candidates through
        # this one predicate, so narrowing it is what removes resend rows from the set
        # being scanned — and a future reader who drops the filter from the query gets
        # a sequential scan and a failing performance assertion before they get a
        # duplicate SMS.
        Index(
            "ix_execution_intent_unresolved",
            "state",
            "attempt_started_at",
            postgresql_where=text(
                f"state IN ({UNRESOLVED_INTENT_STATES_SQL}) AND {RECONCILABLE_EFFECT_KIND_SQL}"
            ),
        ),
        Index("ix_execution_intent_case_id_attempt_ordinal", "case_id", "attempt_ordinal"),
    )


class PaymentStateRead(RowBase):
    """One authoritative read of a payment's state from the provider.

    Authoritative meaning: read directly from the provider's API, not inferred from
    a webhook. Only these can verify a recovery — a webhook says something
    happened, a read says what is currently true.
    """

    __tablename__ = "payment_state_read"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    provider_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    """The provider's own vocabulary. No ``CHECK``: they may add a status and we
    would rather store an unrecognised one than refuse the read."""

    amount: Mapped[int] = mapped_column(MONEY, nullable=False)
    amount_refunded: Mapped[int] = mapped_column(MONEY, nullable=False, server_default="0")
    """Captured on every read. MVP recovery figures are labelled gross of refunds
    rather than quietly netted, so this column exists before it is used."""

    captured: Mapped[bool] = mapped_column(nullable=False)
    read_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    raw: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """The provider's response, PII-free. Kept so a conflict is reconstructable."""

    __table_args__ = (
        CheckConstraint("amount >= 0 AND amount_refunded >= 0", name="amounts_nonnegative"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        # Reason: reconciliation reads the newest read for a case, and the
        # conflict reporter reads them in order.
        Index("ix_payment_state_read_case_id_read_at", "case_id", "read_at"),
        Index(
            "ix_payment_state_read_merchant_id_provider_payment_id",
            "merchant_id",
            "provider_payment_id",
        ),
    )


class RecoveryOutcome(RowBase):
    """The verified end of a case, counted exactly once.

    ``classification`` licenses three different claims and they are not
    interchangeable. ``NATURAL`` — the money arrived without us. ``OBSERVED`` — we
    acted and the money arrived, which is not the same as causing it.
    ``ATTRIBUTED`` — a controlled comparison supports the causal claim. Only the
    third one may be reported as incremental recovery.
    """

    __tablename__ = "recovery_outcome"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    recovered_amount: Mapped[int] = mapped_column(MONEY, nullable=False)
    recovery_timestamp: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    seconds_to_recovery: Mapped[int | None] = mapped_column(Integer)
    verified_by_read_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_state_read.id", ondelete="RESTRICT"), nullable=False
    )
    """``NOT NULL`` by design: no authoritative read, no recorded recovery. This is
    the column that makes a reported recovery figure defensible."""

    reconciled_from_terminal_state: Mapped[str | None] = mapped_column(Text)
    """Set when a case that had already ended for another reason was later found to
    have been paid — a delayed webhook, or a payment made after the window closed.
    Permitted exactly once per case, and only against a verified capture."""

    __table_args__ = (
        # Recovery is counted once per case (half of P20). A second row would
        # double-count the money in every aggregate that sums this table.
        UniqueConstraint("case_id", name="uq_recovery_outcome_case_id"),
        CheckConstraint("recovered_amount >= 0", name="recovered_amount_nonnegative"),
        CheckConstraint(
            "seconds_to_recovery IS NULL OR seconds_to_recovery >= 0",
            name="seconds_to_recovery_nonnegative",
        ),
        enum_check("recovery_outcome", "classification", OutcomeClass),
        enum_check("recovery_outcome", "reconciled_from_terminal_state", CaseState),
        # Reason: cohort aggregation sums recovered amounts per merchant over a
        # time range.
        Index(
            "ix_recovery_outcome_merchant_id_recovery_timestamp",
            "merchant_id",
            "recovery_timestamp",
        ),
    )

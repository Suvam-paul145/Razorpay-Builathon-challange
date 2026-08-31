"""The aggregate root and its diagnoses.

``recovery_case`` is the row every other decision hangs off, and it is where the
two invariants that bound customer contact are made structural rather than
promised.

**One open case per payment.** A partial unique index over the non-terminal states,
built from ``domain.transitions.TERMINAL_STATES`` so the predicate cannot drift
from the state machine. Two concurrent detections of the same failed payment
produce one case, and the second insert fails at the database rather than
succeeding and doubling every subsequent bound. Once a case reaches a terminal
state the index no longer covers it, which is deliberate: a payment that failed
again in September deserves a new case.

**Counters only ever increase, and only so far.** ``counters_within_bounds`` is a
schema-level backstop at ceilings well above the configured bounds. The configured
bounds themselves are enforced in the policy layer, because they are
merchant-configurable and a ``CHECK`` cannot be. The point of the ceiling is that a
bug in the policy layer produces a failed transaction instead of eleven messages.

``audit_seq`` lives here rather than on ``audit_record`` because gap-free per-case
sequencing needs a counter that increments inside the same transaction as the
insert and rolls back with it. A Postgres sequence has gaps by design and
``max(seq)+1`` races; a counter on a row already held under ``FOR UPDATE`` does
neither.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.enums import (
    CaseState,
    DiagnosisMethod,
    Provenance,
    RiskCause,
    TerminalReason,
)
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models.base import (
    CONFIDENCE,
    MONEY,
    TIMESTAMPTZ,
    RowBase,
    enum_check,
)

__all__ = [
    "MAX_AUDIT_CEILING",
    "SCHEMA_COUNTER_CEILING",
    "TERMINAL_STATE_SQL",
    "Diagnosis",
    "RecoveryCase",
]

TERMINAL_STATE_SQL: str = ", ".join(f"'{state.value}'" for state in sorted(TERMINAL_STATES))
"""Rendered from the state machine's own declaration. If a terminal state is added
to ``domain.transitions``, regenerating the migration moves the index with it."""

SCHEMA_COUNTER_CEILING: int = 10
"""Backstop, not the bound. ``MAX_RECOVERY_ATTEMPTS`` defaults to 3 and is
configurable per merchant; this is the number above which the row is refused
outright because no legitimate configuration reaches it."""

MAX_AUDIT_CEILING: int = 10_000
"""Per-case audit sequence ceiling. A case that has produced ten thousand audit
records is in a loop, and the transaction that would write the next one should
fail loudly rather than fill a table."""


class RecoveryCase(RowBase):
    """One payment at risk, tracked from detection to a terminal state."""

    __tablename__ = "recovery_case"

    state: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    """Optimistic concurrency. Two writers who read the same case both attempt the
    increment; one loses and retries against the state it did not see."""

    provider_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(Text)

    payment_amount: Mapped[int] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    customer_key: Mapped[str] = mapped_column(Text, nullable=False)
    """Keyed non-reversible hash of the contact. Joins to ``customer_consent`` so
    an opt-out recorded on one case suppresses contact on all of them."""

    customer_contact_masked: Mapped[str | None] = mapped_column(Text)
    """For display and support. Never the full identifier — the cleartext exists
    only inside the encrypted ``webhook_event`` payload."""

    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_event.id", ondelete="RESTRICT")
    )
    """Which event opened the case. Also how execution finds the ciphertext it must
    decrypt just in time to build a notifying payment link."""

    detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    window_end_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    """Persisted rather than recomputed from ``RECOVERY_WINDOW_DURATION`` at read
    time: changing the bound must not silently reopen or expire live cases."""

    executed_action_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    customer_message_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    decision_cycle_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """Set at the transition into EXECUTING, before the provider call. The
    cooldown is measured from this, so a crash mid-call still costs an attempt
    rather than allowing an immediate second one."""

    audit_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    human_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT")
    )
    human_assigned_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """A human owner suspends automated action entirely — the ``HUMAN_OWNERSHIP``
    policy check fails while this is set."""

    verified_payment_status: Mapped[str | None] = mapped_column(Text)
    """From the last authoritative provider read. Provider status vocabulary, so
    ``TEXT`` with no ``CHECK``: it is theirs to extend."""

    verified_amount_refunded: Mapped[int | None] = mapped_column(MONEY)
    risk_flagged: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="REAL")
    """``SYNTHETIC`` propagates to every surface and export. A figure is only
    ``REAL`` when every observation behind it is."""

    synthetic_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("synthetic_run.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        # THE one-open-case invariant (P5, and the reason a duplicate detection
        # cannot double a customer's messages). Partial, so a terminal case does
        # not block a genuinely new failure on the same payment.
        Index(
            "one_open_case_per_payment",
            "merchant_id",
            "provider_payment_id",
            unique=True,
            postgresql_where=text(f"state NOT IN ({TERMINAL_STATE_SQL})"),
        ),
        # Schema-level backstop above the configured bounds. The middle clause is
        # the one that protects a customer: a customer-visible message is always
        # an executed action, so the message count can never outrun it.
        CheckConstraint(
            f"executed_action_count <= {SCHEMA_COUNTER_CEILING} "
            "AND customer_message_count <= executed_action_count "
            f"AND decision_cycle_count <= {SCHEMA_COUNTER_CEILING} "
            f"AND audit_seq <= {MAX_AUDIT_CEILING}",
            name="counters_within_bounds",
        ),
        # A case with a non-positive amount is not revenue at risk, it is a bug.
        CheckConstraint("payment_amount > 0", name="payment_amount_positive"),
        CheckConstraint(
            "executed_action_count >= 0 AND customer_message_count >= 0 "
            "AND decision_cycle_count >= 0 AND audit_seq >= 0",
            name="counters_nonnegative",
        ),
        enum_check("recovery_case", "state", CaseState),
        enum_check("recovery_case", "terminal_reason", TerminalReason),
        enum_check("recovery_case", "provenance", Provenance),
        # Reason: the lifecycle sweeper scans non-terminal cases whose window is
        # closing, per merchant. This is the hottest periodic query in the system.
        Index("ix_recovery_case_merchant_id_state_window_end_at", "merchant_id", "state",
              "window_end_at"),
        # Reason: cohort aggregation and dashboard ordering, both of which are
        # "this merchant's cases, newest first".
        Index("ix_recovery_case_merchant_id_detected_at", "merchant_id", "detected_at"),
        # Reason: the opt-out sweep and the cross-case consent join.
        Index("ix_recovery_case_merchant_id_customer_key", "merchant_id", "customer_key"),
    )


class Diagnosis(RowBase):
    """Why this payment failed, and how much that answer is trusted.

    One active diagnosis per decision cycle, enforced by a partial unique index
    rather than by the code that writes it. A second active diagnosis for one cycle
    would mean two different causes could each justify a different action, and
    nothing in the record would say which one the recommendation used.

    ``method`` distinguishes a deterministic mapping-table hit from an AI-assisted
    guess from a rejected AI response. It is the column that makes the AI boundary
    auditable: a ``REJECTED_AI_OUTPUT`` row with cause ``UNKNOWN`` is the system
    working correctly.
    """

    __tablename__ = "diagnosis"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(CONFIDENCE, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """The provider fields the cause was derived from. Masked — evidence is shown
    on the dashboard and written into audit records."""

    decision_cycle: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ai_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_invocation.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    substituted_to_unknown: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    """True when confidence fell below ``DIAGNOSIS_CONFIDENCE_FLOOR`` and the cause
    was replaced with ``UNKNOWN``. The original is kept in ``evidence`` so the
    substitution is reviewable rather than invisible."""

    __table_args__ = (
        # Exactly one active diagnosis per cycle (R3.C4) as a database fact.
        Index(
            "one_active_diagnosis_per_cycle",
            "case_id",
            "decision_cycle",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        CheckConstraint("decision_cycle >= 0", name="decision_cycle_nonnegative"),
        enum_check("diagnosis", "cause", RiskCause),
        enum_check("diagnosis", "method", DiagnosisMethod),
        # Reason: the dashboard and the memory writer both read "the active
        # diagnosis for this case", and the superseded ones are history.
        Index("ix_diagnosis_case_id_decision_cycle", "case_id", "decision_cycle"),
    )

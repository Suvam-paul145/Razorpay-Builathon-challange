"""The audit record and the AI invocation log.

``audit_record`` is append-only, and that is enforced by the database rather than
by convention: the application role has no ``UPDATE``, ``DELETE`` or ``TRUNCATE``
grant, and a ``BEFORE UPDATE OR DELETE`` trigger raises on top of that. Two
mechanisms, because a grant can be restored by a careless migration and a trigger
cannot be dropped by accident in the same way. Both are installed by their own
migration; the model here only declares the shape.

There is deliberately no hash chain. It would detect out-of-band tampering by
someone with direct database access — a party who can also rewrite the chain. The
right answer to that threat is shipping records to append-only external storage,
and until that exists, a chain would be reassurance rather than protection.

**Sequence numbers.** ``seq`` is allocated from ``recovery_case.audit_seq`` inside
the same transaction as the insert, by a writer already holding the case row under
``FOR UPDATE``. A Postgres sequence would have gaps (allocations are not rolled
back) and ``max(seq)+1`` would race. ``UNIQUE (case_id, seq)`` enforces the part
that allocation alone cannot: no duplicates.

Records that are not about a case — a rejected signature, a rate limit, a failed
login — carry ``case_id = NULL`` and ``seq = NULL`` and rely on their correlation
id instead. The ``CHECK`` below makes the two nullable columns move together, so a
record cannot claim a position in a sequence it is not part of.

``ai_invocation`` gets one row per invocation *including failures*. A timeout that
left no row would make the AI layer look more reliable than it is, and the
deterministic-fallback rate is the number that says whether the AI path is
carrying its weight.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState
from revora.persistence.models.base import (
    AUDIT_TIMESTAMP,
    CONFIDENCE,
    RowBase,
    enum_check,
)

__all__ = ["AiInvocation", "AuditRecord"]


class AuditRecord(RowBase):
    """One durable explanation of one thing that happened.

    The audit log *is* the domain observability for this system — there is no
    metrics or tracing backend — so the columns are wide enough to answer "why did
    this customer get this message" without joining six tables under time pressure.
    """

    __tablename__ = "audit_record"

    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT")
    )
    seq: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    """Plain ``TEXT``. The vocabulary is large, it grows with every requirement that
    names a new record type, and pinning it in a ``CHECK`` here would mean a
    migration every time a new audited occurrence is added."""

    actor: Mapped[str] = mapped_column(Text, nullable=False)
    """``NOT NULL``. Every record names who or what caused it — a component name, a
    user id, or the provider. An unattributed audit record cannot answer the only
    question anyone asks of it."""

    previous_state: Mapped[str | None] = mapped_column(Text)
    new_state: Mapped[str | None] = mapped_column(Text)
    diagnosis: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    decision: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    policy_result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    action: Mapped[str | None] = mapped_column(Text)
    action_result: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """``NOT NULL`` even for records with no case. It is the only thread that ties a
    rejected signature to the retry that later succeeded."""

    occurred_at: Mapped[datetime] = mapped_column(AUDIT_TIMESTAMP, nullable=False)
    """``TIMESTAMPTZ(3)``. Millisecond precision, because several records for one
    case can land inside the same second and their order is the point."""

    truncated_fields: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    """Names of fields cut to ``MAX_AUDIT_FIELD_LENGTH``. Recording *that* a field
    was truncated keeps a shortened value from being read as the whole value."""

    __table_args__ = (
        # THE gap-free ordering constraint (P12). Allocation from
        # recovery_case.audit_seq gives strictly increasing and gap-free; this
        # gives no duplicates.
        UniqueConstraint("case_id", "seq", name="uq_audit_record_case_id_seq"),
        # A record either belongs to a case sequence or it does not. Half-belonging
        # would leave a gap that looks like a deleted record.
        CheckConstraint(
            "(case_id IS NULL) = (seq IS NULL)",
            name="case_and_seq_together",
        ),
        CheckConstraint("seq IS NULL OR seq >= 1", name="seq_positive"),
        enum_check("audit_record", "previous_state", CaseState),
        enum_check("audit_record", "new_state", CaseState),
        enum_check("audit_record", "action", CandidateAction),
        # Reason: trace one inbound event end to end, across every component and
        # every asynchronous job it spawned.
        Index("ix_audit_record_correlation_id", "correlation_id"),
        # Reason: the audit export and the retention sweep, both per merchant over
        # a time range.
        Index("ix_audit_record_merchant_id_occurred_at", "merchant_id", "occurred_at"),
    )


class AiInvocation(RowBase):
    """One call to the reasoning layer, successful or not.

    ``influenced_recommendation`` is the column that makes the AI boundary
    measurable: it is false for every rejected or timed-out invocation, and it can
    only be true for the three sanctioned uses. No row here can be the reason an
    external effect happened — that authority belongs to ``policy_decision``.
    """

    __tablename__ = "ai_invocation"

    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT")
    )
    prompt_contract_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Which versioned, code-declared contract governed the call. The contract's
    allow-list is what stops a field reaching the model that should not."""

    model_id: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    """Which gate the invocation reached and what happened there. Free text rather
    than an enum because the gate vocabulary belongs to the reasoning layer, which
    does not exist yet; task 13 pins it."""

    influenced_recommendation: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    raw_response_truncated: Mapped[str | None] = mapped_column(Text)
    """A rejected response, cut to ``AI_RAW_CAPTURE_LIMIT``. Kept because "the model
    returned something we refused" is only diagnosable if the something is visible."""

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        Index("ix_ai_invocation_case_id", "case_id"),
        # Reason: the AI reliability report counts invocations and fallbacks per
        # merchant over a window.
        Index("ix_ai_invocation_merchant_id_created_at", "merchant_id", "created_at"),
    )

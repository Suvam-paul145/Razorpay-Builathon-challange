"""The customer response loop's four tables: token, signal, suppression, promise.

A customer has no session and never will, so this module holds a second, deliberately
weaker credential and the three records a holder of one can produce or cause. Migration
``0008`` installed all four; these declarations exist so autogenerate has something to
compare against and so the enum registry covers the new columns. Every constraint below
is spelled the way ``0008`` spelled it — a difference here is a spurious diff there.

**Four requirements are enforced by shape rather than by code**, and those are the ones
worth reading the declarations for rather than the docstrings:

* :class:`CustomerSignal` has **no ``amount``, no ``instalment_count`` and no
  ``schedule`` column**. R22.C1 says a Partial_Arrangement_Request carries none of
  those. A ``CHECK`` would say "do not store this"; an absent column means there is
  nowhere to store it, so no code path can accept one even by mistake.
* :class:`ContactSuppression` has **no ``expires_at`` column**. R21.C2's "no expiry
  instant" is the absence of the column, not a nullable one nobody is supposed to set —
  the second form is a column something eventually populates.
* :class:`CustomerAccessToken` carries the **partial** unique index
  ``one_live_token_per_case`` over the unrevoked rows, which is R18.C14. Expiry cannot
  be in the predicate because it needs ``now()``, so minting a replacement for an
  expired predecessor revokes it with ``EXPIRED_SUPERSEDED`` in the same transaction.
  That makes the supersession auditable instead of implicit.
* :class:`PromiseToPay` carries ``follow_up_within_window``, which is half of P42 as a
  database fact: a Follow_Up_Instant at or past the recovery window end cannot be
  stored, whatever the clamp arithmetic computes.

``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` is deliberately **not** a constraint here. It is a
configurable bound, and encoding today's value of 5 in the schema would make raising it
a migration. The durable enforcement is
:meth:`~revora.persistence.repositories.customer.CustomerAccessTokenRepository.increment_accepted_submissions`,
which compares against the configured value inside the same statement that increments.
``MAX_PROMISES_PER_CASE`` is the opposite call and is recorded as one on
``uq_promise_to_pay_merchant_id_case_id``: today's value of 1 *is* encoded, as a
backstop behind the application check.

Nothing here holds a reversible copy of a token secret. ``secret_hash`` is
``HMAC-SHA256(signing_key, token_id ‖ secret)`` and ``secret_hash_length`` refuses
anything that is not 32 bytes, so a hex or base64 copy fails at insert rather than
sitting in the column looking plausible (R18.C3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    HardStopReason,
    PromiseStatus,
    Provenance,
    TokenRevocationReason,
)
from revora.persistence.models.base import TIMESTAMPTZ, RowBase, enum_check

__all__ = [
    "DELAY_NOTE_MAX_LENGTH",
    "FOLLOW_UP_PENDING_STATUS_SQL",
    "SECRET_HASH_BYTES",
    "ContactSuppression",
    "CustomerAccessToken",
    "CustomerSignal",
    "PromiseToPay",
]

SECRET_HASH_BYTES: int = 32
"""HMAC-SHA256 output length. The check built from it is what stops a truncated or a
hex-encoded copy being stored in a column whose whole purpose is that it is not
reversible (R18.C3)."""

DELAY_NOTE_MAX_LENGTH: int = 500
"""``DELAY_NOTE_MAX_LENGTH`` as a backstop, and the one configurable bound on
``customer_signal`` that is encoded in the schema. Encoded because the truncation R20.C2
performs is lossy anyway, so raising the bound only ever affects future rows."""

FOLLOW_UP_PENDING_STATUS_SQL: str = ", ".join(
    f"'{member.value}'"
    for member in (PromiseStatus.RECORDED, PromiseStatus.FOLLOW_UP_SCHEDULED)
)
"""The two statuses the promise sweep scans for, rendered from the enum rather than
restated, so a rename cannot leave the index predicate matching nothing."""


class CustomerAccessToken(RowBase):
    """The second credential: one case's worth of authority and nothing else.

    Bounded so narrowly that its compromise costs one case's amount and one recovery
    opportunity. ``token_id`` is the public handle and is the only form that appears in
    a log line or an audit record; the secret has no reversible representation anywhere.

    ``key_version`` records which signing secret minted the row and is observability
    only. Verification tries every active secret (R29.C14), so this column is never a
    filter on the hot path — making it one would reintroduce the timing distinction the
    accumulate-and-never-break loop exists to remove.
    """

    __tablename__ = "customer_access_token"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(Text, nullable=False)
    """26-char unpadded lowercase base32 of 16 random bytes. Separately random rather
    than derived from the secret, so the lookup handle leaks nothing about it."""

    secret_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    """``HMAC-SHA256(signing_key, token_id ‖ secret)``. Keyed, so a database dump alone
    does not permit offline verification, and the only persisted trace of the secret."""

    key_version: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    """The earlier of ``issued_at + CUSTOMER_TOKEN_LIFETIME`` and the case's
    ``window_end_at``, never extended."""

    accepted_submission_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    """Incremented under a row lock in the same transaction as the signal insert. This
    counter, not the rate limiter, is the durable bound of R19.C5 — no number of
    replicas can exceed it."""

    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    approved_action: Mapped[str] = mapped_column(Text, nullable=False)
    """The approved candidate action whose execution the token accompanies (R18.C12)."""

    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="validity_window_positive"),
        CheckConstraint(
            "accepted_submission_count >= 0", name="accepted_submission_count_nonnegative"
        ),
        # A revoked token names its reason, and an unrevoked one cannot carry a stale
        # one. Both directions, so neither half is storable alone.
        CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)",
            name="revocation_reason_iff_revoked",
        ),
        # The hash is a hash. A hex or base64 copy would be 64 or 44 bytes and fails
        # here rather than sitting in the column looking plausible.
        CheckConstraint(
            f"octet_length(secret_hash) = {SECRET_HASH_BYTES}", name="secret_hash_length"
        ),
        enum_check("customer_access_token", "revocation_reason", TokenRevocationReason),
        enum_check("customer_access_token", "approved_action", CandidateAction),
        # One row per handle, and the key every verification looks up by.
        UniqueConstraint(
            "merchant_id", "token_id", name="uq_customer_access_token_merchant_id_token_id"
        ),
        # INVARIANT: at most one live token per case (R18.C14). Partial on the
        # unrevoked rows, because expiry needs now() and so cannot be indexed — which
        # is why an expired predecessor must be revoked with EXPIRED_SUPERSEDED rather
        # than left to fall out of the predicate on its own.
        Index(
            "one_live_token_per_case",
            "merchant_id",
            "case_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Reason: the dashboard read of a case's tokens, and the bulk revoke on a
        # terminal transition or a persisted suppression (R18.C8, R21.C10).
        Index("ix_customer_access_token_merchant_id_case_id", "merchant_id", "case_id"),
    )


class CustomerSignal(RowBase):
    """What a customer said on the response page, as evidence rather than as an order.

    Every row is an input to the next decision and none of them is a decision. A signal
    schedules nothing and transitions nothing by itself — the worker applies every
    consequence through ``apply_transition``, which stays the only writer of
    ``recovery_case.state``.

    ``delay_reason_note`` is inert text: never evaluated, never interpolated into a
    query or a provider request, never rendered as markup. ``note_redacted_at`` is set
    by the retention sweep together with the config version it applied, and
    ``redacted_note_is_absent`` makes the claim honest — a redacted note is gone, not
    merely marked.
    """

    __tablename__ = "customer_signal"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The actor, in the form R18.C11 permits: the handle, never the secret."""

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    delay_reason: Mapped[str | None] = mapped_column(Text)
    delay_reason_note: Mapped[str | None] = mapped_column(Text)
    note_truncated: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    note_redacted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    retention_config_version: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, server_default="REAL")
    submitted_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            f"char_length(delay_reason_note) <= {DELAY_NOTE_MAX_LENGTH}",
            name="delay_reason_note_within_max_length",
        ),
        # A delay-reason signal carries one.
        CheckConstraint(
            f"kind <> '{CustomerSignalKind.DELAY_REASON.value}' OR delay_reason IS NOT NULL",
            name="delay_reason_present_for_delay_reason_kind",
        ),
        # The kinds do not overlap: a promise is not a stated reason wearing a date.
        CheckConstraint(
            f"kind <> '{CustomerSignalKind.PROMISE_TO_PAY.value}' OR delay_reason IS NULL",
            name="promise_carries_no_delay_reason",
        ),
        # Without this the retention sweep could report a redaction it had not made.
        CheckConstraint(
            "note_redacted_at IS NULL OR delay_reason_note IS NULL",
            name="redacted_note_is_absent",
        ),
        enum_check("customer_signal", "kind", CustomerSignalKind),
        enum_check("customer_signal", "delay_reason", DelayReason),
        enum_check("customer_signal", "provenance", Provenance),
        # Reason: the per-case read the dashboard and Recovery_Memory both perform, and
        # the MAX_CUSTOMER_SIGNALS_PER_CASE count of R19.C7.
        Index(
            "ix_customer_signal_merchant_id_case_id_submitted_at",
            "merchant_id",
            "case_id",
            "submitted_at",
        ),
        # Reason: the retention sweep's scan for notes past CUSTOMER_DATA_RETENTION
        # (R29.C10). Partial, because most signals carry no note and indexing them
        # would make the sweep read every row it has no work to do on.
        Index(
            "ix_customer_signal_notes_for_retention",
            "merchant_id",
            "submitted_at",
            postgresql_where=text("delay_reason_note IS NOT NULL"),
        ),
        # NOTE: there is deliberately no amount column, no instalment_count column and
        # no schedule column. See the module docstring — R22.C1 is their absence.
    )


class ContactSuppression(RowBase):
    """A permanent end to contact on one scope, releasable only by a named person.

    ``scope_key`` is ``sha256(customer_key ‖ order_id or case_id)`` — a hash rather than
    a composite of readable parts, so the column can be indexed and compared without
    holding a second copy of the customer key beside the order id. The preimage is
    recoverable from the ``recovery_case`` row the suppression names.

    There is no ``expires_at``. A suppression ends when a person ends it, and
    ``release_names_a_user`` is both halves of that: an anonymous release is not
    storable, and neither is an approver attached to a suppression still in force.
    """

    __tablename__ = "contact_suppression"

    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    origin_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    customer_signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_signal.id", ondelete="RESTRICT"), nullable=False
    )
    """The signal that caused it. A suppression with no evidence behind it would be
    indistinguishable from one somebody added by hand."""

    hard_stop_reason: Mapped[str] = mapped_column(Text, nullable=False)
    suppressed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    released_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT")
    )
    release_config_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # R21.C2, on the same terms model_promotion.approving_user_id does it.
        CheckConstraint(
            "(released_at IS NULL) = (released_by_user_id IS NULL)",
            name="release_names_a_user",
        ),
        CheckConstraint(
            "released_at IS NULL OR released_at >= suppressed_at",
            name="release_not_before_suppression",
        ),
        enum_check("contact_suppression", "hard_stop_reason", HardStopReason),
        # One suppression per scope, so a second hard stop on the same scope is
        # idempotent rather than a second row nobody reconciles.
        UniqueConstraint(
            "merchant_id", "scope_key", name="uq_contact_suppression_merchant_id_scope_key"
        ),
        # Reason: the CUSTOMER_OPTED_OUT policy check looks up "is this scope
        # suppressed right now" on every decision that could produce a
        # customer-visible action. Partial on the rows still in force, because a
        # released suppression is history and must not be read by the hot path.
        Index(
            "ix_contact_suppression_in_force",
            "merchant_id",
            "scope_key",
            postgresql_where=text("released_at IS NULL"),
        ),
        # NOTE: there is deliberately no expires_at column. See the module docstring.
    )


class PromiseToPay(RowBase):
    """A stated intention to pay by a date, with the follow-up it schedules.

    ``window_end_at_snapshot`` is the recovery window end as it stood when the promise
    was recorded, snapshotted so ``follow_up_within_window`` is a fact about this row
    rather than a join to a table that keeps moving — and R2.C5 means it cannot
    legitimately move anyway.

    ``received_representation`` retains the submitted string as it arrived (R23.C1)
    beside the UTC instant, on the same terms R16.C13 applies to a payment event
    timestamp: a timezone read wrongly is only diagnosable if what arrived is still
    there.

    ``BEYOND_WINDOW_ESCALATED`` is terminal for the promise and schedules nothing,
    which ``escalated_schedules_nothing`` makes structural: the window is never
    extended, so a promise dated past it is a case for a person.
    """

    __tablename__ = "promise_to_pay"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recovery_case.id", ondelete="RESTRICT"), nullable=False
    )
    customer_signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_signal.id", ondelete="RESTRICT"), nullable=False
    )
    promise_date: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    received_representation: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """The clamped Follow_Up_Instant. ``NULL`` when nothing was scheduled."""

    window_end_at_snapshot: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    kept_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    missed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    seconds_promise_to_payment: Mapped[int | None] = mapped_column(BigInteger)
    """R23.C10. Signed, and ``BIGINT`` rather than ``INTEGER``: paying early is normal,
    so a negative interval is a correct measurement and not an error to clamp away."""

    voided_by_terminal_state: Mapped[str | None] = mapped_column(Text)
    """R23.C12: the terminal state that voided a still-``RECORDED`` promise."""

    __table_args__ = (
        # Half of P42 as a database fact, which is what makes the window's
        # immutability survive a bug in the clamp rather than depend on it.
        CheckConstraint(
            "follow_up_at IS NULL OR follow_up_at < window_end_at_snapshot",
            name="follow_up_within_window",
        ),
        # An escalated promise scheduled nothing (R23.C5, C6).
        CheckConstraint(
            f"status <> '{PromiseStatus.BEYOND_WINDOW_ESCALATED.value}' "
            "OR follow_up_at IS NULL",
            name="escalated_schedules_nothing",
        ),
        # Ordering only. PROMISE_MIN_LEAD_TIME is configurable and checked in
        # application code; a promise dated before its own recording is not a
        # lead-time question.
        CheckConstraint("promise_date > recorded_at", name="promise_date_after_recording"),
        CheckConstraint(
            f"(status = '{PromiseStatus.KEPT.value}') = (kept_at IS NOT NULL)",
            name="kept_at_iff_kept",
        ),
        enum_check("promise_to_pay", "status", PromiseStatus),
        enum_check("promise_to_pay", "voided_by_terminal_state", CaseState),
        # MAX_PROMISES_PER_CASE = 1. This encodes today's value of a configurable
        # bound, which is a deliberate coupling and is recorded as one: R23.C7's
        # rejection is checked in application code against the configured value, and
        # this is the backstop behind it. Raising the bound needs a later migration.
        UniqueConstraint(
            "merchant_id", "case_id", name="uq_promise_to_pay_merchant_id_case_id"
        ),
        # Reason: the promise sweep's scan for a reached Follow_Up_Instant (R23.C13).
        # Partial over the two statuses that can still have one.
        Index(
            "ix_promise_to_pay_due_for_follow_up",
            "merchant_id",
            "follow_up_at",
            postgresql_where=text(f"status IN ({FOLLOW_UP_PENDING_STATUS_SQL})"),
        ),
    )

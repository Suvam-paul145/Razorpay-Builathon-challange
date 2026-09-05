"""The four tables the customer response loop stands on.

A customer has no session and never will, so this migration adds a second credential
(``customer_access_token``) and the three records a holder of one can produce or cause:
``customer_signal``, ``contact_suppression`` and ``promise_to_pay``.

**Several requirements are enforced here by shape rather than by code**, and those are the
ones worth reading the DDL for:

* ``customer_signal`` has **no ``amount``, no ``instalment_count`` and no ``schedule``
  column**. R22.C1 says a Partial_Arrangement_Request carries none of those; a check
  constraint would say "do not store this", whereas an absent column means there is nowhere
  to store it and no code path that can accidentally accept one.
* ``contact_suppression`` has **no ``expires_at`` column**. R21.C2's "no expiry instant" is
  the absence of the column, not a nullable one nobody is supposed to set — the second form
  is a column something eventually populates.
* ``contact_suppression`` carries ``CHECK ((released_at IS NULL) = (released_by_user_id IS
  NULL))``, so a release always names a person, on the same terms
  ``model_promotion.approving_user_id`` does.
* ``promise_to_pay`` carries ``CHECK (follow_up_at IS NULL OR follow_up_at <
  window_end_at_snapshot)``, which is half of P42 as a database fact: a Follow_Up_Instant at
  or past the recovery window end cannot be stored, whatever the clamp arithmetic does.
* ``customer_access_token`` carries a **partial** ``UNIQUE (merchant_id, case_id) WHERE
  revoked_at IS NULL``, which is R18.C14 — at most one live token per case. Expiry cannot be
  in the predicate because it needs ``now()``, so minting a replacement for an expired
  predecessor revokes it with ``EXPIRED_SUPERSEDED`` in the same transaction. That makes the
  supersession auditable instead of implicit.

**Five existing tables are also altered, and one of those alterations is a mechanism rather
than a record.** ``recovery_case`` gains ``next_review_at`` with a check that pins it inside
the recovery window; ``ai_invocation`` gains a nullable ``call_kind``; and
``execution_intent`` gains ``effect_kind``, whose only reason to exist is that
``ix_execution_intent_unresolved`` is rebuilt with ``effect_kind = 'PAYMENT_LINK_CREATE'`` in
its predicate. A resend is re-readable by nothing, so an ``UNCERTAIN`` resend intent is
permanently unresolvable by provider read — and the index predicate is what keeps such a row
*out of the set the reconciliation sweep scans*, rather than skipped by a branch someone can
delete. See ``ExecutionEffectKind`` for the provider fact behind it.

**The last two alterations carry a data migration, and it is the only irreversible arithmetic
here.** ``candidate_estimate`` and ``recommendation_candidate`` lose the blended
``action_cost`` and gain ``financial_cost`` and ``communication_cost`` with a method label
each (R31.C1). Every existing row's whole ``action_cost`` becomes its ``financial_cost``, its
``communication_cost`` becomes zero, and both labels become ``COST_SPLIT_NOT_MEASURED``
(R31.C9) — because no measurement of the split exists for a historical row and inferring one
from the action type would put a fabricated figure in the column whose purpose is to name
which cost caused an exclusion. The split cannot be recovered afterwards; the total can, which
is what the downgrade restores.

**``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` is deliberately not a check constraint.** It is a
configurable bound, and encoding today's value of 5 in the schema would turn raising it into
a migration. It is enforced under the row lock in the same transaction as the signal insert
(R19.C5). ``MAX_PROMISES_PER_CASE`` is the opposite call and is stated as such on the
``UNIQUE (merchant_id, case_id)`` below: today's value of 1 *is* encoded, as a backstop
behind the application check, and raising it needs a later migration to drop the index.

**Row-level security is created here rather than inherited.** Migration 0003 derives its
table list from the model metadata as it stood then and intersects it with the tables that
exist, so a table added now gets no policy from it. Each of the four enables RLS, creates its
own ``tenant_isolation`` policy, and grants ``revora_app`` — the last guarded by a role
existence check, because the managed instance connects as ``neondb_owner`` and has no
``revora_app`` role at all, exactly as 0002 and 0003 already handle.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    EstimationMethod,
    ExecutionEffectKind,
    HardStopReason,
    IntentState,
    PromiseStatus,
    Provenance,
    ReasoningCallKind,
    TokenRevocationReason,
)
from revora.persistence.models.base import MONEY, enum_check, nonnegative_money_check

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "revora_app"

_TENANT_EXPR = (
    "merchant_id = NULLIF(current_setting('revora.merchant_id', true), '')::uuid"
)
"""Identical to migration 0003's expression, deliberately repeated rather than imported.

A migration is a historical record of what was applied. Importing a shared constant would let
a later edit to that constant silently change what this migration is understood to have done,
which is the one property a migration must not have.
"""

_NEW_TABLES: tuple[str, ...] = (
    "customer_access_token",
    "customer_signal",
    "contact_suppression",
    "promise_to_pay",
)
"""In creation order: ``contact_suppression`` and ``promise_to_pay`` both reference
``customer_signal``, so the signal table has to exist first."""

_SECRET_HASH_BYTES = 32
"""HMAC-SHA256 output length. The check is what stops a truncated or a hex-encoded copy
being stored in a column whose whole purpose is that it is not reversible (R18.C3)."""

_DELAY_NOTE_MAX_LENGTH = 500
"""``DELAY_NOTE_MAX_LENGTH`` as a backstop, and the one configurable bound on
``customer_signal`` that is encoded. Encoded because the truncation R20.C2 performs is lossy
anyway, so raising the bound only ever affects future rows and needs no rewrite of old ones."""

_FOLLOW_UP_PENDING_STATUSES = ", ".join(
    f"'{member.value}'"
    for member in (PromiseStatus.RECORDED, PromiseStatus.FOLLOW_UP_SCHEDULED)
)
"""The two statuses the promise sweep scans for, generated from the enum rather than
restated, so a rename cannot leave the index predicate matching nothing."""

_UNRESOLVED_INTENT_STATES_SQL = ", ".join(
    f"'{member.value}'" for member in (IntentState.ATTEMPTED, IntentState.UNCERTAIN)
)
"""The predicate ``0001`` gave ``ix_execution_intent_unresolved``, reproduced from the enum.

Rendered here rather than imported from ``revora.persistence.models.execution`` for the same
reason ``_TENANT_EXPR`` is repeated: a migration records what was applied, and a later edit to
a shared constant must not change what this migration is understood to have done. Generated
from :class:`IntentState` rather than typed out so that a rename is a failure at import time
instead of a predicate that silently matches nothing."""

_RECONCILABLE_EFFECT_KIND_SQL = f"effect_kind = '{ExecutionEffectKind.PAYMENT_LINK_CREATE.value}'"
"""The clause added to that predicate, generated from the enum member for the same reason."""

_COST_SPLIT_TABLES: tuple[str, ...] = ("candidate_estimate", "recommendation_candidate")
"""The two tables that persist a blended ``action_cost``.

Read off the schema rather than taken from the design: ``0001`` created ``action_cost`` on
exactly these two tables and no later migration added a third, so this list is the complete
set and the ``UPDATE`` below leaves no blended figure behind anywhere.
"""

_SPLIT_MONEY_COLUMNS: tuple[str, ...] = ("financial_cost", "communication_cost")
_SPLIT_METHOD_COLUMNS: tuple[str, ...] = (
    "financial_cost_method",
    "communication_cost_method",
)

_MIGRATED_SPLIT_METHOD_SQL = f"'{EstimationMethod.COST_SPLIT_NOT_MEASURED.value}'"
"""The literal the data migration writes into both method columns, rendered from the enum
member rather than typed out, so a rename is an import-time failure instead of an ``UPDATE``
that writes a string no ``CHECK`` admits."""

_ESTIMATION_METHOD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("baseline_estimate", "method"),
    ("candidate_estimate", "method"),
)
"""Every column that already carried an ``EstimationMethod`` ``CHECK`` before this migration.

Both are widened, not only the one the design's DDL names. ``enum_check`` derives the
permitted set from the Python enum, so adding ``COST_SPLIT_NOT_MEASURED`` widens what
``baseline_estimate.method``'s model-declared constraint says too; leaving the installed
constraint at four members would make the model and the schema disagree about that column
for no reason anyone reading either would be able to reconstruct. Widening it permits a value
no estimator produces on a baseline — a weaker statement than the drift, and the same
statement ``ai_invocation.call_kind``'s nullability already makes.
"""


def _enum_check_sql(table: str, column: str, enum: type[StrEnum]) -> str:
    """The ``CHECK`` body :func:`enum_check` would build, as SQL text.

    ``enum_check`` returns a ``CheckConstraint`` for use inside ``create_table``; these three
    columns are added to tables that already exist, so what is needed is the condition on its
    own. Going through the helper rather than restating ``IN ('A','B')`` by hand is what keeps
    the constraint and the Python enum a single source: adding a member is one edit.
    """
    return str(enum_check(table, column, enum).sqltext)


def _enable_row_level_security() -> None:
    """Enable RLS, create the ``tenant_isolation`` policy, and grant ``revora_app``.

    The grant is wrapped in a role existence check for the same reason 0002 and 0003 wrap
    theirs: the managed instance connects as ``neondb_owner`` and has no ``revora_app`` role,
    and a migration that failed there would be a migration that only applies locally. RLS
    itself is enabled unconditionally — it costs nothing on a database whose only connection
    is the owner, and skipping it would make the local schema and the deployed schema differ
    in the one control that exists to catch a forgotten ``WHERE``.
    """
    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
        )

    grants = "".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};"
        for table in _NEW_TABLES
    )
    op.execute(
        f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    {grants}
  END IF;
END
$do$;
"""
    )


def _create_customer_access_token() -> None:
    op.create_table(
        "customer_access_token",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        # The public handle: 26-char base32 of 16 random bytes. Appears in logs and audit
        # records; the secret never does.
        sa.Column("token_id", sa.Text(), nullable=False),
        # HMAC-SHA256(signing_key, token_id || secret). R18.C3 forbids a reversible copy of
        # the secret anywhere, so this is the only persisted trace of it.
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        # Which signing secret minted it. Observability only — verification tries every
        # active secret (R29.C14), so this column is never a filter on the hot path.
        sa.Column("key_version", sa.Text(), nullable=False),
        sa.Column("issued_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "accepted_submission_count",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        # The approved candidate action whose execution the token accompanies (R18.C12).
        sa.Column("approved_action", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name=op.f("ck_customer_access_token_validity_window_positive"),
        ),
        sa.CheckConstraint(
            "accepted_submission_count >= 0",
            name=op.f("ck_customer_access_token_accepted_submission_count_nonnegative"),
        ),
        # A revoked token names its reason, and an unrevoked one cannot carry a stale one.
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)",
            name=op.f("ck_customer_access_token_revocation_reason_iff_revoked"),
        ),
        # The hash is a hash. A hex or base64 copy would be 64 or 44 bytes and would fail
        # here rather than sitting in the column looking plausible.
        sa.CheckConstraint(
            f"octet_length(secret_hash) = {_SECRET_HASH_BYTES}",
            name=op.f("ck_customer_access_token_secret_hash_length"),
        ),
        enum_check("customer_access_token", "revocation_reason", TokenRevocationReason),
        enum_check("customer_access_token", "approved_action", CandidateAction),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["recovery_case.id"],
            name=op.f("fk_customer_access_token_case_id_recovery_case"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_customer_access_token_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_access_token")),
        # One row per handle, and the key every verification looks up by.
        sa.UniqueConstraint(
            "merchant_id",
            "token_id",
            name="uq_customer_access_token_merchant_id_token_id",
        ),
    )
    # Reason: the dashboard read of a case's tokens, and the bulk revoke on a terminal
    # transition or a persisted suppression (R18.C8, R21.C10).
    op.create_index(
        "ix_customer_access_token_merchant_id_case_id",
        "customer_access_token",
        ["merchant_id", "case_id"],
        unique=False,
    )
    # INVARIANT: at most one live token per case (R18.C14).
    # Partial on the unrevoked rows. Expiry is deliberately not in the predicate — it would
    # need now(), which is not immutable and so not indexable — which is why minting a
    # replacement for an expired predecessor must revoke it with EXPIRED_SUPERSEDED in the
    # same transaction. The revoke is what makes the supersession auditable.
    op.create_index(
        "one_live_token_per_case",
        "customer_access_token",
        ["merchant_id", "case_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def _create_customer_signal() -> None:
    op.create_table(
        "customer_signal",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        # The actor, in the form R18.C11 permits: the handle, never the secret.
        sa.Column("token_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("delay_reason", sa.Text(), nullable=True),
        # Inert text (R20.C2): never evaluated, never interpolated into a query or a provider
        # request, never rendered as markup.
        sa.Column("delay_reason_note", sa.Text(), nullable=True),
        sa.Column("note_truncated", sa.Boolean(), server_default="false", nullable=False),
        # Set by the retention sweep, together with the config version it applied (R29.C10).
        sa.Column(
            "note_redacted_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("retention_config_version", sa.Text(), nullable=True),
        sa.Column("provenance", sa.Text(), server_default="REAL", nullable=False),
        sa.Column("submitted_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"char_length(delay_reason_note) <= {_DELAY_NOTE_MAX_LENGTH}",
            name=op.f("ck_customer_signal_delay_reason_note_within_max_length"),
        ),
        # A delay-reason signal carries one.
        sa.CheckConstraint(
            f"kind <> '{CustomerSignalKind.DELAY_REASON.value}' OR delay_reason IS NOT NULL",
            name=op.f("ck_customer_signal_delay_reason_present_for_delay_reason_kind"),
        ),
        # The kinds do not overlap: a promise is not a stated reason wearing a date.
        sa.CheckConstraint(
            f"kind <> '{CustomerSignalKind.PROMISE_TO_PAY.value}' OR delay_reason IS NULL",
            name=op.f("ck_customer_signal_promise_carries_no_delay_reason"),
        ),
        # A redacted note is gone, not merely marked. Without this the retention sweep could
        # report a redaction it had not performed.
        sa.CheckConstraint(
            "note_redacted_at IS NULL OR delay_reason_note IS NULL",
            name=op.f("ck_customer_signal_redacted_note_is_absent"),
        ),
        enum_check("customer_signal", "kind", CustomerSignalKind),
        enum_check("customer_signal", "delay_reason", DelayReason),
        enum_check("customer_signal", "provenance", Provenance),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["recovery_case.id"],
            name=op.f("fk_customer_signal_case_id_recovery_case"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_customer_signal_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_signal")),
        # NOTE: there is deliberately no amount column, no instalment_count column and no
        # schedule column. R22.C1 is enforced by their absence — a Partial_Arrangement_Request
        # is a piece of evidence and a reason to fetch a human, and there is nowhere for a
        # partial amount to be written even by mistake.
    )
    # Reason: the per-case read the dashboard and Recovery_Memory both perform, and the
    # MAX_CUSTOMER_SIGNALS_PER_CASE count of R19.C7.
    op.create_index(
        "ix_customer_signal_merchant_id_case_id_submitted_at",
        "customer_signal",
        ["merchant_id", "case_id", "submitted_at"],
        unique=False,
    )
    # Reason: the retention sweep's scan for notes past CUSTOMER_DATA_RETENTION (R29.C10).
    # Partial, because most signals carry no note and indexing them would make the sweep read
    # every row it has no work to do on.
    op.create_index(
        "ix_customer_signal_notes_for_retention",
        "customer_signal",
        ["merchant_id", "submitted_at"],
        unique=False,
        postgresql_where=sa.text("delay_reason_note IS NOT NULL"),
    )


def _create_contact_suppression() -> None:
    op.create_table(
        "contact_suppression",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        # sha256(customer_key || order_id or case_id). A hash rather than a composite of
        # readable parts, so the column can be indexed and compared without holding a second
        # copy of the customer key beside the order id. The preimage is recoverable from the
        # recovery_case row this suppression names.
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("origin_case_id", sa.UUID(), nullable=False),
        sa.Column("customer_signal_id", sa.UUID(), nullable=False),
        sa.Column("hard_stop_reason", sa.Text(), nullable=False),
        sa.Column("suppressed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("released_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("released_by_user_id", sa.UUID(), nullable=True),
        sa.Column("release_config_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # R21.C2: a release always names a person, on the same terms
        # model_promotion.approving_user_id does. Both directions, so neither an anonymous
        # release nor an approver attached to a suppression still in force is storable.
        sa.CheckConstraint(
            "(released_at IS NULL) = (released_by_user_id IS NULL)",
            name=op.f("ck_contact_suppression_release_names_a_user"),
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= suppressed_at",
            name=op.f("ck_contact_suppression_release_not_before_suppression"),
        ),
        enum_check("contact_suppression", "hard_stop_reason", HardStopReason),
        sa.ForeignKeyConstraint(
            ["customer_signal_id"],
            ["customer_signal.id"],
            name=op.f("fk_contact_suppression_customer_signal_id_customer_signal"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_contact_suppression_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_case_id"],
            ["recovery_case.id"],
            name=op.f("fk_contact_suppression_origin_case_id_recovery_case"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["released_by_user_id"],
            ["merchant_user.id"],
            name=op.f("fk_contact_suppression_released_by_user_id_merchant_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_suppression")),
        # One suppression per scope, so a second hard stop on the same scope is idempotent
        # rather than a second row nobody reconciles.
        sa.UniqueConstraint(
            "merchant_id",
            "scope_key",
            name="uq_contact_suppression_merchant_id_scope_key",
        ),
        # NOTE: there is deliberately no expires_at column. R21.C2's "no expiry instant" is
        # enforced by absence rather than by a nullable column nobody sets — a column that
        # exists but must stay NULL is a column something eventually populates.
    )
    # Reason: the CUSTOMER_OPTED_OUT policy check (check 5 in the R8.C2 order) looks up
    # "is this scope suppressed right now" on every decision that could produce a
    # customer-visible action. Partial on the rows still in force, because a released
    # suppression is history and must not be read by the hot path.
    op.create_index(
        "ix_contact_suppression_in_force",
        "contact_suppression",
        ["merchant_id", "scope_key"],
        unique=False,
        postgresql_where=sa.text("released_at IS NULL"),
    )


def _create_promise_to_pay() -> None:
    op.create_table(
        "promise_to_pay",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("customer_signal_id", sa.UUID(), nullable=False),
        sa.Column("promise_date", postgresql.TIMESTAMP(timezone=True), nullable=False),
        # The submitted string as received (R23.C1), retained beside the UTC instant on the
        # same terms R16.C13 applies to a payment event timestamp: a timezone read wrongly is
        # only diagnosable if what arrived is still there.
        sa.Column("received_representation", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # The clamped Follow_Up_Instant. NULL when nothing was scheduled.
        sa.Column("follow_up_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        # The recovery window end as it stood when the promise was recorded. Snapshotted so
        # the clamp below is a fact about this row rather than a join to a table that keeps
        # moving — and R2.C5 means it cannot legitimately move anyway.
        sa.Column(
            "window_end_at_snapshot",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column("recorded_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("kept_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("missed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        # R23.C10. Signed, and BIGINT rather than INTEGER: paying early is normal, so a
        # negative interval is a correct measurement and not an error to clamp away.
        sa.Column("seconds_promise_to_payment", sa.BigInteger(), nullable=True),
        # R23.C12: the terminal state that voided a still-RECORDED promise.
        sa.Column("voided_by_terminal_state", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Half of P42 as a database fact. A Follow_Up_Instant at or past the window end
        # cannot be stored, whatever the clamp arithmetic computes — which is what makes the
        # window's immutability (R2.C5) survive a bug in the clamp rather than depend on it.
        sa.CheckConstraint(
            "follow_up_at IS NULL OR follow_up_at < window_end_at_snapshot",
            name=op.f("ck_promise_to_pay_follow_up_within_window"),
        ),
        # An escalated promise scheduled nothing (R23.C5, C6).
        sa.CheckConstraint(
            f"status <> '{PromiseStatus.BEYOND_WINDOW_ESCALATED.value}' "
            "OR follow_up_at IS NULL",
            name=op.f("ck_promise_to_pay_escalated_schedules_nothing"),
        ),
        # Ordering only. PROMISE_MIN_LEAD_TIME is configurable and checked in application
        # code; a promise dated before its own recording is not a lead-time question.
        sa.CheckConstraint(
            "promise_date > recorded_at",
            name=op.f("ck_promise_to_pay_promise_date_after_recording"),
        ),
        sa.CheckConstraint(
            f"(status = '{PromiseStatus.KEPT.value}') = (kept_at IS NOT NULL)",
            name=op.f("ck_promise_to_pay_kept_at_iff_kept"),
        ),
        enum_check("promise_to_pay", "status", PromiseStatus),
        enum_check("promise_to_pay", "voided_by_terminal_state", CaseState),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["recovery_case.id"],
            name=op.f("fk_promise_to_pay_case_id_recovery_case"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_signal_id"],
            ["customer_signal.id"],
            name=op.f("fk_promise_to_pay_customer_signal_id_customer_signal"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_promise_to_pay_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promise_to_pay")),
        # MAX_PROMISES_PER_CASE = 1. This constraint encodes today's value of a configurable
        # bound, which is a deliberate coupling and is recorded as one: R23.C7's rejection is
        # checked in application code against the configured value, and this is the backstop
        # behind it. Raising the bound requires a later migration to drop this constraint.
        sa.UniqueConstraint(
            "merchant_id", "case_id", name="uq_promise_to_pay_merchant_id_case_id"
        ),
    )
    # Reason: the promise sweep's scan for a reached Follow_Up_Instant (R23.C13). Partial over
    # the two statuses that can still have one, generated from PromiseStatus so a rename
    # cannot leave the predicate matching nothing.
    op.create_index(
        "ix_promise_to_pay_due_for_follow_up",
        "promise_to_pay",
        ["merchant_id", "follow_up_at"],
        unique=False,
        postgresql_where=sa.text(f"status IN ({_FOLLOW_UP_PENDING_STATUSES})"),
    )


def _add_recovery_case_next_review_at() -> None:
    """``recovery_case.next_review_at``, its window check, and the sweeper's partial index.

    The check is the **second clause of P63 enforced by the database**: no persisted review
    instant can fall outside the recovery window, whatever the clamp arithmetic in
    ``handle_policy`` computes. It is expressible as a table ``CHECK`` because it compares two
    columns of the same row — ``window_end_at`` is persisted on ``recovery_case`` and R2.C5
    means it never moves, so this is a fact about the row rather than a join.

    Nullable, and NULL is the normal state: a case has a review instant only while it is
    waiting at ``POLICY_CHECK`` having chosen not to act. Every edge out of that state clears
    it (task 37.2), which is why the index predicate can rely on the state and the column
    together.
    """
    op.add_column(
        "recovery_case",
        sa.Column("next_review_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_recovery_case_review_within_window"),
        "recovery_case",
        "next_review_at IS NULL OR next_review_at <= window_end_at",
    )
    # Reason: the Review_Sweeper's scan for a reached review instant (R30.C5). Partial on both
    # conjuncts, because a case waiting at POLICY_CHECK is a small minority of a merchant's
    # rows and a full index would make the sweep read the rest. The state literal comes from
    # CaseState so a rename cannot leave the predicate matching nothing.
    op.create_index(
        "ix_recovery_case_due_for_review",
        "recovery_case",
        ["merchant_id", "next_review_at"],
        unique=False,
        postgresql_where=sa.text(
            f"state = '{CaseState.POLICY_CHECK.value}' AND next_review_at IS NOT NULL"
        ),
    )


def _add_execution_intent_effect_kind() -> None:
    """``execution_intent.effect_kind``, and the rebuilt index that is the actual mechanism.

    **The server default stays on the column after the backfill.** It is what backfills every
    existing row correctly — every intent written before ``0008`` was a link creation — and
    there is a real argument for dropping it afterwards: because the default is the *scanned*
    kind, a resend writer that omitted the column would record a create, and a create row is
    in the reconciliation set. A column with no default would turn that omission into a
    ``NOT NULL`` violation at insert time, which is louder.

    It stays for two reasons that outweigh it. First, ``alembic/env.py`` configures
    ``compare_server_default=True``, and the model declares this default; a column whose
    default was dropped here would show as a permanent autogenerate diff, which is how genuine
    drift stops being visible among the noise. Second, the omission that argument protects
    against is not guarded by the absence of a default anyway — the resend path passes
    ``effect_kind`` explicitly and P-level tests assert every resend intent row carries
    ``PAYMENT_LINK_RESEND``. The shape is the same one ``recovery_case.provenance`` already
    has: an enum column whose overwhelming majority value is the safe one.
    """
    op.add_column(
        "execution_intent",
        sa.Column(
            "effect_kind",
            sa.Text(),
            nullable=False,
            server_default=ExecutionEffectKind.PAYMENT_LINK_CREATE.value,
        ),
    )
    op.create_check_constraint(
        op.f("ck_execution_intent_effect_kind_enum"),
        "execution_intent",
        _enum_check_sql("execution_intent", "effect_kind", ExecutionEffectKind),
    )
    # The index is dropped and recreated rather than a second index created beside it. Both
    # reconcile_intents and promote_stale_intents read their candidates through this one
    # predicate, so narrowing it is what removes resend rows from the set being scanned — and
    # a future reader who drops the effect_kind filter from the query gets a sequential scan
    # and a failing performance assertion before they get a duplicate SMS. Columns and the
    # state clause are reproduced exactly as 0001 wrote them; the kind clause is all that is
    # added, and it is rendered from ExecutionEffectKind rather than typed out.
    op.drop_index("ix_execution_intent_unresolved", table_name="execution_intent")
    op.create_index(
        "ix_execution_intent_unresolved",
        "execution_intent",
        ["state", "attempt_started_at"],
        unique=False,
        postgresql_where=sa.text(
            f"state IN ({_UNRESOLVED_INTENT_STATES_SQL}) AND {_RECONCILABLE_EFFECT_KIND_SQL}"
        ),
    )


def _add_ai_invocation_call_kind() -> None:
    """``ai_invocation.call_kind``, nullable with a three-member check.

    Nullable, and that is a statement rather than a convenience: a row written before the
    Reasoning_Adapter existed genuinely does not know its kind, and backfilling one would
    assert a fact nobody recorded. ``enum_check``'s ``NULL`` passes by construction —
    ``NULL IN (...)`` is unknown — so the check admits the unknown rows and refuses a fourth
    kind (R27.C12).

    A separate column rather than encoding the kind into the existing ``prompt_contract_id``,
    because both are queried independently: "how many ``CAUSE_HYPOTHESIS`` calls fell back this
    week" should be a ``WHERE``, not a ``LIKE`` over a version string.
    """
    op.add_column("ai_invocation", sa.Column("call_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_ai_invocation_call_kind_enum"),
        "ai_invocation",
        _enum_check_sql("ai_invocation", "call_kind", ReasoningCallKind),
    )


def _nonnegative_money_constraint(table: str, column: str) -> None:
    """Install ``nonnegative_money_check``'s constraint on an already-existing table.

    Same reason :func:`_enum_check_sql` exists: the helper returns a ``CheckConstraint`` for
    use inside ``create_table``, and both its condition *and* its name are needed here
    separately. Both are taken from the helper rather than restated, so the constraint this
    migration installs is character-for-character the one the model declares — which is what
    the naming convention exists to guarantee and what task 34.5's models rely on.
    """
    constraint = nonnegative_money_check(column)
    op.create_check_constraint(
        op.f(f"ck_{table}_{constraint.name}"), table, str(constraint.sqltext)
    )


def _split_action_cost_into_two_terms() -> None:
    """Replace ``action_cost`` with ``financial_cost`` + ``communication_cost`` (R31.C1, C9).

    **The whole pre-split value goes into ``financial_cost``, ``communication_cost`` becomes
    zero, and both method columns become ``COST_SPLIT_NOT_MEASURED``.** Nothing infers a split
    from the action type. Putting ``MESSAGE_COMMUNICATION_COST`` into the communication term
    of every ``CUSTOMER_MESSAGE`` row would look far more plausible and would be strictly
    worse: it would place a number no measurement supports in the exact column whose only
    purpose is to say *which* cost caused an exclusion under MAX_COST_TO_VALUE_RATIO. The
    marking is what stops a reader taking a migrated zero for a measured zero (R31.C10), and a
    fabricated split would defeat it while still carrying it.

    **``action_cost`` is dropped rather than left nullable.** R31.C1 forbids persisting it on
    an estimate produced after this migration, and a column that exists but must not be
    written is a column something will write. Dropping it takes its two
    ``ck_*_action_cost_nonnegative`` constraints with it, which is why they are not dropped
    explicitly.

    The ordering inside the loop is the load-bearing part. The ``UPDATE`` reads ``action_cost``
    and writes ``financial_cost``, so it has to sit between the ``ADD COLUMN``s and the
    ``DROP COLUMN`` — and the money columns arrive with ``DEFAULT 0``, so a mistaken ordering
    would not fail, it would silently zero every historical cost figure in the database.

    ``recommendation_candidate`` gets the two method columns even though it carries no method
    column at all today, and ``candidate_estimate`` gets them beside its existing row-level
    ``method``. That is deliberate: R31.C5 wants a label per cost figure, and the single
    ``method`` column records ``weakest_method`` over the row, which cannot say that the
    financial term is a migrated non-measurement while the probability is real.
    """
    for table in _COST_SPLIT_TABLES:
        for column in _SPLIT_MONEY_COLUMNS:
            op.add_column(
                table,
                sa.Column(column, MONEY, nullable=False, server_default="0"),
            )
        for column in _SPLIT_METHOD_COLUMNS:
            # Nullable, and unlike execution_intent.effect_kind there is no server default to
            # backfill with: there is no correct value to guess for a row this migration has
            # not reached yet, and NOT NULL with no default cannot be added before the UPDATE
            # below runs. R31.C5's "leave neither label unset" is an obligation on the
            # estimator, and every row that exists when this migration finishes carries a
            # label — the UPDATE gives it one.
            op.add_column(table, sa.Column(column, sa.Text(), nullable=True))

        # Between the ADD COLUMNs and the DROP COLUMN. See the ordering note above.
        op.execute(
            f"UPDATE {table} SET "
            f"financial_cost = action_cost, "
            f"communication_cost = 0, "
            f"financial_cost_method = {_MIGRATED_SPLIT_METHOD_SQL}, "
            f"communication_cost_method = {_MIGRATED_SPLIT_METHOD_SQL}"
        )

        op.drop_column(table, "action_cost")

        for column in _SPLIT_MONEY_COLUMNS:
            _nonnegative_money_constraint(table, column)
        for column in _SPLIT_METHOD_COLUMNS:
            op.create_check_constraint(
                op.f(f"ck_{table}_{column}_enum"),
                table,
                _enum_check_sql(table, column, EstimationMethod),
            )

    # The two pre-existing EstimationMethod checks are dropped and recreated over the widened
    # enumeration. A CHECK cannot be altered in place, and ADD CONSTRAINT ... NOT VALID would
    # leave a constraint whose name suggests it is enforced.
    for table, column in _ESTIMATION_METHOD_COLUMNS:
        # op.f, because drop_constraint runs a bare name through the naming convention too
        # and would ask for ck_baseline_estimate_ck_baseline_estimate_method_enum.
        op.drop_constraint(op.f(f"ck_{table}_{column}_enum"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_{column}_enum"),
            table,
            _enum_check_sql(table, column, EstimationMethod),
        )


def upgrade() -> None:
    _create_customer_access_token()
    _create_customer_signal()
    _create_contact_suppression()
    _create_promise_to_pay()
    _enable_row_level_security()
    _add_recovery_case_next_review_at()
    _add_execution_intent_effect_kind()
    _add_ai_invocation_call_kind()
    _split_action_cost_into_two_terms()


_PRE_SPLIT_ESTIMATION_METHODS: tuple[EstimationMethod, ...] = (
    EstimationMethod.DETERMINISTIC,
    EstimationMethod.PRIOR_FALLBACK,
    EstimationMethod.UNCALIBRATED,
    EstimationMethod.DEFINITIONAL,
)
"""The four members ``EstimationMethod`` had before this migration widened it.

Named one by one rather than derived, and both halves of that are deliberate.

*Not* ``enum_check``/:func:`_enum_check_sql`, because those read the Python enum, which still
contains ``COST_SPLIT_NOT_MEASURED`` after a downgrade — the enum member is code, not schema,
and task 34.5 keeps it. Regenerating from it would restore the widened check, not the narrow
one, and the downgrade would be a no-op wearing a drop-and-recreate.

*Not* ``EstimationMethod`` minus ``COST_SPLIT_NOT_MEASURED`` either, which is the tempting
form. That expression says "everything current except one", and a member added by ``0009``
would silently enter a constraint whose whole job is to state what the schema looked like
before ``0008``. A closed list cannot drift that way.

Naming the members by attribute rather than typing the four strings keeps the one property the
derived forms have that a literal list does not: a rename in :class:`EstimationMethod` fails at
import time here, instead of leaving this migration installing a check over strings the enum no
longer contains.
"""


def _pre_split_estimation_method_sql(column: str) -> str:
    """The ``CHECK`` body ``0001`` installed on an ``EstimationMethod`` column.

    Rendered the way :func:`enum_check` renders — quoted column, comma-space separated,
    single-quoted values — so the restored constraint is character-for-character the one
    ``0001`` created and ``0008`` replaced, rather than an equivalent one that would show as a
    diff to anything comparing constraint text.
    """
    rendered = ", ".join(f"'{member.value}'" for member in _PRE_SPLIT_ESTIMATION_METHODS)
    return f'"{column}" IN ({rendered})'


def _role_guarded(statements: str) -> str:
    """Wrap privilege changes so they are skipped when ``revora_app`` does not exist.

    The same guard :func:`_enable_row_level_security` puts around its ``GRANT``, and for the
    same reason: the managed instance connects as ``neondb_owner`` and has no ``revora_app``
    role at all, so an unguarded ``REVOKE`` would make this downgrade one that only runs
    locally.
    """
    return f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    {statements}
  END IF;
END
$do$;
"""


def _refuse_if_anything_would_be_lost() -> None:
    """Every reason to refuse, evaluated before the first ``ALTER``.

    ``env.py`` runs the whole migration run in one transaction and Postgres has transactional
    DDL, so a refusal raised half-way through would in fact roll the dropped tables back. These
    checks are still ordered first, deliberately, because that argument is a property of the
    surrounding configuration rather than of this function: ``transaction_per_migration``, the
    autocommit block a future migration might need, and a non-Postgres backend are all things
    someone can change without reading this file, and each of them turns "it would have rolled
    back" into a half-dropped schema. A refusal that has issued no DDL is safe without needing
    that argument at all.

    Running them first buys nothing under ``alembic downgrade --sql``, where there is no
    connection to count rows on and this function raises ``AttributeError`` instead — the same
    thing ``0007``'s downgrade does, and the right outcome rather than a gap: a downgrade whose
    safety depends on what the rows say cannot be certified as a script written before they are
    read. Generate it online or not at all.

    Three of the four are the design's named pre-assertions (*Data Models — Downgrade*). The
    fourth guards the narrowing of the ``EstimationMethod`` checks, which Postgres would
    otherwise reject with ``is violated by some row`` — no count, no column, no reason.
    """
    connection = op.get_bind()

    # R21.C2. The one irreversible harm in this migration: a suppression with released_at NULL
    # is in force right now, the CUSTOMER_OPTED_OUT policy check reads it on every decision
    # that could produce a customer-visible action, and dropping the table deletes it. Nothing
    # downstream notices, because the absence of a suppression is indistinguishable from never
    # having been asked to stop.
    in_force = connection.execute(
        sa.text("SELECT count(*) FROM contact_suppression WHERE released_at IS NULL")
    ).scalar_one()
    if in_force:
        raise RuntimeError(
            f"refusing to downgrade 0008: contact_suppression holds {in_force} suppression(s) "
            "still in force (released_at IS NULL). Dropping the table would delete them and "
            "silently re-permit contact to customers who opted out or disputed a charge. It is "
            "the one harm in this migration that cannot be undone by re-running the upgrade, "
            "because the messages would already have been sent. Release each one deliberately "
            "(which records the releasing user) or keep 0008 applied."
        )

    # R30.C4. next_review_at is the only record that a case parked at POLICY_CHECK is due to
    # be looked at again; the Review_Sweeper reads it and nothing else re-decides such a case.
    # Dropping the column does not fail the case or escalate it, it strands it — untouched and
    # absent from every queue until its recovery window elapses.
    awaiting_review = connection.execute(
        sa.text(
            "SELECT count(*) FROM recovery_case "
            f"WHERE state = '{CaseState.POLICY_CHECK.value}' AND next_review_at IS NOT NULL"
        )
    ).scalar_one()
    if awaiting_review:
        raise RuntimeError(
            f"refusing to downgrade 0008: {awaiting_review} recovery_case row(s) are waiting "
            f"at {CaseState.POLICY_CHECK.value} with next_review_at set. Dropping the column "
            "would discard the only record that they are due to be re-decided, and nothing "
            "else re-decides such a case: each one would sit invisibly, in no queue, until "
            "its recovery window elapses. Let the review sweeper run them out first, or move "
            "them out of POLICY_CHECK deliberately."
        )

    # The pre-0008 index predicate has no effect_kind clause, so restoring it puts resend rows
    # back into the set reconcile_intents and promote_stale_intents scan. A resend has no
    # provider object to read, so an UNCERTAIN resend is unresolvable by provider read: the
    # sweep would issue calls that can never settle, on a schedule, forever.
    unresolvable_resends = connection.execute(
        sa.text(
            "SELECT count(*) FROM execution_intent "
            f"WHERE effect_kind = '{ExecutionEffectKind.PAYMENT_LINK_RESEND.value}' "
            f"AND state = '{IntentState.UNCERTAIN.value}'"
        )
    ).scalar_one()
    if unresolvable_resends:
        raise RuntimeError(
            f"refusing to downgrade 0008: {unresolvable_resends} execution_intent row(s) are "
            f"{ExecutionEffectKind.PAYMENT_LINK_RESEND.value} in state "
            f"{IntentState.UNCERTAIN.value}. Restoring the pre-0008 predicate on "
            "ix_execution_intent_unresolved drops the effect_kind clause, which would return "
            "these rows to the reconciliation sweep's scanned set, and a resend has no "
            "provider effect to read, so the sweep would issue reads that can never resolve "
            "them. Resolve or fail those intents before downgrading."
        )

    # Not one of the design's three, and not a judgement call either: narrowing the
    # EstimationMethod checks back to four members is a plain constraint violation if any row
    # still carries the fifth. Postgres would report the constraint name and nothing else.
    # There is also no honest repair available — COST_SPLIT_NOT_MEASURED means the split was
    # never measured, so every replacement value would assert a measurement that does not
    # exist (R31.C9 is precisely the refusal to do that). Hence: refuse, and say what to fix.
    for table, column in _ESTIMATION_METHOD_COLUMNS:
        unmeasured = connection.execute(
            sa.text(
                f"SELECT count(*) FROM {table} "
                f"WHERE {column} = {_MIGRATED_SPLIT_METHOD_SQL}"
            )
        ).scalar_one()
        if unmeasured:
            raise RuntimeError(
                f"refusing to downgrade 0008: {table}.{column} holds {unmeasured} row(s) with "
                f"{column} = {_MIGRATED_SPLIT_METHOD_SQL}, which the pre-0008 CHECK does not "
                f"admit; it permits only "
                f"{', '.join(member.value for member in _PRE_SPLIT_ESTIMATION_METHODS)}. "
                "The label means the cost split was never measured, so there is no value to "
                "rewrite it to that would not fabricate a measurement. Delete or re-estimate "
                "those rows before downgrading."
            )


def _restore_action_cost_from_two_terms() -> None:
    """Re-add the blended ``action_cost`` as ``financial_cost + communication_cost``.

    **Lossy by construction and arithmetically faithful.** The split cannot be recovered — one
    column cannot hold two figures — but no net value changes, because every consumer of
    ``action_cost`` summed the two terms anyway. A downgrade that put only ``financial_cost``
    back would silently reduce the cost of every row whose communication term was non-zero,
    and a cost that quietly falls is a cost-to-value ratio that quietly passes.

    The ordering is the mirror of the upgrade's and load-bearing for the same reason: the
    ``UPDATE`` reads the two split columns and writes ``action_cost``, so it has to sit between
    the ``ADD COLUMN`` and the ``DROP COLUMN``s. ``action_cost`` arrives with ``DEFAULT 0``
    exactly as ``0001`` declared it, so a mistaken ordering would not fail — it would zero
    every cost figure in the database.
    """
    for table in _COST_SPLIT_TABLES:
        op.add_column(
            table, sa.Column("action_cost", MONEY, nullable=False, server_default="0")
        )
        op.execute(
            f"UPDATE {table} SET action_cost = financial_cost + communication_cost"
        )
        # The four columns take their own constraints with them — two
        # ck_*_nonnegative and two ck_*_method_enum — which is why none is dropped
        # explicitly, on the same terms the upgrade drops action_cost's.
        for column in (*_SPLIT_MONEY_COLUMNS, *_SPLIT_METHOD_COLUMNS):
            op.drop_column(table, column)
        _nonnegative_money_constraint(table, "action_cost")

    # And the two widened checks go back to the four-member set. Both, because the upgrade
    # widened both. See _PRE_SPLIT_ESTIMATION_METHODS for why the narrow set is written out
    # rather than regenerated from the enum.
    for table, column in _ESTIMATION_METHOD_COLUMNS:
        # op.f, because drop_constraint runs a bare name through the naming convention too.
        op.drop_constraint(op.f(f"ck_{table}_{column}_enum"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_{column}_enum"),
            table,
            _pre_split_estimation_method_sql(column),
        )


def _drop_ai_invocation_call_kind() -> None:
    op.drop_constraint(
        op.f("ck_ai_invocation_call_kind_enum"), "ai_invocation", type_="check"
    )
    op.drop_column("ai_invocation", "call_kind")


def _drop_execution_intent_effect_kind() -> None:
    """Restore ``ix_execution_intent_unresolved``'s original predicate, then drop the column.

    The index is rebuilt before the column goes, not after: the current predicate references
    ``effect_kind``, so ``DROP COLUMN`` would take the index with it and leave the sweep's scan
    on a sequential read until someone noticed. Columns, uniqueness and the state clause are
    reproduced exactly as ``0001`` wrote them, the state literals from
    ``_UNRESOLVED_INTENT_STATES_SQL``.
    """
    op.drop_index("ix_execution_intent_unresolved", table_name="execution_intent")
    op.create_index(
        "ix_execution_intent_unresolved",
        "execution_intent",
        ["state", "attempt_started_at"],
        unique=False,
        postgresql_where=sa.text(f"state IN ({_UNRESOLVED_INTENT_STATES_SQL})"),
    )
    op.drop_constraint(
        op.f("ck_execution_intent_effect_kind_enum"), "execution_intent", type_="check"
    )
    op.drop_column("execution_intent", "effect_kind")


def _drop_recovery_case_next_review_at() -> None:
    op.drop_index("ix_recovery_case_due_for_review", table_name="recovery_case")
    op.drop_constraint(
        op.f("ck_recovery_case_review_within_window"), "recovery_case", type_="check"
    )
    op.drop_column("recovery_case", "next_review_at")


def _disable_row_level_security() -> None:
    """Drop the policies and revoke the grants, before the tables go.

    ``DROP TABLE`` would take both with it, so this is for the case where it does not run:
    under ``--sql`` the emitted script is read and applied by hand, and a script that dropped
    four tables without ever naming the policies and grants it also removed would understate
    what it does. ``IF EXISTS`` on the policy matches ``0003`` and ``0006``.

    RLS is not disabled on the way out — the tables are about to cease existing, and
    ``ALTER TABLE ... DISABLE ROW LEVEL SECURITY`` on a table being dropped in the same
    transaction states nothing.
    """
    revokes = "".join(
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM {APP_ROLE};"
        for table in _NEW_TABLES
    )
    op.execute(_role_guarded(revokes))
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")


def _drop_new_tables() -> None:
    """Drop the four tables, dependants first.

    ``_NEW_TABLES`` is in creation order and ``contact_suppression`` and ``promise_to_pay``
    both carry a ``RESTRICT`` foreign key to ``customer_signal``, so the reverse of that order
    is the only one Postgres accepts. Each table's indexes go with it, which is why none is
    dropped by name.
    """
    for table in reversed(_NEW_TABLES):
        op.drop_table(table)


def downgrade() -> None:
    """Reverse :func:`upgrade` step for step, having first refused if anything would be lost.

    The refusals come first so that a refused downgrade has performed no DDL at all — see
    :func:`_refuse_if_anything_would_be_lost` for why that is ordered rather than left to the
    surrounding transaction. Everything after it is ``upgrade()``'s call list read backwards.
    """
    _refuse_if_anything_would_be_lost()

    _restore_action_cost_from_two_terms()
    _drop_ai_invocation_call_kind()
    _drop_execution_intent_effect_kind()
    _drop_recovery_case_next_review_at()
    _disable_row_level_security()
    _drop_new_tables()

"""The dashboard session table, so a tenant is read from a row rather than trusted.

One addition. Every API request has to answer "which merchant is this?" before it may read a
single tenant row, and the answer must not come from anything the caller sends. R17.C2 says
``merchant_id`` is derived from the session and any merchant id in a payload or query is ignored.

**Why a row and not a signed token.** A stateless token cannot be revoked. R17 wants a session
that stops being valid on expiry *and* on logout *and* when an operator decides it should, and a
signature cannot be un-signed. Storing the session also means the tenant is read from a row this
system wrote, which is the literal form of the requirement rather than an approximation of it.

**The token is never stored.** ``token_digest`` is a keyed HMAC of the bearer token under a
dedicated secret, so a disclosure of this table does not hand over live sessions, and an offline
attacker holding the table cannot confirm a guessed token without also holding the secret. The
uniqueness constraint is global rather than per-merchant because the lookup happens *before* the
tenant is established beyond what the token claims — a per-merchant constraint would leave one
digest usable against two merchants.

**``expires_at`` is persisted, not derived.** Same reason as ``recovery_case.window_end_at``:
shortening ``SESSION_LIFETIME`` must not retroactively invalidate sessions legitimately issued
under the old bound, and lengthening it must not silently resurrect expired ones.

The table is tenant-scoped and carries the same RLS policy as every other tenant table. That is
not redundant with the token lookup: authentication resolves the merchant from the token's slug
prefix and then looks the digest up *inside* that tenant, so a token whose claimed merchant is
wrong finds no row and fails closed rather than reading another tenant's session.

Additive only. New table, no column dropped, no constraint tightened on existing data, so this
cannot fail on a populated database.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "revora_app"

_TENANT_EXPR = (
    "merchant_id = NULLIF(current_setting('revora.merchant_id', true), '')::uuid"
)
"""Identical to migrations 0003 and 0005, deliberately repeated rather than imported.

A migration is a historical record of what was applied. Importing a shared constant would let a
later edit to that constant silently change what this migration is understood to have done,
which is the one property a migration must not have.
"""

_TABLE = "merchant_session"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "merchant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "merchant_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchant_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_digest", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("token_digest", name="uq_merchant_session_token_digest"),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name=op.f("ck_merchant_session_expiry_after_issue"),
        ),
    )
    op.create_index(
        "ix_merchant_session_merchant_id_expires_at",
        _TABLE,
        ["merchant_id", "expires_at"],
    )

    # Row-level security. Migration 0003 derives its table list from the model metadata at the
    # time it ran, so a table added later gets no policy from it — this has to be done here or
    # this would be the one tenant table in the schema without isolation.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute(
        f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE};
  END IF;
END
$do$;
"""
    )

    op.execute(
        f"COMMENT ON TABLE {_TABLE} IS "
        "'One authenticated dashboard session. The only source of an API request''s "
        "merchant_id — a merchant id in a payload or query is ignored. The bearer token is "
        "never stored, only its keyed digest, so disclosing this table does not hand over "
        "live sessions.'"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.drop_index("ix_merchant_session_merchant_id_expires_at", table_name=_TABLE)
    op.drop_table(_TABLE)

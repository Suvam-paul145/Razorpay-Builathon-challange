"""Row-level security on every tenant-scoped table, as defence in depth.

This is the belt, not the braces. The primary tenant control is the repository layer,
where ``merchant_id`` is a required argument on every read — because the mistake that
actually happens is a forgotten ``WHERE`` in application code, and a required argument
is what prevents that. RLS exists to catch precisely the case where the primary
control was bypassed.

The policy reads ``current_setting('revora.merchant_id', true)``, which
``persistence.repositories.session`` sets with ``SET LOCAL`` at the start of every
transaction. ``LOCAL`` matters: the setting reverts at commit or rollback, so a pooled
connection cannot carry one tenant's identity into the next transaction that borrows
it.

``NULLIF(..., '')`` is not decoration. With the setting absent, ``current_setting``
returns ``NULL`` and the comparison is false, which is what we want — an untenanted
transaction sees nothing. With the setting present but empty, the bare cast would
raise ``invalid input syntax for type uuid`` and turn a missing binding into a
confusing runtime error instead of an empty result.

Both ``USING`` and ``WITH CHECK`` are set. ``USING`` governs what can be read,
updated and deleted; ``WITH CHECK`` governs what can be written. Without the second,
a tenant could insert a row belonging to another merchant and then be unable to see
it — a write it cannot audit and cannot undo.

RLS is *not* forced on the table owner. Migrations and the seed run as the owner and
have to be able to write the sentinel tenant's configuration rows. The application
connects as ``revora_app``, which is not the owner, so the policies apply to it.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.persistence.models import TENANT_SCOPED_TABLES
from revora.platform.config import DEFAULTS_MERCHANT_ID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "revora_app"

_TENANT_EXPR = (
    "merchant_id = NULLIF(current_setting('revora.merchant_id', true), '')::uuid"
)

_APP_CONFIG_READ_EXPR = (
    f"{_TENANT_EXPR} OR merchant_id = '{DEFAULTS_MERCHANT_ID}'::uuid"
)
"""``app_config`` alone can read one extra tenant's rows: the sentinel that owns the
seeded defaults. Without this the fallback layer would be invisible under RLS and a
merchant with no overrides of its own would load an empty configuration. Writes are
still restricted to the merchant's own rows — see the ``WITH CHECK`` below."""

_IMMUTABLE_ASSIGNMENT_COLUMNS = ("contaminated", "excluded", "exclusion_reason")
"""The only columns of ``experiment_assignment`` the application may update.

``group`` and ``case_id`` are excluded at the privilege level, which is what R13.C2
asks for: an assignment that can be moved from control to treatment destroys the
comparison it was part of, and the move would be invisible in the result."""


def _tables_present() -> tuple[str, ...]:
    """The tenant-scoped tables that exist *at this point in the migration history*.

    ``TENANT_SCOPED_TABLES`` is derived from live model metadata, which makes it correct
    about the schema as it is today and wrong about the schema as it was when this migration
    was written. Adding any new tenant-scoped model retroactively changes what this migration
    tries to do, and on a fresh database it then fails: the table its own later migration
    creates does not exist yet.

    That was not hypothetical — adding ``experiment_result`` in 0005 broke every fresh
    install until this filter was added. Intersecting with what the database actually holds
    keeps the derivation (so a *new* table cannot be forgotten by a hand-maintained list)
    while making the migration honest about when it ran. A table introduced later is
    responsible for its own policy, and 0005 does exactly that.
    """
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    return tuple(name for name in TENANT_SCOPED_TABLES if name in existing)


def upgrade() -> None:
    tables = _tables_present()

    for table in tables:
        read_expr = _APP_CONFIG_READ_EXPR if table == "app_config" else _TENANT_EXPR
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({read_expr}) WITH CHECK ({_TENANT_EXPR})"
        )

    # Privileges for the application role. Granted per table rather than with a
    # blanket GRANT ALL so that the audit_record revocation from 0002 is not quietly
    # undone here — audit_record is granted SELECT and INSERT only.
    grants = [f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};"]
    for table in (*tables, "merchant"):
        if table == "audit_record":
            grants.append(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE};")
        elif table == "experiment_assignment":
            columns = ", ".join(_IMMUTABLE_ASSIGNMENT_COLUMNS)
            grants.append(
                f"GRANT SELECT, INSERT, DELETE ON {table} TO {APP_ROLE};"
                f"GRANT UPDATE ({columns}) ON {table} TO {APP_ROLE};"
            )
        else:
            grants.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE};")

    op.execute(
        f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    {"".join(grants)}
  END IF;
END
$do$;
"""
    )

    op.execute(
        "COMMENT ON POLICY tenant_isolation ON recovery_case IS "
        "'Defence in depth behind the repository layer, which requires merchant_id on "
        "every read. Reads the revora.merchant_id session variable set per transaction.'"
    )


def downgrade() -> None:
    for table in _tables_present():
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

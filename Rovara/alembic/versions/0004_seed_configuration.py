"""Seed the ~50 configurable bounds as ``app_config`` rows for the sentinel tenant.

The rows are generated from ``platform.config.CATALOGUE`` rather than written out in
SQL, so the typed accessor and the seeded values cannot disagree about a default.
Writing them twice is how ``MAX_RECOVERY_ATTEMPTS`` ends up as 3 in code and 5 in the
database, with the policy layer honouring one and the settings screen showing the
other.

**Every seeded value is an assumption placeholder**, and every row says so in
``is_assumption``. They were chosen to make the requirements testable. Nothing here is
calibrated, because calibration needs merchant data that does not exist yet.

Three values depart from the requirements table, each because the requirement as
written does not survive contact with the provider or with the deadline it sits inside:

* ``INGEST_ACK_TIMEOUT`` is 1500 ms, not 3000.
* ``MAX_MESSAGE_LENGTH`` is 300 characters, not 480.
* ``RISK_REASON_CODES`` is a configured set — ``payment_risk_check_failed`` and
  ``compliance_violation`` — from which the fraud-or-risk condition derives, rather
  than a hard-coded condition.

**Why a sentinel tenant.** ``app_config.merchant_id`` is ``NOT NULL`` like every other
tenant-scoped column, and at the moment the schema is created there is no merchant to
attach defaults to. Relaxing the column would put a hole in the one column every
isolation mechanism depends on, so the defaults get a tenant of their own. A
merchant's own row overrides it; the sentinel is the fallback.

``approved_by_user_id`` is null on these rows, which is correct rather than a gap:
nobody approved them because nobody chose them. Every change made through the
application must name a user (R15.C6).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import DEFAULTS_MERCHANT_ID, seed_rows

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SENTINEL_SLUG = "__platform_defaults__"
"""Not a real merchant, and named so nobody mistakes it for one. It has no users, no
webhook secret and no cases; the ``state`` column marks it so a merchant listing can
exclude it with a filter rather than by hard-coding an id."""


def upgrade() -> None:
    connection = op.get_bind()

    # The sentinel tenant. ON CONFLICT DO NOTHING so re-running against a database
    # that already has it is not an error.
    connection.execute(
        sa.text(
            """
            INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                  reporting_timezone, created_at)
            VALUES (:id, :slug, 'Platform configuration defaults', 'INR',
                    'SYSTEM', 'UTC', now())
            ON CONFLICT (slug) DO NOTHING
            """
        ),
        {"id": str(DEFAULTS_MERCHANT_ID), "slug": SENTINEL_SLUG},
    )

    rows = [
        {
            "merchant_id": str(DEFAULTS_MERCHANT_ID),
            "key": row["key"],
            "value": row["value"],
            "value_kind": row["value_kind"],
            "config_version": row["config_version"],
            # [ASSUMPTION] on every row. The requirements table's defaults are
            # placeholders chosen to make the bounds testable, and marking them lets
            # the dashboard say "3 attempts, assumed" rather than "3 attempts".
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows()
    ]

    connection.execute(
        sa.text(
            """
            INSERT INTO app_config (merchant_id, key, value, value_kind, config_version,
                                    is_active, effective_at, approved_by_user_id,
                                    is_assumption, purpose, note, created_at)
            VALUES (:merchant_id, :key, :value, :value_kind, :config_version,
                    true, now(), NULL, :is_assumption, :purpose, :note, now())
            ON CONFLICT (merchant_id, key, config_version) DO NOTHING
            """
        ),
        rows,
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM app_config WHERE merchant_id = :id"),
        {"id": str(DEFAULTS_MERCHANT_ID)},
    )
    connection.execute(
        sa.text("DELETE FROM merchant WHERE id = :id"), {"id": str(DEFAULTS_MERCHANT_ID)}
    )

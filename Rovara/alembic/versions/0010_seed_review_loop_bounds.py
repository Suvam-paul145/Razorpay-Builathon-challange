"""Seed ``WAIT_REVIEW_INTERVAL`` and ``REVIEW_SWEEP_INTERVAL`` as rows.

R30.C3 and R30.C5 both name an interval, and both are marked ``[ASSUMPTION]`` in the
requirements because nobody has measured either. They are configuration rows on the same
terms as every other bound (R15.C6): how long a case that chose restraint waits before
Revora looks at it again is a merchant's judgement about their own customers, and a
judgement is a recorded change with an approving user on it, not a redeploy.

**Why a third seed migration rather than an edit to 0004 or 0009.** Both have run
everywhere the schema exists, and Alembic will not re-run either. A key added to one of
their row sets today would appear in the catalogue and never in the table, so every
deployment would load it from the placeholder while the settings screen showed nothing.
Each new bound therefore gets a migration of its own. That is the cost of the rows being
data rather than code, and it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates
the rows from ``platform.config.CATALOGUE``, exactly as ``0004`` and ``0009`` do, and the
keys come from ``REVIEW_LOOP_BOUND_KEYS`` rather than being spelt out here. So this
migration holds no key string, no value, no kind and no purpose text of its own, and the
accessor and the seeded row cannot disagree. 43200 and 300 are written down once, in the
catalogue.

**The version label is ``0004``'s**, for the reason ``0009`` gives: these two belong to
the same baseline of assumptions as everything else seeded so far, the sentinel tenant
carries one configuration version, and ``ConfigurationRepository`` reads the sentinel's
rows without reference to their version.

``approved_by_user_id`` is null, correct because nobody approved them — nobody chose them.
Every change made through the application must name a user.

**The downgrade is genuinely reversible**, like ``0009``'s. It deletes two seeded default
rows and nothing derived from them: no case, no estimate and no decision holds a foreign
key to a configuration row, and a build at ``0009`` does not know these two keys, so their
absence returns it to exactly the state it was in. It deliberately leaves a *merchant's*
own override of either key in place — that row is a recorded decision with an approving
user on it, deleting it would destroy the record, and ``Configuration.unrecognized_keys``
exists precisely so a row whose key the running build does not know is carried rather than
rejected.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    REVIEW_LOOP_BOUND_KEYS,
    seed_rows,
)

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()

    rows = [
        {
            "merchant_id": str(DEFAULTS_MERCHANT_ID),
            "key": row["key"],
            "value": row["value"],
            "value_kind": row["value_kind"],
            "config_version": row["config_version"],
            # [ASSUMPTION] on both. Twelve hours between reviews and five minutes between
            # sweeps are guesses, and the flag is what lets the settings screen say
            # "12 hours, assumed" rather than "12 hours".
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=REVIEW_LOOP_BOUND_KEYS)
    ]
    if len(rows) != len(REVIEW_LOOP_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(REVIEW_LOOP_BOUND_KEYS)} rows from the catalogue, got {len(rows)}"
        )

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
        sa.text(
            """
            DELETE FROM app_config
            WHERE merchant_id = :merchant_id
              AND config_version = :version
              AND key = ANY(:keys)
            """
        ),
        {
            "merchant_id": str(DEFAULTS_MERCHANT_ID),
            "version": DEFAULT_CONFIG_VERSION,
            "keys": list(REVIEW_LOOP_BOUND_KEYS),
        },
    )

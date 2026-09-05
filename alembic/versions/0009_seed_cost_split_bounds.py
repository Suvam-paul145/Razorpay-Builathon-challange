"""Seed ``PAYMENT_LINK_FINANCIAL_COST`` and ``MESSAGE_COMMUNICATION_COST`` as rows.

R31.C11: both are versioned configuration rows changeable only through a recorded change
naming an approving merchant user, on the same terms R15.C6 applies to every other bound.
Neither is a code constant and neither is an environment variable.

**Why a second seed migration rather than an edit to 0004.** ``0004`` has run everywhere
the schema exists. Alembic will not re-run it, so a bound added to its row set today would
appear in the catalogue and never in the table, and every deployment would load it from the
placeholder while the settings screen showed nothing. The two bounds therefore get their
own migration, and any later addition gets another one. That is the cost of the rows being
data rather than code, and it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates
the rows from ``platform.config.CATALOGUE``, exactly as ``0004`` does, and the keys come
from ``COST_SPLIT_BOUND_KEYS`` rather than being spelt out here. So this migration contains
no key string, no value, no kind and no purpose text of its own, and the accessor, the
seeded row and ``estimation.candidates.COST_PRIORS`` — which reads the same catalogue
defaults through ``money_default`` — cannot disagree. There is exactly one place 300 and 25
are written down.

**The version label is ``0004``'s.** These two are part of the same baseline of
assumptions: nobody measured either, and giving them a version of their own would split one
assumption baseline across two labels and break the fact that the sentinel tenant carries
one configuration version. ``ConfigurationRepository`` reads the sentinel's rows without
reference to their version and cites ``DEFAULT_CONFIG_VERSION`` for a merchant running on
defaults, so a single label is also what that code already assumes.

``approved_by_user_id`` is null, correct for the same reason it is on ``0004``'s rows:
nobody approved them because nobody chose them. Every change made through the application
must name a user.

**The downgrade is genuinely reversible**, unlike ``0008``'s. It deletes two seeded default
rows and nothing derived from them — no case, no estimate and no decision holds a foreign
key to a configuration row, and a build at ``0008`` does not know these keys, so their
absence returns it to exactly the state it was in. It deliberately leaves a *merchant's* own
override of either key in place: that row is a recorded decision with an approving user on
it, deleting it would destroy the record, and ``Configuration.unrecognized_keys`` exists
precisely so a row whose key the running build does not know is carried rather than
rejected.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    COST_SPLIT_BOUND_KEYS,
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    seed_rows,
)

revision: str = "0009"
down_revision: str | None = "0008"
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
            # [ASSUMPTION] on both. A provider fee nobody has invoiced us for and a
            # per-message cost nobody has billed are guesses, and the flag is what lets
            # the settings screen say "₹3.00, assumed" rather than "₹3.00".
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=COST_SPLIT_BOUND_KEYS)
    ]
    if len(rows) != len(COST_SPLIT_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(COST_SPLIT_BOUND_KEYS)} rows from the catalogue, got {len(rows)}"
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
            "keys": list(COST_SPLIT_BOUND_KEYS),
        },
    )

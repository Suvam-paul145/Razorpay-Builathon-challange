"""Seed ``CUSTOMER_STATED_CAUSE_CONFIDENCE`` as an ``app_config`` row.

The one bound R20.C4 introduces: the confidence recorded on a Diagnosis whose cause came from
a customer's stated Delay_Reason rather than from the provider's own error fields. It is
marked ``[ASSUMPTION]`` in the requirements table and it is the weakest-evidenced number in
the catalogue — design.md's open-questions table records that nobody has established whether a
stated reason correlates with the recovery outcome at all, let alone at what strength.

**Why 0.90 and not 1.0.** 1.0 is reserved for a cause read off the provider (R3.C10), so a
confidence of exactly 1.0 in a stored row means "the provider told us" and never anything
else. A customer's account of their own finances is evidence about the next decision and it is
not an authoritative observation of the failed charge, so it is recorded below the reserved
value. The gap between 0.90 and 1.0 is not measured either; it is the smallest honest
statement of "we believe this, and less than we believe the provider".

**Why not below the confidence floor.** ``DIAGNOSIS_CONFIDENCE_FLOOR`` defaults to 0.60, and a
value below it would be substituted to ``UNKNOWN`` under R3.C8 — the capture would be recorded
and then discarded, and the whole customer-response loop would sharpen nothing (R20.C7). The
diagnosis path deliberately still routes a stated cause through that same gate rather than
exempting it, so a deployment that violated the ordering would produce a recorded substitution
naming ``CONFIDENCE_BELOW_FLOOR`` instead of a cause that quietly outranked the rule every
other source obeys.

**Why a sixth seed migration rather than an edit to 0004, 0009, 0010, 0011 or 0012.** All five
have run everywhere the schema exists and Alembic will not re-run any of them. A key added to
one of their row sets today would appear in the catalogue and never in the table, so every
deployment would load it from the placeholder while the settings screen showed nothing. Each
new bound gets a migration of its own. That is the cost of the rows being data rather than
code, and it is the correct cost.

**Where the number comes from.** Nowhere in this file. ``seed_rows(keys=...)`` generates the
row from ``platform.config.CATALOGUE``, exactly as ``0004`` and its four successors do, and the
key comes from ``CUSTOMER_STATED_CAUSE_BOUND_KEYS`` rather than being spelt out here. So this
migration holds no key string, no value, no kind and no purpose text of its own, and the
accessor and the seeded row cannot disagree. 0.90 is written down once, in the catalogue.

**The version label is ``0004``'s**, for the reason ``0009`` through ``0012`` give: this bound
belongs to the same baseline of assumptions as everything else seeded so far, the sentinel
tenant carries one configuration version, and ``ConfigurationRepository`` reads the sentinel's
rows without reference to their version.

``approved_by_user_id`` is null, correct because nobody approved it — nobody chose it.

**The downgrade is genuinely reversible.** It deletes one seeded default row and nothing
derived from it: no diagnosis holds a foreign key to a configuration row, and a build at
``0012`` does not know this key, so its absence returns that build to exactly the state it was
in. A diagnosis already recorded at 0.90 keeps its stored confidence, which is correct — the
confidence is a fact about a decision that was made, not a live read of a bound. It
deliberately leaves a *merchant's* own override in place: that row is a recorded decision with
an approving user on it, deleting it would destroy the record, and
``Configuration.unrecognized_keys`` exists precisely so a row whose key the running build does
not know is carried rather than rejected.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    CUSTOMER_STATED_CAUSE_BOUND_KEYS,
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    seed_rows,
)

revision: str = "0013"
down_revision: str | None = "0012"
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
            # [ASSUMPTION], and the flag is load-bearing on this one more than most: an
            # operator reading "0.90" beside a customer-derived cause would otherwise take it
            # for a measured correspondence, and it is a plausible reading of a stranger's
            # words.
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=CUSTOMER_STATED_CAUSE_BOUND_KEYS)
    ]
    if len(rows) != len(CUSTOMER_STATED_CAUSE_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(CUSTOMER_STATED_CAUSE_BOUND_KEYS)} rows from the catalogue, "
            f"got {len(rows)}"
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
            "keys": list(CUSTOMER_STATED_CAUSE_BOUND_KEYS),
        },
    )

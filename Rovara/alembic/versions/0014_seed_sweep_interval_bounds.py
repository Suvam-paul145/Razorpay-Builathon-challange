"""Seed the three sweep intervals the ticker role needs as ``app_config`` rows.

``DETECTION_GAP_BACKFILL_INTERVAL``, ``CALIBRATION_REPORT_INTERVAL`` and
``CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL``. Four of the seven periodic sweeps already had a
configured interval; these are the other three, and until this revision they had **no interval
bound at all**. That was not a gap anybody had to notice, because nothing produced the sweeps:
``revora.jobs.scheduler.enqueue_sweep`` had exactly one caller in the repository and it was a
development script. The ticker role is the production caller, and it prices every member of
``PERIODIC_SWEEP_KINDS`` from a bound and *refuses* a kind it cannot price — so all seven have
to exist before a schedule can run at all.

**Why the retention sweep's interval is the one to read carefully.** R17.C11 gives 24 hours
after ``CUSTOMER_DATA_RETENTION`` elapses for contact data to be redacted, and no configured
interval expressed that deadline before this row. The sweep's own docstring in
``revora.jobs.scheduler`` states it. An hourly pass leaves 24 attempts inside the deadline,
which is the margin a merchant with a backlog needs, since one pass redacts a batch and
enqueues its own successor while more remains. It is an ``[ASSUMPTION]`` like every other
default here — nothing has measured a real backlog — but it is an assumption chosen against a
stated deadline rather than against nothing.

The key is named ``CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL`` and not
``CUSTOMER_DATA_RETENTION_INTERVAL``, because ``CUSTOMER_DATA_RETENTION`` already exists and is
the retention *period* — 180 days. Confusing the two is expensive in both directions: an hourly
retention period would redact live cases, and a 180-day sweep interval would miss R17.C11 by
half a year. The longer name is the cheapest available guard against a reader collapsing them.

**Why a seventh seed migration rather than an edit to 0004 or any of 0009 through 0013.** All
six have run everywhere the schema exists and Alembic will not re-run any of them. A key added
to one of their row sets today would appear in the catalogue and never in the table, so every
deployment would load it from the placeholder while the settings screen showed nothing. Each
new bound gets a migration of its own. That is the cost of the rows being data rather than
code, and it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates the
rows from ``platform.config.CATALOGUE``, exactly as ``0004`` and its five successors do, and
the keys come from ``SWEEP_INTERVAL_BOUND_KEYS`` rather than being spelt out here. So this
migration holds no key string, no value, no kind and no purpose text of its own, and the
accessor and the seeded row cannot disagree. 900 and 3600 are written down once, in the
catalogue.

**The version label is ``0004``'s**, for the reason ``0009`` through ``0013`` give: these three
belong to the same baseline of assumptions as everything else seeded so far, the sentinel tenant
carries one configuration version, and ``ConfigurationRepository`` reads the sentinel's rows
without reference to their version.

``approved_by_user_id`` is null, correct because nobody approved them — nobody chose them.

**The downgrade is genuinely reversible.** It deletes three seeded default rows and nothing
derived from them: no case, no estimate, no decision and no job holds a foreign key to a
configuration row, and a build at ``0013`` does not know these three keys, so their absence
returns it to exactly the state it was in. A job already enqueued under one of these intervals
keeps its dedupe key, which is correct — the bucket in that key is a fact about a tick that
happened, not a live read of a bound. The downgrade deliberately leaves a *merchant's* own
override of any of the three in place: that row is a recorded decision with an approving user
on it, deleting it would destroy the record, and ``Configuration.unrecognized_keys`` exists
precisely so a row whose key the running build does not know is carried rather than rejected.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    SWEEP_INTERVAL_BOUND_KEYS,
    seed_rows,
)

revision: str = "0014"
down_revision: str | None = "0013"
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
            # [ASSUMPTION] on all three. The flag is what lets the settings screen say
            # "every hour, assumed" rather than "every hour" — and on the retention sweep it
            # carries more than usual, because an interval printed beside a 24-hour
            # regulatory deadline reads as though somebody derived it from the deadline.
            # Nobody did; it was chosen to fit inside it with room to spare.
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=SWEEP_INTERVAL_BOUND_KEYS)
    ]
    if len(rows) != len(SWEEP_INTERVAL_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(SWEEP_INTERVAL_BOUND_KEYS)} rows from the catalogue, got {len(rows)}"
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
            "keys": list(SWEEP_INTERVAL_BOUND_KEYS),
        },
    )

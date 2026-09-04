"""Seed the five Promise_To_Pay bounds R23 needs as rows.

``PROMISE_MIN_LEAD_TIME`` (R23.C2), ``PROMISE_FOLLOW_UP_OFFSET`` and
``PROMISE_WINDOW_SAFETY_MARGIN`` (R23.C3), ``MAX_PROMISES_PER_CASE`` (R23.C7) and
``PROMISE_SWEEP_INTERVAL`` (R23.C13). Every one of the five is marked ``[ASSUMPTION]`` in the
requirements and nobody measured any of them. They are configuration rows on the same terms as
every other bound (R15.C6): how soon a customer's promise may be for, and how long after it
Revora looks, are that merchant's judgement about their own customers, and a judgement is a
recorded change with an approving user on it, not a redeploy.

**Why these five were absent until now, and why that was correct.** Four of them were named in
``0008``'s own DDL comments, in ``revora.persistence.models.customer`` and in
``PromiseToPayRepository``'s docstrings — and in none of those places as a value. The table they
govern had no writer, and implementing a check against an unconfigured bound would have meant
inventing the number in the module that enforces it, which is the one place R15.C6 forbids a
bound from living. So the constraint comments named the bounds and deferred them, and this
migration is where they stop being names.

**Two of the five decide what is *storable*.** ``promise_to_pay`` carries ``CHECK (follow_up_at
IS NULL OR follow_up_at < window_end_at_snapshot)`` from ``0008``, so
``PROMISE_WINDOW_SAFETY_MARGIN`` at or below zero would make every clamped Follow_Up_Instant a
failed ``INSERT`` on a public endpoint — ``Configuration.from_values`` refuses such a value at
load rather than letting a customer discover it as a 503. And ``UNIQUE (merchant_id, case_id)``
encodes ``MAX_PROMISES_PER_CASE = 1``, so the seeded 1 is not a coincidence with the index: it
is the same number, and the catalogue's note on that key records how the two are reconciled.
Seeding a value above 1 here would seed a bound the schema refuses to honour.

**Why a seventh seed migration rather than an edit to 0004 or any of 0009 through 0014.** All of
them have run everywhere the schema exists and Alembic will not re-run any of them. A key added
to one of their row sets today would appear in the catalogue and never in the table, so every
deployment would load it from the placeholder while the settings screen showed nothing. Each new
bound gets a migration of its own. That is the cost of the rows being data rather than code, and
it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates the
rows from ``platform.config.CATALOGUE``, exactly as ``0004`` and ``0009`` through ``0014`` do,
and the keys come from ``PROMISE_BOUND_KEYS`` rather than being spelt out here. So this migration
holds no key string, no value, no kind and no purpose text of its own, and the accessor and the
seeded row cannot disagree. 3600, 86400, 3600, 1 and 300 are written down once, in the catalogue.

``PROMISE_BOUND_KEYS`` is a *mapping* rather than a tuple, unlike ``REVIEW_LOOP_BOUND_KEYS`` and
``SWEEP_INTERVAL_BOUND_KEYS``, because the five are of mixed kinds — four durations and a count —
so no uniform kind check is available and each key is paired with the kind it must have instead.
That difference does not reach this file: the row-count assertion and the ``key = ANY(:keys)``
downgrade read the same way against a mapping as against a tuple, because both iterate to their
keys.

**The version label is ``0004``'s**, for the reason ``0009`` through ``0014`` give: these five
belong to the same baseline of assumptions as everything else seeded so far, the sentinel tenant
carries one configuration version, and ``ConfigurationRepository`` reads the sentinel's rows
without reference to their version.

``approved_by_user_id`` is null, correct because nobody approved them — nobody chose them.

**The downgrade is genuinely reversible.** It deletes five seeded default rows and nothing
derived from them: no case, no promise and no decision holds a foreign key to a configuration
row, and a build at ``0015`` does not know these five keys, so their absence returns it to
exactly the state it was in. It deliberately leaves a *merchant's* own override of any of the
five in place — that row is a recorded decision with an approving user on it, deleting it would
destroy the record, and ``Configuration.unrecognized_keys`` exists precisely so a row whose key
the running build does not know is carried rather than rejected.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    PROMISE_BOUND_KEYS,
    seed_rows,
)

revision: str = "0016"
down_revision: str | None = "0015"
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
            # [ASSUMPTION] on all five. One hour of lead time, a day's follow-up offset, an
            # hour of safety margin, one promise per case and a five-minute sweep are guesses,
            # and the flag is what lets the settings screen say "1 hour, assumed" rather than
            # "1 hour".
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=PROMISE_BOUND_KEYS)
    ]
    if len(rows) != len(PROMISE_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(PROMISE_BOUND_KEYS)} rows from the catalogue, got {len(rows)}"
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
            "keys": list(PROMISE_BOUND_KEYS),
        },
    )

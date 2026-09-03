"""Seed the public customer surface's four bounds as ``app_config`` rows.

``CUSTOMER_PAGE_RATE_LIMIT`` and ``CUSTOMER_PAGE_SOURCE_RATE_LIMIT`` (R29.C1),
``MAX_CUSTOMER_SIGNALS_PER_CASE`` (R19.C7) and ``DELAY_NOTE_MAX_LENGTH`` (R20.C2). All four are
marked ``[ASSUMPTION]`` in the requirements table, because nobody has measured how often a person
refreshes a page they are being asked to pay from, how many times one person has something to say
about one payment, or how much free text they need to say it in.

They are configuration rows on the same terms as every other bound (R15.C6): raising the signal
cap or the retained note length is a decision with a person's name on it, not a redeploy.

**Why two of these are not check constraints and the third is.**
``MAX_CUSTOMER_SIGNALS_PER_CASE`` has no schema counterpart at all — encoding today's 5 would make
raising it a migration, and the enforcement is a count taken under the token's row lock in the same
transaction as the insert. The two rate limits could not be constraints even in principle: they
bound a request rate, and the database sees no requests. ``DELAY_NOTE_MAX_LENGTH`` is the exception
and the coupling is deliberate: ``customer_signal`` carries
``CHECK (char_length(delay_reason_note) <= 500)`` from migration ``0008``, so this row is the
authority and that constraint is the backstop. Lowering the row takes effect on the next write;
raising it above 500 cannot, which is why the writer truncates to the smaller of the two rather
than attempting a row the schema would refuse. Both numbers being 500 today is the point — a row
that disagreed with its own backstop would fail at insert on the one surface a stranger can reach.

**Why a fifth seed migration rather than an edit to 0004, 0009, 0010 or 0011.** All four have run
everywhere the schema exists and Alembic will not re-run any of them. A key added to one of their
row sets today would appear in the catalogue and never in the table, so every deployment would load
it from the placeholder while the settings screen showed nothing. Each new bound gets a migration of
its own. That is the cost of the rows being data rather than code, and it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates the rows
from ``platform.config.CATALOGUE``, exactly as ``0004``, ``0009``, ``0010`` and ``0011`` do, and the
keys come from ``CUSTOMER_SURFACE_BOUND_KEYS`` rather than being spelt out here. So this migration
holds no key string, no value, no kind and no purpose text of its own, and the accessor and the
seeded row cannot disagree. 30, 120, 5 and 500 are written down once, in the catalogue.

**The version label is ``0004``'s**, for the reason ``0009`` through ``0011`` give: these four
belong to the same baseline of assumptions as everything else seeded so far, the sentinel tenant
carries one configuration version, and ``ConfigurationRepository`` reads the sentinel's rows without
reference to their version.

``approved_by_user_id`` is null, correct because nobody approved them — nobody chose them.

**The downgrade is genuinely reversible.** It deletes four seeded default rows and nothing derived
from them: no case, no token, no signal and no decision holds a foreign key to a configuration row,
and a build at ``0011`` does not know these four keys, so their absence returns it to exactly the
state it was in. It deliberately leaves a *merchant's* own override of any of them in place — that
row is a recorded decision with an approving user on it, deleting it would destroy the record, and
``Configuration.unrecognized_keys`` exists precisely so a row whose key the running build does not
know is carried rather than rejected.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    CUSTOMER_SURFACE_BOUND_KEYS,
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    seed_rows,
)

revision: str = "0012"
down_revision: str | None = "0011"
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
            # [ASSUMPTION] on all four. The flag is what lets the settings screen say
            # "5, assumed" rather than "5" — a number in a table looks measured whether or
            # not it is, and these four are read by an operator deciding whether a customer
            # was cut off by a bound or by a bug.
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=CUSTOMER_SURFACE_BOUND_KEYS)
    ]
    if len(rows) != len(CUSTOMER_SURFACE_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(CUSTOMER_SURFACE_BOUND_KEYS)} rows from the catalogue, "
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
            "keys": list(CUSTOMER_SURFACE_BOUND_KEYS),
        },
    )

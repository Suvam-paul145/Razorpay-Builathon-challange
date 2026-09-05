"""Seed ``CUSTOMER_TOKEN_LIFETIME`` and ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` as rows.

R18.C2 and R18.C9 both name a bound and both are marked ``[ASSUMPTION]`` in the
requirements, because nobody has measured how long a customer needs a payment page to stay
reachable or how many times one person has something to say about one payment. They are
configuration rows on the same terms as every other bound (R15.C6): raising the submission
cap is a decision with a person's name on it, not a redeploy.

**Why the submission cap is a row and not a check constraint.** ``customer_access_token``
carries ``CHECK (accepted_submission_count >= 0)`` and deliberately no upper bound. Encoding
today's 5 in the schema would make raising it a migration, and the durable enforcement is
already elsewhere and stronger than a constraint would be: the comparison lives inside
``increment_accepted_submissions``' own conditional ``UPDATE``, so the check and the
increment cannot be separated by a concurrent request.

**Why a fourth seed migration rather than an edit to 0004, 0009 or 0010.** All three have
run everywhere the schema exists and Alembic will not re-run any of them. A key added to one
of their row sets today would appear in the catalogue and never in the table, so every
deployment would load it from the placeholder while the settings screen showed nothing. Each
new bound gets a migration of its own. That is the cost of the rows being data rather than
code, and it is the correct cost.

**Where the numbers come from.** Nowhere in this file. ``seed_rows(keys=...)`` generates the
rows from ``platform.config.CATALOGUE``, exactly as ``0004``, ``0009`` and ``0010`` do, and
the keys come from ``CUSTOMER_TOKEN_BOUND_KEYS`` rather than being spelt out here. So this
migration holds no key string, no value, no kind and no purpose text of its own, and the
accessor and the seeded row cannot disagree. 259200 and 5 are written down once, in the
catalogue.

**The version label is ``0004``'s**, for the reason ``0009`` and ``0010`` give: these two
belong to the same baseline of assumptions as everything else seeded so far, the sentinel
tenant carries one configuration version, and ``ConfigurationRepository`` reads the
sentinel's rows without reference to their version.

``approved_by_user_id`` is null, correct because nobody approved them — nobody chose them.

**The downgrade is genuinely reversible.** It deletes two seeded default rows and nothing
derived from them: no case, no token and no decision holds a foreign key to a configuration
row, and a build at ``0010`` does not know these two keys, so their absence returns it to
exactly the state it was in. It deliberately leaves a *merchant's* own override of either key
in place — that row is a recorded decision with an approving user on it, deleting it would
destroy the record, and ``Configuration.unrecognized_keys`` exists precisely so a row whose
key the running build does not know is carried rather than rejected.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.platform.config import (
    CUSTOMER_TOKEN_BOUND_KEYS,
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    seed_rows,
)

revision: str = "0011"
down_revision: str | None = "0010"
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
            # [ASSUMPTION] on both. Seventy-two hours of reachability and five submissions
            # are guesses, and the flag is what lets the settings screen say "5, assumed"
            # rather than "5".
            "is_assumption": bool(row["is_assumption"]),
            "purpose": row["purpose"],
            "note": row["note"],
        }
        for row in seed_rows(keys=CUSTOMER_TOKEN_BOUND_KEYS)
    ]
    if len(rows) != len(CUSTOMER_TOKEN_BOUND_KEYS):  # pragma: no cover - defensive
        raise RuntimeError(
            f"expected {len(CUSTOMER_TOKEN_BOUND_KEYS)} rows from the catalogue, "
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
            "keys": list(CUSTOMER_TOKEN_BOUND_KEYS),
        },
    )

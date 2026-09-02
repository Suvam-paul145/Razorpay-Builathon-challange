"""Widen the three duration-in-milliseconds columns to ``bigint``.

``integer`` holds at most 2,147,483,647, which as milliseconds is **24.8 days**. Every one of these
three columns records an elapsed duration with no upper bound on it, so each one has a value beyond
which the insert fails with ``NumericValueOutOfRange`` — and the failure is not a wrong number, it
is a dead job. The insert raises, the handler fails, the job retries, it fails identically, and it
dead-letters. The work is then never done.

**``detection_verdict.latency_ms`` is the one that is actually reachable**, and it is reachable
through a supported path rather than a pathological one. The column records persistence-to-verdict
latency, measured against ``DETECTION_LATENCY_BOUND``. Two things can make it enormous:

* The **detection-gap backfill**. It lists provider payments over a window and ingests the failures
  no webhook delivered — that is the whole point of it, since a disabled webhook means silent total
  detection loss. Nothing bounds that window to 25 days, so a backfill run after a long outage
  computes a latency past the ceiling and crashes on the very events it exists to rescue.
* A worker down longer than 25 days with events already persisted, which is the same shape.

Either way the events most in need of detection are exactly the ones that cannot be detected. That
inversion is what makes this worth a migration rather than a clamp.

``job_attempt.duration_ms`` and ``ai_invocation.latency_ms`` are widened in the same breath. Neither
is realistically reachable — an execution lease expires in 60 seconds and a reasoning call times out
in 10 — but they are the same kind of column measuring the same kind of quantity, and leaving two of
three narrow would mean the next person has to rediscover which is which.

**Why widen rather than clamp.** A clamped latency is a wrong measurement presented as a right one:
a report would show 24.8 days for an event that was 200 days late, and nothing on the row would say
it had been truncated. Widening loses nothing — ``bigint`` milliseconds runs to 292 million years —
and keeps the column meaning exactly what its name says.

**This one is not purely additive.** ``ALTER TYPE integer -> bigint`` rewrites the table and takes
an ``ACCESS EXCLUSIVE`` lock for the duration. On these three tables at any plausible size that is
sub-second, but it is a rewrite and it is worth saying so rather than discovering it on a large
deployment. The change is a widening, so no existing value can fail to convert and the downgrade is
only safe while every stored value still fits in an ``integer`` — which the downgrade asserts rather
than assumes.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(table, column)`` for every duration-in-milliseconds column.
_DURATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("detection_verdict", "latency_ms"),
    ("ai_invocation", "latency_ms"),
    ("job_attempt", "duration_ms"),
)

_INTEGER_MAX = 2_147_483_647


def upgrade() -> None:
    for table, column in _DURATION_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Narrow back, refusing to run if any stored value would not survive it.

    A downgrade that silently truncated a duration would produce a plausible-looking wrong number
    with nothing on the row to mark it — the exact failure mode the upgrade exists to remove. So the
    check runs first and the downgrade fails loudly instead.
    """
    connection = op.get_bind()
    for table, column in _DURATION_COLUMNS:
        oversized = connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} > :ceiling"),
            {"ceiling": _INTEGER_MAX},
        ).scalar_one()
        if oversized:
            raise RuntimeError(
                f"{table}.{column} holds {oversized} value(s) above the integer ceiling; "
                "narrowing would silently truncate a recorded duration"
            )

    for table, column in _DURATION_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )

"""The startup schema-revision check.

A worker running against an older schema than the API is a class of bug that is
very hard to diagnose, because it does not look like a schema problem. It looks
like a wrong recovery number, or a case that never leaves a state, and the
investigation starts in the wrong place.

So both roles verify at startup that the migration revision in the database matches
the revision this build expects, and refuse to serve on a mismatch. Refusing is the
whole point: a process that starts and then behaves subtly differently is worse than
one that does not start, because the first failure is silent and the second is
loud and immediate.

:data:`EXPECTED_REVISION` is the revision this build was written against. It is
updated by whoever adds a migration, in the same commit as the migration, which is
what makes the check meaningful — a revision read from the migration directory at
runtime would agree with itself no matter what the database said.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Engine, text

__all__ = [
    "EXPECTED_REVISION",
    "SchemaRevisionMismatchError",
    "current_revision",
    "verify_schema_revision",
]

EXPECTED_REVISION: Final[str] = "0007"
"""The head this build expects. Bump in the same commit as a new migration.

``0005`` added ``experiment_result`` and ``memory_observation.diagnosis_method``; ``0006``
adds ``merchant_session``; ``0007`` widens the three duration-in-milliseconds columns to
``bigint``, because ``integer`` milliseconds runs out after 24.8 days and a detection
backfill over a longer window crashed on the very events it existed to rescue. The bump
was forgotten when 0005 was written and the pg tier caught it immediately, which is the
whole reason this constant is hand-maintained rather than read from the migration
directory: a value derived from the files present can never disagree with them, and so can
never detect a database that has not been migrated."""


class SchemaRevisionMismatchError(RuntimeError):
    """The database schema is not the one this build expects.

    Carries both revisions so the message is enough to decide what to do: if the
    database is behind, run migrations; if it is ahead, this build is the old one
    and should not be serving.
    """

    def __init__(self, found: str | None, expected: str) -> None:
        self.found = found
        self.expected = expected
        detail = "no alembic_version row" if found is None else f"found {found!r}"
        super().__init__(
            f"schema revision mismatch: expected {expected!r}, {detail}. "
            "Run migrations before starting this process."
        )


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in ``alembic_version``, or ``None`` if unmigrated.

    Reads the table directly rather than through Alembic's own API so the check
    costs one query and no migration-environment import at startup.
    """
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one()
        if not exists:
            return None
        row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
        return None if row is None else str(row[0])


def verify_schema_revision(engine: Engine, *, expected: str = EXPECTED_REVISION) -> str:
    """Return the current revision, or refuse to continue.

    Raises:
        SchemaRevisionMismatchError: if the database is unmigrated or on another
            revision. Callers must not catch this and continue — it is the one
            startup failure that should stop the process.
    """
    found = current_revision(engine)
    if found != expected:
        raise SchemaRevisionMismatchError(found, expected)
    return found

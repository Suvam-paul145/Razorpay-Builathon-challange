"""Every enum-backed column rejects a value outside its enum. Every one of them.

The test is parametrized over the registry that :func:`revora.persistence.models.base
.enum_check` fills as the model modules import, so a column added next month is
covered the moment it is declared. Testing a remembered handful would leave the newest
column — the one most likely to be wrong — untested.

The mechanism deserves explanation. Inserting a bad value directly into the real table
would work, but the insert would first have to satisfy every other ``NOT NULL`` and
foreign key on that table, which is thirty-one different row builders and a test that
fails for reasons unrelated to what it is checking. Instead each case reads the
``CHECK`` expression that is actually installed in the database, builds a scratch
temporary table carrying that same expression over a column of the same name, and
proves it accepts a member and rejects a non-member.

That keeps the assertion about the installed constraint rather than about the Python
that generated it, which is the part that could drift.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from revora.persistence.models import enum_backed_columns
from revora.persistence.models.base import EnumBackedColumn

pytestmark = pytest.mark.pg

OUTSIDE_THE_ENUM = "__NOT_A_MEMBER__"

ENUM_COLUMNS = enum_backed_columns()


def test_the_registry_is_not_empty() -> None:
    """A guard on the parametrization itself.

    If the registry were empty — an import reordered, a helper bypassed — every
    parametrized case below would silently vanish and the suite would still be green.
    """
    assert len(ENUM_COLUMNS) > 30


@pytest.mark.parametrize(
    "column",
    ENUM_COLUMNS,
    ids=[f"{entry.table}.{entry.column}" for entry in ENUM_COLUMNS],
)
def test_enum_check_rejects_a_value_outside_the_enum(
    owner_engine: Engine, column: EnumBackedColumn
) -> None:
    """The installed ``CHECK`` accepts a member and refuses anything else."""
    with owner_engine.connect() as connection:
        definition = connection.execute(
            text(
                """
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = :table AND c.conname = :constraint
                """
            ),
            {"table": column.table, "constraint": column.constraint_name},
        ).scalar_one_or_none()

    assert definition is not None, (
        f"{column.constraint_name} is not installed on {column.table}; "
        "the model declares it, so the migration is out of date"
    )

    expression = definition.removeprefix("CHECK ")

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                f'CREATE TEMPORARY TABLE enum_probe ("{column.column}" text, '
                f"CONSTRAINT probe_check CHECK {expression}) ON COMMIT DROP"
            )
        )
        insert = text(f'INSERT INTO enum_probe ("{column.column}") VALUES (:value)')

        # A real member is accepted.
        connection.execute(insert, {"value": column.permitted[0]})

        # A NULL is accepted: the fact has not been recorded yet, which is different
        # from having been recorded as something illegal. Columns that must always
        # hold a value say so with NOT NULL instead.
        connection.execute(insert, {"value": None})

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert, {"value": OUTSIDE_THE_ENUM})


def test_enum_check_permitted_values_match_the_domain(owner_engine: Engine) -> None:
    """The database's permitted set is exactly the domain enum's members.

    Not just "rejects a bad value" — an over-permissive constraint listing a member the
    domain has since removed would pass the test above and still let a retired value
    into a column.
    """
    mismatched: list[str] = []
    with owner_engine.connect() as connection:
        for column in ENUM_COLUMNS:
            definition = connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(c.oid)
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = :table AND c.conname = :constraint
                    """
                ),
                {"table": column.table, "constraint": column.constraint_name},
            ).scalar_one_or_none()
            if definition is None:
                mismatched.append(f"{column.table}.{column.column}: constraint missing")
                continue
            for member in column.permitted:
                if f"'{member}'" not in definition:
                    mismatched.append(
                        f"{column.table}.{column.column}: {member} not permitted in the schema"
                    )

    assert not mismatched, "\n".join(mismatched)

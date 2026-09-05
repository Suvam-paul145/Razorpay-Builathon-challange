"""The declarative base, the shared mixins, and the type discipline in one place.

Three things live here, and each of them exists so that a decision cannot be made
differently in two places.

**The type map.** Money is ``BIGINT`` minor units, a probability is
``NUMERIC(6,4)``, a signed increment is ``NUMERIC(7,4)``, a confidence is
``NUMERIC(4,3)``, and time is ``TIMESTAMPTZ``. There is no ``FLOAT`` or ``REAL``
column anywhere in Revora, because a float column is how a stored revenue figure
stops matching the sum it was computed from — the same reason ``domain.money``
refuses a float argument. The aliases below are the only way to spell those types,
so a new table cannot quietly pick ``NUMERIC(5,2)`` for a probability.

**The mixins.** Every table has ``id``, ``created_at`` and — except ``merchant``
itself — ``merchant_id``. Repeating those three by hand across thirty-one tables
is how one of them ends up nullable.

**The enum registry.** Enums are stored as ``TEXT`` plus a ``CHECK`` built from the
enum members, because a Postgres enum type needs a migration to extend while a
``CHECK`` does not, and because the authoritative definition has to stay in
``revora.domain``. :func:`enum_check` derives the constraint from the enum itself,
so adding a member to ``domain.enums`` and forgetting the database is impossible
once the migration is regenerated. Every constraint it builds is also recorded in
:data:`ENUM_BACKED_COLUMNS`, which is what lets one test assert that *every*
enum-backed column rejects a value outside its enum rather than the handful
somebody remembered to test.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Final, NamedTuple

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from revora.platform.clock import now

__all__ = [
    "AUDIT_TIMESTAMP",
    "CONFIDENCE",
    "ENUM_BACKED_COLUMNS",
    "MONEY",
    "NAMING_CONVENTION",
    "PROBABILITY",
    "SIGNED_INCREMENT",
    "TIMESTAMPTZ",
    "Base",
    "CreatedAtMixin",
    "EnumBackedColumn",
    "IdMixin",
    "MerchantScopedMixin",
    "RowBase",
    "enum_check",
    "money_column",
    "nonnegative_money_check",
]

# ---------------------------------------------------------------------------
# Type discipline. These are the only spellings permitted.
# ---------------------------------------------------------------------------

MONEY: Final = BigInteger
"""Minor currency units, always integer. ``BIGINT`` holds ₹92 quadrillion in
paise, so overflow is not a realistic failure mode; rounding is."""

PROBABILITY: Final = Numeric(6, 4)
"""0.0000 … 1.0000, four places, matching ``domain.probability.Probability``."""

SIGNED_INCREMENT: Final = Numeric(7, 4)
"""-1.0000 … 1.0000. The extra digit is the sign, and the sign is load-bearing:
an action estimated to make recovery less likely must store as negative."""

CONFIDENCE: Final = Numeric(4, 3)
"""0.000 … 1.000, three places, matching ``domain.probability.Confidence``."""

TIMESTAMPTZ: Final = TIMESTAMP(timezone=True)
"""Always UTC. Every timing bound in Revora is a comparison of two stored
instants, and an offset-naive column makes one of those comparisons wrong twice a
year."""

AUDIT_TIMESTAMP: Final = TIMESTAMP(timezone=True, precision=3)
"""``TIMESTAMPTZ(3)``. Audit ordering is reconstructed from these, and millisecond
precision is what R11.C2 asks for."""


NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names. Without this, an unnamed constraint gets a
server-generated name and the migration that has to drop it later cannot say
which one it means."""


class Base(DeclarativeBase):
    """Declarative base carrying the metadata every migration is generated from."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text(),
        bool: Boolean(),
        int: Integer(),
        bytes: LargeBinary(),
        datetime: TIMESTAMPTZ,
        uuid.UUID: UUID(as_uuid=True),
        Decimal: PROBABILITY,
    }
    """``Decimal`` maps to the probability type because that is the overwhelming
    majority of decimal columns. Confidence, signed increments and money override
    it explicitly at the column, and money is never a ``Decimal`` in the first
    place."""


# ---------------------------------------------------------------------------
# Shared mixins
# ---------------------------------------------------------------------------


class IdMixin:
    """``id UUID PRIMARY KEY DEFAULT gen_random_uuid()``.

    Server-side default as well as a client-side one so a row inserted by a
    migration, a seed script or a psql session still gets a well-formed key.
    UUIDs rather than sequences because ids appear in job payloads and audit
    records, and a monotonic integer there leaks volume across tenants.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class CreatedAtMixin:
    """``created_at TIMESTAMPTZ NOT NULL``.

    The Python default routes through ``platform.clock.now`` rather than
    ``datetime.now`` so a test that freezes the clock also freezes what gets
    written, which is what makes retention and window assertions exact.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ,
        nullable=False,
        default=now,
        server_default=text("now()"),
    )


class MerchantScopedMixin:
    """``merchant_id UUID NOT NULL REFERENCES merchant``.

    Present on every table except ``merchant``. It is what the repository layer
    filters on, what the row-level-security policy compares against, and the
    leading column of nearly every index — three mechanisms that all collapse if
    one table is allowed to omit it.
    """

    @declared_attr.directive
    def merchant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("merchant.id", ondelete="RESTRICT"),
            nullable=False,
            index=False,
        )


class RowBase(Base, IdMixin, CreatedAtMixin, MerchantScopedMixin):
    """The shape of a tenant-scoped row: id, created_at, merchant_id.

    Abstract, so it contributes no table of its own.
    """

    __abstract__ = True


# ---------------------------------------------------------------------------
# Enum-backed TEXT columns
# ---------------------------------------------------------------------------


class EnumBackedColumn(NamedTuple):
    """One ``TEXT`` column whose permitted values come from a domain enum."""

    table: str
    column: str
    enum: type[StrEnum]
    constraint_name: str

    @property
    def permitted(self) -> tuple[str, ...]:
        return tuple(member.value for member in self.enum)


_registry: list[EnumBackedColumn] = []


def enum_check(
    table: str,
    column: str,
    enum: type[StrEnum],
    *,
    extra: tuple[str, ...] = (),
) -> CheckConstraint:
    """A ``CHECK`` restricting ``column`` to the members of ``enum``.

    Derived from the enum rather than hand-listed, so the database and
    ``revora.domain`` cannot drift. ``extra`` admits sentinel strings that are
    legitimately storable but are not enum members — ``NOT_ESTABLISHED`` is the
    example, and it is deliberately not an enum member because it means "we have
    not measured this", which is not a value of the thing being measured.

    A ``NULL`` passes: ``NULL IN (...)`` evaluates to unknown, and a nullable
    column means the fact has not been recorded yet, which is different from
    having been recorded as something illegal.
    """
    permitted = tuple(member.value for member in enum) + extra
    rendered = ", ".join(f"'{value}'" for value in permitted)
    name = f"{column}_enum"
    _registry.append(EnumBackedColumn(table, column, enum, f"ck_{table}_{name}"))
    # The column is always quoted. Two of these columns are called ``group``,
    # which is a reserved word, and an unquoted reference there is a syntax error
    # that would only surface when the migration runs.
    return CheckConstraint(f'"{column}" IN ({rendered})', name=name)


def money_column(*, nullable: bool = True, **kwargs: Any) -> Mapped[int]:
    """A ``BIGINT`` money column. Exists so no column can be spelt any other way."""
    return mapped_column(MONEY, nullable=nullable, **kwargs)


def nonnegative_money_check(column: str) -> CheckConstraint:
    """``column >= 0`` for a cost.

    Costs are never negative — a negative cost is a revenue claim wearing a
    disguise, and the value model already has a signed column for that.
    """
    return CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")


def enum_backed_columns() -> tuple[EnumBackedColumn, ...]:
    """Every enum-backed column declared so far.

    Populated as the model modules import. ``models/__init__`` imports all of
    them, so importing that package is enough to see the complete set — which is
    what the constraint test relies on to check every column rather than a
    remembered subset.
    """
    return tuple(_registry)


ENUM_BACKED_COLUMNS = enum_backed_columns
"""Callable rather than a tuple: the registry is still filling up at import time
of this module, and a snapshot taken here would be empty."""

# Re-exported so a model module can build a SMALLINT counter without importing
# SQLAlchemy's name for it in six places.
COUNTER: Final = SmallInteger

"""The base repository, whose whole purpose is that ``merchant_id`` is required.

Tenant isolation in this system has two mechanisms and this is the primary one.
Row-level security is the backstop. The reason the application layer is primary is
that it is where the mistake actually happens: somebody writes a query, forgets the
filter, and the test passes because the test database has one tenant.

So there is no read, list, count or export function anywhere in this package that
does not take ``merchant_id`` as a required argument. Not defaulted, not optional,
not inferred from a context variable — a context variable can be unset, and an
unset context variable that silently means "all tenants" is the failure this design
is trying to make unreachable.

The rule is enforced three ways: every method below takes the argument positionally
and first; every scoped statement is built by :meth:`MerchantScopedRepository.scoped`
rather than by hand; and a test walks the public functions of this package asserting
the parameter is present. The last one is what catches a new repository written six
months from now.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from revora.persistence.models.base import RowBase

__all__ = ["MerchantScopedRepository"]


class MerchantScopedRepository[ModelT: RowBase]:
    """Reads for one model, always filtered to one merchant.

    Subclasses set :attr:`model` and add whatever query methods they need, building
    every one of them from :meth:`scoped`.
    """

    model: ClassVar[type[Any]]

    __slots__ = ("session",)

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- statement construction ------------------------------------------------

    def scoped(self, merchant_id: uuid.UUID) -> Select[Any]:
        """A select over this model, already filtered to ``merchant_id``.

        Every read in every subclass starts here. A subclass that builds a
        ``select(self.model)`` directly has bypassed the only mechanism that makes
        the filter unforgettable, and review should treat that as the bug it is.
        """
        return select(self.model).where(self.model.merchant_id == merchant_id)

    # -- reads -----------------------------------------------------------------

    def get(self, merchant_id: uuid.UUID, row_id: uuid.UUID) -> ModelT | None:
        """One row by id, within one merchant.

        Scoped even though the id is a UUID and guessing one is impractical. The
        point is not to stop a guess; it is that an id arriving from a URL is
        attacker-controlled input, and "the id is unguessable" is a statement about
        difficulty rather than about authorization.
        """
        statement = self.scoped(merchant_id).where(self.model.id == row_id)
        return self.session.execute(statement).scalar_one_or_none()

    def list_page(
        self,
        merchant_id: uuid.UUID,
        *,
        limit: int,
        offset: int = 0,
    ) -> Sequence[ModelT]:
        """A page of rows, newest first, within one merchant.

        ``limit`` is required rather than defaulted. An unbounded list is a query
        whose cost grows with a tenant's success, and the dashboard has a configured
        page size precisely so the bound is a decision rather than an accident.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .order_by(self.model.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(statement).scalars())

    def count(self, merchant_id: uuid.UUID) -> int:
        """How many rows this merchant has."""
        statement = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.merchant_id == merchant_id)
        )
        return int(self.session.execute(statement).scalar_one())

    # -- writes ----------------------------------------------------------------

    def add(self, merchant_id: uuid.UUID, row: ModelT) -> ModelT:
        """Stage a row, forcing its ``merchant_id`` to the argument.

        Overwriting rather than validating on purpose. A caller that built the row
        with a different merchant has made a mistake, and the safe resolution is the
        merchant the caller was authorized for — not the one it typed into the
        object.
        """
        row.merchant_id = merchant_id
        self.session.add(row)
        return row

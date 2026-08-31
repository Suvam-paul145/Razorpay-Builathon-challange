"""Tenant reads that precede knowing the tenant.

Almost every read in this package requires a ``merchant_id``. Two do not, and cannot,
because they run before the tenant is known: the worker's ``claimable_merchant_ids``
(in ``jobs``) and this module's :func:`merchant_by_slug`. The inbound webhook route
receives a URL slug and must resolve it to a merchant before it can bind a
transaction to one or verify a signature against one.

Resolving by slug returns the ``merchant`` row and nothing tenant-scoped. The
``merchant`` table is the one table with no ``merchant_id`` and no row-level-security
policy — it *is* the tenant — so reading it outside a tenant binding is correct
rather than a hole. Everything the route does after resolving the merchant goes
through a merchant-bound transaction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from revora.persistence.models import Merchant

__all__ = ["merchant_by_slug"]


def merchant_by_slug(session: Session, slug: str) -> Merchant | None:
    """The merchant for a URL slug, or ``None`` if the slug is unknown.

    ``None`` is the route's cue to answer without disclosing whether the slug exists
    — an unknown slug and a bad signature are both answered without detail, because
    the endpoint is unauthenticated and must not be an oracle for valid slugs.
    """
    return session.execute(
        select(Merchant).where(Merchant.slug == slug)
    ).scalar_one_or_none()

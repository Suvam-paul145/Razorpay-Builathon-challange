"""Tenant reads that precede knowing the tenant.

Almost every read in this package requires a ``merchant_id``. Three do not, and cannot,
because they run before the tenant is known: the worker's ``claimable_merchant_ids``
(in ``jobs``), the ticker's :func:`schedulable_merchants`, and this module's
:func:`merchant_by_slug`. The inbound webhook route receives a URL slug and must
resolve it to a merchant before it can bind a transaction to one or verify a
signature against one.

Resolving by slug returns the ``merchant`` row and nothing tenant-scoped. The
``merchant`` table is the one table with no ``merchant_id`` and no row-level-security
policy — it *is* the tenant — so reading it outside a tenant binding is correct
rather than a hole. Everything the route does after resolving the merchant goes
through a merchant-bound transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from revora.persistence.models import Merchant
from revora.platform.config import DEFAULTS_MERCHANT_ID

__all__ = ["merchant_by_slug", "schedulable_merchants"]


def merchant_by_slug(session: Session, slug: str) -> Merchant | None:
    """The merchant for a URL slug, or ``None`` if the slug is unknown.

    ``None`` is the route's cue to answer without disclosing whether the slug exists
    — an unknown slug and a bad signature are both answered without detail, because
    the endpoint is unauthenticated and must not be an oracle for valid slugs.
    """
    return session.execute(
        select(Merchant).where(Merchant.slug == slug)
    ).scalar_one_or_none()


def schedulable_merchants(session: Session) -> Sequence[tuple[uuid.UUID, str]]:
    """Every real tenant, as ``(id, slug)`` pairs. What the ticker iterates.

    The counterpart to ``claimable_merchant_ids``: the worker asks which tenants have work
    *waiting*, and the ticker asks which tenants should have work *created*. Those are
    different questions and the second one cannot be answered from the ``job`` table, because
    a tenant with an empty queue is precisely the tenant whose sweeps have not been enqueued
    yet. So this reads the ``merchant`` table, which is the one table with no tenant scope.

    The slug comes back alongside the id purely so the ticker's per-tick log names a merchant
    a human recognises rather than a UUID. Nothing tenant-scoped is returned, and every read
    the caller makes afterwards is bound to one of these ids.

    **The sentinel tenant is excluded by id**, not by ``state``. It holds the platform
    configuration defaults and has no cases, no intents and no contact data, so sweeping it
    would enqueue seven jobs per tick that each do nothing. Filtering on
    :data:`DEFAULTS_MERCHANT_ID` rather than on ``state <> 'SYSTEM'`` is deliberate: a
    suspended or onboarding merchant still has open cases that must expire, intents that must
    reconcile and contact data that must be redacted on R17.C11's deadline, so excluding it by
    state would stop exactly the sweeps whose whole purpose is to run without anyone asking.
    ``DEFAULTS_MERCHANT_ID`` is also the constant migration ``0004`` seeded the row with, so
    this filter names the same thing the schema does rather than repeating its slug literal.

    Ordered by id so two ticks visit merchants in the same sequence. Nothing depends on the
    order — every enqueue is independent and dedupe-keyed — but a stable order makes two ticks'
    log output comparable line by line, which is the difference between reading a diff and
    reading two lists.
    """
    statement = (
        select(Merchant.id, Merchant.slug)
        .where(Merchant.id != DEFAULTS_MERCHANT_ID)
        .order_by(Merchant.id)
    )
    return [(row[0], row[1]) for row in session.execute(statement).all()]

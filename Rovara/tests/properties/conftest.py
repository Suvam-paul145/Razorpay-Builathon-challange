"""Fixtures for the property tier, including the database harness a few properties need.

Most properties here are pure and take no fixtures at all. Property 3 is the exception and
cannot be otherwise: "exactly one external effect survives an arbitrary crash" is a claim
about what a real transaction does when it is abandoned, and a fake session that never
talked to Postgres would let a broken implementation pass. So the pg harness is re-exported
here rather than duplicated.

Re-exported by import rather than moved, deliberately. Those fixtures are session-scoped
and build the container, create the ``revora_app`` role and run every Alembic migration;
having two definitions of that would mean two migrated databases per run and a real chance
of the two drifting. Importing the names binds this directory to the same fixture objects
the persistence tier uses, so there is exactly one migrated database per session no matter
which tier asked for it first.

Only the session-scoped fixtures are taken. The function-scoped ``merchant_id`` is
deliberately *not* re-exported: Hypothesis runs many examples inside one test function, so
a function-scoped fixture is set up once for the whole run and every example would share
one merchant. Since Property 3 is a statement about a unique constraint scoped by
``merchant_id``, sharing that scope across examples would let one example's rows collide
with another's and report a failure that says nothing about the property. Each example
seeds its own merchant instead.
"""

from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401 - re-exported as fixtures
    app_engine,
    migrated_url,
    owner_engine,
    owner_url,
)

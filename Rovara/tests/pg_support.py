"""A migrated PostgreSQL, shared by every tier that needs a real server.

These fixtures lived in ``tests/persistence/conftest.py`` until the synthetic evidence harness
needed them too. A conftest's fixtures are visible only to tests below it, so the choice was to
duplicate the migration bootstrap in a second directory or to lift it to a plugin both can see.
Duplicating it would have meant two places where the server version, the app role and the
migration invocation could drift apart — and the whole point of migrating rather than calling
``create_all`` is that the migration is the thing that drifts.

Registered from ``tests/conftest.py`` as a plugin, so the fixtures are available suite-wide.
Nothing is started eagerly: they are session-scoped and lazy, so a run that touches no ``pg``
test never contacts a database.

**Three tiers use these, and one definition is the point.** The persistence constraint tests, the
synthetic evidence harness, and Property 3 in the property tier — "exactly one external effect
survives an arbitrary crash", which is a claim about what a real transaction does when it is
abandoned and cannot be checked against a fake session. Each of those directories previously
reached for the fixtures a different way, and the last one did it by importing another
directory's conftest. Being session-scoped, the fixtures build the role and run every Alembic
migration, so a second definition would mean a second migrated database per run and a real chance
of the two drifting.

Note what is deliberately *not* here: a ``merchant_id`` fixture. Hypothesis runs many examples
inside one test function, and a function-scoped fixture is set up once for that whole function, so
every example would share one merchant. Property 3 is a statement about a unique constraint scoped
by ``merchant_id``, so sharing that scope across examples would let one example's rows collide with
another's and report a failure that says nothing about the property. :func:`insert_merchant` is a
plain function for exactly that reason — a caller decides when a new universe begins.

The database comes from one of two places, in this order:

1. ``REVORA_TEST_DATABASE_URL``, if set. For a developer with a local server, and for CI where
   the database is a service container rather than something the test suite starts.
2. ``testcontainers``, otherwise.

If neither is available the whole tier skips with a reason rather than failing. That is
deliberate: these tests run on every push, and a developer without Docker should still be able to
run the pure and model tiers without a wall of red.

Two engines, because the RLS tests need both. The owner engine runs migrations and sets up
fixtures. The ``revora_app`` engine is what the application uses, and it is the only one the
policies actually apply to: Postgres exempts a table's owner from row-level security unless the
table forces it, and forcing it would block the seed migration from writing the sentinel tenant's
configuration.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from revora.persistence.repositories.config import invalidate_configuration_cache

REPO_ROOT = Path(__file__).resolve().parents[1]

APP_ROLE = "revora_app"
APP_PASSWORD = "revora_app"

__all__ = [
    "APP_PASSWORD",
    "APP_ROLE",
    "REPO_ROOT",
    "insert_merchant",
]


@pytest.fixture(scope="session")
def owner_url() -> Iterator[str]:
    """A URL for a PostgreSQL this tier may create and drop objects in."""
    external = (os.environ.get("REVORA_TEST_DATABASE_URL") or "").strip()
    if external:
        yield external
        return

    # The module moved in testcontainers 4.x and the old path warns. Both are tried so
    # the tier does not start failing on a patch upgrade of a test-only dependency.
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:  # pragma: no cover - depends on the installed extras
            pytest.skip(
                "testcontainers is not installed and REVORA_TEST_DATABASE_URL is unset"
            )

    # 18 to match docker-compose and the managed Neon instance. Testing against an older
    # server than production runs on is how a migration passes every gate and then fails
    # on deploy.
    try:
        with PostgresContainer("postgres:18-alpine", driver="psycopg") as container:
            yield container.get_connection_url()
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"no container runtime available for the pg tier: {exc}")


@pytest.fixture(scope="session")
def migrated_url(owner_url: str) -> str:
    """The same URL, with the app role created and every migration applied.

    The role is created *before* the migrations run, because 0002 and 0003 grant and
    revoke privileges on it and skip themselves entirely when it does not exist. A
    database migrated without the role would pass the append-only test on the trigger
    alone and never exercise the grant.
    """
    from alembic.config import Config

    from alembic import command

    engine = create_engine(owner_url, future=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
        ).first()
        if exists is None:
            connection.execute(
                text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
            )
    engine.dispose()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    previous = os.environ.get("REVORA_DATABASE_URL")
    os.environ["REVORA_DATABASE_URL"] = owner_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("REVORA_DATABASE_URL", None)
        else:
            os.environ["REVORA_DATABASE_URL"] = previous

    # The seed migrations write ``app_config``, and unlike a deployment — where Alembic runs as
    # its own process and there is no cache to stale — here they run inside the test process,
    # alongside the memoization in ``persistence.repositories.config``. Every writer of
    # configuration invalidates; this is one of them.
    invalidate_configuration_cache()
    return owner_url


@pytest.fixture(scope="session")
def owner_engine(migrated_url: str) -> Iterator[Engine]:
    """Engine connected as the database owner. Exempt from row-level security."""
    engine = create_engine(migrated_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(migrated_url: str) -> Iterator[Engine]:
    """Engine connected as ``revora_app`` — the role the policies actually apply to."""
    url = make_url(migrated_url).set(username=APP_ROLE, password=APP_PASSWORD)
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


def insert_merchant(engine: Engine, *, display_name: str = "Test merchant") -> uuid.UUID:
    """Insert a merchant and return its id.

    A fresh merchant per test rather than a shared one that gets truncated: every table is
    scoped by ``merchant_id``, so a unique merchant gives each test its own universe without a
    teardown that could delete another test's rows mid-run.
    """
    new_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, :name, 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(new_id), "slug": f"test-{new_id}", "name": display_name},
        )
    return new_id

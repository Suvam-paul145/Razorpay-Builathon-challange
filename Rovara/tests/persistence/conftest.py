"""A migrated PostgreSQL for the constraint tier, and the fixtures built on it.

The database comes from one of two places, in this order:

1. ``REVORA_TEST_DATABASE_URL``, if set. For a developer with a local server, and for
   CI where the database is a service container rather than something the test suite
   starts.
2. ``testcontainers``, otherwise.

If neither is available the whole tier skips with a reason rather than failing. That
is deliberate: these tests are the ``pg`` tier, they run on every push, and a
developer without Docker should still be able to run the pure and model tiers without
a wall of red.

The schema is created by running the actual migrations, not by ``create_all``. If it
were ``create_all``, these tests would prove the *models* reject what they must while
saying nothing about the migration that production actually applies — and the
migration is the thing that can drift.

Two engines, because the RLS test needs both. The owner engine runs migrations and
sets up fixtures. The ``revora_app`` engine is what the application uses, and it is
the only one the policies apply to: Postgres exempts a table's owner from row-level
security unless the table forces it, and forcing it would block the seed migration
from writing the sentinel tenant's configuration.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from revora.domain.enums import CaseState
from revora.platform.clock import now

REPO_ROOT = Path(__file__).resolve().parents[2]

APP_ROLE = "revora_app"
APP_PASSWORD = "revora_app"

pytestmark = pytest.mark.pg


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

    try:
        with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
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


@pytest.fixture
def merchant_id(owner_engine: Engine) -> uuid.UUID:
    """A fresh merchant per test.

    Fresh rather than shared-and-truncated: every table is scoped by ``merchant_id``,
    so a unique merchant gives each test its own universe without a teardown that
    could delete another test's rows mid-run.
    """
    return _insert_merchant(owner_engine)


@pytest.fixture
def other_merchant_id(owner_engine: Engine) -> uuid.UUID:
    """A second merchant, for the cross-tenant tests."""
    return _insert_merchant(owner_engine)


def _insert_merchant(engine: Engine) -> uuid.UUID:
    new_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Test merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(new_id), "slug": f"test-{new_id}"},
        )
    return new_id


# ---------------------------------------------------------------------------
# Row builders. Only the columns a constraint depends on are interesting; the rest
# are filled with the least surprising legal value so a failure is never about a
# missing NOT NULL.
# ---------------------------------------------------------------------------


def insert_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    provider_payment_id: str,
    state: CaseState = CaseState.DETECTED,
    case_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a recovery case and return its id."""
    new_id = case_id or uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, detected_at, window_end_at, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :provider_payment_id, 250000,
                    'INR', :customer_key, :detected_at, :window_end_at, now()
                )
                """
            ),
            {
                "id": str(new_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "provider_payment_id": provider_payment_id,
                "customer_key": f"ck-{new_id}",
                "detected_at": moment,
                "window_end_at": moment + timedelta(hours=168),
            },
        )
    return new_id


def insert_policy_decision(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> uuid.UUID:
    """Insert an approved policy decision, since an intent cannot exist without one."""
    new_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, 'PAYMENT_LINK', 'POLICY_CHECK', 1, now()
                )
                """
            ),
            {
                "id": str(new_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
            },
        )
    return new_id


def audit_row_values(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID | None = None,
    *,
    seq: int | None = None,
) -> dict[str, object]:
    """The parameter set for one audit record insert."""
    return {
        "merchant_id": str(merchant_id),
        "case_id": None if case_id is None else str(case_id),
        "seq": seq,
        "event_type": "STATE_TRANSITION",
        "actor": "test",
        "correlation_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(UTC),
    }


AUDIT_INSERT = text(
    """
    INSERT INTO audit_record (
        merchant_id, case_id, seq, event_type, actor, correlation_id, occurred_at,
        created_at
    ) VALUES (
        :merchant_id, :case_id, :seq, :event_type, :actor, :correlation_id,
        :occurred_at, now()
    ) RETURNING id
    """
)

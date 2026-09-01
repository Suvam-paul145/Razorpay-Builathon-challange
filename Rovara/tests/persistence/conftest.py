"""Fixtures and row builders for the constraint tier.

The migrated-PostgreSQL fixtures this tier runs on (``owner_url``, ``migrated_url``,
``owner_engine``, ``app_engine``) live in :mod:`tests.pg_support`, registered as a plugin from the
root conftest. They moved there when the synthetic evidence harness needed the same server: a
conftest's fixtures are visible only to tests below it, and a second copy of the migration
bootstrap would have been two places for the server version, the app role and the migration
invocation to drift apart.

The schema is created by running the actual migrations, not by ``create_all``. If it were
``create_all``, these tests would prove the *models* reject what they must while saying nothing
about the migration that production actually applies — and the migration is the thing that can
drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from revora.domain.enums import CaseState
from revora.platform.clock import now
from tests.pg_support import APP_PASSWORD, APP_ROLE, REPO_ROOT, insert_merchant

__all__ = [
    "APP_PASSWORD",
    "APP_ROLE",
    "AUDIT_INSERT",
    "REPO_ROOT",
    "audit_row_values",
    "insert_case",
    "insert_policy_decision",
]

# There is deliberately no `pytestmark = pytest.mark.pg` here. There was, and it did
# nothing: a module-level `pytestmark` applies to tests defined in that module, and a
# conftest defines none. It read as a directory-wide marker and was not one, which is how
# `test_merchant_scoping.py` sat unmarked and outside CI's fast tier while its own
# docstring claimed to be in it. Every module in this directory sets its own `pg` marker.


@pytest.fixture
def merchant_id(owner_engine: Engine) -> uuid.UUID:
    """A fresh merchant per test.

    Fresh rather than shared-and-truncated: every table is scoped by ``merchant_id``,
    so a unique merchant gives each test its own universe without a teardown that
    could delete another test's rows mid-run.
    """
    return insert_merchant(owner_engine)


@pytest.fixture
def other_merchant_id(owner_engine: Engine) -> uuid.UUID:
    """A second merchant, for the cross-tenant tests."""
    return insert_merchant(owner_engine)


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

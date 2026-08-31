"""The startup schema check, and the configuration the seed migration wrote.

Both are startup concerns, and both fail in ways that are hard to diagnose later. A
worker on an older schema than the API produces a wrong recovery number rather than an
error; a process running on code defaults rather than stored configuration honours a
bound the settings screen does not show.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.schema import (
    EXPECTED_REVISION,
    SchemaRevisionMismatchError,
    current_revision,
    verify_schema_revision,
)
from revora.persistence.repositories.session import case_advisory_key
from revora.platform.config import CATALOGUE, DEFAULT_CONFIG_VERSION

pytestmark = pytest.mark.pg

EXPECTED_COOLDOWN = timedelta(hours=24)
EXPECTED_COST_RATIO = Decimal("0.30")
EXPECTED_INGEST_ACK = timedelta(milliseconds=1500)
EXPECTED_RISK_REASONS = frozenset({"payment_risk_check_failed", "compliance_violation"})


def test_migrated_database_is_at_the_expected_revision(owner_engine: Engine) -> None:
    """The head the migrations reach is the head this build expects.

    If this fails after adding a migration, the fix is to bump ``EXPECTED_REVISION`` in
    the same commit — which is the discipline that makes the startup check meaningful.
    """
    assert current_revision(owner_engine) == EXPECTED_REVISION
    assert verify_schema_revision(owner_engine) == EXPECTED_REVISION


def test_startup_refuses_on_a_revision_mismatch(owner_engine: Engine) -> None:
    """A mismatch raises rather than degrading.

    The message has to say which way round the mismatch is, because "the database is
    behind" and "this build is behind" call for opposite actions.
    """
    with pytest.raises(SchemaRevisionMismatchError) as caught:
        verify_schema_revision(owner_engine, expected="9999_not_a_revision")

    assert "9999_not_a_revision" in str(caught.value)
    assert caught.value.found == EXPECTED_REVISION


def test_seeded_configuration_loads_as_a_typed_accessor(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Every bound is present, typed, and nothing fell back to a code default.

    ``defaulted_keys`` being empty is the real assertion. A non-empty set would mean the
    process is running on placeholders that no stored row backs, which is invisible
    until somebody asks why a case stopped after three attempts.
    """
    with Session(owner_engine) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    assert config.defaulted_keys == frozenset()
    assert config.version == DEFAULT_CONFIG_VERSION

    # The typed accessor, not a string lookup.
    assert config.MAX_RECOVERY_ATTEMPTS == 3
    assert config.MAX_CUSTOMER_MESSAGES == 2
    assert config.COOLDOWN_INTERVAL == EXPECTED_COOLDOWN
    assert config.MIN_NET_VALUE_THRESHOLD == 5000
    assert config.MAX_COST_TO_VALUE_RATIO == EXPECTED_COST_RATIO


def test_amended_bounds_are_seeded_with_the_amended_values(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The three places the design departs from the requirements table."""
    with Session(owner_engine) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    # 1500 ms, not the requirements table's 3000: the provider's own webhook deadline is
    # five seconds and 3000 leaves too little margin.
    assert config.INGEST_ACK_TIMEOUT == EXPECTED_INGEST_ACK
    # 300 characters, not 480: the provider's payment-link description does not take 480.
    assert config.MAX_MESSAGE_LENGTH == 300
    # A configured set, from which the fraud-or-risk condition derives.
    assert config.RISK_REASON_CODES == EXPECTED_RISK_REASONS


def test_every_seeded_row_is_marked_as_an_assumption(owner_engine: Engine) -> None:
    """None of these values was measured, and every row says so.

    The flag is what lets the settings screen show "3 attempts, assumed" rather than
    "3 attempts", which is the difference between a placeholder and a claim.
    """
    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT key, is_assumption, config_version FROM app_config "
                "WHERE config_version = :version"
            ),
            {"version": DEFAULT_CONFIG_VERSION},
        ).all()

    assert len(rows) == len(CATALOGUE)
    assert all(is_assumption for _, is_assumption, _ in rows)


def test_a_merchant_row_overrides_the_seeded_default(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A tenant's own row wins, and the loaded version becomes that tenant's version.

    Which is what a policy decision then records, so a decision made under a message cap
    of one stays distinguishable from one made under a cap of two.
    """
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_config (
                    merchant_id, key, value, value_kind, config_version, is_active,
                    effective_at, is_assumption, created_at
                ) VALUES (
                    :merchant_id, 'MAX_CUSTOMER_MESSAGES', '1', 'INTEGER',
                    '2025.02.0-merchant-choice', true, now(), false, now()
                )
                """
            ),
            {"merchant_id": str(merchant_id)},
        )

    with Session(owner_engine) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    assert config.MAX_CUSTOMER_MESSAGES == 1
    assert config.MAX_RECOVERY_ATTEMPTS == 3, "unchanged bounds still come from the defaults"
    assert config.version == "2025.02.0-merchant-choice"


def test_advisory_lock_key_is_stable_and_fits_a_bigint(owner_engine: Engine) -> None:
    """The per-case advisory key round-trips through Postgres.

    Derived from the UUID's own bits rather than a Python hash, because ``hash()`` is
    salted per process and a lock key that differs between two workers is not a lock.
    """
    case_id = uuid.uuid4()
    key = case_advisory_key(case_id)

    with owner_engine.begin() as connection:
        taken = connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        ).scalar_one()
        assert taken is True

    assert case_advisory_key(case_id) == key
    assert -(2**63) <= key < 2**63

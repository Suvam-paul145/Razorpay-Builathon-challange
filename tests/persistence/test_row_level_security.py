"""Row-level security, tested as the application role rather than as the owner.

The distinction is the whole test. Postgres exempts a table's owner from row-level
security unless the table forces it, so a test run as the owner would read every
tenant's rows and pass an assertion that proves nothing. These run as ``revora_app``,
which is what the application connects as.

What is being checked is deliberately narrow: with the session variable set to one
merchant, another merchant's rows are not visible, and a write for another merchant
cannot be committed. That is the backstop working. The primary control — a required
``merchant_id`` argument on every repository read — is checked separately, without a
database.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from revora.persistence.repositories.session import TENANT_SETTING
from revora.platform.config import DEFAULTS_MERCHANT_ID
from tests.persistence.conftest import insert_case

pytestmark = pytest.mark.pg

_SET_TENANT = text(f"SELECT set_config('{TENANT_SETTING}', :merchant_id, true)")


def test_cross_tenant_select_returns_nothing(
    owner_engine: Engine,
    app_engine: Engine,
    merchant_id: uuid.UUID,
    other_merchant_id: uuid.UUID,
) -> None:
    """Bound to one merchant, a query with no tenant filter sees only that merchant.

    The query below is written the way a bug is written — no ``WHERE merchant_id`` at
    all. That is the point: this is the mistake RLS exists to catch.
    """
    mine = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    theirs = insert_case(
        owner_engine, other_merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}"
    )

    with app_engine.begin() as connection:
        connection.execute(_SET_TENANT, {"merchant_id": str(merchant_id)})
        visible = {
            row[0] for row in connection.execute(text("SELECT id FROM recovery_case"))
        }

    assert mine in visible
    assert theirs not in visible


def test_untenanted_transaction_sees_nothing(
    owner_engine: Engine, app_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """With no session variable set, a tenant table is empty.

    Fails closed rather than open. An unset variable returning every row would make a
    forgotten ``set_tenant`` the most dangerous line of code in the system; instead it
    produces an obvious, harmless nothing.
    """
    insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    with app_engine.begin() as connection:
        count = connection.execute(text("SELECT count(*) FROM recovery_case")).scalar_one()

    assert count == 0


def test_empty_session_variable_is_treated_as_unset(
    owner_engine: Engine, app_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """An empty setting yields no rows rather than a cast error.

    Without the ``NULLIF`` in the policy, ``''::uuid`` raises and a missing binding
    surfaces as a confusing type error at query time instead of an empty result.
    """
    insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    with app_engine.begin() as connection:
        connection.execute(_SET_TENANT, {"merchant_id": ""})
        count = connection.execute(text("SELECT count(*) FROM recovery_case")).scalar_one()

    assert count == 0


def test_write_for_another_tenant_is_rejected(
    app_engine: Engine, merchant_id: uuid.UUID, other_merchant_id: uuid.UUID
) -> None:
    """``WITH CHECK`` stops a tenant writing a row it would not be able to read back.

    Without it, a bug could insert a row belonging to another merchant and then be
    unable to see, audit or undo it.
    """
    with pytest.raises(DBAPIError) as caught, app_engine.begin() as connection:
        connection.execute(_SET_TENANT, {"merchant_id": str(merchant_id)})
        connection.execute(
            text(
                """
                    INSERT INTO customer_consent (
                        merchant_id, customer_key, opted_out, effective_at, created_at
                    ) VALUES (:merchant_id, 'ck-test', true, now(), now())
                    """
            ),
            {"merchant_id": str(other_merchant_id)},
        )

    assert "row-level security" in str(caught.value).lower()


def test_seeded_configuration_defaults_remain_readable(
    app_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The sentinel tenant's rows are visible, and only on ``app_config``.

    Without this exception a merchant that has overridden nothing would load an empty
    configuration under RLS. With it, the fallback layer works and every other table
    stays strictly one-tenant.
    """
    with app_engine.begin() as connection:
        connection.execute(_SET_TENANT, {"merchant_id": str(merchant_id)})
        defaults = connection.execute(
            text("SELECT count(*) FROM app_config WHERE merchant_id = :defaults"),
            {"defaults": str(DEFAULTS_MERCHANT_ID)},
        ).scalar_one()
        sentinel_cases = connection.execute(
            text("SELECT count(*) FROM recovery_case WHERE merchant_id = :defaults"),
            {"defaults": str(DEFAULTS_MERCHANT_ID)},
        ).scalar_one()

    assert defaults > 40, "the seeded bounds should be readable as the fallback layer"
    assert sentinel_cases == 0


def test_every_tenant_scoped_table_has_the_policy_enabled(owner_engine: Engine) -> None:
    """All thirty tenant-scoped tables, not the ones somebody remembered.

    The list is derived from the metadata, so a table added later without a policy
    fails here rather than becoming the one table where a repository bug reads across
    merchants.
    """
    from revora.persistence.models import TENANT_SCOPED_TABLES

    with owner_engine.connect() as connection:
        enabled = {
            row[0]
            for row in connection.execute(
                text("SELECT relname FROM pg_class WHERE relrowsecurity")
            )
        }
        policies = {
            row[0]
            for row in connection.execute(
                text("SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'")
            )
        }

    missing_rls = set(TENANT_SCOPED_TABLES) - enabled
    missing_policy = set(TENANT_SCOPED_TABLES) - policies
    assert not missing_rls, f"row-level security not enabled on: {sorted(missing_rls)}"
    assert not missing_policy, f"no tenant_isolation policy on: {sorted(missing_policy)}"

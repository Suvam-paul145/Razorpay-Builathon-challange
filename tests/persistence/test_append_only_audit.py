"""Both append-only mechanisms, tested independently of each other.

The trigger is tested as the owner, because the owner has every grant and so the
trigger is the only thing that can stop it. The grant is tested as ``revora_app``,
where the privilege check fires first. Testing only one would leave the other
untested precisely when it matters — the whole point of having two is that one can be
undone by accident.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.persistence.conftest import AUDIT_INSERT, audit_row_values, insert_case

pytestmark = pytest.mark.pg


def test_update_on_audit_record_raises_for_the_owner(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The trigger stops a mutation even for a role that holds every grant."""
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    with owner_engine.begin() as connection:
        record_id = connection.execute(
            AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=1)
        ).scalar_one()

    with pytest.raises(DBAPIError) as caught, owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE audit_record SET actor = 'rewritten' WHERE id = :id"),
            {"id": str(record_id)},
        )

    assert "append-only" in str(caught.value)


def test_delete_on_audit_record_raises_for_the_owner(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Same trigger, other operation. A record cannot be removed either."""
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    with owner_engine.begin() as connection:
        record_id = connection.execute(
            AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=1)
        ).scalar_one()

    with pytest.raises(DBAPIError) as caught, owner_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM audit_record WHERE id = :id"), {"id": str(record_id)}
        )

    assert "append-only" in str(caught.value)

    with owner_engine.connect() as connection:
        still_there = connection.execute(
            text("SELECT count(*) FROM audit_record WHERE id = :id"), {"id": str(record_id)}
        ).scalar_one()
    assert still_there == 1


def test_application_role_has_no_update_or_delete_grant(
    owner_engine: Engine, app_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The primary mechanism: the application role's privileges do not include mutation.

    Checked through the catalogue rather than by attempting an ``UPDATE``, because an
    attempted update would be refused by the trigger as well and the test would pass
    even if the grant had been restored — which is the exact failure the trigger exists
    to cover for.
    """
    with app_engine.connect() as connection:
        privileges = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = 'audit_record' AND grantee = 'revora_app'"
                )
            )
        }

    assert "SELECT" in privileges
    assert "INSERT" in privileges
    assert "UPDATE" not in privileges
    assert "DELETE" not in privileges
    assert "TRUNCATE" not in privileges


def test_truncate_is_refused_for_the_application_role(app_engine: Engine) -> None:
    """``TRUNCATE`` bypasses row triggers, so the revoked grant is the only defence.

    This is why the design revokes ``TRUNCATE`` explicitly rather than relying on the
    trigger: a ``BEFORE DELETE FOR EACH ROW`` trigger never fires for a truncate.
    """
    with pytest.raises(ProgrammingError), app_engine.begin() as connection:
        connection.execute(text("TRUNCATE audit_record"))


def test_mutation_rejected_is_itself_recorded_naming_the_actor(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A refused mutation produces its own audit record, naming who tried.

    Written by the separate insert-only function, on a fresh transaction. It has to be
    a different transaction: the trigger has already aborted the one that attempted the
    mutation, and nothing can be written on an aborted transaction.
    """
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    with owner_engine.begin() as connection:
        record_id = connection.execute(
            AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=1)
        ).scalar_one()

    with pytest.raises(DBAPIError), owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE audit_record SET actor = 'rewritten' WHERE id = :id"),
            {"id": str(record_id)},
        )

    correlation_id = uuid.uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "SELECT record_audit_mutation_rejected(:merchant_id, :actor, :operation, "
                ":correlation_id)"
            ),
            {
                "merchant_id": str(merchant_id),
                "actor": "merchant_user:auditor@example.test",
                "operation": "UPDATE",
                "correlation_id": str(correlation_id),
            },
        )

    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT event_type, actor, evidence, case_id, seq FROM audit_record "
                "WHERE correlation_id = :correlation_id"
            ),
            {"correlation_id": str(correlation_id)},
        ).one()

    event_type, actor, evidence, recorded_case_id, seq = row
    assert event_type == "AUDIT_MUTATION_REJECTED"
    assert actor == "merchant_user:auditor@example.test"
    # The operation goes in evidence rather than in `action`, which is constrained to
    # the recovery-action enum. 'UPDATE' is not a recovery action.
    assert evidence == {"attempted_operation": "UPDATE"}
    # Not attached to a case: this is a fact about an attempted mutation, not about the
    # case whose record was targeted, and giving it a seq would put it in that case's
    # ordering.
    assert recorded_case_id is None
    assert seq is None

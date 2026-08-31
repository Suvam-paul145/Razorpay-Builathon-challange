"""The four constraints that turn invariants into database facts.

Each test here asserts the thing the constraint exists to make impossible. They are
written against a real PostgreSQL because the guarantees are PostgreSQL's: ``ON
CONFLICT`` under concurrency, a partial unique index's predicate, and a unique
constraint's behaviour when two transactions race.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from revora.domain.enums import CaseState
from revora.platform.clock import now
from tests.persistence.conftest import insert_case, insert_policy_decision

pytestmark = pytest.mark.pg


EVENT_INSERT = text(
    """
    INSERT INTO webhook_event (
        merchant_id, provider_event_id, event_name, raw_payload_ciphertext,
        raw_payload_nonce, key_version, canonical, received_at, correlation_id,
        signature_verified, created_at
    ) VALUES (
        :merchant_id, :provider_event_id, 'payment.failed', :ciphertext, :nonce, 1,
        '{}'::jsonb, now(), :correlation_id, true, now()
    )
    ON CONFLICT (merchant_id, provider_event_id) DO NOTHING
    RETURNING id
    """
)


def _event_params(merchant_id: uuid.UUID, provider_event_id: str) -> dict[str, object]:
    return {
        "merchant_id": str(merchant_id),
        "provider_event_id": provider_event_id,
        "ciphertext": b"ciphertext-stands-in-for-AES-GCM-output",
        "nonce": b"123456789012",
        "correlation_id": str(uuid.uuid4()),
    }


def test_concurrent_duplicate_event_inserts_yield_exactly_one_row(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Two simultaneous deliveries of one provider_event_id persist once.

    Threads rather than sequential inserts, because sequential inserts would pass even
    without the constraint — the second one would simply see the first. What this
    checks is the case the provider actually produces: two retries in flight at once,
    both past the ``SELECT`` an unconstrained implementation would have relied on.
    """
    provider_event_id = f"evt_{uuid.uuid4()}"
    returned: list[uuid.UUID | None] = []
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def deliver() -> None:
        try:
            with owner_engine.begin() as connection:
                barrier.wait(timeout=10)
                row = connection.execute(
                    EVENT_INSERT, _event_params(merchant_id, provider_event_id)
                ).first()
                returned.append(None if row is None else row[0])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"delivery raised: {errors}"

    with owner_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT count(*) FROM webhook_event "
                "WHERE merchant_id = :merchant_id AND provider_event_id = :provider_event_id"
            ),
            {"merchant_id": str(merchant_id), "provider_event_id": provider_event_id},
        ).scalar_one()

    assert stored == 1
    # Exactly one caller was told it had inserted. The other got no row back, which is
    # the signal to write DUPLICATE_EVENT_DISCARDED and answer 200 rather than to
    # treat the delivery as new.
    assert sum(1 for value in returned if value is not None) == 1


def test_second_open_case_for_one_payment_is_rejected(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """One open case per payment. The second insert cannot commit."""
    payment_id = f"pay_{uuid.uuid4()}"
    insert_case(owner_engine, merchant_id, provider_payment_id=payment_id)

    with pytest.raises(IntegrityError) as caught:
        insert_case(owner_engine, merchant_id, provider_payment_id=payment_id)

    assert "one_open_case_per_payment" in str(caught.value)


def test_second_case_is_allowed_once_the_first_is_terminal(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A payment that failed again later gets a new case.

    The index is partial for exactly this reason. If it covered terminal cases too, a
    customer whose payment failed in March could never be helped when it failed again
    in June — and the per-case bounds would still be sitting at their March values.
    """
    payment_id = f"pay_{uuid.uuid4()}"
    first = insert_case(owner_engine, merchant_id, provider_payment_id=payment_id)

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET state = :state, terminal_reason = :reason "
                "WHERE id = :id"
            ),
            {
                "state": CaseState.EXPIRED.value,
                "reason": "RECOVERY_WINDOW_ELAPSED",
                "id": str(first),
            },
        )

    second = insert_case(owner_engine, merchant_id, provider_payment_id=payment_id)
    assert second != first


def test_one_payment_per_merchant_not_across_merchants(
    owner_engine: Engine, merchant_id: uuid.UUID, other_merchant_id: uuid.UUID
) -> None:
    """The constraint is per tenant.

    Provider payment ids are the provider's namespace, not ours. Scoping the index to
    the merchant is what keeps one tenant's traffic from blocking another's — a global
    unique index here would be a cross-tenant denial of service with a straight face.
    """
    payment_id = f"pay_{uuid.uuid4()}"
    insert_case(owner_engine, merchant_id, provider_payment_id=payment_id)
    insert_case(owner_engine, other_merchant_id, provider_payment_id=payment_id)


def test_second_intent_for_one_idempotency_key_is_rejected(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Exactly one external effect per authorization.

    This is the constraint that stands between a retry storm and a customer receiving
    the same payment link five times.
    """
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    key = f"idem_{uuid.uuid4()}"

    _insert_intent(owner_engine, merchant_id, case_id, decision_id, key, ordinal=1)

    with pytest.raises(IntegrityError) as caught:
        _insert_intent(owner_engine, merchant_id, case_id, decision_id, key, ordinal=2)

    assert "uq_execution_intent_merchant_id_idempotency_key" in str(caught.value)


def test_audit_seq_is_unique_per_case(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Gap-free ordering needs no duplicates, and that half is the constraint's job."""
    from tests.persistence.conftest import AUDIT_INSERT, audit_row_values

    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    with owner_engine.begin() as connection:
        connection.execute(AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=1))

    with pytest.raises(IntegrityError) as caught, owner_engine.begin() as connection:
        connection.execute(AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=1))

    assert "uq_audit_record_case_id_seq" in str(caught.value)


def test_audit_seq_allocation_is_gap_free_under_serial_writers(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Allocation from the case row yields 1..n with no gaps.

    Allocated by ``UPDATE ... RETURNING`` on the case row, in the same transaction as
    the insert. That is what a Postgres sequence cannot give — its allocations survive
    a rollback and leave a hole that is indistinguishable from a deleted record.
    """
    from tests.persistence.conftest import AUDIT_INSERT, audit_row_values

    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    for _ in range(5):
        with owner_engine.begin() as connection:
            allocated = connection.execute(
                text(
                    "UPDATE recovery_case SET audit_seq = audit_seq + 1 "
                    "WHERE merchant_id = :merchant_id AND id = :case_id RETURNING audit_seq"
                ),
                {"merchant_id": str(merchant_id), "case_id": str(case_id)},
            ).scalar_one()
            connection.execute(
                AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=allocated)
            )

    with owner_engine.connect() as connection:
        sequence = [
            row[0]
            for row in connection.execute(
                text("SELECT seq FROM audit_record WHERE case_id = :case_id ORDER BY seq"),
                {"case_id": str(case_id)},
            )
        ]

    assert sequence == [1, 2, 3, 4, 5]


def test_rolled_back_allocation_leaves_no_gap(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A rolled-back transaction does not consume a sequence number.

    The property a sequence cannot provide, stated as a test: the failed attempt is
    invisible afterwards, and the next successful write takes the number the failed one
    was going to use.
    """
    from tests.persistence.conftest import AUDIT_INSERT, audit_row_values

    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    allocate = text(
        "UPDATE recovery_case SET audit_seq = audit_seq + 1 "
        "WHERE merchant_id = :merchant_id AND id = :case_id RETURNING audit_seq"
    )
    params = {"merchant_id": str(merchant_id), "case_id": str(case_id)}

    connection = owner_engine.connect()
    transaction = connection.begin()
    assert connection.execute(allocate, params).scalar_one() == 1
    transaction.rollback()
    connection.close()

    with owner_engine.begin() as connection:
        second = connection.execute(allocate, params).scalar_one()
        connection.execute(AUDIT_INSERT, audit_row_values(merchant_id, case_id, seq=second))

    assert second == 1


def test_counters_within_bounds_rejects_more_messages_than_actions(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A customer-visible message is always an executed action, so it cannot outrun it.

    This is the clause of ``counters_within_bounds`` that protects a person rather than
    a number: whatever goes wrong in the policy layer, the database will not store a
    case that sent more messages than it took actions.
    """
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    with pytest.raises(IntegrityError) as caught, owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET executed_action_count = 1, "
                "customer_message_count = 2 WHERE id = :id"
            ),
            {"id": str(case_id)},
        )

    assert "counters_within_bounds" in str(caught.value)


def test_counters_within_bounds_rejects_exceeding_the_ceiling(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The ceiling is a backstop well above any configured bound, and it still holds."""
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    with pytest.raises(IntegrityError), owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE recovery_case SET executed_action_count = 11 WHERE id = :id"),
            {"id": str(case_id)},
        )


def test_one_active_diagnosis_per_decision_cycle(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Two active diagnoses for one cycle would leave the recommendation unexplainable."""
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")

    def add(cause: str, is_active: bool) -> None:
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO diagnosis (
                        merchant_id, case_id, cause, confidence, method, decision_cycle,
                        is_active, created_at
                    ) VALUES (
                        :merchant_id, :case_id, :cause, 0.900, 'DETERMINISTIC', 1,
                        :is_active, now()
                    )
                    """
                ),
                {
                    "merchant_id": str(merchant_id),
                    "case_id": str(case_id),
                    "cause": cause,
                    "is_active": is_active,
                },
            )

    add("INSUFFICIENT_FUNDS", True)
    # A superseded diagnosis for the same cycle is fine — that is history.
    add("EXPIRED_PAYMENT_METHOD", False)

    with pytest.raises(IntegrityError) as caught:
        add("ABANDONMENT", True)

    assert "one_active_diagnosis_per_cycle" in str(caught.value)


def test_recovery_outcome_is_unique_per_case(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """Recovery is counted once per case, which is half of "the total is not inflated"."""
    case_id = insert_case(owner_engine, merchant_id, provider_payment_id=f"pay_{uuid.uuid4()}")
    read_id = _insert_payment_state_read(owner_engine, merchant_id, case_id)

    def add_outcome() -> None:
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO recovery_outcome (
                        merchant_id, case_id, classification, recovered_amount,
                        recovery_timestamp, verified_by_read_id, created_at
                    ) VALUES (
                        :merchant_id, :case_id, 'OBSERVED', 250000, now(), :read_id, now()
                    )
                    """
                ),
                {
                    "merchant_id": str(merchant_id),
                    "case_id": str(case_id),
                    "read_id": str(read_id),
                },
            )

    add_outcome()
    with pytest.raises(IntegrityError) as caught:
        add_outcome()

    assert "uq_recovery_outcome_case_id" in str(caught.value)


def test_one_pending_job_per_dedupe_key_then_reusable_once_claimed(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """A sweep cannot double-enqueue, but it can enqueue again on the next tick.

    Both halves matter. Without the constraint, two schedulers produce two sweeps.
    Without the partial predicate, the sweep runs once and then never again, which
    fails silently and looks like recovery having stopped working.
    """
    dedupe_key = f"sweep-{uuid.uuid4()}"

    def enqueue() -> uuid.UUID:
        with owner_engine.begin() as connection:
            return connection.execute(
                text(
                    """
                    INSERT INTO job (merchant_id, kind, payload, state, run_after,
                                     dedupe_key, created_at)
                    VALUES (:merchant_id, 'LIFECYCLE_SWEEP', '{}'::jsonb, 'PENDING',
                            now(), :dedupe_key, now())
                    RETURNING id
                    """
                ),
                {"merchant_id": str(merchant_id), "dedupe_key": dedupe_key},
            ).scalar_one()

    first = enqueue()

    with pytest.raises(IntegrityError) as caught:
        enqueue()
    assert "one_pending_job_per_dedupe_key" in str(caught.value)

    with owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET state = 'DONE' WHERE id = :id"), {"id": str(first)}
        )

    assert enqueue() != first


def _insert_intent(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_id: uuid.UUID,
    idempotency_key: str,
    *,
    ordinal: int,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, created_at
                ) VALUES (
                    :merchant_id, :case_id, :decision_id, :key, 'PAYMENT_LINK',
                    :ordinal, 'ATTEMPTED', :started_at, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "decision_id": str(decision_id),
                "key": idempotency_key,
                "ordinal": ordinal,
                "started_at": now() - timedelta(seconds=1),
            },
        )


def _insert_payment_state_read(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> uuid.UUID:
    with engine.begin() as connection:
        return connection.execute(
            text(
                """
                INSERT INTO payment_state_read (
                    merchant_id, case_id, provider_payment_id, status, amount,
                    amount_refunded, captured, read_at, attempt_no, created_at
                ) VALUES (
                    :merchant_id, :case_id, 'pay_verified', 'captured', 250000, 0, true,
                    now(), 1, now()
                ) RETURNING id
                """
            ),
            {"merchant_id": str(merchant_id), "case_id": str(case_id)},
        ).scalar_one()

"""The diagnosis guarantees that are database facts, against real Postgres.

Four things cannot be established without a real database, and each of them is a
guarantee somebody would otherwise have to take on trust:

* **Exactly one active diagnosis per ``(case_id, decision_cycle)``.** The guarantee is
  the ``one_active_diagnosis_per_cycle`` partial unique index, so a fake repository
  would only prove the fake agrees with itself.
* **Idempotency under job retry.** Running the service twice for one cycle must leave
  one diagnosis, and must report the *existing* row's values rather than the ones the
  second run computed.
* **The coverage metric.** It is JSONB aggregation over ``diagnosis.evidence``, which is
  Postgres arithmetic, not Python.
* **A superseded cycle does not conflict.** The index is partial and scoped to a cycle,
  and a re-diagnosis after a decision cycle advances has to be insertable.

Also asserted here, because it is the queryable half of the zero-AI claim: every row
this path writes has ``ai_invocation_id IS NULL`` and no ``ai_invocation`` row exists.
The structural half — that the package cannot import the LLM adapter — is in
``tests/test_diagnosis.py``.

The ``pg`` mark comes from this directory's conftest, which also supplies the migrated
database and the per-test merchant.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.audit.events import (
    DIAGNOSIS_ALREADY_RECORDED,
    DIAGNOSIS_RECORDED,
    DIAGNOSIS_UNMAPPED_REASON,
    MERCHANT_INTEGRATION_FAULT,
)
from revora.diagnosis.service import run_diagnosis
from revora.domain.enums import CaseState, DiagnosisMethod, RiskCause
from revora.domain.failure_taxonomy import (
    EVIDENCE_MATCH_KEY,
    EVIDENCE_MATCHED,
    EVIDENCE_OUTCOME,
    EVIDENCE_RULE_ID,
    MatchKey,
    MatchOutcome,
)
from revora.domain.payment_event import CanonicalPaymentEvent
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.platform.clock import now
from revora.platform.config import default_configuration

pytestmark = pytest.mark.pg


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """Sessions on the migrated database, as the owner.

    The owner rather than ``revora_app`` because row-level security is not what these
    tests are about — ``tests/persistence/test_row_level_security.py`` owns that — and
    an RLS failure here would present as a confusing empty read rather than as the
    isolation problem it is.
    """
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


def _seed_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    error_reason: str | None = None,
    error_source: str | None = None,
    error_step: str | None = None,
    error_code: str | None = None,
    method: str | None = "card",
    decision_cycle: int = 0,
) -> uuid.UUID:
    """A webhook event carrying the given error fields, and a case opened from it.

    Written with raw SQL rather than through ingestion so the error fields are exactly
    what the test says they are. Going through ingestion would make every diagnosis
    assertion depend on canonicalization as well, and canonicalization has its own
    tests.
    """
    event_id = uuid.uuid4()
    case_id = uuid.uuid4()
    moment = now()
    canonical = CanonicalPaymentEvent(
        event_name="payment.failed",
        provider_payment_id=f"pay_{case_id.hex[:16]}",
        amount=250000,
        currency="INR",
        status="failed",
        method=method,
        error_code=error_code,
        error_reason=error_reason,
        error_source=error_source,
        error_step=error_step,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, canonical, received_at, correlation_id,
                    signature_verified, created_at
                ) VALUES (
                    :id, :merchant_id, :provider_event_id, 'payment.failed',
                    :ciphertext, CAST(:canonical AS jsonb), :received_at,
                    :correlation_id, true, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "provider_event_id": f"evt_{event_id.hex[:16]}",
                "ciphertext": b"not-a-real-ciphertext",
                "canonical": _json(canonical),
                "received_at": moment,
                "correlation_id": str(uuid.uuid4()),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, source_event_id, detected_at,
                    window_end_at, decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :provider_payment_id, 250000,
                    'INR', :customer_key, :source_event_id, :detected_at,
                    :window_end_at, :decision_cycle, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": CaseState.DETECTED.value,
                "provider_payment_id": canonical.provider_payment_id,
                "customer_key": f"ck-{case_id}",
                "source_event_id": str(event_id),
                "detected_at": moment,
                "window_end_at": moment + timedelta(hours=168),
                "decision_cycle": decision_cycle,
            },
        )
    return case_id


def _json(canonical: CanonicalPaymentEvent) -> str:
    import json

    return json.dumps(canonical.to_dict())


def _advance_cycle(engine: Engine, case_id: uuid.UUID) -> None:
    """Move the case's decision cycle on, as the transition into DECISION_PENDING does."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET decision_cycle_count = decision_cycle_count + 1 "
                "WHERE id = :id"
            ),
            {"id": str(case_id)},
        )


def _diagnosis_rows(engine: Engine, case_id: uuid.UUID) -> list[dict[str, object]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT cause, confidence, method, decision_cycle, is_active, "
                "substituted_to_unknown, ai_invocation_id, evidence "
                "FROM diagnosis WHERE case_id = :c ORDER BY decision_cycle, created_at"
            ),
            {"c": str(case_id)},
        ).mappings()
        return [dict(row) for row in rows]


def _audit_event_types(engine: Engine, case_id: uuid.UUID) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT event_type FROM audit_record WHERE case_id = :c ORDER BY seq"),
            {"c": str(case_id)},
        ).all()
    return [str(row[0]) for row in rows]


def _run(factory: sessionmaker[Session], merchant_id: uuid.UUID, case_id: uuid.UUID):
    """One diagnosis run in its own transaction, as the job handler would."""
    config = default_configuration()
    with factory() as session, session.begin():
        return run_diagnosis(session, merchant_id, case_id, config)


# ---------------------------------------------------------------------------
# The deterministic path, recorded
# ---------------------------------------------------------------------------


def test_a_mapped_reason_records_one_deterministic_diagnosis(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A table hit writes one active row at confidence 1.0, with no AI invocation.

    ``ai_invocation_id IS NULL`` and an empty ``ai_invocation`` table are the queryable
    form of R3.C2's "issues zero LLM invocations": a claim about a run, checkable after
    the fact, rather than a claim about the code.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="insufficient_funds")
    outcome = _run(factory, merchant_id, case_id)

    assert outcome.cause is RiskCause.INSUFFICIENT_FUNDS
    assert outcome.method is DiagnosisMethod.DETERMINISTIC
    assert outcome.confidence == Decimal("1.000")
    assert outcome.deterministic_hit is True
    assert outcome.reasoning_layer_invoked is False

    rows = _diagnosis_rows(owner_engine, case_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["cause"] == RiskCause.INSUFFICIENT_FUNDS.value
    assert Decimal(str(row["confidence"])) == Decimal("1.000")
    assert row["method"] == DiagnosisMethod.DETERMINISTIC.value
    assert row["is_active"] is True
    assert row["substituted_to_unknown"] is False
    assert row["ai_invocation_id"] is None

    evidence = row["evidence"]
    assert isinstance(evidence, dict)
    assert evidence[EVIDENCE_RULE_ID] == "error_reason:insufficient_funds"
    assert evidence[EVIDENCE_MATCH_KEY] == MatchKey.ERROR_REASON.value
    assert evidence[EVIDENCE_MATCHED] is True

    with owner_engine.connect() as connection:
        invocations = connection.execute(
            text("SELECT count(*) FROM ai_invocation WHERE merchant_id = :m"),
            {"m": str(merchant_id)},
        ).scalar_one()
    assert invocations == 0

    assert _audit_event_types(owner_engine, case_id) == [DIAGNOSIS_RECORDED]


def test_evidence_carries_no_contact_or_amount(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The persisted evidence document is PII-free.

    Asserted on the stored row rather than on the function's return value, because the
    thing that matters is what reached durable storage — masking is a write-time
    property, not a display convention.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="incorrect_cvv")
    _run(factory, merchant_id, case_id)
    evidence = _diagnosis_rows(owner_engine, case_id)[0]["evidence"]
    assert isinstance(evidence, dict)
    forbidden = {"contact", "email", "customer_key", "amount", "error_description"}
    assert forbidden.isdisjoint(evidence.keys())


def test_an_unmapped_reason_records_fallback_unknown_and_an_audit_record(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A coverage gap is recorded twice: as a diagnosis, and as a named gap.

    The second record is what lets the table be extended from the audit trail alone,
    and it is why the gap is visible on the case rather than only in an aggregate
    somebody has to think to run.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="reason_from_the_future")
    outcome = _run(factory, merchant_id, case_id)

    assert outcome.cause is RiskCause.UNKNOWN
    assert outcome.method is DiagnosisMethod.FALLBACK_UNKNOWN
    assert outcome.confidence == Decimal("0.000")
    assert outcome.coverage_gap is True
    assert outcome.substituted_to_unknown is True

    assert _audit_event_types(owner_engine, case_id) == [
        DIAGNOSIS_RECORDED,
        DIAGNOSIS_UNMAPPED_REASON,
        "DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN",
    ]


def test_a_merchant_integration_fault_raises_the_operational_alert(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """Our bug gets its own audit record, not a line in someone's log.

    ``MERCHANT_INTEGRATION_FAULT`` is the on-call item. The diagnosis is still
    ``TECHNICAL_ISSUE``, so the lifecycle continues normally; what changes is that
    somebody finds out the integration is sending bad order ids.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="order_amount_mismatch")
    outcome = _run(factory, merchant_id, case_id)

    assert outcome.cause is RiskCause.TECHNICAL_ISSUE
    assert outcome.needs_operational_alert is True
    assert MERCHANT_INTEGRATION_FAULT in _audit_event_types(owner_engine, case_id)


def test_a_configured_risk_reason_requires_policy_evaluation(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """R3.C6: a fraud signal flags that policy must decide before anything is scheduled.

    The service reports it rather than enforcing it, because the schedule gate belongs
    to the case manager and the policy engine. What is asserted here is that the signal
    reaches the caller at all — a fraud decline the handler never learns about is the
    failure this flag exists to prevent.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="payment_risk_check_failed")
    outcome = _run(factory, merchant_id, case_id)

    assert outcome.cause is RiskCause.FRAUD_OR_RISK_SIGNAL
    assert outcome.requires_policy_evaluation is True
    assert outcome.method is DiagnosisMethod.DETERMINISTIC


def test_source_and_step_refinement_is_used_when_the_reason_is_absent(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A failure with no reason still resolves from where it happened."""
    case_id = _seed_case(
        owner_engine, merchant_id, error_source="gateway", error_step="payment_authorization"
    )
    outcome = _run(factory, merchant_id, case_id)
    assert outcome.cause is RiskCause.TECHNICAL_ISSUE
    assert outcome.match_key is MatchKey.SOURCE_STEP


# ---------------------------------------------------------------------------
# The active-diagnosis invariant
# ---------------------------------------------------------------------------


def test_a_second_run_for_the_same_cycle_is_idempotent(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A retried job leaves one diagnosis and reports the existing one.

    Reports the *existing* row rather than what the second run computed, because the
    lifecycle continues on the recorded diagnosis: a caller told otherwise would
    transition a case on a cause that was never persisted.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="card_expired")
    first = _run(factory, merchant_id, case_id)
    second = _run(factory, merchant_id, case_id)

    assert first.already_recorded is False
    assert second.already_recorded is True
    assert second.diagnosis_id == first.diagnosis_id
    assert second.cause is first.cause
    assert len(_diagnosis_rows(owner_engine, case_id)) == 1
    assert DIAGNOSIS_ALREADY_RECORDED in _audit_event_types(owner_engine, case_id)


def test_the_index_refuses_a_second_active_diagnosis_for_one_cycle(
    owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """``one_active_diagnosis_per_cycle`` is enforced by the database.

    Inserted directly, bypassing the service's existence check, because the point is
    that the guarantee does not depend on that check. Two active causes for one cycle
    would mean two different actions could each claim to be the justified one.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="card_expired")
    insert = text(
        """
        INSERT INTO diagnosis (
            id, merchant_id, case_id, cause, confidence, method, decision_cycle,
            is_active, created_at
        ) VALUES (
            gen_random_uuid(), :m, :c, :cause, 1.0, 'DETERMINISTIC', 0, true, now()
        )
        """
    )
    with owner_engine.begin() as connection:
        connection.execute(
            insert,
            {"m": str(merchant_id), "c": str(case_id), "cause": RiskCause.ABANDONMENT.value},
        )
    with pytest.raises(Exception, match="one_active_diagnosis_per_cycle"), owner_engine.begin() as connection:  # noqa: E501
        connection.execute(
            insert,
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "cause": RiskCause.TECHNICAL_ISSUE.value,
            },
        )


def test_a_new_decision_cycle_gets_its_own_diagnosis(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The index is scoped to a cycle, so a re-diagnosis after re-entry is insertable.

    Both rows stay active. That is deliberate: the earlier cycle's cause is what
    explains the decision that was made in that cycle, and superseding it by
    deactivation would erase the explanation a merchant is owed.
    """
    case_id = _seed_case(owner_engine, merchant_id, error_reason="insufficient_funds")
    first = _run(factory, merchant_id, case_id)
    _advance_cycle(owner_engine, case_id)
    second = _run(factory, merchant_id, case_id)

    assert second.already_recorded is False
    assert second.diagnosis_id != first.diagnosis_id
    rows = _diagnosis_rows(owner_engine, case_id)
    assert [row["decision_cycle"] for row in rows] == [0, 1]

    with_repo = DiagnosisRepository
    with factory() as session, session.begin():
        latest = with_repo(session).active_for_case(merchant_id, case_id)
        assert latest is not None
        assert latest.decision_cycle == 1


# ---------------------------------------------------------------------------
# The measured facts
# ---------------------------------------------------------------------------


def test_coverage_and_unmapped_reasons_are_queryable(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The deterministic hit rate and the unmapped-reason backlog come out of SQL.

    This is the design's ``[INFERENCE]`` about table coverage turned into a
    measurement. Three hits, two gaps of the same unrecognized reason, and one
    ``order_already_paid`` that must not distort either number: the rate is 3/5, and
    the not-at-risk answer is excluded from the denominator rather than counted as a
    miss.
    """
    for reason in ("insufficient_funds", "card_expired", "bank_technical_error"):
        _run(factory, merchant_id, _seed_case(owner_engine, merchant_id, error_reason=reason))
    for _ in range(2):
        _run(
            factory,
            merchant_id,
            _seed_case(owner_engine, merchant_id, error_reason="brand_new_reason"),
        )
    paid_case = _seed_case(owner_engine, merchant_id, error_reason="order_already_paid")
    _run(factory, merchant_id, paid_case)

    with factory() as session, session.begin():
        repo = DiagnosisRepository(session)
        coverage = repo.coverage(merchant_id)
        assert coverage.total == 6
        assert coverage.deterministic_hits == 3
        assert coverage.unmapped == 2
        assert coverage.not_at_risk == 1
        assert coverage.hit_rate == Decimal("0.6000")

        unmapped = repo.unmapped_reasons(merchant_id, limit=10)
        assert [(item.reason, item.occurrences) for item in unmapped] == [
            ("brand_new_reason", 2)
        ]

        keys = repo.match_key_counts(merchant_id)
        assert keys[MatchKey.ERROR_REASON.value] == 4  # 3 hits + order_already_paid

        methods = repo.method_counts(merchant_id)
        assert methods[DiagnosisMethod.DETERMINISTIC] == 3
        assert methods[DiagnosisMethod.FALLBACK_UNKNOWN] == 3
        assert DiagnosisMethod.AI_ASSISTED not in methods

        assert repo.substituted_count(merchant_id) == 3
        causes = repo.cause_counts(merchant_id)
        assert causes[RiskCause.UNKNOWN] == 3


def test_coverage_of_an_empty_merchant_is_zero_rather_than_an_error(
    factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A fresh deployment reads as zeros, not as an exception in a dashboard endpoint."""
    with factory() as session, session.begin():
        coverage = DiagnosisRepository(session).coverage(merchant_id)
    assert coverage.total == 0
    assert coverage.hit_rate == Decimal("0")


def test_the_taxonomy_outcome_is_persisted_for_every_diagnosis(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """Every row carries the outcome the aggregate groups by.

    A row whose evidence lacks ``taxonomy_outcome`` is invisible to the coverage
    counts, so it would silently lower the total and misreport the rate.
    """
    expected = {
        "insufficient_funds": MatchOutcome.MAPPED,
        "order_already_paid": MatchOutcome.NOT_AT_RISK,
        "never_seen_before": MatchOutcome.UNMAPPED,
    }
    for reason, outcome in expected.items():
        case_id = _seed_case(owner_engine, merchant_id, error_reason=reason)
        _run(factory, merchant_id, case_id)
        evidence = _diagnosis_rows(owner_engine, case_id)[0]["evidence"]
        assert isinstance(evidence, dict)
        assert evidence[EVIDENCE_OUTCOME] == outcome.value

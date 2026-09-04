"""Task 21 and 22: recovery is declared only from an authoritative read, and only once.

Every test here is an attempt to make Revora report money that did not arrive. The recovery
figure is the number a merchant would take to their finance team, so the tests are written
from the position of someone trying to inflate it:

* declare recovery from a webhook alone;
* declare it from an ``authorized`` payment, where the money has not moved;
* declare it from a partial payment;
* count the same recovery twice by replaying the success signal;
* count it while an execution intent is still ``UNCERTAIN``, before it is knowable whether
  an action reached the customer;
* declare it when the provider read will not complete at all.

All six must fail. The two that must *succeed* are a genuine captured read, and a captured
read arriving after the case had already ended for another reason — counted exactly once.

Against real Postgres throughout, because both halves of Property 20 are schema constraints:
``verified_by_read_id NOT NULL`` and ``UNIQUE (case_id)`` on ``recovery_outcome``. Neither is
observable against a fake session.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, DiagnosisMethod, IntentState, OutcomeClass, RiskCause
from revora.domain.keys import execution_key
from revora.domain.payment_event import PaymentStatus
from revora.ingestion.backfill import backfill_detection_gap, backfill_event_id
from revora.outcome.monitor import OutcomeVerdict, observe_payment_outcome
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour

pytestmark = pytest.mark.pg


class _Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    """Crypto keys, so a case can carry an encrypted source event."""
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"O" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"M" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"M" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_case(
    engine: Engine,
    *,
    state: CaseState = CaseState.WAITING_FOR_OUTCOME,
    amount: int = 250_000,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A merchant and a case awaiting its outcome. Returns (merchant, case, payment id)."""
    merchant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    event_id = uuid.uuid4()
    payment_id = f"pay_{case_id.hex[:14]}"
    moment = now()

    encrypted = payload_cipher().encrypt(
        json.dumps(
            {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": payment_id,
                            "entity": "payment",
                            "status": "failed",
                            "contact": "+919000090000",
                            "email": "outcome-test@example.com",
                        }
                    }
                },
            }
        ).encode()
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Outcome merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"outcome-{merchant_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, raw_payload_nonce, key_version,
                    canonical, correlation_id, signature_verified, received_at, created_at
                ) VALUES (
                    :id, :merchant_id, :provider_event_id, 'payment.failed',
                    :ciphertext, :nonce, :key_version,
                    :canonical, :correlation_id, true, :received_at, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "provider_event_id": f"evt_{case_id.hex[:16]}",
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "canonical": json.dumps({"provider_payment_id": payment_id}),
                "correlation_id": str(uuid.uuid4()),
                "received_at": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, source_event_id, detected_at, window_end_at,
                    created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, :amount,
                    'INR', :customer_key, :source_event_id, :detected_at, :window_end_at,
                    now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "payment_id": payment_id,
                "amount": amount,
                "customer_key": f"ck-{case_id}",
                "source_event_id": str(event_id),
                "detected_at": moment - timedelta(hours=2),
                "window_end_at": moment + timedelta(hours=168),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :cause, 0.90, :method, 1,
                    true, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "cause": RiskCause.INSUFFICIENT_FUNDS.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
    return merchant_id, case_id, payment_id


def _seed_intent(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    state: IntentState,
) -> str:
    """An execution intent for the case, so the action race can be exercised."""
    key = execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1)
    decision_id = uuid.uuid4()
    moment = now()
    resolved = state in (IntentState.CONFIRMED, IntentState.FAILED)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, idempotency_key, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, :action, 'ACTION_SCHEDULED', 1, :key, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
                "action": CandidateAction.PAYMENT_LINK.value,
                "key": key,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, resolved_at,
                    provider_response_id, is_post_payment, reconciliation_attempts,
                    counter_applied, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :decision_id, :key, :action,
                    1, :state, :started_at, :resolved_at, :response_id, false, 0, true, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "decision_id": str(decision_id),
                "key": key,
                "action": CandidateAction.PAYMENT_LINK.value,
                "state": state.value,
                "started_at": moment,
                "resolved_at": moment if resolved else None,
                "response_id": "plink_test123456" if state is IntentState.CONFIRMED else None,
            },
        )
    return key


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def _outcome_row(engine: Engine, case_id: uuid.UUID) -> dict[str, object] | None:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT classification, recovered_amount, verified_by_read_id,
                       seconds_to_recovery, reconciled_from_terminal_state
                FROM recovery_outcome WHERE case_id = :case_id
                """
            ),
            {"case_id": str(case_id)},
        ).one_or_none()
        return None if row is None else dict(row._mapping)


def _read_rows(engine: Engine, case_id: uuid.UUID) -> list[dict[str, object]]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT status, captured, amount, amount_refunded, attempt_no, raw
                FROM payment_state_read WHERE case_id = :case_id ORDER BY attempt_no
                """
            ),
            {"case_id": str(case_id)},
        )
        return [dict(row._mapping) for row in rows]


def _case_state(engine: Engine, case_id: uuid.UUID) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT state FROM recovery_case WHERE id = :id"),
                {"id": str(case_id)},
            ).scalar_one()
        )


def _event_types(engine: Engine, case_id: uuid.UUID) -> list[str]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT event_type FROM audit_record WHERE case_id = :case_id ORDER BY seq"
            ),
            {"case_id": str(case_id)},
        )
        return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# The six ways it must refuse
# ---------------------------------------------------------------------------


def test_an_authorized_payment_is_not_recovery(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C2. An authorization is a hold, not money in the account.

    The most tempting false positive in the system: ``authorized`` looks like success, arrives
    on the happy path, and would inflate every figure. It is not recovery because the money has
    not moved and the hold can still fail to settle.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.AUTHORIZED))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.NOT_RECOVERED, assessment.verdict.value
    assert not assessment.declared_recovery
    assert _outcome_row(owner_engine, case_id) is None, (
        "an authorized payment produced a recovery row"
    )
    assert _case_state(owner_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value
    # The read is still persisted, because a read that happened must be enumerable.
    assert len(_read_rows(owner_engine, case_id)) == 1


def test_a_read_that_will_not_complete_declares_nothing(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """A 5xx is not evidence. Hold, and never declare on an uncorroborated webhook."""
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.unavailable_payment_reads(1))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory, signal_status="captured"
    )

    assert assessment.verdict is OutcomeVerdict.READ_UNAVAILABLE
    assert not assessment.declared_recovery
    assert _outcome_row(owner_engine, case_id) is None
    assert _read_rows(owner_engine, case_id) == [], (
        "a failed read must persist no payment_state_read row — the column holds the "
        "provider's own status vocabulary and has none to record"
    )
    assert "PAYMENT_STATE_READ_UNAVAILABLE" in _event_types(owner_engine, case_id)


def test_consecutive_unreadable_reads_escalate_without_declaring_recovery(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C7. At the attempt bound the case goes to a human, and no recovery is declared.

    The unsatisfying ending, asserted because it is the correct one. The amount is preserved as
    unresolved rather than counted, because a webhook nobody could corroborate is not evidence
    that money arrived.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.unavailable_payment_reads(20))

    verdicts = [
        observe_payment_outcome(
            merchant_id, case_id, provider=fake, factory=factory, signal_status="captured"
        ).verdict
        for _ in range(6)
    ]

    assert OutcomeVerdict.UNVERIFIABLE in verdicts, verdicts
    assert _outcome_row(owner_engine, case_id) is None, (
        "escalation must not be accompanied by a recovery row"
    )
    assert _case_state(owner_engine, case_id) == CaseState.ESCALATED.value
    types = _event_types(owner_engine, case_id)
    assert "PAYMENT_STATE_UNVERIFIABLE" in types
    assert "RECOVERY_RECORDED" not in types


def test_a_partial_payment_is_not_recovery(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C11. Some money is not all the money, and counting it inflates every figure.

    A partly-recovered payment presents as a capture whose refunded amount sits strictly
    between zero and the full amount — ``partially_paid`` is a payment-*link* status, not a
    payment status, so this is the shape a partial actually takes on the read.

    The full-capture control is asserted in the companion test below, so this one is about the
    partial and nothing else.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(
        ProviderBehaviour(
            payment_statuses=(PaymentStatus.CAPTURED,),
            payment_amount=250_000,
            payment_amount_refunded=100_000,
        )
    )

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.PARTIAL_HELD, assessment.verdict.value
    assert not assessment.declared_recovery
    assert _outcome_row(owner_engine, case_id) is None, (
        "a partial payment produced a recovery row"
    )
    assert _case_state(owner_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value
    assert "PARTIAL_PAYMENT_OBSERVED" in _event_types(owner_engine, case_id)
    # The refunded amount is captured on the read, so a later restatement is arithmetic.
    rows = _read_rows(owner_engine, case_id)
    assert int(rows[0]["amount_refunded"]) == 100_000


def test_a_conflicting_signal_holds_the_case(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C6. A success webhook contradicted by the read means we do not know yet."""
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.read_disagreeing_with_success_webhook())

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory, signal_status="captured"
    )

    assert assessment.verdict is OutcomeVerdict.CONFLICT_HELD, assessment.verdict.value
    assert not assessment.declared_recovery
    assert _outcome_row(owner_engine, case_id) is None
    assert _case_state(owner_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value
    assert "PAYMENT_STATE_CONFLICT" in _event_types(owner_engine, case_id)


def test_classification_is_withheld_while_an_intent_is_uncertain(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C12. Whether an action reached the customer decides NATURAL versus OBSERVED.

    While an intent is ``UNCERTAIN`` that is unknown, so the classification is unknowable too.
    Recording a guess would put a wrong value in the one table the headline figure is summed
    from — and the amount is not lost, because the unique constraint means a later pass can
    still count it exactly once.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    _seed_intent(owner_engine, merchant_id, case_id, state=IntentState.UNCERTAIN)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.WITHHELD_UNCERTAIN_INTENT
    assert not assessment.declared_recovery
    assert _outcome_row(owner_engine, case_id) is None
    assert _case_state(owner_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value


def test_a_duplicate_success_signal_counts_nothing_twice(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C13. At-least-once delivery makes duplicate success signals ordinary.

    The second signal must issue no read at all — not merely fail to insert a second row. An
    extra provider call per duplicate would be a self-inflicted rate-limit problem during
    exactly the redelivery storm that produces duplicates.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    first = observe_payment_outcome(merchant_id, case_id, provider=fake, factory=factory)
    assert first.verdict is OutcomeVerdict.RECOVERED
    reads_after_first = fake.call_count

    second = observe_payment_outcome(merchant_id, case_id, provider=fake, factory=factory)

    assert second.verdict is OutcomeVerdict.DUPLICATE_DISCARDED
    assert fake.call_count == reads_after_first, (
        "the duplicate issued another provider read"
    )
    assert len(_read_rows(owner_engine, case_id)) == 1
    assert "DUPLICATE_RECOVERY_EVENT_DISCARDED" in _event_types(owner_engine, case_id)


# ---------------------------------------------------------------------------
# The two that must succeed
# ---------------------------------------------------------------------------


def test_a_captured_read_declares_recovery_once_verified_by_that_read(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C1/C3 and both halves of Property 20.

    The amount comes from the read, never the webhook; the recovery row names the read that
    verified it; and the classification is ``NATURAL`` because no Revora action was confirmed.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine, amount=250_000)
    fake = FakeRazorpay(
        ProviderBehaviour(payment_statuses=(PaymentStatus.CAPTURED,), payment_amount=180_000)
    )

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory, signal_status="captured"
    )

    assert assessment.verdict is OutcomeVerdict.RECOVERED
    assert assessment.declared_recovery
    row = _outcome_row(owner_engine, case_id)
    assert row is not None
    # From the read (180000), not from the case's amount at risk (250000).
    assert int(row["recovered_amount"]) == 180_000
    assert row["verified_by_read_id"] is not None
    assert row["classification"] == OutcomeClass.NATURAL.value
    assert int(row["seconds_to_recovery"]) >= 0
    assert _case_state(owner_engine, case_id) == CaseState.RECOVERED.value
    assert "RECOVERY_RECORDED" in _event_types(owner_engine, case_id)


def test_a_confirmed_action_makes_the_recovery_observed_not_attributed(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C8. One or more confirmed actions makes it ``OBSERVED``.

    Never ``ATTRIBUTED``. That is a causal claim and needs a controlled comparison; defaulting
    to it here would quietly convert correlation into a claim of incremental revenue, which is
    the specific dishonesty this project exists to avoid.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    _seed_intent(owner_engine, merchant_id, case_id, state=IntentState.CONFIRMED)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.RECOVERED
    row = _outcome_row(owner_engine, case_id)
    assert row is not None
    assert row["classification"] == OutcomeClass.OBSERVED.value
    assert row["classification"] != OutcomeClass.ATTRIBUTED.value
    # The action went out and the customer had already paid, so it is flagged post-payment.
    assert assessment.post_payment_intents == 1
    assert "POST_PAYMENT_ACTION" in _event_types(owner_engine, case_id)


def test_a_delayed_capture_reconciles_a_terminal_case_exactly_once(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R10.C14. A case that ended as EXPIRED can still be found to have been paid.

    One reconciliation transition, the superseded state recorded, and the amount counted once
    however many times the signal is replayed.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine, state=CaseState.EXPIRED)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.DELAYED_RECOVERY, assessment.verdict.value
    assert assessment.superseded_state is CaseState.EXPIRED
    row = _outcome_row(owner_engine, case_id)
    assert row is not None
    assert row["reconciled_from_terminal_state"] == CaseState.EXPIRED.value
    assert _case_state(owner_engine, case_id) == CaseState.RECOVERED.value
    assert "DELAYED_RECOVERY_RECONCILED" in _event_types(owner_engine, case_id)

    # Replay: still exactly one outcome row, and no second read.
    calls = fake.call_count
    again = observe_payment_outcome(merchant_id, case_id, provider=fake, factory=factory)
    assert again.verdict is OutcomeVerdict.DUPLICATE_DISCARDED
    assert fake.call_count == calls


@pytest.mark.parametrize(
    "state", [CaseState.DECISION_PENDING, CaseState.EXECUTING], ids=lambda s: s.value
)
def test_a_capture_mid_pipeline_recovers_the_case_rather_than_only_the_money(
    owner_engine: Engine, factory: sessionmaker[Session], state: CaseState
) -> None:
    """A customer can pay while the case is still being worked, and the state must follow.

    The regression this pins. ``RECOVERED`` used to be reachable only from
    ``WAITING_FOR_OUTCOME`` and from the terminal states, so an authoritative captured read
    taken while the case sat mid-pipeline wrote ``RECOVERY_RECORDED``, counted the money, and
    then had its transition refused as illegal — leaving a case that expired hours later while
    its amount was already in the recovery figure. Measured at 23 cases in 150.

    Two states rather than one, and they fail for different reasons. ``DECISION_PENDING`` is
    before anything has been sent, so nothing about the case explains the capture — the
    customer simply paid. ``EXECUTING`` is with a provider request in flight, which is the race
    ``_settle_action_race`` exists for, and it is the state where the refusal was most likely to
    be mistaken for a normal outcome of that race.

    The absence of ``DELAYED_RECOVERY_RECONCILED`` is asserted, not incidental: this is not a
    delayed reconciliation of an ended case, and recording it as one would tell a merchant the
    case had finished and been corrected when it never finished at all.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine, state=state)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    assessment = observe_payment_outcome(
        merchant_id, case_id, provider=fake, factory=factory
    )

    assert assessment.verdict is OutcomeVerdict.RECOVERED, assessment.verdict.value
    assert assessment.superseded_state is None
    assert _case_state(owner_engine, case_id) == CaseState.RECOVERED.value, (
        f"a verified capture on a case at {state.value} recorded the money but left the case "
        "out of RECOVERED; the recovery figure and the case list now disagree"
    )
    events = _event_types(owner_engine, case_id)
    assert "RECOVERY_RECORDED" in events
    assert "DELAYED_RECOVERY_RECONCILED" not in events, (
        "a case that had not ended was reconciled as a delayed recovery"
    )
    row = _outcome_row(owner_engine, case_id)
    assert row is not None
    assert row["reconciled_from_terminal_state"] is None


def test_the_persisted_read_never_contains_contact_or_email(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R17.C6. ``payment_state_read.raw`` is unencrypted JSONB, so PII may not reach it.

    The payment entity can now carry ``contact`` and ``email``, because the detection-gap
    backfill needs them to derive a customer key. This pins the consequence: they are excluded
    from anything persisted in clear, and the exclusion is asserted rather than trusted.
    """
    merchant_id, case_id, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.payment_status(PaymentStatus.CAPTURED))

    observe_payment_outcome(merchant_id, case_id, provider=fake, factory=factory)

    rows = _read_rows(owner_engine, case_id)
    assert rows, "no read was persisted"
    for row in rows:
        raw = row["raw"]
        assert isinstance(raw, dict)
        assert "contact" not in raw, "a contact number reached an unencrypted column"
        assert "email" not in raw, "an email address reached an unencrypted column"
        serialized = json.dumps(raw)
        assert "@" not in serialized
        assert "+91" not in serialized


# ---------------------------------------------------------------------------
# Task 22 — the detection-gap backfill
# ---------------------------------------------------------------------------


def test_backfill_ingests_a_missed_failure_exactly_once(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Task 22. A failed payment no webhook ever delivered still becomes a case.

    This is the whole point of the job: sustained delivery failure disables the webhook, and
    without this Revora would report healthy numbers while detecting nothing at all.
    """
    merchant_id, _, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.missed_failures(3))

    report = backfill_detection_gap(merchant_id, provider=fake, factory=factory)

    assert report.failed_seen == 3
    assert report.ingested == 3, f"ingested {report.ingested}; {report}"
    assert report.gap_closed == 3
    assert not report.truncated

    # Re-running is a no-op: the pre-check by payment id recognises them.
    second = backfill_detection_gap(merchant_id, provider=fake, factory=factory)
    assert second.ingested == 0
    assert second.already_present == 3, f"{second}"

    with owner_engine.begin() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*) FROM webhook_event
                WHERE merchant_id = :merchant_id AND provider_event_id LIKE 'backfill:%'
                """
            ),
            {"merchant_id": str(merchant_id)},
        ).scalar_one()
    assert int(count) == 3, "backfill created a second event for a payment it already had"


def test_backfill_of_an_already_delivered_payment_creates_nothing(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Task 22.2. A payment that arrived by webhook is not ingested a second time.

    The dedup index alone cannot catch this: the webhook carried the provider's event id and a
    backfill would mint ``backfill:<id>:<status>``, so the index sees two different keys. The
    pre-check on the canonical payment id is what makes it a no-op.
    """
    merchant_id, case_id, payment_id = _seed_case(owner_engine)

    # The seeded case already has a webhook_event whose canonical names this payment.
    fake = FakeRazorpay(
        ProviderBehaviour(window_payments=((payment_id, PaymentStatus.FAILED),))
    )

    report = backfill_detection_gap(merchant_id, provider=fake, factory=factory)

    assert report.failed_seen == 1
    assert report.already_present == 1, f"{report}"
    assert report.ingested == 0, "a payment already delivered by webhook was ingested again"

    with owner_engine.begin() as connection:
        cases = connection.execute(
            text(
                "SELECT count(*) FROM recovery_case WHERE merchant_id = :m AND "
                "provider_payment_id = :p"
            ),
            {"m": str(merchant_id), "p": payment_id},
        ).scalar_one()
    assert int(cases) == 1, "the backfill created a second case for one payment"
    assert case_id is not None


def test_backfill_paginates_past_the_hundred_record_page_cap(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The page cap is 100, verified from the provider's documentation.

    A backfill that asked for the whole window in one call would silently ignore everything
    past the first page — the same detection gap it exists to close, reintroduced by an
    off-by-a-page.
    """
    merchant_id, _, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour.missed_failures(150))

    report = backfill_detection_gap(merchant_id, provider=fake, factory=factory)

    assert report.pages_read == 2, f"expected two pages for 150 records, got {report.pages_read}"
    assert report.failed_seen == 150
    assert report.ingested == 150


def test_backfill_does_not_treat_an_unreadable_window_as_an_empty_one(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """A 5xx must not be reported as "nothing to backfill".

    The most dangerous possible bug in this job: reporting success on a failed read leaves the
    detection gap open while telling an operator it is closed.
    """
    merchant_id, _, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(
        ProviderBehaviour(
            window_payments=(("pay_unreachable01", PaymentStatus.FAILED),),
            listing_window_unavailable_reads=1,
        )
    )

    report = backfill_detection_gap(merchant_id, provider=fake, factory=factory)

    assert report.read_failures == 1
    assert report.ingested == 0
    assert report.payments_seen == 0
    assert report.gap_closed == 0


def test_a_backfilled_case_carries_a_customer_key_when_the_read_had_a_contact(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Whether a backfilled case is actionable depends on the read carrying a contact.

    ``recovery_case.customer_key`` is ``NOT NULL`` and the canonicalizer derives it from the
    payload's ``contact``/``email``. So a read with contact produces a case that can be acted
    on; a read without one produces a case that can only be *seen*.

    Both are asserted, because the difference decides what the backfill is worth during an
    outage, and because a contact reaching the event store must not also reach the canonical
    row — the canonical column is unencrypted and holds a masked form only.
    """
    merchant_id, _, _ = _seed_case(owner_engine)
    fake = FakeRazorpay(
        ProviderBehaviour(
            window_payments=(("pay_withcontact01", PaymentStatus.FAILED),),
            window_contact="+919000090001",
            window_email="backfilled@example.com",
        )
    )

    report = backfill_detection_gap(merchant_id, provider=fake, factory=factory)
    assert report.ingested == 1, f"{report}"

    with owner_engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT canonical FROM webhook_event
                WHERE merchant_id = :m AND provider_event_id = :eid
                """
            ),
            {
                "m": str(merchant_id),
                "eid": backfill_event_id("pay_withcontact01", PaymentStatus.FAILED.value),
            },
        ).one()

    canonical = row[0]
    assert canonical["customer_key"], (
        "no customer key derived, so the case could not join to consent"
    )
    # The canonical row is unencrypted. The cleartext contact belongs only in the ciphertext.
    serialized = json.dumps(canonical)
    assert "+919000090001" not in serialized, (
        "a cleartext contact reached the unencrypted canonical column"
    )
    assert "backfilled@example.com" not in serialized


def test_the_synthetic_event_id_is_the_specified_shape() -> None:
    """``backfill:<payment_id>:<status>`` exactly, so the dedup index and audit queries match."""
    assert backfill_event_id("pay_ABC123", "failed") == "backfill:pay_ABC123:failed"
    # Status is part of the key: the same payment observed failed then captured is two facts.
    assert backfill_event_id("pay_ABC123", "captured") != backfill_event_id(
        "pay_ABC123", "failed"
    )

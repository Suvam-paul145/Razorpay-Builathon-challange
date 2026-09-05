"""R25.C5: the recommendation names the Customer_Signal that produced its candidate set.

Two claims, and the first is the one that needed no code.

**The candidate set is already built from the refined cause.** R20.C4 records its refinement as
the *active diagnosis for the next decision cycle*, and every stage downstream reads the cause
from exactly that row — so a stated reason reaches the next cycle's candidates through the
mechanism every other cause reaches them through, and a test that seeded a refined diagnosis and
then found a candidate set built from the provider's cause would be reporting a bug in the
diagnosis service rather than in the optimizer. What is asserted here is the consequence: the
recorded comparison names the cause it was built from, so a reader does not have to join back to
a diagnosis row to know which population was priced.

**The signal identifier is recorded.** That did need code. Before this, two decision cycles with
identical candidate sets — one priced against a cause the provider's error code gave and one
against a cause a customer typed — were indistinguishable in the record, and they license very
different amounts of confidence. The identifier lands in the ``RECOMMENDATION_RECORDED``
Audit_Record's ``decision`` document, at the top level, so the join is
``decision->>'cause_signal_id'``. The trade-off against a dedicated column is argued in
``revora.optimizer.service``'s module docstring; what this file checks is that the value is
present, correct, and queryable by that path.

The negative case is asserted too, and it is not a formality: a provider-diagnosed cycle must
record ``cause_signal_id`` as ``null`` rather than omitting the key, because a consumer
distinguishing "no customer input" from "this build does not record it" has only the key to go on.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.audit.events import RECOMMENDATION_RECORDED
from revora.diagnosis.service import (
    EVIDENCE_CAUSE_REFINED,
    EVIDENCE_CUSTOMER_SIGNAL_ID,
    EVIDENCE_STATED_REASON,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    CaseState,
    DelayReason,
    DiagnosisEvidenceSource,
    DiagnosisMethod,
    EstimationMethod,
    Provenance,
    RiskCause,
    ValidationStatus,
)
from revora.domain.failure_taxonomy import EVIDENCE_SOURCE
from revora.optimizer.service import run_optimizer
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store

pytestmark = pytest.mark.pg


class _Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture(autouse=True)
def installed_secrets() -> Iterator[None]:
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"M" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"E" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"E" * 32).decode(),
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
# Seeding: one case, one diagnosis, one baseline, one priced candidate set
# ---------------------------------------------------------------------------


def _seed(
    engine: Engine,
    *,
    cause: RiskCause,
    evidence: dict[str, object],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A case in ``DECISION_PENDING`` with everything the optimizer reads already recorded.

    Seeded with SQL rather than by running the pipeline, so the test's input is exactly the
    ``diagnosis.evidence`` document R20.C4 writes and nothing else varies. The stages before the
    optimizer are covered by their own tests; what this file is about is what the optimizer does
    with the row it finds.
    """
    merchant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    event_id = uuid.uuid4()
    baseline_id = uuid.uuid4()
    moment = now()
    encrypted = payload_cipher().encrypt(
        json.dumps({"event": "payment.failed", "payload": {}}).encode()
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'Provenance merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"prov-{merchant_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, raw_payload_nonce, key_version,
                    canonical, correlation_id, signature_verified, received_at, created_at
                ) VALUES (
                    :id, :merchant_id, :eid, 'payment.failed',
                    :ct, :nonce, :kv, :canonical, :corr, true, :received, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "eid": f"evt_{case_id.hex[:16]}",
                "ct": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "kv": encrypted.key_version,
                "canonical": json.dumps(
                    {
                        "provider_payment_id": f"pay_{case_id.hex[:14]}",
                        "method": "card",
                        "error_source": "issuer",
                    }
                ),
                "corr": str(uuid.uuid4()),
                "received": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, source_event_id, detected_at, window_end_at, provenance,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :pid, 500000, 'INR',
                    :ck, :sid, :detected, :window_end, :prov, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                # The state the optimizer runs in: estimation has produced a baseline and a
                # priced candidate set, and nothing has been authorized. The optimizer does not
                # transition the case — that is the policy engine's — so the state is an input
                # it reads for the audit record and not a precondition it enforces.
                "state": CaseState.DECISION_PENDING.value,
                "pid": f"pay_{case_id.hex[:14]}",
                "ck": f"ck-{case_id}",
                "sid": str(event_id),
                "detected": moment - timedelta(hours=3),
                "window_end": moment + timedelta(hours=168),
                "prov": Provenance.REAL.value,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, evidence, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :cause, 0.70, :method, 1,
                    true, false, :evidence, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "cause": cause.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
                "evidence": json.dumps(evidence),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO baseline_estimate (
                    id, merchant_id, case_id, decision_cycle, probability,
                    uncertainty_available, segment_id, method, provenance,
                    validation_status, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 1, 0.1000, false, 'L6:GLOBAL',
                    :method, :prov, :validation, now()
                )
                """
            ),
            {
                "id": str(baseline_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "method": EstimationMethod.PRIOR_FALLBACK.value,
                "prov": Provenance.REAL.value,
                "validation": ValidationStatus.UNVALIDATED_BASELINE.value,
            },
        )
        # The two null actions plus one real one, which is the minimum honest candidate set:
        # a comparison with nothing to lose to is not a comparison.
        for action, probability in (
            (CandidateAction.DO_NOTHING, Decimal("0.1000")),
            (CandidateAction.WAIT, Decimal("0.1000")),
            (CandidateAction.PAYMENT_LINK, Decimal("0.4000")),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_estimate (
                        id, merchant_id, case_id, baseline_estimate_id, action,
                        intervention_probability, financial_cost, communication_cost,
                        risk_cost, customer_cost, method, provenance, availability,
                        created_at
                    ) VALUES (
                        gen_random_uuid(), :merchant_id, :case_id, :bid, :action,
                        :prob, 0, 500, 0, 0, :method, :prov, :availability, now()
                    )
                    """
                ),
                {
                    "merchant_id": str(merchant_id),
                    "case_id": str(case_id),
                    "bid": str(baseline_id),
                    "action": action.value,
                    "prob": probability,
                    "method": EstimationMethod.PRIOR_FALLBACK.value,
                    "prov": Provenance.REAL.value,
                    "availability": ActionAvailability.AVAILABLE.value,
                },
            )
    return merchant_id, case_id


def _recommendation_decision(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    """The ``RECOMMENDATION_RECORDED`` record's ``decision`` document, as JSON."""
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                """
                SELECT decision FROM audit_record
                WHERE case_id = :case_id AND event_type = :event
                ORDER BY seq DESC LIMIT 1
                """
            ),
            {"case_id": str(case_id), "event": RECOMMENDATION_RECORDED},
        ).scalar_one()
    return dict(raw)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_a_customer_stated_cause_is_named_with_the_signal_that_produced_it(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R25.C5, on a cycle whose cause a Delay_Reason refined.

    The candidate set was priced against ``INSUFFICIENT_FUNDS`` because the customer said their
    salary was late, and the record now says both halves of that: the cause the set was built
    from, and the ``customer_signal`` row it came from. Without the second, this recommendation
    and one built from the provider's own error code are the same document — and a merchant
    explaining a second contact to a customer needs to know which of the two happened.
    """
    signal_id = uuid.uuid4()
    merchant_id, case_id = _seed(
        owner_engine,
        cause=RiskCause.INSUFFICIENT_FUNDS,
        evidence={
            EVIDENCE_CUSTOMER_SIGNAL_ID: str(signal_id),
            EVIDENCE_STATED_REASON: DelayReason.SALARY_OR_CASHFLOW_TIMING.value,
            EVIDENCE_CAUSE_REFINED: True,
            EVIDENCE_SOURCE: DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value,
        },
    )

    with factory() as session:
        outcome = run_optimizer(session, merchant_id, case_id, default_configuration())
        session.commit()

    assert outcome.recommendation_id is not None, outcome.failure_reason
    assert outcome.cause_provenance is not None
    assert outcome.cause_provenance.signal_id == str(signal_id)
    assert outcome.cause_provenance.risk_cause is RiskCause.INSUFFICIENT_FUNDS

    decision = _recommendation_decision(owner_engine, case_id)
    assert decision["cause_signal_id"] == str(signal_id), (
        "the recommendation does not name the Customer_Signal that produced its candidate set, "
        "so a customer-stated cause and a provider-derived one are indistinguishable in the "
        "record that R11.C6 makes the complete explanation of the decision"
    )
    assert decision["candidate_set_risk_cause"] == RiskCause.INSUFFICIENT_FUNDS.value
    assert decision["cause_delay_reason"] == DelayReason.SALARY_OR_CASHFLOW_TIMING.value
    assert decision["cause_refined_by_customer"] is True
    assert decision["customer_stated_cause"] is True
    # The comparison itself is unchanged: R25.C5 adds attribution and takes no ranking input.
    assert decision["ranked_on"] == "net_recovery_value"
    assert len(list(decision["candidates"])) == 3


def test_a_provider_diagnosed_cycle_records_the_key_as_null_rather_than_omitting_it(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """The absent case, and why the key is still written.

    A consumer distinguishing "this cycle had no customer input" from "this build does not record
    the field" has nothing but the key to go on. Omitting it makes the two identical, and the
    second is what a reader would assume of an old record — so every recommendation carries the
    key and an absent signal carries ``null`` under it.
    """
    merchant_id, case_id = _seed(
        owner_engine,
        cause=RiskCause.BANK_OR_NETWORK_FAILURE,
        evidence={EVIDENCE_SOURCE: DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value},
    )

    with factory() as session:
        outcome = run_optimizer(session, merchant_id, case_id, default_configuration())
        session.commit()

    assert outcome.recommendation_id is not None, outcome.failure_reason
    assert outcome.cause_provenance is not None
    assert outcome.cause_provenance.signal_id is None

    decision = _recommendation_decision(owner_engine, case_id)
    assert "cause_signal_id" in decision, (
        "the key is omitted for a provider-diagnosed cycle, so 'no customer input' and 'this "
        "build does not record it' are the same document"
    )
    assert decision["cause_signal_id"] is None
    assert decision["candidate_set_risk_cause"] == RiskCause.BANK_OR_NETWORK_FAILURE.value
    assert decision["cause_refined_by_customer"] is None
    assert decision["customer_stated_cause"] is False


def test_a_reason_that_refined_nothing_is_still_recorded(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """R20.C6 carried through to the recommendation, and it is a three-state field.

    ``OTHER`` names no Risk_Cause, so the recorded cause is unchanged and the candidate set is
    the provider's. The signal identifier is still recorded and ``cause_refined_by_customer`` is
    ``False`` rather than ``None`` — because "the customer told us something and it changed
    nothing" is exactly the fact that explains a second contact looking like a repeat of the
    first, and it is the fact a two-state field would lose.
    """
    signal_id = uuid.uuid4()
    merchant_id, case_id = _seed(
        owner_engine,
        cause=RiskCause.BANK_OR_NETWORK_FAILURE,
        evidence={
            EVIDENCE_CUSTOMER_SIGNAL_ID: str(signal_id),
            EVIDENCE_STATED_REASON: DelayReason.OTHER.value,
            EVIDENCE_CAUSE_REFINED: False,
            EVIDENCE_SOURCE: DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value,
        },
    )

    with factory() as session:
        run_optimizer(session, merchant_id, case_id, default_configuration())
        session.commit()

    decision = _recommendation_decision(owner_engine, case_id)
    assert decision["cause_signal_id"] == str(signal_id)
    assert decision["cause_delay_reason"] == DelayReason.OTHER.value
    assert decision["cause_refined_by_customer"] is False, (
        "'a reason was submitted and refined nothing' has collapsed into 'no reason was "
        "submitted', and only the first explains a repeated contact"
    )
    assert decision["candidate_set_risk_cause"] == RiskCause.BANK_OR_NETWORK_FAILURE.value
    assert decision["customer_stated_cause"] is False

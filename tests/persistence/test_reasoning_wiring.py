"""Tasks 49.3 and 49.4 against a real database, because the claims are about rows.

Five things cannot be established without Postgres, and each of them is the point of the
task rather than an implementation detail:

* **One ``ai_invocation`` row per invocation, including the failures** (R27.C12). A fake
  repository would only prove the fake agrees with itself, and the claim is about what a
  reliability report can read back.
* **The bound counted from committed rows** (R27.C13). The whole reason it is a query rather
  than a counter is that it has to survive a restart, and a test against an in-memory count
  would assert the property the design rejected.
* **R27.C16's short-circuit**, asserted with a transport that *raises* if it is reached. A
  negative needs a witness: "no call was made" is checkable only if a call would be loud.
* **The diagnosis's ``ai_invocation_id`` foreign key resolves.** The row is committed in its
  own transaction before ``run_diagnosis`` is entered precisely so it does, and an ordering
  mistake here would surface as a constraint violation rather than as a wrong number.
* **The explanation lands in the explanation-only column and nowhere else** (R27.C8), with
  ``influenced_recommendation`` false on the row that produced it.

``httpx.MockTransport`` is injected through the adapter's constructor throughout, so no test
here opens a socket. The credential is a resolver-backed obvious fake, which is also what
keeps these tests meaningful in the deployed reality where ``REVORA_LLM_CREDENTIAL`` is unset:
without it every one of these paths would take the ``Unavailable`` branch and assert nothing.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import Engine, text

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    CaseState,
    DiagnosisMethod,
    EstimationMethod,
    Provenance,
    ReasoningCallKind,
    RiskCause,
    SelectionReason,
    ValidationStatus,
)
from revora.domain.payment_event import CanonicalPaymentEvent
from revora.jobs import pipeline
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.reasoning.adapter import CONTENT_REJECTED, ReasoningAdapter, ReasoningVerdict

pytestmark = pytest.mark.pg

_LLM_CREDENTIAL = "fake-llm-credential"
_UNMAPPED_REASON = "a_reason_nobody_mapped"
"""A provider reason the failure taxonomy does not cover, so ``match.cause`` is ``None``.

The only condition under which R27.C16 permits a ``CAUSE_HYPOTHESIS`` call at all, which
makes it the reason every AI-path test here has to use."""

_MAPPED_REASON = "insufficient_funds"
"""A verified reason that resolves deterministically. The short-circuit's input."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_engine(migrated_url: str) -> Iterator[Engine]:
    """A process-wide engine, because the handlers open their own transactions.

    ``tenant_transaction`` resolves the engine from module state rather than taking one, which
    is what makes the handlers callable from the worker with a case id and nothing else. So a
    test that drives a handler has to install one.
    """
    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield engine
    finally:
        dispose_engine()


@pytest.fixture
def installed_secrets() -> Iterator[None]:
    """A store holding an obvious fake LLM credential, restored afterwards.

    Resolver-backed rather than environment-backed. The store is a module global, so leaving a
    fake installed would leak into every test that followed — and reading the environment
    would make these tests pass or fail on a variable the deployment deliberately does not
    set.
    """
    resolver = EnvironmentSecretResolver(
        {
            "REVORA_LLM_CREDENTIAL": _LLM_CREDENTIAL,
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"R" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"S" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


# ---------------------------------------------------------------------------
# Provider doubles
# ---------------------------------------------------------------------------


def _envelope(text_part: str) -> dict[str, object]:
    return {
        "candidates": [{"content": {"parts": [{"text": text_part}], "role": "model"}}],
        "modelVersion": "gemini-2.5-flash-001",
    }


def _answering(text_part: str, *, status: int = 200) -> ReasoningAdapter:
    """An adapter that answers every request with one text part. No socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=_envelope(text_part))

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


def _refusing(status: int) -> ReasoningAdapter:
    """An adapter whose provider declines. A non-2xx that is not worth retrying."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "no"}})

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


def _exploding(fail: Callable[[], None]) -> ReasoningAdapter:
    """An adapter that calls ``fail()`` if it is ever asked for anything.

    The witness for every "no call was made" claim in this module. An adapter that merely
    returned nothing would let a short-circuit regression pass silently, because the
    deterministic outcome is the same either way — which is exactly how a short-circuit stops
    short-circuiting without anybody noticing.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        fail()
        raise AssertionError("unreachable")

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


def _cause_body(cause: RiskCause = RiskCause.ABANDONMENT, confidence: str = "0.87") -> str:
    return json.dumps(
        {
            "cause": cause.value,
            "confidence": confidence,
            "evidence_summary": "provider reason unrecognised; customer note read",
        }
    )


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _seed_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    error_reason: str,
    amount: int = 250_000,
    state: CaseState = CaseState.DETECTED,
) -> uuid.UUID:
    """A webhook event with the given error fields, and a case opened from it.

    Raw SQL rather than ingestion, so the error fields are exactly what the test says they
    are — the diagnosis assertions would otherwise depend on canonicalization too, and
    canonicalization has its own tests.
    """
    event_id = uuid.uuid4()
    case_id = uuid.uuid4()
    moment = now()
    canonical = CanonicalPaymentEvent(
        event_name="payment.failed",
        provider_payment_id=f"pay_{case_id.hex[:16]}",
        amount=amount,
        currency="INR",
        status="failed",
        method="card",
        error_code="BAD_REQUEST_ERROR",
        error_reason=error_reason,
        error_source="issuer_bank",
        error_step="payment_authorization",
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
                    :id, :m, :provider_event_id, 'payment.failed',
                    :ciphertext, CAST(:canonical AS jsonb), :received_at,
                    :correlation_id, true, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "m": str(merchant_id),
                "provider_event_id": f"evt_{event_id.hex[:16]}",
                "ciphertext": b"not-a-real-ciphertext",
                "canonical": json.dumps(canonical.to_dict()),
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
                    :id, :m, :state, :provider_payment_id, :amount,
                    'INR', :customer_key, :source_event_id, :detected_at,
                    :window_end_at, 0, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state.value,
                "provider_payment_id": canonical.provider_payment_id,
                "amount": amount,
                "customer_key": f"ck-{case_id}",
                "source_event_id": str(event_id),
                "detected_at": moment,
                "window_end_at": moment + timedelta(hours=168),
            },
        )
    return case_id


def _seed_comparison(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> uuid.UUID:
    """A recorded comparison: a diagnosis, a baseline, two candidates and one recommendation.

    Seeded rather than produced by running the optimizer, because what is under test is what
    ``_explain_decision`` does with an *already recorded* comparison. Driving the optimizer
    would make the assertions depend on the priors, and a prior change would then look like a
    reasoning-layer regression.
    """
    baseline_id = uuid.uuid4()
    winner_estimate = uuid.uuid4()
    runner_estimate = uuid.uuid4()
    recommendation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    :id, :m, :c, :cause, 1.000, :method, 0, true, false, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "m": str(merchant_id),
                "c": str(case_id),
                "cause": RiskCause.INSUFFICIENT_FUNDS.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO baseline_estimate (
                    id, merchant_id, case_id, decision_cycle, probability, method,
                    provenance, validation_status, created_at
                ) VALUES (:id, :m, :c, 0, 0.2000, :method, :prov, :status, now())
                """
            ),
            {
                "id": str(baseline_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "method": EstimationMethod.PRIOR_FALLBACK.value,
                "prov": Provenance.REAL.value,
                "status": ValidationStatus.UNVALIDATED_BASELINE.value,
            },
        )
        for estimate_id, action in (
            (winner_estimate, CandidateAction.PAYMENT_LINK),
            (runner_estimate, CandidateAction.DO_NOTHING),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_estimate (
                        id, merchant_id, case_id, baseline_estimate_id, action,
                        intervention_probability, method, provenance, availability, created_at
                    ) VALUES (
                        :id, :m, :c, :b, :action, 0.2800, :method, :prov, :avail, now()
                    )
                    """
                ),
                {
                    "id": str(estimate_id),
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "b": str(baseline_id),
                    "action": action.value,
                    "method": EstimationMethod.PRIOR_FALLBACK.value,
                    "prov": Provenance.REAL.value,
                    "avail": ActionAvailability.AVAILABLE.value,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO recommendation (
                    id, merchant_id, case_id, baseline_estimate_id, decision_cycle,
                    selected_action, selection_reason, created_at
                ) VALUES (:id, :m, :c, :b, 0, :action, :reason, now())
                """
            ),
            {
                "id": str(recommendation_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "b": str(baseline_id),
                "action": CandidateAction.PAYMENT_LINK.value,
                "reason": SelectionReason.HIGHEST_NET_VALUE.value,
            },
        )
        for estimate_id, action, net, rank in (
            (winner_estimate, CandidateAction.PAYMENT_LINK, 7_000, 1),
            (runner_estimate, CandidateAction.DO_NOTHING, 0, 2),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO recommendation_candidate (
                        id, merchant_id, recommendation_id, candidate_estimate_id, action,
                        incremental_probability, expected_incremental_revenue,
                        net_recovery_value, excluded, rank, created_at
                    ) VALUES (
                        :id, :m, :r, :ce, :action, 0.0800, 20000, :net, false, :rank, now()
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "m": str(merchant_id),
                    "r": str(recommendation_id),
                    "ce": str(estimate_id),
                    "action": action.value,
                    "net": net,
                    "rank": rank,
                },
            )
    return recommendation_id


def _seed_approved_decision(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> None:
    """An approved, unconsumed ``PAYMENT_LINK`` decision, so a draft is worth asking for."""
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
                    :id, :m, :c, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, :action, 'POLICY_CHECK', 0, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "m": str(merchant_id),
                "c": str(case_id),
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
                "action": CandidateAction.PAYMENT_LINK.value,
            },
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _invocations(engine: Engine, case_id: uuid.UUID) -> list[dict[str, object]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT call_kind, prompt_contract_id, model_id, model_version, latency_ms, "
                "verdict, influenced_recommendation, raw_response_truncated "
                "FROM ai_invocation WHERE case_id = :c ORDER BY created_at, id"
            ),
            {"c": str(case_id)},
        ).mappings()
        return [dict(row) for row in rows]


def _diagnosis(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT cause, confidence, method, substituted_to_unknown, "
                "ai_invocation_id, evidence FROM diagnosis "
                "WHERE case_id = :c AND is_active ORDER BY decision_cycle DESC LIMIT 1"
            ),
            {"c": str(case_id)},
        ).mappings()
        return dict(next(iter(row)))


def _audit_types(engine: Engine, case_id: uuid.UUID) -> list[str]:
    with engine.connect() as connection:
        return [
            str(value)
            for value in connection.execute(
                text("SELECT event_type FROM audit_record WHERE case_id = :c ORDER BY seq"),
                {"c": str(case_id)},
            ).scalars()
        ]


def _audit_evidence(engine: Engine, case_id: uuid.UUID, event_type: str) -> dict[str, object]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT evidence FROM audit_record "
                "WHERE case_id = :c AND event_type = :e ORDER BY seq LIMIT 1"
            ),
            {"c": str(case_id), "e": event_type},
        ).scalar_one()
    assert isinstance(row, dict)
    return row


def _message_count(engine: Engine, case_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT customer_message_count FROM recovery_case WHERE id = :c"),
                {"c": str(case_id)},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# R27.C16 — the short-circuit
# ---------------------------------------------------------------------------


def test_a_deterministic_diagnosis_issues_no_cause_hypothesis_call(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """R27.C16. A mapped provider reason costs nothing at all.

    Asserted with an adapter that fails the test if it is reached, because the deterministic
    outcome would look identical if the call *were* made and its answer discarded — and that
    is precisely the regression a softer assertion would miss.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_MAPPED_REASON)
    calls: list[str] = []

    pipeline.handle_diagnosis(
        merchant_id,
        case_id,
        reasoning=_exploding(lambda: calls.append("issued")),
    )

    assert calls == [], "a deterministic diagnosis reached the provider (R27.C16)"
    assert _invocations(installed_engine, case_id) == []
    row = _diagnosis(installed_engine, case_id)
    assert row["method"] == DiagnosisMethod.DETERMINISTIC.value
    assert Decimal(str(row["confidence"])) == Decimal("1.000")
    assert row["ai_invocation_id"] is None


def test_the_absent_credential_path_writes_no_row_and_asks_nothing(
    installed_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """R27.C7, and the deployed reality. Note the missing ``installed_secrets``.

    With no credential the resolver returns no adapter, so ``handle_diagnosis`` never enters
    the reasoning path: no client is constructed, no payload is built, nothing is waited for
    and no ``ai_invocation`` row is written. The diagnosis is the one the deterministic table
    produced, and this test is the whole of "the suite passes with the credential absent"
    stated as an assertion rather than as a hope.
    """
    previous = set_secret_store(SecretStore(EnvironmentSecretResolver({})))
    try:
        assert pipeline.reasoning_adapter() is None
        case_id = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)
        pipeline.handle_diagnosis(merchant_id, case_id)
    finally:
        set_secret_store(previous)

    assert _invocations(installed_engine, case_id) == []
    row = _diagnosis(installed_engine, case_id)
    assert row["method"] == DiagnosisMethod.FALLBACK_UNKNOWN.value
    assert row["cause"] == RiskCause.UNKNOWN.value
    assert row["ai_invocation_id"] is None
    evidence = row["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["reasoning_layer_invoked"] is False


# ---------------------------------------------------------------------------
# R27.C4, R27.C12 — the accepted path
# ---------------------------------------------------------------------------


def test_an_accepted_cause_is_recorded_ai_assisted_with_one_invocation_row(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """The full accepted path: capped confidence, a resolved foreign key, one row.

    ``ai_invocation_id`` is the assertion that carries the ordering claim. The row is
    committed in its own transaction *before* ``run_diagnosis`` is entered, so the foreign key
    resolves; had the two been written in one transaction with the diagnosis first, this would
    fail as a constraint violation rather than as a wrong number.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)

    pipeline.handle_diagnosis(
        merchant_id, case_id, reasoning=_answering(_cause_body(confidence="1.0"))
    )

    rows = _invocations(installed_engine, case_id)
    assert len(rows) == 1
    (row,) = rows
    assert row["call_kind"] == ReasoningCallKind.CAUSE_HYPOTHESIS.value
    assert row["prompt_contract_id"] == "cause-hypothesis/1"
    assert row["verdict"] == ReasoningVerdict.ACCEPTED.value
    assert row["influenced_recommendation"] is True
    assert row["model_id"] == "gemini-2.5-flash"
    assert row["model_version"] == "gemini-2.5-flash-001"
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
    assert row["raw_response_truncated"] is None

    diagnosis = _diagnosis(installed_engine, case_id)
    assert diagnosis["method"] == DiagnosisMethod.AI_ASSISTED.value
    assert diagnosis["cause"] == RiskCause.ABANDONMENT.value
    assert Decimal(str(diagnosis["confidence"])) == Decimal("0.990"), (
        "R27.C4 caps an AI-assisted confidence at 0.99; 1.000 stays reserved for a "
        "deterministic mapping-table match"
    )
    assert diagnosis["ai_invocation_id"] is not None
    evidence = diagnosis["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["reasoning_layer_invoked"] is True


def test_a_schema_rejected_response_falls_back_and_retains_the_body(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """R27.C5 and R27.C12 together. The refusal is a row, not an absence.

    ``influenced_recommendation`` is false and the recorded cause is ``UNKNOWN`` — so a query
    for "how often did a model's answer actually get used" cannot be inflated by an invocation
    whose answer was thrown away.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)
    invalid = json.dumps({"cause": "VIBES", "confidence": "0.9", "evidence_summary": "hmm"})

    pipeline.handle_diagnosis(merchant_id, case_id, reasoning=_answering(invalid))

    (row,) = _invocations(installed_engine, case_id)
    assert row["verdict"] == ReasoningVerdict.REJECTED_SCHEMA.value
    assert row["influenced_recommendation"] is False
    retained = row["raw_response_truncated"]
    assert isinstance(retained, str) and "VIBES" in retained

    diagnosis = _diagnosis(installed_engine, case_id)
    assert diagnosis["method"] == DiagnosisMethod.REJECTED_AI_OUTPUT.value
    assert diagnosis["cause"] == RiskCause.UNKNOWN.value
    assert Decimal(str(diagnosis["confidence"])) == Decimal("0.000")
    assert diagnosis["substituted_to_unknown"] is True
    assert diagnosis["ai_invocation_id"] is not None


def test_a_provider_refusal_is_a_row_with_the_unavailable_verdict(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """A request that reached the wire and was declined still costs a row.

    ``UNAVAILABLE`` rather than a verdict of its own, because R27.C12 names five — the reason
    lives in the adapter's ``UnavailableReason`` and, where one applies, in the audit event
    type. ``model_id`` is present because a request *was* issued to a known model.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)

    pipeline.handle_diagnosis(merchant_id, case_id, reasoning=_refusing(401))

    (row,) = _invocations(installed_engine, case_id)
    assert row["verdict"] == ReasoningVerdict.UNAVAILABLE.value
    assert row["influenced_recommendation"] is False
    assert row["model_id"] == "gemini-2.5-flash"

    diagnosis = _diagnosis(installed_engine, case_id)
    assert diagnosis["method"] == DiagnosisMethod.FALLBACK_UNKNOWN.value


# ---------------------------------------------------------------------------
# R27.C13 — the bound, from committed rows
# ---------------------------------------------------------------------------


def test_the_per_case_bound_is_read_from_committed_rows(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """R27.C13, and the reason it is a query.

    The rows are inserted directly, which is the point: nothing in this process has counted
    anything, so a bound held in memory would be at zero. Counting committed rows is what
    makes the allowance survive a restart, a redelivery and a second worker — and an exploding
    adapter proves the bound is checked *before* the request rather than after it.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)
    bound = pipeline.reasoning_call_bound(default_configuration())
    with installed_engine.begin() as connection:
        for _ in range(bound):
            connection.execute(
                text(
                    """
                    INSERT INTO ai_invocation (
                        id, merchant_id, case_id, call_kind, prompt_contract_id,
                        verdict, influenced_recommendation, created_at
                    ) VALUES (:id, :m, :c, :kind, 'cause-hypothesis/1', 'TIMEOUT', false, now())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "kind": ReasoningCallKind.CAUSE_HYPOTHESIS.value,
                },
            )

    calls: list[str] = []
    pipeline.handle_diagnosis(
        merchant_id, case_id, reasoning=_exploding(lambda: calls.append("issued"))
    )

    assert calls == [], "the per-case bound did not stop the request"
    assert len(_invocations(installed_engine, case_id)) == bound
    assert _diagnosis(installed_engine, case_id)["ai_invocation_id"] is None


def test_the_bound_counts_only_this_case(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """Per case, not per merchant. One busy case must not silence another's allowance."""
    spent = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)
    fresh = _seed_case(installed_engine, merchant_id, error_reason=_UNMAPPED_REASON)
    bound = pipeline.reasoning_call_bound(default_configuration())
    with installed_engine.begin() as connection:
        for _ in range(bound):
            connection.execute(
                text(
                    """
                    INSERT INTO ai_invocation (
                        id, merchant_id, case_id, call_kind, prompt_contract_id,
                        verdict, influenced_recommendation, created_at
                    ) VALUES (:id, :m, :c, :kind, 'cause-hypothesis/1', 'TIMEOUT', false, now())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "m": str(merchant_id),
                    "c": str(spent),
                    "kind": ReasoningCallKind.CAUSE_HYPOTHESIS.value,
                },
            )

    pipeline.handle_diagnosis(merchant_id, fresh, reasoning=_answering(_cause_body()))

    assert len(_invocations(installed_engine, fresh)) == 1
    assert len(_invocations(installed_engine, spent)) == bound


# ---------------------------------------------------------------------------
# R27.C8 — the explanation, and the column it may not leave
# ---------------------------------------------------------------------------


def test_the_explanation_lands_in_the_explanation_only_column_and_influenced_nothing(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """R27.C8. Prose on one column, and a row saying it changed nothing.

    ``influenced_recommendation`` is false by construction on this call kind rather than false
    because nobody set it: the selected action came from an integer comparison that had already
    committed before the model was asked. The selection and its reason are re-read afterwards
    and asserted unchanged, because "held no influence" is a claim about the other columns.
    """
    case_id = _seed_case(
        installed_engine, merchant_id, error_reason=_MAPPED_REASON, state=CaseState.DECISION_PENDING
    )
    recommendation_id = _seed_comparison(installed_engine, merchant_id, case_id)
    body = json.dumps({"explanation": "A payment link nets the most after costs."})

    pipeline._explain_decision(
        merchant_id, case_id, _answering(body), correlation_id=None
    )

    with installed_engine.connect() as connection:
        row = dict(
            next(
                iter(
                    connection.execute(
                        text(
                            "SELECT selected_action, selection_reason, ai_explanation_text "
                            "FROM recommendation WHERE id = :r"
                        ),
                        {"r": str(recommendation_id)},
                    ).mappings()
                )
            )
        )
    assert row["ai_explanation_text"] == "A payment link nets the most after costs."
    assert row["selected_action"] == CandidateAction.PAYMENT_LINK.value
    assert row["selection_reason"] == SelectionReason.HIGHEST_NET_VALUE.value

    (invocation,) = _invocations(installed_engine, case_id)
    assert invocation["call_kind"] == ReasoningCallKind.DECISION_EXPLANATION.value
    assert invocation["verdict"] == ReasoningVerdict.ACCEPTED.value
    assert invocation["influenced_recommendation"] is False, (
        "R27.C8: a DECISION_EXPLANATION may never claim to have influenced the selection"
    )


def test_a_rejected_explanation_leaves_the_column_null_and_still_writes_a_row(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """An over-long explanation is refused rather than truncated into the column.

    ``parse_decision_explanation`` rejects anything past
    ``REASONING_EXPLANATION_MAX_LENGTH``, so the recommendation carries no prose and the
    invocation row carries the refusal. Storing a sentence that stopped mid-word would turn
    the bound from a validation gate into a formatting step.
    """
    case_id = _seed_case(
        installed_engine, merchant_id, error_reason=_MAPPED_REASON, state=CaseState.DECISION_PENDING
    )
    recommendation_id = _seed_comparison(installed_engine, merchant_id, case_id)
    too_long = "x" * (pipeline.REASONING_EXPLANATION_MAX_LENGTH + 1)

    pipeline._explain_decision(
        merchant_id,
        case_id,
        _answering(json.dumps({"explanation": too_long})),
        correlation_id=None,
    )

    with installed_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT ai_explanation_text FROM recommendation WHERE id = :r"),
            {"r": str(recommendation_id)},
        ).scalar_one()
    assert stored is None

    (invocation,) = _invocations(installed_engine, case_id)
    assert invocation["verdict"] == ReasoningVerdict.REJECTED_SCHEMA.value
    assert invocation["influenced_recommendation"] is False


def test_a_second_optimizer_run_does_not_spend_a_second_explanation_call(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """Idempotent under job retry: prose already on the row is not re-asked for.

    A retried optimizer job would otherwise spend a call to overwrite an explanation that is
    already there, which is a cost with no benefit and a second string for the dashboard to
    have shown.
    """
    case_id = _seed_case(
        installed_engine, merchant_id, error_reason=_MAPPED_REASON, state=CaseState.DECISION_PENDING
    )
    _seed_comparison(installed_engine, merchant_id, case_id)
    body = json.dumps({"explanation": "A payment link nets the most after costs."})

    pipeline._explain_decision(merchant_id, case_id, _answering(body), correlation_id=None)
    calls: list[str] = []
    pipeline._explain_decision(
        merchant_id, case_id, _exploding(lambda: calls.append("issued")), correlation_id=None
    )

    assert calls == []
    assert len(_invocations(installed_engine, case_id)) == 1


# ---------------------------------------------------------------------------
# R27.C9, R27.C10 — the link description, suppressed and substituted
# ---------------------------------------------------------------------------


def test_an_accepted_draft_is_returned_for_substitution(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """The happy path of the execution site: a validated draft, and a row that says it was used."""
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_MAPPED_REASON)
    _seed_approved_decision(installed_engine, merchant_id, case_id)
    draft = "Your payment to Acme did not go through. You can pay securely using this link."

    returned = pipeline._draft_link_description(
        merchant_id,
        case_id,
        _answering(json.dumps({"description": draft})),
        correlation_id=None,
    )

    assert returned == draft
    (row,) = _invocations(installed_engine, case_id)
    assert row["call_kind"] == ReasoningCallKind.LINK_DESCRIPTION.value
    assert row["verdict"] == ReasoningVerdict.ACCEPTED.value
    assert row["influenced_recommendation"] is True


def test_a_content_rejected_draft_is_suppressed_and_retained(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """R27.C10, all four halves of it.

    The draft is suppressed (``None`` is returned, so the engine composes the approved
    template), ``CONTENT_REJECTED`` is written naming the violated rule, the draft is retained
    on both the record and the invocation row, and the customer-message counter is
    **unchanged** by the suppression. The last one is the one worth being explicit about: the
    counter moves inside the engine on the transition into ``EXECUTING``, once, for the action
    — so a suppressed draft costs no message allowance, which is what makes this a
    substitution rather than a non-execution.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_MAPPED_REASON)
    _seed_approved_decision(installed_engine, merchant_id, case_id)
    before = _message_count(installed_engine, case_id)
    # Names an amount that is not the case's ₹2,500.00, which is the amount-equality rule.
    draft = "Pay INR 9,999.00 to Acme to complete your order."

    returned = pipeline._draft_link_description(
        merchant_id,
        case_id,
        _answering(json.dumps({"description": draft})),
        correlation_id=None,
    )

    assert returned is None, "a content-rejected draft must not reach the customer"
    assert _message_count(installed_engine, case_id) == before, (
        "the suppression moved the customer-message counter; a draft nobody sent must cost "
        "no message allowance"
    )

    (row,) = _invocations(installed_engine, case_id)
    assert row["verdict"] == ReasoningVerdict.REJECTED_CONTENT.value
    assert row["influenced_recommendation"] is False
    assert row["raw_response_truncated"] == draft

    assert CONTENT_REJECTED in _audit_types(installed_engine, case_id)
    evidence = _audit_evidence(installed_engine, case_id, CONTENT_REJECTED)
    assert evidence["violated_rule"] == "AMOUNT_MISMATCH"
    assert evidence["retained_draft"] == draft


def test_no_draft_is_requested_without_an_approved_customer_visible_decision(
    installed_engine: Engine, installed_secrets: None, merchant_id: uuid.UUID
) -> None:
    """A case the engine would refuse anyway costs no model call.

    The read that decides is a filter on whether to ask, never an authorization: the engine
    reloads and re-authorizes everything under its own lock regardless, so nothing this
    returns is trusted. What it buys is that the per-case allowance is not spent on a case
    with no approval to act under.
    """
    case_id = _seed_case(installed_engine, merchant_id, error_reason=_MAPPED_REASON)
    calls: list[str] = []

    returned = pipeline._draft_link_description(
        merchant_id, case_id, _exploding(lambda: calls.append("issued")), correlation_id=None
    )

    assert returned is None
    assert calls == []
    assert _invocations(installed_engine, case_id) == []

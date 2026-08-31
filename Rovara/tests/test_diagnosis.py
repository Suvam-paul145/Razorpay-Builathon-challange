"""Diagnosis service behaviour that needs no database.

Three groups, all in the ``pure`` tier.

**The zero-AI claim, structurally.** R3.C1 and R3.C2 say the deterministic path issues
no LLM request and no provider call. That is not something a runtime test can establish
— it would only show that *this* run made no call — so it is checked as a property of
the import graph. ``lint-imports`` enforces the layering; this narrows it to the two
packages that specifically matter and fails with the offending line named.

**The deterministic derivation.** Given a taxonomy match, which method and confidence
get recorded. Pure, because the derivation is pure.

**The fraud routing predicate.** R3.C6 forces a policy evaluation before any action,
irrespective of method, and the edge case — a risk signal that was substituted to
``UNKNOWN`` — is the one worth pinning down in a test rather than in a comment.

The database-level guarantees (one active row per cycle, idempotency under retry, the
coverage aggregate) are in ``tests/persistence/test_diagnosis_service.py``, because the
partial unique index and the JSONB aggregation are Postgres facts and a fake would only
prove the fake agrees with itself.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from revora.audit.events import (
    ALL_EVENT_TYPES,
    DIAGNOSIS_ALREADY_RECORDED,
    DIAGNOSIS_RECORDED,
    DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN,
    DIAGNOSIS_UNMAPPED_REASON,
    MERCHANT_INTEGRATION_FAULT,
)
from revora.diagnosis.service import (
    DETERMINISTIC_CONFIDENCE,
    UNKNOWN_CONFIDENCE,
    DiagnosisOutcome,
    RecordedDiagnosis,
    _requires_policy_evaluation,
    _resolve_from_match,
)
from revora.domain.enums import DiagnosisMethod, RiskCause
from revora.domain.failure_taxonomy import MatchOutcome, classify_failure

pytestmark = pytest.mark.pure

REPO_ROOT = Path(__file__).resolve().parents[1]
FLOOR = Decimal("0.60")

FORBIDDEN_PACKAGES = ("revora.reasoning", "revora.providers")
"""The two packages the deterministic path must not be able to reach. ``reasoning`` is
the LLM adapter; ``providers`` is the Razorpay client. An import of either from this
package would make "zero LLM invocations and zero provider calls" a promise instead of
a structural fact."""


def _match(
    *,
    reason: str | None = None,
    source: str | None = None,
    step: str | None = None,
    code: str | None = None,
    risk_reasons: frozenset[str] = frozenset(),
):
    return classify_failure(
        error_reason=reason,
        error_source=source,
        error_step=step,
        error_code=code,
        risk_reason_codes=risk_reasons,
    )


# ---------------------------------------------------------------------------
# The zero-AI, zero-provider claim
# ---------------------------------------------------------------------------


def test_diagnosis_package_cannot_reach_the_llm_or_the_provider() -> None:
    """No module in ``revora.diagnosis`` imports ``reasoning`` or ``providers``.

    Parsed rather than grepped so a mention inside a docstring — this file's own module
    docstring names both packages — cannot fail the check, and so the failure message
    can name the exact import statement.
    """
    offences: list[str] = []
    for path in sorted((REPO_ROOT / "revora" / "diagnosis").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            for module in modules:
                if module.startswith(FORBIDDEN_PACKAGES):
                    relative = path.relative_to(REPO_ROOT).as_posix()
                    offences.append(f"{relative}:{node.lineno}: imports {module}")
    assert not offences, (
        "the deterministic diagnosis path must issue zero LLM and zero provider "
        "calls, which is a property of the import graph:\n" + "\n".join(offences)
    )


def test_every_diagnosis_audit_event_type_is_registered() -> None:
    """The five new event types are declared in the one catalogue.

    ``audit.events`` exists so no component invents a type string, and a constant that
    is defined but missing from ``ALL_EVENT_TYPES`` is invisible to the test that
    asserts a writer's type is a member.
    """
    for event_type in (
        DIAGNOSIS_RECORDED,
        DIAGNOSIS_ALREADY_RECORDED,
        DIAGNOSIS_UNMAPPED_REASON,
        DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN,
        MERCHANT_INTEGRATION_FAULT,
    ):
        assert event_type in ALL_EVENT_TYPES


# ---------------------------------------------------------------------------
# What gets recorded, per taxonomy outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected_cause"),
    [
        ("insufficient_funds", RiskCause.INSUFFICIENT_FUNDS),
        ("card_expired", RiskCause.EXPIRED_PAYMENT_METHOD),
        ("bank_technical_error", RiskCause.BANK_OR_NETWORK_FAILURE),
        ("incorrect_otp", RiskCause.CUSTOMER_ACTION_REQUIRED),
        ("payment_cancelled", RiskCause.ABANDONMENT),
        ("invalid_order_id", RiskCause.TECHNICAL_ISSUE),
    ],
)
def test_a_match_records_deterministic_at_full_confidence(
    reason: str, expected_cause: RiskCause
) -> None:
    """A table hit is ``DETERMINISTIC`` at exactly 1.0, with no substitution.

    Confidence 1.0 is reserved for this method (R3.C10) — the AI path is capped at 0.99
    — so a 1.0 in the column means the provider told us, never that a model was sure.
    """
    recorded = _resolve_from_match(_match(reason=reason), FLOOR)
    assert recorded.cause is expected_cause
    assert recorded.method is DiagnosisMethod.DETERMINISTIC
    assert recorded.confidence == DETERMINISTIC_CONFIDENCE
    assert recorded.substituted_to_unknown is False


def test_an_unmapped_reason_records_fallback_unknown_at_zero() -> None:
    """No match yields ``UNKNOWN`` / ``FALLBACK_UNKNOWN`` / 0.0, substitution flagged.

    Zero rather than the confidence floor: the floor is where a claim stops being
    usable, and this is the absence of a claim.
    """
    recorded = _resolve_from_match(_match(reason="a_reason_nobody_has_seen"), FLOOR)
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.method is DiagnosisMethod.FALLBACK_UNKNOWN
    assert recorded.confidence == UNKNOWN_CONFIDENCE
    assert recorded.substituted_to_unknown is True


def test_already_paid_records_unknown_without_being_a_gap() -> None:
    """``order_already_paid`` records ``UNKNOWN`` but is not a coverage gap.

    The taxonomy answered correctly and named no cause, so the recorded diagnosis is
    ``UNKNOWN`` while the outcome in evidence stays ``NOT_AT_RISK`` — which is what
    keeps it out of the unmapped-reason backlog.
    """
    match = _match(reason="order_already_paid")
    recorded = _resolve_from_match(match, FLOOR)
    assert recorded.cause is RiskCause.UNKNOWN
    assert match.outcome is MatchOutcome.NOT_AT_RISK
    assert match.is_coverage_gap is False


def test_a_risk_signal_records_deterministic_and_is_never_substituted() -> None:
    """A configured risk reason is a full-confidence deterministic answer.

    It must not be substituted away: R3.C6 depends on the recorded cause reaching the
    policy engine, and a substituted risk signal that only appeared in evidence would
    be a fraud decline the gate never saw.
    """
    match = _match(
        reason="payment_risk_check_failed", risk_reasons=frozenset({"payment_risk_check_failed"})
    )
    recorded = _resolve_from_match(match, FLOOR)
    assert recorded.cause is RiskCause.FRAUD_OR_RISK_SIGNAL
    assert recorded.method is DiagnosisMethod.DETERMINISTIC
    assert recorded.substituted_to_unknown is False


# ---------------------------------------------------------------------------
# R3.C6: fraud routes to policy before anything is scheduled
# ---------------------------------------------------------------------------


def test_fraud_signal_requires_policy_evaluation() -> None:
    """A recorded ``FRAUD_OR_RISK_SIGNAL`` forces a policy decision first."""
    recorded = _resolve_from_match(
        _match(reason="compliance_violation", risk_reasons=frozenset({"compliance_violation"})),
        FLOOR,
    )
    assert _requires_policy_evaluation(recorded) is True


def test_a_substituted_fraud_signal_still_requires_policy_evaluation() -> None:
    """Substitution to ``UNKNOWN`` does not un-happen the risk decline.

    The substitution says "do not build an action set from this cause", not "the
    provider's risk decline did not occur". Reading only the recorded cause would let a
    low-confidence fraud answer skip the gate R3.C6 exists to force — which is the one
    place in the diagnosis layer where getting it wrong contacts a customer the provider
    declined for risk.
    """
    substituted = RecordedDiagnosis(
        cause=RiskCause.UNKNOWN,
        original_cause=RiskCause.FRAUD_OR_RISK_SIGNAL,
        confidence=Decimal("0.400"),
        method=DiagnosisMethod.AI_ASSISTED,
        substituted_to_unknown=True,
        substitution_reason="CONFIDENCE_BELOW_FLOOR",
    )
    assert _requires_policy_evaluation(substituted) is True


def test_an_ordinary_cause_does_not_set_the_fraud_gate() -> None:
    """Every case reaches policy eventually; only fraud sets this flag.

    The flag means "policy before anything is scheduled", not "policy at some point",
    so setting it for ordinary causes would drain it of meaning.
    """
    recorded = _resolve_from_match(_match(reason="insufficient_funds"), FLOOR)
    assert _requires_policy_evaluation(recorded) is False


# ---------------------------------------------------------------------------
# The outcome the job handler reads
# ---------------------------------------------------------------------------


def _outcome(method: DiagnosisMethod, **overrides: object) -> DiagnosisOutcome:
    return DiagnosisOutcome(
        diagnosis_id=None,
        cause=RiskCause.UNKNOWN,
        original_cause=RiskCause.UNKNOWN,
        confidence=UNKNOWN_CONFIDENCE,
        method=method,
        substituted_to_unknown=False,
        substitution_reason=None,
        match_key=None,
        rule_id="unmapped",
        deterministic_hit=False,
        coverage_gap=True,
        needs_operational_alert=False,
        requires_policy_evaluation=False,
        case_version=1,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "method",
    [
        DiagnosisMethod.DETERMINISTIC,
        DiagnosisMethod.FALLBACK_UNKNOWN,
        DiagnosisMethod.REJECTED_AI_OUTPUT,
        DiagnosisMethod.AI_ASSISTED,
    ],
)
def test_reasoning_invoked_defaults_to_not_invoked(method: DiagnosisMethod) -> None:
    """R3.C7's indicator defaults to ``False`` and is never inferred from the method.

    Inference would be wrong in both directions. ``FALLBACK_UNKNOWN`` means "the table
    did not resolve it" on this path and "the model timed out" on the reasoning path
    (R3.C9), so the method cannot distinguish one invocation from none. And
    ``REJECTED_AI_OUTPUT`` means a request *was* sent and its answer discarded — an
    invocation that a "did AI contribute" reading would report as zero. Only the
    component that sent the request knows, so it states it rather than the reader
    guessing.
    """
    assert _outcome(method).reasoning_layer_invoked is False


def test_reasoning_invoked_can_be_stated_by_the_ai_path() -> None:
    """The field is settable, which is what makes it a fact rather than a default.

    Task 14's adapter is the only caller that will ever set it true. Asserted here so
    the field is not quietly read-only by accident.
    """
    assert (
        _outcome(DiagnosisMethod.AI_ASSISTED, reasoning_layer_invoked=True)
    ).reasoning_layer_invoked is True

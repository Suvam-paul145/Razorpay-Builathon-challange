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
from revora.customer.signals import DELAY_REASON_CAUSE, cause_for_delay_reason
from revora.diagnosis.service import (
    DETERMINISTIC_CONFIDENCE,
    SUBSTITUTION_BELOW_FLOOR,
    UNKNOWN_CONFIDENCE,
    DiagnosisOutcome,
    RecordedDiagnosis,
    _requires_policy_evaluation,
    _resolve_from_match,
    resolve_stated_reason_diagnosis,
)
from revora.domain.enums import (
    DelayReason,
    DiagnosisEvidenceSource,
    DiagnosisMethod,
    RiskCause,
)
from revora.domain.failure_taxonomy import (
    EVIDENCE_SOURCE,
    MatchOutcome,
    classify_failure,
    match_evidence,
)
from revora.platform.config import default_configuration

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


# ---------------------------------------------------------------------------
# The Delay_Reason mapping table, and the second deterministic source
# ---------------------------------------------------------------------------


STATED_CONFIDENCE = Decimal("0.900")
"""``CUSTOMER_STATED_CAUSE_CONFIDENCE``'s catalogue default, written out rather than read
from the configuration.

Deliberate duplication, and the one place in this file where it is: a test that asks the
catalogue what the number is would agree with the catalogue however the catalogue changed,
including a change to 0.10 that R20.C7 forbids. The assertion below that ties this to the
catalogue is the one that catches a drift; this constant is what makes it an assertion
rather than a tautology."""


def _provider(cause: RiskCause = RiskCause.UNKNOWN) -> RecordedDiagnosis:
    """What the failure taxonomy would have produced, as the refinement's starting point."""
    method = (
        DiagnosisMethod.FALLBACK_UNKNOWN
        if cause is RiskCause.UNKNOWN
        else DiagnosisMethod.DETERMINISTIC
    )
    confidence = UNKNOWN_CONFIDENCE if cause is RiskCause.UNKNOWN else DETERMINISTIC_CONFIDENCE
    return RecordedDiagnosis(
        cause=cause,
        original_cause=cause,
        confidence=confidence,
        method=method,
        substituted_to_unknown=False,
        substitution_reason=None,
    )


def test_the_mapping_table_is_exactly_what_r20_c5_declares() -> None:
    """R20.C5, row by row, as a literal.

    Written out rather than derived, because this table *is* the requirement. A test that
    computed the expected mapping from the same source the code reads would pass whatever
    the table said, and every row of it is an ``[ASSUMPTION]`` about what a stranger meant
    by a phrase — precisely the kind of value that gets adjusted without the requirement
    being reopened.
    """
    assert DELAY_REASON_CAUSE == {
        DelayReason.SALARY_OR_CASHFLOW_TIMING: RiskCause.INSUFFICIENT_FUNDS,
        DelayReason.BANK_OR_CARD_PROBLEM: RiskCause.BANK_OR_NETWORK_FAILURE,
        DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW: RiskCause.INSUFFICIENT_FUNDS,
        DelayReason.OTHER: None,
        DelayReason.DISPUTES_THE_CHARGE: None,
        DelayReason.NO_LONGER_WANTS_THE_ORDER: None,
    }


def test_the_mapping_table_is_total_over_the_enumeration() -> None:
    """Every ``DelayReason`` has a row, so ``None`` never means "nobody added one".

    The table's own import-time check enforces this; asserting it here is what makes the
    check's absence a failure too. A seventh member falling through as ``None`` would read
    as "this reason names no cause" — a legitimate answer for three of the six members
    today — so the omission would be invisible rather than loud.
    """
    assert set(DELAY_REASON_CAUSE) == set(DelayReason)
    for reason in DelayReason:
        # No KeyError, and the accessor and the table agree.
        assert cause_for_delay_reason(reason) == DELAY_REASON_CAUSE[reason]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (DelayReason.SALARY_OR_CASHFLOW_TIMING, RiskCause.INSUFFICIENT_FUNDS),
        (DelayReason.BANK_OR_CARD_PROBLEM, RiskCause.BANK_OR_NETWORK_FAILURE),
        (DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW, RiskCause.INSUFFICIENT_FUNDS),
    ],
)
def test_a_mapped_reason_records_a_deterministic_cause_at_the_stated_confidence(
    reason: DelayReason, expected: RiskCause
) -> None:
    """R20.C4 for each of the three reasons that name a cause.

    ``DETERMINISTIC`` at a *configured* confidence rather than at 1.0 is the whole shape of
    the requirement: the method says a closed table produced it with no model involved, and
    the confidence says we believe a customer less than we believe the provider. R3.C10
    reserves exactly 1.0 for the provider, so the inequality is asserted rather than left
    implied.
    """
    recorded = resolve_stated_reason_diagnosis(
        reason=reason,
        provider=_provider(),
        stated_confidence=STATED_CONFIDENCE,
        confidence_floor=FLOOR,
    )
    assert recorded.cause is expected
    assert recorded.method is DiagnosisMethod.DETERMINISTIC
    assert recorded.confidence == STATED_CONFIDENCE
    assert recorded.confidence < DETERMINISTIC_CONFIDENCE
    assert recorded.substituted_to_unknown is False


@pytest.mark.parametrize(
    "reason",
    [
        DelayReason.OTHER,
        DelayReason.DISPUTES_THE_CHARGE,
        DelayReason.NO_LONGER_WANTS_THE_ORDER,
    ],
)
@pytest.mark.parametrize(
    "provider_cause",
    [RiskCause.UNKNOWN, RiskCause.INSUFFICIENT_FUNDS, RiskCause.FRAUD_OR_RISK_SIGNAL],
)
def test_a_reason_naming_no_cause_leaves_the_recorded_diagnosis_untouched(
    reason: DelayReason, provider_cause: RiskCause
) -> None:
    """R20.C6, and the two Hard_Stop_Reasons on the same terms.

    Untouched means *the same object*, not an equal one. The caller distinguishes "the
    customer's words refined the cause" from "they did not" by identity, so an equal copy
    would make the refinement flag depend on a comparison that a later field addition could
    silently break.

    Run against three provider causes rather than one because "unchanged" is only
    interesting when there was something to change. ``UNKNOWN`` alone would pass even if the
    function overwrote the cause with ``UNKNOWN``.
    """
    provider = _provider(provider_cause)
    assert (
        resolve_stated_reason_diagnosis(
            reason=reason,
            provider=provider,
            stated_confidence=STATED_CONFIDENCE,
            confidence_floor=FLOOR,
        )
        is provider
    )


def test_a_stated_cause_below_the_floor_is_substituted_and_says_why() -> None:
    """The stated path goes through R3.C8's gate like every other path.

    R20.C7 forbids configuring ``CUSTOMER_STATED_CAUSE_CONFIDENCE`` below
    ``DIAGNOSIS_CONFIDENCE_FLOOR``, and this is what the system does if that ordering is
    ever violated anyway: substitute to ``UNKNOWN`` and record ``CONFIDENCE_BELOW_FLOOR``.
    The alternative — exempting a customer-derived cause from the gate — would let the least
    trustworthy source in the system be the one input that outranks the rule.
    """
    recorded = resolve_stated_reason_diagnosis(
        reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
        provider=_provider(),
        stated_confidence=Decimal("0.100"),
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.original_cause is RiskCause.INSUFFICIENT_FUNDS
    assert recorded.substituted_to_unknown is True
    assert recorded.substitution_reason == SUBSTITUTION_BELOW_FLOOR


def test_the_configured_stated_confidence_sits_between_the_floor_and_the_reserved_value(
) -> None:
    """R20.C7 and R3.C10 as a bound on the catalogue's default.

    Both ends matter and for different reasons. Below the floor the capture is inert — every
    stated cause would be substituted to ``UNKNOWN`` and the second cycle would decide on
    exactly the evidence the first had. At 1.0 a stored confidence would stop meaning "the
    provider told us", which is the one thing that value is reserved to mean.
    """
    configured = default_configuration().CUSTOMER_STATED_CAUSE_CONFIDENCE
    assert configured == STATED_CONFIDENCE
    assert configured >= default_configuration().DIAGNOSIS_CONFIDENCE_FLOOR
    assert configured < DETERMINISTIC_CONFIDENCE


def test_a_refinement_cannot_switch_off_the_fraud_gate() -> None:
    """R3.C6 survives R20.C4.

    The failure this guards against is specific and it is the worst one available on this
    path: a customer whose card was declined for risk says "my salary is late", the cause is
    refined to ``INSUFFICIENT_FUNDS``, and the fraud routing the provider's decline demanded
    is gone — an untrusted input having switched off a gate no trusted input can. So the
    superseded cause is read whether or not it is the one that got recorded.
    """
    provider = _provider(RiskCause.FRAUD_OR_RISK_SIGNAL)
    refined = resolve_stated_reason_diagnosis(
        reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
        provider=provider,
        stated_confidence=STATED_CONFIDENCE,
        confidence_floor=FLOOR,
    )
    assert refined.cause is RiskCause.INSUFFICIENT_FUNDS
    assert _requires_policy_evaluation(refined) is False, (
        "the refined diagnosis alone carries no risk signal, which is exactly why the "
        "superseded cause has to be passed in"
    )
    assert _requires_policy_evaluation(refined, superseded=provider.cause) is True


def test_the_evidence_source_enumeration_distinguishes_the_two_sources() -> None:
    """The audit-trail half of R20.C4.

    Two deterministic tables can now each produce a ``DETERMINISTIC`` cause, so the method
    no longer answers "what was read". A reviewer weighing a recommendation has to be able
    to tell a provider error code from a stranger's account of their own finances without
    joining back to the signal table, and a two-member enumeration is how that is stated.
    """
    assert set(DiagnosisEvidenceSource) == {
        DiagnosisEvidenceSource.PROVIDER_ERROR_CODE,
        DiagnosisEvidenceSource.CUSTOMER_STATED_REASON,
    }
    evidence = match_evidence(
        classify_failure(
            error_reason="insufficient_funds",
            error_source="bank",
            error_step="authorization",
            error_code="BAD_REQUEST_ERROR",
            risk_reason_codes=frozenset(),
        ),
        error_reason="insufficient_funds",
        error_source="bank",
        error_step="authorization",
        error_code="BAD_REQUEST_ERROR",
        payment_method="card",
    )
    assert evidence[EVIDENCE_SOURCE] == DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value

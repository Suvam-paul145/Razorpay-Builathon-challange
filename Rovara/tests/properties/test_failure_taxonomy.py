"""The mapping table as executable documentation, plus the rules around it.

The parametrized case list below is one entry per verified ``error_reason`` in the
design's Deterministic Layer table. That is the point of it: the table in code and the
table in the document are two copies of the same decision, and this is the diff between
them. If someone adds a reason to
:data:`revora.domain.failure_taxonomy._REASON_GROUPS` without adding it here,
``test_every_mapped_reason_is_documented_here`` fails and names it.

Everything here is the ``pure`` tier — no database, no clock, no configuration load. The
taxonomy takes strings and a frozenset and returns a dataclass, which is exactly the
shape that makes a hundred assertions cost microseconds.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from revora.diagnosis.service import (
    DETERMINISTIC_CONFIDENCE,
    SUBSTITUTION_BELOW_FLOOR,
    SUBSTITUTION_METHOD_UNTRUSTED,
    UNKNOWN_CONFIDENCE,
    resolve_recorded_diagnosis,
)
from revora.domain.enums import DiagnosisMethod, RiskCause
from revora.domain.failure_taxonomy import (
    ALREADY_PAID_REASONS,
    CODE_TO_CAUSE,
    ERROR_STEPS,
    EVIDENCE_ERROR_REASON,
    EVIDENCE_MATCH_KEY,
    EVIDENCE_MATCHED,
    EVIDENCE_RULE_ID,
    MERCHANT_INTEGRATION_FAULT_REASONS,
    REASON_TO_CAUSE,
    UNMAPPED_RULE_ID,
    ErrorCode,
    ErrorSource,
    ErrorStep,
    MatchKey,
    MatchOutcome,
    classify_failure,
    match_evidence,
)

pytestmark = pytest.mark.pure

#: The configured risk set as the seed migration ships it. Passed explicitly rather
#: than loaded, because the whole point of the fraud condition being configured is that
#: the taxonomy does not know where the set came from.
SEEDED_RISK_REASONS = frozenset({"payment_risk_check_failed", "compliance_violation"})

#: One entry per verified provider reason, in the design's table order. This list *is*
#: the documentation of the Deterministic Layer subsection.
DOCUMENTED_REASONS: tuple[tuple[str, RiskCause], ...] = (
    ("insufficient_funds", RiskCause.INSUFFICIENT_FUNDS),
    ("card_expired", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("card_not_enrolled", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("card_number_invalid", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("incorrect_card_details", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("incorrect_card_expiry_date", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("card_type_invalid", RiskCause.EXPIRED_PAYMENT_METHOD),
    ("bank_technical_error", RiskCause.BANK_OR_NETWORK_FAILURE),
    ("upi_app_technical_error", RiskCause.BANK_OR_NETWORK_FAILURE),
    ("bank_account_invalid", RiskCause.BANK_OR_NETWORK_FAILURE),
    ("user_not_registered_for_netbanking", RiskCause.BANK_OR_NETWORK_FAILURE),
    ("server_error", RiskCause.TECHNICAL_ISSUE),
    ("payment_failed", RiskCause.TECHNICAL_ISSUE),
    ("verification_failed", RiskCause.TECHNICAL_ISSUE),
    ("capture_failed", RiskCause.TECHNICAL_ISSUE),
    ("payment_timed_out", RiskCause.ABANDONMENT),
    ("otp_expired", RiskCause.ABANDONMENT),
    ("otp_attempts_exceeded", RiskCause.ABANDONMENT),
    ("pin_attempts_exceeded", RiskCause.ABANDONMENT),
    ("payment_cancelled", RiskCause.ABANDONMENT),
    ("incorrect_otp", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("incorrect_cvv", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("incorrect_pin", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("incorrect_atm_pin", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("authentication_failed", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("invalid_vpa", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("transaction_limit_exceeded", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("transaction_daily_limit_exceeded", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("transaction_frequency_limit_exceeded", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("international_transaction_not_allowed", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("transaction_on_vpa_restricted", RiskCause.CUSTOMER_ACTION_REQUIRED),
    ("payment_risk_check_failed", RiskCause.FRAUD_OR_RISK_SIGNAL),
    ("compliance_violation", RiskCause.FRAUD_OR_RISK_SIGNAL),
    ("debit_instrument_blocked", RiskCause.FRAUD_OR_RISK_SIGNAL),
    ("input_validation_failed", RiskCause.TECHNICAL_ISSUE),
    ("invalid_order_id", RiskCause.TECHNICAL_ISSUE),
    ("order_amount_mismatch", RiskCause.TECHNICAL_ISSUE),
    ("live_mode_not_enabled", RiskCause.TECHNICAL_ISSUE),
    ("payment_method_not_enabled", RiskCause.TECHNICAL_ISSUE),
    ("bank_not_enabled", RiskCause.TECHNICAL_ISSUE),
)


def _classify(
    *,
    reason: str | None = None,
    source: str | None = None,
    step: str | None = None,
    code: str | None = None,
    risk_reasons: frozenset[str] = frozenset(),
):
    """Call the classifier with only the fields a test cares about."""
    return classify_failure(
        error_reason=reason,
        error_source=source,
        error_step=step,
        error_code=code,
        risk_reason_codes=risk_reasons,
    )


# ---------------------------------------------------------------------------
# Tier 1: the reason table, one case per verified reason (task 13.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("reason", "expected"), DOCUMENTED_REASONS, ids=lambda v: str(v))
def test_documented_reason_maps_to_expected_cause(reason: str, expected: RiskCause) -> None:
    """Every verified provider reason yields the cause the design assigns it.

    Deliberately does not pass a configured risk set, so this asserts the static
    table's own answer. ``payment_risk_check_failed`` still resolves to
    ``FRAUD_OR_RISK_SIGNAL`` here because it is in the table as well as in the seeded
    configuration — belt and braces, and the separate precedence test below covers the
    case where the two disagree.
    """
    match = _classify(reason=reason)
    assert match.cause is expected
    assert match.match_key is MatchKey.ERROR_REASON
    assert match.rule_id == f"error_reason:{reason}"
    assert match.is_deterministic_hit
    assert not match.is_coverage_gap


def test_every_mapped_reason_is_documented_here() -> None:
    """The table in code and the list above hold the same reasons.

    This is the test that keeps the documentation honest. Adding a reason to the
    taxonomy without adding it here fails with the reason named, so the parametrized
    list cannot silently fall behind the thing it documents.
    """
    documented = {reason for reason, _ in DOCUMENTED_REASONS}
    in_table = set(REASON_TO_CAUSE)
    assert in_table - documented == set(), "reason in the table but not documented above"
    assert documented - in_table == set(), "reason documented above but absent from the table"


def test_reason_lookup_is_case_and_whitespace_insensitive() -> None:
    """A stray space or an upper-cased token still resolves.

    Defensive rather than corrective — the provider emits lower-case snake_case — but a
    hand-repaired quarantine row must not create a phantom unmapped reason in the
    coverage metric.
    """
    assert _classify(reason="  INSUFFICIENT_FUNDS ").cause is RiskCause.INSUFFICIENT_FUNDS


# ---------------------------------------------------------------------------
# Tier 2: source and step refinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [ErrorSource.INTERNAL.value, ErrorSource.GATEWAY.value])
@pytest.mark.parametrize("step", [*sorted(ERROR_STEPS), None])
def test_internal_or_gateway_source_yields_technical_issue(
    source: str, step: str | None
) -> None:
    """``source ∈ {internal, gateway}`` is infrastructure, at every step.

    Including with no step at all: "internal, step unknown" is still unambiguously not
    something a customer can fix, and a redelivered or partial payload can carry a
    source without a step.
    """
    match = _classify(source=source, step=step)
    assert match.cause is RiskCause.TECHNICAL_ISSUE
    assert match.match_key is MatchKey.SOURCE_STEP
    assert match.is_deterministic_hit


def test_customer_at_authentication_yields_customer_action_required() -> None:
    """``customer`` at ``payment_authentication`` means a retryable customer error."""
    match = _classify(
        source=ErrorSource.CUSTOMER.value, step=ErrorStep.PAYMENT_AUTHENTICATION.value
    )
    assert match.cause is RiskCause.CUSTOMER_ACTION_REQUIRED
    assert match.match_key is MatchKey.SOURCE_STEP
    assert match.rule_id == "source_step:customer:payment_authentication"


@pytest.mark.parametrize(
    "step",
    [
        ErrorStep.PAYMENT_AUTHORIZATION.value,
        ErrorStep.PAYMENT_CAPTURE.value,
        ErrorStep.PAYMENT_INITIATION.value,
        None,
    ],
)
def test_customer_source_at_other_steps_is_not_refined(step: str | None) -> None:
    """``customer`` outside authentication resolves to nothing, on purpose.

    A customer-sourced failure at authorization could be an insufficient balance, a
    limit, or a risk decline. Guessing between them from the step alone would put a
    fabricated cause where ``UNKNOWN`` belongs, and the fabrication would then be
    labelled ``DETERMINISTIC`` at confidence 1.0.
    """
    match = _classify(source=ErrorSource.CUSTOMER.value, step=step)
    assert match.outcome is MatchOutcome.UNMAPPED
    assert match.cause is None


def test_reason_takes_precedence_over_source_and_step() -> None:
    """A recognized reason wins over the coarser refinement, as the order requires.

    ``insufficient_funds`` arriving with ``source = internal`` must stay
    ``INSUFFICIENT_FUNDS``: the provider named the cause, and the location of the
    failure is the weaker signal.
    """
    match = _classify(
        reason="insufficient_funds",
        source=ErrorSource.INTERNAL.value,
        step=ErrorStep.PAYMENT_AUTHORIZATION.value,
    )
    assert match.cause is RiskCause.INSUFFICIENT_FUNDS
    assert match.match_key is MatchKey.ERROR_REASON


# ---------------------------------------------------------------------------
# Tier 3: the error code family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", [ErrorCode.GATEWAY_ERROR.value, ErrorCode.SERVER_ERROR.value]
)
def test_infrastructure_codes_yield_technical_issue(code: str) -> None:
    """The two unambiguous code families resolve when nothing better is available."""
    match = _classify(code=code)
    assert match.cause is RiskCause.TECHNICAL_ISSUE
    assert match.match_key is MatchKey.ERROR_CODE
    assert match.rule_id == f"error_code:{code}"


def test_bad_request_error_is_deliberately_unmapped() -> None:
    """``BAD_REQUEST_ERROR`` spans three different causes, so it maps to none of them.

    It covers an incorrect CVV, an expired card, and a mismatched order amount — three
    causes with three different eligible action sets. Picking one would be fabrication
    dressed as coverage, and the unmapped count is the honest signal instead.
    """
    match = _classify(code=ErrorCode.BAD_REQUEST_ERROR.value)
    assert match.outcome is MatchOutcome.UNMAPPED
    assert ErrorCode.BAD_REQUEST_ERROR.value not in CODE_TO_CAUSE


def test_source_step_takes_precedence_over_code() -> None:
    """Where it failed beats which family it belongs to.

    Both resolve to ``TECHNICAL_ISSUE`` here, so the assertion is on the recorded match
    key rather than the cause — the key is what the coverage metric groups by, and a
    tier silently answering for another one is exactly what
    ``match_key_counts`` exists to expose.
    """
    match = _classify(
        source=ErrorSource.GATEWAY.value, code=ErrorCode.BAD_REQUEST_ERROR.value
    )
    assert match.match_key is MatchKey.SOURCE_STEP


# ---------------------------------------------------------------------------
# The configured fraud condition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(SEEDED_RISK_REASONS))
def test_configured_risk_reason_yields_fraud_or_risk_signal(reason: str) -> None:
    """Membership in the configured set produces ``FRAUD_OR_RISK_SIGNAL``.

    Derived from a configured set, not from a provider flag field — no such field
    exists on the payment entity — and not from a hard-coded condition.
    """
    match = _classify(reason=reason, risk_reasons=SEEDED_RISK_REASONS)
    assert match.cause is RiskCause.FRAUD_OR_RISK_SIGNAL
    assert match.outcome is MatchOutcome.RISK_SIGNAL
    assert match.match_key is MatchKey.RISK_REASON_CODE
    assert match.rule_id == f"risk_reason_code:{reason}"


def test_configured_risk_set_overrides_the_static_table() -> None:
    """The configured answer wins where the two disagree.

    ``authentication_failed`` is ``CUSTOMER_ACTION_REQUIRED`` in the table. An operator
    who adds it to ``RISK_REASON_CODES`` because their traffic shows it is a risk
    decline in disguise is making a deliberate, recorded safety decision, and a static
    table must not overrule it. This is the precedence assertion the requirement is
    about.
    """
    without = _classify(reason="authentication_failed")
    assert without.cause is RiskCause.CUSTOMER_ACTION_REQUIRED

    extended = SEEDED_RISK_REASONS | {"authentication_failed"}
    with_config = _classify(reason="authentication_failed", risk_reasons=extended)
    assert with_config.cause is RiskCause.FRAUD_OR_RISK_SIGNAL
    assert with_config.match_key is MatchKey.RISK_REASON_CODE


def test_empty_risk_set_leaves_the_table_answer_intact() -> None:
    """An empty configured set removes only the configured tier, not the table's row.

    ``payment_risk_check_failed`` is verified as a risk decline in the design's own
    table, so it stays ``FRAUD_OR_RISK_SIGNAL`` even with no configuration. Losing the
    fraud condition entirely because a bound was misconfigured would be the worst
    possible failure mode here.
    """
    match = _classify(reason="payment_risk_check_failed", risk_reasons=frozenset())
    assert match.cause is RiskCause.FRAUD_OR_RISK_SIGNAL


# ---------------------------------------------------------------------------
# Merchant-side integration faults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(MERCHANT_INTEGRATION_FAULT_REASONS))
def test_merchant_integration_fault_sets_the_operational_alert_flag(reason: str) -> None:
    """Our bug, not the customer's: ``TECHNICAL_ISSUE`` plus an alert.

    The flag is on the result rather than in a log line because messaging a customer
    about a fault we caused wastes the contact, and this whole class of failure is one
    nobody knows to go looking for.
    """
    match = _classify(reason=reason)
    assert match.cause is RiskCause.TECHNICAL_ISSUE
    assert match.needs_operational_alert is True


@pytest.mark.parametrize(
    "reason", ["insufficient_funds", "card_expired", "bank_technical_error", "server_error"]
)
def test_ordinary_failures_do_not_raise_an_operational_alert(reason: str) -> None:
    """A genuine payment failure is not an integration bug.

    ``server_error`` is the interesting case: it is ``TECHNICAL_ISSUE`` like the
    integration faults, and it must not raise the alert, or the alert becomes noise
    within a day.
    """
    assert _classify(reason=reason).needs_operational_alert is False


# ---------------------------------------------------------------------------
# The gaps and the non-causes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["totally_new_provider_reason", "some_reason_added_next_quarter", "", "   "],
)
def test_unmapped_reason_is_a_counted_coverage_gap(reason: str) -> None:
    """An unrecognized reason yields no cause and is flagged as a gap.

    Blank and whitespace-only reasons land here too, through the normalizer that maps
    them to absent. They are gaps of a different kind — a failure that carried no reason
    at all — and the repository's ``unmapped_reasons`` read separates them by returning
    a ``None`` reason.
    """
    match = _classify(reason=reason)
    assert match.outcome is MatchOutcome.UNMAPPED
    assert match.cause is None
    assert match.is_coverage_gap
    assert not match.is_deterministic_hit
    assert match.rule_id == UNMAPPED_RULE_ID
    assert match.match_key is None


def test_no_error_fields_at_all_is_unmapped_rather_than_an_error() -> None:
    """Every field absent is a legitimate input with an honest answer.

    The provider leaves these ``null`` where it has nothing to say, and the classifier
    is total: it returns ``UNMAPPED`` rather than raising, so a diagnosis job cannot be
    poisoned by a sparse payload.
    """
    assert _classify().outcome is MatchOutcome.UNMAPPED


@pytest.mark.parametrize("reason", sorted(ALREADY_PAID_REASONS))
def test_already_paid_is_not_at_risk_and_not_a_coverage_gap(reason: str) -> None:
    """``order_already_paid`` names no cause, and that is the table working.

    It must not count as a deterministic hit — there is no cause — and it must not count
    as a coverage gap either, or the unmapped-reason backlog fills with a reason that
    needs no entry.
    """
    match = _classify(reason=reason)
    assert match.outcome is MatchOutcome.NOT_AT_RISK
    assert match.cause is None
    assert not match.is_deterministic_hit
    assert not match.is_coverage_gap


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_evidence_carries_match_metadata_and_error_fields_only() -> None:
    """The evidence document is PII-free and holds what the metric reads.

    The keys asserted here are an interface: the repository's coverage aggregate reads
    ``taxonomy_outcome``, ``deterministic_match``, ``matched_key`` and ``error_reason``
    straight out of the JSONB column.
    """
    match = _classify(reason="insufficient_funds")
    evidence = match_evidence(
        match,
        error_reason="insufficient_funds",
        error_source="customer",
        error_step="payment_authorization",
        error_code="BAD_REQUEST_ERROR",
        payment_method="card",
    )
    assert evidence[EVIDENCE_MATCHED] is True
    assert evidence[EVIDENCE_MATCH_KEY] == MatchKey.ERROR_REASON.value
    assert evidence[EVIDENCE_RULE_ID] == "error_reason:insufficient_funds"
    assert evidence[EVIDENCE_ERROR_REASON] == "insufficient_funds"
    assert evidence["payment_method"] == "card"


def test_evidence_contains_no_contact_shaped_key() -> None:
    """No field that could hold a contact, an email or an instrument reference.

    Asserted against the key set rather than against a value, because the risk is a
    future edit adding a field, not this call leaking one. ``error_description`` is
    excluded deliberately: it is provider free text, the cause is never derived from
    it, and it is the one error field that has carried a partial instrument reference.
    """
    evidence = match_evidence(
        _classify(reason="incorrect_cvv"),
        error_reason="incorrect_cvv",
        error_source="customer",
        error_step="payment_authentication",
        error_code="BAD_REQUEST_ERROR",
        payment_method="card",
    )
    forbidden = {"contact", "email", "customer_key", "card", "vpa", "error_description"}
    assert forbidden.isdisjoint(evidence.keys())


def test_evidence_omits_absent_fields_rather_than_recording_null() -> None:
    """An absent provider field reads as absent, not as a recorded null.

    Same discipline as ``CanonicalPaymentEvent.to_dict``: a stored ``null`` is a value
    a downstream reader can mistake for one, and the coverage aggregate counting
    ``error_reason IS NULL`` rows depends on the difference.
    """
    evidence = match_evidence(
        _classify(source=ErrorSource.GATEWAY.value),
        error_reason=None,
        error_source=ErrorSource.GATEWAY.value,
        error_step=None,
        error_code=None,
        payment_method=None,
    )
    assert EVIDENCE_ERROR_REASON not in evidence
    assert "error_step" not in evidence
    assert evidence["error_source"] == ErrorSource.GATEWAY.value


# ---------------------------------------------------------------------------
# Table integrity
# ---------------------------------------------------------------------------


def test_every_mapped_cause_is_a_risk_cause_member() -> None:
    """No table entry names a cause outside the frozen enum."""
    members = set(RiskCause)
    assert set(REASON_TO_CAUSE.values()) <= members
    assert set(CODE_TO_CAUSE.values()) <= members


def test_no_reason_maps_to_unknown() -> None:
    """``UNKNOWN`` is what an *absence* of a match records, never a match.

    A table entry mapping to ``UNKNOWN`` would record ``method=DETERMINISTIC`` at
    confidence 1.0 alongside a cause that means "we do not know", which is a
    contradiction the optimizer would read as a confident answer.
    """
    assert RiskCause.UNKNOWN not in set(REASON_TO_CAUSE.values())
    assert RiskCause.UNKNOWN not in set(CODE_TO_CAUSE.values())


def test_merchant_fault_reasons_are_all_in_the_reason_table() -> None:
    """The alert set cannot name a reason the table does not resolve.

    A reason in the alert set but absent from the table would take the unmapped path
    and never raise its alert, which is the silent version of the failure this flag
    exists to make loud.
    """
    assert set(REASON_TO_CAUSE) >= MERCHANT_INTEGRATION_FAULT_REASONS


def test_already_paid_reasons_are_absent_from_the_reason_table() -> None:
    """``order_already_paid`` must not also carry a cause.

    If it were in both, the reason tier would resolve it to a cause before the
    not-at-risk branch could report that the payment already succeeded.
    """
    assert ALREADY_PAID_REASONS.isdisjoint(set(REASON_TO_CAUSE))


def test_tables_are_read_only() -> None:
    """The lookup tables reject mutation at runtime.

    Wrapped in a mapping proxy so a component cannot register a reason at import time
    and change what every other component diagnoses. A new reason is a source edit with
    a design reference next to it.
    """
    with pytest.raises(TypeError):
        REASON_TO_CAUSE["fabricated_reason"] = RiskCause.UNKNOWN  # type: ignore[index]


# ---------------------------------------------------------------------------
# The substitution gate (R3.C8) — pure, so it belongs in this tier
# ---------------------------------------------------------------------------

FLOOR = Decimal("0.60")


def test_deterministic_hit_is_recorded_unchanged() -> None:
    """Confidence 1.0 with ``DETERMINISTIC`` passes the gate untouched."""
    recorded = resolve_recorded_diagnosis(
        cause=RiskCause.INSUFFICIENT_FUNDS,
        confidence=DETERMINISTIC_CONFIDENCE,
        method=DiagnosisMethod.DETERMINISTIC,
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.INSUFFICIENT_FUNDS
    assert recorded.substituted_to_unknown is False
    assert recorded.substitution_reason is None


@pytest.mark.parametrize(
    "method", [DiagnosisMethod.FALLBACK_UNKNOWN, DiagnosisMethod.REJECTED_AI_OUTPUT]
)
def test_untrusted_method_substitutes_unknown_and_keeps_the_original(
    method: DiagnosisMethod,
) -> None:
    """An untrusted method records ``UNKNOWN`` at any confidence, original retained.

    Retained rather than discarded so the substitution is reviewable. A rejected AI
    answer whose cause vanished from the record is a rejection nobody can audit.
    """
    recorded = resolve_recorded_diagnosis(
        cause=RiskCause.ABANDONMENT,
        confidence=Decimal("0.950"),
        method=method,
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.original_cause is RiskCause.ABANDONMENT
    assert recorded.substituted_to_unknown is True
    assert recorded.substitution_reason == SUBSTITUTION_METHOD_UNTRUSTED
    assert recorded.confidence == Decimal("0.950")


def test_confidence_below_the_floor_substitutes_unknown() -> None:
    """Below ``DIAGNOSIS_CONFIDENCE_FLOOR`` the cause is not usable."""
    recorded = resolve_recorded_diagnosis(
        cause=RiskCause.ABANDONMENT,
        confidence=Decimal("0.599"),
        method=DiagnosisMethod.AI_ASSISTED,
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.original_cause is RiskCause.ABANDONMENT
    assert recorded.substitution_reason == SUBSTITUTION_BELOW_FLOOR


def test_confidence_exactly_at_the_floor_is_kept() -> None:
    """The floor is inclusive: at the floor the answer is still usable.

    ``Decimal`` on both sides is what makes this boundary deterministic. The same
    comparison in binary floating point is a coin flip on the third decimal place.
    """
    recorded = resolve_recorded_diagnosis(
        cause=RiskCause.ABANDONMENT,
        confidence=FLOOR,
        method=DiagnosisMethod.AI_ASSISTED,
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.ABANDONMENT
    assert recorded.substituted_to_unknown is False


def test_untrusted_method_reason_wins_over_the_confidence_reason() -> None:
    """A zero-confidence fallback records ``METHOD_NOT_TRUSTED``, not the floor reason.

    Both conditions hold. Only one is the explanation, and the dashboard groups by this
    token, so recording the incidental one would misattribute every fallback to a
    confidence problem.
    """
    recorded = resolve_recorded_diagnosis(
        cause=RiskCause.UNKNOWN,
        confidence=UNKNOWN_CONFIDENCE,
        method=DiagnosisMethod.FALLBACK_UNKNOWN,
        confidence_floor=FLOOR,
    )
    assert recorded.substitution_reason == SUBSTITUTION_METHOD_UNTRUSTED

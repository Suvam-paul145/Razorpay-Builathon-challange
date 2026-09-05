"""The verified provider failure taxonomy: error fields in, one ``RiskCause`` out.

This is the deterministic path, and it is the *primary* path. The single most
valuable verification result in design.md is that the provider already publishes the
failure reason in a machine-handleable field — ``error_reason`` on the payment entity
— so the common cases need no model, no prompt, and no network call to diagnose. The
LLM, if it is enabled at all, handles the tail.

**Every value below is copied from the design's Deterministic Layer table and its
Payment Failure Taxonomy finding. Nothing is invented.** That constraint is the whole
point: a plausible-looking reason string that the provider never emits is worse than
an absent one, because it looks like coverage and behaves like a hole. When a new
provider reason appears in traffic it falls through to ``UNMAPPED``, gets counted,
and the count is what tells us to extend this table — see the metric discussion
below.

Three keys, tried in a fixed order, because they carry different amounts of
information:

1. ``error_reason`` — the provider's own named cause. Most specific, so first.
2. ``(error_source, error_step)`` — *where* the failure happened when the reason is
   absent or unrecognized. The design's refinement rule: ``source ∈ {internal,
   gateway}`` pushes toward ``TECHNICAL_ISSUE`` because those are infrastructure
   rather than the customer, and ``source = customer`` at ``step =
   payment_authentication`` pushes toward ``CUSTOMER_ACTION_REQUIRED`` because the
   customer got an OTP, a PIN or a CVV wrong and can try again.
3. ``error_code`` — the coarse family. Last resort, and only two of the three
   verified codes are mapped at all (see :data:`CODE_TO_CAUSE`).

Two things sit outside that ordering and are handled before it:

**The fraud-or-risk condition is configured, not hard-coded.** No dedicated
fraud-flag or risk-score field exists on the payment entity; what exists is a set of
failure reasons. So ``FRAUD_OR_RISK_SIGNAL`` is derived from membership in a set the
caller passes in, sourced from ``Configuration.RISK_REASON_CODES``, and it is checked
*before* the ordinary tables. Precedence matters here in a way it does not elsewhere:
``debit_instrument_blocked`` is in the reason table as ``FRAUD_OR_RISK_SIGNAL``
already, but if an operator adds ``authentication_failed`` to the configured set
because their traffic shows it is a risk decline in disguise, the configured answer
has to win over the table's ``CUSTOMER_ACTION_REQUIRED``. A merchant extending the
risk set is making a deliberate, recorded safety decision, and a static table must
not quietly override it.

**Merchant-side integration faults are flagged, not just mapped.** The six reasons in
:data:`MERCHANT_INTEGRATION_FAULT_REASONS` all mean the integration sent the provider
something wrong — a bad order id, a mismatched amount, a payment method that was
never enabled. They map to ``TECHNICAL_ISSUE`` like any other technical failure, but
they are *our* bug, not the customer's, and messaging a customer about a fault we
caused is both useless and embarrassing. So the match result carries
``needs_operational_alert``, a boolean the caller must act on. A log line would not
do: a log line is something somebody has to go looking for, and the whole class of
failure here is one nobody knows to look for.

Purity: this module imports the standard library and ``revora.domain.enums``, nothing
else. It is reachable from the diagnosis service, from a test with zero setup, and
from the synthetic harness, and it holds no ``float`` — there is no arithmetic here
at all, but the prohibition is worth stating because the hit-rate metric computed
from these matches is a ratio, and a ratio is exactly where a float creeps in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final

from revora.domain.enums import DiagnosisEvidenceSource, RiskCause

__all__ = [
    "ALREADY_PAID_REASONS",
    "CODE_TO_CAUSE",
    "ERROR_CODES",
    "ERROR_SOURCES",
    "ERROR_STEPS",
    "EVIDENCE_ERROR_CODE",
    "EVIDENCE_ERROR_REASON",
    "EVIDENCE_ERROR_SOURCE",
    "EVIDENCE_ERROR_STEP",
    "EVIDENCE_MATCHED",
    "EVIDENCE_MATCH_KEY",
    "EVIDENCE_METHOD_INPUT",
    "EVIDENCE_OPERATIONAL_ALERT",
    "EVIDENCE_OUTCOME",
    "EVIDENCE_RULE_ID",
    "EVIDENCE_SOURCE",
    "MERCHANT_INTEGRATION_FAULT_REASONS",
    "REASON_TO_CAUSE",
    "SOURCE_STEP_TO_CAUSE",
    "TECHNICAL_SOURCES",
    "UNMAPPED_RULE_ID",
    "ErrorCode",
    "ErrorSource",
    "ErrorStep",
    "MatchKey",
    "MatchOutcome",
    "TaxonomyMatch",
    "classify_failure",
    "match_evidence",
]


# ---------------------------------------------------------------------------
# The verified provider vocabulary
# ---------------------------------------------------------------------------


@unique
class ErrorSource(StrEnum):
    """Verified ``error_source`` values, unioned across payment methods.

    The provider documents these per method — cards do not emit ``customer_psp`` and
    UPI does not emit ``issuer`` — but a diagnosis reads whatever arrived rather than
    first deciding which method's vocabulary applies, so the union is the useful set.
    Method-specific narrowing would buy nothing here and would add a way to be wrong.
    """

    CUSTOMER = "customer"
    BUSINESS = "business"
    INTERNAL = "internal"
    GATEWAY = "gateway"
    ISSUER_BANK = "issuer_bank"
    CUSTOMER_PSP = "customer_psp"
    NETWORK = "network"
    BENEFICIARY_BANK = "beneficiary_bank"
    ISSUER = "issuer"


@unique
class ErrorStep(StrEnum):
    """Verified ``error_step`` values — how far the payment got before it failed."""

    PAYMENT_INITIATION = "payment_initiation"
    CARD_ENROLLMENT_CHECK = "card_enrollment_check"
    PAYMENT_AUTHENTICATION = "payment_authentication"
    PAYMENT_AUTHORIZATION = "payment_authorization"
    PAYMENT_CAPTURE = "payment_capture"
    PAYMENT_ELIGIBILITY_CHECK = "payment_eligibility_check"


@unique
class ErrorCode(StrEnum):
    """The three verified ``error_code`` families."""

    BAD_REQUEST_ERROR = "BAD_REQUEST_ERROR"
    GATEWAY_ERROR = "GATEWAY_ERROR"
    SERVER_ERROR = "SERVER_ERROR"


ERROR_SOURCES: Final[frozenset[str]] = frozenset(member.value for member in ErrorSource)
ERROR_STEPS: Final[frozenset[str]] = frozenset(member.value for member in ErrorStep)
ERROR_CODES: Final[frozenset[str]] = frozenset(member.value for member in ErrorCode)


# ---------------------------------------------------------------------------
# Tier 1: error_reason -> RiskCause
# ---------------------------------------------------------------------------

_REASON_GROUPS: Final[tuple[tuple[RiskCause, tuple[str, ...]], ...]] = (
    (RiskCause.INSUFFICIENT_FUNDS, ("insufficient_funds",)),
    # An expired, invalid or unenrolled card is one problem from the recovery point
    # of view: the instrument cannot be used and the customer needs a different one.
    # Splitting "expired" from "invalid" would produce two causes with identical
    # eligible actions, which is a distinction the value model cannot spend.
    (
        RiskCause.EXPIRED_PAYMENT_METHOD,
        (
            "card_expired",
            "card_not_enrolled",
            "card_number_invalid",
            "incorrect_card_details",
            "incorrect_card_expiry_date",
            "card_type_invalid",
        ),
    ),
    (
        RiskCause.BANK_OR_NETWORK_FAILURE,
        (
            "bank_technical_error",
            "upi_app_technical_error",
            "bank_account_invalid",
            "user_not_registered_for_netbanking",
        ),
    ),
    (
        RiskCause.TECHNICAL_ISSUE,
        ("server_error", "payment_failed", "verification_failed", "capture_failed"),
    ),
    # payment_timed_out -> ABANDONMENT is [ASSUMPTION]: the provider documents it as
    # also occurring when no gateway response arrives, which would make it a bank or
    # network failure instead. otp_expired -> ABANDONMENT is [INFERENCE] from the
    # customer having stopped responding. Both are recorded as judgement calls in
    # design.md, and both are exactly what the deterministic-hit-rate metric and the
    # LLM disagreement rate exist to test. They live in this table rather than in
    # configuration for now because moving them would need a bound per reason.
    (
        RiskCause.ABANDONMENT,
        (
            "payment_timed_out",
            "otp_expired",
            "otp_attempts_exceeded",
            "pin_attempts_exceeded",
            "payment_cancelled",
        ),
    ),
    (
        RiskCause.CUSTOMER_ACTION_REQUIRED,
        (
            "incorrect_otp",
            "incorrect_cvv",
            "incorrect_pin",
            "incorrect_atm_pin",
            "authentication_failed",
            "invalid_vpa",
            # Limit and restriction declines: the instrument works, but not for this
            # amount, not this often, or not across this border. The customer can act
            # — raise the limit, wait for the daily window, use another method — so
            # this is not a technical failure and not abandonment.
            "transaction_limit_exceeded",
            "transaction_daily_limit_exceeded",
            "transaction_frequency_limit_exceeded",
            "international_transaction_not_allowed",
            "transaction_on_vpa_restricted",
        ),
    ),
    (
        RiskCause.FRAUD_OR_RISK_SIGNAL,
        ("payment_risk_check_failed", "compliance_violation", "debit_instrument_blocked"),
    ),
    # Merchant-side integration faults. TECHNICAL_ISSUE plus an operational alert.
    (
        RiskCause.TECHNICAL_ISSUE,
        (
            "input_validation_failed",
            "invalid_order_id",
            "order_amount_mismatch",
            "live_mode_not_enabled",
            "payment_method_not_enabled",
            "bank_not_enabled",
        ),
    ),
)


def _build_reason_table() -> Mapping[str, RiskCause]:
    """Flatten the grouped declaration, refusing a reason declared twice.

    The groups are written to mirror the design's table row by row, which makes
    review a diff against the document. That layout admits one mistake the flat form
    would not — the same reason appearing under two causes — so the build refuses it
    rather than letting the later group silently win. "Two or more conflicting risk
    causes" is a case R3.C3 hands to the LLM, and it must not be reachable through a
    copy-paste error in a static table.
    """
    table: dict[str, RiskCause] = {}
    for cause, reasons in _REASON_GROUPS:
        for reason in reasons:
            existing = table.get(reason)
            if existing is not None and existing is not cause:
                raise ValueError(
                    f"error_reason {reason!r} is declared for both {existing} and {cause}"
                )
            table[reason] = cause
    return MappingProxyType(table)


REASON_TO_CAUSE: Final[Mapping[str, RiskCause]] = _build_reason_table()
"""Every verified ``error_reason`` and the ``RiskCause`` it determines.

Read-only. A component that wants a new reason handled adds it to
:data:`_REASON_GROUPS` with the design reference that verifies it, not at runtime.
"""

MERCHANT_INTEGRATION_FAULT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "input_validation_failed",
        "invalid_order_id",
        "order_amount_mismatch",
        "live_mode_not_enabled",
        "payment_method_not_enabled",
        "bank_not_enabled",
    }
)
"""Reasons that mean Revora's own integration, or the merchant's, sent the provider
something wrong. They map to ``TECHNICAL_ISSUE`` and additionally raise an
operational alert, because contacting a customer about a fault we caused wastes the
contact and misplaces the blame."""

ALREADY_PAID_REASONS: Final[frozenset[str]] = frozenset({"order_already_paid"})
"""``order_already_paid`` names no risk cause at all — it says the payment succeeded.

The design's table records it as "not at risk", which is a *detection* answer, and by
the time diagnosis runs a case already exists. So this is its own outcome
(:attr:`MatchOutcome.NOT_AT_RISK`) rather than a cause: the diagnosis records
``UNKNOWN`` and the flag travels in evidence, where the policy engine's
``ALREADY_PAID`` check and the outcome monitor can act on it. Deliberately *not*
counted as an unmapped reason, because the table handles it correctly and the
unmapped count is a signal about coverage gaps."""


# ---------------------------------------------------------------------------
# Tier 2: (error_source, error_step) -> RiskCause
# ---------------------------------------------------------------------------

TECHNICAL_SOURCES: Final[frozenset[str]] = frozenset(
    {ErrorSource.INTERNAL.value, ErrorSource.GATEWAY.value}
)
"""Sources that mean infrastructure failed rather than a person or an instrument.

``internal`` is the provider's own systems, ``gateway`` is the acquiring gateway.
Neither is something a customer can fix, and neither should produce a message asking
them to try again with a different card."""

_CUSTOMER_ACTION_SOURCE_STEP: Final[tuple[str, str]] = (
    ErrorSource.CUSTOMER.value,
    ErrorStep.PAYMENT_AUTHENTICATION.value,
)
"""The one customer-side refinement the design verifies. ``customer`` at any other
step is *not* mapped: a customer-sourced failure at ``payment_authorization`` could
be an insufficient balance, a limit, or a risk decline, and guessing between those
from the step alone would put a fabricated cause where ``UNKNOWN`` belongs."""


def _build_source_step_table() -> Mapping[tuple[str, str | None], RiskCause]:
    """Expand the design's two refinement rules into an explicit lookup.

    Expanded rather than evaluated as predicates so the table is inspectable — a
    reader can enumerate every ``(source, step)`` pair that resolves and see that
    nothing else does. The ``None`` step entries matter in practice: the provider
    populates ``error_step`` on the payment entity but a redelivered or partial
    payload can carry a source with no step, and "internal, step unknown" is still
    unambiguously a technical issue.
    """
    table: dict[tuple[str, str | None], RiskCause] = {}
    steps: tuple[str | None, ...] = (*sorted(ERROR_STEPS), None)
    for source in sorted(TECHNICAL_SOURCES):
        for step in steps:
            table[(source, step)] = RiskCause.TECHNICAL_ISSUE
    table[_CUSTOMER_ACTION_SOURCE_STEP] = RiskCause.CUSTOMER_ACTION_REQUIRED
    return MappingProxyType(table)


SOURCE_STEP_TO_CAUSE: Final[Mapping[tuple[str, str | None], RiskCause]] = (
    _build_source_step_table()
)
"""Where-it-failed refinement, consulted only when the reason did not resolve."""


# ---------------------------------------------------------------------------
# Tier 3: error_code -> RiskCause
# ---------------------------------------------------------------------------

CODE_TO_CAUSE: Final[Mapping[str, RiskCause]] = MappingProxyType(
    {
        ErrorCode.GATEWAY_ERROR.value: RiskCause.TECHNICAL_ISSUE,
        ErrorCode.SERVER_ERROR.value: RiskCause.TECHNICAL_ISSUE,
    }
)
"""The coarse family, and the honest limits of it.

The three code values are verified; the design gives no code-to-cause table, so this
tier is an **[INFERENCE]** drawn from the same reasoning the source refinement uses.
``GATEWAY_ERROR`` and ``SERVER_ERROR`` are infrastructure by definition, which is the
identical judgement as ``source ∈ {internal, gateway} → TECHNICAL_ISSUE``, so mapping
them costs nothing new.

``BAD_REQUEST_ERROR`` is deliberately absent. It spans an incorrect CVV
(``CUSTOMER_ACTION_REQUIRED``), an expired card (``EXPIRED_PAYMENT_METHOD``) and a
mismatched order amount (``TECHNICAL_ISSUE`` with an alert) — three different causes
with three different action sets. Picking one would be fabrication dressed as
coverage. An unresolved ``BAD_REQUEST_ERROR`` becomes ``UNKNOWN``, which is a true
statement, and the unmapped count makes the gap visible."""


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@unique
class MatchKey(StrEnum):
    """Which key produced the answer. Recorded in evidence and aggregated.

    This is the measurement half of the design's claim that the table covers the
    large majority of real failures. That claim is marked ``[INFERENCE]`` in
    design.md, and an inference stated in a document is not a fact about production.
    Persisting the key that matched turns "I expect this covers most failures" into a
    query, and a query is something an operator can be wrong about out loud.
    """

    ERROR_REASON = "error_reason"
    SOURCE_STEP = "source_step"
    ERROR_CODE = "error_code"
    RISK_REASON_CODE = "risk_reason_code"


@unique
class MatchOutcome(StrEnum):
    """What the taxonomy concluded.

    ``MAPPED`` and ``RISK_SIGNAL`` are both deterministic hits and both carry a
    cause; they are separate because the risk answer came from configuration rather
    than from the static table, and because R3.C6 routes it differently.
    ``NOT_AT_RISK`` and ``UNMAPPED`` both yield ``UNKNOWN``, and they are separate
    because only one of them is a coverage gap.
    """

    MAPPED = "MAPPED"
    RISK_SIGNAL = "RISK_SIGNAL"
    NOT_AT_RISK = "NOT_AT_RISK"
    UNMAPPED = "UNMAPPED"


UNMAPPED_RULE_ID: Final[str] = "unmapped"
"""Rule identifier recorded when nothing matched. A recorded token rather than a null,
so a ``GROUP BY`` over rule ids counts the misses alongside the hits instead of
dropping them."""


@dataclass(frozen=True, slots=True)
class TaxonomyMatch:
    """One classification: the cause, which key found it, and what to do about it.

    ``rule_id`` is what R3.C2 calls "the matched mapping rule identifier". It is
    formatted so the key and the matched value are both readable in a raw evidence
    document — ``error_reason:insufficient_funds``, ``source_step:internal:*`` — since
    the person reading it is usually asking "why did it say that" and a bare index
    would not answer them.
    """

    outcome: MatchOutcome
    cause: RiskCause | None
    match_key: MatchKey | None
    rule_id: str
    needs_operational_alert: bool = False

    @property
    def matched(self) -> bool:
        """Whether a cause was determined at all."""
        return self.cause is not None

    @property
    def is_deterministic_hit(self) -> bool:
        """Whether this counts toward the deterministic hit rate.

        ``NOT_AT_RISK`` does not: the table answered correctly but named no cause, so
        counting it as a hit would inflate the rate, and counting it as a miss would
        invent a coverage gap that does not exist. It is excluded from both, and
        reported on its own.
        """
        return self.outcome in (MatchOutcome.MAPPED, MatchOutcome.RISK_SIGNAL)

    @property
    def is_coverage_gap(self) -> bool:
        """Whether this is an unmapped reason that should extend the table."""
        return self.outcome is MatchOutcome.UNMAPPED


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


def classify_failure(
    *,
    error_reason: str | None,
    error_source: str | None,
    error_step: str | None,
    error_code: str | None,
    risk_reason_codes: frozenset[str],
) -> TaxonomyMatch:
    """Determine the ``RiskCause`` from persisted provider error fields.

    Pure and total: no I/O, no clock, no configuration lookup, and every input
    combination returns a result rather than raising. A failure with no error fields
    at all is a legitimate input — the provider leaves them ``null`` where it has
    nothing to say — and the answer for it is ``UNMAPPED``.

    Args:
        error_reason: the provider's ``error_reason``. Checked first.
        error_source: the provider's ``error_source``, for the refinement tier.
        error_step: the provider's ``error_step``, for the refinement tier.
        error_code: the provider's ``error_code``, for the last-resort tier.
        risk_reason_codes: ``Configuration.RISK_REASON_CODES``. Required rather than
            defaulted to empty: an empty default would let a caller that forgot the
            argument silently lose the fraud-or-risk condition, and the visible
            consequence of losing it is Revora messaging a customer whose payment the
            provider declined for risk. A caller with genuinely no configured set
            passes ``frozenset()`` and has said so.

    Returns:
        A :class:`TaxonomyMatch`. ``cause`` is ``None`` only for ``NOT_AT_RISK`` and
        ``UNMAPPED``, both of which the diagnosis service records as ``UNKNOWN``.
    """
    reason = _normalize(error_reason)
    source = _normalize(error_source)
    step = _normalize(error_step)
    code = _normalize_code(error_code)

    # Configured risk set first, ahead of the static table. See the module docstring:
    # an operator extending this set is making a safety decision, and a hard-coded
    # table must not overrule it.
    if reason is not None and reason in risk_reason_codes:
        return TaxonomyMatch(
            MatchOutcome.RISK_SIGNAL,
            RiskCause.FRAUD_OR_RISK_SIGNAL,
            MatchKey.RISK_REASON_CODE,
            f"{MatchKey.RISK_REASON_CODE.value}:{reason}",
        )

    if reason is not None and reason in ALREADY_PAID_REASONS:
        return TaxonomyMatch(
            MatchOutcome.NOT_AT_RISK,
            None,
            MatchKey.ERROR_REASON,
            f"{MatchKey.ERROR_REASON.value}:{reason}",
        )

    if reason is not None:
        cause = REASON_TO_CAUSE.get(reason)
        if cause is not None:
            return TaxonomyMatch(
                MatchOutcome.MAPPED,
                cause,
                MatchKey.ERROR_REASON,
                f"{MatchKey.ERROR_REASON.value}:{reason}",
                needs_operational_alert=reason in MERCHANT_INTEGRATION_FAULT_REASONS,
            )

    if source is not None:
        # The exact pair first, then the source with the step unknown. Not the other
        # way round: a source that resolves for every step resolves the same either
        # way, but a source that resolves for only one step must not be answered from
        # its wildcard.
        for candidate_step in (step, None):
            cause = SOURCE_STEP_TO_CAUSE.get((source, candidate_step))
            if cause is not None:
                rendered = candidate_step if candidate_step is not None else "*"
                return TaxonomyMatch(
                    MatchOutcome.MAPPED,
                    cause,
                    MatchKey.SOURCE_STEP,
                    f"{MatchKey.SOURCE_STEP.value}:{source}:{rendered}",
                )

    if code is not None:
        cause = CODE_TO_CAUSE.get(code)
        if cause is not None:
            return TaxonomyMatch(
                MatchOutcome.MAPPED,
                cause,
                MatchKey.ERROR_CODE,
                f"{MatchKey.ERROR_CODE.value}:{code}",
            )

    return TaxonomyMatch(MatchOutcome.UNMAPPED, None, None, UNMAPPED_RULE_ID)


def _normalize(value: str | None) -> str | None:
    """Strip and lower-case a provider token, mapping blank to absent.

    The provider's reason, source and step values are lower-case snake_case, so this
    is defensive rather than corrective. It exists because an empty string reaching a
    dictionary lookup is a miss that looks like a value, and a stray space in a
    hand-fixed quarantine row should not create a phantom unmapped reason in the
    coverage metric.
    """
    if value is None:
        return None
    stripped = value.strip().lower()
    return stripped or None


def _normalize_code(value: str | None) -> str | None:
    """Same, but upper-case: the code family is upper-case snake_case."""
    if value is None:
        return None
    stripped = value.strip().upper()
    return stripped or None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

EVIDENCE_SOURCE: Final[str] = "evidence_source"
"""Which input the cause was read off, as a :class:`DiagnosisEvidenceSource` value.

Declared here with the other evidence keys even though the *second* source lives above
the domain layer, because the key is the interface and one name for it is the point. A
row without this key predates R20.C4 and its source is the provider's error fields by
construction — there was no other one."""

EVIDENCE_OUTCOME: Final[str] = "taxonomy_outcome"
EVIDENCE_MATCH_KEY: Final[str] = "matched_key"
EVIDENCE_RULE_ID: Final[str] = "rule_id"
EVIDENCE_MATCHED: Final[str] = "deterministic_match"
EVIDENCE_OPERATIONAL_ALERT: Final[str] = "needs_operational_alert"
EVIDENCE_ERROR_REASON: Final[str] = "error_reason"
EVIDENCE_ERROR_SOURCE: Final[str] = "error_source"
EVIDENCE_ERROR_STEP: Final[str] = "error_step"
EVIDENCE_ERROR_CODE: Final[str] = "error_code"
EVIDENCE_METHOD_INPUT: Final[str] = "payment_method"


def match_evidence(
    match: TaxonomyMatch,
    *,
    error_reason: str | None,
    error_source: str | None,
    error_step: str | None,
    error_code: str | None,
    payment_method: str | None,
) -> dict[str, str | bool]:
    """The PII-free evidence document for one classification.

    Declared here rather than in the service because two other things read these
    keys: the repository aggregate that computes the deterministic hit rate and the
    unmapped-reason counts straight out of ``diagnosis.evidence``, and the dashboard
    that shows a merchant why a cause was chosen. Three readers and a JSONB column
    means the key names are an interface, and an interface belongs next to the thing
    that defines it.

    Nothing sensitive can enter here. The inputs are provider error tokens, the
    payment method, and the match metadata. There is no contact, no email, no
    instrument reference, and no amount — deliberately not even an amount band, since
    diagnosis does not use one and evidence should carry only what the cause was
    actually derived from.

    ``error_description`` is also excluded, despite being present on the payment
    entity. It is free text the provider may extend at any time, it is the one error
    field that has ever carried a partial instrument reference in the wild, and the
    cause is never derived from it. Retaining it would add risk and no explanatory
    power.
    """
    evidence: dict[str, str | bool] = {
        # Stated rather than implied. Until R20.C4 the provider's error fields were the
        # only source there was, so naming them looked redundant; now that a stated
        # reason can produce a DETERMINISTIC cause too, a row that names no source is
        # ambiguous rather than obvious.
        EVIDENCE_SOURCE: DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value,
        EVIDENCE_OUTCOME: match.outcome.value,
        EVIDENCE_RULE_ID: match.rule_id,
        EVIDENCE_MATCHED: match.is_deterministic_hit,
        EVIDENCE_OPERATIONAL_ALERT: match.needs_operational_alert,
    }
    if match.match_key is not None:
        evidence[EVIDENCE_MATCH_KEY] = match.match_key.value
    for key, value in (
        (EVIDENCE_ERROR_REASON, error_reason),
        (EVIDENCE_ERROR_SOURCE, error_source),
        (EVIDENCE_ERROR_STEP, error_step),
        (EVIDENCE_ERROR_CODE, error_code),
        (EVIDENCE_METHOD_INPUT, payment_method),
    ):
        normalized = value.strip() if value is not None else None
        if normalized:
            evidence[key] = normalized
    return evidence

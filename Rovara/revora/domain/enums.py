"""Every enumeration in Revora, in one place.

These are the authoritative definitions. The database stores them as ``TEXT`` with
a ``CHECK`` constraint rather than as a Postgres enum type, because a ``CHECK`` is
cheaper to evolve than an enum that needs a migration to extend — see the Type
Discipline discussion in design.md.

All members are string-valued so a stored row is readable without a lookup.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "ActionAvailability",
    "CaseState",
    "CheckOutcome",
    "DecisionSource",
    "DetectionVerdict",
    "DiagnosisMethod",
    "EstimationMethod",
    "ExclusionReason",
    "ExperimentGroup",
    "ExperimentLabel",
    "ExperimentState",
    "FieldKind",
    "IntentState",
    "InterventionStatus",
    "OutcomeClass",
    "PolicyCheck",
    "PolicyVerdict",
    "Provenance",
    "RiskCause",
    "SelectionReason",
    "TerminalReason",
    "ValidationStatus",
]


@unique
class RiskCause(StrEnum):
    """Why revenue is at risk. Derived from the provider's own error fields."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    EXPIRED_PAYMENT_METHOD = "EXPIRED_PAYMENT_METHOD"
    BANK_OR_NETWORK_FAILURE = "BANK_OR_NETWORK_FAILURE"
    TECHNICAL_ISSUE = "TECHNICAL_ISSUE"
    ABANDONMENT = "ABANDONMENT"
    CUSTOMER_ACTION_REQUIRED = "CUSTOMER_ACTION_REQUIRED"
    FRAUD_OR_RISK_SIGNAL = "FRAUD_OR_RISK_SIGNAL"
    UNKNOWN = "UNKNOWN"


@unique
class CaseState(StrEnum):
    """The fourteen states a recovery case can hold."""

    NEW = "NEW"
    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    DECISION_PENDING = "DECISION_PENDING"
    POLICY_CHECK = "POLICY_CHECK"
    ACTION_SCHEDULED = "ACTION_SCHEDULED"
    EXECUTING = "EXECUTING"
    WAITING_FOR_OUTCOME = "WAITING_FOR_OUTCOME"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


@unique
class PolicyVerdict(StrEnum):
    """The policy engine's answer. Only APPROVED permits an external effect."""

    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    ESCALATE = "ESCALATE"


@unique
class PolicyCheck(StrEnum):
    """The twelve policy checks, in their fixed evaluation order.

    The order is not arbitrary. Absolute prohibitions come first so an expensive or
    case-specific check can never be the reason a paid or opted-out customer was
    contacted. ``ORDER`` below is the authoritative sequence.
    """

    ALREADY_PAID = "ALREADY_PAID"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    FRAUD_OR_RISK = "FRAUD_OR_RISK"
    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    CONSENT_MISSING = "CONSENT_MISSING"
    HUMAN_OWNERSHIP = "HUMAN_OWNERSHIP"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    MAX_MESSAGES_REACHED = "MAX_MESSAGES_REACHED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ACTION_NOT_ELIGIBLE = "ACTION_NOT_ELIGIBLE"


POLICY_CHECK_ORDER: tuple[PolicyCheck, ...] = (
    PolicyCheck.ALREADY_PAID,
    PolicyCheck.ALREADY_TERMINAL,
    PolicyCheck.DUPLICATE_ACTION,
    PolicyCheck.FRAUD_OR_RISK,
    PolicyCheck.CUSTOMER_OPTED_OUT,
    PolicyCheck.CONSENT_MISSING,
    PolicyCheck.HUMAN_OWNERSHIP,
    PolicyCheck.WINDOW_EXPIRED,
    PolicyCheck.MAX_ATTEMPTS_REACHED,
    PolicyCheck.MAX_MESSAGES_REACHED,
    PolicyCheck.COOLDOWN_ACTIVE,
    PolicyCheck.ACTION_NOT_ELIGIBLE,
)
"""The fixed order. Every decision records an outcome for all twelve."""

POLICY_INPUT_UNAVAILABLE = "POLICY_INPUT_UNAVAILABLE"
"""Reason recorded when a required policy input could not be read. The engine
blocks rather than assuming the check would have passed."""


@unique
class CheckOutcome(StrEnum):
    """One policy check's result. UNAVAILABLE forces a block, never a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class DiagnosisMethod(StrEnum):
    """How a risk cause was arrived at."""

    DETERMINISTIC = "DETERMINISTIC"
    AI_ASSISTED = "AI_ASSISTED"
    REJECTED_AI_OUTPUT = "REJECTED_AI_OUTPUT"
    FALLBACK_UNKNOWN = "FALLBACK_UNKNOWN"


@unique
class EstimationMethod(StrEnum):
    """How a probability or cost figure was produced.

    ``DEFINITIONAL`` is reserved for DO_NOTHING, whose figures are fixed by
    definition rather than estimated. ``UNCALIBRATED`` means no observation of that
    action exists for the segment yet, and it propagates to every surface.
    """

    DETERMINISTIC = "DETERMINISTIC"
    PRIOR_FALLBACK = "PRIOR_FALLBACK"
    UNCALIBRATED = "UNCALIBRATED"
    DEFINITIONAL = "DEFINITIONAL"


@unique
class ActionAvailability(StrEnum):
    """Whether an action can actually be executed against the provider."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class IntentState(StrEnum):
    """The four states of an execution intent.

    ``UNCERTAIN`` means the provider was called and the outcome is unknown. While
    an intent sits there, no further external call is issued for that case until
    reconciliation resolves it. That fails safe, but it fails silently, so the
    count of records in this state needs an alarm.
    """

    ATTEMPTED = "ATTEMPTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@unique
class OutcomeClass(StrEnum):
    """The three kinds of recovery, which license three different claims.

    NATURAL — the money arrived without us.
    OBSERVED — we acted and the money arrived. Not "we caused it".
    ATTRIBUTED — a controlled comparison supports the causal claim.
    """

    NATURAL = "NATURAL"
    OBSERVED = "OBSERVED"
    ATTRIBUTED = "ATTRIBUTED"


@unique
class Provenance(StrEnum):
    """Whether a figure came from real traffic or generated data.

    SYNTHETIC propagates to every presentation surface and every export. A figure
    is only REAL when every observation behind it is real.
    """

    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"


@unique
class InterventionStatus(StrEnum):
    """Whether an observation is usable as a baseline training label.

    Only ``NO_INTERVENTION_CONFIRMED`` is usable, and even that means "no Revora
    action and no *recorded* merchant action" — Revora cannot see a merchant
    phoning a customer. The weakness is labelled rather than solved.
    """

    NO_INTERVENTION_CONFIRMED = "NO_INTERVENTION_CONFIRMED"
    REVORA_INTERVENED = "REVORA_INTERVENED"
    MERCHANT_INTERVENTION_UNKNOWN = "MERCHANT_INTERVENTION_UNKNOWN"


@unique
class DecisionSource(StrEnum):
    """Who or what chose the action on a finished case.

    Retained so that a model trained on this data cannot silently reproduce past
    human choices without the skew being visible.
    """

    AUTOMATED = "AUTOMATED"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
    BASELINE_WORKFLOW = "BASELINE_WORKFLOW"


@unique
class ValidationStatus(StrEnum):
    """How much an estimate has been checked against observed outcomes."""

    VALIDATED = "VALIDATED"
    UNVALIDATED_BASELINE = "UNVALIDATED_BASELINE"
    CALIBRATION_SUSPECT = "CALIBRATION_SUSPECT"
    CALIBRATION_UNVERIFIED = "CALIBRATION_UNVERIFIED"


@unique
class FieldKind(StrEnum):
    """What sort of sensitive value a field holds, for the masking serializer.

    ``PROVIDER_SHORT_URL`` is sensitive because a payment link is a bearer
    capability — anyone holding the URL can pay. It is shown in the dashboard but
    must never reach a log line or an audit record unmasked.
    """

    CONTACT = "CONTACT"
    INSTRUMENT = "INSTRUMENT"
    PROVIDER_SHORT_URL = "PROVIDER_SHORT_URL"
    NON_SENSITIVE = "NON_SENSITIVE"


SENSITIVE_FIELD_KINDS: frozenset[FieldKind] = frozenset(
    {FieldKind.CONTACT, FieldKind.INSTRUMENT, FieldKind.PROVIDER_SHORT_URL}
)
"""Kinds the masking serializer must never write in clear."""


@unique
class DetectionVerdict(StrEnum):
    """Exactly one of these is recorded per persisted event.

    ``DEFERRED_TRIGGER`` covers checkout abandonment, missed promise-to-pay and
    payment-window expiry: modelled in the event schema, out of scope as triggers,
    so the event is retained and visible but no case opens.
    """

    AT_RISK = "AT_RISK"
    NOT_AT_RISK = "NOT_AT_RISK"
    DEFERRED_TRIGGER = "DEFERRED_TRIGGER"


@unique
class SelectionReason(StrEnum):
    """Why the optimizer chose what it chose.

    The two null-action reasons matter as much as the positive one: a merchant who
    cannot see why Revora did nothing will assume it is broken.
    """

    HIGHEST_NET_VALUE = "HIGHEST_NET_VALUE"
    NO_POSITIVE_VALUE = "NO_POSITIVE_VALUE"
    HIGH_BASELINE_NO_INTERVENTION = "HIGH_BASELINE_NO_INTERVENTION"


@unique
class ExclusionReason(StrEnum):
    """Why a candidate action was excluded from selection."""

    CAUSE_NOT_ELIGIBLE = "CAUSE_NOT_ELIGIBLE"
    PROVIDER_CAPABILITY_UNVERIFIED = "PROVIDER_CAPABILITY_UNVERIFIED"
    NON_POSITIVE_INCREMENTAL_VALUE = "NON_POSITIVE_INCREMENTAL_VALUE"
    COST_RATIO_EXCEEDED = "COST_RATIO_EXCEEDED"
    INVALID_ESTIMATE_INPUT = "INVALID_ESTIMATE_INPUT"
    BELOW_NET_VALUE_THRESHOLD = "BELOW_NET_VALUE_THRESHOLD"
    BELOW_INCREMENTAL_PROBABILITY = "BELOW_INCREMENTAL_PROBABILITY"


DIVERGENCE_HIGHER_PROBABILITY_LOWER_NET_VALUE = "HIGHER_PROBABILITY_LOWER_NET_VALUE"
"""Recorded when the highest-probability candidate is not the selected one. The
dashboard shows both, because that divergence is the product's whole argument."""


@unique
class TerminalReason(StrEnum):
    """Why a case ended. Drives the unresolved-revenue grouping on the dashboard."""

    RECOVERED_VERIFIED = "RECOVERED_VERIFIED"
    RECOVERY_WINDOW_ELAPSED = "RECOVERY_WINDOW_ELAPSED"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    DECISION_CYCLE_LIMIT_REACHED = "DECISION_CYCLE_LIMIT_REACHED"
    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    ALREADY_PAID = "ALREADY_PAID"
    FRAUD_OR_RISK_FLAG = "FRAUD_OR_RISK_FLAG"
    PAYMENT_STATE_UNVERIFIABLE = "PAYMENT_STATE_UNVERIFIABLE"
    EXECUTION_RESULT_UNVERIFIABLE = "EXECUTION_RESULT_UNVERIFIABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    HUMAN_OWNERSHIP = "HUMAN_OWNERSHIP"
    COMMUNICATION_FAILED = "COMMUNICATION_FAILED"


@unique
class ExperimentGroup(StrEnum):
    """Which arm a case was assigned to, before any diagnosis ran."""

    CONTROL = "CONTROL"
    TREATMENT = "TREATMENT"


@unique
class ExperimentState(StrEnum):
    """An experiment's lifecycle."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


@unique
class ExperimentLabel(StrEnum):
    """Labels that disqualify a result from supporting a causal claim.

    Any of these present means no attributed recovery from that experiment.
    """

    UNDERPOWERED = "UNDERPOWERED"
    INVALIDATED = "INVALIDATED"
    SYNTHETIC = "SYNTHETIC"
    CONTAMINATED = "CONTAMINATED"
    EXPLORATORY = "EXPLORATORY"
    CAUSALITY_NOT_ESTABLISHED = "CAUSALITY_NOT_ESTABLISHED"


NOT_ESTABLISHED = "NOT_ESTABLISHED"
"""Reported for incremental recovered revenue when no adequate experiment
supports a number. Deliberately not zero — "we have not measured this" and "we
measured this and it was nothing" are different statements."""

RECOVERY_GROSS_OF_REFUNDS = "RECOVERY_GROSS_OF_REFUNDS"
"""Label on MVP recovery figures. Refunds are captured on every authoritative
read but not yet netted out of reported recovery."""

"""The ~50 tunable bounds, as database rows with a version, not as environment.

Every bound in Revora is configuration. None of them is a constant in code, and
none of them is an environment variable. The reason is R15.C6: a change to a policy
bound must be recorded with an approving user, and a redeploy cannot name a person.
An environment variable also cannot be versioned in a way a stored decision can
point back at, so "why did this case stop after three attempts" would become
unanswerable the first time the bound changed.

What environment *does* hold is the connection string, secret references and the
process role. That is the whole list.

**Every default here is an assumption placeholder.** They were chosen to make the
requirements testable, not because anything measured them. Real calibration is
impossible until merchant data exists. Each row carries ``is_assumption`` so the
dashboard can say so, and every entry below repeats it in a comment, because a
number in a table looks measured whether or not it is.

Three of the values differ from the requirements table, and deliberately:

* ``INGEST_ACK_TIMEOUT`` is 1500 ms, not 3000. The provider's own webhook deadline
  is five seconds, and a 3000 ms budget leaves too little room for the response to
  cross the network before the provider gives up and retries.
* ``MAX_MESSAGE_LENGTH`` is 300 characters, not 480. The provider's payment-link
  ``description`` field is the channel for customer-visible text, and it does not
  accept 480.
* ``RISK_REASON_CODES`` is a configured set rather than a hard-coded condition,
  and it contains ``payment_risk_check_failed`` and ``compliance_violation``. The
  fraud-or-risk diagnosis derives from membership in this set, so extending it does
  not require a release.

The accessor is a frozen dataclass with one typed attribute per bound, so a caller
writes ``config.MAX_RECOVERY_ATTEMPTS`` and gets an ``int``. A string-keyed lookup
would type-check everywhere and fail at the one call site that misspelt a key.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final

from revora.domain.money import Minor

__all__ = [
    "CATALOGUE",
    "DEFAULTS_MERCHANT_ID",
    "DEFAULT_CONFIG_VERSION",
    "Configuration",
    "ConfigurationBound",
    "ConfigurationError",
    "ValueKind",
    "default_configuration",
    "parse_value",
    "seed_rows",
]

DEFAULTS_MERCHANT_ID: Final[uuid.UUID] = uuid.UUID("00000000-0000-0000-0000-000000000000")
"""The sentinel tenant the seeded defaults belong to.

``app_config.merchant_id`` is ``NOT NULL`` like every other tenant-scoped column,
and the seed migration has no real merchant to attach defaults to. Making the
column nullable for this one case would put a hole in the column that row-level
security and every repository filter depend on, so the defaults get a tenant of
their own instead. A merchant's own row overrides the sentinel's; the sentinel is
the fallback, never the answer when a real row exists.
"""

DEFAULT_CONFIG_VERSION: Final[str] = "2025.01.0-assumption-baseline"
"""The version label of the seeded defaults. Named to state what it is: a baseline
of assumptions, not a calibrated configuration."""


class ConfigurationError(RuntimeError):
    """A configuration value could not be parsed, or a required bound is missing.

    Raised rather than defaulted. A bound that silently falls back to its
    placeholder is how a merchant's deliberate limit of one message per case turns
    back into two without anyone being told.
    """


@unique
class ValueKind(StrEnum):
    """How a stored ``TEXT`` value is parsed.

    Six kinds rather than a column per type. ``DURATION_SECONDS`` accepts a decimal
    so a sub-second bound like the ingest acknowledgement budget can be expressed
    without a second unit.
    """

    INTEGER = "INTEGER"
    DURATION_SECONDS = "DURATION_SECONDS"
    DECIMAL = "DECIMAL"
    MONEY_MINOR = "MONEY_MINOR"
    STRING = "STRING"
    STRING_SET = "STRING_SET"


@dataclass(frozen=True, slots=True)
class ConfigurationBound:
    """One bound's declaration: its kind, its placeholder default, and its purpose."""

    key: str
    kind: ValueKind
    default: str
    purpose: str
    is_assumption: bool = True
    note: str | None = None
    """Set where the value departs from the requirements table, naming why."""


def parse_value(kind: ValueKind, raw: str) -> object:
    """Parse a stored value according to its declared kind.

    Raises:
        ConfigurationError: on anything unparseable. A malformed bound is a
            deployment failure, not a reason to guess.
    """
    text = raw.strip()
    try:
        match kind:
            case ValueKind.INTEGER:
                return int(text)
            case ValueKind.DURATION_SECONDS:
                # Via microseconds so the conversion never passes through a
                # binary floating-point value. A sub-second bound like the ingest
                # acknowledgement budget is expressed as "1.5" and lands exactly.
                return timedelta(microseconds=int(Decimal(text) * 1_000_000))
            case ValueKind.DECIMAL:
                return Decimal(text)
            case ValueKind.MONEY_MINOR:
                return Minor(int(text))
            case ValueKind.STRING:
                return text
            case ValueKind.STRING_SET:
                return frozenset(part.strip() for part in text.split(",") if part.strip())
    except (ValueError, ArithmeticError, InvalidOperation) as exc:
        raise ConfigurationError(f"value {raw!r} is not a valid {kind.value}") from exc
    raise ConfigurationError(f"unknown value kind {kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# The catalogue. Every default is an [ASSUMPTION] placeholder.
# ---------------------------------------------------------------------------

_BOUNDS: tuple[ConfigurationBound, ...] = (
    # --- Case bounds. These are what actually protect a customer from being
    # contacted repeatedly, and they are the ones a merchant is most likely to
    # want to change. [ASSUMPTION] defaults.
    ConfigurationBound(
        "MAX_RECOVERY_ATTEMPTS", ValueKind.INTEGER, "3",
        "Upper limit on outbound actions per recovery case",
    ),
    ConfigurationBound(
        "MAX_CUSTOMER_MESSAGES", ValueKind.INTEGER, "2",
        "Upper limit on customer-visible communications per recovery case",
    ),
    ConfigurationBound(
        "RECOVERY_WINDOW_DURATION", ValueKind.DURATION_SECONDS, "604800",
        "Maximum recovery case lifetime before EXPIRED (168 hours)",
    ),
    ConfigurationBound(
        "COOLDOWN_INTERVAL", ValueKind.DURATION_SECONDS, "86400",
        "Minimum gap between outbound actions (24 hours)",
    ),
    # --- Value model thresholds. [ASSUMPTION] defaults; these decide whether
    # acting is worth it at all, so a wrong value here shows up as either
    # inaction or unjustified spend.
    ConfigurationBound(
        "MIN_NET_VALUE_THRESHOLD", ValueKind.MONEY_MINOR, "5000",
        "Minimum net recovery value required to justify action",
    ),
    ConfigurationBound(
        "MIN_INCREMENTAL_PROBABILITY", ValueKind.DECIMAL, "0.05",
        "Minimum incremental probability required to justify action",
    ),
    ConfigurationBound(
        "MAX_COST_TO_VALUE_RATIO", ValueKind.DECIMAL, "0.30",
        "Maximum total cost divided by expected incremental revenue",
    ),
    ConfigurationBound(
        "HIGH_BASELINE_THRESHOLD", ValueKind.DECIMAL, "0.80",
        "Baseline probability above which DO_NOTHING is preferred",
    ),
    # --- Reasoning layer. [ASSUMPTION] defaults.
    ConfigurationBound(
        "REASONING_TIMEOUT", ValueKind.DURATION_SECONDS, "10",
        "Maximum wait for a reasoning-layer response",
    ),
    ConfigurationBound(
        "REASONING_RETRY_COUNT", ValueKind.INTEGER, "1",
        "Additional reasoning-layer requests permitted per processing step",
    ),
    ConfigurationBound(
        "REASONING_UNAVAILABLE_THRESHOLD", ValueKind.INTEGER, "3",
        "Consecutive failures after which the reasoning layer is marked unavailable",
    ),
    ConfigurationBound(
        "AI_RAW_CAPTURE_LIMIT", ValueKind.INTEGER, "8000",
        "Maximum retained length of a rejected reasoning-layer response",
    ),
    ConfigurationBound(
        "DIAGNOSIS_CONFIDENCE_FLOOR", ValueKind.DECIMAL, "0.60",
        "Recorded confidence below which the risk cause is treated as UNKNOWN",
    ),
    # --- Ingestion. [ASSUMPTION] defaults.
    ConfigurationBound(
        "INGEST_ACK_TIMEOUT", ValueKind.DURATION_SECONDS, "1.5",
        "Maximum event-ingestion acknowledgement latency",
        note=(
            "[ASSUMPTION] 1500 ms, amended down from the requirements table's 3000 ms. "
            "The provider's own webhook deadline is 5 seconds and a 3000 ms internal "
            "budget leaves too little margin for the response to arrive before a retry."
        ),
    ),
    ConfigurationBound(
        "MAX_INBOUND_PAYLOAD_SIZE", ValueKind.INTEGER, "1048576",
        "Maximum accepted inbound payload size in bytes (1 MB)",
    ),
    ConfigurationBound(
        "EVENT_DEDUP_RETENTION", ValueKind.DURATION_SECONDS, "2592000",
        "Retention of provider_event_id records used for duplicate detection (30 days)",
    ),
    ConfigurationBound(
        "QUARANTINE_RETENTION", ValueKind.DURATION_SECONDS, "2592000",
        "Retention of quarantined malformed events (30 days)",
    ),
    ConfigurationBound(
        "INGEST_RATE_LIMIT", ValueKind.INTEGER, "600",
        "Maximum accepted ingestion requests per minute per source identifier",
    ),
    # --- Detection and lifecycle. [ASSUMPTION] defaults.
    ConfigurationBound(
        "MIN_DETECTION_AMOUNT", ValueKind.MONEY_MINOR, "100",
        "Minimum payment amount required for revenue-at-risk classification",
    ),
    ConfigurationBound(
        "DETECTION_LATENCY_BOUND", ValueKind.DURATION_SECONDS, "60",
        "Maximum interval from event persistence to a recorded detection verdict",
    ),
    ConfigurationBound(
        "LIFECYCLE_EVALUATION_INTERVAL", ValueKind.DURATION_SECONDS, "300",
        "Maximum interval between lifecycle evaluations of a non-terminal case",
    ),
    ConfigurationBound(
        "ESCALATION_AMOUNT_THRESHOLD", ValueKind.MONEY_MINOR, "50000",
        "Amount at or above which attempt exhaustion escalates rather than stops",
    ),
    ConfigurationBound(
        "OUTCOME_WAIT_TIMEOUT", ValueKind.DURATION_SECONDS, "259200",
        "Maximum wait for an outcome after an executed action (72 hours)",
    ),
    # --- Policy and execution. [ASSUMPTION] defaults.
    ConfigurationBound(
        "POLICY_DECISION_VALIDITY", ValueKind.DURATION_SECONDS, "900",
        "Maximum age of an APPROVED policy decision accepted by execution (15 minutes)",
    ),
    ConfigurationBound(
        "PROVIDER_CALL_TIMEOUT", ValueKind.DURATION_SECONDS, "15",
        "Maximum wait for a provider response before the result is UNCERTAIN",
    ),
    ConfigurationBound(
        "EXECUTION_LOCK_LEASE", ValueKind.DURATION_SECONDS, "60",
        "Maximum lease duration of the per-case execution lock",
    ),
    ConfigurationBound(
        "EXECUTION_RECONCILIATION_INTERVAL", ValueKind.DURATION_SECONDS, "300",
        "Maximum interval between reconciliation attempts on an unresolved intent",
    ),
    ConfigurationBound(
        "MAX_EXECUTION_RECONCILIATION_ATTEMPTS", ValueKind.INTEGER, "6",
        "Reconciliation attempt bound before EXECUTION_RESULT_UNVERIFIABLE escalation",
    ),
    ConfigurationBound(
        "MAX_MESSAGE_LENGTH", ValueKind.INTEGER, "300",
        "Maximum length of customer-visible message content",
        note=(
            "[ASSUMPTION] 300 characters, amended down from the requirements table's 480. "
            "Customer-visible text travels in the provider's payment-link description "
            "field, which does not accept 480."
        ),
    ),
    ConfigurationBound(
        "RISK_REASON_CODES", ValueKind.STRING_SET,
        "payment_risk_check_failed,compliance_violation",
        "Provider reason codes from which the fraud-or-risk condition derives",
        note=(
            "[ASSUMPTION] Configured as a set rather than a hard-coded condition so a "
            "new provider reason code can be treated as a risk signal without a release."
        ),
    ),
    # --- Outcome verification. [ASSUMPTION] defaults.
    ConfigurationBound(
        "OUTCOME_READ_LATENCY_BOUND", ValueKind.DURATION_SECONDS, "60",
        "Maximum interval from a payment-success event to an authoritative read",
    ),
    ConfigurationBound(
        "PAYMENT_STATE_RECONCILIATION_INTERVAL", ValueKind.DURATION_SECONDS, "900",
        "Interval between authoritative reads while payment-state signals conflict",
    ),
    ConfigurationBound(
        "MAX_PAYMENT_STATE_READ_ATTEMPTS", ValueKind.INTEGER, "5",
        "Read attempts before PAYMENT_STATE_UNVERIFIABLE escalation",
    ),
    # --- Estimation and calibration. [ASSUMPTION] defaults.
    ConfigurationBound(
        "BASELINE_ESTIMATION_TIMEOUT", ValueKind.DURATION_SECONDS, "2",
        "Maximum wait for a baseline recovery probability (2000 ms)",
    ),
    ConfigurationBound(
        "MIN_SEGMENT_SAMPLE_SIZE", ValueKind.INTEGER, "30",
        "Minimum NO_INTERVENTION_CONFIRMED observations per feature segment",
    ),
    ConfigurationBound(
        "CALIBRATION_REPORT_CASE_TRIGGER", ValueKind.INTEGER, "100",
        "Resolved control-group case count triggering a calibration report",
    ),
    ConfigurationBound(
        "CALIBRATION_REPORT_TIME_TRIGGER", ValueKind.DURATION_SECONDS, "604800",
        "Elapsed time since the previous calibration report triggering a new one (7 days)",
    ),
    ConfigurationBound(
        "MIN_CALIBRATION_BAND_COUNT", ValueKind.INTEGER, "20",
        "Minimum control-group observations for a calibration band to be validated",
    ),
    ConfigurationBound(
        "MAX_UNKNOWN_INTERVENTION_SHARE", ValueKind.DECIMAL, "0.20",
        "Maximum MERCHANT_INTERVENTION_UNKNOWN share before a segment is a bias risk",
    ),
    ConfigurationBound(
        "CALIBRATION_TOLERANCE", ValueKind.DECIMAL, "0.10",
        "Maximum permitted deviation between predicted and observed recovery rate",
    ),
    # --- Experiments. [ASSUMPTION] defaults.
    ConfigurationBound(
        "EXPERIMENT_ALLOCATION_RATIO", ValueKind.STRING, "1:1",
        "Control to treatment assignment ratio",
    ),
    ConfigurationBound(
        "EXPERIMENT_SIGNIFICANCE_LEVEL", ValueKind.DECIMAL, "0.05",
        "Significance level used in the required sample size computation",
    ),
    ConfigurationBound(
        "EXPERIMENT_POWER", ValueKind.DECIMAL, "0.80",
        "Statistical power used in the required sample size computation",
    ),
    ConfigurationBound(
        "EXPERIMENT_CONFIDENCE_LEVEL", ValueKind.DECIMAL, "0.95",
        "Confidence level of a reported incremental lift interval (two-sided)",
    ),
    # --- Persistence and audit. [ASSUMPTION] defaults.
    ConfigurationBound(
        "PERSISTENCE_TIMEOUT", ValueKind.DURATION_SECONDS, "2",
        "Maximum wait for a persistence operation before it is treated as failed",
    ),
    ConfigurationBound(
        "AUDIT_WRITE_TIMEOUT", ValueKind.DURATION_SECONDS, "2",
        "Maximum wait for an audit record write to reach durable persistence",
    ),
    ConfigurationBound(
        "MAX_AUDIT_FIELD_LENGTH", ValueKind.INTEGER, "8000",
        "Maximum retained length of a single audit record field value",
    ),
    ConfigurationBound(
        "AUDIT_RETENTION_PERIOD", ValueKind.DURATION_SECONDS, "63072000",
        "Minimum retention period of a persisted audit record (24 months)",
    ),
    # --- Security, privacy and presentation. [ASSUMPTION] defaults.
    ConfigurationBound(
        "MASK_DISCLOSURE_LENGTH", ValueKind.INTEGER, "4",
        "Maximum characters of a contact identifier retained in clear form",
    ),
    ConfigurationBound(
        "CUSTOMER_DATA_RETENTION", ValueKind.DURATION_SECONDS, "15552000",
        "Retention period of stored customer contact data (180 days)",
    ),
    ConfigurationBound(
        "SESSION_LIFETIME", ValueKind.DURATION_SECONDS, "43200",
        "Maximum age of an authenticated merchant-user session (12 hours)",
    ),
    ConfigurationBound(
        "DASHBOARD_PAGE_SIZE", ValueKind.INTEGER, "100",
        "Maximum recovery cases presented per list page",
    ),
    ConfigurationBound(
        "DASHBOARD_METRICS_TIMEOUT", ValueKind.DURATION_SECONDS, "5",
        "Maximum wait for a metrics figure before a data-unavailable indication",
    ),
)

CATALOGUE: Mapping[str, ConfigurationBound] = MappingProxyType(
    {bound.key: bound for bound in _BOUNDS}
)
"""Every bound, by key. Read-only — a component must not add one at runtime."""


# ---------------------------------------------------------------------------
# The typed accessor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Configuration:
    """Every bound, typed, plus the version the values came from.

    One attribute per bound so a caller writes ``config.MAX_RECOVERY_ATTEMPTS``.
    ``version`` is copied onto every policy decision, which is what makes a past
    decision explicable after a bound has changed.
    """

    version: str

    MAX_RECOVERY_ATTEMPTS: int
    MAX_CUSTOMER_MESSAGES: int
    RECOVERY_WINDOW_DURATION: timedelta
    COOLDOWN_INTERVAL: timedelta

    MIN_NET_VALUE_THRESHOLD: Minor
    MIN_INCREMENTAL_PROBABILITY: Decimal
    MAX_COST_TO_VALUE_RATIO: Decimal
    HIGH_BASELINE_THRESHOLD: Decimal

    REASONING_TIMEOUT: timedelta
    REASONING_RETRY_COUNT: int
    REASONING_UNAVAILABLE_THRESHOLD: int
    AI_RAW_CAPTURE_LIMIT: int
    DIAGNOSIS_CONFIDENCE_FLOOR: Decimal

    INGEST_ACK_TIMEOUT: timedelta
    MAX_INBOUND_PAYLOAD_SIZE: int
    EVENT_DEDUP_RETENTION: timedelta
    QUARANTINE_RETENTION: timedelta
    INGEST_RATE_LIMIT: int

    MIN_DETECTION_AMOUNT: Minor
    DETECTION_LATENCY_BOUND: timedelta
    LIFECYCLE_EVALUATION_INTERVAL: timedelta
    ESCALATION_AMOUNT_THRESHOLD: Minor
    OUTCOME_WAIT_TIMEOUT: timedelta

    POLICY_DECISION_VALIDITY: timedelta
    PROVIDER_CALL_TIMEOUT: timedelta
    EXECUTION_LOCK_LEASE: timedelta
    EXECUTION_RECONCILIATION_INTERVAL: timedelta
    MAX_EXECUTION_RECONCILIATION_ATTEMPTS: int
    MAX_MESSAGE_LENGTH: int
    RISK_REASON_CODES: frozenset[str]

    OUTCOME_READ_LATENCY_BOUND: timedelta
    PAYMENT_STATE_RECONCILIATION_INTERVAL: timedelta
    MAX_PAYMENT_STATE_READ_ATTEMPTS: int

    BASELINE_ESTIMATION_TIMEOUT: timedelta
    MIN_SEGMENT_SAMPLE_SIZE: int
    CALIBRATION_REPORT_CASE_TRIGGER: int
    CALIBRATION_REPORT_TIME_TRIGGER: timedelta
    MIN_CALIBRATION_BAND_COUNT: int
    MAX_UNKNOWN_INTERVENTION_SHARE: Decimal
    CALIBRATION_TOLERANCE: Decimal

    EXPERIMENT_ALLOCATION_RATIO: str
    EXPERIMENT_SIGNIFICANCE_LEVEL: Decimal
    EXPERIMENT_POWER: Decimal
    EXPERIMENT_CONFIDENCE_LEVEL: Decimal

    PERSISTENCE_TIMEOUT: timedelta
    AUDIT_WRITE_TIMEOUT: timedelta
    MAX_AUDIT_FIELD_LENGTH: int
    AUDIT_RETENTION_PERIOD: timedelta

    MASK_DISCLOSURE_LENGTH: int
    CUSTOMER_DATA_RETENTION: timedelta
    SESSION_LIFETIME: timedelta
    DASHBOARD_PAGE_SIZE: int
    DASHBOARD_METRICS_TIMEOUT: timedelta

    defaulted_keys: frozenset[str] = field(default_factory=frozenset)
    """Bounds that had no row and fell back to the placeholder. Non-empty is a
    deployment smell: it means the seed migration did not run, or a key was
    renamed in code without a migration."""

    unrecognized_keys: frozenset[str] = field(default_factory=frozenset)
    """Rows whose key is not in the catalogue. Kept rather than rejected, because a
    retired bound left behind in a table should not stop the process from serving."""

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, str],
        *,
        version: str,
        strict: bool = False,
    ) -> Configuration:
        """Build from raw ``app_config`` values.

        Args:
            values: key to raw stored value.
            version: the configuration version these rows came from.
            strict: refuse rather than default when a bound is missing. The API and
                worker bootstrap pass ``True``; a test building a partial
                configuration passes ``False``.

        Raises:
            ConfigurationError: on an unparseable value, or on a missing bound when
                ``strict``.
        """
        parsed: dict[str, object] = {}
        defaulted: set[str] = set()
        for key, bound in CATALOGUE.items():
            raw = values.get(key)
            if raw is None:
                if strict:
                    raise ConfigurationError(f"configuration bound {key} has no row")
                defaulted.add(key)
                raw = bound.default
            parsed[key] = parse_value(bound.kind, raw)
        unrecognized = frozenset(values) - frozenset(CATALOGUE)
        return cls(
            version=version,
            defaulted_keys=frozenset(defaulted),
            unrecognized_keys=unrecognized,
            **parsed,  # type: ignore[arg-type]
        )

    def as_raw(self) -> Mapping[str, str]:
        """The values as they would be stored. Used by the seed and by round-trip tests."""
        return MappingProxyType({key: bound.default for key, bound in CATALOGUE.items()})


def default_configuration() -> Configuration:
    """The placeholder configuration, with nothing read from a database.

    For the pure and model test tiers, and for the one legitimate production use:
    reporting what a bound *would* be before a merchant has any rows of its own.
    """
    return Configuration.from_values(
        {key: bound.default for key, bound in CATALOGUE.items()},
        version=DEFAULT_CONFIG_VERSION,
    )


def seed_rows() -> Iterable[dict[str, object]]:
    """The rows the seed migration inserts, one per bound.

    Generated from the catalogue rather than written out again in SQL, so the
    accessor and the seeded rows cannot disagree about a default. ``is_assumption``
    is true on every one of them, because it is.
    """
    for bound in _BOUNDS:
        yield {
            "key": bound.key,
            "value": bound.default,
            "value_kind": bound.kind.value,
            "config_version": DEFAULT_CONFIG_VERSION,
            "is_assumption": bound.is_assumption,
            "purpose": bound.purpose,
            "note": bound.note,
        }


def _field_names() -> frozenset[str]:
    return frozenset(
        f.name
        for f in fields(Configuration)
        if f.name not in {"version", "defaulted_keys", "unrecognized_keys"}
    )


# The accessor and the catalogue are two lists of the same thing, so they are
# checked against each other at import. A bound added to one and not the other is
# a TypeError at construction time otherwise, and the message would name a keyword
# argument rather than the mistake.
_missing_fields = frozenset(CATALOGUE) - _field_names()
_extra_fields = _field_names() - frozenset(CATALOGUE)
if _missing_fields or _extra_fields:  # pragma: no cover - import-time invariant
    raise ConfigurationError(
        "Configuration accessor and CATALOGUE disagree; "
        f"missing attributes: {sorted(_missing_fields)}, "
        f"attributes with no bound: {sorted(_extra_fields)}"
    )

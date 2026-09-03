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

Two of the bounds are cost priors rather than limits — ``PAYMENT_LINK_FINANCIAL_COST``
and ``MESSAGE_COMMUNICATION_COST``, which R31.C11 requires to be versioned rows on the
same terms as everything else here. They live in this catalogue and
``revora.estimation.candidates`` reads their defaults back out through
:func:`money_default`, so the two numbers are written down exactly once.

The accessor is a frozen dataclass with one typed attribute per bound, so a caller
writes ``config.MAX_RECOVERY_ATTEMPTS`` and gets an ``int``. A string-keyed lookup
would type-check everywhere and fail at the one call site that misspelt a key.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final

from revora.domain.money import Minor

__all__ = [
    "CATALOGUE",
    "COST_SPLIT_BOUND_KEYS",
    "CUSTOMER_STATED_CAUSE_BOUND_KEYS",
    "CUSTOMER_SURFACE_BOUND_KEYS",
    "CUSTOMER_TOKEN_BOUND_KEYS",
    "DEFAULTS_MERCHANT_ID",
    "DEFAULT_CONFIG_VERSION",
    "REVIEW_LOOP_BOUND_KEYS",
    "SWEEP_INTERVAL_BOUND_KEYS",
    "Configuration",
    "ConfigurationBound",
    "ConfigurationError",
    "ValueKind",
    "default_configuration",
    "money_default",
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
    # --- The review loop of R30. These two are the only new bounds it needs, and they
    # are bounds rather than constants for the ordinary reason: how long a merchant is
    # willing to leave a case waiting before looking at it again is that merchant's
    # judgement about their own customers, and R15.C6 means changing it is a recorded
    # decision with a person's name on it. [ASSUMPTION] defaults on both.
    ConfigurationBound(
        "WAIT_REVIEW_INTERVAL", ValueKind.DURATION_SECONDS, "43200",
        "Interval after a null-action selection before the case is reviewed (12 hours)",
    ),
    ConfigurationBound(
        "REVIEW_SWEEP_INTERVAL", ValueKind.DURATION_SECONDS, "300",
        "Maximum interval between review sweeps over cases whose review is due (5 minutes)",
    ),
    # --- The Customer_Access_Token of R18. Both are bounds rather than constants for the
    # ordinary reason, and both are the kind of bound a merchant has an opinion about: how
    # long a payment link's companion page stays reachable, and how many times one customer
    # may say something about one payment. Neither is a check constraint —
    # ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` deliberately so, because encoding today's 5 in the
    # schema would make raising it a migration; the durable enforcement is the comparison
    # inside ``increment_accepted_submissions``' own ``UPDATE`` statement.
    # [ASSUMPTION] defaults on both.
    ConfigurationBound(
        "CUSTOMER_TOKEN_LIFETIME", ValueKind.DURATION_SECONDS, "259200",
        "Maximum age of a customer access token before it stops being served (72 hours)",
        note=(
            "[ASSUMPTION] 72 hours, and deliberately shorter than RECOVERY_WINDOW_DURATION. "
            "R18.C2 makes the expiry the *earlier* of this and the case's window end, so a "
            "value at or above the window duration would make this bound inert and leave the "
            "window as the only thing bounding a bearer credential's life."
        ),
    ),
    ConfigurationBound(
        "CUSTOMER_TOKEN_MAX_SUBMISSIONS", ValueKind.INTEGER, "5",
        "Accepted customer signal writes permitted on one customer access token",
    ),
    # --- The public customer surface of R19 and R29. Four bounds, and the pairing is
    # the thing to read: the two *rate* limits guard the read path and are enforced by a
    # process-local fixed-window counter, so they are coarse by construction (see
    # ``revora.platform.ratelimit``); the two *cap* bounds guard the write path and are
    # enforced durably, one inside the conditional UPDATE that increments the token's
    # counter and one against a count under that token's row lock. Putting them in one
    # block invites the comparison, because treating a rate limit as a correctness bound is
    # the mistake this arrangement exists to prevent.
    # [ASSUMPTION] defaults on all four.
    ConfigurationBound(
        "CUSTOMER_PAGE_RATE_LIMIT", ValueKind.INTEGER, "30",
        "Accepted customer response page requests per minute per customer access token",
        note=(
            "[ASSUMPTION] 30 per minute per token. A coarse flood guard, not a quota: the "
            "counter is per process, so two API replicas admit twice this. The durable bound "
            "on the write path is CUSTOMER_TOKEN_MAX_SUBMISSIONS, which no replica count can "
            "exceed."
        ),
    ),
    ConfigurationBound(
        "CUSTOMER_PAGE_SOURCE_RATE_LIMIT", ValueKind.INTEGER, "120",
        "Accepted customer response page requests per minute per source identifier",
        note=(
            "[ASSUMPTION] 120 per minute per source, four times the per-token rate. Higher "
            "because a source identifier is shared: everyone behind one NAT presents the same "
            "one, so a per-source rate equal to the per-token rate would refuse the second "
            "customer on a mobile network rather than the attacker."
        ),
    ),
    ConfigurationBound(
        "MAX_CUSTOMER_SIGNALS_PER_CASE", ValueKind.INTEGER, "5",
        "Customer signals recorded per recovery case",
    ),
    ConfigurationBound(
        "DELAY_NOTE_MAX_LENGTH", ValueKind.INTEGER, "500",
        "Retained length of a delay-reason note, in characters",
        note=(
            "[ASSUMPTION] 500 characters, and the same number is a CHECK constraint on "
            "customer_signal.delay_reason_note. The column is the backstop and this row is the "
            "authority: lowering the bound takes effect on the next write, while raising it "
            "above the column's 500 cannot, so the writer truncates to the smaller of the two "
            "rather than attempting a row the schema would refuse."
        ),
    ),
    # --- The one bound the Delay_Reason cause refinement of R20.C4 introduces. It sits
    # here rather than beside DIAGNOSIS_CONFIDENCE_FLOOR because it is a customer-surface
    # figure, and the pairing with the four above is the thing to read: those four bound
    # what a customer may submit, this one bounds how much weight what they submitted
    # carries once it reaches a decision. [ASSUMPTION] default.
    ConfigurationBound(
        "CUSTOMER_STATED_CAUSE_CONFIDENCE", ValueKind.DECIMAL, "0.90",
        "Confidence recorded on a diagnosis derived from a customer-stated delay reason",
        note=(
            "[ASSUMPTION] 0.90, and nothing has measured whether a stated reason "
            "correlates with the recovery outcome at all. Below 1.0 because a customer's "
            "account of their own finances is not the provider's own error field, and "
            "1.0 is reserved for DETERMINISTIC causes read off the provider (R3.C10). "
            "At or above DIAGNOSIS_CONFIDENCE_FLOOR because a value below it would be "
            "substituted to UNKNOWN under R3.C8 and the whole capture would be inert "
            "(R20.C7)."
        ),
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
    # --- Cost priors for the four-term cost decomposition (R31.C11). These two are
    # bounds for the same reason every other value here is: a provider fee and a
    # per-message delivery cost are numbers a merchant renegotiates, and a
    # renegotiation is a recorded decision with a person's name on it, not a redeploy.
    # They are the only two of the six cost priors in
    # ``revora.estimation.candidates`` that R31.C11 names, and the remaining four stay
    # in that module: the promise follow-up's financial term is a *verified* zero
    # rather than a tunable figure, and nothing has asked for the other three to be
    # changeable. ``COST_PRIORS`` reads its two values from the defaults declared
    # here, so the number is written down once and the accessor, the seeded row and
    # the prior table cannot disagree. [ASSUMPTION] defaults.
    ConfigurationBound(
        "PAYMENT_LINK_FINANCIAL_COST", ValueKind.MONEY_MINOR, "300",
        "Provider fee attributable to creating one payment link, in minor units",
    ),
    ConfigurationBound(
        "MESSAGE_COMMUNICATION_COST", ValueKind.MONEY_MINOR, "25",
        "Per-message delivery cost of one customer-visible action, in minor units",
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
    # --- The rest of the sweep schedule. Four of the seven periodic sweeps already had
    # an interval here — LIFECYCLE_EVALUATION_INTERVAL, EXECUTION_RECONCILIATION_INTERVAL,
    # PAYMENT_STATE_RECONCILIATION_INTERVAL and REVIEW_SWEEP_INTERVAL. These are the other
    # three, and their absence was not a gap in the catalogue so much as a gap in the
    # deployment: nothing produced the sweeps at all, so nothing had ever needed to ask how
    # often. ``revora.jobs.ticker`` is what asks, and it refuses a kind it cannot price
    # rather than falling back to a default — which is why all seven have to be here.
    # An interval is a bound rather than a constant for the ordinary reason (R15.C6): how
    # often a merchant is willing to pay for a provider listing call, or to have its
    # customers' contact data swept, is that merchant's judgement, and changing it is a
    # recorded decision with a person's name on it. [ASSUMPTION] defaults on all three.
    ConfigurationBound(
        "DETECTION_GAP_BACKFILL_INTERVAL", ValueKind.DURATION_SECONDS, "900",
        "Interval between provider listing passes that backfill undelivered failures",
        note=(
            "[ASSUMPTION] 15 minutes, and deliberately the longest of the three. Each "
            "pass is a provider listing call whose cost falls on the merchant's API "
            "quota rather than on Revora, and the failure it catches — a disabled or "
            "broken webhook — persists for hours once it starts, so detecting it four "
            "times an hour and detecting it sixty times an hour close the same gap. It "
            "is well inside DETECTION_LATENCY_BOUND's sibling concern for the ordinary "
            "path: a delivered webhook is still detected in seconds, and this is only "
            "the path for one that never arrived."
        ),
    ),
    ConfigurationBound(
        "CALIBRATION_REPORT_INTERVAL", ValueKind.DURATION_SECONDS, "3600",
        "Interval between checks of whether a calibration report is due",
        note=(
            "[ASSUMPTION] hourly, and this is the interval of the *check*, not of the "
            "report. What decides that a report is due is "
            "CALIBRATION_REPORT_CASE_TRIGGER or CALIBRATION_REPORT_TIME_TRIGGER (7 "
            "days); this bound only says how often those two are consulted. Hourly is "
            "three orders of magnitude finer than the time trigger, which is what keeps "
            "the sweep from being the thing that decides when a report appears."
        ),
    ),
    ConfigurationBound(
        "CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL", ValueKind.DURATION_SECONDS, "3600",
        "Interval between redaction passes over contact data past its retention period",
        note=(
            "[ASSUMPTION] hourly, and named SWEEP_INTERVAL so it cannot be read as "
            "CUSTOMER_DATA_RETENTION, which is the retention *period* (180 days) and a "
            "different kind of number entirely. Confusing the two in either direction is "
            "expensive: an hourly retention period would redact live cases, and a "
            "180-day sweep interval would miss R17.C11's deadline by half a year. "
            "R17.C11 gives 24 hours after the period elapses, and no configured interval "
            "expressed that before this row existed — hourly leaves 24 passes inside the "
            "deadline, which is the margin a merchant with a backlog needs, since one "
            "pass redacts a batch and re-enqueues its own successor while more remains."
        ),
    ),
)

CATALOGUE: Mapping[str, ConfigurationBound] = MappingProxyType(
    {bound.key: bound for bound in _BOUNDS}
)
"""Every bound, by key. Read-only — a component must not add one at runtime."""

COST_SPLIT_BOUND_KEYS: Final[tuple[str, ...]] = (
    "PAYMENT_LINK_FINANCIAL_COST",
    "MESSAGE_COMMUNICATION_COST",
)
"""The two bounds R31.C11 names, as a named subset rather than as a list in a migration.

Migration ``0009`` seeds exactly these keys, and it selects them through this tuple so
that the migration contains no key string and no value of its own. A migration that
spelt the keys out would be a third place the pair is written down, which is the failure
the whole arrangement exists to avoid.

Every member must be a ``MONEY_MINOR`` bound; the check below is at import time because a
mistake here would otherwise surface as a seeded row of the wrong kind."""

if any(CATALOGUE[key].kind is not ValueKind.MONEY_MINOR for key in COST_SPLIT_BOUND_KEYS):
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "every COST_SPLIT_BOUND_KEYS member must be a MONEY_MINOR bound"
    )

REVIEW_LOOP_BOUND_KEYS: Final[tuple[str, ...]] = (
    "WAIT_REVIEW_INTERVAL",
    "REVIEW_SWEEP_INTERVAL",
)
"""The two bounds R30 introduces, as a named subset for the seed migration to select.

Migration ``0010`` seeds exactly these keys through this tuple, on the pattern ``0009``
set: the migration holds no key string, no value and no purpose text of its own, so the
accessor and the seeded row cannot disagree about a default.

Both must be ``DURATION_SECONDS``; the check below is at import time because a mistake
here would otherwise surface as a seeded row of the wrong kind, parsed by the accessor
into a value of the wrong type."""

if any(
    CATALOGUE[key].kind is not ValueKind.DURATION_SECONDS for key in REVIEW_LOOP_BOUND_KEYS
):
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "every REVIEW_LOOP_BOUND_KEYS member must be a DURATION_SECONDS bound"
    )

SWEEP_INTERVAL_BOUND_KEYS: Final[tuple[str, ...]] = (
    "DETECTION_GAP_BACKFILL_INTERVAL",
    "CALIBRATION_REPORT_INTERVAL",
    "CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL",
)
"""The three sweep intervals the ticker role introduces, for migration ``0014`` to select.

A tuple rather than the mapping ``CUSTOMER_TOKEN_BOUND_KEYS`` uses, because all three are
durations and a uniform kind check is available — which is the stronger guarantee of the two,
so it is taken where it can be, on the pattern ``0009`` set and ``0010`` through ``0013``
followed. The migration holds no key string, no value, no kind and no purpose text of its own.

The three are grouped because they arrived together and for one reason: ``revora.jobs.ticker``
prices every member of ``PERIODIC_SWEEP_KINDS`` from a bound and refuses a kind it cannot
price, so these were the three that had to exist before a schedule could run at all. They are
not otherwise related — a provider listing pass, a report trigger check and a privacy
redaction pass answer to three different requirements.

Every member must be ``DURATION_SECONDS``; the check below is at import time because a
mistake here would otherwise surface as a seeded row of the wrong kind, parsed by the accessor
into a value the ticker would then divide a timestamp by."""

if any(
    CATALOGUE[key].kind is not ValueKind.DURATION_SECONDS for key in SWEEP_INTERVAL_BOUND_KEYS
):
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "every SWEEP_INTERVAL_BOUND_KEYS member must be a DURATION_SECONDS bound"
    )

CUSTOMER_TOKEN_BOUND_KEYS: Final[Mapping[str, ValueKind]] = MappingProxyType(
    {
        "CUSTOMER_TOKEN_LIFETIME": ValueKind.DURATION_SECONDS,
        "CUSTOMER_TOKEN_MAX_SUBMISSIONS": ValueKind.INTEGER,
    }
)
"""The two bounds R18 introduces, as a named subset for the seed migration to select.

Migration ``0011`` seeds exactly these keys through this mapping, on the pattern ``0009`` set
and ``0010`` followed: the migration holds no key string, no value and no purpose text of its
own, so the accessor and the seeded row cannot disagree about a default.

A mapping rather than the tuple its two predecessors use, because these two bounds are of
**different kinds** — a duration and a count — so there is no single kind to assert. Pairing
each key with the kind it must have keeps the same import-time guarantee the tuples get from a
uniform check: a bound renamed or re-typed in the catalogue fails here rather than seeding a row
the accessor will later parse into a value of the wrong type."""

CUSTOMER_SURFACE_BOUND_KEYS: Final[tuple[str, ...]] = (
    "CUSTOMER_PAGE_RATE_LIMIT",
    "CUSTOMER_PAGE_SOURCE_RATE_LIMIT",
    "MAX_CUSTOMER_SIGNALS_PER_CASE",
    "DELAY_NOTE_MAX_LENGTH",
)
"""The four bounds the public customer surface introduces, for migration ``0012`` to select.

A tuple rather than the mapping ``CUSTOMER_TOKEN_BOUND_KEYS`` uses, because all four are counts
and a uniform kind check is available — which is the stronger guarantee of the two, so it is
taken where it can be. The migration holds no key string, no value and no purpose text of its
own, on the pattern ``0009`` set and ``0010`` and ``0011`` followed."""

if any(
    CATALOGUE[key].kind is not ValueKind.INTEGER for key in CUSTOMER_SURFACE_BOUND_KEYS
):
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "every CUSTOMER_SURFACE_BOUND_KEYS member must be an INTEGER bound"
    )

CUSTOMER_STATED_CAUSE_BOUND_KEYS: Final[tuple[str, ...]] = (
    "CUSTOMER_STATED_CAUSE_CONFIDENCE",
)
"""The one bound R20.C4 introduces, for migration ``0013`` to select.

A tuple of one rather than a bare string, so the migration's row-count assertion and its
``key = ANY(:keys)`` downgrade read the same way as ``0009`` through ``0012``. A second
bound added to this group later is an edit here and nowhere else.

It must be ``DECIMAL``: a confidence is compared against ``DIAGNOSIS_CONFIDENCE_FLOOR``
and stored in a ``NUMERIC(4,3)`` column, and an ``INTEGER`` or ``DURATION_SECONDS``
mis-declaration would surface as a seeded row the accessor parses into the wrong type and
a comparison that rounds. The check is at import time for that reason."""

if any(
    CATALOGUE[key].kind is not ValueKind.DECIMAL
    for key in CUSTOMER_STATED_CAUSE_BOUND_KEYS
):
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "every CUSTOMER_STATED_CAUSE_BOUND_KEYS member must be a DECIMAL bound"
    )

_mistyped_customer_token_bounds = sorted(
    key for key, kind in CUSTOMER_TOKEN_BOUND_KEYS.items() if CATALOGUE[key].kind is not kind
)
if _mistyped_customer_token_bounds:
    raise ConfigurationError(  # pragma: no cover - import-time invariant
        "CUSTOMER_TOKEN_BOUND_KEYS disagrees with the catalogue about the kind of "
        f"{_mistyped_customer_token_bounds}"
    )


def money_default(key: str) -> Minor:
    """The catalogue default of a ``MONEY_MINOR`` bound, typed.

    The one supported way for a module below the persistence layer to name a configured
    money bound's default. ``revora.estimation.candidates`` uses it for the two cost
    priors of R31.C11: the estimator's prior table needs a value at import time, before
    any session exists to read a row through, and this makes that value *the catalogue's
    default* rather than a second literal that happens to match it today.

    Parsed through :func:`parse_value`, the same function the accessor uses, so a default
    that the accessor would reject cannot reach a prior table by another route.

    Raises:
        ConfigurationError: if ``key`` is not a bound, is not ``MONEY_MINOR``, or has an
            unparseable default.
    """
    bound = CATALOGUE.get(key)
    if bound is None:
        raise ConfigurationError(f"no configuration bound named {key!r}")
    if bound.kind is not ValueKind.MONEY_MINOR:
        raise ConfigurationError(
            f"bound {key!r} is {bound.kind.value}, not {ValueKind.MONEY_MINOR.value}"
        )
    parsed = parse_value(bound.kind, bound.default)
    if isinstance(parsed, bool) or not isinstance(parsed, int):  # pragma: no cover
        raise ConfigurationError(f"bound {key!r} did not parse to an integer")
    return Minor(parsed)


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

    WAIT_REVIEW_INTERVAL: timedelta
    REVIEW_SWEEP_INTERVAL: timedelta

    CUSTOMER_TOKEN_LIFETIME: timedelta
    CUSTOMER_TOKEN_MAX_SUBMISSIONS: int

    CUSTOMER_PAGE_RATE_LIMIT: int
    CUSTOMER_PAGE_SOURCE_RATE_LIMIT: int
    MAX_CUSTOMER_SIGNALS_PER_CASE: int
    DELAY_NOTE_MAX_LENGTH: int
    CUSTOMER_STATED_CAUSE_CONFIDENCE: Decimal

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

    PAYMENT_LINK_FINANCIAL_COST: Minor
    MESSAGE_COMMUNICATION_COST: Minor

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

    DETECTION_GAP_BACKFILL_INTERVAL: timedelta
    CALIBRATION_REPORT_INTERVAL: timedelta
    CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL: timedelta

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


def seed_rows(*, keys: Collection[str] | None = None) -> Iterable[dict[str, object]]:
    """The rows a seed migration inserts, one per bound.

    Generated from the catalogue rather than written out again in SQL, so the
    accessor and the seeded rows cannot disagree about a default. ``is_assumption``
    is true on every one of them, because it is.

    Args:
        keys: restrict the output to these bounds. ``None`` yields every bound, which
            is what migration ``0004`` wants. A later migration seeding bounds added
            after ``0004`` passes the subset it introduced — see
            :data:`COST_SPLIT_BOUND_KEYS` — so it neither re-states a value nor has to
            re-insert rows that already exist.

    Raises:
        ConfigurationError: if ``keys`` names something that is not a bound. Silently
            yielding nothing would make a renamed key look like a migration that ran.
    """
    if keys is None:
        selected = _BOUNDS
    else:
        unknown = frozenset(keys) - frozenset(CATALOGUE)
        if unknown:
            raise ConfigurationError(f"no configuration bound named {sorted(unknown)}")
        wanted = frozenset(keys)
        selected = tuple(bound for bound in _BOUNDS if bound.key in wanted)
    for bound in selected:
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

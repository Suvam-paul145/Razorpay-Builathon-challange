"""Hierarchical feature segments, and the backoff that makes the sample size real.

The five features here are the design's, unchanged: ``risk_cause``, ``amount_band``,
``payment_method``, ``attempt_ordinal_band`` and ``error_source_band``. All five are
categorical, deliberately, because the estimator underneath them is a per-segment
Beta posterior rather than a fitted model — a continuous feature would have to be
binned to be counted anyway, and binning it here means the bin is visible in the
recorded ``segment_id`` instead of buried in a model artefact.

**Why the segments must back off.** The cross product is 8 causes x 4 amount bands x
6 methods x 2 attempt bands x 4 source bands, which is well over a thousand cells.
``MIN_SEGMENT_SAMPLE_SIZE`` defaults to 30, so satisfying it in every cell would need
tens of thousands of resolved no-intervention cases before a single estimate stopped
being a fallback. That is not a hackathon-scale number and it is not a first-year-of-
production number either. Without backoff the threshold would be a sentence in a
requirements document that no deployment ever satisfies, and every estimate would sit
at the same fallback label forever, which tells a reader nothing.

With backoff the threshold does real work. The lookup asks for the most specific cell
first and drops one feature at a time until a level has enough confirmed observations
to stand on, and **the level it stopped at is recorded in the segment id**. So the
recorded estimate says not only "0.31" but "0.31, from the cause-and-amount level,
because the full cell had four observations". A reader can see how much the number was
allowed to specialize. That is the difference between a fallback and a confident number
computed from three samples.

**The drop order is not arbitrary.** It runs from the feature carrying the least
information about recovery to the one carrying the most, so precision is surrendered
in the cheapest order:

1. ``error_source_band`` — mostly redundant once the cause is known, since the cause
   was largely derived from the same provider fields.
2. ``attempt_ordinal_band`` — a two-way split, so dropping it costs the least
   resolution of anything remaining.
3. ``payment_method`` — retry economics genuinely differ by method, but the six-way
   split is the largest single multiplier in the cross product.
4. ``amount_band`` — kept nearly last because willingness to complete a payment does
   track its size, and because R12.C10 already reports metrics by amount band, so
   keeping the model and the metrics aligned has value beyond the estimate.
5. ``risk_cause`` alone — the dominant signal, dropped only into the global prior.
6. the global prior — every confirmed observation the merchant has. Weak, and labelled
   weak.

**No ``float`` and no arithmetic.** This module builds keys and reads bands off
integer boundaries. It imports the standard library and ``revora.domain``, so it is
testable with no setup and reachable from the synthetic harness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final

from revora.domain.enums import RiskCause
from revora.domain.failure_taxonomy import ErrorSource
from revora.domain.money import Minor

# NOTE ON LOCATION. This module lived in ``revora.estimation`` until recovery memory needed
# it. The feature document is the interface between the component that *writes* observations
# (``revora.memory``) and the component that *reads* them into a posterior
# (``revora.estimation``) — they agree by JSONB containment on these exact five keys, and a
# key that drifts on one side does not raise, it silently stops matching and collapses every
# segment to the global prior.
#
# Those two packages sit in the same layer, so neither may import the other. The choices were
# to duplicate the banding rules in both (the drift bug, guaranteed) or to move the shared
# vocabulary down to where both can see it. It belongs here on its own merits: it is pure
# enums and integer arithmetic over domain types, with no I/O and no configuration, and it
# defines what a segment *is* rather than how one is estimated.

__all__ = [
    "AMOUNT_BAND_BOUNDARIES",
    "BACKOFF_ORDER",
    "FEATURE_AMOUNT_BAND",
    "FEATURE_ATTEMPT_ORDINAL_BAND",
    "FEATURE_ERROR_SOURCE_BAND",
    "FEATURE_KEYS",
    "FEATURE_PAYMENT_METHOD",
    "FEATURE_RISK_CAUSE",
    "GLOBAL_SEGMENT_ID",
    "LEVEL_FEATURES",
    "AmountBand",
    "AttemptOrdinalBand",
    "ErrorSourceBand",
    "PaymentMethodBand",
    "SegmentFeatures",
    "SegmentLevel",
    "amount_band_for",
    "attempt_ordinal_band_for",
    "backoff_candidates",
    "error_source_band_for",
    "payment_method_band_for",
    "segment_id_for",
]


# ---------------------------------------------------------------------------
# The five feature vocabularies
# ---------------------------------------------------------------------------


@unique
class AmountBand(StrEnum):
    """Four non-overlapping, exhaustive payment-amount bands.

    Four rather than a finer split because the band has to be populated by real
    observations to be worth anything, and because the same four bands carry the
    amount-band metric segmentation R12.C10 asks for. One vocabulary, two consumers,
    no chance of the dashboard and the estimator disagreeing about what "small" means.
    """

    MICRO = "MICRO"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"


AMOUNT_BAND_BOUNDARIES: Final[tuple[tuple[Minor, AmountBand], ...]] = (
    (Minor(25_000), AmountBand.MICRO),
    (Minor(500_000), AmountBand.SMALL),
    (Minor(5_000_000), AmountBand.MEDIUM),
)
"""Exclusive upper bounds in minor units, ascending; anything at or above the last
one is ``LARGE``. So ``MICRO`` is under ₹250, ``SMALL`` under ₹5,000, ``MEDIUM`` under
₹50,000 and ``LARGE`` from ₹50,000 up.

**[ASSUMPTION]**, and every one of the three numbers is a placeholder that no
measurement supports. They are not configuration yet only because the configuration
catalogue in ``platform.config`` has no bound for them; when it gains one, these move
there and this tuple becomes its default.

No boundary coincides with a configured policy threshold, and that is deliberate:
``ESCALATION_AMOUNT_THRESHOLD`` defaults to 50 000 minor units and
``MIN_DETECTION_AMOUNT`` to 100, so neither sits on a band edge. A segment boundary
that lined up exactly with a policy threshold would make the two impossible to tell
apart when a past decision is reviewed — every case in the band would also be every
case the threshold applied to, and the effect of one would be indistinguishable from
the effect of the other."""


@unique
class PaymentMethodBand(StrEnum):
    """The verified provider payment-method values, plus a catch-all.

    ``OTHER`` exists because the provider's method vocabulary is theirs to extend, and
    an unrecognized method must land in a named band rather than in whichever band the
    code happened to default to. A method we have never seen before is genuinely a
    different population, and calling it ``OTHER`` says so.
    """

    CARD = "CARD"
    NETBANKING = "NETBANKING"
    UPI = "UPI"
    WALLET = "WALLET"
    EMI = "EMI"
    OTHER = "OTHER"


_METHOD_TOKENS: Final[Mapping[str, PaymentMethodBand]] = MappingProxyType(
    {
        "card": PaymentMethodBand.CARD,
        "netbanking": PaymentMethodBand.NETBANKING,
        "upi": PaymentMethodBand.UPI,
        "wallet": PaymentMethodBand.WALLET,
        "emi": PaymentMethodBand.EMI,
    }
)
"""Provider tokens as the provider spells them, lower-case. Read-only."""


@unique
class AttemptOrdinalBand(StrEnum):
    """First failure versus a repeat. A second failure is not a first failure."""

    FIRST = "FIRST"
    REPEAT = "REPEAT"


@unique
class ErrorSourceBand(StrEnum):
    """Where the failure came from, collapsed from the verified per-method sources.

    Three meaningful bands: the customer has to act, infrastructure at the bank or on
    the network failed, or the provider's own systems or the acquiring gateway failed.
    The collapse is what makes the band populated at all — the provider publishes nine
    source values, split across payment methods, and nine three-observation cells are
    worth less than three thirty-observation ones.

    ``UNSTATED`` is a fourth band and an addition to the design's three. It is here
    because the provider leaves ``error_source`` null on some failures and a
    redelivered or partial payload can carry none at all, and folding those into one of
    the three real bands would attribute a failure to a party we did not observe. It is
    an honest absence, and since this feature is the first one backoff drops, a fourth
    value costs nothing in practice.
    """

    CUSTOMER = "CUSTOMER"
    BANK_OR_NETWORK = "BANK_OR_NETWORK"
    INTERNAL_OR_GATEWAY = "INTERNAL_OR_GATEWAY"
    UNSTATED = "UNSTATED"


_SOURCE_BANDS: Final[Mapping[str, ErrorSourceBand]] = MappingProxyType(
    {
        ErrorSource.CUSTOMER.value: ErrorSourceBand.CUSTOMER,
        ErrorSource.CUSTOMER_PSP.value: ErrorSourceBand.CUSTOMER,
        ErrorSource.ISSUER_BANK.value: ErrorSourceBand.BANK_OR_NETWORK,
        ErrorSource.ISSUER.value: ErrorSourceBand.BANK_OR_NETWORK,
        ErrorSource.BENEFICIARY_BANK.value: ErrorSourceBand.BANK_OR_NETWORK,
        ErrorSource.NETWORK.value: ErrorSourceBand.BANK_OR_NETWORK,
        ErrorSource.INTERNAL.value: ErrorSourceBand.INTERNAL_OR_GATEWAY,
        ErrorSource.GATEWAY.value: ErrorSourceBand.INTERNAL_OR_GATEWAY,
        ErrorSource.BUSINESS.value: ErrorSourceBand.INTERNAL_OR_GATEWAY,
    }
)
"""Every verified ``error_source`` and the band it collapses into.

``business`` sits with ``internal_or_gateway`` rather than getting its own band: it
means the merchant's own integration sent something wrong, which is infrastructure
from the customer's point of view and is already surfaced separately as an operational
alert by ``domain.failure_taxonomy``. ``customer_psp`` sits with ``customer`` because
the customer's own payment app failing is something only the customer can resolve."""


# ---------------------------------------------------------------------------
# Feature derivation
# ---------------------------------------------------------------------------


def amount_band_for(amount: Minor) -> AmountBand:
    """The band a payment amount falls in. Total over every integer amount."""
    value = int(amount)
    for boundary, band in AMOUNT_BAND_BOUNDARIES:
        if value < int(boundary):
            return band
    return AmountBand.LARGE


def payment_method_band_for(method: str | None) -> PaymentMethodBand:
    """The band a provider method token falls in; anything unrecognized is ``OTHER``."""
    if method is None:
        return PaymentMethodBand.OTHER
    return _METHOD_TOKENS.get(method.strip().lower(), PaymentMethodBand.OTHER)


def attempt_ordinal_band_for(executed_action_count: int) -> AttemptOrdinalBand:
    """First versus repeat, from the case's executed-action counter.

    Read off the counter rather than from a provider attempt number because the
    provider gives us none for one-off payments — a failed payment is terminal there
    and a further attempt is a new payment the customer starts. What Revora can
    observe is whether it has already acted on this case, which is the distinction the
    band is actually being used for.
    """
    return AttemptOrdinalBand.FIRST if executed_action_count < 1 else AttemptOrdinalBand.REPEAT


def error_source_band_for(error_source: str | None) -> ErrorSourceBand:
    """The band an ``error_source`` collapses into; absent or unknown is ``UNSTATED``."""
    if error_source is None:
        return ErrorSourceBand.UNSTATED
    normalized = error_source.strip().lower()
    if not normalized:
        return ErrorSourceBand.UNSTATED
    return _SOURCE_BANDS.get(normalized, ErrorSourceBand.UNSTATED)


# ---------------------------------------------------------------------------
# The feature vector and the levels
# ---------------------------------------------------------------------------

FEATURE_RISK_CAUSE: Final[str] = "risk_cause"
FEATURE_AMOUNT_BAND: Final[str] = "amount_band"
FEATURE_PAYMENT_METHOD: Final[str] = "payment_method"
FEATURE_ATTEMPT_ORDINAL_BAND: Final[str] = "attempt_ordinal_band"
FEATURE_ERROR_SOURCE_BAND: Final[str] = "error_source_band"

FEATURE_KEYS: Final[tuple[str, ...]] = (
    FEATURE_RISK_CAUSE,
    FEATURE_AMOUNT_BAND,
    FEATURE_PAYMENT_METHOD,
    FEATURE_ATTEMPT_ORDINAL_BAND,
    FEATURE_ERROR_SOURCE_BAND,
)
"""The five keys, in the order a segment id renders them.

These names are an interface, not an implementation detail. They are the keys of
``baseline_estimate.features`` and of ``memory_observation.features``, and the segment
aggregate query matches on them by containment — so a rename here without a
corresponding migration would silently stop matching historical observations and every
segment would fall back to the global prior with nothing looking broken."""


@dataclass(frozen=True, slots=True)
class SegmentFeatures:
    """One case's five categorical feature values.

    Frozen, so a value recorded on an estimate cannot be mutated by a later consumer
    and leave the stored ``features`` document disagreeing with the segment id it was
    computed from.
    """

    risk_cause: RiskCause
    amount_band: AmountBand
    payment_method: PaymentMethodBand
    attempt_ordinal_band: AttemptOrdinalBand
    error_source_band: ErrorSourceBand

    @classmethod
    def derive(
        cls,
        *,
        risk_cause: RiskCause,
        amount: Minor,
        payment_method: str | None,
        executed_action_count: int,
        error_source: str | None,
    ) -> SegmentFeatures:
        """Build from the persisted case and canonical-event fields.

        Every input is either an enum, an integer, or a provider token, and every one
        of them is already PII-free — the amount enters as a band and nothing here
        touches a contact, a customer key or an instrument reference. That is why
        ``baseline_estimate.features`` can be stored in clear and shown on a dashboard.
        """
        return cls(
            risk_cause=risk_cause,
            amount_band=amount_band_for(amount),
            payment_method=payment_method_band_for(payment_method),
            attempt_ordinal_band=attempt_ordinal_band_for(executed_action_count),
            error_source_band=error_source_band_for(error_source),
        )

    def as_values(self) -> dict[str, str]:
        """The full feature vector as a plain string mapping.

        The stored form. String values rather than enum members because this lands in
        ``JSONB`` and is read back by an aggregate query, a calibration report and the
        dashboard — three readers, so the serialized shape is the contract.
        """
        return {
            FEATURE_RISK_CAUSE: self.risk_cause.value,
            FEATURE_AMOUNT_BAND: self.amount_band.value,
            FEATURE_PAYMENT_METHOD: self.payment_method.value,
            FEATURE_ATTEMPT_ORDINAL_BAND: self.attempt_ordinal_band.value,
            FEATURE_ERROR_SOURCE_BAND: self.error_source_band.value,
        }

    def values_at(self, level: SegmentLevel) -> dict[str, str]:
        """The feature subset a given backoff level keys on.

        This is what the aggregate query matches by containment, which is what makes
        backoff a narrowing of one query rather than five different queries: a level's
        subset is a subset of the level above it, so an observation counted at a
        specific level is counted at every more general level too.
        """
        keep = LEVEL_FEATURES[level]
        return {key: value for key, value in self.as_values().items() if key in keep}


@unique
class SegmentLevel(StrEnum):
    """The six backoff levels, most specific first.

    Named for what they hold rather than numbered, because the level is recorded in
    ``segment_id`` and read by a person asking why an estimate is what it is.
    ``L1``-style prefixes on the rendered id preserve the ordering for a sort while the
    name preserves the meaning.
    """

    FULL = "FULL"
    WITHOUT_ERROR_SOURCE = "WITHOUT_ERROR_SOURCE"
    WITHOUT_ATTEMPT_ORDINAL = "WITHOUT_ATTEMPT_ORDINAL"
    WITHOUT_PAYMENT_METHOD = "WITHOUT_PAYMENT_METHOD"
    CAUSE_ONLY = "CAUSE_ONLY"
    GLOBAL = "GLOBAL"


LEVEL_FEATURES: Final[Mapping[SegmentLevel, tuple[str, ...]]] = MappingProxyType(
    {
        SegmentLevel.FULL: FEATURE_KEYS,
        SegmentLevel.WITHOUT_ERROR_SOURCE: FEATURE_KEYS[:4],
        SegmentLevel.WITHOUT_ATTEMPT_ORDINAL: FEATURE_KEYS[:3],
        SegmentLevel.WITHOUT_PAYMENT_METHOD: FEATURE_KEYS[:2],
        SegmentLevel.CAUSE_ONLY: FEATURE_KEYS[:1],
        SegmentLevel.GLOBAL: (),
    }
)
"""Which features each level keys on. Derived from one ordered tuple by truncation
rather than written out five times, so the drop order and the level definitions cannot
disagree — adding a sixth feature to ``FEATURE_KEYS`` extends every level at once."""

BACKOFF_ORDER: Final[tuple[SegmentLevel, ...]] = (
    SegmentLevel.FULL,
    SegmentLevel.WITHOUT_ERROR_SOURCE,
    SegmentLevel.WITHOUT_ATTEMPT_ORDINAL,
    SegmentLevel.WITHOUT_PAYMENT_METHOD,
    SegmentLevel.CAUSE_ONLY,
    SegmentLevel.GLOBAL,
)
"""The order a lookup tries levels in. The first level with at least
``MIN_SEGMENT_SAMPLE_SIZE`` confirmed no-intervention observations wins; if none
reaches it, the global level is used and the estimate is labelled a fallback."""

GLOBAL_SEGMENT_ID: Final[str] = "L6:GLOBAL"
"""The id of the last-resort level. Named as a constant because the dashboard groups
by segment id and this is the group that means "we had nothing specific to say"."""

_LEVEL_PREFIX: Final[Mapping[SegmentLevel, str]] = MappingProxyType(
    {level: f"L{index + 1}" for index, level in enumerate(BACKOFF_ORDER)}
)


def segment_id_for(features: SegmentFeatures, level: SegmentLevel) -> str:
    """Render the segment id, with the level used as its first component.

    Format: ``L3:INSUFFICIENT_FUNDS|MEDIUM|CARD``. The level prefix is first and
    mandatory, which is what satisfies "the level used is recorded in ``segment_id``"
    — and it is a prefix rather than a suffix so that grouping by level on a dashboard
    is a prefix match rather than a parse.

    Deliberately human-readable rather than a hash. A merchant asking why two
    similar-looking payments got different baselines needs to be able to see that they
    landed in different cells, and a hash makes that a lookup instead of a glance.
    """
    prefix = _LEVEL_PREFIX[level]
    if level is SegmentLevel.GLOBAL:
        return GLOBAL_SEGMENT_ID
    ordered = features.values_at(level)
    rendered = "|".join(ordered[key] for key in LEVEL_FEATURES[level])
    return f"{prefix}:{rendered}"


def backoff_candidates(
    features: SegmentFeatures,
) -> tuple[tuple[SegmentLevel, str, dict[str, str]], ...]:
    """Every level to try, in order, with its segment id and its matching subset.

    Returned as a tuple rather than yielded, because the caller records how many
    levels it examined in the audit trail and a generator would make that require a
    second pass. Six entries, always.
    """
    return tuple(
        (level, segment_id_for(features, level), features.values_at(level))
        for level in BACKOFF_ORDER
    )

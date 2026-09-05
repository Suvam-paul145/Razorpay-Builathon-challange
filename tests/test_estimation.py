"""Estimation behaviour that needs no database.

Five groups, all in the ``pure`` tier, because the estimator was deliberately built as a
pure core with a thin service around it. The rules that can be wrong in a way nobody
notices — which backoff level wins, which labels get recorded, what happens when the
memory store cannot be read — are all in the core, and all checkable here in
microseconds against a dictionary.

**The backoff.** ``MIN_SEGMENT_SAMPLE_SIZE`` is only satisfiable because segments back
off, so the tests are about *which* level was used and whether the record says so. A
segment that fell back and reported a confident-looking number without saying it fell
back is the failure mode.

**The labels.** Method, provenance and validation status describe the conditions an
estimate was produced under, and none of them is reconstructable later. Each has one
rule and each rule gets a test.

**Failure records nothing.** R5.C11's path is checked at the level where it is decided:
the core returns a ``BaselineFailure``, which has no probability to write, so the service
physically cannot persist a row from it. That is a stronger statement than "the insert is
behind an if".

**The candidate set.** Retention of unavailable actions and rejection of invalid figures,
including the rejection path forced by a deliberately corrupted cost prior — the one
input a misconfiguration could realistically break.

**The structural claim.** R6.C8 says zero provider requests. A runtime test would only
show that *this* run made none, so it is checked as a property of the import graph.

The database-level guarantees — idempotency under a real retry, the interval-present-iff-
available constraint, the JSONB containment aggregate — belong in the Postgres tier,
because a partial unique index and a ``@>`` operator are Postgres facts and a fake would
only prove the fake agrees with itself.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from revora.audit.events import (
    ALL_EVENT_TYPES,
    BASELINE_ALREADY_RECORDED,
    BASELINE_ESTIMATE_RECORDED,
    BASELINE_ESTIMATION_FAILED,
    CANDIDATE_ACTION_UNAVAILABLE,
    CANDIDATE_ESTIMATES_ALREADY_RECORDED,
    CANDIDATE_ESTIMATES_RECORDED,
    CANDIDATE_MEMORY_UNAVAILABLE,
    INVALID_ESTIMATE,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    EstimationMethod,
    ExclusionReason,
    Provenance,
    RiskCause,
    ValidationStatus,
)
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.domain.segments import (
    BACKOFF_ORDER,
    FEATURE_KEYS,
    GLOBAL_SEGMENT_ID,
    AmountBand,
    AttemptOrdinalBand,
    ErrorSourceBand,
    PaymentMethodBand,
    SegmentFeatures,
    SegmentLevel,
    amount_band_for,
    error_source_band_for,
    payment_method_band_for,
    segment_id_for,
)
from revora.estimation.baseline import (
    BASELINE_MODEL_VERSION,
    FAILURE_MEMORY_UNAVAILABLE,
    FAILURE_TIMEOUT,
    UNCERTAINTY_UNAVAILABLE,
    BaselineFailure,
    BaselineFigures,
    MemoryUnavailableError,
    estimate_baseline,
    select_segment,
)
from revora.estimation.candidates import (
    COST_PRIORS,
    UPLIFT_PRIORS,
    RawFigures,
    RejectedFigure,
    build_candidate_set,
    cost_prior_for,
    validate_figures,
)
from revora.persistence.repositories.estimates import SegmentCounts
from revora.platform.config import default_configuration

pytestmark = pytest.mark.pure

REPO_ROOT = Path(__file__).resolve().parents[1]
MIN_SAMPLE = 30
WINDOW = timedelta(days=7)
CONFIG = default_configuration()
"""The catalogue defaults. ``build_candidate_set`` resolves the two R31.C11 cost rows off
one of these, so a test that prices an action has to supply it."""

FORBIDDEN_PACKAGES = ("revora.providers", "revora.reasoning")
"""The two packages estimation must not be able to reach. ``providers`` is the payment
client, so an import of it would make R6.C8's "zero provider requests" a promise rather
than a structural fact; ``reasoning`` is the LLM adapter, and an estimate influenced by a
model output would put an AI-derived figure into the value chain."""


FEATURES = SegmentFeatures(
    risk_cause=RiskCause.INSUFFICIENT_FUNDS,
    amount_band=AmountBand.MEDIUM,
    payment_method=PaymentMethodBand.CARD,
    attempt_ordinal_band=AttemptOrdinalBand.FIRST,
    error_source_band=ErrorSourceBand.CUSTOMER,
)


def counts(
    observations: int = 0,
    recoveries: int = 0,
    *,
    synthetic: int = 0,
    unknown: int = 0,
    control: int = 0,
) -> SegmentCounts:
    """A segment aggregate, spelt out so a test reads as the situation it describes."""
    return SegmentCounts(
        observations=observations,
        recoveries=recoveries,
        synthetic_contributions=synthetic,
        unknown_intervention=unknown,
        resolved_control=control,
    )


def lookup_from(table: Mapping[SegmentLevel, SegmentCounts]):
    """A segment lookup backed by a dictionary. Missing levels are empty."""

    def lookup(level: SegmentLevel, subset: Mapping[str, str]) -> SegmentCounts:
        return table.get(level, counts())

    return lookup


# ---------------------------------------------------------------------------
# Feature derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, AmountBand.MICRO),
        (24_999, AmountBand.MICRO),
        (25_000, AmountBand.SMALL),
        (499_999, AmountBand.SMALL),
        (500_000, AmountBand.MEDIUM),
        (4_999_999, AmountBand.MEDIUM),
        (5_000_000, AmountBand.LARGE),
        (10_000_000_000, AmountBand.LARGE),
    ],
)
def test_amount_bands_are_exhaustive_and_non_overlapping(
    amount: int, expected: AmountBand
) -> None:
    """Every integer amount lands in exactly one band, boundaries included.

    Parametrized on both sides of each boundary, because a band assignment that is off by
    one minor unit puts a case in a different segment and therefore against a different
    baseline — a difference nobody would ever see in an aggregate.
    """
    assert amount_band_for(Minor(amount)) is expected


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("card", PaymentMethodBand.CARD),
        ("UPI", PaymentMethodBand.UPI),
        (" netbanking ", PaymentMethodBand.NETBANKING),
        ("emi", PaymentMethodBand.EMI),
        ("cardless_emi", PaymentMethodBand.OTHER),
        (None, PaymentMethodBand.OTHER),
        ("", PaymentMethodBand.OTHER),
    ],
)
def test_payment_method_bands(method: str | None, expected: PaymentMethodBand) -> None:
    """An unrecognized or absent method is ``OTHER``, never a default real method.

    A method the provider added last week is genuinely a different population, and
    silently filing it under ``CARD`` would pollute the one segment that has enough
    observations to matter.
    """
    assert payment_method_band_for(method) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("customer", ErrorSourceBand.CUSTOMER),
        ("customer_psp", ErrorSourceBand.CUSTOMER),
        ("issuer_bank", ErrorSourceBand.BANK_OR_NETWORK),
        ("network", ErrorSourceBand.BANK_OR_NETWORK),
        ("internal", ErrorSourceBand.INTERNAL_OR_GATEWAY),
        ("gateway", ErrorSourceBand.INTERNAL_OR_GATEWAY),
        ("business", ErrorSourceBand.INTERNAL_OR_GATEWAY),
        (None, ErrorSourceBand.UNSTATED),
        ("something_new", ErrorSourceBand.UNSTATED),
    ],
)
def test_error_source_bands_collapse_the_verified_sources(
    source: str | None, expected: ErrorSourceBand
) -> None:
    """Nine provider sources collapse into three, and absence is its own band.

    ``UNSTATED`` rather than folding an absent source into one of the three: attributing
    a failure to the customer because the provider said nothing is exactly the kind of
    quiet inference that ends in a message to somebody who did nothing wrong.
    """
    assert error_source_band_for(source) is expected


def test_derive_reads_the_features_off_the_case_and_the_event() -> None:
    """The five features come from persisted fields, and none of them is PII."""
    features = SegmentFeatures.derive(
        risk_cause=RiskCause.EXPIRED_PAYMENT_METHOD,
        amount=Minor(2_000_000),
        payment_method="card",
        executed_action_count=2,
        error_source="issuer_bank",
    )
    assert features.risk_cause is RiskCause.EXPIRED_PAYMENT_METHOD
    assert features.amount_band is AmountBand.MEDIUM
    assert features.payment_method is PaymentMethodBand.CARD
    assert features.attempt_ordinal_band is AttemptOrdinalBand.REPEAT
    assert features.error_source_band is ErrorSourceBand.BANK_OR_NETWORK
    assert set(features.as_values()) == set(FEATURE_KEYS)


# ---------------------------------------------------------------------------
# The backoff, and what the segment id records
# ---------------------------------------------------------------------------


def test_segment_id_records_the_level_used_as_its_prefix() -> None:
    """The level is the first component of the id, and the global level is named.

    R5.C2 requires the segment identifier on every estimate and the design requires the
    level used to be recorded in it. A prefix rather than a suffix so grouping by level
    on a dashboard is a prefix match rather than a parse.
    """
    assert segment_id_for(FEATURES, SegmentLevel.FULL) == (
        "L1:INSUFFICIENT_FUNDS|MEDIUM|CARD|FIRST|CUSTOMER"
    )
    assert segment_id_for(FEATURES, SegmentLevel.WITHOUT_ERROR_SOURCE) == (
        "L2:INSUFFICIENT_FUNDS|MEDIUM|CARD|FIRST"
    )
    assert segment_id_for(FEATURES, SegmentLevel.WITHOUT_PAYMENT_METHOD) == (
        "L4:INSUFFICIENT_FUNDS|MEDIUM"
    )
    assert segment_id_for(FEATURES, SegmentLevel.CAUSE_ONLY) == "L5:INSUFFICIENT_FUNDS"
    assert segment_id_for(FEATURES, SegmentLevel.GLOBAL) == GLOBAL_SEGMENT_ID


def test_levels_drop_one_feature_at_a_time_in_the_declared_order() -> None:
    """The drop order is the design's, and each level is a subset of the one above.

    The subset relation is what makes backoff a narrowing of one query: an observation
    counted at a specific level is counted at every more general one, so the counts can
    only grow as the lookup backs off.
    """
    subsets = [set(FEATURES.values_at(level)) for level in BACKOFF_ORDER]
    for finer, coarser in pairwise(subsets):
        assert coarser < finer
    assert subsets[0] == set(FEATURE_KEYS)
    assert subsets[-1] == set()


def test_a_populated_specific_level_wins_without_backing_off() -> None:
    """Backoff stops at the first level that clears the threshold."""
    selected = select_segment(
        FEATURES,
        lookup=lookup_from({SegmentLevel.FULL: counts(40, 12)}),
        min_sample_size=MIN_SAMPLE,
    )
    assert selected.level is SegmentLevel.FULL
    assert selected.sample_size_satisfied is True
    assert selected.levels_examined == 1
    assert selected.segment_id.startswith("L1:")


def test_a_thin_segment_backs_off_and_the_used_level_is_in_the_segment_id() -> None:
    """Three observations is not thirty, so the lookup drops features until it is.

    This is the property that makes ``MIN_SEGMENT_SAMPLE_SIZE`` real rather than
    theoretical. The specific cell has three observations and reporting a confident
    number from three samples is exactly what backoff exists to prevent; the coarser
    level has thirty-one, and the recorded id says which one answered.
    """
    selected = select_segment(
        FEATURES,
        lookup=lookup_from(
            {
                SegmentLevel.FULL: counts(3, 1),
                SegmentLevel.WITHOUT_ERROR_SOURCE: counts(9, 4),
                SegmentLevel.WITHOUT_ATTEMPT_ORDINAL: counts(31, 10),
            }
        ),
        min_sample_size=MIN_SAMPLE,
    )
    assert selected.level is SegmentLevel.WITHOUT_ATTEMPT_ORDINAL
    assert selected.sample_size_satisfied is True
    assert selected.levels_examined == 3
    assert selected.segment_id == "L3:INSUFFICIENT_FUNDS|MEDIUM|CARD"


def test_an_empty_history_falls_all_the_way_to_the_global_prior() -> None:
    """With nothing anywhere, the global level answers and says it was not satisfied.

    The ordinary state of a fresh deployment. An estimate is still produced — a case
    cannot wait for thirty observations — but every part of the record says how little is
    behind it: the global segment id, the unsatisfied flag, and the wide interval.
    """
    selected = select_segment(
        FEATURES, lookup=lookup_from({}), min_sample_size=MIN_SAMPLE
    )
    assert selected.level is SegmentLevel.GLOBAL
    assert selected.segment_id == GLOBAL_SEGMENT_ID
    assert selected.sample_size_satisfied is False
    assert selected.levels_examined == len(BACKOFF_ORDER)


def test_only_confirmed_no_intervention_observations_count_toward_the_threshold() -> None:
    """R5.C6: a segment full of unknown-status observations is still a thin segment.

    Fifty observations Revora cannot vouch for do not make a baseline. If they counted,
    the threshold would be cleared by exactly the data whose intervention status is
    unknowable, which is the bias the requirement exists to keep visible.
    """
    selected = select_segment(
        FEATURES,
        lookup=lookup_from({SegmentLevel.FULL: counts(2, 1, unknown=50)}),
        min_sample_size=MIN_SAMPLE,
    )
    assert selected.level is SegmentLevel.GLOBAL


# ---------------------------------------------------------------------------
# The estimate, and its labels
# ---------------------------------------------------------------------------


def figures_for(
    table: Mapping[SegmentLevel, SegmentCounts], *, case_is_synthetic: bool = False
) -> BaselineFigures:
    result = estimate_baseline(
        FEATURES,
        lookup=lookup_from(table),
        min_sample_size=MIN_SAMPLE,
        case_is_synthetic=case_is_synthetic,
    )
    assert isinstance(result, BaselineFigures)
    return result


def test_cold_start_estimate_is_the_prior_mean_with_a_nearly_useless_interval() -> None:
    """No data gives 0.500 and [0.025, 0.975], recorded to three decimal places.

    The design's cold-start case end to end. The interval width is the message, and it
    reaches the record intact rather than being narrowed on the way.
    """
    figures = figures_for({})
    assert figures.probability == Probability(Decimal("0.500"))
    assert figures.interval == (
        Probability(Decimal("0.025")),
        Probability(Decimal("0.975")),
    )
    assert figures.uncertainty_available is True
    # Rendered at the column's four places, from a value rounded outward at three.
    assert figures.interval_label == "[0.0250, 0.9750]"
    assert figures.probability.value.as_tuple().exponent == -4


def test_probability_is_recorded_to_three_decimal_places() -> None:
    """R5.C1 fixes three places. The fourth is noise at these sample sizes.

    Held in a four-place ``Probability``, so what lands in the ``NUMERIC(6,4)`` column is
    a three-place figure with a trailing zero rather than a differently-rounded one.
    """
    figures = figures_for({SegmentLevel.FULL: counts(30, 10)})
    # (1 + 10) / (2 + 30) = 0.34375, which rounds half-up at three places to 0.344.
    assert figures.probability == Probability(Decimal("0.344"))
    assert str(figures.probability) == "0.3440"


def test_interval_narrows_as_the_segment_fills_but_the_label_does_not_change() -> None:
    """More data tightens the interval; the method stays ``PRIOR_FALLBACK``.

    Because no estimator other than the posterior exists. Calling a well-populated
    posterior ``DETERMINISTIC`` would claim a fitted model, and the fitted model is BUILD
    LATER. What actually changes with the sample size is the interval and the
    ``sample_size_satisfied`` flag, and both are recorded.
    """
    thin = figures_for({})
    dense = figures_for({SegmentLevel.FULL: counts(200, 60)})
    thin_low, thin_high = thin.interval or (None, None)
    dense_low, dense_high = dense.interval or (None, None)
    assert thin_low is not None and dense_low is not None
    assert (dense_high.value - dense_low.value) < (thin_high.value - thin_low.value)
    assert thin.method is EstimationMethod.PRIOR_FALLBACK
    assert dense.method is EstimationMethod.PRIOR_FALLBACK
    assert thin.segment.sample_size_satisfied is False
    assert dense.segment.sample_size_satisfied is True


def test_no_resolved_control_case_means_unvalidated_baseline() -> None:
    """R5.C12: with nothing observed under no intervention, nothing has been checked."""
    figures = figures_for({SegmentLevel.FULL: counts(40, 10, control=0)})
    assert figures.validation_status is ValidationStatus.UNVALIDATED_BASELINE


def test_control_observations_upgrade_the_status_only_to_unverified() -> None:
    """Data existing is not the same as data having been checked.

    ``CALIBRATION_UNVERIFIED`` rather than ``VALIDATED``, because the calibration report
    that would compare predicted against observed is a separate, optional component and
    it is not built. Recording ``VALIDATED`` here would assert a comparison nobody ran.
    """
    figures = figures_for({SegmentLevel.FULL: counts(40, 10, control=7)})
    assert figures.validation_status is ValidationStatus.CALIBRATION_UNVERIFIED


def test_one_synthetic_contributor_makes_the_whole_estimate_synthetic() -> None:
    """R5.C4: ``REAL`` requires *every* contributing observation to be real."""
    real = figures_for({SegmentLevel.FULL: counts(40, 10)})
    tainted = figures_for({SegmentLevel.FULL: counts(40, 10, synthetic=1)})
    assert real.provenance is Provenance.REAL
    assert tainted.provenance is Provenance.SYNTHETIC


def test_a_synthetic_case_is_synthetic_even_from_real_observations() -> None:
    """The case being estimated is itself a contributor to the claim.

    A synthetic case with a real segment behind it still produces a figure that cannot
    support a real-world claim, because the payment it is about did not happen.
    """
    figures = figures_for({SegmentLevel.FULL: counts(40, 10)}, case_is_synthetic=True)
    assert figures.provenance is Provenance.SYNTHETIC


def test_the_snapshot_id_names_the_estimator_and_the_counts_behind_it() -> None:
    """R5.C2's training-data snapshot identifier, as a reproducible content descriptor.

    Given the same aggregate the estimate is reconstructable exactly; a snapshot id that
    differs tells you the aggregate moved. That is what the requirement is for, and it is
    achievable without materializing a copy of the observation set per case.
    """
    figures = figures_for({SegmentLevel.FULL: counts(40, 10)})
    assert figures.training_snapshot_id == (
        f"{BASELINE_MODEL_VERSION}/L1:INSUFFICIENT_FUNDS|MEDIUM|CARD|FIRST|CUSTOMER/n40/s10"
    )
    assert figures.model_version == BASELINE_MODEL_VERSION


def test_the_feature_document_carries_the_features_and_the_counts() -> None:
    """What lands in ``baseline_estimate.features``.

    The five feature values so a reader can see the cell, plus the counts and the level
    so a later calibration report can recompute the posterior against exactly what the
    estimator saw rather than against whatever the segment holds by then.
    """
    figures = figures_for({SegmentLevel.FULL: counts(40, 10, unknown=3, control=2)})
    document = figures.feature_document()
    for key in FEATURE_KEYS:
        assert key in document
    assert document["segment_level"] == SegmentLevel.FULL.value
    assert document["observations"] == 40
    assert document["recoveries"] == 10
    assert document["unknown_intervention"] == 3
    assert document["resolved_control"] == 2
    assert document["posterior_alpha"] == 11
    assert document["posterior_beta"] == 31
    assert document["sample_size_satisfied"] is True


# ---------------------------------------------------------------------------
# R5.C11: failure records nothing at all
# ---------------------------------------------------------------------------


def test_an_unreachable_memory_store_produces_no_estimate() -> None:
    """A failure has no probability, so nothing can be written from it.

    The structural form of "a missing baseline must never read as zero": the core returns
    a type with no probability field, so the persistence path has nothing to insert. Not
    a zero, not a default, not a half-populated row.
    """

    def broken(level: SegmentLevel, subset: Mapping[str, str]) -> SegmentCounts:
        raise MemoryUnavailableError("connection reset")

    result = estimate_baseline(FEATURES, lookup=broken, min_sample_size=MIN_SAMPLE)
    assert isinstance(result, BaselineFailure)
    assert result.reason == FAILURE_MEMORY_UNAVAILABLE
    assert not hasattr(result, "probability")


def test_a_spent_budget_produces_no_estimate() -> None:
    """``BASELINE_ESTIMATION_TIMEOUT`` exhausted is the same answer as an outage.

    Both leave the case in ``DIAGNOSED`` with a ``BASELINE_ESTIMATION_FAILED`` record and
    no row, and the reason token distinguishes them so an operator can tell a capacity
    problem from an outage.
    """
    result = estimate_baseline(
        FEATURES,
        lookup=lookup_from({}),
        min_sample_size=MIN_SAMPLE,
        out_of_time=lambda: True,
    )
    assert isinstance(result, BaselineFailure)
    assert result.reason == FAILURE_TIMEOUT


def test_a_budget_that_expires_mid_backoff_abandons_rather_than_settling() -> None:
    """A partially walked backoff is not a result.

    The level it stopped at was not chosen, it was merely the last one reached, so
    returning it would record a specificity the data does not support. Two levels are
    queried and then the budget expires.
    """
    calls: list[SegmentLevel] = []

    def lookup(level: SegmentLevel, subset: Mapping[str, str]) -> SegmentCounts:
        calls.append(level)
        return counts(1, 0)

    result = estimate_baseline(
        FEATURES,
        lookup=lookup,
        min_sample_size=MIN_SAMPLE,
        out_of_time=lambda: len(calls) >= 2,
    )
    assert isinstance(result, BaselineFailure)
    assert result.reason == FAILURE_TIMEOUT
    assert result.levels_examined == 0
    assert len(calls) == 2


def test_uncertainty_unavailable_is_a_literal_string_and_never_produced_here() -> None:
    """R5.C9's alternative exists as a token, and the MVP always has an interval.

    Asserted so the constant is not quietly dropped before the fitted estimator needs it,
    and so its absence from every MVP estimate is a stated fact rather than an accident.
    """
    assert UNCERTAINTY_UNAVAILABLE == "UNCERTAINTY_UNAVAILABLE"
    assert figures_for({}).interval_label != UNCERTAINTY_UNAVAILABLE


# ---------------------------------------------------------------------------
# R6.C12: an invalid figure marks the candidate unavailable, and is recorded
# ---------------------------------------------------------------------------


def raw(**overrides: object) -> RawFigures:
    """A valid raw figure set, with fields overridden per test."""
    base: dict[str, object] = {
        "action": CandidateAction.PAYMENT_LINK,
        "intervention_probability": Decimal("0.4000"),
        "financial_cost": 300,
        "communication_cost": 25,
        "risk_cost": 0,
        "customer_cost": 1_000,
        "probability_method": EstimationMethod.UNCALIBRATED,
        "financial_cost_method": EstimationMethod.PRIOR_FALLBACK,
        "communication_cost_method": EstimationMethod.PRIOR_FALLBACK,
        "risk_cost_method": EstimationMethod.UNCALIBRATED,
        "customer_cost_method": EstimationMethod.UNCALIBRATED,
    }
    base.update(overrides)
    return RawFigures(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "figure"),
    [
        ({"intervention_probability": Decimal("1.5")}, "intervention_probability"),
        ({"intervention_probability": Decimal("-0.1")}, "intervention_probability"),
        ({"financial_cost": -1}, "financial_cost"),
        ({"communication_cost": -1}, "communication_cost"),
        ({"risk_cost": -500}, "risk_cost"),
        ({"customer_cost": True}, "customer_cost"),
    ],
)
def test_out_of_range_figures_are_rejected_and_named(
    overrides: dict[str, object], figure: str
) -> None:
    """A figure outside its declared range names itself in the rejection.

    ``True`` is included because it is an ``int`` in Python: a cost of ``True`` would pass
    a naive non-negativity check and store as one paisa, which is the kind of bug that
    survives review.
    """
    rejection = validate_figures(raw(**overrides))
    assert isinstance(rejection, RejectedFigure)
    assert rejection.figure == figure


@pytest.mark.parametrize(
    "missing",
    [
        "probability_method",
        "financial_cost_method",
        "communication_cost_method",
        "risk_cost_method",
        "customer_cost_method",
    ],
)
def test_a_figure_with_no_recorded_method_is_rejected(missing: str) -> None:
    """R6.C12 treats a method-less figure as invalid, not as a default.

    A figure with no method is a figure nobody can weigh, and the optimizer would
    otherwise consume it alongside labelled ones with no way to tell them apart.
    """
    rejection = validate_figures(raw(**{missing: None}))
    assert isinstance(rejection, RejectedFigure)


def test_a_rejected_candidate_is_retained_as_a_neutral_unavailable_member() -> None:
    """A broken figure excludes the action from selection but not from the record.

    Forced by corrupting the configured payment link fee, which under R31.C11 is now a
    versioned configuration row and so is the realistic source of an invalid figure: a
    merchant can change it, and nothing between the row and the estimator would otherwise
    notice a negative one. The substitute claims nothing — probability equal to the
    baseline and zero costs — so it cannot be selected on its merits either, and the
    rejection is reported so an ``INVALID_ESTIMATE`` record names the figure.

    Previously this corrupted ``COST_PRIORS`` in place. That idiom stopped being able to
    express the failure once the two R31.C11 terms came from the row rather than the
    table: a patched entry would have had its financial term overwritten by the
    configured one and the candidate would have priced correctly.
    """
    config = dataclasses.replace(CONFIG, PAYMENT_LINK_FINANCIAL_COST=Minor(-1))
    baseline = Probability(Decimal("0.400"))
    candidates = build_candidate_set(
        RiskCause.INSUFFICIENT_FUNDS,
        baseline=baseline,
        remaining=timedelta(days=2),
        window=WINDOW,
        config=config,
    )
    figure = candidates.figure_for(CandidateAction.PAYMENT_LINK)
    assert figure is not None
    assert figure.availability is ActionAvailability.UNAVAILABLE
    assert figure.unavailable_reason == ExclusionReason.INVALID_ESTIMATE_INPUT.value
    assert figure.intervention_probability == baseline
    assert figure.total_cost == 0
    # Both actions that read ``PAYMENT_LINK_FINANCIAL_COST`` are rejected, and that is the
    # point rather than collateral damage: one bad row invalidates every estimate priced
    # from it, and an implementation that rejected only the first would leave the second
    # priced from a negative fee.
    assert [rejection.action for rejection in candidates.rejected] == [
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
    ]
    assert {rejection.figure for rejection in candidates.rejected} == {"financial_cost"}


def test_every_uplift_prior_belongs_to_an_executable_action() -> None:
    """An uplift is a claim about an effect, so it needs an act that can occur.

    A prior attached to an action the MVP cannot perform would be an effect claimed from
    something that never happens — and it would be read by the optimizer, which does not
    know that.
    """
    from revora.domain.actions import EXECUTABLE_ACTIONS

    assert set(UPLIFT_PRIORS) <= EXECUTABLE_ACTIONS


def test_every_action_has_a_cost_prior() -> None:
    """No action in the vocabulary is priced by omission.

    A missing entry would silently fall back to zero across all four costs, which for a
    customer-visible action would make it look free.
    """
    assert set(COST_PRIORS) == set(CandidateAction)


def test_the_two_configured_cost_rows_default_to_the_prior_table() -> None:
    """R31.C11, first half: the row and the table cannot disagree at the default.

    ``COST_PRIORS`` binds the two figures from the catalogue's declared defaults and
    migration ``0009`` seeds the rows from the same declaration, so an unchanged
    deployment prices exactly as the table says. If this ever failed it would mean the
    number had been written down twice.
    """
    for action in (CandidateAction.PAYMENT_LINK, CandidateAction.CUSTOMER_MESSAGE):
        assert cost_prior_for(action, CONFIG) == COST_PRIORS[action]


@pytest.mark.parametrize(
    "action", [CandidateAction.PAYMENT_LINK, CandidateAction.CUSTOMER_MESSAGE]
)
def test_a_changed_cost_row_changes_what_the_estimator_prices(
    action: CandidateAction,
) -> None:
    """R31.C11, second half: changing the row has a consequence.

    This is the half that seeding the rows did not close. An estimator reading the module
    constant would price identically whatever the row said, which would make "changeable
    only through a recorded configuration change naming an approving Merchant_User" a
    statement about a value nothing consumes.

    Asserted through :func:`build_candidate_set` rather than through the accessor alone,
    because the accessor being right is not the claim — the claim is that the figure the
    optimizer eventually subtracts came from the row.
    """
    config = dataclasses.replace(
        CONFIG,
        PAYMENT_LINK_FINANCIAL_COST=Minor(777),
        MESSAGE_COMMUNICATION_COST=Minor(13),
    )
    candidates = build_candidate_set(
        RiskCause.INSUFFICIENT_FUNDS,
        baseline=Probability(Decimal("0.400")),
        remaining=timedelta(days=2),
        window=WINDOW,
        config=config,
    )
    figure = candidates.figure_for(action)
    assert figure is not None
    assert int(figure.financial_cost) == 777
    assert int(figure.communication_cost) == 13
    # The two terms the requirement does not name are untouched by the change.
    assert figure.risk_cost == COST_PRIORS[action].risk_cost
    assert figure.customer_cost == COST_PRIORS[action].customer_cost


def test_the_other_seven_actions_are_priced_from_the_table_alone() -> None:
    """R31.C11 names two figures, and only those two are configuration.

    ``PROMISE_TO_PAY_FOLLOW_UP``'s financial term is a verified zero — the resend creates
    no second link — so exposing it as a row would invite somebody to tune away a fact
    about the provider's API. The rest have no requirement asking for them to change.
    """
    config = dataclasses.replace(
        CONFIG,
        PAYMENT_LINK_FINANCIAL_COST=Minor(777),
        MESSAGE_COMMUNICATION_COST=Minor(13),
    )
    unconfigured = set(CandidateAction) - {
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
    }
    for action in unconfigured:
        assert cost_prior_for(action, config) == COST_PRIORS[action], action


# ---------------------------------------------------------------------------
# Audit vocabulary and the structural claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        BASELINE_ESTIMATE_RECORDED,
        BASELINE_ESTIMATION_FAILED,
        BASELINE_ALREADY_RECORDED,
        CANDIDATE_ESTIMATES_RECORDED,
        CANDIDATE_ESTIMATES_ALREADY_RECORDED,
        CANDIDATE_ACTION_UNAVAILABLE,
        CANDIDATE_MEMORY_UNAVAILABLE,
        INVALID_ESTIMATE,
    ],
)
def test_estimation_event_types_are_declared_centrally(event_type: str) -> None:
    """Every event type this phase writes is a member of the declared catalogue.

    What turns "no component invents a type string" from a convention into a checked
    fact. A literal at a call site would pass every functional test and then answer a
    query for one spelling while missing the other.
    """
    assert event_type in ALL_EVENT_TYPES


def test_estimation_cannot_import_the_provider_or_the_llm_adapter() -> None:
    """R6.C8's zero-provider-requests claim, as a property of the import graph.

    A runtime assertion could only show that one particular run issued no request. This
    shows that no run can, because there is nothing importable to call. ``lint-imports``
    enforces the layering globally; this narrows it to the two packages that specifically
    matter and names the offending line.
    """
    offences: list[str] = []
    for path in sorted((REPO_ROOT / "revora" / "estimation").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name.startswith(package) for package in FORBIDDEN_PACKAGES):
                    offences.append(f"{path.name}:{node.lineno}: {name}")
    assert not offences, f"estimation must not import {FORBIDDEN_PACKAGES}: {offences}"

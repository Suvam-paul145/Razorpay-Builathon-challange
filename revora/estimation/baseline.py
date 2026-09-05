"""The baseline: what happens if Revora does nothing, with its uncertainty attached.

This is the denominator of every claim the product makes. ``net_recovery_value`` is
built on ``intervention - baseline``, so an overstated baseline makes Revora look
useless and an understated one makes every intervention look valuable. There is no
figure in the system whose honesty matters more, and there is no figure the MVP knows
less about.

**What this is: a calibrated prior with an explicit interval. Not a learned system.**
That sentence is the design's and it is repeated here because it has to survive contact
with the code. The estimator is a Beta-Binomial posterior over the confirmed
no-intervention recovery rate of a feature segment, with the design's weak
``alpha = beta = 1`` prior. At zero observations it returns the prior mean of 0.500 with an
interval of ``[0.025, 0.975]``, and **that interval is the output, not an embarrassment
to be tidied up**. It tells a reader the number means almost nothing, and it propagates
to the dashboard unchanged. Anything that narrowed it — a tighter prior chosen to look
credible, a suppressed interval, a rounding that pulled the bounds inward — would
convert an honest ignorance into a false measurement.

**What this deliberately is not.** It does not train on the merchant's history. R5's
preamble is right: historical outcomes reflect the merchant's past intervention, so a
model fitted to them estimates *intervened* recovery and then calls it a baseline. The
only unbiased source is the experiment's own control arm, which is why the control arm
is MVP and the fitted logistic regression, the isotonic calibration and the bootstrap
interval are all BUILD LATER. None of the three exists in this module and none of them
is stubbed here — a stub would be a place for someone to add a fit without noticing
what it would mean.

**Why the method is ``PRIOR_FALLBACK`` at every sample size.** R5.C3 requires that
label below ``MIN_SEGMENT_SAMPLE_SIZE``. The design reserves ``DETERMINISTIC`` for a
model fitted from data, and no such model is built, so recording ``DETERMINISTIC`` once
a segment crosses thirty observations would claim a fitted estimator that does not
exist. The posterior is prior-based at any ``n``; the honest label is the same at any
``n``; and what actually changes with ``n`` is the interval, which narrows visibly, and
:attr:`SelectedSegment.sample_size_satisfied`, which the dashboard can read. The
requirement is satisfied and nothing above it is misled.

**A customer who answered a message is not a no-intervention observation** (R25.C4). The
training labels come from exactly one intervention status, :data:`TRAINING_LABEL_STATUS`, and
R25.C4 moves a case into a different one: an observation carrying a Customer_Signal that arrived
after a Revora action is ``REVORA_INTERVENED``, so it never enters the ``n`` or the ``s`` of any
posterior computed here. That has teeth in the one situation where the old rule was silently
wrong. ``NO_INTERVENTION_CONFIRMED`` was granted on *zero confirmed actions plus a control-arm
assignment*, and an intent stranded in ``ATTEMPTED`` or ``UNCERTAIN`` has no confirmation — so a
control case whose message demonstrably reached somebody, because that somebody submitted a
Delay_Reason afterwards, would have become a baseline label. The classification itself is made at
write time in :func:`revora.memory.store.classify_intervention_status`, because the fact being
classified — which instant a submission arrived at relative to which attempt — is knowable in the
terminal transition and unrecoverable from an aggregate afterwards. What lives here is the label
*filter*, :func:`usable_as_training_label`, which is exhaustive over
:class:`~revora.domain.enums.InterventionStatus` so that a member added later cannot become a
training label by default.

Nothing about a Customer_Signal reaches this estimator by any other route. There is no
``delay_reason`` in :data:`~revora.domain.segments.FEATURE_KEYS`, so ``backoff_candidates``
produces no containment probe naming one and no segment this module selects can be defined by
what a customer said. The signal fields R25.C1 puts on an observation are recorded and
selectable — deliberately, so a future trainer can condition on them — and they are not
segment dimensions, which is why adding them moved no baseline.

**Failure records nothing.** R5.C11: a timeout or an unreachable memory store writes
``BASELINE_ESTIMATION_FAILED``, records **no estimate at all**, and leaves the case in
``DIAGNOSED``. This is the single most important error path in the module, because the
alternative failure mode is silent and expensive — a missing baseline defaulted to zero
makes every candidate look maximally valuable, and the pipeline downstream has no way
to tell an absent denominator from a genuinely hopeless payment. So there is no default
and no partial write. The service returns a failure, the caller declines to transition,
and the case sits in ``DIAGNOSED`` where a human or a retry can find it.

**Shape.** A pure core (:func:`select_segment`, :func:`estimate_baseline`) and a thin
service (:func:`run_baseline_estimation`) that does the I/O. The split is not
decoration: the backoff rule, the label rules and the interval are the parts that can
be wrong in a way nobody notices, and they are exactly the parts that are then testable
in microseconds with no database. The service holds the transaction, the lock, the
audit and the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from revora.audit.events import (
    BASELINE_ALREADY_RECORDED,
    BASELINE_ESTIMATE_RECORDED,
    BASELINE_ESTIMATION_FAILED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.enums import (
    EstimationMethod,
    InterventionStatus,
    Provenance,
    RiskCause,
    ValidationStatus,
)
from revora.domain.money import Minor
from revora.domain.payment_event import CanonicalPaymentEvent
from revora.domain.probability import Probability
from revora.domain.segments import (
    SegmentFeatures,
    SegmentLevel,
    backoff_candidates,
)
from revora.estimation.beta import UNIFORM_PRIOR, BetaPosterior, BetaPrior
from revora.persistence.models.estimates import BaselineEstimate
from revora.persistence.repositories.cases import (
    RecoveryCaseRepository,
    WebhookEventRepository,
)
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.estimates import (
    BaselineEstimateRepository,
    SegmentCounts,
    SegmentObservationRepository,
)
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = [
    "BASELINE_MODEL_VERSION",
    "BASELINE_PROBABILITY_PLACES",
    "FAILURE_CASE_MISSING",
    "FAILURE_MEMORY_UNAVAILABLE",
    "FAILURE_NO_ACTIVE_DIAGNOSIS",
    "FAILURE_TIMEOUT",
    "TRAINING_LABEL_STATUS",
    "UNCERTAINTY_UNAVAILABLE",
    "BaselineComputation",
    "BaselineFailure",
    "BaselineFigures",
    "BaselineOutcome",
    "MemoryUnavailableError",
    "SegmentLookup",
    "SelectedSegment",
    "estimate_baseline",
    "run_baseline_estimation",
    "select_segment",
    "usable_as_training_label",
]

_logger = get_logger(__name__)

_BASELINE_ACTOR: Final = "baseline_model"

BASELINE_MODEL_VERSION: Final[str] = "beta-binomial-1"
"""The estimator's version label, recorded on every estimate.

Named for what it is rather than as a bare number. A reader who finds
``beta-binomial-1`` in a training snapshot id knows immediately that no regression was
involved, which is the point of versioning the estimator at all. The ``model_version``
foreign key on the row stays ``NULL``: that column points at a ``model_version`` row
holding a trained artefact, and there is no artefact here to point at. Inventing a row
for a closed-form posterior would put a fake trained model in the table the promotion
audit reads."""

BASELINE_PROBABILITY_PLACES: Final[Decimal] = Decimal("0.001")
"""R5.C1 fixes the probability at three decimal places.

Three, not four, and it matters that this is narrower than the ``NUMERIC(6,4)`` column
holding it: the fourth place of a number derived from a handful of observations is
noise, and publishing it would invite a reader to compare two baselines on a digit that
carries no information. The value is still constructed as a ``Probability``, which
quantizes to four places, so what lands in the column is a three-place figure with a
trailing zero rather than a differently-rounded one."""

UNCERTAINTY_UNAVAILABLE: Final[str] = "UNCERTAINTY_UNAVAILABLE"
"""R5.C9's explicit alternative to an interval, recorded as a literal string.

Never produced by this module: the closed-form posterior always has quantiles, so the
MVP always has an interval. It exists because the fitted estimator that is BUILD LATER
gets its interval from a bootstrap, the bootstrap is separately optional, and the
combination "fitted model, no bootstrap run" has no interval to report. When that day
comes the answer is this string with both interval columns ``NULL`` — never a wide
guess, and never a narrow one. The schema's ``interval_present_iff_available`` check
makes the half-populated middle case uncommittable."""

TRAINING_LABEL_STATUS: Final[InterventionStatus] = (
    InterventionStatus.NO_INTERVENTION_CONFIRMED
)
"""The one intervention status a baseline training label may carry (R5.C6, R25.C4).

Named as a constant rather than written into the two places that compare against it, because
this is the single decision that separates a baseline from a description of intervened
outcomes. The segment aggregate filters on the same value in SQL, and the two agreeing is
what makes ``SegmentCounts.observations`` mean what its docstring says it means.

R25.C4 adds no member here and removes none. It changes which cases *arrive* carrying this
status — a case with a post-action Customer_Signal now arrives as ``REVORA_INTERVENED`` — and
the filter is unchanged, which is the whole shape of the change: the estimator kept drawing
from one status and the writer stopped mislabelling cases into it."""


def usable_as_training_label(intervention_status: str | InterventionStatus) -> bool:
    """Whether an observation with this status may inform a baseline posterior.

    Exhaustive by construction: it compares against :data:`TRAINING_LABEL_STATUS` and admits
    nothing else, so a fourth :class:`~revora.domain.enums.InterventionStatus` member added
    later is excluded by default rather than included by default. That direction is the point.
    A new status describing some newly-observable kind of intervention would, under a
    deny-list, silently join the training set the day it was declared — and the resulting
    baseline would look better-evidenced while being contaminated, which is the failure mode
    this whole module is arranged against.

    Accepts the string form as well as the enum because the value arrives from a ``TEXT``
    column. An unrecognized string is ``False`` rather than an error: a row written by a build
    this one does not understand is a row that must not train anything, and raising would take
    a whole estimation run down over one unfamiliar label.
    """
    if isinstance(intervention_status, InterventionStatus):
        return intervention_status is TRAINING_LABEL_STATUS
    return intervention_status == TRAINING_LABEL_STATUS.value


FAILURE_TIMEOUT: Final[str] = "ESTIMATION_TIMEOUT"
FAILURE_MEMORY_UNAVAILABLE: Final[str] = "MEMORY_UNAVAILABLE"
FAILURE_NO_ACTIVE_DIAGNOSIS: Final[str] = "NO_ACTIVE_DIAGNOSIS"
FAILURE_CASE_MISSING: Final[str] = "CASE_NOT_FOUND"
"""The four reasons no estimate is produced.

Recorded as tokens rather than sentences because the dashboard groups by them and an
operator needs to know which one is happening: a timeout is a capacity problem, an
unreachable store is an outage, a missing diagnosis is an ordering bug in the job
pipeline, and a missing case is a stale job from a wiped environment or a
merchant-scoping mistake. A single ``BASELINE_ESTIMATION_FAILED`` carrying prose would
make all four look like the same incident."""


class MemoryUnavailableError(RuntimeError):
    """The segment aggregate could not be read.

    Raised by the service's lookup wrapper and caught by the pure core, which is the
    only reason it is an exception rather than a returned value: the lookup is a
    callable the core invokes once per backoff level, and threading a failure result
    back through six levels of a loop would make the ordinary path carry the
    vocabulary of the failure path.
    """


SegmentLookup = Callable[[SegmentLevel, Mapping[str, str]], SegmentCounts]
"""How the core reads a segment. Injected rather than imported so the backoff rule is
testable against a dictionary, and so a lookup that fails is a lookup that raises
rather than a database that has to be broken on purpose."""

OutOfTime = Callable[[], bool]
"""Whether the estimation budget is spent. A predicate rather than a deadline instant
so the core needs no clock of its own, which is what keeps it pure — and so a test can
express "the budget ran out after the second level" directly instead of arranging for
real time to pass."""


# ---------------------------------------------------------------------------
# Segment selection: the backoff rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectedSegment:
    """Which backoff level the estimate was computed at, and what it held."""

    level: SegmentLevel
    segment_id: str
    counts: SegmentCounts
    sample_size_satisfied: bool
    """Whether this level reached ``MIN_SEGMENT_SAMPLE_SIZE``. False means backoff ran
    all the way to the global prior and still did not find enough confirmed
    observations, which is the ordinary state of a fresh deployment. Exposed as its own
    field rather than inferred from the level, because a *specific* level can also fail
    the threshold in principle and the two situations should not be conflated."""

    levels_examined: int
    """How many levels were queried before this one was chosen, inclusive. Recorded so
    the audit trail shows the backoff actually ran — six here means every level was
    tried and none was populated."""


def select_segment(
    features: SegmentFeatures,
    *,
    lookup: SegmentLookup,
    min_sample_size: int,
    out_of_time: OutOfTime | None = None,
) -> SelectedSegment:
    """Walk the backoff order and return the first level with enough observations.

    Args:
        features: the case's five feature values.
        lookup: reads a segment's counts. Called at most once per level, and at most
            six times.
        min_sample_size: ``Configuration.MIN_SEGMENT_SAMPLE_SIZE``. The count compared
            against is the :data:`TRAINING_LABEL_STATUS` count and nothing else —
            R5.C6 restricts training labels to that status, so counting a
            ``REVORA_INTERVENED`` observation toward the threshold would let intervened
            outcomes decide when the estimator stops calling itself a fallback. R25.C4 puts
            a case carrying a post-action Customer_Signal in that excluded group, so a
            responded-to control case no longer moves this threshold either.
        out_of_time: optional budget predicate, consulted before each query.

    Returns:
        The chosen :class:`SelectedSegment`. When no level reaches the threshold — the
        normal case early in a deployment — the last level examined is returned with
        ``sample_size_satisfied=False``, so an estimate is still produced from whatever
        the global prior holds. That is not a silent degradation: the level, the counts
        and the flag are all recorded, and the interval derived from them is wide.

    Raises:
        MemoryUnavailableError: propagated from ``lookup``.
        TimeoutError: if ``out_of_time`` reports the budget spent. Raised rather than
            returned because a partially walked backoff has no meaningful result — the
            level it stopped at was not chosen, it was merely the last one reached, and
            returning it would record a specificity the data does not support.
    """
    candidates = backoff_candidates(features)
    chosen: SelectedSegment | None = None
    for index, (level, segment_id, subset) in enumerate(candidates, start=1):
        if out_of_time is not None and out_of_time():
            raise TimeoutError(FAILURE_TIMEOUT)
        counts = lookup(level, subset)
        satisfied = counts.observations >= min_sample_size
        chosen = SelectedSegment(
            level=level,
            segment_id=segment_id,
            counts=counts,
            sample_size_satisfied=satisfied,
            levels_examined=index,
        )
        if satisfied:
            return chosen
    # Unreachable with the real backoff order, which always has six entries, but the
    # loop is written against a tuple and an empty one would otherwise return None.
    if chosen is None:  # pragma: no cover - BACKOFF_ORDER is never empty
        raise RuntimeError("backoff order produced no levels to examine")
    return chosen


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineFigures:
    """A produced baseline, with every caveat the record has to carry.

    Every field here lands in a column or in the audit record. Nothing is derived at
    read time, because the whole set of labels — method, provenance, validation status
    — describes the *conditions the estimate was produced under*, and those conditions
    are not reconstructable later from whichever model happens to be active then.
    """

    probability: Probability
    interval: tuple[Probability, Probability] | None
    posterior: BetaPosterior
    features: SegmentFeatures
    segment: SelectedSegment
    method: EstimationMethod
    provenance: Provenance
    validation_status: ValidationStatus
    training_snapshot_id: str
    model_version: str = BASELINE_MODEL_VERSION

    @property
    def uncertainty_available(self) -> bool:
        """Whether an interval exists. Drives the schema's
        ``interval_present_iff_available`` check, so it is read from the interval
        rather than stored independently — two fields that must agree are two fields
        that can disagree."""
        return self.interval is not None

    @property
    def interval_label(self) -> str:
        """The interval as a display string, or :data:`UNCERTAINTY_UNAVAILABLE`.

        For the audit record and the dashboard. The literal string is what R5.C9
        requires where no interval exists; rendering an absent interval as ``[—, —]``
        or as an empty pair would let a reader mistake absence for a value.
        """
        if self.interval is None:
            return UNCERTAINTY_UNAVAILABLE
        low, high = self.interval
        return f"[{low}, {high}]"

    def feature_document(self) -> dict[str, object]:
        """What goes in ``baseline_estimate.features``.

        The five feature values, plus the segment level, the counts behind the
        posterior and the estimator version. The column's stated purpose is that a
        later calibration report can be recomputed against exactly what the model saw
        — and the feature values alone are not that. Recomputing the posterior needs
        the ``n`` and the ``s`` it was built from, and knowing whether a deviation is
        the estimator's fault needs the level, because a global-prior estimate deviating
        from a specific segment's observed rate is expected rather than a calibration
        failure.
        """
        document: dict[str, object] = dict(self.features.as_values())
        document["segment_level"] = self.segment.level.value
        document["segment_id"] = self.segment.segment_id
        document["observations"] = self.segment.counts.observations
        document["recoveries"] = self.segment.counts.recoveries
        document["unknown_intervention"] = self.segment.counts.unknown_intervention
        document["resolved_control"] = self.segment.counts.resolved_control
        document["sample_size_satisfied"] = self.segment.sample_size_satisfied
        document["levels_examined"] = self.segment.levels_examined
        document["posterior_alpha"] = self.posterior.alpha
        document["posterior_beta"] = self.posterior.beta
        document["model_version"] = self.model_version
        # Which status the ``n`` and ``s`` above were drawn from (R5.C6, R25.C4). Recorded on
        # the estimate rather than left as a fact about the code, because a calibration report
        # recomputed years later has to know what population the counts described — and
        # "observations" alone does not say whether an intervened case was in it.
        document["training_label_status"] = TRAINING_LABEL_STATUS.value
        return document


@dataclass(frozen=True, slots=True)
class BaselineFailure:
    """No estimate was produced, and why.

    A distinct type rather than a ``BaselineFigures`` with null fields, so the service
    physically cannot write a row from a failure — the insert path takes
    :class:`BaselineFigures` and a failure has no probability to hand it. That is the
    structural form of "a missing baseline must never read as zero".
    """

    reason: str
    levels_examined: int = 0


BaselineComputation = BaselineFigures | BaselineFailure
"""What the pure core returns. The caller has to look at which one it got."""


def estimate_baseline(
    features: SegmentFeatures,
    *,
    lookup: SegmentLookup,
    min_sample_size: int,
    prior: BetaPrior = UNIFORM_PRIOR,
    case_is_synthetic: bool = False,
    out_of_time: OutOfTime | None = None,
) -> BaselineComputation:
    """Produce the baseline for one case, or report why none could be produced.

    Pure apart from the injected ``lookup`` and ``out_of_time``. No clock, no session,
    no configuration object — the two bounds it needs arrive as arguments, which is
    what makes every rule below testable without a database.

    Args:
        features: the case's five feature values.
        lookup: the segment aggregate reader.
        min_sample_size: ``Configuration.MIN_SEGMENT_SAMPLE_SIZE``.
        prior: the Beta prior. Defaults to the design's ``alpha = beta = 1``. A
            merchant-supplied prior is passed here; it is a parameter rather than a
            configuration read because the configuration catalogue has no bound for it
            yet, and inventing one in this module would put a tunable in a place
            R15.C6 cannot record an approving user for.
        case_is_synthetic: whether the case being estimated is itself synthetic.
            Combined with the segment's synthetic contributions, because R5.C4 says
            ``REAL`` requires *every* contributing observation to be real and a
            synthetic case is one of the things contributing.
        out_of_time: the budget predicate.

    Returns:
        :class:`BaselineFigures` on success, :class:`BaselineFailure` on a timeout or an
        unreachable memory store.
    """
    try:
        segment = select_segment(
            features,
            lookup=lookup,
            min_sample_size=min_sample_size,
            out_of_time=out_of_time,
        )
    except MemoryUnavailableError:
        return BaselineFailure(FAILURE_MEMORY_UNAVAILABLE)
    except TimeoutError:
        return BaselineFailure(FAILURE_TIMEOUT)

    counts = segment.counts
    posterior = prior.posterior(successes=counts.recoveries, trials=counts.observations)

    # The interval is computed under the same budget as the lookups. It is exact
    # arithmetic rather than I/O, so it will not hang, but its cost grows with the
    # sample size and a segment that has accumulated an enormous history should degrade
    # into a recorded failure rather than into a slow job holding a case row lock.
    if out_of_time is not None and out_of_time():
        return BaselineFailure(FAILURE_TIMEOUT, levels_examined=segment.levels_examined)
    interval = _recorded_interval(posterior)
    if out_of_time is not None and out_of_time():
        return BaselineFailure(FAILURE_TIMEOUT, levels_examined=segment.levels_examined)

    probability = Probability(posterior.mean(places=BASELINE_PROBABILITY_PLACES))
    synthetic = case_is_synthetic or not counts.all_synthetic_free

    return BaselineFigures(
        probability=probability,
        interval=interval,
        posterior=posterior,
        features=features,
        segment=segment,
        method=EstimationMethod.PRIOR_FALLBACK,
        provenance=Provenance.SYNTHETIC if synthetic else Provenance.REAL,
        validation_status=_validation_status(counts),
        training_snapshot_id=_snapshot_id(segment),
    )


def _recorded_interval(posterior: BetaPosterior) -> tuple[Probability, Probability]:
    """The 95 percent interval, rounded outward to the recorded precision.

    ``beta.central_interval`` already rounds outward onto a four-place grid; this
    rounds outward again onto the three places the probability itself is recorded at,
    so the bound and the point estimate can be read on the same scale. Floor for the
    lower bound and ceiling for the upper, never nearest: quantization must not be able
    to make the published interval narrower than the posterior supports, which would be
    the one rounding error in this system that overstates knowledge rather than
    misplacing a paisa.
    """
    low, high = posterior.interval()
    return (
        Probability(low.quantize(BASELINE_PROBABILITY_PLACES, rounding=ROUND_FLOOR)),
        Probability(high.quantize(BASELINE_PROBABILITY_PLACES, rounding=ROUND_CEILING)),
    )


def _validation_status(counts: SegmentCounts) -> ValidationStatus:
    """How much this segment's estimate has been checked against observed outcomes.

    ``UNVALIDATED_BASELINE`` while the segment holds no resolved control-arm
    observation (R5.C12): with nothing observed under no intervention, there is
    literally nothing the estimate could have been wrong about yet.

    Once control observations exist the status becomes ``CALIBRATION_UNVERIFIED``
    rather than ``VALIDATED``, and that is not hedging. ``VALIDATED`` would assert that
    a calibration report compared predicted against observed and found the deviation
    inside ``CALIBRATION_TOLERANCE``. The calibration report is a separate, optional
    component that is not built. Data existing is not the same as data having been
    checked, and ``CALIBRATION_UNVERIFIED`` is the enum member that says exactly that.
    """
    if counts.resolved_control <= 0:
        return ValidationStatus.UNVALIDATED_BASELINE
    return ValidationStatus.CALIBRATION_UNVERIFIED


def _snapshot_id(segment: SelectedSegment) -> str:
    """The training-data snapshot identifier R5.C2 requires.

    A content descriptor — estimator version, segment id, ``n`` and ``s`` — rather than
    a pointer into a frozen table, because no snapshot table exists: the posterior is
    computed from an aggregate at estimation time, and materializing a copy of the
    observation set for every case would multiply the memory table by the case count
    for no gain. The identifier still discharges what the requirement is for, which is
    reproducibility: given the same aggregate, the estimate is reconstructable exactly,
    and a snapshot id that differs tells you the aggregate moved.

    When the fitted estimator arrives it trains against a genuinely frozen set and this
    becomes a reference to that set's id. The column is ``TEXT`` for that reason.
    """
    counts = segment.counts
    return (
        f"{BASELINE_MODEL_VERSION}/{segment.segment_id}"
        f"/n{counts.observations}/s{counts.recoveries}"
    )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineOutcome:
    """What one baseline estimation run did, and what the caller must do next.

    The caller is the job handler, and it owns the one thing this service deliberately
    does not: the state transition. On failure it must **not** transition — R5.C11
    leaves the case in ``DIAGNOSED`` — which is why ``failure_reason`` and
    ``requires_candidate_estimation`` are separate fields rather than one being derived
    from the other at the call site.
    """

    baseline_estimate_id: uuid.UUID | None
    probability: Probability | None
    interval: tuple[Probability, Probability] | None
    segment_id: str | None
    segment_level: SegmentLevel | None
    method: EstimationMethod | None
    provenance: Provenance | None
    validation_status: ValidationStatus | None
    training_snapshot_id: str | None
    observations: int
    recoveries: int
    sample_size_satisfied: bool
    failure_reason: str | None
    requires_candidate_estimation: bool
    case_version: int | None
    already_recorded: bool = False


def run_baseline_estimation(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
) -> BaselineOutcome:
    """Produce and persist the baseline for one case's current decision cycle.

    Must be called inside a transaction; it commits nothing itself. The worker's job
    handler owns the transaction so that the estimate row and its audit record are
    atomic with each other — an estimate with no audit record explaining what it was
    computed from is not a state this can be left in.

    Takes the case row under ``FOR UPDATE``, for the same two reasons diagnosis does:
    the audit writer allocates its gap-free sequence from a counter on that row and
    requires the lock to be held already, and the lock serializes a concurrent second
    estimation job onto the existing-estimate check rather than onto a second insert.

    On failure it writes ``BASELINE_ESTIMATION_FAILED`` and inserts nothing. The audit
    record is written on the same session; the memory reads that can fail are each
    wrapped in a savepoint, so a database error rolls back to the savepoint and leaves
    the session usable for exactly that write. Without the savepoint the failure would
    poison the transaction and the audit record explaining it could not be persisted —
    which is the one outcome worse than the failure itself.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        # No case row means no audit sequence to allocate against and no case for the
        # record to belong to, so this is the one failure that is logged rather than
        # audited. Retrying will not help either, which is why the outcome still
        # reports a failure the handler completes on instead of raising.
        _logger.warning("baseline estimation for missing case", case_id=str(case_id))
        return _failed_outcome(FAILURE_CASE_MISSING, case_version=None)

    started = now()
    deadline = started + config.BASELINE_ESTIMATION_TIMEOUT
    decision_cycle = case.decision_cycle_count
    baselines = BaselineEstimateRepository(session)
    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    existing = baselines.for_cycle(merchant_id, case_id, decision_cycle)
    if existing is not None:
        return _already_recorded(
            writer, merchant_id, case_id, existing, decision_cycle, correlation_id, started
        )

    diagnosis = DiagnosisRepository(session).active_for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if diagnosis is None:
        # R5.C1 is triggered by the case entering DIAGNOSED, so an active diagnosis for
        # this cycle is a precondition, not an input to be defaulted. Estimating from a
        # guessed cause would produce a segment nobody chose and a candidate set built
        # from the wrong eligibility row.
        _write_failure(
            writer,
            merchant_id,
            case_id,
            reason=FAILURE_NO_ACTIVE_DIAGNOSIS,
            decision_cycle=decision_cycle,
            levels_examined=0,
            correlation_id=correlation_id,
            moment=started,
        )
        return _failed_outcome(FAILURE_NO_ACTIVE_DIAGNOSIS, case_version=case.version)

    canonical = _canonical_for_case(session, merchant_id, case.source_event_id)
    features = SegmentFeatures.derive(
        risk_cause=RiskCause(diagnosis.cause),
        amount=Minor(int(case.payment_amount)),
        payment_method=canonical.method,
        executed_action_count=int(case.executed_action_count),
        error_source=canonical.error_source,
    )

    observations = SegmentObservationRepository(session)

    def lookup(level: SegmentLevel, subset: Mapping[str, str]) -> SegmentCounts:
        """Read one level's counts, translating a database failure into R5.C11's path.

        The savepoint is what keeps the failure recoverable. ``level`` is unused in the
        query — containment on the feature subset is the whole predicate — but it is in
        the signature because the core passes it and a lookup that wanted to cache or
        log per level should not have to reconstruct it.
        """
        try:
            with session.begin_nested():
                return observations.segment_counts(merchant_id, features=subset)
        except SQLAlchemyError as exc:
            _logger.warning(
                "segment aggregate unavailable",
                case_id=str(case_id),
                segment_level=level.value,
            )
            raise MemoryUnavailableError(str(exc)) from exc

    computation = estimate_baseline(
        features,
        lookup=lookup,
        min_sample_size=config.MIN_SEGMENT_SAMPLE_SIZE,
        case_is_synthetic=case.provenance == Provenance.SYNTHETIC.value,
        out_of_time=lambda: now() > deadline,
    )

    if isinstance(computation, BaselineFailure):
        _write_failure(
            writer,
            merchant_id,
            case_id,
            reason=computation.reason,
            decision_cycle=decision_cycle,
            levels_examined=computation.levels_examined,
            correlation_id=correlation_id,
            moment=now(),
        )
        return _failed_outcome(computation.reason, case_version=case.version)

    row = baselines.insert(
        merchant_id,
        values={
            "case_id": case_id,
            "decision_cycle": decision_cycle,
            "probability": computation.probability.value,
            "ci_low": computation.interval[0].value if computation.interval else None,
            "ci_high": computation.interval[1].value if computation.interval else None,
            "uncertainty_available": computation.uncertainty_available,
            "segment_id": computation.segment.segment_id,
            "features": computation.feature_document(),
            # Deliberately null: see BASELINE_MODEL_VERSION. There is no trained
            # artefact for a closed-form posterior to point at.
            "model_version_id": None,
            "method": computation.method.value,
            "provenance": computation.provenance.value,
            "validation_status": computation.validation_status.value,
            "training_snapshot_id": computation.training_snapshot_id,
        },
    )

    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=BASELINE_ESTIMATE_RECORDED,
            actor=_BASELINE_ACTOR,
            evidence={
                "decision_cycle": decision_cycle,
                "baseline_probability": str(computation.probability),
                "interval": computation.interval_label,
                "segment_id": computation.segment.segment_id,
                "segment_level": computation.segment.level.value,
                "levels_examined": computation.segment.levels_examined,
                "observations": computation.segment.counts.observations,
                "recoveries": computation.segment.counts.recoveries,
                "unknown_intervention_share": str(
                    computation.segment.counts.unknown_share()
                ),
                "sample_size_satisfied": computation.segment.sample_size_satisfied,
                # R25.C4, in the record a reviewer reads: the labels behind this estimate came
                # from one status, and an observation carrying a post-action Customer_Signal
                # does not carry it.
                "training_label_status": TRAINING_LABEL_STATUS.value,
                "method": computation.method.value,
                "provenance": computation.provenance.value,
                "validation_status": computation.validation_status.value,
                "model_version": computation.model_version,
                "training_snapshot_id": computation.training_snapshot_id,
                "features": computation.features.as_values(),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=now(),
    )

    return BaselineOutcome(
        baseline_estimate_id=row.id,
        probability=computation.probability,
        interval=computation.interval,
        segment_id=computation.segment.segment_id,
        segment_level=computation.segment.level,
        method=computation.method,
        provenance=computation.provenance,
        validation_status=computation.validation_status,
        training_snapshot_id=computation.training_snapshot_id,
        observations=computation.segment.counts.observations,
        recoveries=computation.segment.counts.recoveries,
        sample_size_satisfied=computation.segment.sample_size_satisfied,
        failure_reason=None,
        requires_candidate_estimation=True,
        case_version=case.version,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _canonical_for_case(
    session: Session, merchant_id: uuid.UUID, source_event_id: uuid.UUID | None
) -> CanonicalPaymentEvent:
    """The PII-free canonical event two of the five features are read off.

    A case with no reachable source event returns an empty canonical rather than
    raising, exactly as the diagnosis service does. The consequence is an ``OTHER``
    payment method and an ``UNSTATED`` error source — two honest bands that say "not
    observed" — and the backoff drops both before anything else, so a degenerate case
    lands in a more general segment instead of failing a job forever.
    """
    if source_event_id is None:
        return CanonicalPaymentEvent(event_name="")
    event = WebhookEventRepository(session).get(merchant_id, source_event_id)
    if event is None:
        _logger.warning(
            "baseline source event missing", source_event_id=str(source_event_id)
        )
        return CanonicalPaymentEvent(event_name="")
    return CanonicalPaymentEvent.from_dict(event.canonical)


def _write_failure(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    reason: str,
    decision_cycle: int,
    levels_examined: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """Record that no estimate was produced, naming the reason.

    The record carries no probability field at all — not a null one, not a zero. The
    requirement is that a missing baseline never reads as zero, and the cheapest way to
    guarantee that is for the number to be absent from the record as well as from the
    table.
    """
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=BASELINE_ESTIMATION_FAILED,
            actor=_BASELINE_ACTOR,
            evidence={
                "failure_reason": reason,
                "decision_cycle": decision_cycle,
                "levels_examined": levels_examined,
                "estimate_recorded": False,
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _failed_outcome(reason: str, *, case_version: int | None) -> BaselineOutcome:
    """The outcome for a run that produced nothing.

    Every estimate-bearing field is ``None`` and ``requires_candidate_estimation`` is
    false, so a caller that ignores ``failure_reason`` still cannot proceed to the
    candidate set or to a transition — it has no baseline id to hang candidates off.
    """
    return BaselineOutcome(
        baseline_estimate_id=None,
        probability=None,
        interval=None,
        segment_id=None,
        segment_level=None,
        method=None,
        provenance=None,
        validation_status=None,
        training_snapshot_id=None,
        observations=0,
        recoveries=0,
        sample_size_satisfied=False,
        failure_reason=reason,
        requires_candidate_estimation=False,
        case_version=case_version,
    )


def _already_recorded(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    existing: BaselineEstimate,
    decision_cycle: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> BaselineOutcome:
    """Report the baseline already recorded for this cycle, changing nothing.

    The idempotent answer for a retried job. It reports the *stored* row's figures
    rather than recomputing, because the candidate set and the recommendation are built
    on the persisted baseline and a caller told a different number would compare
    against a denominator that was never written down.
    """
    features: Mapping[str, object] = existing.features or {}
    interval = (
        (Probability(existing.ci_low), Probability(existing.ci_high))
        if existing.ci_low is not None and existing.ci_high is not None
        else None
    )
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=BASELINE_ALREADY_RECORDED,
            actor=_BASELINE_ACTOR,
            evidence={
                "decision_cycle": decision_cycle,
                "existing_baseline_estimate_id": str(existing.id),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )
    return BaselineOutcome(
        baseline_estimate_id=existing.id,
        probability=Probability(existing.probability),
        interval=interval,
        segment_id=existing.segment_id,
        segment_level=_level_from_document(features),
        method=EstimationMethod(existing.method),
        provenance=Provenance(existing.provenance),
        validation_status=ValidationStatus(existing.validation_status),
        training_snapshot_id=existing.training_snapshot_id,
        observations=_int_from_document(features, "observations"),
        recoveries=_int_from_document(features, "recoveries"),
        sample_size_satisfied=features.get("sample_size_satisfied") is True,
        failure_reason=None,
        requires_candidate_estimation=True,
        case_version=None,
        already_recorded=True,
    )


def _level_from_document(features: Mapping[str, object]) -> SegmentLevel | None:
    """Read the backoff level back out of a stored feature document.

    Tolerant of an absent or unrecognized value: a row written by an earlier build,
    or one whose level name has since been renamed, should still be reportable as an
    existing baseline rather than crashing a retried job on a field the caller mostly
    uses for display.
    """
    raw = features.get("segment_level")
    if not isinstance(raw, str):
        return None
    try:
        return SegmentLevel(raw)
    except ValueError:
        return None


def _int_from_document(features: Mapping[str, object], key: str) -> int:
    value = features.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

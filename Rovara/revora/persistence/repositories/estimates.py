"""Baseline and candidate estimate rows, and the segment aggregate they are built on.

Three repositories, and the third is the one that needs explaining.

:class:`BaselineEstimateRepository` and :class:`CandidateEstimateRepository` are
ordinary merchant-scoped readers and writers with one thing worth noting: both expose
an existence read keyed on the decision cycle, because estimation runs from a job and
a job can be retried after a worker crash. The estimate rows carry no unique index on
``(case_id, decision_cycle)`` — a re-estimation in a later cycle is legitimate and a
re-estimation in the *same* cycle is not, and the schema cannot tell those apart — so
idempotency is the service's responsibility and these reads are how it discharges it.

:class:`SegmentObservationRepository` reads ``memory_observation`` rather than an
estimate table, and it lives here rather than in a repository of its own because that
table is the baseline's **training-label source** and nothing else in this phase reads
it. ``revora.memory`` sits in the same architectural layer as ``revora.estimation``, so
the estimator cannot import it; the interface between them is the persisted row, and
this is the read side of that interface.

The aggregate answers five questions in one query, which is the point. Splitting them
would mean five round trips inside ``BASELINE_ESTIMATION_TIMEOUT`` per backoff level,
and up to six levels, which turns a 2-second budget into a real risk rather than a
formality:

* how many ``NO_INTERVENTION_CONFIRMED`` observations the segment holds — the ``n`` of
  the Beta posterior, and the count ``MIN_SEGMENT_SAMPLE_SIZE`` is compared against;
* how many of those recovered — the ``s``;
* how many carry ``SYNTHETIC`` provenance, because R5.C4 makes one synthetic
  contributor enough to mark the whole estimate synthetic;
* how many carry ``MERCHANT_INTERVENTION_UNKNOWN``, which is the intervention-bias
  signal R5.C7 reports against ``MAX_UNKNOWN_INTERVENTION_SHARE``;
* how many resolved control-arm observations exist, because R5.C12 marks an estimate
  ``UNVALIDATED_BASELINE`` until there is at least one observed no-intervention outcome
  to check it against.

**Segments are matched by JSONB containment, not by equality.** The features of an
observation are a five-key document; a backoff level is a subset of those keys. So
``features @> '{"risk_cause": "...", "amount_band": "..."}'`` matches every observation
whose features agree on the keys the level cares about and says nothing about the rest.
That is what makes backoff one parameterized query instead of six, and it is why
``estimation.segments.FEATURE_KEYS`` is documented there as an interface: a key renamed
on one side of this boundary stops matching silently, and every segment would quietly
collapse to the global prior with nothing looking broken.

**No ``float``.** Counts are integers and the shares derived from them are computed by
the caller in ``Decimal``. This module returns counts only.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy import ColumnElement, func, select

from revora.domain.actions import CandidateAction
from revora.domain.enums import ExperimentGroup, InterventionStatus, OutcomeClass, Provenance
from revora.persistence.models.estimates import BaselineEstimate, CandidateEstimate
from revora.persistence.models.learning import MemoryObservation
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = [
    "RECOVERED_OUTCOME_CLASSES",
    "SHARE_PLACES",
    "BaselineEstimateRepository",
    "CandidateEstimateRepository",
    "SegmentCounts",
    "SegmentObservationRepository",
]

RECOVERED_OUTCOME_CLASSES: Final[frozenset[str]] = frozenset(
    member.value for member in OutcomeClass
)
"""Every outcome class counts as a recovery for labelling purposes.

All three of ``NATURAL``, ``OBSERVED`` and ``ATTRIBUTED`` mean the money arrived; they
differ only in what causal claim the arrival supports. A no-intervention observation
should only ever carry ``NATURAL``, but the label is written by a different component
and treating the other two as non-recoveries would understate the baseline if that
component ever mislabels one — and understating the baseline overstates every
incremental claim built on it. Erring toward a higher baseline errs toward doing
nothing, which is the safe direction.

``NOT_ESTABLISHED`` and ``NULL`` are excluded, and deliberately so: "we have not
measured this" is not a failure to recover, so an unresolved observation is neither a
success nor a trial."""

SHARE_PLACES: Final[Decimal] = Decimal("0.0001")
"""Four places, matching the probability discipline. A share is compared against
``MAX_UNKNOWN_INTERVENTION_SHARE``, which is a ``Decimal`` bound."""


# ---------------------------------------------------------------------------
# The segment aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentCounts:
    """What one segment holds, at one backoff level.

    Counts only. Every derived quantity — the posterior, the interval, the synthetic
    flag, the share comparison — is computed by the estimator from these, so there is
    one place where a count becomes a claim and it is not in a SQL statement.
    """

    observations: int
    """``NO_INTERVENTION_CONFIRMED`` observations. The ``n`` of the posterior, and the
    only count ``MIN_SEGMENT_SAMPLE_SIZE`` is ever compared against — R5.C6 restricts
    training labels to this status and nothing else may enlarge the sample."""

    recoveries: int
    """How many of :attr:`observations` recovered. The ``s`` of the posterior."""

    synthetic_contributions: int
    """How many contributing observations came from a synthetic dataset. One is
    enough to make the whole estimate ``SYNTHETIC`` (R5.C4)."""

    unknown_intervention: int
    """``MERCHANT_INTERVENTION_UNKNOWN`` observations in the same segment. Not part of
    the posterior — they are excluded from it, which is the whole point — but their
    share is the measure of how much of this segment's history Revora simply cannot
    see."""

    resolved_control: int
    """Control-arm observations in the segment, recovered or not.

    Every one of them is by construction a *resolved* case: a memory observation is
    written once, when the case reaches a terminal state. So the presence of any
    control-arm row is the presence of an observed no-intervention outcome, which is
    what R5.C12 asks about. A control case that ended without recovery checks the
    estimate every bit as much as one that recovered — arguably more — so filtering
    these to recoveries would report a segment as unvalidated precisely when the
    evidence disagreed with the estimate.

    Zero means nothing has ever been observed against which this segment's estimate
    could be checked, which is exactly what ``UNVALIDATED_BASELINE`` records."""

    @property
    def failures(self) -> int:
        """Observations that did not recover. The ``n - s`` of the posterior."""
        return self.observations - self.recoveries

    @property
    def all_synthetic_free(self) -> bool:
        """True when every contributing observation is real, which R5.C4 requires
        before provenance may be ``REAL``."""
        return self.synthetic_contributions == 0

    @property
    def considered(self) -> int:
        """Usable plus unknown-status observations: the denominator of the share."""
        return self.observations + self.unknown_intervention

    def unknown_share(self) -> Decimal:
        """The ``MERCHANT_INTERVENTION_UNKNOWN`` share of the segment.

        Zero for an empty segment rather than undefined. A segment with no history at
        all is not an intervention-bias risk — it is a cold start, which the sample
        size and the interval already say far more loudly than a share of nothing
        would.
        """
        if self.considered <= 0:
            return Decimal("0").quantize(SHARE_PLACES)
        share = Decimal(self.unknown_intervention) / Decimal(self.considered)
        return share.quantize(SHARE_PLACES, rounding=ROUND_HALF_UP)


class SegmentObservationRepository(MerchantScopedRepository[MemoryObservation]):
    """The baseline's label source: confirmed no-intervention outcomes per segment."""

    model = MemoryObservation

    def segment_counts(
        self, merchant_id: uuid.UUID, *, features: Mapping[str, str]
    ) -> SegmentCounts:
        """Aggregate one segment, matching observations by feature containment.

        Args:
            merchant_id: required, like every read in this package.
            features: the feature subset the backoff level keys on. An empty mapping
                is legitimate and means the global level — every observation the
                merchant has — which is why containment is the right operator: the
                empty document is contained in every document, so the global level
                needs no special case in SQL.

        Returns:
            :class:`SegmentCounts`. Never ``None``: an empty segment returns zeros,
            because "this segment has no observations" is the answer the cold-start
            path is built for and raising on it would make the ordinary case an
            exception.
        """
        confirmed = MemoryObservation.intervention_status == (
            InterventionStatus.NO_INTERVENTION_CONFIRMED.value
        )
        recovered = MemoryObservation.outcome_class.in_(sorted(RECOVERED_OUTCOME_CLASSES))
        statement = (
            select(
                func.count().filter(confirmed).label("observations"),
                func.count().filter(confirmed & recovered).label("recoveries"),
                func.count()
                .filter(MemoryObservation.provenance == Provenance.SYNTHETIC.value)
                .label("synthetic"),
                func.count()
                .filter(
                    MemoryObservation.intervention_status
                    == InterventionStatus.MERCHANT_INTERVENTION_UNKNOWN.value
                )
                .label("unknown_intervention"),
                func.count()
                .filter(MemoryObservation.group == ExperimentGroup.CONTROL.value)
                .label("resolved_control"),
            )
            .select_from(MemoryObservation)
            .where(
                MemoryObservation.merchant_id == merchant_id,
                *self._feature_match(features),
            )
        )
        row = self.session.execute(statement).one()
        return SegmentCounts(
            observations=int(row.observations),
            recoveries=int(row.recoveries),
            synthetic_contributions=int(row.synthetic),
            unknown_intervention=int(row.unknown_intervention),
            resolved_control=int(row.resolved_control),
        )

    def action_observation_counts(
        self, merchant_id: uuid.UUID, *, features: Mapping[str, str]
    ) -> dict[CandidateAction, int]:
        """How many observations of each selected action the segment holds.

        Read by the candidate prior lookup for one narrow purpose: R6.C6 marks a
        candidate ``UNCALIBRATED`` where the segment holds no observation of that
        action. In the MVP every non-definitional figure is ``UNCALIBRATED`` regardless,
        because nothing is ever fitted — so this count does not currently change a
        label. It is read and recorded anyway, because it is the number that says when
        calibration becomes possible, and a count nobody collects is a count nobody
        can act on.

        Unrecognized action strings are skipped rather than raising. The column carries
        a ``CHECK`` against the enum, so a value outside it means the enum shrank after
        the row was written, and a shrinking enum should not break estimation for every
        case in the segment.
        """
        occurrences = func.count().label("occurrences")
        statement = (
            select(MemoryObservation.selected_action.label("action"), occurrences)
            .select_from(MemoryObservation)
            .where(
                MemoryObservation.merchant_id == merchant_id,
                MemoryObservation.selected_action.is_not(None),
                *self._feature_match(features),
            )
            .group_by(MemoryObservation.selected_action)
        )
        counts: dict[CandidateAction, int] = {}
        for row in self.session.execute(statement):
            try:
                action = CandidateAction(row.action)
            except ValueError:
                continue
            counts[action] = int(row.occurrences)
        return counts

    @staticmethod
    def _feature_match(features: Mapping[str, str]) -> list[ColumnElement[bool]]:
        """The containment condition for a backoff level, as ``WHERE`` conditions.

        Returned as a list so the empty mapping produces no condition at all rather
        than a tautology the planner has to reason about — the global level really is
        "every observation this merchant has", and saying that with an absent predicate
        is both clearer and cheaper than with ``features @> '{}'``.
        """
        if not features:
            return []
        return [MemoryObservation.features.contains(dict(features))]


# ---------------------------------------------------------------------------
# The estimate rows
# ---------------------------------------------------------------------------


class BaselineEstimateRepository(MerchantScopedRepository[BaselineEstimate]):
    """Writes and reads the one baseline per case per decision cycle."""

    model = BaselineEstimate

    def for_cycle(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, decision_cycle: int
    ) -> BaselineEstimate | None:
        """The baseline recorded for one decision cycle, if there is one.

        The idempotency read. Ordered newest first and limited to one rather than
        ``scalar_one_or_none``: there is no unique index enforcing one row per cycle,
        so if a race ever produced two, this returns the later one instead of raising
        inside a job that would then retry forever and produce a third.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                BaselineEstimate.case_id == case_id,
                BaselineEstimate.decision_cycle == decision_cycle,
            )
            .order_by(BaselineEstimate.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def latest_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> BaselineEstimate | None:
        """The newest baseline for a case, across cycles.

        What the dashboard and the optimizer mean by "the baseline". Ordered by
        decision cycle then creation time, because a later cycle's baseline supersedes
        an earlier one — the case has been re-diagnosed and re-estimated since.
        """
        statement = (
            self.scoped(merchant_id)
            .where(BaselineEstimate.case_id == case_id)
            .order_by(
                BaselineEstimate.decision_cycle.desc(), BaselineEstimate.created_at.desc()
            )
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> BaselineEstimate:
        """Stage one baseline row and flush it so its id is available.

        Flushed rather than left pending because the candidate rows that follow carry
        ``baseline_estimate_id`` as a ``NOT NULL`` foreign key, and the alternative —
        relying on SQLAlchemy's insert ordering — makes a correctness property of the
        schema depend on the unit of work's internal sort.
        """
        row = BaselineEstimate(**dict(values))
        self.add(merchant_id, row)
        self.session.flush()
        return row


class CandidateEstimateRepository(MerchantScopedRepository[CandidateEstimate]):
    """Writes and reads the per-action estimates hanging off one baseline."""

    model = CandidateEstimate

    def exists_for_baseline(
        self, merchant_id: uuid.UUID, baseline_estimate_id: uuid.UUID
    ) -> bool:
        """Whether candidates have already been recorded for a baseline.

        The idempotency read for the candidate job. Keyed on the baseline rather than
        on the case and cycle because ``uq_candidate_estimate_baseline_estimate_id_action``
        is what actually prevents a second set — the unique index is on the baseline,
        so that is what the check has to agree with.
        """
        statement = (
            select(func.count())
            .select_from(CandidateEstimate)
            .where(
                CandidateEstimate.merchant_id == merchant_id,
                CandidateEstimate.baseline_estimate_id == baseline_estimate_id,
            )
        )
        return int(self.session.execute(statement).scalar_one()) > 0

    def list_for_baseline(
        self, merchant_id: uuid.UUID, baseline_estimate_id: uuid.UUID
    ) -> Sequence[CandidateEstimate]:
        """Every candidate recorded against one baseline, unavailable ones included.

        The optimizer reads this and so does the dashboard, and neither may be handed
        a filtered list. R6.C9 retains an unavailable action in the recorded set
        precisely so the dashboard can show that a retry *was* considered; a read that
        quietly dropped them would undo that at the last step.

        Ordered by action so a rendered comparison is stable between requests. Not
        ordered by any figure: ranking is the optimizer's job and a repository that
        pre-sorted by net value would be making a decision.
        """
        statement = (
            self.scoped(merchant_id)
            .where(CandidateEstimate.baseline_estimate_id == baseline_estimate_id)
            .order_by(CandidateEstimate.action)
        )
        return list(self.session.execute(statement).scalars())

    def insert_all(
        self, merchant_id: uuid.UUID, *, rows: Sequence[Mapping[str, object]]
    ) -> Sequence[CandidateEstimate]:
        """Stage a whole candidate set and flush it.

        The whole set in one call, because a partial set is not a meaningful state:
        the optimizer compares candidates against each other, so four of five rows
        would produce a selection made from an incomplete comparison. The caller's
        transaction still decides whether any of it commits.
        """
        created = [CandidateEstimate(**dict(values)) for values in rows]
        for row in created:
            self.add(merchant_id, row)
        self.session.flush()
        return created

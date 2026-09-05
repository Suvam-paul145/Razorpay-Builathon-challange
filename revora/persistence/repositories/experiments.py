"""Experiment, assignment, version-freeze and result reads and writes.

Four tables, one module, because they are only ever used together: an experiment is defined,
frozen, assigned to, and analysed, and a caller doing any of those needs the others in view.

The one method worth reading twice is :meth:`ExperimentAssignmentRepository.assign_if_absent`.
It is an ``ON CONFLICT DO NOTHING`` insert, and the conflict target is ``UNIQUE (case_id)`` —
one arm per case, ever. Returning ``None`` on conflict is not a failure: it means another worker
assigned this case first, and because assignment is deterministic that worker computed the same
arm. The correct response is to carry on, not to retry.

Nothing here opens a transaction. Assignment in particular must land in the *caller's*
transaction — the one that created the case — so that a case cannot exist without its arm and an
arm cannot exist for a case that was rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import ExperimentGroup, ExperimentState
from revora.persistence.models import (
    Experiment,
    ExperimentAssignment,
    ExperimentResult,
    ExperimentVersionFreeze,
)
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = [
    "ArmCounts",
    "ExperimentAssignmentRepository",
    "ExperimentRepository",
    "ExperimentResultRepository",
    "ExperimentVersionFreezeRepository",
]


class ArmCounts:
    """Per-arm case counts, and the contaminated and excluded counts alongside them.

    A small class rather than a tuple because the four numbers are reported together and
    reading them positionally is how a contaminated count ends up presented as a case count.
    """

    __slots__ = ("contaminated", "control", "excluded", "treatment")

    def __init__(
        self, *, control: int, treatment: int, contaminated: int, excluded: int
    ) -> None:
        self.control = control
        self.treatment = treatment
        self.contaminated = contaminated
        self.excluded = excluded

    @property
    def total(self) -> int:
        return self.control + self.treatment

    def __repr__(self) -> str:
        return (
            f"ArmCounts(control={self.control}, treatment={self.treatment}, "
            f"contaminated={self.contaminated}, excluded={self.excluded})"
        )


class ExperimentRepository(MerchantScopedRepository[Experiment]):
    """Experiment definitions and their states."""

    model = Experiment

    def active(self, merchant_id: uuid.UUID) -> Experiment | None:
        """The merchant's active experiment, if one is running.

        ``first()`` on an ordered query rather than ``scalar_one_or_none``: nothing in the schema
        prevents two ``ACTIVE`` experiments, and if two ever exist, raising here would stop
        detection for every case rather than degrading one comparison. The oldest wins, so the
        choice is at least stable — and the assignment path audits which experiment it used, so
        the situation is visible in the trail rather than silent.
        """
        statement = (
            self.scoped(merchant_id)
            .where(Experiment.state == ExperimentState.ACTIVE.value)
            .order_by(Experiment.activated_at, Experiment.created_at)
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def by_name(self, merchant_id: uuid.UUID, name: str) -> Experiment | None:
        """One experiment by name — the pair the unique constraint covers."""
        statement = self.scoped(merchant_id).where(Experiment.name == name)
        return self.session.execute(statement).scalar_one_or_none()

    def insert(self, merchant_id: uuid.UUID, *, values: Mapping[str, object]) -> Experiment:
        """Stage one experiment and flush so its id is available for the version freezes."""
        row = Experiment(**values)
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def list_by_state(
        self, merchant_id: uuid.UUID, state: ExperimentState, *, limit: int = 50
    ) -> Sequence[Experiment]:
        statement = (
            self.scoped(merchant_id)
            .where(Experiment.state == state.value)
            .order_by(Experiment.created_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class ExperimentAssignmentRepository(MerchantScopedRepository[ExperimentAssignment]):
    """Which arm each case is in. Written once, never moved."""

    model = ExperimentAssignment

    def assign_if_absent(
        self,
        merchant_id: uuid.UUID,
        *,
        experiment_id: uuid.UUID,
        case_id: uuid.UUID,
        group: ExperimentGroup,
        assigned_at: datetime,
    ) -> uuid.UUID | None:
        """Claim this case for an arm, or discover it is already claimed.

        Returns the new assignment's id, or ``None`` when the case already has one.

        ``None`` is a normal outcome and requires no retry. Assignment is deterministic, so
        whoever won the race computed the same arm from the same two ids — there is nothing to
        reconcile. Letting the database arbitrate on ``UNIQUE (case_id)`` is what makes that
        true without a lock: read-then-insert would let two workers both pass the read.
        """
        statement = (
            insert(ExperimentAssignment)
            .values(
                merchant_id=merchant_id,
                experiment_id=experiment_id,
                case_id=case_id,
                group=group.value,
                assigned_at=assigned_at,
            )
            .on_conflict_do_nothing(index_elements=[ExperimentAssignment.case_id])
            .returning(ExperimentAssignment.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> ExperimentAssignment | None:
        statement = self.scoped(merchant_id).where(ExperimentAssignment.case_id == case_id)
        return self.session.execute(statement).scalar_one_or_none()

    def arm_counts(self, merchant_id: uuid.UUID, experiment_id: uuid.UUID) -> ArmCounts:
        """Per-arm counts for an experiment, excluding contaminated and excluded cases.

        The two exclusions are counted *and* reported, not quietly dropped. A lift computed
        after discarding a third of the control arm is a different claim from one that discarded
        none, and R13.C15 requires the reader be shown which happened.

        Contaminated cases are excluded from the arm counts because a control case that received
        an action is not a control observation. Keeping it in the denominator would dilute the
        control rate toward the treatment rate and understate the lift; moving it to treatment
        would be worse, since the randomization never selected it.
        """
        eligible = (
            ~ExperimentAssignment.contaminated
        ) & (~ExperimentAssignment.excluded)
        statement = (
            select(
                func.count()
                .filter(eligible & (ExperimentAssignment.group == ExperimentGroup.CONTROL.value))
                .label("control"),
                func.count()
                .filter(
                    eligible
                    & (ExperimentAssignment.group == ExperimentGroup.TREATMENT.value)
                )
                .label("treatment"),
                func.count().filter(ExperimentAssignment.contaminated).label("contaminated"),
                func.count().filter(ExperimentAssignment.excluded).label("excluded"),
            )
            .select_from(ExperimentAssignment)
            .where(
                ExperimentAssignment.merchant_id == merchant_id,
                ExperimentAssignment.experiment_id == experiment_id,
            )
        )
        row = self.session.execute(statement).one()
        return ArmCounts(
            control=int(row.control),
            treatment=int(row.treatment),
            contaminated=int(row.contaminated),
            excluded=int(row.excluded),
        )

    def case_ids_in_arm(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID, group: ExperimentGroup
    ) -> Sequence[uuid.UUID]:
        """Eligible case ids in one arm. Contaminated and excluded cases are left out."""
        statement = (
            select(ExperimentAssignment.case_id)
            .where(
                ExperimentAssignment.merchant_id == merchant_id,
                ExperimentAssignment.experiment_id == experiment_id,
                ExperimentAssignment.group == group.value,
                ~ExperimentAssignment.contaminated,
                ~ExperimentAssignment.excluded,
            )
            .order_by(ExperimentAssignment.assigned_at)
        )
        return list(self.session.execute(statement).scalars())


class ExperimentVersionFreezeRepository(MerchantScopedRepository[ExperimentVersionFreeze]):
    """The component versions an experiment is pinned to for its whole life."""

    model = ExperimentVersionFreeze

    def freeze(
        self,
        merchant_id: uuid.UUID,
        *,
        experiment_id: uuid.UUID,
        component: str,
        version_id: str,
    ) -> uuid.UUID | None:
        """Pin one component. ``None`` if it was already pinned.

        ``ON CONFLICT DO NOTHING`` on ``(experiment_id, component)``. Re-pinning must not
        overwrite: the first freeze is the one the experiment was activated under, and silently
        replacing it would let a mid-experiment promotion look like it had always been the frozen
        version — which is precisely the change the freeze exists to detect.
        """
        statement = (
            insert(ExperimentVersionFreeze)
            .values(
                merchant_id=merchant_id,
                experiment_id=experiment_id,
                component=component,
                version_id=version_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ExperimentVersionFreeze.experiment_id,
                    ExperimentVersionFreeze.component,
                ]
            )
            .returning(ExperimentVersionFreeze.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def frozen_versions(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> dict[str, str]:
        """Component to pinned version, as a mapping for direct comparison against live state."""
        statement = self.scoped(merchant_id).where(
            ExperimentVersionFreeze.experiment_id == experiment_id
        )
        return {
            str(row.component): str(row.version_id)
            for row in self.session.execute(statement).scalars()
        }


class ExperimentResultRepository(MerchantScopedRepository[ExperimentResult]):
    """Analyses of experiments. Rows accumulate; nothing is overwritten."""

    model = ExperimentResult

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> ExperimentResult:
        row = ExperimentResult(**values)
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def latest_for_experiment(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> ExperimentResult | None:
        """The newest analysis of an experiment.

        What the metrics engine reads when deciding whether an attributed claim is permitted.
        Newest by ``computed_at``, because a later analysis over more data supersedes an earlier
        one — while the earlier row stays on disk so the history of what was concluded when
        remains reconstructable.
        """
        statement = (
            self.scoped(merchant_id)
            .where(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.computed_at.desc(), ExperimentResult.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def list_for_experiment(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> Sequence[ExperimentResult]:
        """Every analysis of an experiment, oldest first — the interim-look history."""
        statement = (
            self.scoped(merchant_id)
            .where(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.computed_at)
        )
        return list(self.session.execute(statement).scalars())

"""The write side of recovery memory, and the assignment read that labels it.

The read side lives in ``repositories/estimates.py`` — ``SegmentObservationRepository``
aggregates this table into the Beta posterior's counts. The two are deliberately separate
classes over one table, because they answer different questions and are called from different
layers: the estimator asks "how many confirmed no-intervention observations does this segment
hold", and the memory writer asks "does this case already have an observation". Merging them
would put an aggregate query and a single-row insert behind one name.

``assignment_for_case`` sits here rather than in an experiment repository for one reason: the
observation writer needs the arm to compute ``intervention_status``, and that is the only
question memory asks about experiments. A repository for the experiment tables belongs with the
experiment engine, which owns creating and analysing them.

Nothing here opens a transaction. The insert is staged and flushed on the caller's session,
because the caller is the terminal state transition and the whole guarantee is that the two
commit together.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from revora.domain.enums import InterventionStatus
from revora.persistence.models import ExperimentAssignment, MemoryObservation
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["MemoryObservationRepository"]


class MemoryObservationRepository(MerchantScopedRepository[MemoryObservation]):
    """One observation per resolved case: the idempotency read, and the arm lookup."""

    model = MemoryObservation

    def for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> MemoryObservation | None:
        """The observation for a case, if it has one.

        The idempotency read, and it has to exist even though ``UNIQUE (case_id)`` would also
        prevent a duplicate. Relying on the constraint alone would mean a legitimate second
        terminal transition — reconciliation moving an expired case to ``RECOVERED`` — raised
        an ``IntegrityError`` inside the transition's transaction and rolled the transition
        back. Reading first turns that into a no-op, and the constraint stays as the backstop
        for a genuine race.
        """
        statement = self.scoped(merchant_id).where(MemoryObservation.case_id == case_id)
        return self.session.execute(statement).scalar_one_or_none()

    def assignment_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> ExperimentAssignment | None:
        """The case's experiment arm, if it was assigned to one.

        ``None`` is an ordinary answer, not a missing row to work around: a case created while
        no experiment was active is unassigned, and R13.C14 says such a case runs the baseline
        workflow rather than being quietly counted as treatment. The observation writer turns
        ``None`` into ``MERCHANT_INTERVENTION_UNKNOWN`` for exactly that reason — Revora did
        nothing, but has no basis to claim nobody else did.
        """
        statement = (
            select(ExperimentAssignment)
            .where(
                ExperimentAssignment.merchant_id == merchant_id,
                ExperimentAssignment.case_id == case_id,
            )
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def usable_label_count(self, merchant_id: uuid.UUID) -> int:
        """How many observations are usable as baseline training labels.

        Only ``NO_INTERVENTION_CONFIRMED`` counts, which is the same restriction the segment
        aggregate applies. Exposed for the composition report and for an operator asking the
        question that actually matters early in a deployment: not "how much history do we
        have" but "how much of it can the estimator learn from". Those two numbers diverge
        sharply while most cases are being treated.
        """
        statement = (
            select(func.count())
            .select_from(MemoryObservation)
            .where(
                MemoryObservation.merchant_id == merchant_id,
                MemoryObservation.intervention_status
                == InterventionStatus.NO_INTERVENTION_CONFIRMED.value,
            )
        )
        return int(self.session.execute(statement).scalar_one())

    def list_for_cases(
        self, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
    ) -> Sequence[MemoryObservation]:
        """Observations for a set of cases, for cohort-scoped reporting.

        Empty input returns empty rather than every observation the merchant has. An
        ``IN ()`` with no members is a query that silently means "everything" in some dialects
        and "nothing" in others, and a metrics figure computed over the wrong set is worse
        than one that is absent.
        """
        if not case_ids:
            return []
        statement = self.scoped(merchant_id).where(
            MemoryObservation.case_id.in_(list(case_ids))
        )
        return list(self.session.execute(statement).scalars())

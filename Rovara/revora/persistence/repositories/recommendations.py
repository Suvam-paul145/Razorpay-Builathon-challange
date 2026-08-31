"""Recommendation rows and the ranked candidates beneath them.

One thing here is worth stating plainly: :meth:`RecommendationRepository.insert_candidates`
takes the whole set in one call and there is no method to add a single candidate. That is
deliberate. A recommendation is a comparison, so a partially written candidate set is not
a partially useful record — it is a record that shows a decision made against alternatives
that appear not to have existed. The schema's
``uq_recommendation_candidate_recommendation_action`` prevents duplicates; this prevents
the other failure, which is a set that is quietly incomplete.

Reads return excluded candidates too. R7.C8 wants every rejected alternative with its
figures and its reason, and a read that filtered them would undo that at the last step —
the dashboard's whole case-detail view is the comparison, not just the winner.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select

from revora.persistence.models.estimates import Recommendation, RecommendationCandidate
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["RecommendationRepository"]


class RecommendationRepository(MerchantScopedRepository[Recommendation]):
    """The one recommendation per case per decision cycle, and its candidates."""

    model = Recommendation

    def for_cycle(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, decision_cycle: int
    ) -> Recommendation | None:
        """The recommendation recorded for one decision cycle, if there is one.

        The idempotency read for the optimizer job. ``scalar_one_or_none`` is safe here
        — unlike the estimate tables, ``uq_recommendation_case_id_decision_cycle`` makes
        a second row for one cycle uncommittable, so if this ever raised it would be
        reporting a broken invariant rather than a race.
        """
        statement = self.scoped(merchant_id).where(
            Recommendation.case_id == case_id,
            Recommendation.decision_cycle == decision_cycle,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def latest_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Recommendation | None:
        """The newest recommendation for a case, across cycles."""
        statement = (
            self.scoped(merchant_id)
            .where(Recommendation.case_id == case_id)
            .order_by(Recommendation.decision_cycle.desc(), Recommendation.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> Recommendation:
        """Stage one recommendation and flush it so its id is available.

        Flushed because the candidate rows carry ``recommendation_id`` as a ``NOT NULL``
        foreign key, and relying on the unit of work's insert ordering would make a
        schema guarantee depend on SQLAlchemy's internal sort.
        """
        row = Recommendation(**dict(values))
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def insert_candidates(
        self, merchant_id: uuid.UUID, *, rows: Sequence[Mapping[str, object]]
    ) -> Sequence[RecommendationCandidate]:
        """Stage the whole ranked candidate set. See the module docstring."""
        created = [RecommendationCandidate(**dict(values)) for values in rows]
        for row in created:
            row.merchant_id = merchant_id
            self.session.add(row)
        self.session.flush()
        return created

    def candidates_for(
        self, merchant_id: uuid.UUID, recommendation_id: uuid.UUID
    ) -> Sequence[RecommendationCandidate]:
        """Every candidate of one recommendation, survivors first then the excluded.

        Ordered by rank with the unranked last, which is the order the case-detail view
        renders: the comparison in the order the optimizer made it, then the alternatives
        it ruled out. ``NULLS LAST`` is explicit because Postgres sorts nulls first on a
        descending order and last on an ascending one, and depending on that default is
        how a rendered list quietly changes when somebody flips a sort.
        """
        statement = (
            select(RecommendationCandidate)
            .where(
                RecommendationCandidate.merchant_id == merchant_id,
                RecommendationCandidate.recommendation_id == recommendation_id,
            )
            .order_by(
                RecommendationCandidate.rank.asc().nullslast(),
                RecommendationCandidate.action,
            )
        )
        return list(self.session.execute(statement).scalars())

    def candidate_count(self, merchant_id: uuid.UUID, recommendation_id: uuid.UUID) -> int:
        """How many candidates one recommendation recorded."""
        statement = (
            select(func.count())
            .select_from(RecommendationCandidate)
            .where(
                RecommendationCandidate.merchant_id == merchant_id,
                RecommendationCandidate.recommendation_id == recommendation_id,
            )
        )
        return int(self.session.execute(statement).scalar_one())

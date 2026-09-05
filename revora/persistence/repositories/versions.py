"""Model version reads: which version is active, and what has been recorded.

Small on purpose. The interesting logic — that activation needs an approving user, that
retirement happens before promotion — lives in ``revora.memory.versions``, because those are
decisions rather than queries. This module answers questions.

``active_for_component`` is the one read that matters at runtime. A stored estimate carries a
version label, and the only way to explain a stored estimate later is for exactly one version
per component to have been active when it was produced. The partial unique index guarantees
that; this method is how a caller finds it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from revora.persistence.models import ModelPromotion, ModelVersion
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["ModelPromotionRepository", "ModelVersionRepository"]


class ModelVersionRepository(MerchantScopedRepository[ModelVersion]):
    """Trained artefacts and their states."""

    model = ModelVersion

    def active_for_component(
        self, merchant_id: uuid.UUID, component: str
    ) -> ModelVersion | None:
        """The active version for a component, if one is active.

        ``scalar_one_or_none`` rather than ``first()``, deliberately. The partial unique index
        ``one_active_model_version_per_component`` makes two active rows impossible, so a
        second row would mean the index is gone — and that is a schema fault worth raising on
        rather than quietly resolving by taking whichever row sorted first. Picking one would
        mean two different baselines in use with no way to say which produced a given estimate.
        """
        statement = self.scoped(merchant_id).where(
            ModelVersion.component == component,
            ModelVersion.state == "ACTIVE",
        )
        return self.session.execute(statement).scalar_one_or_none()

    def by_label(
        self, merchant_id: uuid.UUID, component: str, version_label: str
    ) -> ModelVersion | None:
        """One version by its component and label — the pair the unique constraint covers."""
        statement = self.scoped(merchant_id).where(
            ModelVersion.component == component,
            ModelVersion.version_label == version_label,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_component(
        self, merchant_id: uuid.UUID, component: str, *, limit: int = 50
    ) -> Sequence[ModelVersion]:
        """Every recorded version for a component, newest first."""
        statement = (
            self.scoped(merchant_id)
            .where(ModelVersion.component == component)
            .order_by(ModelVersion.created_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class ModelPromotionRepository(MerchantScopedRepository[ModelPromotion]):
    """The record of who activated what, and when."""

    model = ModelPromotion

    def list_for_version(
        self, merchant_id: uuid.UUID, model_version_id: uuid.UUID
    ) -> Sequence[ModelPromotion]:
        """Every promotion of one version, oldest first.

        Plural because a version can be promoted, retired and — in principle — promoted again
        by a later decision. The history is what answers "who decided this and when", and
        collapsing it to the latest would lose the earlier approval.
        """
        statement = (
            self.scoped(merchant_id)
            .where(ModelPromotion.model_version_id == model_version_id)
            .order_by(ModelPromotion.promoted_at)
        )
        return list(self.session.execute(statement).scalars())

    def latest(self, merchant_id: uuid.UUID, *, limit: int = 50) -> Sequence[ModelPromotion]:
        """The most recent promotions across every component.

        For the operator's question when a figure moves unexpectedly: what changed recently,
        and who approved it.
        """
        statement = (
            self.scoped(merchant_id)
            .order_by(ModelPromotion.promoted_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

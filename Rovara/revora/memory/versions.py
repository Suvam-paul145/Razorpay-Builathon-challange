"""Model versions and promotions: activation is a decision a person takes.

Training a model and activating it are separate events here, and the separation is the whole
content of this module. Recording a version produces an ``INACTIVE`` row. Making it the one the
estimator loads requires a promotion, and a promotion requires ``approving_user_id`` — which
is ``NOT NULL`` in the schema, so there is no code path that activates a model without a name
attached to it.

That is the same reasoning that puts the tunable bounds in database rows rather than
environment variables. A redeploy cannot supply an approving user. When someone asks why the
recovery numbers moved last Tuesday, the answer should be a person and an evaluation report,
not a commit.

**At most one active version per component per merchant**, enforced by a partial unique index
rather than by care in this module. Two active versions would mean two different baselines and
no way to say which produced a stored estimate — so the second promotion fails at the database
rather than producing a schema state nobody can interpret.

**The synthetic count is separate from the training count**, not added to it. A model trained
partly on generated data cannot support a real-world claim, and the split is the only way to
see that from the row. Summing them would produce a single impressive number that hides
exactly the thing a reader needs to know.

**Nothing here trains anything.** Actual retraining is out of scope — the MVP baseline is a
closed-form Beta-Binomial posterior with no artefact to promote. This module exists so that
the *governance* around a trained model is in place before there is one, because governance
retrofitted onto a running model is governance nobody applies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from revora.audit.events import MODEL_PROMOTED, MODEL_VERSION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.persistence.models import ModelPromotion, ModelVersion
from revora.persistence.repositories.versions import ModelVersionRepository
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "COMPONENT_BASELINE_MODEL",
    "COMPONENT_BASELINE_WORKFLOW",
    "COMPONENT_CANDIDATE_PRIORS",
    "COMPONENT_POLICY_RULE_SET",
    "FROZEN_COMPONENTS",
    "ModelState",
    "PromotionRefused",
    "PromotionResult",
    "promote_model_version",
    "record_model_version",
]

_logger = get_logger(__name__)

_ACTOR = "model_registry"

COMPONENT_BASELINE_MODEL = "baseline_model"
COMPONENT_CANDIDATE_PRIORS = "candidate_priors"
COMPONENT_POLICY_RULE_SET = "policy_rule_set"
COMPONENT_BASELINE_WORKFLOW = "baseline_workflow"

FROZEN_COMPONENTS: tuple[str, ...] = (
    COMPONENT_BASELINE_WORKFLOW,
    COMPONENT_POLICY_RULE_SET,
    COMPONENT_BASELINE_MODEL,
    COMPONENT_CANDIDATE_PRIORS,
)
"""The four components an experiment pins at activation (R13.C5).

Named constants rather than free strings because ``experiment_version_freeze.component`` has no
enum ``CHECK`` — a typo would create a freeze row for a component nothing ever compares
against, and the experiment would look frozen while the thing it meant to pin moved freely.
The one failure mode this list prevents is the silent one.
"""


@unique
class ModelState(StrEnum):
    """The three states a model version can hold.

    Mirrors the literal ``CHECK`` on ``model_version.state`` rather than replacing it. The
    column predates this enum and the constraint is hand-written there; defining the enum here
    gives the application one spelling without a migration to convert the constraint, and the
    constraint remains the authority.
    """

    INACTIVE = "INACTIVE"
    """Recorded and evaluable. Training completing produces this, never ``ACTIVE``."""

    ACTIVE = "ACTIVE"
    """The version the estimator loads. At most one per component per merchant."""

    RETIRED = "RETIRED"
    """Superseded. Kept rather than deleted, because stored estimates point at it and a
    dangling version label makes every historical estimate unexplainable."""


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """A completed promotion."""

    promotion_id: uuid.UUID
    model_version_id: uuid.UUID
    prior_version_id: uuid.UUID | None
    promoted_at: datetime


@unique
class PromotionRefused(StrEnum):
    """Why a promotion did not happen. Each is a refusal, not an error."""

    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    """The version is already the active one. A no-op dressed as an approval would put a
    promotion record in the log that changed nothing, and a reader auditing why the numbers
    moved would find an event that did not move them."""

    RETIRED_VERSION = "RETIRED_VERSION"
    """Reactivating a retired version is refused. Estimates recorded under it were produced by
    a model that has since been superseded, and letting it come back would make two disjoint
    periods of history claim the same version label."""


def record_model_version(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    component: str,
    version_label: str,
    config: Configuration,
    training_observation_count: int = 0,
    synthetic_observation_count: int = 0,
    metrics: dict[str, object] | None = None,
    training_snapshot_id: str | None = None,
    artifact: bytes | None = None,
    correlation_id: uuid.UUID | None = None,
) -> ModelVersion:
    """Record a trained version as ``INACTIVE``. Never activates anything.

    Must be called inside a transaction; commits nothing.

    ``INACTIVE`` is not a default that a caller may override — there is no parameter for the
    state. A function that could create an ``ACTIVE`` version would be a path to activation
    that bypasses the approving user, and R15.C6 exists precisely to close that path.

    Args:
        synthetic_observation_count: how many training observations came from generated data.
            Recorded separately from ``training_observation_count`` rather than folded into it,
            because a model with any synthetic contribution cannot support a real-world claim
            and that has to be visible from the row.
    """
    versions = ModelVersionRepository(session)
    row = ModelVersion(
        component=component,
        version_label=version_label,
        state=ModelState.INACTIVE.value,
        training_observation_count=training_observation_count,
        synthetic_observation_count=synthetic_observation_count,
        artifact=artifact,
        metrics=metrics,
        training_snapshot_id=training_snapshot_id,
    )
    versions.add(merchant_id, row)
    session.flush()

    _audit(
        session,
        merchant_id,
        config,
        event_type=MODEL_VERSION_RECORDED,
        correlation_id=correlation_id,
        detail={
            "component": component,
            "version_label": version_label,
            "state": ModelState.INACTIVE.value,
            "training_observation_count": training_observation_count,
            "synthetic_observation_count": synthetic_observation_count,
            "trained_on_synthetic_data": synthetic_observation_count > 0,
            "training_snapshot_id": training_snapshot_id,
        },
    )
    return row


def promote_model_version(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    model_version_id: uuid.UUID,
    approving_user_id: uuid.UUID,
    config: Configuration,
    evaluation_report: dict[str, object] | None = None,
    notes: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> PromotionResult | PromotionRefused:
    """Activate a version, retiring whichever was active. Records who approved it.

    Must be called inside a transaction; commits nothing.

    ``approving_user_id`` is a required argument with no default, which is the point. The
    column is ``NOT NULL`` and there is no automated caller — a promotion is something a person
    does, and the signature is where that is enforced before the database has to.

    The previously active version is moved to ``RETIRED`` in the same transaction. It is not
    deleted: stored estimates carry its version label, and removing the row would leave every
    historical estimate pointing at a version nobody can look up.
    """
    versions = ModelVersionRepository(session)
    target = versions.get(merchant_id, model_version_id)
    if target is None:
        return PromotionRefused.VERSION_NOT_FOUND

    state = ModelState(str(target.state))
    if state is ModelState.ACTIVE:
        return PromotionRefused.ALREADY_ACTIVE
    if state is ModelState.RETIRED:
        return PromotionRefused.RETIRED_VERSION

    component = str(target.component)
    previous = versions.active_for_component(merchant_id, component)

    # Retire first. The partial unique index permits one ACTIVE row per component, so
    # promoting before retiring would violate it — and relying on statement ordering inside a
    # transaction is exactly the kind of thing that works until someone reorders two lines.
    prior_version_id: uuid.UUID | None = None
    if previous is not None and previous.id != target.id:
        prior_version_id = previous.id
        previous.state = ModelState.RETIRED.value
        session.flush()

    target.state = ModelState.ACTIVE.value
    moment = now()
    promotion = ModelPromotion(
        model_version_id=target.id,
        prior_version_id=prior_version_id,
        approving_user_id=approving_user_id,
        promoted_at=moment,
        evaluation_report=evaluation_report,
        notes=notes,
    )
    session.add(promotion)
    promotion.merchant_id = merchant_id
    session.flush()

    _audit(
        session,
        merchant_id,
        config,
        event_type=MODEL_PROMOTED,
        correlation_id=correlation_id,
        detail={
            "component": component,
            "version_label": str(target.version_label),
            "model_version_id": str(target.id),
            "prior_version_id": None if prior_version_id is None else str(prior_version_id),
            "approving_user_id": str(approving_user_id),
            "trained_on_synthetic_data": int(target.synthetic_observation_count) > 0,
        },
    )
    _logger.warning(
        "model version promoted",
        merchant_id=str(merchant_id),
        component=component,
        version_label=str(target.version_label),
    )
    return PromotionResult(
        promotion_id=promotion.id,
        model_version_id=target.id,
        prior_version_id=prior_version_id,
        promoted_at=moment,
    )


def _audit(
    session: Session,
    merchant_id: uuid.UUID,
    config: Configuration,
    *,
    event_type: str,
    correlation_id: uuid.UUID | None,
    detail: dict[str, object],
) -> None:
    """Append an unattached record. A model version belongs to a merchant, not to a case.

    Unattached because the case-attached path allocates a gap-free sequence number from a case
    row, and there is no case here. A promotion affects every case estimated afterwards, which
    is the opposite of belonging to one.
    """
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_unattached(
        merchant_id,
        AuditEntry(event_type=event_type, actor=_ACTOR, decision=detail),
        correlation_id=correlation_id,
    )

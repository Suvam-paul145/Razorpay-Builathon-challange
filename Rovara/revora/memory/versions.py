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

**The composition report is the other half of that governance** (R15.C12, R25.C8).
:func:`training_set_composition` answers "what is in the training set of the version you are
about to approve", and the thing it is built around is the *zero* counts. A report of what a
training set contains is easy to read and easy to be reassured by; the question that decides
whether a promotion is safe is which populations it contains **nothing** about, because those
are the cases the promoted model will estimate from a prior it cannot check. So every
Candidate_Action with no observations is named (R15.C12) and every Delay_Reason value with no
observations is named (R25.C8) — named, as values, not summarized as a count of gaps, because
"three reasons are unobserved" is not something an approver can act on and
"``DISPUTES_THE_CHARGE`` is unobserved" is.

The report reads whole-table aggregates rather than a frozen snapshot, and that is a stated
limitation rather than an oversight: no snapshot table exists, because the MVP posterior is
computed from an aggregate at estimation time (see
``revora.estimation.baseline._snapshot_id``). ``model_version_id`` therefore scopes the report
to the observations *already trained into* that version where any have been marked, and falls
back to the merchant's whole memory where none have — which is the honest description of what a
candidate version would be trained on if it were trained today.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select

from revora.audit.events import MODEL_PROMOTED, MODEL_VERSION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import CandidateAction
from revora.domain.enums import DelayReason, PromiseStatus
from revora.memory.store import FEATURE_DELAY_REASON, FEATURE_PROMISE_STATUS
from revora.persistence.models import MemoryObservation, ModelPromotion, ModelVersion
from revora.persistence.repositories.memory import MemoryObservationRepository
from revora.persistence.repositories.versions import ModelVersionRepository
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import InstrumentedAttribute, Session
    from sqlalchemy.sql.elements import ColumnElement

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
    "TrainingSetComposition",
    "promote_model_version",
    "record_model_version",
    "training_set_composition",
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


# ---------------------------------------------------------------------------
# Training-set composition (R15.C12, R25.C8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrainingSetComposition:
    """What a candidate model version would learn from, and what it would learn nothing about.

    Frozen, and every count is an integer read off one ``GROUP BY``. No rates and no shares:
    a share invites a reader to compare two segments, and the question this report answers is
    not "which is bigger" but "is this one empty".

    The three ``*_present`` mappings hold only the values that actually occur, and the two
    ``unobserved_*`` tuples hold the values that do not. Both halves are reported rather than
    leaving the second derivable, because deriving it means the reader has to know the full
    vocabulary — and the reader is a Merchant_User approving a promotion, not somebody who has
    read :class:`~revora.domain.enums.DelayReason`.
    """

    model_version_id: uuid.UUID | None
    """The version this composition describes, or ``None`` for the merchant's whole memory.

    ``None`` is the ordinary answer today: nothing marks observations as trained-into a version
    because nothing trains, so a report for a candidate version describes what it *would* be
    trained on. See :attr:`scoped_to_version`."""

    scoped_to_version: bool
    """Whether the counts were restricted to observations already marked as trained into
    :attr:`model_version_id`.

    ``False`` where the version exists but no observation names it — which is not the same as
    an empty training set, and reporting zero everywhere would have said it was. So the report
    falls back to the whole of Recovery_Memory and says here that it did."""

    observation_count: int
    """Every observation in scope, whatever its intervention status."""

    usable_label_count: int
    """The subset the baseline may actually train on — ``NO_INTERVENTION_CONFIRMED`` only.

    Beside :attr:`observation_count` rather than instead of it, because early in a deployment
    the two differ by an order of magnitude and the gap is the single most useful number here:
    it is the answer to "we have history, why is every estimate still a fallback"."""

    by_decision_source: Mapping[str, int]
    by_risk_cause: Mapping[str, int]
    by_selected_action: Mapping[str, int]
    by_policy_verdict: Mapping[str, int]
    by_provenance: Mapping[str, int]
    """R15.C12's five groupings, each keyed by the stored string value.

    ``NOT_RECORDED`` stands in for a ``NULL`` rather than the key being dropped. A ``NULL``
    ``selected_action`` means the case ended without one being chosen, which is a real and
    common population — omitting it would make the grouping's counts fail to sum to
    :attr:`observation_count`, and a reader checking that sum is doing the right thing."""

    by_delay_reason: Mapping[str, int]
    by_promise_status: Mapping[str, int]
    """R25.C8's two additions, read from the flat feature keys
    :data:`~revora.memory.store.FEATURE_DELAY_REASON` and
    :data:`~revora.memory.store.FEATURE_PROMISE_STATUS`.

    Absent keys are not counted at all, rather than counted as ``NOT_RECORDED``. That is the
    one place these two differ from the five above, and it follows from how they are stored:
    the writer omits the key entirely for a case that stated no reason, so there is no value to
    group and the sum of this mapping is the number of observations that carry a reason. The
    complement is :attr:`observation_count` minus that sum, which is a subtraction a reader can
    do; inventing a ``NOT_RECORDED`` bucket would instead imply a stored value that is not
    there."""

    unobserved_actions: tuple[str, ...]
    """Every :class:`~revora.domain.actions.CandidateAction` holding zero observations
    (R15.C12). Sorted, so two reports of the same memory render identically."""

    unobserved_delay_reasons: tuple[str, ...]
    """Every :class:`~revora.domain.enums.DelayReason` holding zero observations (R25.C8).

    This is the tuple R25.C8 exists for. A model promoted while this is non-empty will estimate
    cases stating those reasons from a prior it has never checked against an outcome, and the
    approver should be told which reasons those are before approving rather than after."""

    unobserved_promise_statuses: tuple[str, ...]
    """Every :class:`~revora.domain.enums.PromiseStatus` holding zero observations.

    Not required by R25.C8, which names only Delay_Reason values. Reported anyway and reported
    separately, because the argument for naming the Delay_Reason gaps applies verbatim to the
    Promise_Status ones and the cost is one more tuple built from an enumeration already in
    hand. It is listed here as an addition rather than folded in with the required tuple so a
    reader checking the requirement can see which is which."""

    def as_document(self) -> dict[str, object]:
        """The report as a JSON-ready document, for an audit record or an API serializer."""
        return {
            "model_version_id": (
                None if self.model_version_id is None else str(self.model_version_id)
            ),
            "scoped_to_version": self.scoped_to_version,
            "observation_count": self.observation_count,
            "usable_label_count": self.usable_label_count,
            "by_decision_source": dict(self.by_decision_source),
            "by_risk_cause": dict(self.by_risk_cause),
            "by_selected_action": dict(self.by_selected_action),
            "by_policy_verdict": dict(self.by_policy_verdict),
            "by_provenance": dict(self.by_provenance),
            "by_delay_reason": dict(self.by_delay_reason),
            "by_promise_status": dict(self.by_promise_status),
            "unobserved_actions": list(self.unobserved_actions),
            "unobserved_delay_reasons": list(self.unobserved_delay_reasons),
            "unobserved_promise_statuses": list(self.unobserved_promise_statuses),
        }


NOT_RECORDED: Final[str] = "NOT_RECORDED"
"""The grouping key a ``NULL`` column value lands under.

A named bucket rather than a dropped row, so every mapping in
:class:`TrainingSetComposition` sums to ``observation_count`` and a reader can check it. The
alternative — omitting nulls — produces five mappings whose sums silently disagree with the
total by different amounts, and reconciling that is work the report exists to save."""


def training_set_composition(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    model_version_id: uuid.UUID | None = None,
) -> TrainingSetComposition:
    """Report the composition of a candidate version's training set. Reads only.

    ``merchant_id`` is required and every statement below is filtered on it, on the same terms
    the repository package enforces: an aggregate that leaked across tenants would put one
    merchant's recovery history in another's promotion review, and it would do it in a report
    nobody would think to check for that.

    Args:
        model_version_id: the candidate version. Where observations exist carrying it in
            ``trained_into_model_version_id``, the report is scoped to them and
            :attr:`~TrainingSetComposition.scoped_to_version` is ``True``. Where none do — the
            state of every deployment today, since nothing trains — the report covers the
            merchant's whole memory and says so, because reporting an empty training set for a
            version that simply has not been trained yet would answer a question nobody asked.
    """
    observations = MemoryObservationRepository(session)
    scope: list[ColumnElement[bool]] = [MemoryObservation.merchant_id == merchant_id]
    scoped_to_version = False
    if model_version_id is not None:
        marked = int(
            session.execute(
                select(func.count())
                .select_from(MemoryObservation)
                .where(
                    MemoryObservation.merchant_id == merchant_id,
                    MemoryObservation.trained_into_model_version_id == model_version_id,
                )
            ).scalar_one()
        )
        if marked > 0:
            scoped_to_version = True
            scope.append(
                MemoryObservation.trained_into_model_version_id == model_version_id
            )

    total = int(
        session.execute(
            select(func.count()).select_from(MemoryObservation).where(*scope)
        ).scalar_one()
    )

    by_action = _counts(session, MemoryObservation.selected_action, scope)
    by_delay_reason = _feature_counts(session, FEATURE_DELAY_REASON, scope)
    by_promise_status = _feature_counts(session, FEATURE_PROMISE_STATUS, scope)

    composition = TrainingSetComposition(
        model_version_id=model_version_id,
        scoped_to_version=scoped_to_version,
        observation_count=total,
        # Merchant-wide rather than scope-wide, deliberately: this is the estimator's own
        # count, read through the same repository method the estimator's threshold reasoning
        # uses, so the number in this report and the number that decides whether a segment is
        # a fallback are the same number rather than two similar ones.
        usable_label_count=observations.usable_label_count(merchant_id),
        by_decision_source=_counts(session, MemoryObservation.decision_source, scope),
        by_risk_cause=_counts(session, MemoryObservation.cause, scope),
        by_selected_action=by_action,
        by_policy_verdict=_counts(session, MemoryObservation.policy_verdict, scope),
        by_provenance=_counts(session, MemoryObservation.provenance, scope),
        by_delay_reason=by_delay_reason,
        by_promise_status=by_promise_status,
        unobserved_actions=_unobserved(CandidateAction, by_action),
        unobserved_delay_reasons=_unobserved(DelayReason, by_delay_reason),
        unobserved_promise_statuses=_unobserved(PromiseStatus, by_promise_status),
    )
    _logger.info(
        "training set composition reported",
        merchant_id=str(merchant_id),
        model_version_id=None if model_version_id is None else str(model_version_id),
        scoped_to_version=scoped_to_version,
        observation_count=total,
        usable_label_count=composition.usable_label_count,
        unobserved_delay_reasons=list(composition.unobserved_delay_reasons),
    )
    return composition


def _counts(
    session: Session,
    column: InstrumentedAttribute[Any],
    scope: Sequence[ColumnElement[bool]],
) -> dict[str, int]:
    """One ``GROUP BY`` over a nullable text column, with ``NULL`` folded into
    :data:`NOT_RECORDED`.

    Aggregated in SQL rather than by loading observations and counting in Python. The training
    set is the one table in this system whose row count grows without bound — one row per
    resolved case, forever — so a report that materialized it would get slower every month and
    would do it in the path a person waits on before approving a promotion.
    """
    statement = select(column, func.count()).where(*scope).group_by(column)
    return {
        (NOT_RECORDED if row[0] is None else str(row[0])): int(row[1])
        for row in session.execute(statement)
    }


def _feature_counts(
    session: Session, key: str, scope: Sequence[ColumnElement[bool]]
) -> dict[str, int]:
    """One ``GROUP BY`` over a flat JSONB feature key, counting only rows that carry it.

    ``features[key]`` as text, which is why the writer stores these two as flat string values
    rather than nested (R25.C3): a nested object could be selected by containment on the whole
    object but not grouped by one of its members without a second level of extraction, and
    grouping is what a composition report is.

    Rows without the key are excluded by the ``IS NOT NULL`` rather than counted under
    :data:`NOT_RECORDED`. See :attr:`TrainingSetComposition.by_delay_reason` — the key's absence
    is the storage form of "the customer said nothing", and inventing a bucket for it would
    imply a stored value that does not exist.
    """
    value = MemoryObservation.features[key].astext
    statement = (
        select(value, func.count())
        .where(*scope, value.is_not(None))
        .group_by(value)
    )
    return {str(row[0]): int(row[1]) for row in session.execute(statement)}


def _unobserved(vocabulary: type[StrEnum], counts: Mapping[str, int]) -> tuple[str, ...]:
    """Every member of an enumeration holding no observations, sorted.

    Derived from the enumeration rather than from a written-out list, which is the whole
    mechanism: a Delay_Reason added to :class:`~revora.domain.enums.DelayReason` next month
    appears in this tuple on its first report without anybody editing this module. A hardcoded
    vocabulary would instead omit it, and the omission would look exactly like coverage.
    """
    return tuple(
        sorted(member.value for member in vocabulary if counts.get(member.value, 0) == 0)
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

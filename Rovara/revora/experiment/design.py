"""Defining, activating and invalidating an experiment. The sample size is decided here.

Two rules, and both are about stopping a number from being decided after the fact.

**The sample size is computed at definition time and stored.** Not at analysis time, not
implicitly. Computing it afterwards is how an underpowered experiment gets reported as a
finding: the figure stops being a threshold the data has to clear and becomes a description of
whatever data arrived. ``required_sample_size_per_group`` is ``NOT NULL`` in the schema for that
reason, and this module is the only thing that fills it.

**Component versions are frozen at activation.** A mid-experiment model promotion silently
changes what the treatment arm *is*, and the measured difference then means nothing — it
compares control against two different treatments, weighted by whenever the promotion happened.
Four components are pinned: the baseline workflow, the policy rule set, the baseline model and
the candidate priors. If any of them moves while the experiment is ``ACTIVE``, the experiment is
labelled ``INVALIDATED`` and assignment stops.

Invalidation is deliberately not a silent correction. There is no way to salvage a comparison
whose treatment changed halfway through, so the honest options are to label it or to pretend it
did not happen — and the label is a row an operator can find, whereas the pretence is a number
nobody can check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from revora.audit.events import EXPERIMENT_INVALIDATED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.enums import ExperimentLabel, ExperimentState
from revora.experiment.assignment import parse_allocation_ratio
from revora.experiment.statistics import SampleSizeInputs, required_sample_size_per_group
from revora.memory.versions import FROZEN_COMPONENTS
from revora.persistence.repositories.experiments import (
    ExperimentRepository,
    ExperimentVersionFreezeRepository,
)
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "DEFAULT_PRIMARY_METRIC",
    "ExperimentDefinition",
    "FreezeDrift",
    "activate_experiment",
    "define_experiment",
    "detect_freeze_drift",
    "invalidate_experiment",
    "label_set",
]

_logger = get_logger(__name__)

_ACTOR = "experiment_engine"

DEFAULT_PRIMARY_METRIC = "recovery_rate"
"""The metric a lift is measured on unless a definition says otherwise.

Recovery rate rather than recovered revenue, and the distinction matters for the power
calculation: the sample-size formula above is for a difference of two *proportions*. A revenue
lift is a difference of means over a skewed distribution and needs a different calculation
entirely, so making revenue the primary metric without changing the formula would produce a
threshold that does not correspond to the test being run."""


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """A complete experiment definition, with its power calculation already done."""

    experiment_id: uuid.UUID
    name: str
    primary_metric: str
    required_sample_size_per_group: int
    allocation_ratio: str
    frozen_versions: dict[str, str]
    labels: tuple[str, ...]

    @property
    def can_support_attribution(self) -> bool:
        """Whether this definition is capable of supporting a causal claim at all.

        A definition carrying ``SYNTHETIC`` or ``EXPLORATORY`` cannot, no matter what the data
        does later. Checked at definition time so nobody has to discover it at analysis time
        after building a slide on it.
        """
        blocking = {ExperimentLabel.SYNTHETIC.value, ExperimentLabel.EXPLORATORY.value}
        return not blocking.intersection(self.labels)


def label_set(labels: Sequence[str] | None) -> frozenset[str]:
    """The label set as a frozenset, treating ``NULL`` as empty.

    ``experiment.labels`` is a nullable ``TEXT[]`` with no enum ``CHECK``, so callers see either
    ``None`` or a list. Normalizing in one place stops ``None`` being iterated somewhere and
    raising, and stops an absent label array reading as anything other than "no labels".
    """
    return frozenset(labels or ())


def define_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    name: str,
    config: Configuration,
    assumed_baseline_rate: Decimal,
    minimum_detectable_effect: Decimal,
    primary_metric: str = DEFAULT_PRIMARY_METRIC,
    secondary_metrics: Sequence[str] | None = None,
    eligibility: Mapping[str, object] | None = None,
    labels: Sequence[str] | None = None,
    correlation_id: uuid.UUID | None = None,
) -> ExperimentDefinition:
    """Create a ``DRAFT`` experiment with its required sample size computed.

    Must be called inside a transaction; commits nothing.

    Creates the experiment in ``DRAFT``, never ``ACTIVE``. Activation freezes versions and is a
    separate step for the same reason promotion is separate from recording a model version: the
    thing that starts assigning real cases to arms should be an explicit act, not a side effect
    of writing a definition.

    Args:
        assumed_baseline_rate: the rate the control arm is expected to reach. Required, with no
            default — it is the ``p0`` of the power calculation, and a defaulted ``p0`` would
            produce a sample size that looks computed and is not.
        minimum_detectable_effect: the smallest lift worth detecting. Also required, and also
            not defaultable: this is the single number that decides whether the experiment needs
            270 cases per arm or 6,500, and picking it silently would be picking the
            experiment's cost.
        secondary_metrics: reported but labelled ``EXPLORATORY`` and excluded from attribution
            (R13.C11). Recorded so the comparison is complete, never so it can support a claim —
            a secondary metric that happens to reach significance is the classic multiple-
            comparisons artefact.
    """
    ratio = parse_allocation_ratio(config.EXPERIMENT_ALLOCATION_RATIO)
    sample_size = required_sample_size_per_group(
        SampleSizeInputs(
            baseline_rate=assumed_baseline_rate,
            minimum_detectable_effect=minimum_detectable_effect,
            significance_level=config.EXPERIMENT_SIGNIFICANCE_LEVEL,
            power=config.EXPERIMENT_POWER,
        )
    )

    experiment = ExperimentRepository(session).insert(
        merchant_id,
        values={
            "name": name,
            "state": ExperimentState.DRAFT.value,
            "primary_metric": primary_metric,
            "secondary_metrics": list(secondary_metrics) if secondary_metrics else None,
            "eligibility": dict(eligibility) if eligibility else None,
            "allocation_ratio": str(ratio),
            "assumed_baseline_rate": assumed_baseline_rate,
            "minimum_detectable_effect": minimum_detectable_effect,
            "significance_level": config.EXPERIMENT_SIGNIFICANCE_LEVEL,
            "power": config.EXPERIMENT_POWER,
            "analysis_method": "two_proportion_normal_approximation",
            "required_sample_size_per_group": sample_size,
            "labels": list(labels) if labels else None,
        },
    )

    _logger.info(
        "experiment defined",
        merchant_id=str(merchant_id),
        name=name,
        required_sample_size_per_group=sample_size,
    )
    return ExperimentDefinition(
        experiment_id=experiment.id,
        name=name,
        primary_metric=primary_metric,
        required_sample_size_per_group=sample_size,
        allocation_ratio=str(ratio),
        frozen_versions={},
        labels=tuple(labels or ()),
    )


def activate_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    live_versions: Mapping[str, str],
    correlation_id: uuid.UUID | None = None,
) -> dict[str, str] | None:
    """Move a ``DRAFT`` experiment to ``ACTIVE``, pinning every frozen component.

    Must be called inside a transaction; commits nothing.

    Returns the frozen version map, or ``None`` if the experiment was not in ``DRAFT`` —
    activating twice would re-pin against whatever is live now, which is exactly the silent
    version change the freeze exists to catch.

    Args:
        live_versions: the current version of each component, supplied by the caller. Injected
            rather than read here because the four components live in four different places —
            the rule set is built from configuration, the baseline model is a constant in the
            estimator, the workflow is a definition — and having this module reach into all of
            them would couple experiment activation to every one of them. Any component absent
            from the mapping is pinned as ``"unversioned"``, which is honest: an unversioned
            component cannot be detected as having changed, and recording that fact is better
            than omitting the row and implying nothing was pinned.
    """
    experiments = ExperimentRepository(session)
    experiment = experiments.get(merchant_id, experiment_id)
    if experiment is None or ExperimentState(str(experiment.state)) is not ExperimentState.DRAFT:
        return None

    freezes = ExperimentVersionFreezeRepository(session)
    pinned: dict[str, str] = {}
    for component in FROZEN_COMPONENTS:
        version_id = live_versions.get(component, "unversioned")
        freezes.freeze(
            merchant_id,
            experiment_id=experiment_id,
            component=component,
            version_id=version_id,
        )
        pinned[component] = version_id

    experiment.state = ExperimentState.ACTIVE.value
    experiment.activated_at = now()
    session.flush()

    _logger.info(
        "experiment activated",
        merchant_id=str(merchant_id),
        experiment_id=str(experiment_id),
        frozen=pinned,
    )
    return pinned


@dataclass(frozen=True, slots=True)
class FreezeDrift:
    """A component whose live version no longer matches what the experiment pinned."""

    component: str
    frozen_version: str
    live_version: str

    def __str__(self) -> str:
        return f"{self.component}: frozen {self.frozen_version}, live {self.live_version}"


def detect_freeze_drift(
    session: Session,
    merchant_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    live_versions: Mapping[str, str],
) -> tuple[FreezeDrift, ...]:
    """Which pinned components have moved. Empty means the experiment is still comparable.

    Compares the frozen map against the live one. A component that is pinned but missing from
    ``live_versions`` counts as drift rather than being skipped: "we cannot currently tell what
    version is running" is not evidence that the pinned one still is, and treating an unknown as
    a match is how a comparison stays nominally valid while its treatment has changed.
    """
    frozen = ExperimentVersionFreezeRepository(session).frozen_versions(
        merchant_id, experiment_id
    )
    drift: list[FreezeDrift] = []
    for component, frozen_version in sorted(frozen.items()):
        live = live_versions.get(component)
        if live is None:
            drift.append(
                FreezeDrift(component, frozen_version, "unknown")
                if frozen_version != "unversioned"
                else FreezeDrift(component, frozen_version, "unversioned")
            )
            continue
        if live != frozen_version:
            drift.append(FreezeDrift(component, frozen_version, live))
    # An "unversioned" pin matched against an "unversioned" live value is not drift — nothing
    # was ever versioned, which the pin already records.
    return tuple(
        item
        for item in drift
        if not (item.frozen_version == "unversioned" and item.live_version == "unversioned")
    )


def invalidate_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    config: Configuration,
    drift: Sequence[FreezeDrift],
    correlation_id: uuid.UUID | None = None,
) -> bool:
    """Label an experiment ``INVALIDATED`` and stop it assigning. Returns whether it changed.

    Must be called inside a transaction; commits nothing.

    The state moves to ``ABANDONED`` as well as the label being added, and both are needed. The
    label is what disqualifies any result from supporting a causal claim; the state is what stops
    :meth:`ExperimentRepository.active` returning it, and therefore what stops new cases being
    assigned to a comparison that can no longer mean anything.

    Existing assignments are left alone. Deleting them would erase the record that those cases
    were in an experiment at all, and their observations still carry a control-arm label that is
    perfectly good evidence about what happens without intervention — the invalidation is about
    the *comparison*, not about the individual cases.
    """
    experiments = ExperimentRepository(session)
    experiment = experiments.get(merchant_id, experiment_id)
    if experiment is None:
        return False

    labels = label_set(experiment.labels)
    if ExperimentLabel.INVALIDATED.value in labels:
        return False

    experiment.labels = sorted(labels | {ExperimentLabel.INVALIDATED.value})
    experiment.state = ExperimentState.ABANDONED.value
    experiment.completed_at = now()
    session.flush()

    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_unattached(
        merchant_id,
        AuditEntry(
            event_type=EXPERIMENT_INVALIDATED,
            actor=_ACTOR,
            decision={
                "experiment_id": str(experiment_id),
                "drift": [str(item) for item in drift],
                "detail": "a frozen component changed while the experiment was active",
            },
        ),
        correlation_id=correlation_id,
    )
    _logger.error(
        "experiment invalidated by a frozen component change",
        merchant_id=str(merchant_id),
        experiment_id=str(experiment_id),
        drift=[str(item) for item in drift],
    )
    return True

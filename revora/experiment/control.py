"""The control arm: assignment at case creation, and suppression at execution.

Two halves of one idea.

**Assignment**, at case creation, before anything about the case is known. The arm is computed
from two ids and written in the transaction that created the case, so the ordering is durable
rather than a matter of scheduling.

**Suppression**, at execution, for control cases only. A control case runs the *entire* pipeline
— diagnosed, priced, ranked, policy-checked — and produces a real recommendation. That
recommendation is recorded and then withheld: no execution intent is ever created from it.

The suppression *check* is not in this module, and that is the layering contract being right
rather than inconvenient. ``revora.experiment`` and ``revora.execution`` are siblings, so neither
may import the other. The question "am I permitted to produce an external effect" belongs to the
component that produces external effects, so the engine reads the arm from persistence and
applies the rule itself, using the shared ``SUPPRESSED_BY_CONTROL_ARM`` token from
``revora.domain`` so both sides spell the reason identically. This module owns getting the case
into an arm; the engine owns honouring it.

The second half is the part that is easy to get wrong by simplifying. The obvious
implementation is to skip the pipeline for control cases, which is cheaper and destroys the
experiment's value: without a recommendation there is no counterfactual, and "what would Revora
have done here?" becomes unanswerable for exactly the cases where the answer matters. Running
the pipeline and discarding only the *effect* is what makes the control arm a record rather than
an absence. For every control case we know the action Revora wanted to take, its predicted
uplift, and what actually happened without it.

Suppression happens at the execution boundary rather than earlier for a second reason: it is one
check in one place. Scattering "unless control" through diagnosis, estimation and policy would
give five places for the condition to be forgotten, and forgetting it in any of them contaminates
the arm.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from revora.audit.events import (
    CONTROL_CONTAMINATED,
    EXPERIMENT_ASSIGNMENT_RECORDED,
    EXPERIMENT_ASSIGNMENT_SKIPPED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.enums import ExperimentGroup, ExperimentLabel
from revora.experiment.assignment import assign_group, parse_allocation_ratio
from revora.experiment.design import label_set
from revora.persistence.repositories.experiments import (
    ExperimentAssignmentRepository,
    ExperimentRepository,
)
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "AssignmentOutcome",
    "assign_case",
    "mark_contaminated",
]

_logger = get_logger(__name__)

_ACTOR = "experiment_engine"


@dataclass(frozen=True, slots=True)
class AssignmentOutcome:
    """What happened when a case was offered to an experiment."""

    assignment_id: uuid.UUID | None
    experiment_id: uuid.UUID | None
    group: ExperimentGroup | None
    reason: str | None = None

    @property
    def assigned(self) -> bool:
        return self.group is not None

    @property
    def is_control(self) -> bool:
        """Whether this case's actions must be suppressed."""
        return self.group is ExperimentGroup.CONTROL


def assign_case(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
) -> AssignmentOutcome:
    """Assign a newly created case to an arm, if an experiment is running.

    Must be called inside the transaction that created the case, and before the diagnosis job is
    enqueued. Both matter. Sharing the transaction means a case cannot exist without its arm and
    an arm cannot exist for a case that rolled back; being before the enqueue means the arm is
    durable before anything can look at the case's cause.

    Never raises. Every failure path returns an unassigned outcome and lets the case run the
    baseline workflow (R13.C14), because the alternative — propagating an error out of this
    function — would roll back the case creation itself. Losing a real payment failure because an
    experiment could not be assigned would be a bad trade: the experiment is the optional part.
    """
    experiments = ExperimentRepository(session)
    experiment = experiments.active(merchant_id)

    if experiment is None:
        return _skipped(
            session, merchant_id, config, correlation_id, reason="no active experiment"
        )

    labels = label_set(experiment.labels)
    if ExperimentLabel.INVALIDATED.value in labels:
        # Belt and braces: `invalidate_experiment` also moves the state to ABANDONED, so an
        # invalidated experiment should not be returned as active at all. Checked anyway,
        # because assigning a case to a comparison that can never mean anything wastes the case.
        return _skipped(
            session, merchant_id, config, correlation_id, reason="experiment invalidated"
        )

    try:
        ratio = parse_allocation_ratio(str(experiment.allocation_ratio))
    except ValueError as exc:
        _logger.error(
            "experiment has an unparseable allocation ratio; case left unassigned",
            experiment_id=str(experiment.id),
            detail=str(exc),
        )
        return _skipped(
            session, merchant_id, config, correlation_id, reason="unparseable allocation ratio"
        )

    group = assign_group(experiment.id, case_id, ratio)
    assignment_id = ExperimentAssignmentRepository(session).assign_if_absent(
        merchant_id,
        experiment_id=experiment.id,
        case_id=case_id,
        group=group,
        assigned_at=now(),
    )

    if assignment_id is None:
        # Another worker assigned this case first. Deterministic assignment means it computed
        # the same arm from the same two ids, so there is nothing to reconcile and nothing to
        # retry — the arm is exactly what this call would have written.
        existing = ExperimentAssignmentRepository(session).for_case(merchant_id, case_id)
        settled = None if existing is None else ExperimentGroup(str(existing.group))
        return AssignmentOutcome(
            assignment_id=None if existing is None else existing.id,
            experiment_id=experiment.id,
            group=settled,
            reason="already assigned",
        )

    _writer(session, config).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=EXPERIMENT_ASSIGNMENT_RECORDED,
            actor=_ACTOR,
            decision={
                "experiment_id": str(experiment.id),
                "group": group.value,
                "allocation_ratio": str(ratio),
                "assigned_before_diagnosis": True,
            },
        ),
        correlation_id=correlation_id,
    )
    return AssignmentOutcome(
        assignment_id=assignment_id, experiment_id=experiment.id, group=group
    )


def mark_contaminated(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    config: Configuration,
    detail: str,
    correlation_id: uuid.UUID | None = None,
) -> bool:
    """Flag a control case that received an action anyway. Returns whether it changed.

    Contamination excludes the case from every reported result, and the contaminated count is
    reported alongside the lift (R13.C15) rather than quietly netted off. A lift computed after
    discarding part of the control arm is a different claim from one that discarded none.

    Only ``contaminated`` is set — the arm itself is never changed. Moving the case to treatment
    would be worse than leaving it: the randomization never selected it, so counting it as
    treatment biases the treatment arm toward cases somebody chose to act on by hand.

    The limit of what this can detect is worth stating: a merchant phoning a customer is
    invisible to Revora. An uncontaminated control arm means "no contamination we could see",
    which is weaker than "none occurred", and no amount of code here changes that.
    """
    assignments = ExperimentAssignmentRepository(session)
    assignment = assignments.for_case(merchant_id, case_id)
    if assignment is None:
        return False
    if bool(assignment.contaminated):
        return False

    assignment.contaminated = True
    session.flush()

    _writer(session, config).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=CONTROL_CONTAMINATED,
            actor=_ACTOR,
            decision={
                "experiment_id": str(assignment.experiment_id),
                "group": str(assignment.group),
                "detail": detail,
                "excluded_from_results": True,
            },
        ),
        correlation_id=correlation_id,
    )
    _logger.warning(
        "control case contaminated and excluded from results",
        merchant_id=str(merchant_id),
        case_id=str(case_id),
        detail=detail,
    )
    return True


def _skipped(
    session: Session,
    merchant_id: uuid.UUID,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    *,
    reason: str,
) -> AssignmentOutcome:
    """No arm. The case runs the baseline workflow and is in neither group.

    Recorded unattached rather than against the case, because the case row exists but its audit
    sequence is allocated under a lock this path does not hold — and an assignment skip is
    ordinary enough that taking a lock for it would be the wrong trade. The case id travels in
    the fields.
    """
    _writer(session, config).write_unattached(
        merchant_id,
        AuditEntry(
            event_type=EXPERIMENT_ASSIGNMENT_SKIPPED,
            actor=_ACTOR,
            decision={
                "reason": reason,
                "detail": "case runs the baseline workflow and belongs to neither arm",
            },
        ),
        correlation_id=correlation_id,
    )
    return AssignmentOutcome(
        assignment_id=None, experiment_id=None, group=None, reason=reason
    )


def _writer(session: Session, config: Configuration) -> AuditWriter:
    return AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

"""Human ownership of a case. Assigning it suspends every automated action.

This is not a display preference and it is not a tag. Policy check 7 fails while ``human_owner_
user_id`` is set, so writing this column stops the pipeline from acting on the case — which makes
it a control surface, and control surfaces need locks and audit records rather than an ``UPDATE``.

**Why it lives in ``revora.cases`` rather than in the API.** The column is on ``recovery_case`` and
its meaning is part of the case lifecycle: it decides whether the next decision cycle produces an
action. The API is a transport for it, not its owner. Putting the write here also means the worker
could set it — a future auto-escalation rule that assigns a human on a high-value failure has a
function to call rather than a column to discover.

**Why it does not go through ``apply_transition``.** Taking ownership is not a state change. A
``POLICY_CHECK`` case with an owner is still in ``POLICY_CHECK``; what changed is whether the next
evaluation will approve anything. Routing it through the transition machinery would need a
self-transition to be legal for every state, which would weaken the state machine to express
something the state machine is not about.

**The case row is locked anyway.** Two operators clicking "assign to me" at the same moment must
not produce a case with one owner and two audit records claiming to have assigned it. The lock also
gives the audit sequence allocation the row it needs — ``append_for_case`` requires the caller to
already hold it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from revora.audit.events import HUMAN_OWNER_ASSIGNED, HUMAN_OWNER_RELEASED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.enums import CaseState
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.platform.config import Configuration

__all__ = ["OwnershipOutcome", "OwnershipResult", "assign_owner", "release_owner"]

_logger = get_logger(__name__)


@unique
class OwnershipOutcome(StrEnum):
    """What an ownership write did.

    Four outcomes rather than a boolean, because three of them are legitimate and the caller
    answers each differently: ``ASSIGNED`` is 200, ``ALREADY_OWNED`` is 409 with the current
    owner named, ``NOT_OWNED`` is 409 on a release, and ``CASE_TERMINAL`` is 409 with a reason.
    Collapsing them would make "somebody else already has it" indistinguishable from "it worked".
    """

    ASSIGNED = "ASSIGNED"
    RELEASED = "RELEASED"
    ALREADY_OWNED = "ALREADY_OWNED"
    NOT_OWNED = "NOT_OWNED"
    CASE_TERMINAL = "CASE_TERMINAL"
    CASE_NOT_FOUND = "CASE_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class OwnershipResult:
    """The outcome and, where relevant, who holds the case."""

    outcome: OwnershipOutcome
    owner_user_id: uuid.UUID | None = None
    assigned_at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.outcome in (OwnershipOutcome.ASSIGNED, OwnershipOutcome.RELEASED)


def assign_owner(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
    factory: sessionmaker[Session] | None = None,
) -> OwnershipResult:
    """Take ownership of a case, suspending automated action from the next cycle.

    Refuses on a terminal case. Assigning a human to a closed case would record an intention to
    intervene in something that cannot be intervened in, and the ownership column would then sit
    set forever on a row nothing reads — an ownership list that fills with finished work is an
    ownership list nobody trusts.

    Refuses on a case somebody else already owns, and names them. Silently reassigning would be
    worse than refusing: two operators would each believe they held it, and R14.C11's whole point
    is that exactly one person is responsible.
    """
    when = moment or now()
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:
            return OwnershipResult(OwnershipOutcome.CASE_NOT_FOUND)
        if CaseState(str(case.state)) in TERMINAL_STATES:
            return OwnershipResult(
                OwnershipOutcome.CASE_TERMINAL, owner_user_id=case.human_owner_user_id
            )
        if case.human_owner_user_id is not None:
            if case.human_owner_user_id == user_id:
                # Idempotent for the same user: clicking twice is not an error and must not
                # write a second audit record claiming a second assignment.
                return OwnershipResult(
                    OwnershipOutcome.ASSIGNED,
                    owner_user_id=user_id,
                    assigned_at=case.human_assigned_at,
                )
            return OwnershipResult(
                OwnershipOutcome.ALREADY_OWNED,
                owner_user_id=case.human_owner_user_id,
                assigned_at=case.human_assigned_at,
            )

        case.human_owner_user_id = user_id
        case.human_assigned_at = when
        _write(
            session,
            merchant_id,
            case_id,
            config=config,
            event_type=HUMAN_OWNER_ASSIGNED,
            user_id=user_id,
            state=str(case.state),
            correlation_id=correlation_id,
            moment=when,
            note=(
                "policy check HUMAN_OWNERSHIP will fail while this is set, so no automated "
                "action will be scheduled or executed for this case"
            ),
        )
        _logger.info(
            "case ownership assigned",
            merchant_id=str(merchant_id),
            case_id=str(case_id),
            user_id=str(user_id),
        )
        return OwnershipResult(
            OwnershipOutcome.ASSIGNED, owner_user_id=user_id, assigned_at=when
        )


def release_owner(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
    factory: sessionmaker[Session] | None = None,
) -> OwnershipResult:
    """Release ownership. Automation may resume from the next decision cycle.

    Any active user of the merchant may release, not only the holder. A single-operator deployment
    with an owner who has left would otherwise have a permanently frozen case, and "the person who
    took it must be the one to give it back" is a rule with no enforcement mechanism behind it and
    a real failure mode in front of it. The record names who released it, which is the
    accountability that actually matters.
    """
    when = moment or now()
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        if case is None:
            return OwnershipResult(OwnershipOutcome.CASE_NOT_FOUND)
        previous = case.human_owner_user_id
        if previous is None:
            return OwnershipResult(OwnershipOutcome.NOT_OWNED)

        case.human_owner_user_id = None
        case.human_assigned_at = None
        _write(
            session,
            merchant_id,
            case_id,
            config=config,
            event_type=HUMAN_OWNER_RELEASED,
            user_id=user_id,
            state=str(case.state),
            correlation_id=correlation_id,
            moment=when,
            note="automated action may resume from the next decision cycle",
            previous_owner_user_id=previous,
        )
        _logger.info(
            "case ownership released",
            merchant_id=str(merchant_id),
            case_id=str(case_id),
            user_id=str(user_id),
        )
        return OwnershipResult(OwnershipOutcome.RELEASED)


def _write(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    config: Configuration,
    event_type: str,
    user_id: uuid.UUID,
    state: str,
    correlation_id: uuid.UUID | None,
    moment: datetime,
    note: str,
    previous_owner_user_id: uuid.UUID | None = None,
) -> None:
    """Append the audit record inside the same transaction as the column change.

    Same transaction, not the same request: if the audit write fails the ownership change rolls
    back with it. A suspension of automated action that left no trail would be a case that
    mysteriously stopped producing actions, which is the hardest kind of silence to diagnose.
    """
    decision: dict[str, object] = {
        "merchant_user_id": str(user_id),
        "case_state": state,
        "note": note,
    }
    if previous_owner_user_id is not None:
        decision["previous_owner_user_id"] = str(previous_owner_user_id)
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=event_type,
            actor=f"merchant_user:{user_id}",
            decision=decision,
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )

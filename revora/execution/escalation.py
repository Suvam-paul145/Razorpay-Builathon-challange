"""Handing an execution whose outcome cannot be established to a person, once.

One function, used by both paths that can reach that conclusion:

* :mod:`revora.execution.reconcile`, when a create's reconciliation attempt bound is spent
  and the reads still have not settled whether the payment link exists;
* :mod:`revora.execution.resend`, the moment a resend lands ``UNCERTAIN`` — where the bound
  is zero attempts rather than six, because a resend response carries no identifier and no
  endpoint reports whether a notification was sent, so the attempts would be reads that
  cannot answer.

**Shared rather than written twice.** The two callers arrive here with different evidence and
different attempt counts, but the disposition is one thing — audit
``EXECUTION_RESULT_UNVERIFIABLE``, move the case to ``ESCALATED`` carrying
``TerminalReason.EXECUTION_RESULT_UNVERIFIABLE``, write the recovery-memory observation in the
same transaction, and issue no further external call for that case ever. A second copy of that
sequence is a copy that would keep the audit record and drop the observation, or terminate
without the reason, and the failure would be invisible: an escalated case looks the same in a
dashboard whether or not it was recorded correctly.

**The intent is deliberately left ``UNCERTAIN``.** Forcing it to ``FAILED`` would be a claim
nothing supports, and worse, ``FAILED`` licenses a further attempt under a new key — which is
exactly what must not happen when a payment link may be live, or a message may already have
reached a customer. So the row stays unresolved forever, and that is not a leak: for a create
the sweeper's predicate finds it only until it resolves, and for a resend the predicate never
matched it at all.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from revora.audit.events import EXECUTION_RESULT_UNVERIFIABLE
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.manager import apply_locked_transition
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, TerminalReason
from revora.memory.store import observation_writer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from sqlalchemy.orm import Session

    from revora.persistence.models import RecoveryCase
    from revora.platform.config import Configuration

__all__ = ["escalate_unverifiable"]


def escalate_unverifiable(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    action: CandidateAction,
    idempotency_key: str,
    detail: str,
    attempts: int,
    actor: str,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    moment: datetime,
    writer: AuditWriter | None = None,
) -> None:
    """Record an unverifiable execution and terminate the case with it. No further calls.

    Runs inside the caller's transaction, on a case row the caller already holds
    ``FOR UPDATE`` — the audit sequence is allocated off that row, so the lock is not optional.
    Nothing here contacts the provider, and nothing here can: the module imports no client.

    Args:
        action: the action whose outcome could not be established. Recorded on both the audit
            record and the terminal transition so the escalated case names what was attempted.
        idempotency_key: the intent's key, so a person reading the record can find the row.
        detail: why the outcome is unestablished, in one phrase. The two callers differ here
            and the difference matters to whoever picks the case up — "the reads ran out" and
            "no read can answer" ask for different next steps.
        attempts: reconciliation attempts spent. **Zero for a resend**, which is not a missing
            value: it records that the bound was zero by design rather than that a counter was
            not incremented.
        actor: the component concluding this, for the audit record.
        writer: an audit writer already constructed on this session, if the caller has one.
            Built here otherwise. Reusing it keeps one masking and truncation configuration per
            transaction instead of two that could disagree.

    The transition is skipped where the case is already ``ESCALATED``, so a second arrival at
    the same conclusion adds the record and leaves the terminal state alone. The audit record is
    written either way, because the second arrival is itself a fact worth having.
    """
    audit = writer or AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    audit.write_for_case(
        merchant_id,
        case.id,
        AuditEntry(
            event_type=EXECUTION_RESULT_UNVERIFIABLE,
            actor=actor,
            action=action.value,
            idempotency_key=idempotency_key,
            decision={"detail": detail, "attempts": attempts},
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )

    if CaseState(case.state) is CaseState.ESCALATED:
        return

    apply_locked_transition(
        session,
        merchant_id,
        case,
        expected_version=int(case.version),
        target_state=CaseState.ESCALATED,
        reason="execution result unverifiable",
        actor=actor,
        action=action,
        terminal_reason=TerminalReason.EXECUTION_RESULT_UNVERIFIABLE,
        correlation_id=correlation_id,
        # The observation is written in this transaction, not a follow-on job. A case that
        # ends without one never gets a second chance — nothing revisits a terminal case.
        on_success=observation_writer(config, correlation_id=correlation_id),
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

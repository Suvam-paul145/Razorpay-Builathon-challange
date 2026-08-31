"""``apply_transition`` — the only writer of ``recovery_case.state``.

Every state change in the system goes through this one function, and it is the
reason R2.C2-C3 and P5 are provable by a property test rather than by reading every
call site. It does five things in one transaction (R16.C1):

1. locks the case row ``FOR UPDATE``;
2. checks the caller's ``expected_version`` (optimistic concurrency, R16.C7);
3. looks up ``(from, to)`` in the derived transition table in ``domain``;
4. applies the transition's counter effects — and counters only ever increase;
5. writes the new state, the bumped version, and one ``STATE_TRANSITION`` audit
   record, with any follow-on job enqueued in the same transaction via ``on_success``.

A rejected transition changes nothing and records the rejection in a *separate*
transaction, so the rejection is durable even when the caller's own work rolls back.
The two transactions do not overlap: the attempt transaction commits and releases
the case row lock before the rejection is recorded, because the rejection audit
allocates a sequence number by updating that same row and would otherwise block on
the lock the attempt still held.

Counters are monotonic. The transition table carries the deltas as data, the
executed-action and customer-message counters move at the single edge into
``EXECUTING`` *before* the provider request, and nothing here ever decrements. A
replayed event therefore cannot reset a bound and let an extra message through
(R2.C7).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, unique

from sqlalchemy.orm import Session

from revora.audit.events import (
    ILLEGAL_TRANSITION,
    STATE_TRANSITION,
    VERSION_CONFLICT,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import CandidateAction, is_customer_visible
from revora.domain.enums import CaseState, TerminalReason
from revora.domain.transitions import is_terminal, rule_for
from revora.persistence.models import RecoveryCase
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = [
    "TransitionOutcome",
    "TransitionResult",
    "apply_locked_transition",
    "apply_transition",
]

_logger = get_logger(__name__)

OnSuccess = Callable[[Session, RecoveryCase], None]
"""Runs inside the success transaction after the state is written and before commit.
This is how a caller enqueues a follow-on job atomically with the transition it
follows — the whole reason the queue is a table (ADR-3)."""


@unique
class TransitionOutcome(StrEnum):
    """Why ``apply_transition`` did or did not change state."""

    APPLIED = "APPLIED"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    ILLEGAL = "ILLEGAL"
    CAPTURE_NOT_VERIFIED = "CAPTURE_NOT_VERIFIED"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """The result of a transition attempt.

    ``version`` is the case's version *after* the call — bumped on ``APPLIED``,
    unchanged otherwise — so a caller that means to retry a losing optimistic write
    knows what to read against without a second query.
    """

    outcome: TransitionOutcome
    case_id: uuid.UUID
    previous_state: CaseState | None = None
    new_state: CaseState | None = None
    version: int | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is TransitionOutcome.APPLIED


@dataclass(frozen=True, slots=True)
class _Rejection:
    """A transition refused in the attempt transaction, to be recorded after it
    commits and releases the case row lock."""

    outcome: TransitionOutcome
    event_type: str
    current: CaseState
    target: CaseState
    reason: str
    observed_version: int


def apply_transition(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    expected_version: int,
    target_state: CaseState,
    reason: str,
    actor: str,
    action: CandidateAction | None = None,
    terminal_reason: TerminalReason | None = None,
    verified_capture: bool = False,
    correlation_id: uuid.UUID | None = None,
    on_success: OnSuccess | None = None,
    factory: sessionmaker[Session] | None = None,
    disclosure_length: int | None = None,
    max_field_length: int | None = None,
) -> TransitionResult:
    """Attempt one legal state transition.

    Args:
        expected_version: the version the caller last read. A mismatch means another
            writer moved the case; the transition is refused with
            ``VERSION_CONFLICT`` so the caller re-reads before any external call.
        target_state: the state to move to. The edge ``(current, target)`` must be
            in the transition table.
        action: the selected action, if any. Its customer-visibility decides whether
            the customer-message counter moves on the edge into ``EXECUTING``.
        terminal_reason: recorded on the case when ``target_state`` is terminal.
        verified_capture: required by the reconciliation edge into ``RECOVERED`` from
            a non-``RECOVERED`` terminal state — recovery is declared only from an
            authoritative provider read (R2.C14).
        on_success: runs in the success transaction, for a transactional follow-on
            enqueue.

    Returns:
        A :class:`TransitionResult`. Only ``APPLIED`` changed state.
    """
    writer_kwargs = _writer_kwargs(disclosure_length, max_field_length)
    rejection: _Rejection | None = None

    # Phase one: the attempt, in its own transaction. On any non-APPLIED outcome
    # nothing but a SELECT ... FOR UPDATE has happened, so the commit on exit is a
    # no-op that releases the row lock — which the rejection record then needs.
    with tenant_transaction(merchant_id, factory) as session:
        repo = RecoveryCaseRepository(session)
        case = repo.lock_for_update(merchant_id, case_id)
        if case is None:
            _logger.warning("transition on missing case", case_id=str(case_id))
            return TransitionResult(TransitionOutcome.NOT_FOUND, case_id)

        result, rejection = apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=expected_version,
            target_state=target_state,
            reason=reason,
            actor=actor,
            action=action,
            terminal_reason=terminal_reason,
            verified_capture=verified_capture,
            correlation_id=correlation_id,
            on_success=on_success,
            disclosure_length=disclosure_length,
            max_field_length=max_field_length,
        )
        if rejection is None:
            return result

    # Phase two: record the rejection, now that the attempt has committed and the
    # row lock is released. Its own transaction, so it survives a rollback of the
    # caller's surrounding work.
    with tenant_transaction(merchant_id, factory) as session:
        AuditWriter(session, **writer_kwargs).write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=rejection.event_type,
                actor=actor,
                previous_state=rejection.current.value,
                new_state=rejection.target.value,
                decision={"reason": rejection.reason, "outcome": rejection.outcome.value},
            ),
            correlation_id=correlation_id,
        )
    return TransitionResult(
        rejection.outcome,
        case_id,
        previous_state=rejection.current,
        new_state=None,
        version=rejection.observed_version,
    )


def apply_locked_transition(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    expected_version: int,
    target_state: CaseState,
    reason: str,
    actor: str,
    action: CandidateAction | None = None,
    terminal_reason: TerminalReason | None = None,
    verified_capture: bool = False,
    correlation_id: uuid.UUID | None = None,
    on_success: OnSuccess | None = None,
    disclosure_length: int | None = None,
    max_field_length: int | None = None,
) -> tuple[TransitionResult, _Rejection | None]:
    """Apply one transition to a case row the caller already holds locked.

    The same rules as :func:`apply_transition`, minus the transaction management. Split
    out because the execution engine cannot use the wrapper: it needs the intent insert,
    this transition, the decision consumption and the audit record to reach disk in *one*
    transaction, and ``apply_transition`` opens its own — which, since it takes the case
    row ``FOR UPDATE`` on a fresh connection, would deadlock against the caller that is
    already holding that row.

    Extracted rather than reimplemented in the engine. The transition table, the counter
    effects and the ``verified_capture`` gate are the mechanism behind the customer-contact
    bounds, and a second copy of them in the module that talks to the payment provider is
    the copy that would drift.

    Args:
        session: an open, tenant-bound transaction.
        case: the case row, already held ``FOR UPDATE`` by this transaction. The caller
            holding the lock is what makes the audit sequence allocation safe.

    Returns:
        ``(result, None)`` when applied. ``(result, rejection)`` when refused, where the
        rejection carries the event type and reason for a caller that wants to record it.
        The rejection is *not* written here, because a refusal is recorded in its own
        transaction after this one releases the row lock — and a caller that is about to
        roll back may want to record something else entirely.
    """
    writer_kwargs = _writer_kwargs(disclosure_length, max_field_length)
    case_id = case.id
    current = CaseState(case.state)
    rule = rule_for(current, target_state)

    if case.version != expected_version:
        return (
            TransitionResult(
                TransitionOutcome.VERSION_CONFLICT,
                case_id,
                previous_state=current,
                version=case.version,
            ),
            _Rejection(
                TransitionOutcome.VERSION_CONFLICT,
                VERSION_CONFLICT,
                current,
                target_state,
                reason,
                case.version,
            ),
        )

    if rule is None:
        return (
            TransitionResult(
                TransitionOutcome.ILLEGAL,
                case_id,
                previous_state=current,
                version=case.version,
            ),
            _Rejection(
                TransitionOutcome.ILLEGAL,
                ILLEGAL_TRANSITION,
                current,
                target_state,
                reason,
                case.version,
            ),
        )

    if rule.requires_verified_capture and not verified_capture:
        return (
            TransitionResult(
                TransitionOutcome.CAPTURE_NOT_VERIFIED,
                case_id,
                previous_state=current,
                version=case.version,
            ),
            _Rejection(
                TransitionOutcome.CAPTURE_NOT_VERIFIED,
                ILLEGAL_TRANSITION,
                current,
                target_state,
                "reconciliation to RECOVERED requires a verified captured read",
                case.version,
            ),
        )

    # Apply. Counters move here, never down.
    effects = rule.effects
    case.executed_action_count += effects.executed_action_delta
    if (
        effects.customer_message_delta_if_visible
        and action is not None
        and is_customer_visible(action)
    ):
        case.customer_message_count += effects.customer_message_delta_if_visible
    case.decision_cycle_count += effects.decision_cycle_delta
    moment = now()
    if effects.sets_last_outbound_at:
        case.last_outbound_at = moment
    case.state = target_state.value
    case.version += 1
    if terminal_reason is not None and is_terminal(target_state):
        case.terminal_reason = terminal_reason.value

    AuditWriter(session, **writer_kwargs).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=STATE_TRANSITION,
            actor=actor,
            previous_state=current.value,
            new_state=target_state.value,
            action=action.value if action is not None else None,
            decision={"reason": reason},
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )
    new_version = case.version
    if on_success is not None:
        on_success(session, case)
    return (
        TransitionResult(
            TransitionOutcome.APPLIED,
            case_id,
            previous_state=current,
            new_state=target_state,
            version=new_version,
        ),
        None,
    )


def _writer_kwargs(
    disclosure_length: int | None, max_field_length: int | None
) -> dict[str, int]:
    kwargs: dict[str, int] = {}
    if disclosure_length is not None:
        kwargs["disclosure_length"] = disclosure_length
    if max_field_length is not None:
        kwargs["max_field_length"] = max_field_length
    return kwargs

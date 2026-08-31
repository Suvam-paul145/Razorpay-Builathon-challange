"""The execution engine. The only code in Revora that causes an external effect.

The shape of this module is one decision: **the lock does not span the HTTP call, and the
durable intent does.**

A single transaction wrapping lock, intent and call would be simpler to read and wrong in
two ways. It would hold a database connection and a row lock across a request that is
allowed to take fifteen seconds, so a provider slowdown becomes a connection-pool outage.
And it would give a false sense of safety: a lock cannot survive the process that holds it,
so a worker killed mid-call releases everything and leaves no trace — which is exactly the
state in which a second worker calls again.

So there are two transactions with a gap:

**tx-A, under an advisory lock.** Reload the case ``FOR UPDATE``, discarding everything the
job payload claimed. Re-request policy evaluation against what was actually loaded. Verify
the approval is present, matching, unexpired and unconsumed. Insert the ``ATTEMPTED``
intent, move the case to ``EXECUTING`` with its counter effects, consume the decision, audit
``EXECUTION_STARTED``. **Commit.** The lock is released here, on purpose.

**The call**, holding nothing.

**tx-B.** Re-read the intent ``FOR UPDATE``, record the result, move the case on.

Between A and B the guard is the committed intent row, not the lock. It survives the
process; the lock cannot. Anything that finds an unresolved intent — a restarting worker,
the reconciliation sweeper, this engine on a redelivered job — reads it and refuses to call
again, which is the whole of Property 3.

**Why the job payload is discarded.** A job carries a case id and nothing else that is
trusted. The state that authorized this action was read when the action was scheduled, and
the job may have been queued for minutes: the customer may have paid, the window may have
closed, consent may have been withdrawn, a human may have taken ownership. Acting on
payload state is acting on the past. Every value is reloaded and policy is asked again.

**Zero external calls on every refusal path.** Lock contention, a non-``APPROVED``
re-evaluation, an absent or expired or already-consumed approval, an audit-write block, an
existing intent in any state — each of these returns before the provider is touched. The
enumeration is deliberate: it is easier to review "which paths can reach the call" when the
answer is one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from sqlalchemy import select

from revora.audit.events import (
    CONCURRENT_EXECUTION_PREVENTED,
    EXECUTION_ABANDONED_POLICY,
    EXECUTION_REFUSED,
    EXECUTION_RESULT_UNKNOWN,
    EXECUTION_STARTED,
)
from revora.audit.writer import AuditEntry, AuditWriter, is_case_blocked
from revora.cases.manager import apply_locked_transition
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, IntentState, PolicyVerdict
from revora.domain.money import Minor
from revora.execution.authorization import evaluate_against_reloaded_state
from revora.execution.contact import resolve_customer_contact
from revora.execution.intents import (
    IntentDisposition,
    existing_intent_disposition,
    record_result,
    reserve_intent,
)
from revora.execution.messages import description_for
from revora.persistence.models import Merchant
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.session import (
    case_advisory_key,
    tenant_transaction,
    try_advisory_xact_lock,
)
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.logging import get_logger
from revora.providers.payment_link import (
    PaymentLinkRequest,
    PaymentLinkRequestError,
    build_payment_link_request,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.persistence.models import PolicyDecision, RecoveryCase
    from revora.platform.config import Configuration
    from revora.providers.classification import PaymentLinkEntity, ProviderResult
    from revora.providers.razorpay import PaymentProviderClient

__all__ = ["ExecutionAttempt", "ExecutionOutcome", "execute_approved_action"]

_logger = get_logger(__name__)

_ACTOR = "execution_engine"


@unique
class ExecutionOutcome(StrEnum):
    """Why the attempt ended where it did.

    Ordered roughly by how far the attempt got. Everything above ``CALL_ISSUED`` made no
    provider call at all, which is the property worth being able to read off a return value.
    """

    CASE_NOT_FOUND = "CASE_NOT_FOUND"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    """Another worker holds this case. Abandoned, zero calls (R9.C13)."""

    AUDIT_BLOCKED = "AUDIT_BLOCKED"
    """A previous audit write failed for this case, so external action is withheld until a
    record persists. Acting without an audit trail is not permitted (R11.C10)."""

    NO_APPROVAL = "NO_APPROVAL"
    """No approved, unconsumed decision exists. Nothing authorizes a call."""

    REFUSED = "REFUSED"
    """The approval failed a structural check — expired, mismatched key, wrong action."""

    POLICY_ABANDONED = "POLICY_ABANDONED"
    """Re-evaluation against reloaded state did not return ``APPROVED``."""

    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    """This key already has a ``CONFIRMED`` or ``FAILED`` intent. Recorded result returned
    unchanged; no call."""

    HANDED_TO_RECONCILIATION = "HANDED_TO_RECONCILIATION"
    """An unresolved intent exists for this key. Only a read may resolve it. No call."""

    TRANSITION_REFUSED = "TRANSITION_REFUSED"
    """The case could not legally enter ``EXECUTING``. Rolled back, no call."""

    CONTACT_UNAVAILABLE = "CONTACT_UNAVAILABLE"
    """The customer's contact could not be decrypted from the originating event, or the
    payment carried none. Nothing is contactable, so nothing is sent. Costs one recovery
    opportunity, which is strictly better than sending a link nobody receives."""

    NO_APPROVED_MESSAGE = "NO_APPROVED_MESSAGE"
    """The action has no approved customer-visible wording. Refused rather than executed with
    borrowed copy that would describe a different action."""

    REQUEST_INVALID = "REQUEST_INVALID"
    """The request failed validation before send — a description over the length bound, a
    non-positive amount. Rejected, never truncated or coerced."""

    LOST_RESERVATION = "LOST_RESERVATION"
    """Another transaction committed the intent for this key first. No call."""

    CONFIRMED = "CONFIRMED"
    """The call succeeded and the effect is recorded with a provider id."""

    FAILED = "FAILED"
    """The provider definitively refused. No effect exists."""

    UNCERTAIN = "UNCERTAIN"
    """The call's outcome is unknown. Reconciliation owns it now, and no further external
    call happens for this case until it resolves."""


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """What happened, in enough detail for a caller to act without re-reading."""

    outcome: ExecutionOutcome
    case_id: uuid.UUID
    idempotency_key: str | None = None
    intent_id: uuid.UUID | None = None
    intent_state: IntentState | None = None
    provider_response_id: str | None = None
    detail: str | None = None

    @property
    def made_external_call(self) -> bool:
        """Whether a provider request was issued. False on every refusal path."""
        return self.outcome in (
            ExecutionOutcome.CONFIRMED,
            ExecutionOutcome.FAILED,
            ExecutionOutcome.UNCERTAIN,
        )


def execute_approved_action(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    correlation_id: uuid.UUID | None = None,
) -> ExecutionAttempt:
    """Execute the case's approved action at most once. The engine's only entry point.

    Safe to call repeatedly with the same arguments — by the job queue after a crash, by a
    restarting worker, by a redelivered job. Repetition produces at most one external
    effect, because the second call finds the first call's intent and refuses.

    Never raises for a provider condition: the client returns classifications rather than
    exceptions, and this returns an :class:`ExecutionAttempt` for every path.
    """
    configuration = config or default_configuration()

    reservation = _reserve_under_lock(
        merchant_id,
        case_id,
        factory=factory,
        config=configuration,
        correlation_id=correlation_id,
    )
    if reservation.attempt is not None:
        return reservation.attempt

    # tx-A has committed. A durable ATTEMPTED intent exists and the lock is gone. From
    # here, a crash is recoverable by reading — never by calling again.
    assert reservation.request is not None
    result = provider.create_payment_link(reservation.request)

    return _record_under_lock(
        merchant_id,
        case_id,
        idempotency_key=reservation.idempotency_key,
        result=result,
        factory=factory,
        config=configuration,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# tx-A — the locked decision transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Reservation:
    """Either a finished attempt (no call permitted) or a request to issue."""

    attempt: ExecutionAttempt | None
    idempotency_key: str
    request: PaymentLinkRequest | None = None


def _reserve_under_lock(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> _Reservation:
    """Decide whether one call is permitted, and commit the intent that permits it."""
    with tenant_transaction(merchant_id, factory) as session:
        # The advisory lock first, and non-blocking. If another worker is executing this
        # case the right answer is to leave it alone — queueing behind it would mean
        # issuing a second call the moment it finishes.
        if not try_advisory_xact_lock(session, case_advisory_key(case_id)):
            _audit_unattached(
                session,
                merchant_id,
                config,
                event_type=CONCURRENT_EXECUTION_PREVENTED,
                detail=str(case_id),
                correlation_id=correlation_id,
            )
            return _refused(case_id, ExecutionOutcome.LOCK_UNAVAILABLE)

        # Everything from here is reloaded. Nothing from the job payload is trusted.
        cases = RecoveryCaseRepository(session)
        case = cases.lock_for_update(merchant_id, case_id)
        if case is None:
            _logger.warning("execution on missing case", case_id=str(case_id))
            return _refused(case_id, ExecutionOutcome.CASE_NOT_FOUND)

        if is_case_blocked(merchant_id, case_id):
            return _refused(case_id, ExecutionOutcome.AUDIT_BLOCKED)

        decisions = PolicyDecisionRepository(session)
        decision = decisions.latest_approved_unconsumed(merchant_id, case_id)
        if decision is None:
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=EXECUTION_REFUSED,
                detail="no approved unconsumed decision",
                correlation_id=correlation_id,
            )
            return _refused(case_id, ExecutionOutcome.NO_APPROVAL)

        moment = now()
        action = CandidateAction(decision.selected_action)

        # Structural checks on the approval itself, before policy is consulted again.
        # `latest_approved_unconsumed` deliberately does not check expiry — staleness is a
        # separate refusal with its own reason, and conflating the two would report an
        # expired approval as an absent one.
        refusal = _structural_refusal(decision, case, moment=moment)
        if refusal is not None:
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=EXECUTION_REFUSED,
                detail=refusal,
                correlation_id=correlation_id,
                action=action,
                idempotency_key=decision.idempotency_key,
            )
            return _refused(
                case_id, ExecutionOutcome.REFUSED, key=decision.idempotency_key, detail=refusal
            )

        # Authority is re-requested here, against the rows just loaded, and this is the
        # decision that governs. The earlier approval only gets us as far as asking.
        evaluation, assembled = evaluate_against_reloaded_state(
            session,
            merchant_id,
            case,
            action=action,
            config=config,
            moment=moment,
            decision_cycle=int(decision.decision_cycle),
        )
        if evaluation.verdict is not PolicyVerdict.APPROVED:
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=EXECUTION_ABANDONED_POLICY,
                detail=evaluation.primary_reason,
                correlation_id=correlation_id,
                action=action,
                policy_result={
                    "verdict": evaluation.verdict.value,
                    "primary_reason": evaluation.primary_reason,
                    "rules_version": evaluation.rules_version,
                },
            )
            return _refused(
                case_id,
                ExecutionOutcome.POLICY_ABANDONED,
                key=decision.idempotency_key,
                detail=evaluation.primary_reason,
            )

        key = assembled.prospective_key
        if decision.idempotency_key is not None and decision.idempotency_key != key:
            # The stored key and the key derived from current state disagree, which means
            # the attempt ordinal moved since the approval. Acting would either reuse a
            # spent key or mint an unauthorized one.
            detail = "idempotency key does not match reloaded state"
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=EXECUTION_REFUSED,
                detail=detail,
                correlation_id=correlation_id,
                action=action,
                idempotency_key=key,
            )
            return _refused(case_id, ExecutionOutcome.REFUSED, key=key, detail=detail)

        # Does this key already have a record? Checked under the lock, before inserting.
        disposition, existing = existing_intent_disposition(session, merchant_id, key)
        if disposition is IntentDisposition.RESOLVED and existing is not None:
            return _Reservation(
                attempt=ExecutionAttempt(
                    outcome=ExecutionOutcome.ALREADY_RESOLVED,
                    case_id=case_id,
                    idempotency_key=key,
                    intent_id=existing.id,
                    intent_state=IntentState(existing.state),
                    provider_response_id=existing.provider_response_id,
                ),
                idempotency_key=key,
            )
        if disposition is IntentDisposition.IN_FLIGHT and existing is not None:
            return _Reservation(
                attempt=ExecutionAttempt(
                    outcome=ExecutionOutcome.HANDED_TO_RECONCILIATION,
                    case_id=case_id,
                    idempotency_key=key,
                    intent_id=existing.id,
                    intent_state=IntentState(existing.state),
                ),
                idempotency_key=key,
            )

        attempt_ordinal = int(case.executed_action_count) + 1
        reserved = reserve_intent(
            session,
            merchant_id,
            case_id=case_id,
            policy_decision_id=decision.id,
            idempotency_key=key,
            action=action,
            attempt_ordinal=attempt_ordinal,
            moment=moment,
        )
        if not reserved.may_call:
            # Another transaction committed this key between our read and our insert. It
            # owns the call; we make none.
            return _refused(case_id, ExecutionOutcome.LOST_RESERVATION, key=key)

        # The counter-bearing edge. Applied inline rather than through `apply_transition`,
        # which opens its own transaction and would deadlock against the row lock this
        # transaction is already holding.
        transition, rejection = apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.EXECUTING,
            reason="approved action execution",
            actor=_ACTOR,
            action=action,
            correlation_id=correlation_id,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
        if rejection is not None:
            # Rolling back discards the reserved intent too, which is correct: no call has
            # happened, so there is nothing to remember.
            session.rollback()
            return _refused(
                case_id,
                ExecutionOutcome.TRANSITION_REFUSED,
                key=key,
                detail=rejection.event_type,
            )

        # Consume the approval. A partial unique index makes at-most-once structural, so a
        # second intent cannot claim the same decision even if this code were wrong.
        decision.consumed_by_intent_id = reserved.intent_id

        # Contact and copy, both resolved before the intent is relied upon. A failure in
        # either is a refusal in a window where nothing has been sent — which is why they
        # are built here rather than after the commit.
        resolution = resolve_customer_contact(session, merchant_id, case)
        if not resolution.resolved or resolution.contact is None:
            session.rollback()
            return _refused(
                case_id,
                ExecutionOutcome.CONTACT_UNAVAILABLE,
                key=key,
                detail=resolution.reason,
            )

        merchant_name = _merchant_name(session, merchant_id)
        description = description_for(action, merchant_name=merchant_name)
        if description is None:
            session.rollback()
            return _refused(
                case_id,
                ExecutionOutcome.NO_APPROVED_MESSAGE,
                key=key,
                detail=f"no approved template for {action.value}",
            )

        try:
            request = build_payment_link_request(
                case_id=case_id,
                action=action,
                attempt_ordinal=attempt_ordinal,
                amount=Minor(int(case.payment_amount)),
                currency=str(case.currency),
                description=description,
                customer=resolution.contact,
                window_end=case.window_end_at,
                now=moment,
                max_message_length=config.MAX_MESSAGE_LENGTH,
            )
        except PaymentLinkRequestError as exc:
            # A request that cannot be built correctly is not sent at all. Rolling back
            # discards the reserved intent, which is right: no call happened.
            session.rollback()
            _logger.warning(
                "payment link request rejected before send",
                case_id=str(case_id),
                rule=exc.rule,
            )
            return _refused(
                case_id, ExecutionOutcome.REQUEST_INVALID, key=key, detail=exc.rule
            )

        _audit_case(
            session,
            merchant_id,
            case_id,
            config,
            event_type=EXECUTION_STARTED,
            detail=f"attempt {attempt_ordinal}",
            correlation_id=correlation_id,
            action=action,
            idempotency_key=key,
        )
        _ = transition
        return _Reservation(attempt=None, idempotency_key=key, request=request)


def _structural_refusal(
    decision: PolicyDecision, case: RecoveryCase, *, moment: datetime
) -> str | None:
    """Checks on the approval as a record, independent of what policy would say now.

    Separate from re-evaluation because these are not policy questions. An expired approval
    or one recorded against a different case state is not a decision the engine disagrees
    with — it is a decision that no longer describes the situation, and reporting it as a
    policy block would hide a real defect behind a routine-looking reason.
    """
    if decision.expires_at is not None and decision.expires_at <= moment:
        return "approval expired"
    if decision.consumed_by_intent_id is not None:
        return "approval already consumed"
    if str(case.state) != str(decision.case_state_at_evaluation):
        return (
            f"case state moved from {decision.case_state_at_evaluation} to {case.state} "
            "since the approval"
        )
    return None


# ---------------------------------------------------------------------------
# tx-B — recording the result
# ---------------------------------------------------------------------------


def _record_under_lock(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    idempotency_key: str,
    result: ProviderResult[PaymentLinkEntity],
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> ExecutionAttempt:
    """Write the call's outcome, and move the case if the outcome is known.

    A crash before this commits leaves the ``ATTEMPTED`` intent behind, which is exactly
    what reconciliation is for. Nothing here retries the call on any path.
    """
    with tenant_transaction(merchant_id, factory) as session:
        cases = RecoveryCaseRepository(session)
        case = cases.lock_for_update(merchant_id, case_id)
        intent = ExecutionIntentRepository(session).get_by_idempotency_key(
            merchant_id, idempotency_key
        )
        if case is None or intent is None:  # pragma: no cover - both committed in tx-A
            _logger.error(
                "execution result with no intent to record it on",
                case_id=str(case_id),
            )
            return ExecutionAttempt(
                outcome=ExecutionOutcome.UNCERTAIN,
                case_id=case_id,
                idempotency_key=idempotency_key,
                intent_state=IntentState.UNCERTAIN,
            )

        state = record_result(intent, result, moment=now())

        if state is IntentState.CONFIRMED:
            # The counter already moved on the edge into EXECUTING, so this edge carries no
            # counter effects and `counter_applied` records that the attempt was counted —
            # which is what stops reconciliation counting it a second time.
            intent.counter_applied = True
            apply_locked_transition(
                session,
                merchant_id,
                case,
                expected_version=int(case.version),
                target_state=CaseState.WAITING_FOR_OUTCOME,
                reason="payment link created",
                actor=_ACTOR,
                action=CandidateAction(intent.action),
                correlation_id=correlation_id,
                disclosure_length=config.MASK_DISCLOSURE_LENGTH,
                max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
            )
        elif state is IntentState.UNCERTAIN:
            intent.counter_applied = True
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=EXECUTION_RESULT_UNKNOWN,
                detail=intent.provider_failure_code or "unknown",
                correlation_id=correlation_id,
                action=CandidateAction(intent.action),
                idempotency_key=idempotency_key,
            )
        else:
            # FAILED. The attempt is spent and counted; the case stays in EXECUTING for the
            # lifecycle sweeper to move on, because whether another cycle is permitted is a
            # policy question and not one to answer here.
            intent.counter_applied = True

        return ExecutionAttempt(
            outcome=ExecutionOutcome[state.value],
            case_id=case_id,
            idempotency_key=idempotency_key,
            intent_id=intent.id,
            intent_state=state,
            provider_response_id=intent.provider_response_id,
        )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _refused(
    case_id: uuid.UUID,
    outcome: ExecutionOutcome,
    *,
    key: str | None = None,
    detail: str | None = None,
) -> _Reservation:
    """A finished attempt that issued no provider call."""
    return _Reservation(
        attempt=ExecutionAttempt(
            outcome=outcome, case_id=case_id, idempotency_key=key, detail=detail
        ),
        idempotency_key=key or "",
    )


def _merchant_name(session: Session, merchant_id: uuid.UUID) -> str:
    """The merchant's display name, for the customer-visible description.

    Falls back to a generic noun rather than raising or leaving a placeholder. A payment
    request reading "Complete your payment to the merchant" is poor copy; one reading
    "Complete your payment to {merchant}" is a visible bug in front of a paying customer, and
    an exception here would refuse a legitimate recovery over a cosmetic field.
    """
    name = session.execute(
        select(Merchant.display_name).where(Merchant.id == merchant_id)
    ).scalar_one_or_none()
    cleaned = (name or "").strip()
    return cleaned or "the merchant"


def _audit_case(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    event_type: str,
    detail: str,
    correlation_id: uuid.UUID | None,
    action: CandidateAction | None = None,
    idempotency_key: str | None = None,
    policy_result: dict[str, object] | None = None,
) -> None:
    """Append a case-attached record. The caller holds the case row ``FOR UPDATE``."""
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=event_type,
            actor=_ACTOR,
            action=None if action is None else action.value,
            idempotency_key=idempotency_key,
            decision={"detail": detail},
            policy_result=policy_result,
        ),
        correlation_id=correlation_id,
    )


def _audit_unattached(
    session: Session,
    merchant_id: uuid.UUID,
    config: Configuration,
    *,
    event_type: str,
    detail: str,
    correlation_id: uuid.UUID | None,
) -> None:
    """Append a record with no case attachment.

    Only for lock contention. Allocating a case-attached sequence number needs the case row
    under ``FOR UPDATE``, and the worker being reported is the one holding it.
    """
    AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    ).write_unattached(
        merchant_id,
        AuditEntry(
            event_type=event_type, actor=_ACTOR, decision={"case_id": detail}
        ),
        correlation_id=correlation_id,
    )

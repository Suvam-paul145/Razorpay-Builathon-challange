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
intent, move the case to ``EXECUTING`` with its counter effects, consume the decision, mint
the Customer_Access_Token for a customer-visible action, audit ``EXECUTION_STARTED``.
**Commit.** The lock is released here, on purpose.

The mint is inside tx-A because the token URL is what the message carries: a token minted at
confirmation would arrive after the only message that could have delivered it. It adds no
commit and does not widen the gap between the commit and the call — it is work done on the
side of the boundary where a crash costs nothing, because the intent it authorizes does not
exist yet. A failed mint rolls tx-A back, so no intent exists, no counter moved and no call
went out, which is R18.C13 satisfied by the boundary rather than by a compensating action.

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
existing intent in any state, a token that could not be minted — each of these returns before
the provider is touched. The enumeration is deliberate: it is easier to review "which paths
can reach the call" when the answer is one.

**This module holds no reasoning adapter and cannot obtain one.** The optional
``LINK_DESCRIPTION`` draft arrives as ``execute_approved_action``'s ``ai_description``
argument, already schema- and content-validated by the layer that asked for it, and is
substituted for the approved template at the one point where the description is composed.
Every refusal above that point runs identically whether it is set or ``None``, which is what
makes "the reasoning layer cannot change what gets executed" a property of the signature.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from revora.audit.events import (
    CONCURRENT_EXECUTION_PREVENTED,
    CONTROL_ACTION_SUPPRESSED,
    CUSTOMER_TOKEN_ISSUE_FAILED,
    EXECUTION_ABANDONED_POLICY,
    EXECUTION_REFUSED,
    EXECUTION_RESULT_UNKNOWN,
    EXECUTION_STARTED,
)
from revora.audit.writer import AuditEntry, AuditWriter, is_case_blocked
from revora.cases.manager import apply_locked_transition
from revora.customer.promises import mark_follow_up_scheduled
from revora.customer.tokens import TokenService
from revora.domain.actions import CandidateAction, is_customer_visible
from revora.domain.enums import (
    SUPPRESSED_BY_CONTROL_ARM,
    CaseState,
    ExecutionEffectKind,
    ExperimentGroup,
    IntentState,
    PolicyVerdict,
)
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
from revora.execution.resend import ResendTarget, settle_resend_result
from revora.persistence.models import Merchant
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.experiments import ExperimentAssignmentRepository
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
    NotifyMedium,
    PaymentLinkRequest,
    PaymentLinkRequestError,
    build_payment_link_request,
    is_resend_response_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.persistence.models import PolicyDecision, RecoveryCase
    from revora.platform.config import Configuration
    from revora.providers.classification import (
        PaymentLinkEntity,
        PaymentLinkResendAck,
        ProviderResult,
    )
    from revora.providers.razorpay import PaymentProviderClient

__all__ = [
    "CONSUMABLE_STATES",
    "ExecutionAttempt",
    "ExecutionOutcome",
    "execute_approved_action",
]

CONSUMABLE_STATES: frozenset[CaseState] = frozenset(
    {CaseState.POLICY_CHECK, CaseState.ACTION_SCHEDULED}
)
"""The states an approved decision may still be acted on from.

Exactly the two states between "a decision was recorded" and "an effect was attempted".
``POLICY_CHECK`` covers a caller that executes straight off the decision; ``ACTION_SCHEDULED``
covers the pipeline, which advances the case as soon as it has an authorization to consume.

Everything else is excluded for a reason worth naming individually. A **terminal** state means the
case ended — possibly because the payment was already made — and acting would contact a customer
about money they have already sent. **EXECUTING** or **WAITING_FOR_OUTCOME** means an effect
already happened, and a second one is the duplicate this whole engine exists to prevent.
**DECISION_PENDING** means a new decision cycle is under way, so this approval describes a
comparison that has been superseded."""

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

    CONTROL_ARM_SUPPRESSED = "CONTROL_ARM_SUPPRESSED"
    """The case is in an experiment's control arm, so its action is withheld (R13.C3).

    The recommendation and the approved decision both stand and are recorded — only the external
    effect is suppressed. That is what makes the control arm a counterfactual record: we know
    what Revora would have done here, and we know what happened without it. Skipping the pipeline
    for control cases would be cheaper and would leave nothing to compare against."""

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

    TOKEN_ISSUE_FAILED = "TOKEN_ISSUE_FAILED"
    """A Customer_Access_Token could not be minted for an approved customer-visible action
    (R18.C13). Zero external calls, no intent, no counter movement.

    Above ``CALL_ISSUED`` in this enumeration's ordering, and that placement is the whole of
    R18.C13: the mint shares the intent insert's transaction, so a failed mint rolls the
    intent back rather than needing a compensating action to undo it. There is no window in
    which an intent exists for a message that could not have carried its token."""

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
    ai_description: str | None = None,
) -> ExecutionAttempt:
    """Execute the case's approved action at most once. The engine's only entry point.

    Safe to call repeatedly with the same arguments — by the job queue after a crash, by a
    restarting worker, by a redelivered job. Repetition produces at most one external
    effect, because the second call finds the first call's intent and refuses.

    Never raises for a provider condition: the client returns classifications rather than
    exceptions, and this returns an :class:`ExecutionAttempt` for every path.

    Args:
        ai_description: an already-validated ``LINK_DESCRIPTION`` draft, or ``None`` to send
            the approved template (R27.C10). **An argument rather than an adapter**, and that
            is the whole of this module's relationship with the reasoning layer: it holds no
            client, resolves no credential, and cannot tell whether the string it was handed
            came from a model or from a file. ``None`` is the value on every path where no
            model was consulted, where one timed out and where its answer was refused — so
            with no credential configured this engine composes exactly the sentence it
            composed before the reasoning layer existed, and "identical with every model
            response removed" is a call with ``None``.

            It substitutes for the template; it never authorizes anything. Every refusal path
            above the send is evaluated identically whether it is set or not, and a draft
            cannot make an action executable that had no approved wording of its own.
    """
    configuration = config or default_configuration()

    reservation = _reserve_under_lock(
        merchant_id,
        case_id,
        factory=factory,
        config=configuration,
        correlation_id=correlation_id,
        ai_description=ai_description,
    )
    if reservation.attempt is not None:
        if reservation.attempt.outcome is ExecutionOutcome.TOKEN_ISSUE_FAILED:
            # R18.C13's record, in its own transaction because the one that discovered the
            # failure was rolled back — along with the intent, the counter movement and the
            # consumed decision, which is the point. Same two-phase shape
            # ``apply_transition`` uses for a rejected transition, and for the same reason: a
            # refusal has to be durable even though the work it refused is not.
            _audit_token_issue_failure(
                merchant_id,
                case_id,
                factory=factory,
                config=configuration,
                correlation_id=correlation_id,
                reason=reservation.attempt.detail,
                idempotency_key=reservation.attempt.idempotency_key,
            )
        return reservation.attempt

    # tx-A has committed. A durable ATTEMPTED intent exists and the lock is gone. From
    # here, a crash is recoverable by reading — never by calling again.
    #
    # Exactly one of the two branches below issues exactly one request, and which one was
    # decided under the lock and written down as ``effect_kind`` on the committed intent. So the
    # question "what did this key do" is answerable from the row rather than from re-deriving the
    # branch, which matters because for a resend the row is the *only* thing that will ever
    # answer it. Neither branch is wrapped in anything that could run it twice: there is no
    # retry decorator anywhere in this module and the two exception types that would tempt one
    # (``AmbiguousExternalError``, ``TransientInfraError``) are deliberately separate so a
    # decorator cannot be attached to both.
    if reservation.resend is not None:
        resend_result = provider.notify_by(
            reservation.resend.payment_link_id, reservation.resend.medium
        )
        return _record_resend_under_lock(
            merchant_id,
            case_id,
            idempotency_key=reservation.idempotency_key,
            result=resend_result,
            target=reservation.resend,
            factory=factory,
            config=configuration,
            correlation_id=correlation_id,
        )

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
    """Either a finished attempt (no call permitted) or one call to issue.

    ``request`` and ``resend`` are mutually exclusive and at most one is ever set. Two fields
    rather than a union because the two calls take different arguments and settle through
    different code, and because a caller reading ``resend is not None`` is asking the question
    that decides everything downstream: whether this key's effect is re-readable afterwards.
    """

    attempt: ExecutionAttempt | None
    idempotency_key: str
    request: PaymentLinkRequest | None = None
    resend: ResendTarget | None = None
    """Set only for a ``PROMISE_TO_PAY_FOLLOW_UP`` against a case that already holds a live
    payment link. ``None`` for every other action, and for a follow-up on a case with no live
    link — which falls back to creating one under R24.C11 and is therefore an ordinary
    ``request``, reconcilable like any other create."""


def _reserve_under_lock(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    ai_description: str | None = None,
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

        if _is_control_arm(session, merchant_id, case_id):
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=CONTROL_ACTION_SUPPRESSED,
                detail=SUPPRESSED_BY_CONTROL_ARM,
                correlation_id=correlation_id,
            )
            return _refused(
                case_id,
                ExecutionOutcome.CONTROL_ARM_SUPPRESSED,
                detail=SUPPRESSED_BY_CONTROL_ARM,
            )

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

        # Which external effect this key is for, decided under the lock and committed on the
        # intent row before anything leaves the process. R24.C10 and R24.C11 are the two branches:
        # a case holding a live payment link gets that link re-notified and **no second link**; a
        # case holding none has one created, and that creation *is* the effect of this key. Only
        # the second is reconcilable, which is why the choice is persisted rather than recomputed
        # in tx-B — a resend row is absent from the reconciliation sweep's set by virtue of this
        # column, and a column written from a re-derivation could disagree with the call that
        # actually went out.
        resend_target = (
            _live_link_target(session, merchant_id, case_id)
            if action is CandidateAction.PROMISE_TO_PAY_FOLLOW_UP
            else None
        )
        effect_kind = (
            ExecutionEffectKind.PAYMENT_LINK_RESEND
            if resend_target is not None
            else ExecutionEffectKind.PAYMENT_LINK_CREATE
        )

        reserved = reserve_intent(
            session,
            merchant_id,
            case_id=case_id,
            policy_decision_id=decision.id,
            idempotency_key=key,
            action=action,
            attempt_ordinal=attempt_ordinal,
            effect_kind=effect_kind,
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
        #
        # **All three are skipped for a resend, and skipping them is the point rather than an
        # optimization.** A resend composes nothing: the provider re-notifies the link it already
        # holds, against the contact that link was created with, using wording the provider
        # authored — which is exactly what R24.C17 makes the execution record say. There is no
        # request to validate, no description to approve, and no contact to decrypt. Refusing a
        # follow-up because Revora could not decrypt a contact it never sends would be a refusal
        # with no cause, and it would cost the customer the nudge they asked for; worse, it would
        # decrypt PII on a path that has no use for it, which is the one thing
        # ``revora.execution.contact``'s whole docstring is arranged against.
        request: PaymentLinkRequest | None = None
        if resend_target is None:
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
            template = description_for(action, merchant_name=merchant_name)
            if template is None:
                session.rollback()
                return _refused(
                    case_id,
                    ExecutionOutcome.NO_APPROVED_MESSAGE,
                    key=key,
                    detail=f"no approved template for {action.value}",
                )
            # R27.C10's substitution, and note which way round it is: the template is required
            # *first*, and only then may a draft stand in for it. An action with no approved
            # wording is refused above whether or not a draft exists, so a model cannot make an
            # action sendable that Revora has written no copy for. The draft has already passed
            # the output schema, ``validate_description`` and the placeholder, amount-equality
            # and single-link rules before it reached this argument.
            description = template if ai_description is None else ai_description

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

        # The Customer_Access_Token, minted here (R18.C1). Three things about the placement.
        #
        # **Inside this transaction, before the commit.** The token URL is what the message
        # carries, so a token minted at confirmation would arrive after the only message that
        # could have delivered it. Sharing the transaction is also the whole of R18.C13: a
        # failed mint rolls back the intent, the counter movement and the consumed decision
        # together, so the requirement is satisfied by the transaction boundary rather than by
        # a compensating action that could itself fail.
        #
        # **It does not widen the window between the intent commit and the provider call.**
        # That window is the gap between tx-A's commit and the single provider request issued
        # by the entry point, and it is unchanged: there is still nothing between them. The
        # mint is work done *before* the commit, on the side of the boundary where a crash
        # costs nothing, because the intent it would have authorized does not exist yet.
        # Property 3 is untouched.
        #
        # Note for a reader tempted to name the provider method in a comment here: a `pure`
        # test asserts that the *source* of this function does not contain it, which is how
        # "the reservation phase cannot reach the provider" is checked regardless of how the
        # two phases are ordered in the file.
        #
        # **Last in tx-A, after every cheaper refusal.** Lock contention, an absent or stale
        # approval, a policy abandonment, an existing intent, a failed transition, an
        # unreachable contact and an invalid request all return above this line, so none of
        # them costs a mint — and none of them can be reached by a path on which a token was
        # already written.
        #
        # ``is_customer_visible`` rather than an unconditional mint: R18.C1 is scoped to
        # customer-visible actions, and a token accompanying an action the customer never
        # perceives would be a credential nobody was sent.
        if is_customer_visible(action):
            outcome = TokenService.on_session(session, config).mint(
                merchant_id,
                case_id=case_id,
                window_end_at=case.window_end_at,
                approved_action=action,
                moment=moment,
                correlation_id=correlation_id,
            )
            if not outcome.issued:
                reason = (
                    outcome.failure.value if outcome.failure is not None else "UNKNOWN"
                )
                _logger.warning(
                    "customer access token could not be minted; execution abandoned",
                    case_id=str(case_id),
                    reason=reason,
                )
                session.rollback()
                return _refused(
                    case_id,
                    ExecutionOutcome.TOKEN_ISSUE_FAILED,
                    key=key,
                    detail=reason,
                )

        # ``EXECUTION_STARTED``, and for a resend it carries more than a detail line. The record
        # has to say that the identifier beside it is Revora's own composition rather than the
        # provider's, and that the content about to reach the customer was authored by the
        # provider (R24.C17) — both of which are true at the moment this is written and neither of
        # which a later reader could work out. ``ResendTarget.started_audit_fields`` is the payload;
        # it deliberately omits ``short_url``, because a payment link URL is a bearer capability
        # and is never an audit field.
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
            decision=(
                None
                if resend_target is None
                else resend_target.started_audit_fields(attempt_ordinal=attempt_ordinal)
            ),
        )
        _ = transition
        return _Reservation(
            attempt=None, idempotency_key=key, request=request, resend=resend_target
        )


def _structural_refusal(
    decision: PolicyDecision, case: RecoveryCase, *, moment: datetime
) -> str | None:
    """Checks on the approval as a record, independent of what policy would say now.

    Separate from re-evaluation because these are not policy questions. An expired approval or
    one recorded against a case that has since moved somewhere it cannot be acted on is not a
    decision the engine disagrees with — it is a decision that no longer describes the situation,
    and reporting it as a policy block would hide a real defect behind a routine-looking reason.

    **The state check asks whether the case is somewhere an approval may still be consumed, not
    whether it is where it was when the approval was recorded.** It used to ask the second thing,
    and that was a contradiction with the pipeline it is part of: policy evaluates while the case
    is ``DECISION_PENDING``, then the case advances through ``POLICY_CHECK`` to
    ``ACTION_SCHEDULED`` precisely *because* the approval exists. An equality check therefore
    refused every approval the pipeline had actually authorized — the engine's own tests did not
    catch it because they construct a case already sitting in the state the approval names, and the
    integration path that exposed it is the only one that walks the states in order.

    What the check must still catch is a case that moved somewhere the approval cannot describe: a
    terminal state (the case ended, possibly because it was already paid), a state past execution
    (an effect already happened), or back to ``DECISION_PENDING`` (a new cycle is deciding afresh).
    :data:`CONSUMABLE_STATES` is that list, and it is short enough to read.
    """
    if decision.expires_at is not None and decision.expires_at <= moment:
        return "approval expired"
    if decision.consumed_by_intent_id is not None:
        return "approval already consumed"
    current = CaseState(str(case.state))
    if current not in CONSUMABLE_STATES:
        return (
            f"case state moved from {decision.case_state_at_evaluation} to {case.state} "
            "since the approval, which is not a state an approval may be consumed from"
        )
    return None


# ---------------------------------------------------------------------------
# tx-B — recording the result
# ---------------------------------------------------------------------------


def _live_link_target(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> ResendTarget | None:
    """The live payment link this case already holds, as a resend target, or ``None``.

    R24.C10's condition, answered from Revora's own record and **without a provider call**. The
    condition itself is
    :meth:`~revora.persistence.repositories.execution.ExecutionIntentRepository.live_payment_link`
    and it is asked rather than restated here, because it has a second reader:
    :mod:`revora.estimation.candidates` excludes ``PAYMENT_LINK`` from the candidate set with
    ``LIVE_PAYMENT_LINK_EXISTS`` on the same ground, and the two must agree exactly. If they
    disagreed in the loose direction the optimizer would exclude a link the engine then went on to
    create anyway; in the strict direction it would rank a link the engine turned into a resend,
    and a merchant would read a decision that did not happen. What this function adds is the
    *target*: which medium, and which URL to keep showing.

    **Read rather than fetched, deliberately.** A provider read here would be a second external
    call on the path whose whole discipline is one call per idempotency key, and it would be a call
    made *before* the intent is committed, in the window where a crash costs nothing precisely
    because nothing has gone out. It would also be answering a question the record already answers.

    ``None`` is not a failure. It routes the follow-up to R24.C11's fallback — create one link,
    ``accept_partial`` false, expiry clamped to the window end, notification enabled — and that
    creation becomes the effect of this idempotency key, with ``PAYMENT_LINK_CREATE`` on the row,
    so it *is* reconcilable. Which is the whole reason the two branches are distinguished here
    rather than downstream: only one of them can be resolved by reading afterwards.

    A composed resend token is refused rather than used, and that check stays here rather than
    moving into the shared query. It is not part of "is this link live" — ``effect_kind`` already
    excludes resend rows, so the query cannot return one — it is a guard on *using* the stored
    identifier as a link id, which is the specific mistake the token's marker exists to make
    detectable, and a defence that only lives in the client is a defence one caller can route
    around. A refusal here yields ``None`` and so the create fallback, which is still exactly one
    link.
    """
    intent = ExecutionIntentRepository(session).live_payment_link(merchant_id, case_id)
    if intent is None:
        return None
    link_id = str(intent.provider_response_id)
    if is_resend_response_id(link_id):  # pragma: no cover - effect_kind excludes resend rows
        _logger.error(
            "confirmed link creation carries a composed resend token; refusing to resend it",
            case_id=str(case_id),
            intent_id=str(intent.id),
        )
        return None
    # SMS, always. ``resolve_customer_contact`` refuses an action outright when the
    # originating payment carried no phone number — it is the channel the provider always
    # has and ``CustomerContact`` rejects a blank one — so a case that ever produced a link
    # has a phone, while email is optional and may be absent. Choosing the medium from the
    # contact would mean decrypting PII to answer a question whose answer is fixed by that
    # earlier refusal. The provider's rate limit is documented as per link *and* per medium,
    # so a future second medium is a real option; it is not one this action needs.
    return ResendTarget(
        payment_link_id=link_id,
        medium=NotifyMedium.SMS,
        short_url=intent.provider_short_url,
    )


def _record_resend_under_lock(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    idempotency_key: str,
    result: ProviderResult[PaymentLinkResendAck],
    target: ResendTarget,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> ExecutionAttempt:
    """tx-B for a resend. Records the outcome, moves the case, schedules the promise.

    Thin, because :func:`revora.execution.resend.settle_resend_result` owns the disposition and
    this must not hold a second opinion about it. That function records the classification, applies
    R24.C12's confirmed transition to ``WAITING_FOR_OUTCOME``, returns a definitively-refused case
    to ``DECISION_PENDING`` where the bounds still permit, and escalates an ``UNCERTAIN`` one
    exactly once with ``EXECUTION_RESULT_UNVERIFIABLE`` — issuing **no** further external call,
    because ``RESEND_RECONCILIATION_ATTEMPT_BOUND`` is zero and no read can answer.

    **Nothing here retries and nothing here reads the provider back.** A crash before this commits
    leaves the ``ATTEMPTED`` resend intent behind, and that row is *absent* from the reconciliation
    sweep's candidate set by virtue of its ``effect_kind`` — not skipped by it. A later engine pass
    on the same key finds an unresolved intent and returns ``HANDED_TO_RECONCILIATION`` without
    calling, which for a resend means the case waits for the stale-intent promotion to make it
    ``UNCERTAIN`` and then for the escalation a person picks up. That is the accepted cost of Fact
    two and it is the reason a lost nudge is preferred to a possible second message.

    The promise's own status is R24.C12's remaining clause and it is applied here rather than in
    the settlement, because the settlement is shared with any future resend and a promise is
    specific to this action. In the same transaction as the confirmation, so a promise recorded as
    followed-up and an intent recorded as confirmed cannot disagree.
    """
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        intent = ExecutionIntentRepository(session).get_by_idempotency_key(
            merchant_id, idempotency_key
        )
        if case is None or intent is None:  # pragma: no cover - both committed in tx-A
            _logger.error(
                "resend result with no intent to record it on", case_id=str(case_id)
            )
            return ExecutionAttempt(
                outcome=ExecutionOutcome.UNCERTAIN,
                case_id=case_id,
                idempotency_key=idempotency_key,
                intent_state=IntentState.UNCERTAIN,
            )

        settlement = settle_resend_result(
            session,
            merchant_id,
            case,
            intent,
            result,
            target=target,
            config=config,
            correlation_id=correlation_id,
        )

        if settlement.intent_state is IntentState.CONFIRMED:
            # R24.C12's last clause. Conditional on CONFIRMED and on nothing else: a refused or
            # unverifiable follow-up did not reach the customer, so recording their promise as
            # followed-up would be recording a message Revora cannot say went out — and
            # ``resolve_missed`` would then be free to blame them for not paying after it.
            mark_follow_up_scheduled(session, merchant_id, case_id)

        return ExecutionAttempt(
            outcome=ExecutionOutcome[settlement.intent_state.value],
            case_id=case_id,
            idempotency_key=idempotency_key,
            intent_id=intent.id,
            intent_state=settlement.intent_state,
            provider_response_id=intent.provider_response_id,
            detail=settlement.disposition.value,
        )


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


def _audit_token_issue_failure(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    reason: str | None,
    idempotency_key: str | None,
) -> None:
    """Record R18.C13's refusal, in its own transaction after tx-A rolled back.

    A separate transaction because the one that discovered the failure was rolled back on
    purpose — that rollback *is* the requirement, since it takes the intent, the counter
    movement and the consumed decision with it. An audit record written before the rollback
    would have gone with them, and one written by re-using the failed session would not
    commit at all.

    The case row is re-locked here, because a case-attached record allocates its gap-free
    sequence number from a counter on that row. Safe now: tx-A has committed nothing and
    released everything, so this cannot deadlock against the transaction that just ended.

    Failure to write this record is logged and swallowed. Nothing external happened and
    nothing is pending, so raising here would replace "we refused, and here is why" with a
    traceback out of the execution engine on a path that already made no call.
    """
    try:
        with tenant_transaction(merchant_id, factory) as session:
            case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
            if case is None:  # pragma: no cover - the case was loaded moments ago
                return
            _audit_case(
                session,
                merchant_id,
                case_id,
                config,
                event_type=CUSTOMER_TOKEN_ISSUE_FAILED,
                detail=reason or "UNKNOWN",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
    except SQLAlchemyError:  # pragma: no cover - depends on the database
        _logger.error(
            "could not record a customer token issue failure",
            case_id=str(case_id),
            reason=reason or "UNKNOWN",
        )


def _is_control_arm(session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID) -> bool:
    """Whether this case is in an experiment's control arm, and must therefore not act.

    **Why the check lives here rather than in** ``revora.experiment``. That package and this one
    are siblings in the layering contract, so neither may import the other — and the contract is
    right to forbid it. The question being asked is not "what does the experiment engine think",
    it is "am I permitted to produce an external effect", and this module is the only thing that
    produces external effects. So the engine reads the arm from persistence, which sits below
    both, and applies the rule itself.

    Checked once, at the boundary. Scattering "unless control" through diagnosis, estimation and
    policy would give five places to forget it and forgetting it in any one contaminates the arm
    — and a contaminated control case is excluded from the comparison, so the cost of the mistake
    is a smaller experiment rather than a visible error.

    Note what is *not* suppressed: the recommendation and the approved policy decision both stand
    and are already recorded by the time this runs. That is the point. A control case with a
    recorded recommendation and no effect is a counterfactual — we know what Revora wanted to do
    and what happened without it. Suppressing earlier would leave nothing to compare against.
    """
    assignment = ExperimentAssignmentRepository(session).for_case(merchant_id, case_id)
    if assignment is None:
        return False
    return ExperimentGroup(str(assignment.group)) is ExperimentGroup.CONTROL


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
    decision: dict[str, object] | None = None,
) -> None:
    """Append a case-attached record. The caller holds the case row ``FOR UPDATE``.

    ``decision`` overrides the default ``{"detail": detail}`` payload for the one caller that
    needs a richer one — a resend's ``EXECUTION_STARTED``, which has to carry the composed
    identifier and the content classification. ``detail`` stays required rather than becoming
    optional, so every record still has one and the richer payload includes it.
    """
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
            decision={"detail": detail} if decision is None else decision,
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

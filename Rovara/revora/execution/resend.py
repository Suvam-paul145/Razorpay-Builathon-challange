"""The payment-link resend: an effect that is real, cheap, and unreadable afterwards.

Two verified provider facts shape everything in this module, and neither is a preference.

**Fact one: the resend endpoint exists.** ``POST /v1/payment_links/:id/notify_by/:medium``
re-notifies a customer about the payment link already recorded for the case and **creates no
second link**. That is why ``PROMISE_FOLLOW_UP_FINANCIAL_COST`` is zero, and why it is the one
figure in the cost prior table that is measured rather than assumed.

**Fact two: a resend is re-readable by nothing.** The response carries a success boolean. No
notification identifier, and no endpoint that reports whether a notification was sent. A link
creation is exactly-once because the created object can be fetched back by ``reference_id``,
which is how :mod:`revora.execution.reconcile` establishes after a crash whether the effect
exists. There is no equivalent question to ask about a resend.

Three consequences, in the order they bite.

**An ``UNCERTAIN`` resend is terminal.** Not slow to resolve, not resolvable with more
attempts — unresolvable, because no observation answers the question. So the disposition is one
escalation with ``TerminalReason.EXECUTION_RESULT_UNVERIFIABLE``, in the same transaction that
records the classification, and no further external call for that case ever. This is R9.C17's
existing behaviour with the attempt bound set to zero instead of six, and the bound is zero
because the attempts would be reads that cannot answer. Reconcile-then-exhaust was considered
and rejected for exactly that reason: six reads that each return nothing informative are six
reads, not evidence.

The cost is a lost promise follow-up. A customer who said they would pay on Friday does not get
the Friday nudge, and the merchant gets an escalated case instead of a recovery. The alternative
is re-sending a message whose delivery is unknown — an SMS to a real person about money they may
already have paid. A lost nudge is recoverable by a person reading the escalation; a second
message is not recoverable at all.

**The structural half of that lives in the schema, not here.**
``ix_execution_intent_unresolved`` carries ``effect_kind = 'PAYMENT_LINK_CREATE'`` in its
predicate and ``ExecutionIntentRepository.claim_unresolved`` carries the same clause in its
``WHERE``, so a resend row is not skipped by the reconciliation sweep — it is
absent from the set the sweep reads, and ``unresolved_intent_count`` never counts it either. A
permanently-``UNCERTAIN`` row would otherwise ring the stranded-intent alarm forever with
nothing to act on. The unresolvable ones are counted where a person is already looking: the
``ESCALATED`` grouping of R14.C10.

**A definitive failure spends a customer-message increment.** The executed-action and
customer-message counters move on the single ``ACTION_SCHEDULED -> EXECUTING`` edge, before the
request, and nothing here gives them back. So a 429 costs one of the two messages a case is
allowed. That is a recorded deviation from R24.C12, which asks for the increment on
``CONFIRMED``, and the base spec's pessimistic placement wins for two reasons: the counter bounds
how many times Revora *tries* to reach a person, and a design in which a rejected attempt is free
is a design in which a loop against a rate limit burns no budget until the window closes. Moving
it would also put a second counter placement in the system for one action.

What a definitive failure does do is return the case to ``DECISION_PENDING`` where its bounds
still permit, so the next cycle can weigh the follow-up again against everything else. That is a
re-decision, not a retry: it costs a decision cycle, the attempt ordinal advances so the refused
idempotency key can never be reused, and all twelve policy checks run again — including the
cooldown and the message bound the refused attempt already spent an increment against.

Nothing in this module calls a provider. It records what a call concluded and moves the case
accordingly; the client is :meth:`revora.providers.razorpay.RazorpayClient.notify_by` and the
caller is the execution engine, outside any lock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from revora.cases.bounds import (
    BOUND_DECISION_CYCLE_LIMIT,
    BOUND_WINDOW_ELAPSED,
    bound_reached,
)
from revora.cases.manager import apply_locked_transition
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, ExecutionEffectKind, IntentState, TerminalReason
from revora.execution.escalation import escalate_unverifiable
from revora.execution.intents import RESOLVED_STATES, record_resend_result
from revora.platform.clock import now
from revora.platform.logging import get_logger
from revora.providers.payment_link import NotifyMedium, resend_response_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from sqlalchemy.orm import Session

    from revora.persistence.models import ExecutionIntent, RecoveryCase
    from revora.platform.config import Configuration
    from revora.providers.classification import PaymentLinkResendAck, ProviderResult

__all__ = [
    "PROVIDER_HOSTED_LINK_NOTIFICATION",
    "PROVIDER_IDENTIFIER_ABSENT",
    "RESEND_RECONCILIATION_ATTEMPT_BOUND",
    "ResendDisposition",
    "ResendSettlement",
    "ResendTarget",
    "bound_reached",
    "settle_resend_result",
]
"""``bound_reached`` is re-exported rather than defined here.

It moved to :mod:`revora.cases.bounds` when R24.C14 gave it a second caller — a promise moving
to ``MISSED`` asks the same "is there anything left to decide" question, and the Outcome_Monitor
that discovers it sits on this module's own layer band and so cannot import from it. The name
stays in this module's surface because every existing caller and test reads it from here, and
because a definitively refused resend is still the reason the function exists."""

_logger = get_logger(__name__)

_ACTOR = "execution_engine"
"""The same actor the engine writes under, because this is the engine's own bookkeeping
executed in the engine's transaction. A separate actor would suggest a separate component made
the decision, and none did."""

RESEND_RECONCILIATION_ATTEMPT_BOUND: int = 0
"""Reconciliation attempts permitted on a resend intent. **Zero, and it is a number rather
than a comment for one reason:** ``MAX_EXECUTION_RECONCILIATION_ATTEMPTS`` is six for a create,
and the difference between six and zero is the whole of Fact two. Declared here so a reader
comparing the two paths finds the bound written down instead of inferring it from an absence,
and so a test can assert against it."""

PROVIDER_HOSTED_LINK_NOTIFICATION: str = "PROVIDER_HOSTED_LINK_NOTIFICATION"
"""R24.C17's execution-record classification, persisted in the ``EXECUTION_STARTED`` record.

**What it distinguishes.** Everywhere else Revora produces a customer-visible effect it authors
the content and carries exactly one approved link — the Customer_Response_Page URL, with the
payment link living on that page, which is what ``validate_description``'s zero-other-links rule
is applied against. A resend is the one exception in the system: Razorpay composes the message,
Razorpay decides what it says, and the link it carries is the payment link rather than the
response page. Revora chose *that this happens* and controls none of *what it says*.

So the record says so, at the moment it is written. Without this value a reader of two
``EXECUTION_STARTED`` records for two customer-visible actions on one case could not tell which
sentence Revora is answerable for, and the honest answer to "what did you send this customer" for
a resend is "we asked the provider to send theirs again". Whether the provider would permit
Revora-authored content on a re-notification is **[EVIDENCE INSUFFICIENT]**; this classification
is correct either way, because it records what actually happened rather than what was possible.

A plain string rather than an enumeration member, and deliberately not one: it is a field value
inside one event's ``decision`` payload written by exactly one path, not a persisted column, so it
carries no ``CHECK`` and needs no migration. Compare :data:`PROVIDER_IDENTIFIER_ABSENT`, which is
here for the same reason."""

PROVIDER_IDENTIFIER_ABSENT: str = "provider_identifier_absent"
"""Audit field flagging that ``provider_response_id`` holds a Revora-composed token.

Defined here rather than in :mod:`revora.audit.events`, which is the ``event_type``
vocabulary — this is a field key inside one event's ``decision`` payload, written by exactly
one path. It exists so a reader of an ``EXECUTION_STARTED`` record can tell, without knowing
about ``effect_kind``, that the identifier on that intent did not come from the provider and
must not be treated as though it did."""


@unique
class ResendDisposition(StrEnum):
    """What the settlement did with the case. One member per branch, and no default.

    Distinct from the intent state because the two answer different questions. The intent state
    is what is known about the effect; the disposition is what happened to the case as a result,
    and a caller that only had the state would have to re-derive the second from the first plus
    the bounds — which is the derivation this module exists to hold in one place.
    """

    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    """The intent was already ``CONFIRMED`` or ``FAILED``. Its recorded result stands and the
    case was not touched, because the first settlement already moved it."""

    CONFIRMED = "CONFIRMED"
    """The provider acknowledged. The case is waiting for an outcome."""

    RETURNED_TO_DECISION = "RETURNED_TO_DECISION"
    """Definitively refused, nothing delivered, and the bounds still permit another cycle."""

    STOPPED_AT_BOUND = "STOPPED_AT_BOUND"
    """Definitively refused, and a bound is spent. The case is terminal."""

    HELD_FOR_WINDOW_SWEEP = "HELD_FOR_WINDOW_SWEEP"
    """Definitively refused after the recovery window closed. Left where it is for the
    lifecycle sweeper, which owns window expiry and reads ``window_end_at`` off the row —
    expiring it here would put a second writer on that rule."""

    ESCALATED_UNVERIFIABLE = "ESCALATED_UNVERIFIABLE"
    """The outcome is unknown and no read can settle it. Escalated once, no further call."""

    TRANSITION_REFUSED = "TRANSITION_REFUSED"
    """The case could not legally make the move — a version conflict, or a state that moved
    underneath. The intent's recorded result stands; the case is somebody else's now."""


@dataclass(frozen=True, slots=True)
class ResendTarget:
    """Which link a resend went against, and the identifier Revora composes for it.

    Built before the call, from the link already recorded for the case, and carried through to
    the settlement. A dataclass rather than three loose arguments because the composed
    identifier must be derived from exactly the values that were sent — deriving it twice, once
    for the audit record and once for the intent row, is how the two would come to disagree.
    """

    payment_link_id: str
    medium: NotifyMedium
    short_url: str | None = None
    """The link's existing URL, unchanged by the resend. Optional because the settlement does
    not need it to be correct — it is carried so the dashboard keeps showing the same link, and
    a resend that could not supply it must not blank the column."""

    @property
    def provider_response_id(self) -> str:
        """The composed token persisted on the intent. See :func:`resend_response_id`."""
        return resend_response_id(self.payment_link_id, self.medium)

    def started_audit_fields(self, *, attempt_ordinal: int) -> dict[str, object]:
        """The ``decision`` payload for this attempt's ``EXECUTION_STARTED`` record.

        Carries :data:`PROVIDER_IDENTIFIER_ABSENT` true, which is the point of the method: the
        record has to say that the identifier beside it is Revora's own, at the moment it is
        written, rather than leaving a later reader to work it out.

        Carries :data:`PROVIDER_HOSTED_LINK_NOTIFICATION` as the ``classification``, which is
        R24.C17: the message about to go out is the provider's own, so the record says whose
        content it was rather than leaving a reader to assume Revora wrote it. Written here in
        tx-A, alongside the intent that authorizes the call, so the classification exists for
        every attempt including the ones that end ``UNCERTAIN`` — a classification written at
        confirmation would be missing from exactly the records a person has to read.

        ``short_url`` is deliberately absent. A payment link URL is a bearer capability —
        whoever holds it can pay the invoice — and it is never written to an audit field or a
        log line anywhere in this codebase. The link *id* is an opaque handle and is safe.
        """
        return {
            "detail": f"attempt {attempt_ordinal}",
            "classification": PROVIDER_HOSTED_LINK_NOTIFICATION,
            PROVIDER_IDENTIFIER_ABSENT: True,
            "effect_kind": ExecutionEffectKind.PAYMENT_LINK_RESEND.value,
            "payment_link_id": self.payment_link_id,
            "medium": self.medium.value,
            "provider_response_id": self.provider_response_id,
        }


@dataclass(frozen=True, slots=True)
class ResendSettlement:
    """What the settlement concluded, in enough detail for a caller not to re-read anything."""

    intent_state: IntentState
    disposition: ResendDisposition
    case_state: CaseState
    detail: str | None = None

    @property
    def is_terminal_for_case(self) -> bool:
        """Whether the case ended here, so nothing further should be scheduled for it."""
        return self.disposition in (
            ResendDisposition.STOPPED_AT_BOUND,
            ResendDisposition.ESCALATED_UNVERIFIABLE,
        )


def settle_resend_result(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    intent: ExecutionIntent,
    result: ProviderResult[PaymentLinkResendAck],
    *,
    target: ResendTarget,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> ResendSettlement:
    """Record a resend's outcome and move the case, in one transaction. No provider call.

    Runs inside the caller's transaction on a case row it already holds ``FOR UPDATE`` — the
    audit sequence is allocated off that row, and the escalation below writes two records.

    The three branches, and why each is what it is:

    * ``CONFIRMED`` — the provider acknowledged. ``counter_applied`` records that this attempt
      was already counted on the edge into ``EXECUTING``, so nothing counts it again, and the
      case moves to ``WAITING_FOR_OUTCOME``. (The promise's own status is R24.C12's and belongs
      to the follow-up action's execution path, not here.)
    * ``FAILED`` — definitively refused, nothing delivered. The increment stays spent; the case
      returns to ``DECISION_PENDING`` where the bounds still permit, terminates where a counter
      bound is spent, and is left for the lifecycle sweeper where the window has closed.
    * ``UNCERTAIN`` — **terminal.** One escalation, and no read is attempted, because none can
      answer. The intent stays ``UNCERTAIN`` deliberately: ``FAILED`` would license a further
      attempt under a new key while a message may already have reached a customer.

    Idempotent by refusal. An intent already resolved is left exactly as it is and the case is
    not touched, because whichever pass resolved it also moved the case — and a second move from
    a state that has since advanced is the mistake that would follow from re-settling.
    """
    when = moment or now()
    action = CandidateAction(intent.action)

    if IntentState(intent.state) in RESOLVED_STATES:
        return ResendSettlement(
            intent_state=IntentState(intent.state),
            disposition=ResendDisposition.ALREADY_RESOLVED,
            case_state=CaseState(case.state),
        )

    state = record_resend_result(
        intent,
        result,
        provider_response_id=target.provider_response_id,
        provider_short_url=target.short_url,
        moment=when,
    )
    # The attempt was counted on the edge into EXECUTING, before the request. Recording that
    # here is what stops any later pass counting it a second time.
    intent.counter_applied = True

    if state is IntentState.CONFIRMED:
        return _advance(
            session,
            merchant_id,
            case,
            action=action,
            target_state=CaseState.WAITING_FOR_OUTCOME,
            reason="payment link notification acknowledged",
            disposition=ResendDisposition.CONFIRMED,
            intent_state=state,
            config=config,
            correlation_id=correlation_id,
        )

    if state is IntentState.UNCERTAIN:
        escalate_unverifiable(
            session,
            merchant_id,
            case,
            action=action,
            idempotency_key=str(intent.idempotency_key),
            detail=(
                "resend outcome unknown and unresolvable by read: the response carries no "
                "notification identifier and no endpoint reports whether one was sent"
            ),
            attempts=RESEND_RECONCILIATION_ATTEMPT_BOUND,
            actor=_ACTOR,
            config=config,
            correlation_id=correlation_id,
            moment=when,
        )
        _logger.warning(
            "resend outcome unverifiable; case escalated with no further external call",
            case_id=str(case.id),
            failure_code=intent.provider_failure_code,
        )
        return ResendSettlement(
            intent_state=state,
            disposition=ResendDisposition.ESCALATED_UNVERIFIABLE,
            case_state=CaseState(case.state),
            detail=intent.provider_failure_code,
        )

    # FAILED. Nothing was delivered, and the message increment is spent either way.
    failure_code = intent.provider_failure_code or "UNKNOWN"
    bound = bound_reached(case, config, moment=when)

    if bound is None:
        return _advance(
            session,
            merchant_id,
            case,
            action=action,
            target_state=CaseState.DECISION_PENDING,
            reason=f"resend refused by provider ({failure_code}); re-deciding",
            disposition=ResendDisposition.RETURNED_TO_DECISION,
            intent_state=state,
            config=config,
            correlation_id=correlation_id,
            detail=failure_code,
        )

    if bound == BOUND_WINDOW_ELAPSED:
        # Left in place on purpose. The lifecycle sweeper reads window_end_at off the row and
        # expires the case with RECOVERY_WINDOW_ELAPSED; terminating it here would make two
        # components owners of one rule, and they would eventually disagree about the boundary.
        return ResendSettlement(
            intent_state=state,
            disposition=ResendDisposition.HELD_FOR_WINDOW_SWEEP,
            case_state=CaseState(case.state),
            detail=failure_code,
        )

    # A counter bound is spent, so there is nothing left to decide.
    #
    # MAX_MESSAGES_REACHED terminates under TerminalReason.MAX_ATTEMPTS_REACHED rather than a
    # reason of its own, and the transition's `reason` string carries which bound it actually
    # was. TerminalReason is persisted behind a CHECK generated from the enum, so a new member
    # is a migration — and inventing one to make a rare terminal record read slightly better
    # would be a schema change bought with nothing.
    terminal = (
        TerminalReason.DECISION_CYCLE_LIMIT_REACHED
        if bound == BOUND_DECISION_CYCLE_LIMIT
        else TerminalReason.MAX_ATTEMPTS_REACHED
    )
    return _advance(
        session,
        merchant_id,
        case,
        action=action,
        target_state=CaseState.STOPPED,
        reason=f"resend refused by provider ({failure_code}); {bound}",
        disposition=ResendDisposition.STOPPED_AT_BOUND,
        intent_state=state,
        config=config,
        correlation_id=correlation_id,
        terminal_reason=terminal,
        detail=bound,
    )


def _advance(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    action: CandidateAction,
    target_state: CaseState,
    reason: str,
    disposition: ResendDisposition,
    intent_state: IntentState,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    terminal_reason: TerminalReason | None = None,
    detail: str | None = None,
) -> ResendSettlement:
    """One legal move on the locked case row, reporting a refusal rather than raising.

    A refused transition is not an error here and must not be turned into one. The intent's
    result is already recorded and correct; a version conflict means another writer moved the
    case, and that writer's view is newer than this one's. Reporting
    ``TRANSITION_REFUSED`` leaves the durable record intact and lets the caller decide, which is
    the same shape the engine uses for the edge into ``EXECUTING``.

    No audit writer is constructed in this module. Every record it produces is written by
    ``apply_locked_transition`` or by ``escalate_unverifiable``, each building one from the same
    two configured bounds — a third writer on the same transaction would be a third chance for
    the masking configuration to disagree with itself.
    """
    _result, rejection = apply_locked_transition(
        session,
        merchant_id,
        case,
        expected_version=int(case.version),
        target_state=target_state,
        reason=reason,
        actor=_ACTOR,
        action=action,
        terminal_reason=terminal_reason,
        correlation_id=correlation_id,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )
    if rejection is not None:
        _logger.warning(
            "resend settlement could not move the case",
            case_id=str(case.id),
            target_state=target_state.value,
            outcome=rejection.outcome.value,
        )
        return ResendSettlement(
            intent_state=intent_state,
            disposition=ResendDisposition.TRANSITION_REFUSED,
            case_state=CaseState(case.state),
            detail=rejection.event_type,
        )
    return ResendSettlement(
        intent_state=intent_state,
        disposition=disposition,
        case_state=CaseState(case.state),
        detail=detail,
    )

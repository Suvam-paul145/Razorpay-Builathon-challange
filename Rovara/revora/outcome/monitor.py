"""The outcome monitor. Decides whether money actually arrived, and never guesses.

This module is where Revora's headline number comes from, so it is written to be
disappointing on purpose. Its default answer is "no recovery", and it takes a specific,
corroborated piece of evidence to move it off that answer: an authoritative
``fetch_payment`` reporting the payment captured. Everything else — a webhook, an
authorization, a partial payment, a read that would not complete — holds the case and
declares nothing.

The five ways it says no, each with the failure it prevents:

* **A webhook alone.** Never sufficient. Delivery is verified at-least-once and out-of-order,
  so a success webhook proves something was reported once, not that money is in the account.
* **``authorized`` alone.** Not recovery. An authorization is a hold that can be voided or
  fail to settle; counting it would report money that never arrived.
* **A partial payment.** Not recovery. Counting it inflates every figure by the difference.
* **Conflicting signals.** Hold and re-read. Most conflicts are the read lagging a webhook —
  the design marks that lag ``[EVIDENCE INSUFFICIENT]`` — and both resolutions are wrong to
  guess at.
* **A read that will not complete.** Hold, then escalate to a human with the amount recorded
  as unresolved. Declaring recovery on an uncorroborated webhook is the most damaging thing
  this system could do, and the escalation is the cheaper mistake.

Two accounting invariants hold across all of it, and they are enforced by the schema rather
than by care: ``UNIQUE (case_id)`` on ``recovery_outcome`` means recovery is counted at most
once per case, and ``verified_by_read_id NOT NULL`` means it cannot be counted without a
read. Both halves of Property 20.

**The race with execution.** A customer can pay while Revora is deciding to contact them.
If an action is scheduled and no intent exists yet, the action is cancelled before any call —
the good ending. If an intent already exists, the action went out to someone who had already
paid: it is flagged ``is_post_payment`` and counted in ``unnecessary_action_count``. That
metric is deliberately visible, because it is the cost of being wrong, and a system that hid
it would be optimising its own report rather than the merchant's outcome.

**Classification is withheld while any intent is ``UNCERTAIN``.** Whether an action reached
the customer is not yet known, so whether the recovery was natural or observed is not either.
Guessing would put a wrong number in the one table the metrics are summed from.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from revora.audit.events import (
    ACTION_CANCELLED_PAYMENT_RECEIVED,
    DELAYED_RECOVERY_RECONCILED,
    DUPLICATE_RECOVERY_EVENT_DISCARDED,
    PARTIAL_PAYMENT_OBSERVED,
    PAYMENT_STATE_CONFLICT,
    PAYMENT_STATE_READ_RECORDED,
    PAYMENT_STATE_READ_UNAVAILABLE,
    PAYMENT_STATE_UNVERIFIABLE,
    POST_PAYMENT_ACTION,
    POST_SUPPRESSION_ACTION,
    RECOVERY_RECORDED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.manager import apply_locked_transition
from revora.domain.enums import CaseState, IntentState, OutcomeClass, TerminalReason
from revora.domain.transitions import TERMINAL_STATES
from revora.memory.store import observation_writer
from revora.outcome.reads import ReadRecord, payment_timestamp, persist_read
from revora.persistence.models import RecoveryOutcome
from revora.persistence.repositories.audit import AuditRecordRepository
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.session import (
    case_advisory_key,
    tenant_transaction,
    try_advisory_xact_lock,
)
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.persistence.models import ExecutionIntent, RecoveryCase
    from revora.platform.config import Configuration
    from revora.providers.classification import PaymentEntity, ProviderResult
    from revora.providers.razorpay import PaymentProviderClient

__all__ = [
    "DEFAULT_OUTCOME_SWEEP_LIMIT",
    "IN_FLIGHT_INTENT_STATES",
    "OutcomeAssessment",
    "OutcomeVerdict",
    "observe_payment_outcome",
    "sweep_payment_state",
]

DEFAULT_OUTCOME_SWEEP_LIMIT = 100
"""Cases per payment-state sweep. Bounded so one pass is predictable work; a backlog drains
across passes rather than in one long run holding a connection."""

_logger = get_logger(__name__)

_ACTOR = "outcome_monitor"

IN_FLIGHT_INTENT_STATES: Final[frozenset[IntentState]] = frozenset(
    {IntentState.ATTEMPTED, IntentState.CONFIRMED, IntentState.UNCERTAIN}
)
"""The three intent states R21.C7 names: something went out, or may have.

``IntentState`` minus ``FAILED``, and stated as the three members rather than as the
subtraction, because the requirement names three and a reader checking this against R21.C7
should be able to do it by looking. ``FAILED`` is excluded because it means the call was made
and demonstrably did not land — there is no action a customer could have received, and recording
one as post-suppression would overstate the harm in the audit log a merchant reads to decide how
apologetic to be.

Wider than ``UNRESOLVED_INTENT_STATES`` in ``persistence.repositories.policy``, which is
``ATTEMPTED`` and ``UNCERTAIN`` only, and the difference is the difference between the two
questions. That set answers *may a second call be authorized* — a ``CONFIRMED`` intent is
resolved, so it does not block one. This set answers *did something reach the customer* — and a
``CONFIRMED`` intent is the case where something certainly did."""


@unique
class OutcomeVerdict(StrEnum):
    """What the monitor concluded. Only ``RECOVERED`` and ``DELAYED_RECOVERY`` count money."""

    CASE_NOT_FOUND = "CASE_NOT_FOUND"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    """Another worker holds the case. No read issued; the sweeper will retry."""

    DUPLICATE_DISCARDED = "DUPLICATE_DISCARDED"
    """The case is already ``RECOVERED``. No read, no change, no second count (R10.C13)."""

    READ_UNAVAILABLE = "READ_UNAVAILABLE"
    """The read did not complete. Holding; nothing declared."""

    UNVERIFIABLE = "UNVERIFIABLE"
    """The attempt bound is exhausted. Escalated, amount recorded unresolved, no recovery."""

    CONFLICT_HELD = "CONFLICT_HELD"
    """The read disagrees with the signal that prompted it. Holding; will re-read."""

    PARTIAL_HELD = "PARTIAL_HELD"
    """Some money arrived, not all. Not recovery (R10.C11)."""

    NOT_RECOVERED = "NOT_RECOVERED"
    """The read completed and says the money has not moved. The ordinary answer."""

    WITHHELD_UNCERTAIN_INTENT = "WITHHELD_UNCERTAIN_INTENT"
    """Captured, but an intent is ``UNCERTAIN`` so the classification is not yet knowable
    (R10.C12). The recovery is recorded on a later pass, once the intent resolves."""

    RECOVERED = "RECOVERED"
    """Verified captured. Recovery declared and counted exactly once."""

    DELAYED_RECOVERY = "DELAYED_RECOVERY"
    """Verified captured on a case that had already ended for another reason. One
    reconciliation transition, amount still counted exactly once (R10.C14)."""


@dataclass(frozen=True, slots=True)
class OutcomeAssessment:
    """The monitor's answer, with enough detail to act on without re-reading."""

    verdict: OutcomeVerdict
    case_id: uuid.UUID
    read_id: uuid.UUID | None = None
    recovered_amount: int | None = None
    classification: OutcomeClass | None = None
    superseded_state: CaseState | None = None
    post_payment_intents: int = 0
    detail: str | None = None

    @property
    def declared_recovery(self) -> bool:
        """Whether money was counted. The only property the metrics layer should trust."""
        return self.verdict in (OutcomeVerdict.RECOVERED, OutcomeVerdict.DELAYED_RECOVERY)


def observe_payment_outcome(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    signal_status: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> OutcomeAssessment:
    """Read the provider and decide whether this case recovered.

    Idempotent: a case already ``RECOVERED`` short-circuits before any read, so duplicate
    success signals cost nothing and cannot count the amount twice.

    Args:
        signal_status: the payment state the prompting signal claimed, if any. Compared
            against the read purely to detect a conflict — it never contributes to the
            recovery decision, which comes from the read alone.
    """
    configuration = config or default_configuration()

    # Phase one: is a read even warranted? Answered under the lock, before any external
    # call, so a duplicate signal for a recovered case issues no read at all.
    gate = _gate(
        merchant_id, case_id, factory=factory, config=configuration,
        correlation_id=correlation_id,
    )
    if gate is not None:
        return gate

    # Phase two: the read, holding no lock. A database lock must never span an external
    # request — the same rule the execution engine follows, for the same reason.
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).get(merchant_id, case_id)
        provider_payment_id = None if case is None else str(case.provider_payment_id)
    if provider_payment_id is None:  # pragma: no cover - gate already found the case
        return OutcomeAssessment(OutcomeVerdict.CASE_NOT_FOUND, case_id)

    result = provider.fetch_payment(provider_payment_id)

    # Phase three: record and decide.
    return _assess(
        merchant_id,
        case_id,
        result=result,
        provider_payment_id=provider_payment_id,
        factory=factory,
        config=configuration,
        signal_status=signal_status,
        correlation_id=correlation_id,
    )


def sweep_payment_state(
    merchant_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    limit: int = DEFAULT_OUTCOME_SWEEP_LIMIT,
    correlation_id: uuid.UUID | None = None,
) -> tuple[OutcomeAssessment, ...]:
    """Re-read every case waiting on an outcome. The periodic half of the monitor.

    Exists because a case must not depend on a webhook arriving to reach its ending. A
    recovery that happened while delivery was broken is still a recovery, and a conflict that
    needs re-reading will not re-read itself — R10.C6 puts a bound on the attempts and an
    interval between them, and this sweep is where the interval comes from.

    One pass reads each waiting case once. Spacing between passes is the scheduler's job, so
    the attempt bound advances once per ``PAYMENT_STATE_RECONCILIATION_INTERVAL`` rather than
    as fast as a loop can turn.

    ``signal_status`` is deliberately not passed. A sweep has no prompting signal, so there is
    nothing for the read to conflict *with* — treating the absence as a disagreement would
    manufacture conflicts out of routine polling.
    """
    configuration = config or default_configuration()

    with tenant_transaction(merchant_id, factory) as session:
        case_ids = [
            case.id
            for case in RecoveryCaseRepository(session).list_by_state(
                merchant_id, CaseState.WAITING_FOR_OUTCOME, limit=limit
            )
        ]

    return tuple(
        observe_payment_outcome(
            merchant_id,
            case_id,
            provider=provider,
            factory=factory,
            config=configuration,
            correlation_id=correlation_id,
        )
        for case_id in case_ids
    )


# ---------------------------------------------------------------------------
# Phase one — is a read warranted?
# ---------------------------------------------------------------------------


def _gate(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> OutcomeAssessment | None:
    """``None`` to proceed with a read; an assessment to stop here."""
    with tenant_transaction(merchant_id, factory) as session:
        cases = RecoveryCaseRepository(session)
        case = cases.lock_for_update(merchant_id, case_id)
        if case is None:
            return OutcomeAssessment(OutcomeVerdict.CASE_NOT_FOUND, case_id)

        if CaseState(case.state) is CaseState.RECOVERED:
            # R10.C13. No read, no state change, no second count. At-least-once delivery
            # makes this the ordinary path for a duplicate, not an exceptional one.
            _writer(session, config).write_for_case(
                merchant_id,
                case_id,
                AuditEntry(
                    event_type=DUPLICATE_RECOVERY_EVENT_DISCARDED,
                    actor=_ACTOR,
                    decision={"detail": "case already RECOVERED"},
                ),
                correlation_id=correlation_id,
            )
            return OutcomeAssessment(OutcomeVerdict.DUPLICATE_DISCARDED, case_id)

        return None


# ---------------------------------------------------------------------------
# Phase three — record the read and act on it
# ---------------------------------------------------------------------------


def _assess(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    result: ProviderResult[PaymentEntity],
    provider_payment_id: str,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    signal_status: str | None,
    correlation_id: uuid.UUID | None,
) -> OutcomeAssessment:
    with tenant_transaction(merchant_id, factory) as session:
        if not try_advisory_xact_lock(session, case_advisory_key(case_id)):
            return OutcomeAssessment(OutcomeVerdict.LOCK_UNAVAILABLE, case_id)

        cases = RecoveryCaseRepository(session)
        case = cases.lock_for_update(merchant_id, case_id)
        if case is None:  # pragma: no cover - the gate found it moments ago
            return OutcomeAssessment(OutcomeVerdict.CASE_NOT_FOUND, case_id)

        moment = now()
        writer = _writer(session, config)

        record = persist_read(
            session,
            merchant_id,
            case_id,
            result=result,
            provider_payment_id=provider_payment_id,
            moment=moment,
        )

        if record is None:
            return _handle_unreadable(
                session, merchant_id, case, writer,
                config=config, correlation_id=correlation_id, moment=moment,
            )

        # The last authoritative status is kept on the case so policy can read it without
        # joining. Written from the read, never from a webhook.
        case.verified_payment_status = record.entity.status

        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=PAYMENT_STATE_READ_RECORDED,
                actor=_ACTOR,
                decision={
                    "status": record.entity.status,
                    "captured": record.entity.captured,
                    "amount": int(record.entity.amount),
                    "amount_refunded": int(record.entity.amount_refunded),
                    "attempt_no": record.attempt_no,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

        # A signal that disagrees with the read holds the case. Checked before the recovery
        # decision, because a conflict means we do not yet know which is true.
        if _conflicts(signal_status, record):
            return _handle_conflict(
                session, merchant_id, case, writer, record,
                signal_status=signal_status, config=config,
                correlation_id=correlation_id, moment=moment,
            )

        if record.partial:
            writer.write_for_case(
                merchant_id,
                case_id,
                AuditEntry(
                    event_type=PARTIAL_PAYMENT_OBSERVED,
                    actor=_ACTOR,
                    decision={
                        "status": record.entity.status,
                        "amount": int(record.entity.amount),
                        "amount_refunded": int(record.entity.amount_refunded),
                        "detail": "partial payment is not recovery",
                    },
                ),
                correlation_id=correlation_id,
                occurred_at=moment,
            )
            return OutcomeAssessment(
                OutcomeVerdict.PARTIAL_HELD, case_id, read_id=record.row.id
            )

        if not record.recovered:
            return OutcomeAssessment(
                OutcomeVerdict.NOT_RECOVERED,
                case_id,
                read_id=record.row.id,
                detail=record.entity.status,
            )

        return _declare_recovery(
            session, merchant_id, case, writer, record,
            config=config, correlation_id=correlation_id, moment=moment,
        )


def _conflicts(signal_status: str | None, record: ReadRecord) -> bool:
    """Whether the prompting signal and the read disagree about the payment state.

    Only a *success* signal contradicted by a non-success read counts. The reverse — a read
    showing captured after a failure signal — is not a conflict but the ordinary sequence of
    a recovery, which is the entire thing this system is built to observe.
    """
    if signal_status is None:
        return False
    signal_says_paid = signal_status in {"captured", "paid", "authorized"}
    return signal_says_paid and not record.recovered and not record.partial


# ---------------------------------------------------------------------------
# The four endings
# ---------------------------------------------------------------------------


def _handle_unreadable(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    writer: AuditWriter,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> OutcomeAssessment:
    """The read did not complete. Hold, or escalate at the bound. Never declare."""
    writer.write_for_case(
        merchant_id,
        case.id,
        AuditEntry(
            event_type=PAYMENT_STATE_READ_UNAVAILABLE,
            actor=_ACTOR,
            decision={"detail": "authoritative read did not complete"},
        ),
        correlation_id=correlation_id,
    )

    # The attempt counter is the *consecutive* run of unavailable reads at the tail of this
    # case's audit history — R10.C7 asks for consecutive failures, and a successful read in
    # between resets it. Derived from the append-only log rather than a column, so it cannot
    # be lost or quietly reset. The record just written is part of the run.
    consecutive = _consecutive_unavailable_reads(session, merchant_id, case.id)
    if consecutive < int(config.MAX_PAYMENT_STATE_READ_ATTEMPTS):
        return OutcomeAssessment(
            OutcomeVerdict.READ_UNAVAILABLE, case.id, detail=f"attempt {consecutive}"
        )

    writer.write_for_case(
        merchant_id,
        case.id,
        AuditEntry(
            event_type=PAYMENT_STATE_UNVERIFIABLE,
            actor=_ACTOR,
            decision={
                "detail": "payment state unreadable after the attempt bound",
                "attempts": consecutive,
                "last_known_status": case.verified_payment_status,
                "unresolved_amount": int(case.payment_amount),
            },
        ),
        correlation_id=correlation_id,
    )
    if CaseState(case.state) not in TERMINAL_STATES:
        apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.ESCALATED,
            reason="payment state unverifiable",
            actor=_ACTOR,
            terminal_reason=TerminalReason.PAYMENT_STATE_UNVERIFIABLE,
            correlation_id=correlation_id,
            on_success=observation_writer(config, correlation_id=correlation_id),
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )
    return OutcomeAssessment(
        OutcomeVerdict.UNVERIFIABLE, case.id, detail=f"{consecutive} consecutive failures"
    )


def _handle_conflict(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    writer: AuditWriter,
    record: ReadRecord,
    *,
    signal_status: str | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> OutcomeAssessment:
    """Signals disagree. Hold in ``WAITING_FOR_OUTCOME`` and re-read, or escalate at the bound."""
    writer.write_for_case(
        merchant_id,
        case.id,
        AuditEntry(
            event_type=PAYMENT_STATE_CONFLICT,
            actor=_ACTOR,
            decision={
                "signal_status": signal_status,
                "read_status": record.entity.status,
                "read_captured": record.entity.captured,
                "attempt_no": record.attempt_no,
                "detail": "holding; no recovery declared",
            },
        ),
        correlation_id=correlation_id,
    )

    if record.attempt_no >= int(config.MAX_PAYMENT_STATE_READ_ATTEMPTS):
        writer.write_for_case(
            merchant_id,
            case.id,
            AuditEntry(
                event_type=PAYMENT_STATE_UNVERIFIABLE,
                actor=_ACTOR,
                decision={
                    "detail": "signals still disagree at the attempt bound",
                    "attempts": record.attempt_no,
                    "last_known_status": record.entity.status,
                    "unresolved_amount": int(case.payment_amount),
                },
            ),
            correlation_id=correlation_id,
        )
        if CaseState(case.state) not in TERMINAL_STATES:
            apply_locked_transition(
                session,
                merchant_id,
                case,
                expected_version=int(case.version),
                target_state=CaseState.ESCALATED,
                reason="payment state conflict unresolved at the attempt bound",
                actor=_ACTOR,
                terminal_reason=TerminalReason.PAYMENT_STATE_UNVERIFIABLE,
                correlation_id=correlation_id,
                on_success=observation_writer(config, correlation_id=correlation_id),
                disclosure_length=config.MASK_DISCLOSURE_LENGTH,
                max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
            )
        return OutcomeAssessment(
            OutcomeVerdict.UNVERIFIABLE, case.id, read_id=record.row.id,
            detail="conflict unresolved",
        )

    return OutcomeAssessment(
        OutcomeVerdict.CONFLICT_HELD, case.id, read_id=record.row.id,
        detail=f"attempt {record.attempt_no}",
    )


def _declare_recovery(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    writer: AuditWriter,
    record: ReadRecord,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> OutcomeAssessment:
    """The read says captured. Count it once, and settle the race with execution."""
    case_id = case.id
    intents = list(ExecutionIntentRepository(session).list_for_case(merchant_id, case_id))

    # Classification is withheld while any intent is UNCERTAIN (R10.C12): whether an action
    # reached the customer is unknown, so whether this recovery was natural or observed is
    # unknown too. Returning without recording lets a later pass do it properly — the amount
    # is not lost, because the unique constraint means it can still only be counted once.
    if any(IntentState(intent.state) is IntentState.UNCERTAIN for intent in intents):
        return OutcomeAssessment(
            OutcomeVerdict.WITHHELD_UNCERTAIN_INTENT,
            case_id,
            read_id=record.row.id,
            detail="an execution intent is still UNCERTAIN",
        )

    post_payment = _settle_action_race(
        session, merchant_id, case, writer, intents,
        correlation_id=correlation_id,
    )

    confirmed = sum(
        1 for intent in intents if IntentState(intent.state) is IntentState.CONFIRMED
    )
    # NATURAL with zero confirmed Revora actions, OBSERVED with one or more (R10.C8).
    # Never ATTRIBUTED: that claim needs a controlled comparison, and only the experiment
    # engine may make it. Defaulting to OBSERVED here would quietly turn correlation into
    # a causal claim in the table the headline figure is summed from.
    classification = OutcomeClass.OBSERVED if confirmed else OutcomeClass.NATURAL

    previous_state = CaseState(case.state)
    was_terminal = previous_state in TERMINAL_STATES

    recovered_at, timestamp_source = payment_timestamp(record.entity, fallback=record.row.read_at)
    seconds = max(int((recovered_at - case.detected_at).total_seconds()), 0)

    # The amount comes from the read, never from the webhook (R10.C3).
    recovered_amount = int(record.entity.amount)

    outcomes = RecoveryOutcomeRepository(session)
    if outcomes.for_case(merchant_id, case_id) is None:
        outcomes.add(
            merchant_id,
            RecoveryOutcome(
                case_id=case_id,
                classification=classification.value,
                recovered_amount=recovered_amount,
                recovery_timestamp=recovered_at,
                seconds_to_recovery=seconds,
                verified_by_read_id=record.row.id,
                reconciled_from_terminal_state=previous_state.value if was_terminal else None,
            ),
        )
        session.flush()

    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=RECOVERY_RECORDED,
            actor=_ACTOR,
            decision={
                "classification": classification.value,
                "recovered_amount": recovered_amount,
                "seconds_to_recovery": seconds,
                "verified_by_read_id": str(record.row.id),
                "recovery_timestamp_source": timestamp_source,
                "confirmed_actions": confirmed,
                "gross_of_refunds": True,
            },
        ),
        correlation_id=correlation_id,
    )

    if was_terminal:
        # R10.C14. One reconciliation transition, permitted at most once per case and only
        # against a verified capture — both conditions live in the transition rule, so this
        # cannot become a second count of the same money.
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=DELAYED_RECOVERY_RECONCILED,
                actor=_ACTOR,
                previous_state=previous_state.value,
                new_state=CaseState.RECOVERED.value,
                decision={"superseded_state": previous_state.value},
            ),
            correlation_id=correlation_id,
        )

    _, rejection = apply_locked_transition(
        session,
        merchant_id,
        case,
        expected_version=int(case.version),
        target_state=CaseState.RECOVERED,
        reason="authoritative read reports captured",
        actor=_ACTOR,
        verified_capture=True,
        correlation_id=correlation_id,
        # R15.C1: the observation shares this transaction. A recovered case with no
        # observation is a permanent hole in the training set, because nothing revisits a
        # terminal case to fill it in.
        on_success=observation_writer(config, correlation_id=correlation_id),
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )
    if rejection is not None:
        _logger.warning(
            "recovery verified but the transition to RECOVERED was refused",
            case_id=str(case_id),
            outcome=rejection.outcome.value,
            state=previous_state.value,
        )

    return OutcomeAssessment(
        OutcomeVerdict.DELAYED_RECOVERY if was_terminal else OutcomeVerdict.RECOVERED,
        case_id,
        read_id=record.row.id,
        recovered_amount=recovered_amount,
        classification=classification,
        superseded_state=previous_state if was_terminal else None,
        post_payment_intents=post_payment,
    )


def _settle_action_race(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    writer: AuditWriter,
    intents: list[ExecutionIntent],
    *,
    correlation_id: uuid.UUID | None,
) -> int:
    """Resolve what happens to an action when the customer paid first.

    Two endings, and which one applies is decided by whether an intent exists — which is
    exactly the question "did anything go out?".

    No intent and an action scheduled: cancel before any call, counters untouched, audit
    ``ACTION_CANCELLED_PAYMENT_RECEIVED`` (R10.C4). The good ending — the customer paid on
    their own and we noticed in time to stay quiet.

    An intent exists: the action went out to someone who had already paid. Flag it
    ``is_post_payment`` and audit ``POST_PAYMENT_ACTION`` (R10.C5), counted exactly once
    because the flag is idempotent. Returns how many were flagged.
    """
    if not intents:
        if CaseState(case.state) is CaseState.ACTION_SCHEDULED:
            writer.write_for_case(
                merchant_id,
                case.id,
                AuditEntry(
                    event_type=ACTION_CANCELLED_PAYMENT_RECEIVED,
                    actor=_ACTOR,
                    decision={
                        "detail": "payment confirmed before any external call; "
                        "action cancelled, counters unchanged"
                    },
                ),
                correlation_id=correlation_id,
            )
        return 0

    flagged = 0
    for intent in intents:
        if intent.is_post_payment:
            # Already counted on an earlier pass. Exactly-once, by the flag.
            continue
        intent.is_post_payment = True
        flagged += 1
        writer.write_for_case(
            merchant_id,
            case.id,
            AuditEntry(
                event_type=POST_PAYMENT_ACTION,
                actor=_ACTOR,
                action=str(intent.action),
                idempotency_key=intent.idempotency_key,
                decision={
                    "intent_state": str(intent.state),
                    "detail": "action was in flight or completed when the payment confirmed",
                },
            ),
            correlation_id=correlation_id,
        )
    return flagged


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def record_post_suppression_actions(
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    writer: AuditWriter,
    intents: Sequence[ExecutionIntent],
    *,
    correlation_id: uuid.UUID | None = None,
) -> int:
    """One ``POST_SUPPRESSION_ACTION`` per intent that was already in flight (R21.C7).

    The Outcome_Monitor's half of a suppression's arrival. It lives here rather than in the
    suppression handler because R21.C7 names this component, and because its twin — the
    ``POST_PAYMENT_ACTION`` branch of :func:`_settle_action_race` — is a few lines below. The two
    answer the same question, *something went out and the timing turned out to be wrong*, for two
    different causes. Splitting them across modules would mean the next person changing how an
    in-flight action is recorded finding one of them.

    **No further external call is issued, and nothing in this function is what stops one.** The
    stopping is the terminal transition the caller applies in the same transaction: every queued
    execution job re-evaluates policy against reloaded state and refuses on ``ALREADY_TERMINAL``,
    and an ``UNCERTAIN`` intent resolves through the reconciliation path of R9.C15 — which is a
    *read*, not a message. This function only records, and it deliberately does not touch
    ``intent.state``: a customer's objection is not evidence about whether the provider did the
    thing, and rewriting the state on the strength of it would destroy the only record of what the
    provider actually said.

    Takes an already-loaded ``intents`` sequence and a writer rather than opening its own
    transaction, so the records land in the caller's — the same transaction as the escalation. A
    suppression whose escalation committed without these records would leave a case that looks
    like nothing had gone out.

    Returns how many records were written.
    """
    written = 0
    for intent in intents:
        if IntentState(str(intent.state)) not in IN_FLIGHT_INTENT_STATES:
            continue
        written += 1
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=POST_SUPPRESSION_ACTION,
                actor=_ACTOR,
                action=str(intent.action),
                idempotency_key=intent.idempotency_key,
                decision={
                    "intent_state": str(intent.state),
                    "detail": "action was in flight when contact suppression was recorded; "
                    "no further external call, resolving through reconciliation",
                },
            ),
            correlation_id=correlation_id,
        )
    return written


def _consecutive_unavailable_reads(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> int:
    """The trailing run of unavailable-read records in this case's audit history.

    Walks backwards from the newest record and stops at the first one that is not an
    unavailable read, which is what makes the count *consecutive* as R10.C7 requires — a
    successful read in between resets it, and it should.
    """
    history = AuditRecordRepository(session).list_for_case(merchant_id, case_id)
    run = 0
    for record in reversed(list(history)):
        if str(record.event_type) == PAYMENT_STATE_READ_UNAVAILABLE:
            run += 1
            continue
        if str(record.event_type) == PAYMENT_STATE_READ_RECORDED:
            break
        # Any other event type is unrelated bookkeeping and does not break the run.
    return run


def _writer(session: Session, config: Configuration) -> AuditWriter:
    return AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

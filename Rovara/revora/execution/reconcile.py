"""Reconciliation: resolving an unresolved intent by reading, never by calling again.

This is the recovery path for every crash the execution engine cannot prevent, and it has
exactly one rule that matters: **it never issues a create call.** Not on a first pass, not
after an empty read, not when the attempt bound is exhausted. The only provider operation
reachable from this module is ``find_payment_links_by_reference_id``, which is a read.

That rule is what makes the engine's pessimistic ordering safe. The engine commits an intent
before calling out, accepting that a crash strands an ``ATTEMPTED`` row whose outcome nobody
knows. Stranding it is only acceptable because something comes along afterwards and
*establishes* the outcome by observation. If reconciliation could call again, the whole
arrangement would be a slower route to a duplicate payment link.

Two subtleties carry most of the risk.

**An empty listing is not evidence of absence — until it is.** A just-created payment link
may not appear in the fetch-all listing immediately, and the design marks that lag
``[EVIDENCE INSUFFICIENT]``: no measurement exists. So an empty read leaves the intent
``UNCERTAIN`` and tries again later, and only the *final* attempt is permitted to conclude
``FAILED``. Concluding early would free the case for a fresh attempt while a payable link is
outstanding — the duplicate, arriving by a legitimate-looking route.

**Running out of attempts is a real outcome, not a bug to code around.** When the bound is
exhausted without an answer, the case escalates with ``EXECUTION_RESULT_UNVERIFIABLE`` and no
further external call is ever issued for it. This looks bad in a dashboard and it is the
correct behaviour: the alternatives are to guess "it worked" and abandon a customer mid-flow,
or guess "it failed" and ask them to pay again. A human picking up a handful of cases is the
cheapest of the three.

**The interval is the scheduler's job, not a column's.** One call to
:func:`reconcile_intents` is one attempt per eligible intent. Spacing between attempts comes
from the sweep being enqueued every ``EXECUTION_RECONCILIATION_INTERVAL``, which is why there
is no ``last_reconciled_at`` column here — the schedule already carries that information, and
a second copy of it in a row would be a second thing to keep correct.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from revora.audit.events import EXECUTION_INTENT_PROMOTED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.manager import apply_locked_transition
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, IntentState
from revora.execution.escalation import escalate_unverifiable
from revora.execution.intents import (
    RESOLVED_STATES,
    resolve_from_listing,
    stale_attempted_cutoff,
)
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.session import (
    case_advisory_key,
    tenant_transaction,
    try_advisory_xact_lock,
)
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.logging import get_logger
from revora.providers.classification import Success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.persistence.models import ExecutionIntent, RecoveryCase
    from revora.platform.config import Configuration
    from revora.providers.razorpay import PaymentProviderClient

__all__ = [
    "DEFAULT_SWEEP_LIMIT",
    "ReconcileOutcome",
    "ReconcileResult",
    "promote_stale_intents",
    "reconcile_intents",
    "unresolved_intent_count",
]

_logger = get_logger(__name__)

_ACTOR = "execution_reconciler"

DEFAULT_SWEEP_LIMIT = 100
"""Intents per sweep. Bounded so one pass is a predictable amount of work and a large
backlog is drained across several passes rather than in one long transaction."""


@unique
class ReconcileOutcome(StrEnum):
    """Why the sweep left an intent where it did."""

    SKIPPED_NOT_STALE = "SKIPPED_NOT_STALE"
    """``ATTEMPTED`` and younger than one provider-call timeout. The worker that owns it may
    still be inside its call; touching it now would race a live attempt."""

    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    """Another worker holds the case. Left for the next pass."""

    CONFIRMED = "CONFIRMED"
    """The listing showed the link. The effect exists and is now recorded."""

    FAILED = "FAILED"
    """The final attempt read an empty listing. Only reachable on the final attempt."""

    STILL_UNCERTAIN = "STILL_UNCERTAIN"
    """No answer yet — empty listing before the bound, or a read error. Retried later."""

    ESCALATED = "ESCALATED"
    """The bound is exhausted and the outcome is still unknown. A human owns it now, and no
    further external call is ever issued for this case."""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one sweep concluded about one intent."""

    intent_id: uuid.UUID
    idempotency_key: str
    outcome: ReconcileOutcome
    state: IntentState
    attempts: int


def reconcile_intents(
    merchant_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    limit: int = DEFAULT_SWEEP_LIMIT,
    correlation_id: uuid.UUID | None = None,
) -> tuple[ReconcileResult, ...]:
    """One sweep over this merchant's unresolved intents.

    Safe to run concurrently with itself and with the engine: each intent is handled under
    the same per-case advisory lock the engine takes, and a contended case is skipped rather
    than queued behind.

    Returns one result per intent considered, including the skipped ones, so a caller can
    tell "nothing needed doing" from "nothing was reachable".
    """
    configuration = config or default_configuration()
    moment = now()

    # Claimed in its own short transaction. Holding the claim across the provider reads
    # would keep a lock open for the duration of an external call, which is the mistake the
    # engine is arranged to avoid — the same reasoning applies here.
    with tenant_transaction(merchant_id, factory) as session:
        candidates = [
            (
                intent.id,
                intent.idempotency_key,
                IntentState(intent.state),
                intent.attempt_started_at,
            )
            for intent in ExecutionIntentRepository(session).claim_unresolved(
                merchant_id, started_before=moment, limit=limit
            )
        ]

    if not candidates:
        return ()

    cutoff = stale_attempted_cutoff(configuration.PROVIDER_CALL_TIMEOUT, moment)
    results: list[ReconcileResult] = []

    for intent_id, key, state, started_at in candidates:
        if state is IntentState.ATTEMPTED and started_at > cutoff:
            # Possibly still in flight. Not ours to touch.
            results.append(
                ReconcileResult(intent_id, key, ReconcileOutcome.SKIPPED_NOT_STALE, state, 0)
            )
            continue

        result = _reconcile_one(
            merchant_id,
            intent_id=intent_id,
            idempotency_key=key,
            provider=provider,
            factory=factory,
            config=configuration,
            correlation_id=correlation_id,
        )
        results.append(result)

    return tuple(results)


def _reconcile_one(
    merchant_id: uuid.UUID,
    *,
    intent_id: uuid.UUID,
    idempotency_key: str,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> ReconcileResult:
    """Read the provider for one key and record what it says.

    The read happens *outside* any transaction, between two short ones, for the same reason
    the engine's call does: a database lock must never span an external request.
    """
    # The read. A pure query against the idempotency key, which is also the provider's
    # reference_id — one derivation, used for the write and the lookup alike.
    listing = provider.find_payment_links_by_reference_id(idempotency_key)

    found = False
    provider_response_id: str | None = None
    provider_short_url: str | None = None
    read_succeeded = isinstance(listing, Success)

    if read_succeeded:
        assert isinstance(listing, Success)
        links = listing.entity.links
        found = bool(links)
        if found:
            provider_response_id = links[0].id
            provider_short_url = links[0].short_url

    with tenant_transaction(merchant_id, factory) as session:
        intents = ExecutionIntentRepository(session)
        intent = intents.get(merchant_id, intent_id)
        if intent is None:  # pragma: no cover - claimed moments ago
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.SKIPPED_LOCKED,
                IntentState.UNCERTAIN, 0,
            )

        state = IntentState(intent.state)
        if state in RESOLVED_STATES:
            # Someone resolved it between the claim and now. Their answer stands.
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.CONFIRMED
                if state is IntentState.CONFIRMED else ReconcileOutcome.FAILED,
                state, int(intent.reconciliation_attempts),
            )

        if not try_advisory_xact_lock(session, case_advisory_key(intent.case_id)):
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.SKIPPED_LOCKED, state,
                int(intent.reconciliation_attempts),
            )

        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, intent.case_id)
        if case is None:  # pragma: no cover - foreign key guarantees it
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.SKIPPED_LOCKED, state,
                int(intent.reconciliation_attempts),
            )

        moment = now()
        writer = AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )

        # A stale ATTEMPTED becomes UNCERTAIN with a record, because the two describe the
        # same fact — nobody knows whether the provider acted — and the sweeper's partial
        # index is keyed on the state.
        if state is IntentState.ATTEMPTED:
            intent.state = IntentState.UNCERTAIN.value
            intent.resolved_at = None
            writer.write_for_case(
                merchant_id,
                intent.case_id,
                AuditEntry(
                    event_type=EXECUTION_INTENT_PROMOTED,
                    actor=_ACTOR,
                    action=str(intent.action),
                    idempotency_key=idempotency_key,
                    decision={"detail": "attempted past provider call timeout"},
                ),
                correlation_id=correlation_id,
                occurred_at=moment,
            )

        attempts = int(intent.reconciliation_attempts) + 1
        intent.reconciliation_attempts = attempts
        bound = int(config.MAX_EXECUTION_RECONCILIATION_ATTEMPTS)

        # A read error is not an empty listing. It advances the attempt counter — otherwise a
        # persistently unreachable provider would sweep forever — but it must never be read
        # as "the link is absent", so `found` stays false and only the final-attempt branch
        # can conclude anything from it.
        is_final = attempts >= bound

        settled = resolve_from_listing(
            intent,
            found=found,
            provider_response_id=provider_response_id,
            provider_short_url=provider_short_url,
            is_final_attempt=is_final and read_succeeded,
            moment=moment,
        )

        if settled is IntentState.CONFIRMED:
            _advance_confirmed(
                session, merchant_id, case, intent, config=config,
                correlation_id=correlation_id,
            )
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.CONFIRMED, settled, attempts
            )

        if settled is IntentState.FAILED:
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.FAILED, settled, attempts
            )

        # Still unknown. If the bound is spent, hand it to a human and stop forever.
        if is_final:
            _escalate(
                session, merchant_id, case, intent, idempotency_key=idempotency_key,
                config=config, correlation_id=correlation_id, moment=moment, writer=writer,
            )
            return ReconcileResult(
                intent_id, idempotency_key, ReconcileOutcome.ESCALATED, settled, attempts
            )

        return ReconcileResult(
            intent_id, idempotency_key, ReconcileOutcome.STILL_UNCERTAIN, settled, attempts
        )


def _advance_confirmed(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    intent: ExecutionIntent,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None,
) -> None:
    """Move a case whose effect has just been confirmed by reading.

    ``counter_applied`` gates the counter bookkeeping, not the transition: the counters moved
    on the edge into ``EXECUTING`` before the call, so confirming later must not move them
    again. The flag is what lets this path run after a partial crash without double-counting.
    """
    intent.counter_applied = True

    if CaseState(case.state) is CaseState.EXECUTING:
        apply_locked_transition(
            session,
            merchant_id,
            case,
            expected_version=int(case.version),
            target_state=CaseState.WAITING_FOR_OUTCOME,
            reason="payment link confirmed by reconciliation read",
            actor=_ACTOR,
            action=CandidateAction(intent.action),
            correlation_id=correlation_id,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        )


def _escalate(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    intent: ExecutionIntent,
    *,
    idempotency_key: str,
    config: Configuration,
    correlation_id: uuid.UUID | None,
    moment: datetime,
    writer: AuditWriter,
) -> None:
    """Hand an unverifiable intent to a human. No further external call, ever.

    The disposition lives in :func:`revora.execution.escalation.escalate_unverifiable`, shared
    with the resend path — which reaches the same conclusion for a different reason and with a
    bound of zero attempts instead of six. What is local to reconciliation is only the
    *evidence*: the reads ran out. The intent is deliberately left ``UNCERTAIN`` rather than
    forced to ``FAILED``, because ``FAILED`` licenses another attempt under a new key and a
    payment link may be live and unaccounted for.
    """
    escalate_unverifiable(
        session,
        merchant_id,
        case,
        action=CandidateAction(intent.action),
        idempotency_key=idempotency_key,
        detail="reconciliation attempts exhausted without establishing the effect",
        attempts=int(intent.reconciliation_attempts),
        actor=_ACTOR,
        config=config,
        correlation_id=correlation_id,
        moment=moment,
        writer=writer,
    )


def promote_stale_intents(
    merchant_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    limit: int = DEFAULT_SWEEP_LIMIT,
    correlation_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, ...]:
    """Startup step: move every abandoned ``ATTEMPTED`` intent to ``UNCERTAIN``.

    Composed into the restart sequence by the process bootstrap, which sits above both this
    layer and ``revora.cases`` — the case-reload step cannot call this itself without
    inverting the dependency rule, which is why the two halves of the restart sequence live
    apart and are joined at the top.

    An intent still ``ATTEMPTED`` after a restart is one whose worker died mid-call. The
    promotion is the whole of the fix: it makes the sweeper's partial index find the row, so
    the outcome gets established by reading. **No call is repeated and no counter moves.** The
    interrupted attempt keeps its key, which is what lets the eventual read ask the provider
    about the very effect that attempt may have produced — a fresh key would ask about an
    effect nobody ever requested and get a truthful, useless "no".

    Returns the promoted intent ids, so a bootstrap can log how much was interrupted. A
    non-empty result after a clean shutdown is worth investigating.
    """
    configuration = config or default_configuration()
    moment = now()
    cutoff = stale_attempted_cutoff(configuration.PROVIDER_CALL_TIMEOUT, moment)
    promoted: list[uuid.UUID] = []

    with tenant_transaction(merchant_id, factory) as session:
        writer = AuditWriter(
            session,
            disclosure_length=configuration.MASK_DISCLOSURE_LENGTH,
            max_field_length=configuration.MAX_AUDIT_FIELD_LENGTH,
        )
        cases = RecoveryCaseRepository(session)

        for intent in ExecutionIntentRepository(session).claim_unresolved(
            merchant_id, started_before=cutoff, limit=limit
        ):
            if IntentState(intent.state) is not IntentState.ATTEMPTED:
                continue

            # The audit sequence is allocated off the case row, so it has to be held.
            if cases.lock_for_update(merchant_id, intent.case_id) is None:  # pragma: no cover
                continue

            intent.state = IntentState.UNCERTAIN.value
            intent.resolved_at = None
            writer.write_for_case(
                merchant_id,
                intent.case_id,
                AuditEntry(
                    event_type=EXECUTION_INTENT_PROMOTED,
                    actor=_ACTOR,
                    action=str(intent.action),
                    idempotency_key=intent.idempotency_key,
                    decision={"detail": "attempted intent interrupted by restart"},
                ),
                correlation_id=correlation_id,
                occurred_at=moment,
            )
            promoted.append(intent.id)

    if promoted:
        _logger.warning(
            "promoted interrupted execution intents on startup",
            merchant_id=str(merchant_id),
            count=len(promoted),
        )
    return tuple(promoted)


def unresolved_intent_count(
    merchant_id: uuid.UUID, *, factory: sessionmaker[Session] | None = None
) -> int:
    """How many intents are unresolved right now. The alerting metric.

    Exposed because this path fails safe but silently: a rising count means effects are going
    unverified, and nothing else in the system will complain about it. Safe-but-silent is the
    failure mode that gets discovered late.
    """
    with tenant_transaction(merchant_id, factory) as session:
        return len(
            ExecutionIntentRepository(session).claim_unresolved(
                merchant_id, started_before=now(), limit=10_000
            )
        )

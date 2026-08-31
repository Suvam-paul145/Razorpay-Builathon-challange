"""The durable execution intent: what makes at-most-once possible across a crash.

The whole of Property 3 reduces to one ordering decision, and it is worth stating before
any code: **the intent is committed before the call goes out.** Not after, not alongside.

The alternative — call first, record the result — is the obvious design and it is
unrecoverable. A worker that dies between the call and the write leaves a provider holding
an effect that nothing in the database knows about, and the next worker has no way to
distinguish that from a call that never happened. It will call again. That is a customer
receiving two requests to pay one invoice.

Committing first inverts the failure. A worker that dies after the commit leaves a durable
``ATTEMPTED`` row with no result, and the next worker knows exactly what it is looking at:
a call that may or may not have landed, which must be *resolved by reading* and never by
calling again. The cost is real and is accepted deliberately — a crash in that window burns
one recovery attempt on an action that may never have gone out. The design under-attempts
on purpose, because the opposite error charges people money.

Two mechanisms enforce this rather than one, and they are independent:

* ``uq_execution_intent_merchant_id_idempotency_key`` — a second intent for one key cannot
  be committed, so a second call cannot be authorized. This is ours and it is absolute.
* The same key travels to the provider as ``reference_id``. Their side sees the duplicate
  too. Whether they reject it is unverified (spike 3 exists to find out) and nothing here
  depends on the answer — it is a second lock on a door that is already locked.

Nothing in this module makes a provider call. It decides whether one is permitted and
records the answer; the engine makes the call, outside any lock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from revora.domain.actions import CandidateAction
from revora.domain.enums import IntentState
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.platform.clock import now
from revora.providers.classification import (
    ClientError,
    EffectCertainty,
    ProviderResult,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    effect_certainty,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.persistence.models import ExecutionIntent
    from revora.providers.classification import PaymentLinkEntity

__all__ = [
    "RESOLVED_STATES",
    "UNRESOLVED_STATES",
    "IntentDisposition",
    "ReservedIntent",
    "classify_into_intent_state",
    "existing_intent_disposition",
    "record_result",
    "reserve_intent",
    "resolve_from_listing",
    "stale_attempted_cutoff",
]

RESOLVED_STATES: frozenset[IntentState] = frozenset(
    {IntentState.CONFIRMED, IntentState.FAILED}
)
"""The two states that are final. A resolved intent is never revisited and never
re-attempted: its recorded result *is* the answer for that key, forever."""

UNRESOLVED_STATES: frozenset[IntentState] = frozenset(
    {IntentState.ATTEMPTED, IntentState.UNCERTAIN}
)
"""The two states that mean "nobody knows yet". Both hand to reconciliation. Neither ever
permits another call — that is the single most important sentence in this package."""


@unique
class IntentDisposition(StrEnum):
    """What an existing intent for this key means for the caller.

    Three answers, and the engine branches on exactly this. Deliberately not the intent
    state itself: ``ATTEMPTED`` and ``UNCERTAIN`` are different records but the same
    instruction — *do not call, hand to reconciliation* — and collapsing them here means
    the engine cannot accidentally treat one of them as callable.
    """

    ABSENT = "ABSENT"
    """No intent under this key. A call is permitted once one is committed."""

    RESOLVED = "RESOLVED"
    """Already ``CONFIRMED`` or ``FAILED``. Return the recorded result unchanged."""

    IN_FLIGHT = "IN_FLIGHT"
    """``ATTEMPTED`` or ``UNCERTAIN``. Hand to reconciliation. Never call."""


@dataclass(frozen=True, slots=True)
class ReservedIntent:
    """A committed claim on the right to make exactly one call.

    ``reserved`` false means another transaction won the race for this key. That is not an
    error and must not be retried as one — it means a call either has happened or is about
    to, by someone else, and this worker's correct behaviour is to do nothing external.
    """

    intent_id: uuid.UUID | None
    idempotency_key: str
    reserved: bool

    @property
    def may_call(self) -> bool:
        """True only where this transaction owns the right to call."""
        return self.reserved and self.intent_id is not None


def existing_intent_disposition(
    session: Session, merchant_id: uuid.UUID, idempotency_key: str
) -> tuple[IntentDisposition, ExecutionIntent | None]:
    """What the record already says about this key.

    Read under the caller's lock, before anything is inserted. Returns the row alongside
    the disposition so a caller returning a recorded result does not read it twice.
    """
    intent = ExecutionIntentRepository(session).get_by_idempotency_key(
        merchant_id, idempotency_key
    )
    if intent is None:
        return IntentDisposition.ABSENT, None
    state = IntentState(intent.state)
    if state in RESOLVED_STATES:
        return IntentDisposition.RESOLVED, intent
    return IntentDisposition.IN_FLIGHT, intent


def reserve_intent(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    case_id: uuid.UUID,
    policy_decision_id: uuid.UUID,
    idempotency_key: str,
    action: CandidateAction,
    attempt_ordinal: int,
    moment: datetime | None = None,
) -> ReservedIntent:
    """Insert the ``ATTEMPTED`` intent. The caller commits it before calling out.

    ``ON CONFLICT DO NOTHING`` rather than a pre-read: checking first and inserting second
    is a race, and the two workers who both pass the check both call. Letting the database
    arbitrate means exactly one insert returns an id and the loser learns it lost — from the
    same statement, under the same isolation, with no window in between.

    ``resolved_at`` is left null, which the ``resolved_at_iff_resolved`` check requires of
    an unresolved state. ``counter_applied`` starts false and is flipped by whichever path
    applies the counters, so a reconciliation that runs after a partial crash cannot count
    the same attempt twice.
    """
    intent_id = ExecutionIntentRepository(session).reserve(
        merchant_id,
        idempotency_key=idempotency_key,
        values={
            "case_id": case_id,
            "policy_decision_id": policy_decision_id,
            "action": action.value,
            "attempt_ordinal": attempt_ordinal,
            "state": IntentState.ATTEMPTED.value,
            "attempt_started_at": moment or now(),
        },
    )
    return ReservedIntent(
        intent_id=intent_id,
        idempotency_key=idempotency_key,
        reserved=intent_id is not None,
    )


def classify_into_intent_state(result: ProviderResult[PaymentLinkEntity]) -> IntentState:
    """Map a provider result onto the state that describes it honestly.

    Branches on :func:`effect_certainty`, not on the result variant, and the difference
    matters. ``Timeout`` is one class in the type system and two situations in reality: a
    connect-phase timeout definitively sent nothing, while a read timeout may have created a
    payment link. Matching on the variant would collapse them and get one wrong, and the one
    it gets wrong is the one that duplicates an effect.

    ``ClientError`` is the only failure that becomes ``FAILED`` here, because a parsed 4xx
    is the provider stating it did not act. Everything uncertain becomes ``UNCERTAIN``,
    which halts external calls for the case until a read resolves it.
    """
    certainty = effect_certainty(result)
    if certainty is EffectCertainty.EXISTS:
        return IntentState.CONFIRMED
    if certainty is EffectCertainty.DOES_NOT_EXIST:
        return IntentState.FAILED
    return IntentState.UNCERTAIN


def record_result(
    intent: ExecutionIntent,
    result: ProviderResult[PaymentLinkEntity],
    *,
    moment: datetime | None = None,
) -> IntentState:
    """Write the call's outcome onto the intent row. Returns the state it settled in.

    Idempotent by refusal: an intent already in a resolved state is left exactly as it is
    and its recorded state returned. That guard is what makes the *record* stable rather
    than merely the call count — an engine that let a later pass overwrite ``CONFIRMED``
    with ``FAILED`` would re-open the case for a fresh attempt against a link that is still
    live, and would have done so without issuing a second call.
    """
    current = IntentState(intent.state)
    if current in RESOLVED_STATES:
        return current

    state = classify_into_intent_state(result)
    when = moment or now()

    if isinstance(result, Success):
        intent.provider_response_id = result.entity.id
        # A payment link URL is a bearer capability: whoever holds it can pay the invoice.
        # Stored because the dashboard has to show it, and never written to a log line or
        # an audit field.
        intent.provider_short_url = result.entity.short_url
    elif isinstance(result, ClientError):
        intent.provider_failure_code = result.code
    elif isinstance(result, ServerError):
        intent.provider_failure_code = f"HTTP_{result.http_status}"
    elif isinstance(result, Timeout):
        intent.provider_failure_code = f"TIMEOUT_{result.phase.value}"
    elif isinstance(result, Unclassifiable):
        intent.provider_failure_code = "UNCLASSIFIABLE"

    intent.state = state.value
    # The `resolved_at_iff_resolved` check is an equality of booleans, so this has to be
    # set for exactly the resolved states and left null otherwise.
    intent.resolved_at = when if state in RESOLVED_STATES else None
    return state


def resolve_from_listing(
    intent: ExecutionIntent,
    *,
    found: bool,
    provider_response_id: str | None = None,
    provider_short_url: str | None = None,
    is_final_attempt: bool = False,
    moment: datetime | None = None,
) -> IntentState:
    """Resolve an unresolved intent from a reconciliation read of the provider's listing.

    ``found`` true confirms immediately: the effect exists, so the intent that produced it
    is ``CONFIRMED`` and no further call is permitted for that key ever.

    ``found`` false is the delicate case and is **not** treated as failure except on the
    final attempt. An empty listing cannot be distinguished from read-after-write lag, and
    the design marks that lag ``[EVIDENCE INSUFFICIENT]`` — no measurement exists for how
    long a just-created link may be invisible. Concluding ``FAILED`` early would free the
    case for a new attempt while a payable link is outstanding, which is the duplicate this
    package exists to prevent, arriving by a legitimate-looking route.

    Args:
        is_final_attempt: whether the reconciliation attempt bound is now exhausted. Only
            then may an empty read mean ``FAILED``.
    """
    current = IntentState(intent.state)
    if current in RESOLVED_STATES:
        return current

    when = moment or now()

    if found:
        intent.state = IntentState.CONFIRMED.value
        intent.resolved_at = when
        if provider_response_id is not None:
            intent.provider_response_id = provider_response_id
        if provider_short_url is not None:
            intent.provider_short_url = provider_short_url
        return IntentState.CONFIRMED

    if is_final_attempt:
        intent.state = IntentState.FAILED.value
        intent.resolved_at = when
        intent.provider_failure_code = intent.provider_failure_code or "NOT_FOUND_ON_FINAL_READ"
        return IntentState.FAILED

    # Still unknown. Promote out of ATTEMPTED so the sweeper's partial index keeps finding
    # it, and leave resolved_at null — the check constraint requires that of any state
    # other than CONFIRMED or FAILED.
    intent.state = IntentState.UNCERTAIN.value
    intent.resolved_at = None
    return IntentState.UNCERTAIN


def stale_attempted_cutoff(
    provider_call_timeout: timedelta, moment: datetime | None = None
) -> datetime:
    """The instant before which an ``ATTEMPTED`` intent must be considered abandoned.

    An intent still ``ATTEMPTED`` past one provider-call timeout means the worker that owned
    it died mid-call — nobody is coming back to record the result. Promoting it to
    ``UNCERTAIN`` routes it to a read; leaving it would strand the case silently, which is
    the failure mode this whole package is arranged against.
    """
    return (moment or now()) - provider_call_timeout

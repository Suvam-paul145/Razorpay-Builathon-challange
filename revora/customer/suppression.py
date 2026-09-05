"""Contact_Suppression — the record, its scope key, and the lookup policy check 5 reads.

A dispute and a cancellation are not payment problems. They are objections to the debt
itself, so R21 ends automated contact within the objection's scope permanently rather than
scheduling one more message. This module owns the three halves of that: deriving the
Suppression_Scope key, writing the record beside the Customer_Signal that caused it, and
answering "is this scope suppressed right now" for the one policy check that already exists
to refuse contact.

**There is no thirteenth policy check.** The suppression enters the input of check 5,
``CUSTOMER_OPTED_OUT``, which sits fifth of twelve — ahead of the window and all three
counters — precisely because absolute prohibitions belong before every bound. A thirteenth
check would have had to be ordered somewhere, and every position available to it is *after*
a bound, which is the one place a prohibition must not be: a bug in an attempt counter would
then be able to become the recorded reason a customer who disputed a charge was messaged
again. Extending an existing check's input costs nothing structurally and inherits the
ordering argument already made for it (R21.C3, and ``policy.engine`` on the fixed order).

**The suppression is resolved by the caller and passed in.** ``revora/policy/`` is a pure
function that may import only ``revora/domain/`` and ``revora/platform/`` — the
``policy-isolation`` contract in ``.importlinter`` forbids ``revora.persistence`` among
others — so the engine cannot perform this lookup and must not. It arrives on
:class:`~revora.policy.input.PolicyInput` as a named boolean, exactly as every other check's
input does, and ``revora.execution.authorization`` is the one place that resolves it. That is
not a workaround for the contract; it is the contract working. A repository call from inside
the engine would fail ``lint-imports``, and the failure would be correct.

**``scope_key = sha256(customer_key ‖ order_id or case_id)``.** A hash rather than a
composite of readable parts, for one reason: the column is indexed and compared on the policy
hot path, and a readable composite would mean a second copy of ``customer_key`` living beside
an order id in a table that is scanned on every decision. ``customer_key`` is already the
widest-spread customer-derived value in the database and the argument in
``revora.platform.crypto`` for making it non-reversible applies with more force to a second
copy of it, not less. The preimage stays recoverable — the suppression names an
``origin_case_id``, and that ``recovery_case`` row holds both halves — so nothing is lost
except the ability to read a customer identifier off this table.

The order identifier is preferred over the case identifier where the Payment_Event carried
one, which is an **[ASSUMPTION]** recorded in the requirements glossary: it means a dispute
about one order does not suppress a different order for the same customer. That is the
narrower reading, and it is the one that can be widened later without losing data — widening
means hashing fewer inputs, and every historical row keeps its scope. The reverse, starting
customer-wide and narrowing, would need a preimage this table deliberately does not hold.

**Nothing here sets the customer-wide opt-out status.** R21.C9 is explicit and it is the
easiest thing in this requirement to get subtly wrong: ``customer_consent.opted_out`` is a
statement that a person withdrew consent to be contacted at all, and a suppression is a
statement that a person objects to *one debt*. Collapsing the two would make an objection to
one charge indistinguishable from a withdrawal of consent, which is both wrong about what the
customer said and unrecoverable — there is no record left of which of the two it was. So the
suppression blocks contact through its own scoped record and the consent row is not touched.
Property 40 asserts the status is unchanged, and this module writing no consent row is why
that assertion is a structural fact rather than a diligence one: there is no consent
repository imported here.

**What the write is atomic with.** The record joins the transaction that persisted the
Customer_Signal (R21.C1), which is the same transaction that holds the token row lock, the
submission-count increment, the enqueued review and — last — the audit record. So a hard stop
either produced a signal *and* a suppression *and* revoked the case's tokens, or produced
none of them. Token revocation is R21.C10 and belongs in this transaction for the same
reason: a customer whose case has been suppressed must not still hold a working link into it,
and a revocation that happened in a later transaction would leave a window in which they do.

**What the write deliberately does not do.** It applies no state transition, schedules no
action, evaluates no policy and issues no provider request, because R19.C9 forbids all four
inside the request that accepted a signal. The escalation to ``ESCALATED``, the cancellation
of scheduled actions and the treatment of an in-flight intent are R21.C4 through C7 and are
applied by the worker through ``apply_transition``, which stays the only writer of
``recovery_case.state``. Revoking a token is none of those four things — it writes
``customer_access_token``, a table the Customer_Response_Service owns — which is why it is
here and the transition is not.

What it does instead is **enqueue** them, in the same transaction, which is the second half of
R19.C9: *SHALL enqueue the consequences of an accepted Customer_Signal for the worker to apply*.
:data:`CONTACT_SUPPRESSION_KIND` is declared here rather than in ``revora.jobs.scheduler`` for
the reason ``CASE_REVIEW_KIND`` is declared in ``revora.cases.review``: the enqueuer sits below
``revora.jobs`` and cannot import it, and a constant each side spelled for itself would give one
dedupe key two spellings. ``revora.jobs.worker`` imports it from here, which is the direction the
layering allows.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from revora.customer.tokens import revoke_tokens_for_case
from revora.domain.enums import DelayReason, HardStopReason, TokenRevocationReason
from revora.persistence.repositories.customer import ContactSuppressionRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.platform.clock import ensure_utc, now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

__all__ = [
    "CONTACT_SUPPRESSION_KIND",
    "HARD_STOP_FOR",
    "SCOPE_SEPARATOR",
    "SuppressionOutcome",
    "SuppressionScopeFacts",
    "hard_stop_for",
    "record_hard_stop",
    "release_suppression",
    "scope_key_for_case",
    "suppression_in_force",
    "suppression_scope_key",
]

_logger = get_logger(__name__)


CONTACT_SUPPRESSION_KIND: Final[str] = "contact_suppression"
"""The job kind that applies a suppression's transitional consequences (R21.C4 through C7).

Declared here rather than in ``revora.jobs.scheduler``, for the same layering reason
``CASE_REVIEW_KIND`` is declared in ``revora.cases.review``: this package enqueues it and sits
below ``revora.jobs``, so it cannot import the constant from there. The worker imports it from
here.

Not a periodic sweep. It is an event-driven follow-on like the six decision-pipeline kinds — one
job per suppression, enqueued by the transaction that wrote the suppression, so there is no
window in which a suppression exists and its consequences are neither applied nor queued."""


# ---------------------------------------------------------------------------
# Which Delay_Reasons are hard stops
# ---------------------------------------------------------------------------


HARD_STOP_FOR: Final[Mapping[DelayReason, HardStopReason | None]] = MappingProxyType(
    {
        DelayReason.DISPUTES_THE_CHARGE: HardStopReason.DISPUTES_THE_CHARGE,
        DelayReason.NO_LONGER_WANTS_THE_ORDER: HardStopReason.NO_LONGER_WANTS_THE_ORDER,
        # The other four are payment problems: the customer means to pay and is telling us
        # why they have not. They refine the next decision through
        # ``signals.DELAY_REASON_CAUSE`` and they suppress nothing.
        DelayReason.SALARY_OR_CASHFLOW_TIMING: None,
        DelayReason.BANK_OR_CARD_PROBLEM: None,
        DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW: None,
        DelayReason.OTHER: None,
    }
)
"""R21.C1's subset, as a total table over :class:`DelayReason`.

Total rather than a two-member membership test, and the check below is what keeps it total. A
seventh ``DelayReason`` member would otherwise fall through :func:`hard_stop_for` as "not a
hard stop", which is the wrong default in the one direction that matters: a reason nobody
classified would keep the chasing running. Failing at import instead makes the omission
impossible to ship.

Declared here rather than derived from :class:`HardStopReason`'s members. The enumeration
already takes its *values* from :class:`DelayReason` so the two cannot drift apart, and a
derivation would answer "which reasons are hard stops" by reflection — which reads as
incidental, where a table reads as a decision somebody made."""

_unclassified_delay_reasons = sorted(
    reason.value for reason in DelayReason if reason not in HARD_STOP_FOR
)
if _unclassified_delay_reasons:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "HARD_STOP_FOR is not total over DelayReason; missing "
        f"{_unclassified_delay_reasons}"
    )


def hard_stop_for(reason: DelayReason | None) -> HardStopReason | None:
    """The Hard_Stop_Reason a stated reason is, or ``None`` when it is not one.

    A function rather than callers indexing :data:`HARD_STOP_FOR`, so ``None`` means exactly
    *this reason is not a hard stop* and never *somebody forgot a row* — the totality check
    above is what makes the direct lookup safe rather than optimistic.

    Accepts ``None`` because two of the three submission shapes carry no Delay_Reason at all,
    and making every caller guard first would put the same ``if`` in three places.
    """
    if reason is None:
        return None
    return HARD_STOP_FOR[reason]


# ---------------------------------------------------------------------------
# The scope key
# ---------------------------------------------------------------------------


SCOPE_SEPARATOR: Final[str] = "\x1f"
"""ASCII unit separator, between the two halves of the hashed preimage.

``customer_key`` is a fixed-width 64-character hex digest today, so a concatenation of the
two halves is already unambiguous and the separator changes nothing. It is here for the day
that stops being true: if ``CustomerKeyHasher`` ever emits a variable-length key, an
unseparated concatenation makes two different scopes collide onto one hash, and a collision
in *this* table means one customer's dispute suppressing another customer's order. A byte
that cannot occur in either half is a cheaper guarantee than a promise to remember."""


class SuppressionScopeFacts(Protocol):
    """The ``recovery_case`` columns a Suppression_Scope is derived from.

    A Protocol rather than the ORM class, so the derivation is testable from a plain object
    and so this module states the complete list of columns the scope depends on. Three, and
    the reason each is here is worth keeping written down: ``customer_key`` makes the scope
    about a person, ``provider_order_id`` makes it about one order where the Payment_Event
    named one, and ``id`` is the fallback that keeps the scope defined when it did not.
    """

    id: uuid.UUID
    customer_key: str
    provider_order_id: str | None


def suppression_scope_key(
    *, customer_key: str, order_id: str | None, case_id: uuid.UUID
) -> str:
    """The Suppression_Scope key: ``sha256(customer_key ‖ order_id or case_id)``. Hex.

    ``order_id`` is used where the Payment_Event carried one and the case identifier
    otherwise, which is the glossary's definition and the **[ASSUMPTION]** the module
    docstring argues for. An empty or whitespace-only order id is treated as absent rather
    than hashed: ``provider_order_id`` is provider-supplied text, ``""`` is not an order, and
    hashing it would give every order-less case of one customer the same scope while
    *looking* like it had used an order.

    Deterministic and total. There is no failure mode — a case always has an id — which is
    what lets the policy hot path call this without a fallback branch for "no scope".
    """
    scope_id = case_id.hex if order_id is None or not order_id.strip() else order_id.strip()
    preimage = f"{customer_key}{SCOPE_SEPARATOR}{scope_id}"
    return sha256(preimage.encode("utf-8")).hexdigest()


def scope_key_for_case(case: SuppressionScopeFacts) -> str:
    """The scope key of one case, read off its own row.

    The single derivation every caller uses — the writer, the policy-input resolver and the
    dashboard read alike. A second derivation anywhere is a scope that a write and a read
    disagree about, and the direction that disagreement fails in is a suppression that was
    persisted and is never found.
    """
    return suppression_scope_key(
        customer_key=str(case.customer_key),
        order_id=case.provider_order_id,
        case_id=case.id,
    )


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuppressionOutcome:
    """What a hard stop wrote, for the caller's audit payload and the worker's payload.

    Returned rather than logged-and-discarded because the accepting request's audit record
    names it and the enqueued consequence needs the scope key. Frozen and slotted for the
    same reason every other record shape here is: the caller reads it and does not amend it.
    """

    scope_key: str
    hard_stop_reason: HardStopReason
    suppression_id: uuid.UUID | None
    """The new row's id, or ``None`` when this scope was already suppressed.

    ``None`` is not a failure. ``UNIQUE (merchant_id, scope_key)`` makes a second hard stop on
    one scope idempotent, and the correct response to it is to leave the first record — and
    the person named on any release of it — exactly as it stands."""

    tokens_revoked: int
    suppressed_at: datetime

    @property
    def created(self) -> bool:
        """Whether this call is the one that suppressed the scope."""
        return self.suppression_id is not None


# ---------------------------------------------------------------------------
# The write (R21.C1, R21.C10)
# ---------------------------------------------------------------------------


def record_hard_stop(
    session: Session,
    merchant_id: uuid.UUID,
    case: SuppressionScopeFacts,
    *,
    signal_id: uuid.UUID,
    hard_stop_reason: HardStopReason,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> SuppressionOutcome:
    """Suppress a scope, revoke the case's tokens, queue the rest. Caller's transaction.

    Shares the caller's transaction rather than opening its own, and that sharing *is*
    R21.C1: the ``contact_suppression`` row and the ``customer_signal`` row it names commit
    together or neither does. The ``customer_signal_id`` foreign key is ``NOT NULL``, so the
    caller has to have flushed the signal before calling — ``CustomerSignalRepository.insert``
    flushes for exactly this reason.

    Three writes, in this order and no other:

    1. **The suppression**, through ``INSERT ... ON CONFLICT DO NOTHING``. A second hard stop
       on one scope returns ``None`` and writes nothing, which is the requirement rather than
       a tolerated race: one scope holds one suppression, and a second row would be a second
       thing to release and a second thing to forget to release.
    2. **The revocation** (R21.C10), unconditionally — including when the insert conflicted.
       That looks redundant and is not: the conflicting row may be a suppression on a
       *different* case in the same scope, and this case's tokens have never been revoked. The
       revoke is an ``UPDATE`` over the live tokens of one case, so running it a second time
       touches nothing and costs one indexed statement.
    3. **The enqueued consequence** (R19.C9), deduped on ``contact_suppression:{case_id}``.
       Enqueued unconditionally too, and for a sharper version of the same reason: the
       consequences are per *case* while the suppression is per *scope*, so the case whose
       insert conflicted still has to be escalated and still has its scheduled actions to
       cancel. ``ON CONFLICT DO NOTHING`` on the pending-job index means a second submission
       while the first job is still pending adds no second job.

    Returns a :class:`SuppressionOutcome` rather than raising on the idempotent path. A
    repeated hard stop is a normal thing for a customer to do — they said it once and the page
    still let them say it again — and a caller that had to catch an exception to find out would
    be a caller that could forget to.

    **No consent row is written.** See the module docstring: R21.C9 keeps an objection to one
    debt distinguishable from a withdrawal of consent, and the mechanism is that this function
    has no consent repository to reach for.
    """
    instant = now() if moment is None else ensure_utc(moment)
    scope_key = scope_key_for_case(case)

    suppression_id = ContactSuppressionRepository(session).insert_if_absent(
        merchant_id,
        scope_key=scope_key,
        values={
            "origin_case_id": case.id,
            "customer_signal_id": signal_id,
            "hard_stop_reason": hard_stop_reason.value,
            "suppressed_at": instant,
            # released_at, released_by_user_id and release_config_version are left unset
            # together. ``release_names_a_user`` makes any other combination unstorable, so
            # a release is the only thing that may set them and it sets all three at once.
        },
    )

    tokens_revoked = revoke_tokens_for_case(
        session,
        merchant_id,
        case.id,
        reason=TokenRevocationReason.CONTACT_SUPPRESSED,
        moment=instant,
    )

    JobRepository(session).enqueue(
        merchant_id,
        kind=CONTACT_SUPPRESSION_KIND,
        payload={
            "case_id": str(case.id),
            "hard_stop_reason": hard_stop_reason.value,
            "scope_key": scope_key,
            "correlation_id": None if correlation_id is None else str(correlation_id),
        },
        run_after=instant,
        dedupe_key=f"{CONTACT_SUPPRESSION_KIND}:{case.id}",
        case_id=case.id,
        correlation_id=correlation_id,
    )

    _logger.info(
        "contact suppression recorded",
        case_id=str(case.id),
        hard_stop_reason=hard_stop_reason.value,
        created=suppression_id is not None,
        tokens_revoked=tokens_revoked,
    )
    return SuppressionOutcome(
        scope_key=scope_key,
        hard_stop_reason=hard_stop_reason,
        suppression_id=suppression_id,
        tokens_revoked=tokens_revoked,
        suppressed_at=instant,
    )


# ---------------------------------------------------------------------------
# The read policy check 5 consumes (R21.C3, R21.C8)
# ---------------------------------------------------------------------------


def suppression_in_force(
    session: Session, merchant_id: uuid.UUID, case: SuppressionScopeFacts
) -> bool:
    """Whether a live Contact_Suppression covers this case's scope.

    The policy hot path. Served by ``ix_contact_suppression_in_force``, partial over
    ``released_at IS NULL`` so a released suppression — which is history — cannot be read
    here at all rather than being filtered out by a predicate somebody could drop.

    **Keyed on the scope, not on the case**, which is the whole of R21.C8. A case created
    later for the same customer and the same order derives the same ``scope_key``, so it finds
    the suppression on its first decision cycle without anything having to notice that a new
    case appeared and go looking for old objections. The alternative — copying a flag onto
    each new case at creation time — is a copy that the one case created by a path nobody
    updated would not have.

    Returns a plain ``bool`` with no third state. Unlike the consent lookup, absence here is
    not ambiguous: the read either found a row in force or there is none, and there is no
    "suppression record exists but could not be read" to represent — a failed read raises and
    the transaction goes with it, which is the right answer for a control whose failure mode
    must not be "proceed".
    """
    return (
        ContactSuppressionRepository(session).in_force(merchant_id, scope_key_for_case(case))
        is not None
    )


# ---------------------------------------------------------------------------
# Release (R21.C2)
# ---------------------------------------------------------------------------


def release_suppression(
    session: Session,
    merchant_id: uuid.UUID,
    scope_key: str,
    *,
    released_by_user_id: uuid.UUID,
    release_config_version: str,
    moment: datetime | None = None,
) -> bool:
    """Lift a suppression, naming the person who lifted it. Returns whether it moved.

    R21.C2 has two halves and they are enforced in two different places, deliberately.

    **No expiry exists to build.** ``contact_suppression`` has no ``expires_at`` column, so
    "retained with no expiry instant" is not a rule this function honours — it is a shape the
    table has. There is nowhere to write a lapse date, no sweep that could read one, and this
    module holds no clock comparison against a stored deadline. A nullable column that nobody
    sets would have been the weaker version of the same claim: weaker because a later writer
    could set it, and because a reader could not tell "never expires" from "expiry not
    computed yet".

    **A release always names a person.** ``released_by_user_id`` and
    ``release_config_version`` are required keyword arguments and the repository sets all
    three release columns in one ``UPDATE``, but neither of those is the guarantee. The
    guarantee is ``CHECK ((released_at IS NULL) = (released_by_user_id IS NULL))`` from
    migration ``0008``: an anonymous release is *unstorable*, on the same terms
    ``model_promotion.approving_user_id`` is (R15.C6). So this signature is a convenience that
    surfaces the requirement at the call site, and the database is what makes it hold for a
    call site nobody has written yet.

    Only a suppression still in force is touched, so a repeated release returns ``False`` and
    does not overwrite who actually performed the first one or when. That matters more here
    than idempotency usually does: the released-by user is the accountable party for contact
    resuming, and a second call recording a second person would move that accountability to
    whoever happened to retry.
    """
    released = ContactSuppressionRepository(session).release(
        merchant_id,
        scope_key,
        moment=now() if moment is None else ensure_utc(moment),
        released_by_user_id=released_by_user_id,
        release_config_version=release_config_version,
    )
    if released:
        _logger.info(
            "contact suppression released",
            merchant_id=str(merchant_id),
            released_by_user_id=str(released_by_user_id),
            release_config_version=release_config_version,
        )
    return released

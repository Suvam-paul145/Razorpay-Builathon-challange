"""Promise_To_Pay — a promise changes *when* Revora acts and never *how long*.

A customer saying "I will pay on Friday" is the one submission on the public surface that Revora
can act on directly, and R23's whole shape is a restriction on how far that action may go. The
Recovery_Window is set at case creation and never extended (R2.C5); that immutability is what
makes R2.C12's termination bound provable, and a promise that could extend it would remove the
guarantee. So a promise moves the *instant* of the next contact and moves nothing else, and a
promised date beyond the window end is not a scheduling problem to be solved by stretching the
window — it is a case for a person.

**The clamp is the module, and getting it wrong is a 503 rather than a bad decision.**
``promise_to_pay`` carries ``CHECK (follow_up_at IS NULL OR follow_up_at <
window_end_at_snapshot)`` from migration ``0008`` — half of Property 42 as a database fact — so a
Follow_Up_Instant computed at or past the window end is a failed ``INSERT`` on an endpoint
reachable without a session, which answers 503 to a well-formed request. Two things keep that
unreachable, and neither is a check written here:

* ``PROMISE_WINDOW_SAFETY_MARGIN`` is refused at configuration load unless it is strictly
  positive (:func:`revora.platform.config._enforce_positive_margin`), so ``window_end_at -
  margin`` is strictly less than ``window_end_at`` for every value the process can hold; and
* the escalated path writes ``follow_up_at = None``, which the same ``CHECK`` admits
  unconditionally and which ``escalated_schedules_nothing`` *requires*.

So every path that writes a Follow_Up_Instant writes one strictly inside the window, and the
proof is a configuration invariant plus a null, not a comparison in :func:`plan_promise` that
somebody could delete.

**A date beyond the window escalates; it is not clamped to the window end.** R23.C5 and R23.C6
are one decision stated twice — a Promise_Date at or after ``window_end_at``, and a computed
Follow_Up_Instant earlier than the submission instant, both yield ``BEYOND_WINDOW_ESCALATED``
with no Follow_Up_Instant, nothing scheduled, and the case ``ESCALATED`` with
``PROMISE_BEYOND_RECOVERY_WINDOW``. The rejected alternative was clamping to ``window_end_at -
margin`` in both cases, which the ``CHECK`` would have accepted and which would have been worse
than a failed insert: a follow-up on the last hour of a window is a nudge nobody can act on, and
recording it would make the case *look* handled while nothing useful was scheduled. R23.C6 is the
sharper half — a window with less than ``PROMISE_WINDOW_SAFETY_MARGIN`` left admits no follow-up
inside it at all, so a promise arriving then has nowhere to put one however early its date.

**``window_end_at_snapshot`` is a snapshot on purpose, and it cannot drift.**
``recovery_case.window_end_at`` is immutable once the case opens (R2.C5) and no transition writes
it — ``apply_locked_transition`` writes ``state``, ``version``, ``terminal_reason``, the three
counters, ``last_outbound_at`` and ``next_review_at``, and that is the entire set of columns a
transition may move. So the column is not defence against a moving source; it exists so that a
reader of one promise row can check the clamp without joining, and so that
``follow_up_within_window`` is a constraint over one row rather than a trigger over two tables.

**The write joins the accepting request's transaction.** :func:`record_promise` is called from
:func:`revora.customer.signals.record_signal` between the ``customer_signal`` insert — whose id
it needs, because ``customer_signal_id`` is a ``NOT NULL`` foreign key — and the
``CUSTOMER_SIGNAL_RECORDED`` audit record, which stays last so its rollback still undoes
everything staged. That is the same slot :func:`revora.customer.suppression.record_hard_stop` and
:func:`revora.customer.arrangements.enqueue_arrangement_consequence` occupy, and for the same
reason: writing a row this package owns is not one of the five things R19.C9 forbids inside the
accepting request, and transitioning a case is.

So the escalation of R23.C5 is **enqueued**, never applied here.
:data:`PROMISE_ESCALATION_KIND` is declared in this module rather than in
``revora.jobs.scheduler``, on the same layering argument that put ``CONTACT_SUPPRESSION_KIND`` in
:mod:`revora.customer.suppression`, ``PARTIAL_ARRANGEMENT_KIND`` in
:mod:`revora.customer.arrangements` and ``CASE_REVIEW_KIND`` in :mod:`revora.cases.review`: the
enqueuer sits below ``revora.jobs`` and cannot import from it, and a constant each side spelled
for itself would give one dedupe key two spellings. ``revora.jobs.worker`` imports it from here,
which is the direction the layering allows. :data:`PROMISE_SWEEP_KIND` is declared here for the
same reason and is a *periodic* kind, so ``revora.jobs.scheduler`` appends it to
``PERIODIC_SWEEP_KINDS`` exactly as it does ``CASE_REVIEW_KIND``.

**How ``MAX_PROMISES_PER_CASE`` is reconciled with the ``UNIQUE`` backstop.** The configured bound
and ``uq_promise_to_pay_merchant_id_case_id`` are two statements of one number, and they are not
free to disagree: a configured value above 1 would be a bound the database refuses to honour, so
the second promise would be refused by a constraint violation — 503 — instead of by R23.C7's 409.
:func:`effective_promise_limit` therefore takes the **smaller** of the configured value and
:data:`~revora.persistence.models.customer.MAX_PROMISES_PER_CASE`, on exactly the terms
:func:`revora.customer.signals.effective_note_limit` takes the smaller of the configured note
length and its column's ``CHECK``. Lowering the row works immediately and is useful — a merchant
may set 0 and stop accepting promises without a migration. Raising it above 1 cannot work and is
silently floored rather than attempted, and raising it *for real* needs a migration that drops the
index and changes that constant. The application check runs first and the index is the backstop
behind it, which is what makes two concurrent submissions produce one 409 rather than one failed
transaction:
:meth:`~revora.persistence.repositories.customer.PromiseToPayRepository.insert_if_absent` goes
through ``ON CONFLICT DO NOTHING`` for precisely that.

**R23.C12 is applied by the sweep, not by a hook on every terminal transition.** Voiding a still-
``RECORDED`` promise when its case ends is a consequence of a transition, and there are a dozen
call sites that produce one. A hook on each would be a dozen places to remember, and the one
nobody updated would leave a promise ``RECORDED`` forever — inside the partial index the sweep
scans, so the defect would present as a follow-up that never fires rather than as a missing hook.
:func:`sweep_due_promises` is one call site and it is total over the table: every promise that can
still carry a Follow_Up_Instant is in ``ix_promise_to_pay_due_for_follow_up``, and
``follow_up_at < window_end_at_snapshot`` means a case cannot end without its promise's follow-up
instant having passed. So the sweep sees every voidable promise within one
``PROMISE_SWEEP_INTERVAL`` of the ending, which is what R23.C13 asks for.

**No money is read, computed or written anywhere in this module.** The escalation's audit record
names the unresolved amount, and it is read off the case row by the handler in
``revora.jobs.pipeline`` as an ``int`` of minor units. ``seconds_promise_to_payment`` is a signed
integer count of seconds and never a float duration: paying early is normal, so a negative
interval is a correct measurement and not an error to clamp away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final, Protocol

from revora.cases.bounds import (
    BOUND_DECISION_CYCLE_LIMIT,
    BOUND_WINDOW_ELAPSED,
    bound_reached,
)
from revora.cases.manager import apply_locked_transition
from revora.cases.review import enqueue_case_review
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    IntentState,
    PromiseStatus,
    ReviewTrigger,
    TerminalReason,
)
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models.customer import (
    MAX_PROMISES_PER_CASE as SCHEMA_MAX_PROMISES_PER_CASE,
)
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.customer import PromiseToPayRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import ensure_utc, now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.persistence.models import RecoveryCase
    from revora.platform.config import Configuration

__all__ = [
    "DEFAULT_PROMISE_SWEEP_LIMIT",
    "FOLLOW_UP_REVIEW_STATES",
    "PROMISE_ESCALATION_KIND",
    "PROMISE_SWEEP_KIND",
    "MissedDisposition",
    "PromiseOutcome",
    "PromisePlan",
    "PromiseWindowFacts",
    "SweepTally",
    "apply_missed_disposition",
    "effective_promise_limit",
    "follow_up_due_for_case",
    "follow_up_reached",
    "mark_follow_up_scheduled",
    "meets_min_lead_time",
    "plan_promise",
    "record_promise",
    "resolve_kept",
    "resolve_missed",
    "sweep_due_promises",
    "void_for_terminal_state",
]

_logger = get_logger(__name__)

_PROMISE_ACTOR: Final[str] = "promise_manager"
"""The actor on the one transition this module applies (R24.C14).

Its own name rather than ``execution_engine`` or ``outcome_monitor``, because the decision is
neither of theirs: the Outcome_Monitor's read established that the money is not there and this
module established that a follow-up had been confirmed, and it is the *conjunction* — R23.C11's
two halves — that disposes of the case. An audit reader asking who ended a case on a missed
promise is owed the component that applied the rule, not the one that happened to be running."""


PROMISE_ESCALATION_KIND: Final[str] = "promise_escalation"
"""The job kind that applies a beyond-window promise's terminal consequence (R23.C5, C6).

Not a periodic sweep, so it is absent from ``PERIODIC_SWEEP_KINDS`` and no clock enqueues it. One
job per escalated promise, enqueued by the transaction that wrote the ``promise_to_pay`` row,
deduped on the case — so there is no window in which an escalated promise exists and its
consequence is neither applied nor queued, and the ``UNIQUE (merchant_id, case_id)`` backstop
means there is never a second one to queue.

Declared here rather than in ``revora.jobs.scheduler`` for the layering reason the module
docstring gives: this package enqueues it and sits below ``revora.jobs``, so it cannot import the
constant from there. The worker imports it from here."""

PROMISE_SWEEP_KIND: Final[str] = "promise_sweep"
"""The periodic sweep over promises awaiting a follow-up (R23.C13). Every
``PROMISE_SWEEP_INTERVAL``.

Declared here and appended to ``PERIODIC_SWEEP_KINDS`` by ``revora.jobs.scheduler``, on exactly
the terms ``CASE_REVIEW_KIND`` is. Unlike that one it is enqueued from *only* one place — the
ticker — so the constant could have lived in the scheduler; it is here because the sweep's body
is here and a kind whose handler and whose name sit in different packages is a kind somebody
renames in one of them.

**It is the eighth periodic sweep, not the seventh.** The task text calls it the seventh, which
was true when it was written: ``PERIODIC_SWEEP_KINDS`` then held six. Migration ``0014``'s ticker
role added the three intervals that made all seven schedulable, so this one arrives as the eighth
member of a tuple whose seven predecessors all have a bound. ``PROMISE_SWEEP_INTERVAL`` is its
bound, and ``revora.jobs.ticker`` refuses a kind it cannot price rather than defaulting one — so
the count in a docstring is cosmetic and the bound is not."""

DEFAULT_PROMISE_SWEEP_LIMIT: Final[int] = 200
"""How many due promises one sweep pass claims.

Bounded rather than unbounded for the reason every list read in the persistence package is: a
pass that claimed every due row would hold ``FOR UPDATE SKIP LOCKED`` on all of them for the
duration of the pass, and a backlog would turn one slow pass into a lock held across a whole
merchant's promises. A pass that hits the limit leaves the rest for the next one, at most
``PROMISE_SWEEP_INTERVAL`` later — the same arrangement the review sweep uses, and the reason
neither needs a queue of its own."""

_ESCALATION_DEDUPE: Final[str] = PROMISE_ESCALATION_KIND
"""The dedupe-key prefix, named once so the enqueue and any future reader agree."""

_FOLLOW_UP_PENDING: Final[tuple[PromiseStatus, ...]] = (
    PromiseStatus.RECORDED,
    PromiseStatus.FOLLOW_UP_SCHEDULED,
)
"""The two statuses that can still carry a Follow_Up_Instant, and the two the partial index
``ix_promise_to_pay_due_for_follow_up`` is built over.

The same pair the repository's ``_FOLLOW_UP_PENDING`` and the model's
``FOLLOW_UP_PENDING_STATUS_SQL`` name, restated here as the typed enumeration members because
this module's transitions are *conditional on* the status being left — see
:meth:`~revora.persistence.repositories.customer.PromiseToPayRepository.resolve` — and a set of
raw strings would be a second spelling of a status in the one place spelling it wrongly means a
transition that silently matches no row."""


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def effective_promise_limit(config: Configuration) -> int:
    """How many promises one case may hold: the smaller of the configured bound and the schema's.

    Two numbers for one bound, and the ``min`` is why that is safe rather than sloppy. See the
    module docstring for the full argument; the short form is that ``UNIQUE (merchant_id,
    case_id)`` encodes 1, a configured value above 1 would be a bound the database refuses to
    honour, and attempting it would refuse the second promise with a constraint violation — a 503
    — instead of R23.C7's 409. Lowering the configured value takes effect on the next write and is
    a useful thing for a merchant to be able to do; raising it above the schema's 1 cannot take
    effect and is floored here rather than attempted.

    Never negative. A configured negative would mean "fewer than no promises", which is not a
    statement about anything, and flooring at zero makes it mean the nearest thing it could
    sensibly mean: accept none.
    """
    return max(0, min(int(config.MAX_PROMISES_PER_CASE), SCHEMA_MAX_PROMISES_PER_CASE))


def meets_min_lead_time(
    promise_date: datetime, *, instant: datetime, min_lead_time: timedelta
) -> bool:
    """Whether a Promise_Date is far enough ahead of its submission to be a promise (R23.C2).

    Strictly earlier than ``instant + min_lead_time`` is the rejection, so a date exactly at the
    boundary is accepted. Boundary-inclusive on purpose: the bound answers "how soon may a promise
    be for", and a date exactly one lead time away *is* one lead time away. The alternative reading
    would make the accepted set open at both ends and there would be no expressible date that sits
    at the bound.

    A pure predicate rather than a branch inside the writer, so the model tier can generate dates
    across the boundary without a session — and so the refusal and the audit record that names it
    are computed from one comparison rather than from two that could drift.
    """
    return ensure_utc(promise_date) >= ensure_utc(instant) + min_lead_time


# ---------------------------------------------------------------------------
# The clamp (R23.C3, C5, C6)
# ---------------------------------------------------------------------------


class PromiseWindowFacts(Protocol):
    """The ``recovery_case`` columns a promise's clamp depends on.

    A Protocol rather than the ORM class, so the clamp is testable from a plain object and so this
    module states the complete list of columns it reads. Two, and ``window_end_at`` is the only one
    the arithmetic touches — ``id`` is here because every write below names the case it belongs to.

    **``payment_amount`` is deliberately absent.** The escalation's audit record names the
    unresolved amount and reads it off the case row in ``revora.jobs.pipeline``; nothing in this
    module reads a money column, and stating that as an absent protocol member rather than as a
    comment means a future caller reaching for one gets a type error.
    """

    id: uuid.UUID
    window_end_at: datetime


@dataclass(frozen=True, slots=True)
class PromisePlan:
    """What one submitted Promise_Date resolves to, before anything is persisted.

    Computed by :func:`plan_promise` and consumed by :func:`record_promise`, which does nothing
    but write it down. The split exists so Properties 41 and 42 can be asserted over generated
    dates spanning the window boundary and the safety margin without a database — the clamp is
    arithmetic, and arithmetic that needs a session to test is arithmetic nobody tests at the
    boundary.

    Frozen and slotted like every other record shape in this package.
    """

    status: PromiseStatus
    """``RECORDED`` or ``BEYOND_WINDOW_ESCALATED``. Never any of the other four — those are
    resolutions of a persisted promise and not outcomes of a submission."""

    follow_up_at: datetime | None
    """The clamped Follow_Up_Instant, or ``None`` on the escalated path.

    When not ``None`` it is **strictly** earlier than :attr:`window_end_at`, which
    ``follow_up_within_window`` requires and which a strictly positive
    ``PROMISE_WINDOW_SAFETY_MARGIN`` guarantees rather than this class asserting it."""

    window_end_at: datetime
    """The window end the clamp was computed against, snapshotted onto the row (R23.C8)."""

    clamped: bool = False
    """Whether the window end, rather than the promise date, decided the Follow_Up_Instant.

    A recorded fact rather than a property that recomputes ``promise_date + offset``, because the
    audit record and the case-detail view both say *why* a follow-up is where it is, and a second
    derivation of the unclamped instant is a second place the offset is applied. ``False`` on the
    escalated path, because nothing was scheduled to clamp."""

    @property
    def escalates(self) -> bool:
        """Whether persisting this plan also escalates the case (R23.C5, C6)."""
        return self.status is PromiseStatus.BEYOND_WINDOW_ESCALATED


def plan_promise(
    *,
    promise_date: datetime,
    instant: datetime,
    window_end_at: datetime,
    follow_up_offset: timedelta,
    safety_margin: timedelta,
) -> PromisePlan:
    """Resolve a Promise_Date to a status and a Follow_Up_Instant. Pure (R23.C3, C5, C6).

    Three outcomes, in the order the requirements state them:

    1. **The Promise_Date is at or after the window end** (R23.C5) — ``BEYOND_WINDOW_ESCALATED``,
       no Follow_Up_Instant. Checked first and checked against the *date*, not against the
       computed instant, because it is a statement about what the customer said rather than about
       what could be scheduled: a person who names a date past the window has asked for more time
       than the case has, and the answer is a person, not a nudge before their date.
    2. **The clamped Follow_Up_Instant is earlier than the submission instant** (R23.C6) — also
       ``BEYOND_WINDOW_ESCALATED``. Reachable only through the clamped branch, because
       ``promise_date > instant`` is refused before this function is called and
       ``follow_up_offset`` is non-negative, so ``promise_date + offset`` cannot precede
       ``instant``. What it detects is therefore exactly one condition: **the window has less than
       ``safety_margin`` left**, so it admits no follow-up inside it however early the date.
    3. **Otherwise** — ``RECORDED``, with ``follow_up_at = min(promise_date + follow_up_offset,
       window_end_at - safety_margin)``.

    ``ensure_utc`` on all three instants before any comparison, so a naive datetime from a test or
    a differently-offset one from a request cannot make the ordering depend on which of two
    representations reached here first (R23.C1's conversion, applied where the comparison is).

    The window end is **never** moved, added to, or returned changed. R23.C4 is a property of this
    function having no expression that produces a window end: it reads one and passes it through.

    Args:
        promise_date: the submitted instant, already refused if at or before ``instant``.
        instant: the submission instant. The clamp's floor, per R23.C6.
        window_end_at: ``recovery_case.window_end_at``, immutable since case creation (R2.C5).
        follow_up_offset: ``PROMISE_FOLLOW_UP_OFFSET``. Non-negative; a negative value would
            schedule the follow-up *before* the promised date, which is the message R23 exists to
            prevent, and it is floored here rather than refused because refusing would put a
            second validator on a bound the catalogue already documents.
        safety_margin: ``PROMISE_WINDOW_SAFETY_MARGIN``. Strictly positive, which
            ``Configuration.from_values`` enforces — see the module docstring for why a
            non-positive value is unstorable rather than merely permissive.
    """
    promised = ensure_utc(promise_date)
    submitted = ensure_utc(instant)
    window_end = ensure_utc(window_end_at)
    offset = max(follow_up_offset, timedelta(0))

    unclamped = promised + offset
    if promised >= window_end:
        # R23.C5. The date itself is past the window; nothing is computed and nothing scheduled.
        return PromisePlan(
            status=PromiseStatus.BEYOND_WINDOW_ESCALATED,
            follow_up_at=None,
            window_end_at=window_end,
        )

    latest = window_end - safety_margin
    follow_up_at = min(unclamped, latest)
    if follow_up_at < submitted:
        # R23.C6. The window has less than safety_margin left, so there is no instant inside it
        # that a follow-up could occupy. Escalate rather than schedule one in the past.
        return PromisePlan(
            status=PromiseStatus.BEYOND_WINDOW_ESCALATED,
            follow_up_at=None,
            window_end_at=window_end,
        )

    return PromisePlan(
        status=PromiseStatus.RECORDED,
        follow_up_at=follow_up_at,
        window_end_at=window_end,
        clamped=follow_up_at < unclamped,
    )


# ---------------------------------------------------------------------------
# The write (R23.C1, C3, C5, C6, C7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromiseOutcome:
    """What one promise submission wrote, for the caller's audit payload and response.

    Returned rather than logged-and-discarded because the accepting request's audit records name
    every field of it: ``PROMISE_RECORDED`` carries the date, the follow-up instant and the window
    end so R23.C8's "a reader can check the clamp from the audit trail alone" holds, and
    ``PROMISE_ALREADY_RECORDED`` carries the refusal.

    ``promise_id is None`` means **exactly** ``MAX_PROMISES_PER_CASE`` was reached (R23.C7). It is
    not an error and it is not a failed write: the Customer_Signal that carried the submission was
    still persisted, which is the whole asymmetry of that clause.
    """

    promise_id: uuid.UUID | None
    plan: PromisePlan
    escalation_enqueued: bool
    """Whether this write queued the terminal consequence of R23.C5. ``True`` only on the
    escalated path, and a *queued* escalation rather than an applied one — the case is still at
    whatever state it was in when this returned (R19.C9), so a reader must not take ``True`` to
    mean the case is terminal."""

    @property
    def refused(self) -> bool:
        """Whether the promise was refused because the case already holds its limit (R23.C7)."""
        return self.promise_id is None


def record_promise(
    session: Session,
    merchant_id: uuid.UUID,
    case: PromiseWindowFacts,
    config: Configuration,
    *,
    signal_id: uuid.UUID,
    promise_date: datetime,
    received_representation: str,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> PromiseOutcome:
    """Persist one Promise_To_Pay and queue its consequence. Caller's transaction.

    Shares the caller's transaction rather than opening its own, and that sharing is what makes
    R19.C5 hold across the promise: the ``customer_signal`` row, the ``promise_to_pay`` row, the
    token counter increment, any queued escalation and — last — the audit records commit together
    or none of them does. The ``customer_signal_id`` foreign key is ``NOT NULL``, so the caller has
    to have flushed the signal before calling; ``CustomerSignalRepository.insert`` flushes for
    exactly that reason.

    Three steps, and the order is the only one that works:

    1. **Refuse if the case already holds :func:`effective_promise_limit` promises** (R23.C7). A
       count rather than a "is there one" read, because the bound is configurable and a count is
       what a configurable bound is compared against. At the schema's 1 the two are the same
       question; at a configured 0 they are not, and a merchant who set 0 must have the first
       promise refused rather than the second.
    2. **Insert, through ``ON CONFLICT DO NOTHING``.** A ``None`` return is the ``UNIQUE
       (merchant_id, case_id)`` backstop catching what step 1 could not: two submissions racing on
       two different tokens for one case. It produces the same :attr:`PromiseOutcome.refused` and
       therefore the same 409, because from the customer's side the two are one answer.
    3. **Enqueue the escalation** on the ``BEYOND_WINDOW_ESCALATED`` path (R23.C5, C6), deduped on
       the case. A queue write and nothing else: no transition, no cancellation, no policy
       evaluation and no provider call, which are the five things R19.C9 forbids here. The
       transition to ``ESCALATED`` with ``PROMISE_BEYOND_RECOVERY_WINDOW`` is
       ``revora.jobs.pipeline.handle_promise_escalation``'s, because ``apply_transition`` stays the
       only writer of ``recovery_case.state``.

    **The window end is untouched, by construction** (R23.C4). ``window_end_at`` is read off the
    case row, passed to :func:`plan_promise`, and written to ``window_end_at_snapshot``. There is
    no assignment to ``recovery_case.window_end_at`` in this function or anything it calls, and
    ``apply_locked_transition`` — the only writer of case state — has no expression that produces
    one either. So R23.C4 is a property of the set of columns a transition may move, not a
    discipline kept here.

    **No audit record is written here.** Both of the ones this path produces —
    ``PROMISE_RECORDED`` and ``PROMISE_ALREADY_RECORDED`` — are written by
    :func:`revora.customer.signals.record_signal` from the returned outcome, so that the audit
    write stays last in the transaction and its rollback still undoes everything staged. Writing
    one here would put a record before the token increment's own record and make "all or none" a
    claim about ordering rather than a fact about the last statement.

    Args:
        received_representation: the submitted string exactly as it arrived (R23.C1), retained
            beside the UTC instant on the same terms R16.C13 applies to a Payment_Event timestamp.
            A timezone read wrongly is only diagnosable if what arrived is still there.
    """
    instant = now() if moment is None else ensure_utc(moment)
    promises = PromiseToPayRepository(session)

    plan = plan_promise(
        promise_date=promise_date,
        instant=instant,
        window_end_at=case.window_end_at,
        follow_up_offset=config.PROMISE_FOLLOW_UP_OFFSET,
        safety_margin=config.PROMISE_WINDOW_SAFETY_MARGIN,
    )

    limit = effective_promise_limit(config)
    # 0 or 1, and it can be no more than that: ``effective_promise_limit`` is capped at the
    # schema's own bound and ``UNIQUE (merchant_id, case_id)`` makes a second row unstorable, so
    # ``for_case`` answers the count exactly. A ``SELECT count(*)`` would read the same index for
    # the same answer and would suggest the bound could exceed one, which it cannot until a
    # migration drops that index.
    held = 0 if promises.for_case(merchant_id, case.id) is None else 1
    if held >= limit:
        _logger.info(
            "promise refused: the case already holds its limit",
            case_id=str(case.id),
            held=held,
            limit=limit,
        )
        return PromiseOutcome(promise_id=None, plan=plan, escalation_enqueued=False)

    promise_id = promises.insert_if_absent(
        merchant_id,
        case_id=case.id,
        values={
            "customer_signal_id": signal_id,
            "promise_date": ensure_utc(promise_date),
            "received_representation": received_representation,
            "status": plan.status.value,
            "follow_up_at": plan.follow_up_at,
            "window_end_at_snapshot": plan.window_end_at,
            "recorded_at": instant,
            # kept_at, missed_at, seconds_promise_to_payment and voided_by_terminal_state are
            # left unset together. ``kept_at_iff_kept`` makes a KEPT status without an instant
            # unstorable, so a resolution is the only thing that may set any of them.
        },
    )
    if promise_id is None:
        # The backstop, reached only by two concurrent submissions. Same answer as step 1.
        _logger.info(
            "promise refused by the uniqueness backstop", case_id=str(case.id)
        )
        return PromiseOutcome(promise_id=None, plan=plan, escalation_enqueued=False)

    escalation_enqueued = False
    if plan.escalates:
        JobRepository(session).enqueue(
            merchant_id,
            kind=PROMISE_ESCALATION_KIND,
            payload={
                "case_id": str(case.id),
                # The promise id and the signal id both travel, on the same terms the signal id
                # travels on an arrangement job: the row that caused the consequence is named by
                # the job that applies it, so an audit reader can join the two without inferring
                # which of a case's signals was the trigger. The promise *date* is read back
                # rather than carried, because a retried job must produce the same record as the
                # first attempt and a payload copy of a column can disagree with it.
                "promise_id": str(promise_id),
                "signal_id": str(signal_id),
                "correlation_id": None if correlation_id is None else str(correlation_id),
            },
            run_after=instant,
            dedupe_key=f"{_ESCALATION_DEDUPE}:{case.id}",
            case_id=case.id,
            correlation_id=correlation_id,
        )
        escalation_enqueued = True

    _logger.info(
        "promise recorded",
        case_id=str(case.id),
        promise_id=str(promise_id),
        status=plan.status.value,
        clamped=plan.clamped,
        escalation_enqueued=escalation_enqueued,
    )
    return PromiseOutcome(
        promise_id=promise_id, plan=plan, escalation_enqueued=escalation_enqueued
    )


# ---------------------------------------------------------------------------
# Resolutions driven by authoritative reads (R23.C10, C11, C12)
# ---------------------------------------------------------------------------


def resolve_kept(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    paid_at: datetime,
) -> int | None:
    """Mark a case's promise ``KEPT``, returning the signed interval in seconds (R23.C10).

    Returns ``None`` when there is no promise to keep, or when one exists and is no longer in a
    status that can be kept — a promise already ``KEPT`` by an earlier read, or ``VOIDED``, or
    escalated past the window. Neither is an error: the conditional ``UPDATE`` in
    :meth:`~revora.persistence.repositories.customer.PromiseToPayRepository.resolve` is what makes
    a second authoritative read of the same capture idempotent rather than a second measurement.

    **The interval is signed and it is a count of seconds, never a duration in floats.** R23.C10
    asks for "the interval in seconds between the Promise_Date and the provider-reported payment
    timestamp", and a customer who pays early produces a negative one — which is the normal case
    for a promise kept well, not an error to clamp to zero. ``seconds_promise_to_payment`` is a
    signed ``BIGINT`` for that reason, and ``int(...total_seconds())`` truncates toward zero
    rather than rounding, so the stored value is always the whole seconds that elapsed and never a
    second that did not.

    ``paid_at`` is the **provider-reported** payment timestamp, passed in by the Outcome_Monitor
    from the authoritative read rather than read here. R23.C10 is explicit that the read is the
    authority, and a clock consulted in this function would make the measurement depend on when
    the resolution ran instead of on when the money moved.

    Accepted from ``BEYOND_WINDOW_ESCALATED`` as well as from the two pending statuses, and that
    is deliberate: a customer whose date was past the window may still pay, and recording that
    they kept a promise Revora could not schedule around is the honest reading of what happened.
    The case's own escalation stands — this writes no case column.
    """
    promise = PromiseToPayRepository(session).for_case(merchant_id, case_id)
    if promise is None:
        return None

    seconds = int((ensure_utc(paid_at) - ensure_utc(promise.promise_date)).total_seconds())
    moved = PromiseToPayRepository(session).resolve(
        merchant_id,
        promise.id,
        expected_statuses=(*_FOLLOW_UP_PENDING, PromiseStatus.BEYOND_WINDOW_ESCALATED),
        values={
            "status": PromiseStatus.KEPT.value,
            # ``kept_at_iff_kept`` requires the pair, so both move in one statement or neither
            # does. The instant is the provider's, not this transaction's.
            "kept_at": ensure_utc(paid_at),
            "seconds_promise_to_payment": seconds,
        },
    )
    if not moved:
        return None
    _logger.info(
        "promise kept",
        case_id=str(case_id),
        promise_id=str(promise.id),
        seconds_promise_to_payment=seconds,
    )
    return seconds


def resolve_missed(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    moment: datetime | None = None,
) -> bool:
    """Mark a case's promise ``MISSED`` where a follow-up was confirmed and the money did not
    arrive (R23.C11). Returns whether it moved.

    **Both halves of the condition are required and the confirmed follow-up is the harder one.**
    R23.C11 is not "the promised date passed and nothing was paid" — that would mark a promise
    missed while the follow-up Revora owed the customer was still queued, so the record would
    blame the customer for a message Revora had not sent. The clause asks for a
    ``PROMISE_TO_PAY_FOLLOW_UP`` execution-intent record that reached ``CONFIRMED`` **and** a
    subsequent authoritative read reporting a state other than paid or captured. The read is the
    caller's — the Outcome_Monitor calls this from its non-captured path — and the confirmed
    intent is checked here.

    Checked against ``CONFIRMED`` alone, not against ``ATTEMPTED`` or ``UNCERTAIN``. An
    ``UNCERTAIN`` resend is *permanently* unresolvable by provider read (there is no endpoint that
    reports whether a notification was sent), so treating it as a delivered follow-up would make
    "the customer was reminded and did not pay" a claim nobody could check — and it would make the
    promise-kept rate a function of the provider's silence.

    ``missed_at`` records the instant this resolution ran, which is the instant the read that
    established the miss was assessed. It is not the promised date: R23.C11 asks for "the
    missed-promise instant", and a promise is not missed at the moment it was due — it is missed at
    the moment a read established that the follow-up had gone out and the money had not arrived.
    """
    instant = now() if moment is None else ensure_utc(moment)
    promises = PromiseToPayRepository(session)
    promise = promises.for_case(merchant_id, case_id)
    if promise is None:
        return False
    if not _follow_up_confirmed(session, merchant_id, case_id):
        return False

    moved = promises.resolve(
        merchant_id,
        promise.id,
        expected_statuses=_FOLLOW_UP_PENDING,
        values={"status": PromiseStatus.MISSED.value, "missed_at": instant},
    )
    if moved:
        _logger.info("promise missed", case_id=str(case_id), promise_id=str(promise.id))
    return moved


@unique
class MissedDisposition(StrEnum):
    """What R24.C14 did with a case whose promise was just established as missed.

    A named outcome rather than a boolean, because the three answers are what an operator asks
    about: was the case given another cycle, was it ended, or was it left for the sweeper. A
    caller holding only "it moved" would have to re-read the case to find out which.
    """

    RETURNED_TO_DECISION = "RETURNED_TO_DECISION"
    """Every bound still permits a further action, so the case re-enters ``DECISION_PENDING``
    and the next cycle weighs everything again — including a second follow-up, which policy will
    refuse or permit on the cooldown and the message bound rather than on the promise."""

    STOPPED_AT_BOUND = "STOPPED_AT_BOUND"
    """A counter bound is spent, so there is nothing left to decide and the case is terminal."""

    HELD_FOR_WINDOW_SWEEP = "HELD_FOR_WINDOW_SWEEP"
    """The recovery window has closed. Left where it is for the lifecycle sweeper, which owns
    window expiry — terminating it here would put a second writer on that one rule."""

    TRANSITION_REFUSED = "TRANSITION_REFUSED"
    """The case could not legally make the move: a version conflict, or a state that advanced
    underneath. The promise's ``MISSED`` status stands; the case is somebody else's now."""


def apply_missed_disposition(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> MissedDisposition:
    """Dispose of a case whose promise has just been marked ``MISSED`` (R24.C14). Caller's
    transaction, on a case row the caller holds ``FOR UPDATE``.

    R24.C14 asks for the remaining Recovery_Window, ``MAX_RECOVERY_ATTEMPTS``,
    ``MAX_CUSTOMER_MESSAGES``, ``COOLDOWN_INTERVAL`` and the decision-cycle bound to be applied
    **unchanged**, and "unchanged" is the operative word: this function applies none of them
    itself. It asks :func:`revora.cases.bounds.bound_reached` whether another cycle could lead
    anywhere and then makes one legal transition. The cooldown is conspicuously absent from that
    list of checks and its absence is correct — ``COOLDOWN_ACTIVE`` is policy check 8, evaluated
    against ``last_outbound_at`` at the moment of the *decision*, so re-entering
    ``DECISION_PENDING`` is precisely how it gets applied. A cooldown comparison here would be a
    second place the interval is enforced and this is the copy that would drift, because a missed
    promise is resolved by whichever authoritative read happens to run.

    **A missed promise is not an action and this is not a retry.** Re-entry costs a decision cycle
    and the counter bounds it; nothing outbound moves; all twelve checks run again before anything
    reaches a customer. What this decides is only that the question is worth asking again.

    The same three-way shape :func:`revora.execution.resend.settle_resend_result` uses for a
    definitively refused resend, and deliberately the same: "the follow-up did not land" and "the
    follow-up landed and the money did not arrive" are different observations with the same
    consequence for the case, and two different dispositions for one consequence would be two
    behaviours to explain.

    Args:
        case: the locked ``recovery_case`` row itself, not :class:`PromiseWindowFacts`. Every
            other function here takes the narrow protocol because it reads two columns; this one
            hands the row to ``bound_reached`` and to ``apply_locked_transition``, which between
            them read the three counters, the window end and the version and *write* the state.
            Widening the protocol to cover that would have described the whole row under a name
            that promises it does not, so the real type is named instead.
    """
    instant = now() if moment is None else ensure_utc(moment)
    bound = bound_reached(case, config, moment=instant)

    if bound == BOUND_WINDOW_ELAPSED:
        return MissedDisposition.HELD_FOR_WINDOW_SWEEP

    target = CaseState.DECISION_PENDING if bound is None else CaseState.STOPPED
    terminal_reason = None
    if bound is not None:
        # MAX_MESSAGES_REACHED terminates under MAX_ATTEMPTS_REACHED, exactly as the resend
        # settlement does: TerminalReason is persisted behind a CHECK generated from the enum, so
        # a member of its own would be a migration bought to make a rare terminal record read
        # slightly better. The transition's reason string carries which bound it actually was.
        terminal_reason = (
            TerminalReason.DECISION_CYCLE_LIMIT_REACHED
            if bound == BOUND_DECISION_CYCLE_LIMIT
            else TerminalReason.MAX_ATTEMPTS_REACHED
        )

    reason = (
        "promise missed; re-deciding"
        if bound is None
        else f"promise missed; {bound}"
    )
    _result, rejection = apply_locked_transition(
        session,
        merchant_id,
        case,
        expected_version=int(case.version),
        target_state=target,
        reason=reason,
        actor=_PROMISE_ACTOR,
        action=CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
        terminal_reason=terminal_reason,
        correlation_id=correlation_id,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )
    if rejection is not None:
        _logger.warning(
            "missed promise could not move the case",
            case_id=str(case.id),
            target_state=target.value,
            outcome=rejection.outcome.value,
        )
        return MissedDisposition.TRANSITION_REFUSED

    _logger.info(
        "missed promise disposed",
        case_id=str(case.id),
        target_state=target.value,
        bound=bound or "none",
    )
    return (
        MissedDisposition.RETURNED_TO_DECISION
        if bound is None
        else MissedDisposition.STOPPED_AT_BOUND
    )


def mark_follow_up_scheduled(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> bool:
    """Move a case's promise ``RECORDED`` → ``FOLLOW_UP_SCHEDULED`` (R24.C12). Returns whether it
    moved.

    Called by the execution engine when a ``PROMISE_TO_PAY_FOLLOW_UP`` intent reaches
    ``CONFIRMED``, in the transaction that records that confirmation. The sweep usually got there
    first — :func:`sweep_due_promises` is what normally makes the follow-up selectable in the
    first place — so the ordinary case is that this moves nothing, and the conditional ``UPDATE``
    is what makes that a no-op rather than a second write of a status the row already holds.

    **It still has to exist**, because the sweep is not the only route to a decision cycle. A case
    resting at ``POLICY_CHECK`` past its Follow_Up_Instant can be reviewed by the review sweep, by
    a customer signal, or by a webhook, and any of those can produce a cycle in which
    ``follow_up_reached`` answers true off a ``RECORDED`` promise whose row the promise sweep has
    not yet claimed. R24.C12 asks for the status to be ``FOLLOW_UP_SCHEDULED`` once the follow-up
    is confirmed, and "the sweep will get to it" is not that.

    Only ``RECORDED`` moves. A promise already ``FOLLOW_UP_SCHEDULED`` is where the requirement
    wants it; a ``KEPT``, ``MISSED``, ``VOIDED`` or escalated one is history, and rewinding one of
    those to a pending status would put a resolved promise back inside the sweep's scanned set.
    """
    promises = PromiseToPayRepository(session)
    promise = promises.for_case(merchant_id, case_id)
    if promise is None:
        # Reachable only if the promise was resolved between the candidate set that made the
        # follow-up selectable and this confirmation — a customer who paid mid-flight, say. The
        # message went out either way and the counters moved; there is simply no pending promise
        # left to describe, which is a fact about the row rather than an error in this path.
        return False
    moved = promises.resolve(
        merchant_id,
        promise.id,
        expected_statuses=(PromiseStatus.RECORDED,),
        values={"status": PromiseStatus.FOLLOW_UP_SCHEDULED.value},
    )
    if moved:
        _logger.info(
            "promise follow-up scheduled by a confirmed execution",
            case_id=str(case_id),
            promise_id=str(promise.id),
        )
    return moved


def void_for_terminal_state(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    terminal_state: CaseState,
) -> bool:
    """Void a case's pending promise, naming the Terminal_State that voided it (R23.C12).
    Returns whether it moved.

    ``RECOVERED`` is refused rather than ignored, and the refusal is the interesting half. R23.C12
    says *a Terminal_State other than RECOVERED*, because a recovered case's promise was **kept**
    — the status it deserves comes from :func:`resolve_kept` with a measured interval, and voiding
    it would replace a measurement with an absence. Raising here rather than returning ``False``
    makes the mistake a test failure instead of a promise that quietly lost its interval.

    Only a promise still in one of the two pending statuses moves. A ``KEPT``, ``MISSED`` or
    already-``VOIDED`` promise is history, and ``BEYOND_WINDOW_ESCALATED`` is terminal for the
    promise itself: the case ending is *why* it is escalated, so voiding it would erase the reason
    the case ended in the row that recorded it.

    Raises:
        ValueError: if ``terminal_state`` is not terminal, or is ``RECOVERED``. Both are caller
            bugs and both are silent if tolerated — the first would void a promise on a live case,
            and the second would discard a kept promise's interval.
    """
    if terminal_state not in TERMINAL_STATES:
        raise ValueError(f"{terminal_state.value} is not a terminal state")
    if terminal_state is CaseState.RECOVERED:
        raise ValueError(
            "a RECOVERED case's promise is KEPT, not VOIDED (R23.C12); use resolve_kept"
        )

    promises = PromiseToPayRepository(session)
    promise = promises.for_case(merchant_id, case_id)
    if promise is None:
        return False
    moved = promises.resolve(
        merchant_id,
        promise.id,
        expected_statuses=_FOLLOW_UP_PENDING,
        values={
            "status": PromiseStatus.VOIDED.value,
            "voided_by_terminal_state": terminal_state.value,
        },
    )
    if moved:
        _logger.info(
            "promise voided by a terminal state",
            case_id=str(case_id),
            promise_id=str(promise.id),
            terminal_state=terminal_state.value,
        )
    return moved


def _follow_up_confirmed(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> bool:
    """Whether a ``PROMISE_TO_PAY_FOLLOW_UP`` intent for this case reached ``CONFIRMED``.

    Read off ``execution_intent`` rather than inferred from the promise's own status, because
    ``FOLLOW_UP_SCHEDULED`` means the sweep made the follow-up *eligible for selection* and says
    nothing about whether the optimizer selected it, whether policy approved it, or whether the
    provider accepted it. A promise can sit at ``FOLLOW_UP_SCHEDULED`` for its whole remaining
    window because the follow-up never cleared ``MIN_NET_VALUE_THRESHOLD``, and marking that
    promise ``MISSED`` would record a customer's failure where the truth is that Revora chose not
    to spend the message.
    """
    return any(
        CandidateAction(str(intent.action)) is CandidateAction.PROMISE_TO_PAY_FOLLOW_UP
        and IntentState(str(intent.state)) is IntentState.CONFIRMED
        for intent in ExecutionIntentRepository(session).list_for_case(merchant_id, case_id)
    )


# ---------------------------------------------------------------------------
# The sweep (R23.C9, C13)
# ---------------------------------------------------------------------------


def follow_up_reached(
    promise_status: PromiseStatus, follow_up_at: datetime | None, *, instant: datetime
) -> bool:
    """Whether a promise's Follow_Up_Instant has been reached (R23.C9's condition, inverted).

    R23.C9 excludes ``PROMISE_TO_PAY_FOLLOW_UP`` from selection with ``PROMISE_DATE_NOT_REACHED``
    *while* the promise is ``RECORDED`` and the current instant is earlier than the Follow_Up_
    Instant. This is that condition as a predicate the Value_Optimizer's candidate builder reads,
    so the exclusion and the sweep's own "is this due" question are one comparison rather than two
    that could disagree by a second.

    ``FOLLOW_UP_SCHEDULED`` returns ``True`` unconditionally: the sweep only moves a promise into
    that status once the instant has passed, so the status *is* the answer and re-deriving it from
    a timestamp would let a clock skew un-schedule a follow-up the sweep already scheduled.

    A promise with no Follow_Up_Instant — ``BEYOND_WINDOW_ESCALATED`` — returns ``False``. It has
    nothing to reach, and R24.C2 excludes it from the candidate set on a different ground
    (``NO_PROMISE_RECORDED`` covers no *pending* promise), so answering ``True`` here would put a
    follow-up on a case whose promise was the reason it escalated.

    **The consumer arrived with R24.** It is
    :func:`revora.estimation.candidates.promise_follow_up_exclusion`, which asks this question
    third — after the resend capability and after "is there a pending promise at all" — and
    records ``PROMISE_DATE_NOT_REACHED`` where the answer is false. Until R24 the reason was
    unreachable, because ``PROMISE_TO_PAY_FOLLOW_UP`` sat in ``UNAVAILABLE_IN_MVP`` and was
    excluded with ``PROVIDER_CAPABILITY_UNVERIFIED`` before any promise-specific ground was
    consulted.

    It stays declared here rather than there because the promise's statuses and its
    Follow_Up_Instant are this module's, and a predicate over them written one layer up would be a
    second reading of a status this module transitions — which is exactly the drift the
    ``FOLLOW_UP_SCHEDULED`` shortcut above exists to prevent.
    """
    if promise_status is PromiseStatus.FOLLOW_UP_SCHEDULED:
        return True
    if promise_status is not PromiseStatus.RECORDED or follow_up_at is None:
        return False
    return ensure_utc(instant) >= ensure_utc(follow_up_at)


def follow_up_due_for_case(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID, *, instant: datetime
) -> bool:
    """Whether this case holds a pending promise whose Follow_Up_Instant has been reached.

    :func:`follow_up_reached` asked against the row rather than against two values a caller
    supplied, which is what the review handler needs: it holds a case id and has to decide whether
    a re-entry from ``WAITING_FOR_OUTCOME`` is R24.C13's decision cycle or a review job that went
    stale while the case advanced.

    Declared here, beside the predicate and the statuses it reads, so the handler does not read
    ``promise_to_pay`` for itself. A second reader of that row one layer up would be a second
    place the "is this due" comparison is written, which is the drift
    :func:`follow_up_reached` exists to prevent.

    ``False`` where no promise exists, which is the safe direction: the only consequence is that
    the case stays where it is and the sweep does not find it again, because a case with no
    promise is not in the sweep's scanned set to begin with.
    """
    promise = PromiseToPayRepository(session).for_case(merchant_id, case_id)
    if promise is None:
        return False
    return follow_up_reached(
        PromiseStatus(str(promise.status)), promise.follow_up_at, instant=instant
    )


FOLLOW_UP_REVIEW_STATES: Final[frozenset[CaseState]] = frozenset(
    {CaseState.POLICY_CHECK, CaseState.WAITING_FOR_OUTCOME}
)
"""The two states a due follow-up may start a decision cycle from (R24.C13).

**``WAITING_FOR_OUTCOME`` is the one that makes the requirement reachable, and its absence was
the whole defect.** R24.C13 asks for a decision cycle within ``PROMISE_SWEEP_INTERVAL`` of the
Follow_Up_Instant, for the Recovery_Case the promise belongs to. Every case that *can* hold a
promise is standing in ``WAITING_FOR_OUTCOME``: a Customer_Access_Token is minted inside the
transition into ``EXECUTING``, so a customer can only submit a promise about a case Revora has
already acted on, and such a case has left ``POLICY_CHECK`` behind. Enqueuing only from
``POLICY_CHECK`` therefore enqueued a cycle for exactly the cases that cannot have a promise, and
none for the cases that do.

The consequence ran further than one unspent action. ``PROMISE_TO_PAY_FOLLOW_UP`` was never
considered, so no follow-up intent reached ``CONFIRMED``, so :func:`resolve_missed` — which
refuses to blame a customer for a message Revora may not have sent — could never move a promise to
``MISSED``, so :func:`apply_missed_disposition` and the ``WAITING_FOR_OUTCOME ->
DECISION_PENDING`` re-entry edge it applies had no reachable caller. One missing enqueue made four
things unreachable.

``POLICY_CHECK`` stays, and not only for symmetry: a promise can be recorded on a case that
subsequently chose restraint, and that case rests at ``POLICY_CHECK`` with a ``next_review_at``.

The two states take **different edges into ``DECISION_PENDING``** — ``REVIEW`` from
``POLICY_CHECK``, ``REENTRY`` from ``WAITING_FOR_OUTCOME`` — and both carry
``decision_cycle_delta = 1``, which is what makes R24.C13's "SHALL apply the decision-cycle
counter bound of R2.C14" hold for either without a second bound being written anywhere. No new
edge was added for this; both already existed in :mod:`revora.domain.transitions`."""


@dataclass(frozen=True, slots=True)
class SweepTally:
    """What one pass of :func:`sweep_due_promises` did. Four disjoint counts and the total seen.

    Counted rather than logged only, because "the promise sweep is running" and "the promise sweep
    is doing anything" are different claims and an operator needs the second. The four are
    disjoint by construction: each claimed promise takes exactly one of the four branches or none,
    and ``considered`` is the number claimed, so ``considered - (scheduled + voided + skipped)``
    is the number that fell through — which should be zero and is worth being able to compute.
    """

    considered: int = 0
    scheduled: int = 0
    """Promises moved ``RECORDED`` → ``FOLLOW_UP_SCHEDULED`` because their instant was reached."""

    voided: int = 0
    """Promises voided because their case had ended other than ``RECOVERED`` (R23.C12)."""

    skipped: int = 0
    """Promises left alone: window elapsed with the case still live, or the case ``RECOVERED``
    with its promise awaiting the authoritative read's own resolution."""

    reviews_enqueued: int = 0
    """Decision cycles queued so a scheduled follow-up can actually be considered."""


def sweep_due_promises(
    merchant_id: uuid.UUID,
    *,
    limit: int = DEFAULT_PROMISE_SWEEP_LIMIT,
    factory: sessionmaker[Session] | None = None,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> SweepTally:
    """Evaluate every promise whose Follow_Up_Instant has been reached (R23.C13).

    **One transaction per promise, and the claim and the decision are in the same one.**
    :meth:`~revora.persistence.repositories.customer.PromiseToPayRepository.claim_due_for_follow_up`
    takes ``FOR UPDATE SKIP LOCKED``, so two sweeps take different rows rather than both acting on
    one — and the lock is only meaningful while the transaction that took it is still open, which
    is why this does not read a due set, commit, and act on it afterwards the way the review sweep
    does. That sweep can afford to, because its handler re-checks under the case lock; this one
    resolves the promise itself, and a resolution decided on a released lock is a resolution two
    sweeps could both decide.

    Each claimed promise takes exactly one branch, and R23.C13's "within the same evaluation" is
    what makes them branches of one function rather than three sweeps:

    * **The case has ended other than ``RECOVERED``** — void the promise, naming the Terminal_
      State (R23.C12). The single point where that clause is applied; see the module docstring for
      why a hook on every terminal transition was rejected.
    * **The case has ended ``RECOVERED``** — leave it. The authoritative read that verified the
      capture is what sets ``KEPT`` with a measured interval, and a sweep that voided it first
      would replace a measurement with an absence. If the read has not run yet, it will.
    * **The Recovery_Window has elapsed and the case is still live** — leave it, and schedule
      nothing. There is no instant inside the window left for a follow-up to occupy, and the
      expiry is the lifecycle sweep's to apply: transitioning the case here would make two sweeps
      writers of ``EXPIRED`` for one reason. The next pass finds the promise again, by which time
      the case is terminal and the first branch voids it — so the row leaves the scanned set
      within one further ``PROMISE_SWEEP_INTERVAL`` rather than lingering.
    * **Otherwise** — move ``RECORDED`` → ``FOLLOW_UP_SCHEDULED``, which is what makes
      ``PROMISE_TO_PAY_FOLLOW_UP`` eligible for selection under R24.C2, and enqueue R24.C13's one
      decision cycle so the eligibility is actually considered. From either state in
      :data:`FOLLOW_UP_REVIEW_STATES`, and ``WAITING_FOR_OUTCOME`` is the load-bearing half —
      see that constant for why enqueuing only from ``POLICY_CHECK`` enqueued a cycle for exactly
      the cases that cannot hold a promise.

    **The elapsed Cooldown is applied by the decision cycle, not here.** R23.C13 names it among
    the three things one evaluation must apply, and the cycle this branch enqueues is what applies
    it: ``COOLDOWN_INTERVAL`` is policy check 8, evaluated against ``last_outbound_at`` at the
    moment of the decision. A cooldown comparison in the sweep would be a second place the
    interval is enforced, and the one that would drift is this one — the sweep runs every five
    minutes and the check runs on the decision.

    The review is enqueued with ``SCHEDULED_REVIEW``. Not a new ``ReviewTrigger`` member: the
    enumeration is read by a persisted ``CHECK`` constraint and a dashboard filter, so a fourth
    member is a migration, and ``SCHEDULED_REVIEW`` is honest about what happened — a review came
    due on a schedule, and the schedule was the promise's Follow_Up_Instant. R30.C15 means the
    trigger changes nothing the review then does, so nothing is lost by not distinguishing it.
    """
    instant = now() if moment is None else ensure_utc(moment)
    considered = scheduled = voided = skipped = reviews = 0

    with tenant_transaction(merchant_id, factory) as session:
        promises = PromiseToPayRepository(session)
        cases = RecoveryCaseRepository(session)
        # The claimed rows are acted on inside the transaction that claimed them, and the loop
        # reads them directly rather than copying ids out first: the ``FOR UPDATE SKIP LOCKED``
        # that makes two sweeps take different rows is only in force while this transaction is
        # open, so a set of ids carried past a commit would be a set of promises nothing holds.
        for row in promises.claim_due_for_follow_up(merchant_id, now=instant, limit=limit):
            considered += 1
            promise_id = row.id
            case_id = row.case_id
            status = PromiseStatus(str(row.status))
            window_end = ensure_utc(row.window_end_at_snapshot)

            case = cases.get(merchant_id, case_id)
            if case is None:  # pragma: no cover - RESTRICT makes a case undeletable
                continue
            state = CaseState(str(case.state))

            if state in TERMINAL_STATES:
                if state is CaseState.RECOVERED:
                    skipped += 1
                    continue
                if void_for_terminal_state(
                    session, merchant_id, case_id, terminal_state=state
                ):
                    voided += 1
                else:  # pragma: no cover - the claim's lock makes a concurrent move unreachable
                    skipped += 1
                continue

            if window_end <= instant:
                skipped += 1
                _logger.info(
                    "promise follow-up not scheduled: the recovery window has elapsed",
                    case_id=str(case_id),
                    promise_id=str(promise_id),
                )
                continue

            if status is PromiseStatus.RECORDED and promises.resolve(
                merchant_id,
                promise_id,
                expected_statuses=(PromiseStatus.RECORDED,),
                values={"status": PromiseStatus.FOLLOW_UP_SCHEDULED.value},
            ):
                scheduled += 1
            if state in FOLLOW_UP_REVIEW_STATES and (
                enqueue_case_review(
                    session,
                    merchant_id,
                    case_id,
                    trigger=ReviewTrigger.SCHEDULED_REVIEW,
                    correlation_id=correlation_id,
                )
                is not None
            ):
                reviews += 1

    tally = SweepTally(
        considered=considered,
        scheduled=scheduled,
        voided=voided,
        skipped=skipped,
        reviews_enqueued=reviews,
    )

    if tally.considered:
        _logger.info(
            "promise sweep completed",
            merchant_id=str(merchant_id),
            considered=tally.considered,
            scheduled=tally.scheduled,
            voided=tally.voided,
            skipped=tally.skipped,
            reviews_enqueued=tally.reviews_enqueued,
        )
    return tally

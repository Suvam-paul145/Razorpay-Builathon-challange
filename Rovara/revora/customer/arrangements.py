"""Partial_Arrangement_Request — a signal, an escalation, and nothing that touches money.

A customer asking to pay less, or to pay in instalments, is the one submission on the public
surface that names no fact Revora can act on. A Delay_Reason refines the next diagnosis. A
Promise_To_Pay moves when Revora acts. An arrangement request asks Revora to *agree to
something*, and R22 answers that structurally: there is nothing here that can agree. The
request is evidence, it fetches a person, and it changes no money field.

**The absence of an amount is the requirement, and it is enforced twice before this module.**
:class:`~revora.customer.signals.PartialArrangementSubmission` declares only a ``note`` and
sets ``extra="forbid"``, so a body carrying ``amount``, ``instalment_count`` or ``schedule`` is
a 422 from the schema rather than from a check somebody wrote (R22.C1). One layer down,
``customer_signal`` has no column for any of the three, so there is nowhere to put a partial
amount even if a request got past pydantic. This module is the third layer and it adds no
fourth place a number could enter: nothing here reads an amount off a submission, because no
submission carries one.

**What this module owns.** The two halves of the arrangement's consequence that the accepting
request may not perform itself, split exactly where R19.C9 puts the line:

* the **enqueue** — :func:`enqueue_arrangement_consequence`, called inside the transaction
  that persisted the ``customer_signal`` row, so the request either produced a signal *and* a
  queued consequence or produced neither;
* the **facts the worker and the memory layer read back** — :func:`first_arrangement_request`
  and :func:`hard_stop_recorded`, which are the two questions "what did the customer ask, and
  when" and "did they also say something stronger".

The escalation itself is :func:`revora.jobs.pipeline.handle_partial_arrangement`, one layer up,
because a state transition goes through ``apply_locked_transition`` and that stays the only
writer of ``recovery_case.state``. The split is the same one
:mod:`revora.customer.suppression` makes and for the same reason: writing a row the
Customer_Response_Service owns is not one of the five things R19.C9 forbids inside the
accepting request, and transitioning a case is.

**:data:`PARTIAL_ARRANGEMENT_KIND` is declared here rather than in ``revora.jobs.scheduler``**,
on the same layering argument that put ``CONTACT_SUPPRESSION_KIND`` in
:mod:`revora.customer.suppression` and ``CASE_REVIEW_KIND`` in :mod:`revora.cases.review`: the
enqueuer sits below ``revora.jobs`` and cannot import from it, and a constant each side spelled
for itself would give one dedupe key two spellings. ``revora.jobs.worker`` imports it from here,
which is the direction the layering allows.

**Why the hard stop wins, and why that is a read rather than a race** (R22.C10). A customer can
persist both an arrangement request and a Hard_Stop_Reason — the request first, then the hard
stop, because a hard stop revokes the case's tokens and nothing can be submitted after one. Both
enqueue a consequence and both consequences want to terminate the case, and a Recovery_Case
holds exactly one Terminal_State reason. Resolving that by "whichever job runs first" would make
the recorded reason a function of queue order, which is the one thing about it nobody could
defend afterwards: the same two submissions would end one case ``CUSTOMER_DISPUTED_CHARGE`` and
the next ``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT``. So the arrangement handler *asks*, through
:func:`hard_stop_recorded`, and yields. The hard stop is the stronger statement — an objection
to the debt rather than a request about how to settle it — and the arrangement stays recorded as
a signal, which is all R22.C10 asks of it.

**The note is carried by reference, and that is R29.C10 rather than a shortcut.** See
:func:`arrangement_feature`. A verbatim copy of the note in ``memory_observation.features``
would be a copy of customer free text that the retention sweep cannot reach, and R29.C10 is
explicit that Recovery_Memory retains the *non-identifying* Customer_Signal fields. So the
feature holds the signal identifier and the note's non-identifying facts, and the text is
resolved through the row the sweep redacts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from revora.customer.suppression import hard_stop_for
from revora.domain.enums import CustomerSignalKind, DelayReason, HardStopReason
from revora.persistence.repositories.customer import CustomerSignalRepository
from revora.persistence.repositories.jobs import JobRepository
from revora.platform.clock import ensure_utc, now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

__all__ = [
    "FEATURE_PARTIAL_ARRANGEMENT",
    "PARTIAL_ARRANGEMENT_KIND",
    "ArrangementRequest",
    "arrangement_feature",
    "enqueue_arrangement_consequence",
    "first_arrangement_request",
    "hard_stop_recorded",
]

_logger = get_logger(__name__)


PARTIAL_ARRANGEMENT_KIND: Final[str] = "partial_arrangement"
"""The job kind that applies an arrangement request's terminal consequence (R22.C2).

Not a periodic sweep, so it is absent from ``PERIODIC_SWEEP_KINDS`` and no clock enqueues it.
One job per request, enqueued by the transaction that wrote the signal, deduped on the case —
so there is no window in which a request exists and its consequence is neither applied nor
queued, and two requests on one case queue one application."""

FEATURE_PARTIAL_ARRANGEMENT: Final[str] = "partial_arrangement_request"
"""The ``memory_observation.features`` key an arrangement request occupies (R22.C6).

Deliberately **not** one of ``revora.domain.segments.FEATURE_KEYS``. Those five are matched by
JSONB containment against a string subset, and the estimator's backoff levels are truncations of
that one ordered tuple — so a sixth *segment* feature would extend every level at once and
resegment the whole training set. This key is an observation feature in R15.C1's sense and not a
segment dimension: containment queries never name it, and its value is a nested object, which a
containment probe built from string values cannot match even by accident. That is the structural
half of "adding this cannot move a baseline"."""


# ---------------------------------------------------------------------------
# What one request is, once read back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArrangementRequest:
    """One persisted Partial_Arrangement_Request, as the worker and the dashboard read it.

    Frozen and slotted like every other record shape in this package: three consumers read it
    and none of them amends it.

    **There is no ``amount``, no ``instalment_count`` and no ``schedule`` field, and their
    absence here is the third layer of R22.C1.** The schema has no column and the request model
    has no field, so a projection that declared one could only ever fill it with a value it
    invented. Stating that as an absent field rather than as a comment means a future caller
    asking "how much did they offer?" gets a type error instead of a plausible-looking zero.
    """

    signal_id: uuid.UUID
    requested_at: datetime
    """The request instant (R22.C6, R22.C9). ``customer_signal.submitted_at`` — the instant the
    Customer_Response_Service accepted the write, not the instant a sweep noticed it."""

    note: str | None
    """The accompanying Delay_Reason_Note, or ``None``. Customer-supplied, unverified, inert.

    ``None`` covers two different situations on purpose — no note was written, or a note was
    written and the retention sweep has since redacted it — and :attr:`note_redacted_at` is
    what separates them. A presentation surface needs the difference: "the customer said
    nothing" and "the customer said something we are no longer allowed to keep" are not the
    same fact about a case."""

    note_truncated: bool
    note_redacted_at: datetime | None


def first_arrangement_request(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> ArrangementRequest | None:
    """The earliest Partial_Arrangement_Request on a case, or ``None``.

    **Earliest, not latest**, which is the opposite of
    :meth:`~revora.persistence.repositories.customer.CustomerSignalRepository.latest_delay_reason`
    and for a reason that does not carry over. A second stated reason *corrects* the first, so
    diagnosing on the superseded one would make the correction pointless. A second arrangement
    request corrects nothing — it carries no value to revise — it is the same customer asking the
    same thing again because the first ask has not been answered yet. The instant that matters is
    therefore the one the request was first made, since that is when the case became a person's
    problem and it is what the merchant is measured against.

    A second request is reachable, briefly: the escalation is applied by the worker, so a
    customer can submit twice before the job runs. After the escalation the case is terminal and
    ``record_signal`` refuses with 409, and the dedupe key means the two submissions queue one
    application.
    """
    row = CustomerSignalRepository(session).first_of_kind(
        merchant_id, case_id, kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST
    )
    if row is None:
        return None
    return ArrangementRequest(
        signal_id=row.id,
        requested_at=ensure_utc(row.submitted_at),
        note=row.delay_reason_note,
        note_truncated=bool(row.note_truncated),
        note_redacted_at=(
            None if row.note_redacted_at is None else ensure_utc(row.note_redacted_at)
        ),
    )


def hard_stop_recorded(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> HardStopReason | None:
    """The Hard_Stop_Reason this case also holds, or ``None``. R22.C10's condition.

    Keyed on the **case**, not on the Suppression_Scope, and the difference decides whether
    R22.C10 is correct or merely plausible. ``contact_suppression`` is scoped to a customer and
    an order (R21.C8), so a scope lookup would also find a suppression created by a *different*
    case of the same customer — and nothing escalates this case on account of that one, because
    ``handle_contact_suppression`` is enqueued per case. Deferring to it would leave this case
    never terminated at all, which is the worst available outcome: a customer asked for a person
    and no queue ever received the case.

    Asked of the signals rather than of ``contact_suppression`` for a second reason.
    ``record_hard_stop`` inserts ``ON CONFLICT DO NOTHING``, so a hard stop on a scope that was
    already suppressed writes no row naming this case — and yet still enqueues this case's
    consequence, which still escalates it. The persisted Delay_Reason is the fact that is present
    in both paths, and it is also exactly what R22.C10 says: *a Partial_Arrangement_Request and a
    Hard_Stop_Reason are persisted for one Recovery_Case*.

    The classification comes from :func:`~revora.customer.suppression.hard_stop_for`, so which
    reasons are hard stops is decided in one place and this function cannot disagree with the
    module that suppresses contact.
    """
    row = CustomerSignalRepository(session).first_with_delay_reason_in(
        merchant_id, case_id, reasons=_HARD_STOP_REASON_VALUES
    )
    if row is None or row.delay_reason is None:
        return None
    return hard_stop_for(DelayReason(str(row.delay_reason)))


_HARD_STOP_REASON_VALUES: Final[tuple[str, ...]] = tuple(
    sorted(reason.value for reason in DelayReason if hard_stop_for(reason) is not None)
)
"""The Delay_Reason values that are hard stops, as the strings the query compares.

Derived from :func:`~revora.customer.suppression.hard_stop_for` rather than written out, so a
third hard stop is covered by :func:`hard_stop_recorded` the day it is classified instead of
needing a second edit here — and the edit that would be forgotten is this one, because forgetting
it fails nothing: the query would simply stop finding the new reason and an arrangement request
would race a hard stop for the terminal reason, silently and only sometimes."""


# ---------------------------------------------------------------------------
# The enqueue (R19.C9, R22.C2)
# ---------------------------------------------------------------------------


def enqueue_arrangement_consequence(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    signal_id: uuid.UUID,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> None:
    """Queue the escalation an arrangement request causes. Caller's transaction.

    One statement, and everything it deliberately is not is the point of the function existing
    at all. It applies no transition, schedules no Candidate_Action, evaluates no policy, issues
    no Payment_Provider request and issues no Communication_Provider request — the five things
    R19.C9 forbids inside the request that accepted a Customer_Signal. What it does is the second
    half of that clause: *enqueue the consequences for the worker to apply*.

    Shares the caller's transaction, so the ``customer_signal`` row, the token counter increment,
    this job and the audit record commit together or none of them does. An audit write that misses
    ``AUDIT_WRITE_TIMEOUT`` therefore leaves no queued escalation, which is R29.C12's *SHALL
    enqueue no consequence for the worker*.

    **Nothing is cancelled and nothing is expired.** This is the difference between an arrangement
    request and a hard stop, and it is R22.C8. A live payment link stays live and unmodified for
    its remaining validity, so a customer who reads the escalation as "talk to a person" and then
    decides to pay in full still recovers the case under R10.C14. There is no provider client in
    this function's arguments and none reachable from this package, so "issues no cancellation" is
    a fact about what is importable here rather than a discipline this function keeps.

    Deduped on ``partial_arrangement:{case_id}``. Per case rather than per signal, because the
    consequence is one escalation of one case however many times the customer asked — and the
    partial unique index over *pending* jobs is what makes the second submission add no second
    job while the first is still queued.
    """
    instant = now() if moment is None else ensure_utc(moment)
    JobRepository(session).enqueue(
        merchant_id,
        kind=PARTIAL_ARRANGEMENT_KIND,
        payload={
            "case_id": str(case_id),
            # The signal id travels rather than being re-derived by the handler, on the same
            # terms the Hard_Stop_Reason travels on a suppression job: the row that caused the
            # consequence is named by the job that applies it, so an audit reader can join the
            # two without inferring which of a case's signals was the trigger.
            "signal_id": str(signal_id),
            "correlation_id": None if correlation_id is None else str(correlation_id),
        },
        run_after=instant,
        dedupe_key=f"{PARTIAL_ARRANGEMENT_KIND}:{case_id}",
        case_id=case_id,
        correlation_id=correlation_id,
    )
    _logger.info(
        "partial arrangement request recorded",
        case_id=str(case_id),
        signal_id=str(signal_id),
    )


# ---------------------------------------------------------------------------
# The Recovery_Memory feature (R22.C6)
# ---------------------------------------------------------------------------


def arrangement_feature(request: ArrangementRequest) -> dict[str, object]:
    """The observation feature one arrangement request contributes (R22.C6).

    A nested object under a single key rather than three flat keys beside the five segment
    features. Flat keys would sit in the same namespace the estimator probes by containment, and
    while a probe built from :data:`~revora.domain.segments.FEATURE_KEYS` would never name them,
    "would never" is a promise and a namespace is a fact. Nesting makes the separation structural:
    ``features @> '{"risk_cause": "..."}'`` cannot match a nested document, so this key cannot
    become a segment dimension by accident.

    **The note is referenced, not copied, and this is the load-bearing decision in the module.**

    R22.C6 asks the observation to carry "the accompanying Delay_Reason_Note where one exists",
    and R29.C10 requires a note past ``CUSTOMER_DATA_RETENTION`` to be deleted or irreversibly
    masked while *the non-identifying Customer_Signal fields required for metrics and for
    Recovery_Memory are retained*. Those two are reconcilable in exactly one way. A verbatim copy
    of the text in ``memory_observation.features`` would be customer free text in a table the
    retention sweep does not scan and has no index for — a second copy that outlives the
    redaction, which is precisely the failure R29.C10 names. So the feature carries
    ``signal_id``, and the text lives on ``customer_signal.delay_reason_note`` where
    ``claim_notes_for_retention`` can reach it. A reader who wants the note joins; a reader who
    wants it after the retention bound correctly finds nothing.

    The rejected alternatives, both considered and both worse:

    * **Copy the note and add ``memory_observation`` to the retention sweep.** A second table in
      the sweep's scan, a second partial index, a second redaction to keep consistent with the
      first — and the failure mode of getting it wrong is a note that was reported as deleted and
      was not. One copy that one sweep reaches is a smaller claim and a checkable one.
    * **Copy a truncated or hashed note.** A hash of free text is not anonymous — the space of
      things a customer types into this box is small enough to enumerate — and a truncation is
      still the customer's words, just fewer of them.

    What is copied is the note's *non-identifying* facts: whether one exists, how long it is, and
    whether it was truncated on the way in. A future trainer can condition on "this customer wrote
    at length" without the text, and none of the three survives as content a person wrote.
    """
    return {
        FEATURE_PARTIAL_ARRANGEMENT: {
            "signal_id": str(request.signal_id),
            "requested_at": request.requested_at.isoformat(),
            # The note by reference. See above: the text is deliberately absent.
            "note_present": request.note is not None,
            "note_length": 0 if request.note is None else len(request.note),
            "note_truncated": request.note_truncated,
            "note_redacted_at": (
                None
                if request.note_redacted_at is None
                else request.note_redacted_at.isoformat()
            ),
        }
    }

"""Three write shapes, one transaction each, and no authority in any of them.

What a customer may say about their own payment: why it is late, when they will pay, or that
they want to pay differently. Each is a piece of *evidence* about the next decision and none of
them is a decision. R19.C9 is the whole shape of this module: an accepted write causes **no**
Recovery_Case state transition, **no** Candidate_Action scheduling, **no** Policy_Engine
evaluation, **no** Payment_Provider request and **no** Communication_Provider request inside
the request that accepted it. What it does is persist a row and enqueue a job, and the worker
applies every consequence through ``apply_transition`` — which stays the only writer of
``recovery_case.state``.

That separation is structural rather than diligent. This package sits below ``revora.policy``,
``revora.optimizer``, ``revora.execution`` and ``revora.providers`` in the layering contract, so
there is nothing here to import that could evaluate a policy or call a provider. P35 — *no
signal sequence produces an ``APPROVED`` verdict* — is therefore a property of what is reachable
from this file, and ``lint-imports`` is what keeps it one.

**The schema is the rejection.** All three request models set ``extra="forbid"``, so a field
outside the declared shape is a 422 with no hand-written check anywhere (R19.C4). The
partial-arrangement model declares no ``amount``, no ``instalment_count`` and no ``schedule``, so
**R22.C1's rejection is the schema's default behaviour** — and it is backed by the same absence
one layer down, where ``customer_signal`` has no column for any of the three. There is nowhere
to put a partial amount, so no code path can accept one even by mistake.

**Four writes, one transaction, all or none** (R19.C5, R29.C12). An accepted write contains
exactly:

1. the ``accepted_submission_count`` increment, under the token's row lock;
2. the ``customer_signal`` insert;
3. the enqueued ``case_review`` job, when the case is at ``POLICY_CHECK`` (R30.C8);
4. the ``CUSTOMER_SIGNAL_RECORDED`` audit record.

They share the caller's transaction, which is why the failure story needs no compensating
action: an audit write that misses ``AUDIT_WRITE_TIMEOUT`` rolls back the other three, so there
is no signal, no increment and no queued consequence, and the caller answers 503. The audit
write is deliberately **last**, so the rollback it causes is a rollback of work that has already
been staged — which is what makes the "all four or none" claim checkable by injecting a failure
into one statement rather than by reasoning about ordering.

**The Promise_To_Pay write joins that transaction and the audit record stays last.** A promise
adds a ``promise_to_pay`` row, and on the beyond-window path a queued escalation, between the
signal insert and the audit record — the same slot the suppression and the arrangement occupy,
and for the same reason: writing a row this package owns is not one of the five things R19.C9
forbids here, and transitioning a case is. See :mod:`revora.customer.promises` for the clamp and
for why a date past the window escalates instead of being clamped to the window end.

**Two caps, both under the row lock, and they are not the same cap.**
``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` bounds one credential; ``MAX_CUSTOMER_SIGNALS_PER_CASE``
bounds one case. Today both default to 5, and they still both have to exist: a case outlives a
token — a terminal-state revocation followed by a further approved action mints a second one —
so a customer can reach either bound with room left under the other. Both answer 429, and
**reads keep being served** either way (R18.C9): a customer who has explained themselves five
times must not lose the page telling them what they owe as a consequence.

**The Partial_Arrangement_Request enqueues an escalation and changes no money field.** R22.C2
terminates the case ``ESCALATED`` with ``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT``, which is a
state transition and therefore the worker's, so this module enqueues it through
:func:`~revora.customer.arrangements.enqueue_arrangement_consequence` and stops there. It is worth
being explicit about what the accepted write leaves alone, because a request to pay differently is
the one submission somebody would expect to move a number: ``payment_amount``, the currency and
``window_end_at`` are untouched (R22.C7), no live payment link is cancelled or expired (R22.C8),
and there is nowhere to record a proposed amount because neither the request model nor
``customer_signal`` has a field for one (R22.C1).

**What this module deliberately does not do.** It does not *apply* the Delay_Reason mapping
table it declares — :data:`DELAY_REASON_CAUSE` is data, and ``revora.diagnosis`` is what reads
it, on the next decision cycle rather than in this request (R20.C4). It does not compute a
Follow_Up_Instant either: the clamp is :func:`revora.customer.promises.plan_promise`, called
through :func:`~revora.customer.promises.record_promise`, so the arithmetic that has to land
strictly inside a database ``CHECK`` sits in one pure function rather than inline in a request
handler.

**Two promise guards live here, and the ordering between them is deliberate.** A date at or
before the submission instant is the degenerate one and consults no configured bound, because
``promise_date > recorded_at`` is a ``CHECK`` on ``promise_to_pay`` — such a date is not a
promise the system can *hold* rather than one it declines to accept. ``PROMISE_MIN_LEAD_TIME``
(R23.C2) is the configured one, and it is checked *second* precisely so the first still refuses a
past date if a merchant ever configures the lead time to zero. Both answer 422 and both persist
nothing at all. The 409 of R23.C7 is the odd one out and is applied further down, after the
signal has been inserted, because R23.C7 requires the submission kept as a Customer_Signal while
the promise itself is refused.

**The one consequence that is applied here is the hard stop's suppression, and only its
non-transitional half.** R21.C1 requires the ``contact_suppression`` row to be written in the
same atomic transaction as the ``customer_signal`` it names, and R21.C10 requires the case's
tokens revoked when it is — so both join the five writes below rather than waiting for the
worker. Neither is one of the five things R19.C9 forbids in this request: a suppression row and
a token revocation are writes to two tables the Customer_Response_Service owns, not a state
transition, not a scheduling, not a policy evaluation and not a provider call. The transition to
``ESCALATED``, the cancellation of scheduled actions and the treatment of an in-flight intent are
R21.C4 through C7 and stay with the worker, which applies them through ``apply_transition`` — the
only writer of ``recovery_case.state``. See :mod:`revora.customer.suppression` for why the split
falls exactly there.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, ModelWrapValidatorHandler, PrivateAttr, model_validator

from revora.audit.events import (
    CUSTOMER_SIGNAL_LIMIT_REACHED,
    CUSTOMER_SIGNAL_RECORDED,
    CUSTOMER_SIGNAL_REJECTED,
    CUSTOMER_SUBMISSION_LIMIT_REACHED,
    PROMISE_ALREADY_RECORDED,
    PROMISE_RECORDED,
    PROMISE_REJECTED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.review import enqueue_case_review
from revora.customer.arrangements import enqueue_arrangement_consequence
from revora.customer.promises import (
    PromiseOutcome,
    meets_min_lead_time,
    record_promise,
)
from revora.customer.suppression import (
    SuppressionOutcome,
    hard_stop_for,
    record_hard_stop,
)
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    ReviewTrigger,
    RiskCause,
)
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models.customer import (
    DELAY_NOTE_MAX_LENGTH as SCHEMA_DELAY_NOTE_MAX_LENGTH,
)
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.customer import (
    CustomerAccessTokenRepository,
    CustomerSignalRepository,
)
from revora.platform.clock import ensure_utc, now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.customer.tokens import VerifiedToken
    from revora.platform.config import Configuration

__all__ = [
    "DELAY_REASON_CAUSE",
    "PROMISE_LEAD_TIME_DETAIL",
    "SIGNAL_REJECTION_STATUS",
    "DelayReasonSubmission",
    "PartialArrangementSubmission",
    "PromiseSubmission",
    "SignalOutcome",
    "SignalRejection",
    "SignalSubmission",
    "cause_for_delay_reason",
    "effective_note_limit",
    "record_signal",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The three request shapes
# ---------------------------------------------------------------------------


class _Submission(BaseModel):
    """The shared configuration, and nothing else.

    ``extra="forbid"`` on the base so no subclass can forget it, and no field on the base so
    there is no inherited column a shape acquires without declaring it. R19.C4's 422 is this one
    line, applied three times.

    ``str_strip_whitespace`` is on because a note that is only whitespace is not a note, and the
    alternative — storing it and stripping at every read — is three places to forget.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @property
    def kind(self) -> CustomerSignalKind:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError

    @property
    def submitted_note(self) -> str | None:
        """The accompanying free text, where the shape declares one. ``None`` otherwise.

        Named ``submitted_note`` rather than ``note`` deliberately. The wire field *is* ``note``,
        and a Pydantic model field and a base-class property of the same name collide silently —
        the property wins on the subclass and the declared field stops being readable, so the
        stored row and the audit payload quietly disagree with the request body. Prefixing the
        accessors leaves the wire names free for the fields, which is where they have to be:
        aliasing them instead would give every shape two accepted spellings and ``extra="forbid"``
        would stop being the whole of R19.C4.
        """
        return None

    @property
    def submitted_delay_reason(self) -> DelayReason | None:
        """The stated reason, where the shape declares one. ``None`` otherwise."""
        return None

    def recorded_values(self) -> Mapping[str, object]:
        """What the audit record calls "the submitted values" (R19.C6).

        Declared per shape rather than derived from ``model_dump()``, so a field added to a
        request model does not silently become an audit field — and so the audit payload's key
        set is reviewable in this file instead of being a function of Pydantic's serializer.
        """
        return {}


class DelayReasonSubmission(_Submission):
    """``POST .../delay-reason`` — ``{"delay_reason": <enum>, "note": <string?>}``.

    ``delay_reason`` is typed as the enumeration, so a value outside it is a 422 naming the
    field and never reaching a comparison (R20.C1). The two Hard_Stop_Reason members are
    accepted here like any other: recording that a customer disputed a charge is this task's
    job, and suppressing contact because of it is task 42's.
    """

    delay_reason: DelayReason
    note: str | None = None

    @property
    def kind(self) -> CustomerSignalKind:
        return CustomerSignalKind.DELAY_REASON

    @property
    def submitted_note(self) -> str | None:
        return self.note

    @property
    def submitted_delay_reason(self) -> DelayReason:
        return self.delay_reason

    def recorded_values(self) -> Mapping[str, object]:
        # The reason, and *whether* a note was supplied — never the note itself. The note is
        # free text a stranger typed on a public endpoint, and the audit log is the one store
        # that cannot be rewritten, so it is retained on ``customer_signal`` where the retention
        # sweep can reach it and not in a record that outlives every retention bound.
        return {
            "delay_reason": self.delay_reason.value,
            "note_present": bool(self.note),
        }


class PromiseSubmission(_Submission):
    """``POST .../promise`` — ``{"promise_date": <ISO-8601 instant>}`` and nothing else.

    One field. No amount, because a promise is a statement about *when*, and a promise to pay
    less is a Partial_Arrangement_Request, which is a different shape with different
    consequences. Accepting an amount here would let the two be confused at the one point where
    the difference is whether a negotiation happened.
    """

    promise_date: datetime

    _received_representation: str = PrivateAttr(default="")
    """The submitted value exactly as it arrived, retained for R23.C1.

    A **private** attribute rather than a declared field, and that is load-bearing rather than
    stylistic. A declared field would be a field ``extra="forbid"`` then *permits* — so a caller
    could supply their own ``received_representation`` and the column meant to hold what arrived
    would hold whatever they said arrived, which is worse than not having the column. A private
    attribute is unreachable from the request body and is set by the validator below from the raw
    input, so the only writer is the parse itself."""

    @model_validator(mode="wrap")
    @classmethod
    def _retain_received(
        cls,
        data: Any,
        handler: ModelWrapValidatorHandler[PromiseSubmission],
    ) -> PromiseSubmission:
        """Parse normally, then keep the raw ``promise_date`` beside the parsed instant (R23.C1).

        ``mode="wrap"`` rather than ``mode="before"`` because the raw value and the constructed
        model are both needed: ``before`` sees the raw input but has no instance to attach it to,
        and ``after`` has the instance but the raw value is already gone. Wrapping is the one mode
        that has both.

        R16.C13's terms, applied to a promise instead of to a Payment_Event timestamp: a timezone
        read wrongly is only diagnosable if what arrived is still there. Pydantic accepts
        ``"2025-03-12T00:00:00+05:30"`` and ``"2025-03-11T18:30:00Z"`` as the same instant, and
        they are — but "the customer meant midnight their time" and "the customer meant half past
        six in the evening UTC" are different sentences about the same row, and only the retained
        string distinguishes them.

        Falls back to the parsed instant's ISO form when the input was not a mapping — a model
        constructed directly in a test, or validated from another model. The column is ``NOT
        NULL``, so a fallback is required, and the parsed instant is the honest one: it *is* what
        was received when what was received was already a datetime.
        """
        model: PromiseSubmission = handler(data)
        raw = data.get("promise_date") if isinstance(data, Mapping) else None
        model._received_representation = (
            ensure_utc(model.promise_date).isoformat() if raw is None else str(raw)
        )
        return model

    @property
    def received_representation(self) -> str:
        """What arrived, as a string. Never empty for a model built by validation."""
        return self._received_representation

    @property
    def kind(self) -> CustomerSignalKind:
        return CustomerSignalKind.PROMISE_TO_PAY

    def recorded_values(self) -> Mapping[str, object]:
        return {"promise_date": ensure_utc(self.promise_date).isoformat()}


class PartialArrangementSubmission(_Submission):
    """``POST .../partial-arrangement`` — ``{"note": <string?>}``.

    **This class declaring nothing else is R22.C1.** No ``amount``, no ``instalment_count``, no
    ``schedule``, so a submission carrying any of them is a 422 from ``extra="forbid"`` rather
    than from a check somebody wrote and somebody else could delete. The same absence is
    repeated in the schema — ``customer_signal`` has no column for any of the three — so the
    requirement holds at two layers and neither is a validation rule.
    """

    note: str | None = None

    @property
    def kind(self) -> CustomerSignalKind:
        return CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST

    @property
    def submitted_note(self) -> str | None:
        return self.note

    def recorded_values(self) -> Mapping[str, object]:
        return {"note_present": bool(self.note)}


type SignalSubmission = (
    DelayReasonSubmission | PromiseSubmission | PartialArrangementSubmission
)
"""The three shapes, as a closed union.

Closed rather than a protocol, so a fourth write shape is a change to this alias and to the
router's route table rather than something that appears by satisfying an interface. R19.C4 says
*exactly three*, and a union is how that is stated in the type system."""



# ---------------------------------------------------------------------------
# The declared Delay_Reason mapping table (R20.C5)
# ---------------------------------------------------------------------------


DELAY_REASON_CAUSE: Final[Mapping[DelayReason, RiskCause | None]] = MappingProxyType(
    {
        # "My salary has not landed yet" and "that is more than I can pay right now" are both
        # statements about the money not being there, which is what INSUFFICIENT_FUNDS names.
        # Two reasons collapsing to one cause is deliberate rather than a redundancy: they are
        # different things to *say*, the dashboard and Recovery_Memory keep them apart, and
        # only the cause is collapsed.
        DelayReason.SALARY_OR_CASHFLOW_TIMING: RiskCause.INSUFFICIENT_FUNDS,
        DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW: RiskCause.INSUFFICIENT_FUNDS,
        # "My card was declined" is the customer's account of the failure the provider would
        # have reported as a bank or network decline. The one row where the customer and the
        # provider are describing the same event from two sides.
        DelayReason.BANK_OR_CARD_PROBLEM: RiskCause.BANK_OR_NETWORK_FAILURE,
        # OTHER names no cause, and that is the whole reason OTHER exists (R20.C6). A customer
        # whose reason was not anticipated must not be pushed into one that is wrong, and the
        # honest consequence of "we do not know what they meant" is that the recorded cause is
        # left exactly as it was.
        DelayReason.OTHER: None,
        # The two Hard_Stop_Reasons name no cause either, for a different reason worth keeping
        # separate: they are not payment problems at all. A dispute and a cancellation are
        # objections to the debt itself, so they end contact and escalate to a person (R21)
        # instead of sharpening the next automated decision — there is no next decision to
        # sharpen. Listed rather than omitted so the table is total; see the check below.
        DelayReason.DISPUTES_THE_CHARGE: None,
        DelayReason.NO_LONGER_WANTS_THE_ORDER: None,
    }
)
"""R20.C5's table, declared once, in the module that owns the input side of it.

**Every row is an [ASSUMPTION].** Each is a plausible reading of a customer's words, not a
measured correspondence — nobody has established that a person who says "salary timing" in fact
failed for want of funds, and design.md's open-questions table records that gap rather than
hiding it. The mapping is worth having anyway, because the alternative is re-deciding the second
cycle on exactly the evidence the first cycle had.

Total over :class:`DelayReason` by construction, and the check below is what keeps it that way. A
seventh member added to the enumeration fails at import rather than falling through
:func:`cause_for_delay_reason` as "no refinement" — which is the failure that would be invisible,
because "no refinement" is also the correct answer for three of the six members there are today.

The consumer is :func:`revora.diagnosis.service.run_diagnosis`, one layer up. Declared here rather
than there because the enumeration and the three write shapes that carry it live here, and a
mapping table kept apart from the values it maps drifts from them."""

_unmapped_delay_reasons = sorted(
    reason.value for reason in DelayReason if reason not in DELAY_REASON_CAUSE
)
if _unmapped_delay_reasons:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        f"DELAY_REASON_CAUSE is not total over DelayReason; missing {_unmapped_delay_reasons}"
    )


def cause_for_delay_reason(reason: DelayReason) -> RiskCause | None:
    """The Risk_Cause a stated reason refines to, or ``None`` for no refinement.

    A function rather than callers indexing :data:`DELAY_REASON_CAUSE` themselves, so ``None``
    means exactly one thing — *this reason names no cause* — and never *somebody forgot a row*.
    The totality check above is what makes the direct lookup safe rather than optimistic; a
    ``KeyError`` from here would be a bug in that check, not in the caller.
    """
    return DELAY_REASON_CAUSE[reason]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@unique
class SignalRejection(StrEnum):
    """Why a write was refused. The status code is :data:`SIGNAL_REJECTION_STATUS`."""

    CASE_TERMINAL = "CASE_TERMINAL"
    """The case already holds a Terminal_State (R19.C8). 409.

    Distinct from a revoked token, which is 410 and is answered before this code runs: a
    terminal transition revokes the case's tokens, so in practice a customer reaching this
    branch is one whose case ended between their read and their write. Both answers are correct
    for what they say — 410 means *this link is dead*, 409 means *the case ended*."""

    CASE_SIGNAL_LIMIT_REACHED = "CASE_SIGNAL_LIMIT_REACHED"
    """``MAX_CUSTOMER_SIGNALS_PER_CASE`` reached on the case (R19.C7). 429."""

    TOKEN_SUBMISSION_LIMIT_REACHED = "TOKEN_SUBMISSION_LIMIT_REACHED"
    """``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` reached on the token (R18.C9). 429, reads still
    served."""

    PROMISE_DATE_NOT_IN_FUTURE = "PROMISE_DATE_NOT_IN_FUTURE"
    """A Promise_Date at or before the submission instant. 422.

    The degenerate half of R23.C2, and the half that needs no configured bound: ``promise_date >
    recorded_at`` is a ``CHECK`` on ``promise_to_pay``, so a date in the past is not a promise
    the system can hold rather than a promise it declines to accept.

    Kept as its own member now that :attr:`PROMISE_BELOW_MIN_LEAD_TIME` exists and subsumes it at
    every configured lead time above zero. The two are not redundant in the direction that
    matters: a merchant may configure the lead time to zero — meaning "any future date will do" —
    and this member is what still refuses a date the schema cannot hold. Collapsing them would
    make a zero lead time turn a 422 into a failed INSERT and a 503."""

    PROMISE_BELOW_MIN_LEAD_TIME = "PROMISE_BELOW_MIN_LEAD_TIME"
    """A Promise_Date inside ``PROMISE_MIN_LEAD_TIME`` of the submission instant (R23.C2). 422.

    Deferred until the bound existed, and now that it does this is the configured half of R23.C2:
    a date one second in the future is storable and is not a promise, because nothing useful can
    be scheduled around it. Persists no Promise_To_Pay, no Customer_Signal and no submission-count
    increment — unlike :attr:`PROMISE_ALREADY_RECORDED`, which keeps the signal.

    The response and the ``PROMISE_REJECTED`` audit record both name **the bound key** and never
    the interval or the submitted date, which is R23.C2's "naming the lead-time rule": the
    interval is a configuration value the customer has no standing to be told, and the submitted
    date is attacker-supplied text on an endpoint reachable without a session."""

    PROMISE_ALREADY_RECORDED = "PROMISE_ALREADY_RECORDED"
    """The case already holds ``MAX_PROMISES_PER_CASE`` promises (R23.C7). 409.

    **The one refusal on this surface that keeps its write.** The Customer_Signal is persisted,
    the submission count increments, and only the ``promise_to_pay`` row is refused — so the
    submission reaches Recovery_Memory even though the promise does not. R23.C7 asks for exactly
    that, and the reason is that a customer revising a promise is evidence about the case whether
    or not the system will hold a second one.

    So a :class:`SignalOutcome` carrying this rejection also carries a ``signal_id``, which is the
    single exception to that class's "either a persisted signal or a named refusal". The response
    is still the 409 this table names, because the promise is what the request asked for."""


SIGNAL_REJECTION_STATUS: Final[Mapping[SignalRejection, int]] = {
    SignalRejection.CASE_TERMINAL: 409,
    SignalRejection.CASE_SIGNAL_LIMIT_REACHED: 429,
    SignalRejection.TOKEN_SUBMISSION_LIMIT_REACHED: 429,
    SignalRejection.PROMISE_DATE_NOT_IN_FUTURE: 422,
    SignalRejection.PROMISE_BELOW_MIN_LEAD_TIME: 422,
    SignalRejection.PROMISE_ALREADY_RECORDED: 409,
}
"""The design's rejection table for the write path, as data the router reads.

Data rather than a branch the router repeats, for the same reason
``tokens.REJECTION_STATUS`` is: two places that decide a status code are two places that can
disagree, and the one this endpoint would disagree in is the one nobody is authenticated to
notice.

The two promise rows the design's table marked deferred are here rather than in the router, and
that is the whole of what "extend the data rather than adding an ``if``" means: the 422 for a
date inside ``PROMISE_MIN_LEAD_TIME`` and the 409 for a second promise are two entries in this
mapping, and ``revora.api.routers.customer`` gained no branch for either. It reads
:attr:`SignalOutcome.status_code`, which reads this table, so a new refusal is a member and a row
and nothing else."""

PROMISE_LEAD_TIME_DETAIL: Final[str] = "PROMISE_MIN_LEAD_TIME"
"""What the lead-time refusal discloses: the bound's key, and nothing else.

R23.C2 asks the rejection to name the lead-time rule. A key names the rule; the configured
interval would name the *value*, which is a merchant's operational setting and not the customer's
business, and the submitted date would echo attacker-supplied text into an audit log that cannot
be rewritten. Named as a constant so the response body, the audit record and any test assert
against one string."""


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """Either a persisted signal or a named refusal — with exactly one documented exception.

    The exception is R23.C7's 409: a second promise on one case is refused *and* its submission is
    kept as a Customer_Signal, so an outcome carrying
    :attr:`SignalRejection.PROMISE_ALREADY_RECORDED` carries a :attr:`signal_id` too. That is the
    clause rather than a leak in the invariant, and it is stated here because a reader who
    believed the stricter version would conclude the signal had not been written.

    Never *neither*, without exception: every path returns a signal id or a named reason.
    """

    signal_id: uuid.UUID | None = None
    rejection: SignalRejection | None = None
    detail: str | None = None
    """What the response body may name. A field name, or a Terminal_State — never a submitted
    value, which on this endpoint is attacker-supplied text."""

    suppression: SuppressionOutcome | None = None
    """The Contact_Suppression this write produced, where the reason was a Hard_Stop_Reason.

    ``None`` for the four Delay_Reasons that are payment problems and for the other two
    submission shapes, which is most writes. Carried on the outcome rather than left in the
    audit record alone because the router's response and the worker's enqueued consequence both
    name the scope, and re-deriving it in either place would be a second derivation of a key
    whose two copies could disagree."""

    arrangement_enqueued: bool = False
    """Whether this write enqueued the escalation a Partial_Arrangement_Request causes (R22.C2).

    ``True`` for exactly one of the three submission shapes, and carried on the outcome for the
    same reason :attr:`review_enqueued` is: the router's response and the audit record both name
    what the write set in motion, and a caller that had to re-derive it from the submission type
    would be a second place the mapping between shape and consequence lives.

    A queued escalation, never an applied one. The case is still at whatever state it was in when
    this returned — R19.C9 — so a reader must not take ``True`` to mean the case is terminal."""

    review_enqueued: bool = False
    """Whether this write enqueued a decision cycle (R30.C8).

    ``False`` is the common answer and is not a failure: the review trigger fires only for a
    case resting at ``POLICY_CHECK``, and a case waiting on an outcome has a cycle of its own
    already in flight."""

    promise: PromiseOutcome | None = None
    """The Promise_To_Pay this write produced, where the submission was a promise.

    ``None`` for the other two submission shapes, and also for a promise refused before anything
    was persisted — the two 422s. Present and :attr:`~revora.customer.promises.PromiseOutcome.
    refused` for R23.C7's 409, which is how the caller distinguishes "no promise was attempted"
    from "a promise was attempted and the case already holds its limit".

    Carried on the outcome rather than left in the audit record alone for the reason
    :attr:`suppression` is: the ``PROMISE_RECORDED`` record, the router's response and the page's
    next projection all name the same clamp, and re-deriving it in any of them would be a second
    computation of an instant whose two copies could disagree."""

    signals_remaining: int = 0

    @property
    def accepted(self) -> bool:
        """Whether a Customer_Signal was persisted.

        Deliberately *not* the negation of :attr:`rejection`. R23.C7's 409 is both accepted in
        this sense and rejected in the response's, and a reader asking "was the submission kept
        for Recovery_Memory" wants this one."""
        return self.signal_id is not None

    @property
    def status_code(self) -> int:
        """The HTTP status this outcome answers with; 201 when accepted."""
        return 201 if self.rejection is None else SIGNAL_REJECTION_STATUS[self.rejection]


# ---------------------------------------------------------------------------
# Note length
# ---------------------------------------------------------------------------


def effective_note_limit(config: Configuration) -> int:
    """The retained note length: the smaller of the configured bound and the column's.

    Two numbers for one bound, and the ``min`` is why that is safe rather than sloppy.
    ``DELAY_NOTE_MAX_LENGTH`` is an ``app_config`` row so a merchant can lower it, and
    ``customer_signal.delay_reason_note`` carries ``CHECK (char_length(...) <= 500)`` from
    migration ``0008`` as a backstop. Lowering the row takes effect on the next write; raising
    it above the column's 500 cannot, and attempting it would fail the insert on the one
    endpoint reachable without a session. So the writer takes the smaller and the two can never
    contradict each other in the direction that breaks a request.
    """
    return min(int(config.DELAY_NOTE_MAX_LENGTH), SCHEMA_DELAY_NOTE_MAX_LENGTH)


_UNSTORABLE: Final[str] = "\x00"
"""The one character PostgreSQL ``TEXT`` cannot hold.

Not a sanitisation policy and not a denylist — it is the single code point the column physically
cannot store, and a submission containing it would otherwise fail the insert and answer 503. A
generated note found this: ``"line\\u2028break\\x00null"`` is a perfectly ordinary thing for a
stranger to paste into a text box, and 503 is the wrong answer to it, because the request was
well-formed and there is nothing for the caller to retry.

**Every other control character is kept.** ``\\u2028``, a bare carriage return, a right-to-left
override — all storable, all retained, all still inert. Removing them would be deriving a
judgement about the note's contents, which R20.C3 forbids the system from doing, and it would mean
the stored note differed from the submitted one for a reason the retention sweep and the dashboard
would both have to know about."""


def _note_for_storage(note: str | None, limit: int) -> tuple[str | None, bool]:
    """The note as stored, and whether it was truncated (R20.C2).

    An empty or whitespace-only note becomes ``None`` rather than an empty string, because
    ``ix_customer_signal_notes_for_retention`` is partial over ``delay_reason_note IS NOT NULL``
    and an empty string would put a row with nothing to redact into the retention sweep's
    scanned set forever.

    The NUL byte is removed **before** the length check, so the retained length is the length of
    what is actually stored. See :data:`_UNSTORABLE` for why that one character and no others.

    ``note_truncated`` reports length truncation and nothing else. A removed NUL does not set it,
    and that is the honest reading of R20.C2: the flag exists so a reader knows the note is
    *incomplete at the end*, and a note that lost an unstorable byte from the middle has not been
    cut short.

    The text is stored inert: never evaluated, never interpolated into a query — every write in
    this module goes through SQLAlchemy parameter binding — never interpolated into a provider
    request, and never rendered as markup. Escaping for presentation belongs to the surface that
    presents it, and storing an escaped copy would mean the stored value and the submitted value
    differ for reasons the retention sweep would then have to know about.
    """
    if note is None:
        return None, False
    text = note.replace(_UNSTORABLE, "").strip()
    if not text:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def record_signal(
    session: Session,
    config: Configuration,
    token: VerifiedToken,
    submission: SignalSubmission,
    *,
    correlation_id: uuid.UUID,
    moment: datetime | None = None,
) -> SignalOutcome:
    """Record one Customer_Signal, or refuse with a named reason. One transaction.

    Shares the caller's transaction rather than opening its own, and that sharing *is* R19.C5
    and R29.C12: the four writes below commit together or not at all, so there is no state in
    which a signal exists without its audit record, or a queued review exists without the signal
    that caused it.

    The order is fixed and each step is where it is for a reason.

    1. **Lock the token row.** Everything after this is serialized against another submission on
       the same credential, which is what makes the case-signal count below a check rather than a
       guess. Taken before the case is read, so the lock is held for the whole decision.
    2. **Read the case and refuse a terminal one** (R19.C8), naming the Terminal_State. Read
       without a lock: this write touches no case column, and locking the case row would put a
       public endpoint in contention with the pipeline's own writer. A terminal transition racing
       this read is benign in both directions — it revokes the token, so the *next* request is
       410, and the signal it raced is evidence rather than authority.
    3. **Refuse a case at its signal cap** (R19.C7). Counted under the token lock, which bounds
       submissions from one credential exactly; two *different* live tokens for one case cannot
       exist (``one_live_token_per_case``), so there is no second holder to race.
    4. **Increment the token's counter** (R18.C9). The comparison lives inside the ``UPDATE``, so
       the check and the increment cannot be separated by a concurrent request. This is the
       durable bound — the rate limiter is a per-process flood guard and cannot substitute for
       it.
    5. **Insert the signal.**
    5b. **Write the Promise_To_Pay**, where the submission is a promise (R23.C3 through C7).
       After the insert because ``promise_to_pay.customer_signal_id`` is a ``NOT NULL`` foreign key
       to the row step 5 just flushed, and before the audit records for the reason every other
       write is. This is also where R23.C7's 409 is decided, and it is the one refusal that
       happens *after* a write: the signal stays, the promise does not, and the outcome carries
       both a ``signal_id`` and a rejection.

    6. **Write the Contact_Suppression and revoke the case's tokens**, where the submitted
       Delay_Reason is a Hard_Stop_Reason (R21.C1, R21.C10). After the insert because
       ``contact_suppression.customer_signal_id`` is a ``NOT NULL`` foreign key to the row step
       5 just flushed, and before the audit record for the same reason every other write is: the
       audit write is the one that must be able to roll all of it back.
    7. **Enqueue the arrangement's escalation**, where the submission is a
       Partial_Arrangement_Request (R22.C2). Beside the suppression rather than instead of it:
       both are consequences this request may only *queue*, and both are queued in the
       transaction that persisted the signal so R29.C12's "enqueue no consequence" holds by the
       rollback rather than by a compensating delete.
    8. **Enqueue the review** when the case is at ``POLICY_CHECK`` (R30.C8), through the same
       :func:`~revora.cases.review.enqueue_case_review` the sweeper and the detection service
       use. Sharing the entry point is what makes R30.C9's "irrespective of the Review_Trigger"
       hold through one dedupe key rather than three.
    8b. **Write ``PROMISE_RECORDED`` or ``PROMISE_ALREADY_RECORDED``**, where the submission was
       a promise (R23.C8, R23.C7). Before the last record rather than instead of it: the promise
       record exists so a reader can check the clamp from the audit trail alone, and the signal
       record exists because a Customer_Signal was persisted. Both are true of one submission.

    9. **Write the audit record, last.** So a failure here rolls back everything above it.

    Returns a :class:`SignalOutcome`. A refusal is a named reason and never an exception: each
    one is a normal answer with its own status code, and raising would make the router's job
    guessing which exception meant which code.
    """
    instant = now() if moment is None else ensure_utc(moment)
    tokens = CustomerAccessTokenRepository(session)
    audit = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    # The two date guards of R23.C2, in this order and not the other. The degenerate one first,
    # so a merchant who configures the lead time to zero still gets a 422 rather than a failed
    # INSERT against ``promise_date > recorded_at``; the configured one second, because it is the
    # one that consults a bound and it subsumes the first at every positive value.
    if isinstance(submission, PromiseSubmission):
        promised = ensure_utc(submission.promise_date)
        if promised <= instant:
            return _refuse(
                audit,
                token,
                SignalRejection.PROMISE_DATE_NOT_IN_FUTURE,
                detail="promise_date",
                event_type=PROMISE_REJECTED,
                kind=submission.kind,
                correlation_id=correlation_id,
                moment=instant,
                remaining=_remaining(config, token.accepted_submission_count),
            )
        if not meets_min_lead_time(
            promised, instant=instant, min_lead_time=config.PROMISE_MIN_LEAD_TIME
        ):
            return _refuse(
                audit,
                token,
                SignalRejection.PROMISE_BELOW_MIN_LEAD_TIME,
                # The bound's key, never the interval and never the date. See
                # PROMISE_LEAD_TIME_DETAIL.
                detail=PROMISE_LEAD_TIME_DETAIL,
                event_type=PROMISE_REJECTED,
                kind=submission.kind,
                correlation_id=correlation_id,
                moment=instant,
                remaining=_remaining(config, token.accepted_submission_count),
            )

    # 1. The row lock. Its return value is deliberately unused for reading the counter — the
    #    conditional UPDATE below is the authority on the count, and reading it here as well
    #    would invite somebody to compare against it and reintroduce the read-then-write race
    #    the UPDATE exists to remove.
    tokens.lock_by_token_id(token.merchant_id, token.token_id)

    case = RecoveryCaseRepository(session).get(token.merchant_id, token.case_id)
    if case is None:  # pragma: no cover - RESTRICT makes the case undeletable
        return _refuse(
            audit,
            token,
            SignalRejection.CASE_TERMINAL,
            detail="case",
            event_type=CUSTOMER_SIGNAL_REJECTED,
            kind=submission.kind,
            correlation_id=correlation_id,
            moment=instant,
            remaining=_remaining(config, token.accepted_submission_count),
        )

    state = CaseState(str(case.state))
    if state in TERMINAL_STATES:
        return _refuse(
            audit,
            token,
            SignalRejection.CASE_TERMINAL,
            detail=state.value,
            event_type=CUSTOMER_SIGNAL_REJECTED,
            kind=submission.kind,
            correlation_id=correlation_id,
            moment=instant,
            remaining=_remaining(config, token.accepted_submission_count),
        )

    signals = CustomerSignalRepository(session)
    if signals.count_for_case(token.merchant_id, token.case_id) >= (
        config.MAX_CUSTOMER_SIGNALS_PER_CASE
    ):
        return _refuse(
            audit,
            token,
            SignalRejection.CASE_SIGNAL_LIMIT_REACHED,
            detail=None,
            event_type=CUSTOMER_SIGNAL_LIMIT_REACHED,
            kind=submission.kind,
            correlation_id=correlation_id,
            moment=instant,
            remaining=_remaining(config, token.accepted_submission_count),
        )

    accepted_count = tokens.increment_accepted_submissions(
        token.merchant_id,
        token.token_id,
        max_submissions=config.CUSTOMER_TOKEN_MAX_SUBMISSIONS,
    )
    if accepted_count is None:
        return _refuse(
            audit,
            token,
            SignalRejection.TOKEN_SUBMISSION_LIMIT_REACHED,
            detail=None,
            event_type=CUSTOMER_SUBMISSION_LIMIT_REACHED,
            kind=submission.kind,
            correlation_id=correlation_id,
            moment=instant,
            remaining=0,
        )

    stored_note, truncated = _note_for_storage(
        submission.submitted_note, effective_note_limit(config)
    )
    reason = submission.submitted_delay_reason
    row = signals.insert(
        token.merchant_id,
        values={
            "merchant_id": token.merchant_id,
            "case_id": token.case_id,
            "token_id": token.token_id,
            "kind": submission.kind.value,
            "delay_reason": None if reason is None else reason.value,
            "delay_reason_note": stored_note,
            "note_truncated": truncated,
            # The case's provenance, propagated verbatim rather than left to the column's
            # ``REAL`` default. Exactly what ``revora.memory.store`` does with the same value
            # and for the same reason: a signal is evidence *about* a case, so it cannot be
            # more real than the case it is about. Without this a generated batch driven
            # through the real customer surface would leave ``REAL``-provenance signal rows
            # behind, which R28.C16 forbids and which would make one synthetic row indis-
            # tinguishable from a customer's actual words.
            "provenance": str(case.provenance),
            "submitted_at": instant,
            "correlation_id": correlation_id,
        },
    )

    # 5b. The Promise_To_Pay (R23.C1, C3, C5, C6, C7). Between the signal insert and the audit
    #     records, because ``customer_signal_id`` is a NOT NULL foreign key to the row just
    #     flushed and because the audit write has to stay last for its rollback to still undo
    #     everything staged. ``None`` for the other two shapes, which is most writes.
    promise: PromiseOutcome | None = None
    if isinstance(submission, PromiseSubmission):
        promise = record_promise(
            session,
            token.merchant_id,
            case,
            config,
            signal_id=row.id,
            promise_date=submission.promise_date,
            # R23.C1. What arrived, not what it parsed to — see PromiseSubmission.
            received_representation=submission.received_representation,
            correlation_id=correlation_id,
            moment=instant,
        )

    # 6. The hard stop's suppression (R21.C1, R21.C10). Between the signal insert and the
    #    audit record, because it names the signal's id and because the audit record has to
    #    stay last for its rollback to still undo everything staged. ``None`` for every reason
    #    that is a payment problem, which is the common path and costs one dictionary lookup.
    suppression: SuppressionOutcome | None = None
    stop = hard_stop_for(reason)
    if stop is not None:
        suppression = record_hard_stop(
            session,
            token.merchant_id,
            case,
            signal_id=row.id,
            hard_stop_reason=stop,
            correlation_id=correlation_id,
            moment=instant,
        )

    # 7. The arrangement's escalation (R22.C2). A queue write and nothing else: no transition,
    #    no cancellation, no provider call, and no money field touched anywhere on this path.
    #    Deliberately *not* conditional on the case's state — unlike the review below, which fires
    #    only from POLICY_CHECK. A customer asking to pay differently needs a person whatever the
    #    pipeline happens to be doing, and a case mid-execution is exactly the one where waiting
    #    for it to come to rest would mean the next automated message went out first.
    arrangement_enqueued = isinstance(submission, PartialArrangementSubmission)
    if arrangement_enqueued:
        enqueue_arrangement_consequence(
            session,
            token.merchant_id,
            token.case_id,
            signal_id=row.id,
            correlation_id=correlation_id,
            moment=instant,
        )

    review_enqueued = False
    if state is CaseState.POLICY_CHECK:
        review_enqueued = (
            enqueue_case_review(
                session,
                token.merchant_id,
                token.case_id,
                trigger=ReviewTrigger.CUSTOMER_SIGNAL,
                correlation_id=correlation_id,
            )
            is not None
        )

    decision: dict[str, object] = {
        "kind": submission.kind.value,
        "signal_id": str(row.id),
        "case_id": str(token.case_id),
        "token_id": token.token_id,
        "submitted_at": instant.isoformat(),
        "values": dict(submission.recorded_values()),
        "note_truncated": truncated,
        "accepted_submission_count": accepted_count,
        "case_state": state.value,
        "review_enqueued": review_enqueued,
        # R22.C2's consequence, recorded as *queued*. Named in the record that accepted the
        # signal rather than in one of its own, on the same terms the suppression is: one
        # occurrence gets one audited record, and the escalation's own CASE_ESCALATED record is
        # written by the handler that applies it.
        "arrangement_escalation_enqueued": arrangement_enqueued,
    }
    if promise is not None:
        # The clamp, in the record that accepted the submission, so a reader of *this* record
        # knows a promise was attempted whether or not one was persisted. The full clamp fields
        # travel on the promise's own record below, which is what R23.C8 asks for.
        decision["promise"] = {
            "recorded": not promise.refused,
            "status": None if promise.refused else promise.plan.status.value,
        }
    if suppression is not None:
        # The suppression travels on this record rather than in a record of its own, on the same
        # terms the revoked-token count travels on ``STATE_TRANSITION``: a second record would be
        # a second audited occurrence for one occurrence, and the signal is already the reason
        # the suppression exists. Absent when the reason was not a hard stop, so the field's
        # presence means contact ended rather than meaning the field was not computed.
        decision["contact_suppression"] = _suppression_fields(suppression)

    if promise is not None:
        # 8b. R23.C8 for the persisted path, R23.C7 for the refused one. Immediately before the
        #     last record, so a failure in either still rolls back the promise row and the signal.
        _write_promise_record(
            audit,
            token,
            promise,
            promise_date=ensure_utc(submission.promise_date)
            if isinstance(submission, PromiseSubmission)
            else instant,
            correlation_id=correlation_id,
            moment=instant,
        )

    audit.write_for_case(
        token.merchant_id,
        token.case_id,
        AuditEntry(
            event_type=CUSTOMER_SIGNAL_RECORDED,
            # R29.C9: the token identifier in the actor field, because no Merchant_User
            # initiated this. The handle only — the secret has no reversible representation.
            actor=token.token_id,
            decision=decision,
        ),
        correlation_id=correlation_id,
        occurred_at=instant,
    )

    _logger.info(
        "customer signal recorded",
        case_id=str(token.case_id),
        token_id=token.token_id,
        kind=submission.kind.value,
        review_enqueued=review_enqueued,
        contact_suppressed=suppression is not None,
        arrangement_enqueued=arrangement_enqueued,
        promise_recorded=promise is not None and not promise.refused,
    )
    return SignalOutcome(
        signal_id=row.id,
        # R23.C7's 409, and the one place a rejection travels beside a persisted signal. Set from
        # the promise's own outcome rather than from a second condition here, so "the case already
        # holds its limit" is decided once, in the module that owns the bound.
        rejection=(
            SignalRejection.PROMISE_ALREADY_RECORDED
            if promise is not None and promise.refused
            else None
        ),
        suppression=suppression,
        arrangement_enqueued=arrangement_enqueued,
        review_enqueued=review_enqueued,
        promise=promise,
        signals_remaining=_remaining(config, accepted_count),
    )


def _write_promise_record(
    audit: AuditWriter,
    token: VerifiedToken,
    promise: PromiseOutcome,
    *,
    promise_date: datetime,
    correlation_id: uuid.UUID,
    moment: datetime,
) -> None:
    """``PROMISE_RECORDED`` or ``PROMISE_ALREADY_RECORDED``, for one promise submission.

    **R23.C8's purpose is the field list, so the field list is the function.** A reader must be
    able to check the clamp from the audit trail alone, which means the Promise_Date, the computed
    Follow_Up_Instant and the Recovery_Window end have to be in one record — a reader who had to
    join to ``recovery_case`` could not check a clamp against a window end that had moved, and a
    reader of this record can, because the window end travels as the snapshot the clamp used.

    ``clamped`` is recorded beside them rather than left to be inferred from the three. Inferring
    it means recomputing ``promise_date + PROMISE_FOLLOW_UP_OFFSET``, which is applying a
    configured bound at read time to a record written under a possibly different one — so the
    inference would be wrong exactly when the bound had changed, which is the case an audit reader
    is most likely to be investigating.

    The actor is the ``token_id`` (R23.C8, R29.C9), the handle only. The refused record carries no
    Follow_Up_Instant, because none was persisted; it carries the plan's status anyway, so an
    operator can see what the refused promise *would* have been — which is the question "should we
    raise the bound" resolves to.
    """
    if promise.refused:
        audit.write_for_case(
            token.merchant_id,
            token.case_id,
            AuditEntry(
                event_type=PROMISE_ALREADY_RECORDED,
                actor=token.token_id,
                decision={
                    "case_id": str(token.case_id),
                    "token_id": token.token_id,
                    "outcome": SignalRejection.PROMISE_ALREADY_RECORDED.value,
                    # The date is recorded here and nowhere else on this path, and it is a value
                    # the customer supplied — admitted because it is a parsed instant rather than
                    # free text, so it cannot carry anything but a timestamp.
                    "promise_date": promise_date.isoformat(),
                    "would_have_been": promise.plan.status.value,
                    # Named so the record says what did *not* change. R23.C7 is explicit that the
                    # persisted promise, its status and its follow-up are untouched, and an audit
                    # trail that only recorded actions taken could not answer that at all.
                    "detail": (
                        "the case already holds its configured promise limit; the persisted "
                        "Promise_To_Pay, its status and its follow-up instant are unchanged, and "
                        "the submission is retained as a Customer_Signal"
                    ),
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )
        return

    plan = promise.plan
    audit.write_for_case(
        token.merchant_id,
        token.case_id,
        AuditEntry(
            event_type=PROMISE_RECORDED,
            actor=token.token_id,
            decision={
                "case_id": str(token.case_id),
                "token_id": token.token_id,
                "promise_id": str(promise.promise_id),
                "status": plan.status.value,
                # The three fields R23.C8 names, plus the flag that says which of the first two
                # decided the third.
                "promise_date": promise_date.isoformat(),
                "follow_up_at": (
                    None if plan.follow_up_at is None else plan.follow_up_at.isoformat()
                ),
                "window_end_at": plan.window_end_at.isoformat(),
                "clamped": plan.clamped,
                # A *queued* escalation, never an applied one. The case is still at whatever state
                # it was in when this record was written (R19.C9).
                "escalation_enqueued": promise.escalation_enqueued,
                "recorded_at": moment.isoformat(),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _suppression_fields(suppression: SuppressionOutcome) -> dict[str, object]:
    """What the audit record says about a suppression. No scope preimage, by construction.

    The ``scope_key`` is a digest, so recording it discloses no customer identifier while still
    letting an operator join this record to the ``contact_suppression`` row and to every other
    case in the same scope. ``created`` distinguishes the first hard stop from a repeat, which
    is the question "why is there no new suppression row for this signal" resolves to.
    """
    return {
        "scope_key": suppression.scope_key,
        "hard_stop_reason": suppression.hard_stop_reason.value,
        "suppression_id": (
            None if suppression.suppression_id is None else str(suppression.suppression_id)
        ),
        "created": suppression.created,
        "customer_tokens_revoked": suppression.tokens_revoked,
        "suppressed_at": suppression.suppressed_at.isoformat(),
    }


def _remaining(config: Configuration, accepted: int) -> int:
    """How many further signals the token may write. Never negative."""
    return max(0, int(config.CUSTOMER_TOKEN_MAX_SUBMISSIONS) - int(accepted))


def _refuse(
    audit: AuditWriter,
    token: VerifiedToken,
    rejection: SignalRejection,
    *,
    detail: str | None,
    event_type: str,
    kind: CustomerSignalKind,
    correlation_id: uuid.UUID,
    moment: datetime,
    remaining: int,
) -> SignalOutcome:
    """One audit record for a refused write, and no other write at all (R29.C9).

    Attached to the case, unlike the token module's rejection records: a refused *signal* has
    already established that the caller may know this case exists, because the token verified.
    The four token rejections have not, which is why those are unattached.

    Every refusal is recorded, not only the interesting ones. R29.C9 says *accepts or rejects* —
    one record per write either way — and the reason that matters is that a customer complaining
    they cannot submit anything is answerable only if the refusals are in the log beside the
    acceptances.
    """
    audit.write_for_case(
        token.merchant_id,
        token.case_id,
        AuditEntry(
            event_type=event_type,
            actor=token.token_id,
            decision={
                "kind": kind.value,
                "case_id": str(token.case_id),
                "token_id": token.token_id,
                "outcome": rejection.value,
                # A field name or a Terminal_State. Never a submitted value: this endpoint is
                # reachable without a session, so everything in the body is attacker-supplied
                # text and the audit log is the one store that cannot be rewritten.
                "detail": detail,
                "request_at": moment.isoformat(),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )
    return SignalOutcome(
        rejection=rejection, detail=detail, signals_remaining=remaining
    )

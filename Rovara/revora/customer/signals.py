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

**Two caps, both under the row lock, and they are not the same cap.**
``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` bounds one credential; ``MAX_CUSTOMER_SIGNALS_PER_CASE``
bounds one case. Today both default to 5, and they still both have to exist: a case outlives a
token — a terminal-state revocation followed by a further approved action mints a second one —
so a customer can reach either bound with room left under the other. Both answer 429, and
**reads keep being served** either way (R18.C9): a customer who has explained themselves five
times must not lose the page telling them what they owe as a consequence.

**What this module deliberately does not do.** It does not write ``promise_to_pay`` or compute
a Follow_Up_Instant (task 44), and it does
not *apply* the Delay_Reason mapping table it declares — :data:`DELAY_REASON_CAUSE` is data, and
``revora.diagnosis`` is what reads it, on the next decision cycle rather than in this request
(R20.C4). A hard stop and a promise are *recorded here as signals* and their consequences are
applied by the tasks that own them. The one guard on a
promise date that lives here is the degenerate one — a date at or before the submission instant
is not a promise, and it is refused without reference to any configured bound, because
``promise_date > recorded_at`` is a schema fact rather than a policy. The
``PROMISE_MIN_LEAD_TIME`` rejection of R23.C2 belongs with the row it protects.

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
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from revora.audit.events import (
    CUSTOMER_SIGNAL_LIMIT_REACHED,
    CUSTOMER_SIGNAL_RECORDED,
    CUSTOMER_SIGNAL_REJECTED,
    CUSTOMER_SUBMISSION_LIMIT_REACHED,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.cases.review import enqueue_case_review
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
    the system can hold rather than a promise it declines to accept. The
    ``PROMISE_MIN_LEAD_TIME`` rejection belongs with the row it protects (task 44)."""


SIGNAL_REJECTION_STATUS: Final[Mapping[SignalRejection, int]] = {
    SignalRejection.CASE_TERMINAL: 409,
    SignalRejection.CASE_SIGNAL_LIMIT_REACHED: 429,
    SignalRejection.TOKEN_SUBMISSION_LIMIT_REACHED: 429,
    SignalRejection.PROMISE_DATE_NOT_IN_FUTURE: 422,
}
"""The design's rejection table for the write path, as data the router reads.

Data rather than a branch the router repeats, for the same reason
``tokens.REJECTION_STATUS`` is: two places that decide a status code are two places that can
disagree, and the one this endpoint would disagree in is the one nobody is authenticated to
notice."""


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """Either a persisted signal or a named refusal. Never both, never neither."""

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

    review_enqueued: bool = False
    """Whether this write enqueued a decision cycle (R30.C8).

    ``False`` is the common answer and is not a failure: the review trigger fires only for a
    case resting at ``POLICY_CHECK``, and a case waiting on an outcome has a cycle of its own
    already in flight."""

    signals_remaining: int = 0

    @property
    def accepted(self) -> bool:
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
    6. **Write the Contact_Suppression and revoke the case's tokens**, where the submitted
       Delay_Reason is a Hard_Stop_Reason (R21.C1, R21.C10). After the insert because
       ``contact_suppression.customer_signal_id`` is a ``NOT NULL`` foreign key to the row step
       5 just flushed, and before the audit record for the same reason every other write is: the
       audit write is the one that must be able to roll all of it back.
    7. **Enqueue the review** when the case is at ``POLICY_CHECK`` (R30.C8), through the same
       :func:`~revora.cases.review.enqueue_case_review` the sweeper and the detection service
       use. Sharing the entry point is what makes R30.C9's "irrespective of the Review_Trigger"
       hold through one dedupe key rather than three.
    8. **Write the audit record, last.** So a failure here rolls back everything above it.

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

    if isinstance(submission, PromiseSubmission) and ensure_utc(
        submission.promise_date
    ) <= instant:
        return _refuse(
            audit,
            token,
            SignalRejection.PROMISE_DATE_NOT_IN_FUTURE,
            detail="promise_date",
            event_type=CUSTOMER_SIGNAL_REJECTED,
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
            "submitted_at": instant,
            "correlation_id": correlation_id,
        },
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
    }
    if suppression is not None:
        # The suppression travels on this record rather than in a record of its own, on the same
        # terms the revoked-token count travels on ``STATE_TRANSITION``: a second record would be
        # a second audited occurrence for one occurrence, and the signal is already the reason
        # the suppression exists. Absent when the reason was not a hard stop, so the field's
        # presence means contact ended rather than meaning the field was not computed.
        decision["contact_suppression"] = _suppression_fields(suppression)

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
    )
    return SignalOutcome(
        signal_id=row.id,
        suppression=suppression,
        review_enqueued=review_enqueued,
        signals_remaining=_remaining(config, accepted_count),
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

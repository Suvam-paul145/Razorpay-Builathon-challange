"""How a value crosses the HTTP boundary. Three rules, each preventing a specific lie.

**Money is formatted here and nowhere else (R14.C12).** Every currency figure leaves as *both* a
pre-formatted string and its raw minor units. The string is what a client renders; the integer is
there for sorting, for a CSV export and for anybody who needs to check the string. Shipping the
formatted value is what makes client-side arithmetic take deliberate effort rather than being the
path of least resistance — a browser handed ``250000`` will eventually divide it by a hundred in
one place and not in another, and the two numbers will disagree on the same screen.

**A rate is a string, and ``UNDEFINED`` is one of its values.** Rates arrive from the metrics
engine as ``Decimal | str`` because a rate with a zero denominator does not exist. Emitting them as
JSON numbers would force the sentinel to be something else — ``null``, which formats as an empty
cell, or ``0``, which is a false measurement. So both go out as strings and the consumer has to
look.

**An absent value says what is absent and why, never zero.** Two markers, and they are different
claims. :func:`not_yet_recorded` means the pipeline has not reached this yet and names the case
state it is in, so a reader can tell "no policy decision" from "blocked by policy".
:func:`data_unavailable` means a figure was asked for and could not be computed in time, and it
applies to that figure alone — the rest of a metrics response still returns, with its own
timestamps. Substituting zero for either is not a display bug; it is a false financial statement.

**A customer's own words are labelled here, and escaped here (R20.C12, R29.C11).** A
Delay_Reason_Note is the only string in the system that a stranger typed on an endpoint reachable
without a session, and it is the only one that leaves with a label saying so. :func:`escape_markup`
and :func:`customer_supplied_note` are the two halves — see the note document's own docstring for
why the wire carries the verbatim text *and* its escaped form rather than one of the two.

**A human-readable label is chosen here, never in the browser (R26.C14).** ``DO_NOTHING`` and
``WAIT`` share one label because they are the same thing to a merchant — the case is being watched,
not abandoned — while the three Terminal_States get three distinct labels because they are three
different problems. A client that composed the shared label from two enum values would be one
`if` away from rendering restraint as an ending, and it would be a second vocabulary that could
disagree with this one. So the tables are here and the wire carries the label *and* the stored
member, which is the rest of what R26.C14 asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, HardStopReason
from revora.domain.money import (
    INDIAN_GROUPING,
    WESTERN_GROUPING,
    Minor,
    format_minor,
)

__all__ = [
    "CASE_STATE_LABELS",
    "CURRENCY_SYMBOLS",
    "DATA_UNAVAILABLE",
    "HARD_STOP_LABELS",
    "MARKUP_ESCAPES",
    "NOTE_REDACTED",
    "NOT_YET_RECORDED",
    "PRESENT",
    "RENDER_AS_TEXT_ONLY",
    "SELECTED_ACTION_LABELS",
    "UNVERIFIED_CUSTOMER_TEXT",
    "WAITING_AND_WATCHING",
    "MoneyField",
    "case_state_label",
    "customer_supplied_note",
    "data_unavailable",
    "escape_markup",
    "grouping_for",
    "money",
    "not_yet_recorded",
    "rate",
    "selected_action_label",
    "symbol_for",
]

NOT_YET_RECORDED: Final[str] = "NOT_YET_RECORDED"
"""The pipeline has not produced this yet. Carries the case state so the absence is explicable."""

DATA_UNAVAILABLE: Final[str] = "DATA_UNAVAILABLE"
"""This figure could not be computed. Applies to one figure, not to the whole response."""

PRESENT: Final[str] = "PRESENT"
"""A real value. Named so a client can branch on one field rather than on ``is None``."""

CURRENCY_SYMBOLS: Final[dict[str, str]] = dict(
    MappingProxyType({"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "د.إ"})
)
"""Symbols for the currencies this deployment could plausibly see.

Deliberately small and deliberately not a locale library. :func:`symbol_for` falls back to the
ISO code itself, so an unknown currency renders as ``USD 1,200.00`` rather than as a wrong symbol
— an unfamiliar-looking amount is a much smaller problem than an amount labelled with the wrong
country's money."""

_MINOR_DIGITS: Final[dict[str, int]] = {"INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2}
"""Minor-unit digits per currency.

Every currency here happens to use two, and the mapping exists anyway because some do not — JPY
has none and KWD has three. Hard-coding two would make a future JPY amount render a hundred times
too small, which is the kind of bug that looks like a data problem."""

_DEFAULT_MINOR_DIGITS: Final[int] = 2

_INDIAN_GROUPED_CURRENCIES: Final[frozenset[str]] = frozenset({"INR"})
"""Currencies rendered with lakh and crore grouping.

A set of one, and it is a set rather than an ``== "INR"`` because the grouping convention belongs to
a region rather than to a currency code — Pakistani, Bangladeshi and Nepali currency use it too, and
a deployment that added one of them would add it here rather than editing a condition."""


def symbol_for(currency: str) -> str:
    """The display symbol for an ISO currency code, or the code itself."""
    code = currency.strip().upper()
    return CURRENCY_SYMBOLS.get(code, f"{code} ")


def grouping_for(currency: str) -> str:
    """Lakh-and-crore grouping for rupees, thousands for everything else.

    Selected from the currency rather than from a configured locale, because the grouping is a
    property of the money being shown and not of who is looking at it. A merchant billing in INR and
    reading the dashboard from Berlin still wants ``₹12,34,567.89``.
    """
    return (
        INDIAN_GROUPING
        if currency.strip().upper() in _INDIAN_GROUPED_CURRENCIES
        else WESTERN_GROUPING
    )


@dataclass(frozen=True, slots=True)
class MoneyField:
    """One currency figure, formatted server-side, with its integer beside it."""

    minor: int
    currency: str

    @property
    def formatted(self) -> str:
        code = self.currency.strip().upper()
        return format_minor(
            Minor(self.minor),
            symbol=symbol_for(code),
            minor_digits=_MINOR_DIGITS.get(code, _DEFAULT_MINOR_DIGITS),
            grouping=grouping_for(code),
        )

    def as_document(self) -> dict[str, object]:
        return {
            "status": PRESENT,
            "minor": self.minor,
            "currency": self.currency.strip().upper(),
            "formatted": self.formatted,
        }


def money(
    value: int | None, *, currency: str, absent_state: str | None = None
) -> dict[str, object]:
    """A currency figure for the wire, or a not-yet-recorded marker.

    Args:
        value: minor units, or ``None`` when the figure does not exist yet.
        absent_state: the case state to name when ``value`` is ``None``. Required in practice —
            an absent amount with no explanation is exactly the cell somebody fills with a zero.
    """
    if value is None:
        return not_yet_recorded(absent_state or "UNKNOWN", "amount")
    return MoneyField(minor=int(value), currency=currency).as_document()


def rate(value: Decimal | str | None, *, absent_state: str | None = None) -> dict[str, object]:
    """A rate or probability for the wire, always as a string.

    ``UNDEFINED`` arrives here as the string it already is and passes through unchanged, so a
    zero-denominator rate is visibly not a measurement. ``None`` — which means the figure was
    never computed rather than computed as undefined — becomes a not-yet-recorded marker instead.
    """
    if value is None:
        return not_yet_recorded(absent_state or "UNKNOWN", "rate")
    return {"status": PRESENT, "value": str(value)}


def not_yet_recorded(case_state: str, what: str) -> dict[str, object]:
    """R14.C15. Names the case state, because the state is the explanation.

    "No recommendation" on a ``DETECTED`` case means the pipeline has not got there; on a
    ``BLOCKED`` case it means policy stopped it. Same empty cell, opposite meanings, and a reader
    who cannot tell them apart will read the first as a bug and the second as working.
    """
    return {
        "status": NOT_YET_RECORDED,
        "case_state": case_state,
        "detail": f"no {what} has been recorded yet; the case is {case_state}",
    }


def data_unavailable(figure: str, reason: str) -> dict[str, object]:
    """R14.C16. Names the figure, and only this figure.

    A metrics response degrades one figure at a time. Returning an error for the whole response
    because one aggregate timed out would hide the figures that did compute, and their timestamps
    are what make them usable.
    """
    return {"status": DATA_UNAVAILABLE, "figure": figure, "detail": reason}


# ---------------------------------------------------------------------------
# A customer's own words (R20.C12, R29.C11)
# ---------------------------------------------------------------------------

UNVERIFIED_CUSTOMER_TEXT: Final[str] = "customer-supplied unverified text"
"""R20.C12's label, verbatim, chosen here for the reason every other label is.

The requirement says the note is *presented marked as* customer-supplied unverified text, and the
mark is a fact about the data rather than a styling choice — so it travels on the wire beside the
text it describes and the browser renders what it is given. A client that composed this phrase
itself would be a second vocabulary that could disagree with this one, and the disagreement would
be about whether a stranger's assertion is a finding."""

RENDER_AS_TEXT_ONLY: Final[str] = "TEXT_ONLY"
"""How this string may be rendered, stated on the wire (R29.C11).

Named rather than implied by the field's name, so a surface added later has to read a value that
says *text* before it decides how to render. The one thing R29.C11 forbids is the note being
executed as markup or as script, and a field that only *implied* its own inertness is a field
somebody eventually interpolates."""

NOTE_REDACTED: Final[str] = "REDACTED"
"""The note existed and the retention sweep removed it (R29.C10).

A third status beside ``PRESENT`` and the absence markers, because it is a third fact. "This
customer wrote nothing" and "this customer wrote something we are no longer permitted to hold" are
different histories, and a redaction rendered as an absence would make a retention action look
like a customer who stayed silent."""

MARKUP_ESCAPES: Final[dict[str, str]] = dict(
    MappingProxyType(
        {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#x27;",
        }
    )
)
"""Every markup-significant character, and what it becomes (R29.C11).

Five, and the set is closed on purpose. ``<`` and ``>`` open and close a tag; ``&`` starts an
entity and is why the substitution order below matters; ``"`` and ``'`` close an attribute value,
which is the injection an escape that handled only the angle brackets would let through.

**Ampersand first, and this is the whole correctness argument.** Substituting ``<`` before ``&``
turns ``<`` into ``&lt;`` and then that ampersand into ``&amp;lt;``, so the note displays as
``&lt;`` rather than as ``<``. ``dict`` preserves insertion order and ``&`` is declared first, so
:func:`escape_markup` iterating this mapping is correct by the mapping's order rather than by a
comment asking the reader to keep it that way.

Nothing else is touched. A control character, a line separator, a right-to-left override are all
retained: escaping them would be deriving a judgement about the note's contents, which R20.C3
forbids, and none of them is markup."""


def escape_markup(text: str) -> str:
    """Every markup-significant character replaced by its entity. Nothing else changed.

    A local table rather than :func:`html.escape`, for one reason worth the six lines: the
    stdlib's ``quote=True`` escapes ``"`` and leaves ``'`` alone unless you read the source to
    find out, and the set of characters this function handles is a claim R29.C11 makes rather
    than an implementation detail to inherit. :data:`MARKUP_ESCAPES` is that claim, reviewable,
    and a test asserts the function handles every member of it.

    Length is not bounded here and deliberately: the escaped form of a note at
    ``DELAY_NOTE_MAX_LENGTH`` can be several times longer, and truncating *after* escaping is how
    a trailing ``&amp;`` becomes ``&am`` — a broken entity, which is a rendering bug in the one
    place the requirement is about. The stored length is bounded on the way in
    (``revora.customer.signals.effective_note_limit``); this is presentation only.

    Idempotent in the sense that matters and not in the sense that would be wrong: escaping twice
    yields ``&amp;lt;``, because the first pass produced real ampersands and a second pass has no
    way to know they were once escapes. Which is why this is applied at exactly one point, in
    :func:`customer_supplied_note`, and never composed.
    """
    escaped = text
    for character, entity in MARKUP_ESCAPES.items():
        escaped = escaped.replace(character, entity)
    return escaped


def customer_supplied_note(
    note: str | None,
    *,
    truncated: bool,
    redacted_at: str | None = None,
) -> dict[str, object] | None:
    """One Delay_Reason_Note for the wire, labelled and escaped. ``None`` where there is none.

    ``None`` rather than a marker for the absent case, unlike every other absence in this module.
    An absent figure needs a marker because the alternative is a zero somebody reads as a
    measurement; an absent note has no such failure mode — most signals carry none, and
    ``not_yet_recorded`` would claim the pipeline had not got there yet, which is false. A
    *redacted* note is the case that does need naming, and :data:`NOTE_REDACTED` names it.

    **The document carries the verbatim text and its escaped form, and both are deliberate.**
    ``text`` is what a client rendering a text node uses — React escapes on render, so shipping
    only the escaped form would display a customer who typed ``I <3 this`` as ``I &lt;3 this``,
    which is a legibility defect in the one field whose whole purpose is that a merchant can read
    what the customer actually wrote. ``text_escaped`` is what any surface interpolating into
    markup uses, and shipping it is R29.C11 performed by the server rather than delegated to
    every consumer.

    Two copies of the same untrusted string is exactly the "two places that can disagree" shape
    this codebase argues against elsewhere, so the reason it is safe here is worth stating: one is
    a pure function of the other, computed at this single point, at the same instant, from the
    same value. They cannot disagree about content. What they differ in is which rendering
    context they are safe in, and naming that difference on the wire is what stops a consumer
    guessing.

    Args:
        note: the stored note, or ``None``.
        truncated: ``customer_signal.note_truncated`` — whether R20.C2's length truncation
            applied. Surfaced because a note cut short at 500 characters reads as a customer who
            stopped mid-sentence, and a merchant deciding whether to phone them should know which
            it was.
        redacted_at: ISO-8601 instant the retention sweep removed the note, where it did.
    """
    if redacted_at is not None:
        return {
            "status": NOTE_REDACTED,
            "label": UNVERIFIED_CUSTOMER_TEXT,
            "verified": False,
            "render_as": RENDER_AS_TEXT_ONLY,
            "redacted_at": redacted_at,
            "detail": (
                "this customer wrote a note and it has passed CUSTOMER_DATA_RETENTION, so it "
                "was deleted. The signal itself is retained."
            ),
        }
    if note is None:
        return None
    return {
        "status": PRESENT,
        "label": UNVERIFIED_CUSTOMER_TEXT,
        # A boolean beside the label, because the label is prose and this is what a client
        # branches on. R20.C12 is not satisfied by a phrase a consumer might not render.
        "verified": False,
        "render_as": RENDER_AS_TEXT_ONLY,
        "text": note,
        "text_escaped": escape_markup(note),
        "truncated": truncated,
        "redacted_at": None,
    }


# ---------------------------------------------------------------------------
# Labels (R26.C14, R30.C13). Chosen here, rendered verbatim, member kept alongside.
# ---------------------------------------------------------------------------

WAITING_AND_WATCHING: Final[str] = "Waiting and watching"
"""The one label ``DO_NOTHING`` and ``WAIT`` share (R26.C14).

They are two distinct recorded selections and R30.C12 keeps them distinct in storage — the *stored
member* travels beside this label for exactly that reason. But to a merchant they are one situation:
Revora looked, decided this customer is better left alone for now, and will look again. A separate
label for each would invite the reader to guess at a difference that has no operational consequence,
and the far worse alternative — the one this label exists to displace — is either of them reading as
"nothing is happening"."""

_HUMAN_LABEL_UNKNOWN: Final[str] = "Unlabelled"


def _sentence(member: str) -> str:
    """``PAYMENT_LINK`` -> ``Payment link``. The default when a member needs no special wording."""
    lower = member.replace("_", " ").lower()
    return lower[:1].upper() + lower[1:]


SELECTED_ACTION_LABELS: Final[dict[str, str]] = dict(
    MappingProxyType(
        {
            CandidateAction.DO_NOTHING.value: WAITING_AND_WATCHING,
            CandidateAction.WAIT.value: WAITING_AND_WATCHING,
            CandidateAction.RETRY.value: "Retry the charge",
            CandidateAction.DELAYED_RETRY.value: "Retry the charge later",
            CandidateAction.PAYMENT_LINK.value: "Send a payment link",
            CandidateAction.CUSTOMER_MESSAGE.value: "Message the customer",
            CandidateAction.PAYMENT_METHOD_UPDATE.value: "Ask for a different card",
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value: "Follow up on a promise to pay",
            CandidateAction.HUMAN_ESCALATION.value: "Hand to a person",
        }
    )
)
"""Every ``CandidateAction``, labelled. Total on purpose, and a test asserts the totality.

Total rather than defaulted so that adding a candidate action forces a decision about how it reads
to a merchant, in the same commit. A ``.get`` with a fallback would let a new action ship under a
machine-generated label nobody chose."""

CASE_STATE_LABELS: Final[dict[str, str]] = dict(
    MappingProxyType(
        {
            CaseState.NEW.value: "Just arrived",
            CaseState.DETECTED.value: "Failure detected",
            CaseState.DIAGNOSED.value: "Cause identified",
            CaseState.DECISION_PENDING.value: "Deciding",
            CaseState.POLICY_CHECK.value: "Decision recorded",
            CaseState.ACTION_SCHEDULED.value: "Action authorised",
            CaseState.EXECUTING.value: "Acting now",
            CaseState.WAITING_FOR_OUTCOME.value: "Waiting for the payment",
            CaseState.RECOVERED.value: "Recovered",
            CaseState.STOPPED.value: "Stopped trying",
            CaseState.BLOCKED.value: "Blocked by policy",
            CaseState.EXPIRED.value: "Recovery window closed",
            CaseState.ESCALATED.value: "With a person",
            CaseState.FAILED.value: "Failed",
        }
    )
)
"""Every ``CaseState``, labelled, with the three R26.C14 names pairwise distinct.

``STOPPED``, ``BLOCKED`` and ``EXPIRED`` are the same money and three different problems, so
R26.C14 requires three distinct labels and a test asserts they stay distinct. ``POLICY_CHECK`` reads
as *decision recorded* rather than as anything ending, which is R30.C13's half of the same claim:
the state a case rests in when it chose restraint must not be worded as a conclusion."""


HARD_STOP_LABELS: Final[dict[str, str]] = dict(
    MappingProxyType(
        {
            HardStopReason.DISPUTES_THE_CHARGE.value: "Disputes the charge",
            HardStopReason.NO_LONGER_WANTS_THE_ORDER.value: "No longer wants the order",
        }
    )
)
"""The two Hard_Stop_Reasons, labelled for the ``ESCALATED`` grouping (R21.C11).

Two labels rather than one "objected to the charge", because the two need different work from
different people: a dispute is a possible chargeback and a cancellation is a fulfilment and refund
question. This grouping is somebody's queue, and the label is what they triage on.

Phrased in the present tense and in the customer's voice — *disputes*, *no longer wants* — matching
``TERMINAL_REASON_LABELS``' four customer-stated endings. Every other label on this screen describes
something Revora concluded; these two describe something a person said, and a label reading
"contact suppressed" would name the consequence rather than the statement."""

_unlabelled_hard_stops = sorted(
    reason.value for reason in HardStopReason if reason.value not in HARD_STOP_LABELS
)
if _unlabelled_hard_stops:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        f"HARD_STOP_LABELS is missing {_unlabelled_hard_stops}; a Hard_Stop_Reason must not "
        "reach a merchant under a machine-generated label nobody chose"
    )


def selected_action_label(action: str) -> str:
    """The human label for a recorded Candidate_Action. ``DO_NOTHING`` and ``WAIT`` share one."""
    return SELECTED_ACTION_LABELS.get(action, _sentence(action) or _HUMAN_LABEL_UNKNOWN)


def case_state_label(state: str) -> str:
    """The human label for a Recovery_Case state. Each Terminal_State gets its own."""
    return CASE_STATE_LABELS.get(state, _sentence(state) or _HUMAN_LABEL_UNKNOWN)

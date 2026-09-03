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
    "NOT_YET_RECORDED",
    "PRESENT",
    "SELECTED_ACTION_LABELS",
    "WAITING_AND_WATCHING",
    "MoneyField",
    "case_state_label",
    "data_unavailable",
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

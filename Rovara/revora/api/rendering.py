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
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from revora.domain.money import (
    INDIAN_GROUPING,
    WESTERN_GROUPING,
    Minor,
    format_minor,
)

__all__ = [
    "CURRENCY_SYMBOLS",
    "DATA_UNAVAILABLE",
    "NOT_YET_RECORDED",
    "PRESENT",
    "MoneyField",
    "data_unavailable",
    "grouping_for",
    "money",
    "not_yet_recorded",
    "rate",
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

"""Exact decimal probabilities.

Three related quantities, each with a different range and a different number of
decimal places, all backed by ``Decimal`` rather than ``float``:

* :class:`Probability` — 0.0000 to 1.0000, four places. A recovery probability.
* :class:`SignedIncrement` — -1.0000 to 1.0000, four places. The difference
  between two probabilities, which can be negative when an action is estimated to
  make recovery less likely.
* :class:`Confidence` — 0.000 to 1.000, three places. How much a diagnosis is
  trusted.

Invariants are enforced at construction, so a value that exists is a value that
is in range. Requirement 7 fixes the decimal places; the database columns match.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = ["Confidence", "Probability", "SignedIncrement", "increment"]

_PROBABILITY_PLACES = Decimal("0.0001")
_CONFIDENCE_PLACES = Decimal("0.001")

_ZERO = Decimal(0)
_ONE = Decimal(1)
_NEG_ONE = Decimal(-1)


def _coerce(raw: Decimal | int | str, *, places: Decimal, label: str) -> Decimal:
    """Turn an accepted input into a quantized Decimal, refusing floats."""
    if isinstance(raw, float):  # pragma: no cover  — floats are not permitted, ever
        raise TypeError(
            f"{label} must not be built from a float; pass a Decimal, int or str "
            "so the value is exact"
        )
    try:
        value = Decimal(raw) if not isinstance(raw, Decimal) else raw
    except InvalidOperation as exc:
        raise ValueError(f"{label} could not be parsed as a decimal: {raw!r}") from exc
    return value.quantize(places, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True, order=True)
class Probability:
    """A recovery probability between 0 and 1 inclusive, held at four places."""

    value: Decimal

    def __post_init__(self) -> None:
        quantized = _coerce(self.value, places=_PROBABILITY_PLACES, label="Probability")
        if quantized < _ZERO or quantized > _ONE:
            raise ValueError(f"Probability must be between 0 and 1, got {quantized}")
        object.__setattr__(self, "value", quantized)

    @classmethod
    def of(cls, raw: Decimal | int | str) -> Probability:
        """Build from a Decimal, int or decimal string."""
        return cls(Decimal(raw) if not isinstance(raw, Decimal) else raw)

    def __str__(self) -> str:
        return f"{self.value}"


@dataclass(frozen=True, slots=True, order=True)
class SignedIncrement:
    """The difference between two probabilities: -1 to 1 inclusive, four places.

    Negatives are retained rather than clipped. An action estimated to reduce the
    chance of recovery should show as negative so the optimizer excludes it for a
    stated reason, not because the number was quietly flattened to zero.
    """

    value: Decimal

    def __post_init__(self) -> None:
        quantized = _coerce(self.value, places=_PROBABILITY_PLACES, label="SignedIncrement")
        if quantized < _NEG_ONE or quantized > _ONE:
            raise ValueError(f"SignedIncrement must be between -1 and 1, got {quantized}")
        object.__setattr__(self, "value", quantized)

    @classmethod
    def of(cls, raw: Decimal | int | str) -> SignedIncrement:
        return cls(Decimal(raw) if not isinstance(raw, Decimal) else raw)

    @property
    def is_positive(self) -> bool:
        return self.value > _ZERO

    def __str__(self) -> str:
        return f"{self.value}"


@dataclass(frozen=True, slots=True, order=True)
class Confidence:
    """Diagnosis confidence between 0 and 1 inclusive, held at three places.

    A confidence of exactly 1.000 is reserved for a deterministic mapping-table
    match. AI-assisted diagnosis is capped below that — see Requirement 3.
    """

    value: Decimal

    def __post_init__(self) -> None:
        quantized = _coerce(self.value, places=_CONFIDENCE_PLACES, label="Confidence")
        if quantized < _ZERO or quantized > _ONE:
            raise ValueError(f"Confidence must be between 0 and 1, got {quantized}")
        object.__setattr__(self, "value", quantized)

    @classmethod
    def of(cls, raw: Decimal | int | str) -> Confidence:
        return cls(Decimal(raw) if not isinstance(raw, Decimal) else raw)

    def __str__(self) -> str:
        return f"{self.value}"


CERTAIN: Confidence = Confidence(Decimal("1.000"))
"""Reserved for a deterministic mapping-table match."""

NO_CONFIDENCE: Confidence = Confidence(Decimal("0.000"))
"""Recorded when AI output was rejected or the layer was unavailable."""

AI_CONFIDENCE_CEILING: Confidence = Confidence(Decimal("0.990"))
"""AI-assisted diagnosis cannot claim more than this."""


def increment(
    intervention: Probability, baseline: Probability
) -> SignedIncrement:
    """Compute ``intervention - baseline``, keeping the sign.

    This is the first step of the value chain in Requirement 7. It is defined here
    rather than in the optimizer so the subtraction is exact by construction and
    the optimizer cannot accidentally do it in floating point.
    """
    return SignedIncrement(intervention.value - baseline.value)

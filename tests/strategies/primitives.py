"""Hypothesis strategies for the domain primitives.

Defined once and composed everywhere, so that every property test explores the
same boundary values rather than whatever the author of that test happened to
think of.

The boundaries here are not decoration. The design's Generators subsection names
them specifically: zero, one, either side of the detection minimum, either side of
the escalation threshold, and either side of each configured probability
threshold. Those are exactly the values where an off-by-one in a comparison becomes
a wrong decision about somebody's money, and a uniform random sample over a wide
range hits them approximately never.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import strategies as st

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, EstimationMethod, Provenance, RiskCause
from revora.domain.money import Minor
from revora.domain.probability import Confidence, Probability, SignedIncrement
from revora.domain.transitions import NON_TERMINAL_STATES, TERMINAL_STATES

__all__ = [
    "case_states",
    "confidences",
    "estimation_methods",
    "money",
    "non_terminal_states",
    "positive_money",
    "probabilities",
    "provenances",
    "risk_causes",
    "signed_increments",
    "state_pairs",
    "terminal_states",
]

# Placeholder configured bounds, mirrored from the design's Configurable Bounds
# table. They live here as literals rather than being imported from configuration
# because a strategy must not change behaviour when an operator retunes a
# threshold — a property test that explores different values on different machines
# is not a property test.
_MIN_DETECTION_AMOUNT = 100
_ESCALATION_AMOUNT_THRESHOLD = 50_000
_MIN_NET_VALUE_THRESHOLD = 5_000
_MIN_INCREMENTAL_PROBABILITY = Decimal("0.05")
_HIGH_BASELINE_THRESHOLD = Decimal("0.80")

_INTERESTING_AMOUNTS: tuple[int, ...] = (
    0,
    1,
    _MIN_DETECTION_AMOUNT - 1,
    _MIN_DETECTION_AMOUNT,
    _MIN_DETECTION_AMOUNT + 1,
    _MIN_NET_VALUE_THRESHOLD - 1,
    _MIN_NET_VALUE_THRESHOLD,
    _MIN_NET_VALUE_THRESHOLD + 1,
    _ESCALATION_AMOUNT_THRESHOLD - 1,
    _ESCALATION_AMOUNT_THRESHOLD,
    _ESCALATION_AMOUNT_THRESHOLD + 1,
    2_000_000,  # the design's worked example, 20,000 rupees in paise
    9_223_372_036_854_775_807 // 2,  # comfortably inside BIGINT, exercises width
)

_EPSILON = Decimal("0.0001")

_INTERESTING_PROBABILITIES: tuple[Decimal, ...] = (
    Decimal("0.0000"),
    _EPSILON,
    _MIN_INCREMENTAL_PROBABILITY - _EPSILON,
    _MIN_INCREMENTAL_PROBABILITY,
    _MIN_INCREMENTAL_PROBABILITY + _EPSILON,
    Decimal("0.2000"),
    Decimal("0.5000"),
    _HIGH_BASELINE_THRESHOLD - _EPSILON,
    _HIGH_BASELINE_THRESHOLD,
    _HIGH_BASELINE_THRESHOLD + _EPSILON,
    Decimal("1.0000") - _EPSILON,
    Decimal("1.0000"),
)


def money() -> st.SearchStrategy[Minor]:
    """Signed minor-unit amounts, weighted toward the interesting boundaries."""
    return st.one_of(
        st.sampled_from([Minor(value) for value in _INTERESTING_AMOUNTS]),
        st.integers(min_value=-10_000_000, max_value=10_000_000).map(Minor),
    )


def positive_money() -> st.SearchStrategy[Minor]:
    """Amounts a real payment could have: strictly greater than zero."""
    return st.one_of(
        st.sampled_from(
            [Minor(value) for value in _INTERESTING_AMOUNTS if value > 0]
        ),
        st.integers(min_value=1, max_value=10_000_000).map(Minor),
    )


def probabilities() -> st.SearchStrategy[Probability]:
    """Probabilities in [0, 1] at four decimal places."""
    return st.one_of(
        st.sampled_from([Probability(value) for value in _INTERESTING_PROBABILITIES]),
        st.integers(min_value=0, max_value=10_000).map(
            lambda scaled: Probability(Decimal(scaled) / Decimal(10_000))
        ),
    )


def signed_increments() -> st.SearchStrategy[SignedIncrement]:
    """Signed increments in [-1, 1] at four decimal places, including negatives.

    Negatives are sampled deliberately and often: an action estimated to make
    recovery *less* likely must be excluded for a stated reason, and a strategy
    that only produced positives would never exercise that path.
    """
    return st.integers(min_value=-10_000, max_value=10_000).map(
        lambda scaled: SignedIncrement(Decimal(scaled) / Decimal(10_000))
    )


def confidences() -> st.SearchStrategy[Confidence]:
    """Confidences in [0, 1] at three decimal places.

    Includes exactly 1.000, which is reserved for a deterministic mapping match,
    and 0.990, the ceiling for AI-assisted diagnosis.
    """
    return st.one_of(
        st.sampled_from(
            [
                Confidence(Decimal("0.000")),
                Confidence(Decimal("0.001")),
                Confidence(Decimal("0.599")),
                Confidence(Decimal("0.600")),
                Confidence(Decimal("0.601")),
                Confidence(Decimal("0.990")),
                Confidence(Decimal("1.000")),
            ]
        ),
        st.integers(min_value=0, max_value=1_000).map(
            lambda scaled: Confidence(Decimal(scaled) / Decimal(1_000))
        ),
    )


def risk_causes() -> st.SearchStrategy[RiskCause]:
    """Every risk cause, including UNKNOWN and FRAUD_OR_RISK_SIGNAL.

    Those two are the ones with special handling — UNKNOWN yields the narrowest
    candidate set and FRAUD_OR_RISK_SIGNAL forces escalation — so a strategy that
    excluded them would skip the two most consequential branches.
    """
    return st.sampled_from(list(RiskCause))


def case_states() -> st.SearchStrategy[CaseState]:
    """Every one of the fourteen case states."""
    return st.sampled_from(list(CaseState))


def terminal_states() -> st.SearchStrategy[CaseState]:
    return st.sampled_from(sorted(TERMINAL_STATES))


def non_terminal_states() -> st.SearchStrategy[CaseState]:
    return st.sampled_from(sorted(NON_TERMINAL_STATES))


def state_pairs() -> st.SearchStrategy[tuple[CaseState, CaseState]]:
    """Any ordered pair of states, legal or not.

    Deliberately unfiltered: the point of the legality property is that illegal
    pairs are rejected, so the strategy has to generate them.
    """
    return st.tuples(case_states(), case_states())


def candidate_actions() -> st.SearchStrategy[CandidateAction]:
    return st.sampled_from(list(CandidateAction))


def estimation_methods() -> st.SearchStrategy[EstimationMethod]:
    return st.sampled_from(list(EstimationMethod))


def provenances() -> st.SearchStrategy[Provenance]:
    return st.sampled_from(list(Provenance))

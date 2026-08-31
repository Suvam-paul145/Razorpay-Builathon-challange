"""The legal recovery-case transition table, derived rather than hand-listed.

The table is generated from four declarations — forward edges, the single re-entry
edge, the terminal states, and the reconciliation edge. That matters because the
property test that checks the case manager reads *this* declaration. If the table
were hand-written, the test would be checking the table against itself.

Termination is guaranteed by construction. The only cycle in the graph is
``WAITING_FOR_OUTCOME -> DECISION_PENDING``, and it is guarded by a decision-cycle
counter that only ever increases and is capped. See Requirement 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, unique
from types import MappingProxyType

from revora.domain.enums import CaseState

__all__ = [
    "LEGAL_TRANSITIONS",
    "NON_TERMINAL_STATES",
    "TERMINAL_STATES",
    "CounterEffects",
    "TransitionKind",
    "TransitionRule",
    "is_legal",
    "is_terminal",
    "legal_targets",
    "rule_for",
]


@unique
class TransitionKind(StrEnum):
    """Why an edge exists. Used to reason about guards, not just legality."""

    FORWARD = "FORWARD"
    REENTRY = "REENTRY"
    TERMINATION = "TERMINATION"
    RECONCILIATION = "RECONCILIATION"


@dataclass(frozen=True, slots=True)
class CounterEffects:
    """What a transition does to the case counters.

    Counters only ever increase. That is what stops a replayed event from resetting
    a bound and letting an extra message through.
    """

    executed_action_delta: int = 0
    customer_message_delta_if_visible: int = 0
    decision_cycle_delta: int = 0
    sets_last_outbound_at: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("executed_action_delta", self.executed_action_delta),
            ("customer_message_delta_if_visible", self.customer_message_delta_if_visible),
            ("decision_cycle_delta", self.decision_cycle_delta),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative; counters never decrement")


NO_EFFECTS = CounterEffects()


@dataclass(frozen=True, slots=True)
class TransitionRule:
    """One legal edge, with its guards and counter effects."""

    source: CaseState
    target: CaseState
    kind: TransitionKind
    effects: CounterEffects = field(default=NO_EFFECTS)
    requires_verified_capture: bool = False
    """True only for the reconciliation edge. A terminal case may be moved to
    RECOVERED at most once, and only on an authoritative provider read that
    reports the payment captured."""

    at_most_once_per_case: bool = False
    """True only for the reconciliation edge."""


# ---------------------------------------------------------------------------
# The four declarations the table is built from
# ---------------------------------------------------------------------------

TERMINAL_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.RECOVERED,
        CaseState.STOPPED,
        CaseState.BLOCKED,
        CaseState.EXPIRED,
        CaseState.ESCALATED,
        CaseState.FAILED,
    }
)

NON_TERMINAL_STATES: frozenset[CaseState] = frozenset(
    state for state in CaseState if state not in TERMINAL_STATES
)

_FORWARD: tuple[tuple[CaseState, CaseState, CounterEffects], ...] = (
    (CaseState.NEW, CaseState.DETECTED, NO_EFFECTS),
    (CaseState.DETECTED, CaseState.DIAGNOSED, NO_EFFECTS),
    (
        CaseState.DIAGNOSED,
        CaseState.DECISION_PENDING,
        CounterEffects(decision_cycle_delta=1),
    ),
    (CaseState.DECISION_PENDING, CaseState.POLICY_CHECK, NO_EFFECTS),
    (CaseState.POLICY_CHECK, CaseState.ACTION_SCHEDULED, NO_EFFECTS),
    # The executed-action counter moves here, before the provider request goes
    # out. Deliberately pessimistic: a crash right after this burns an attempt
    # that never happened. The alternative risks a crash loop issuing calls while
    # consuming zero attempts, which could exceed the cap and message someone
    # twice. Given the choice, the design under-attempts.
    (
        CaseState.ACTION_SCHEDULED,
        CaseState.EXECUTING,
        CounterEffects(
            executed_action_delta=1,
            customer_message_delta_if_visible=1,
            sets_last_outbound_at=True,
        ),
    ),
    (CaseState.EXECUTING, CaseState.WAITING_FOR_OUTCOME, NO_EFFECTS),
    (CaseState.WAITING_FOR_OUTCOME, CaseState.RECOVERED, NO_EFFECTS),
)

_REENTRY: tuple[tuple[CaseState, CaseState, CounterEffects], ...] = (
    (
        CaseState.WAITING_FOR_OUTCOME,
        CaseState.DECISION_PENDING,
        CounterEffects(decision_cycle_delta=1),
    ),
)

_RECONCILIATION_TARGETS: frozenset[CaseState] = frozenset(
    TERMINAL_STATES - {CaseState.RECOVERED}
)
"""A case that ended for another reason can still be reconciled to RECOVERED if
the money turns out to have arrived — a delayed webhook, or a payment made after
the window closed. Exactly once, and only against a verified read."""


def _build_table() -> Mapping[tuple[CaseState, CaseState], TransitionRule]:
    table: dict[tuple[CaseState, CaseState], TransitionRule] = {}

    for source, target, effects in _FORWARD:
        table[(source, target)] = TransitionRule(
            source=source, target=target, kind=TransitionKind.FORWARD, effects=effects
        )

    for source, target, effects in _REENTRY:
        table[(source, target)] = TransitionRule(
            source=source, target=target, kind=TransitionKind.REENTRY, effects=effects
        )

    # One edge from every non-terminal state to every terminal state except
    # RECOVERED. RECOVERED is excluded here because it is only reachable from
    # WAITING_FOR_OUTCOME on the forward path, or by reconciliation below —
    # never as a generic termination.
    for source in NON_TERMINAL_STATES:
        for target in TERMINAL_STATES - {CaseState.RECOVERED}:
            table[(source, target)] = TransitionRule(
                source=source, target=target, kind=TransitionKind.TERMINATION
            )

    for source in _RECONCILIATION_TARGETS:
        table[(source, CaseState.RECOVERED)] = TransitionRule(
            source=source,
            target=CaseState.RECOVERED,
            kind=TransitionKind.RECONCILIATION,
            requires_verified_capture=True,
            at_most_once_per_case=True,
        )

    return MappingProxyType(table)


LEGAL_TRANSITIONS: Mapping[tuple[CaseState, CaseState], TransitionRule] = _build_table()
"""Every legal edge. Read-only — a component must not extend this at runtime."""


def is_terminal(state: CaseState) -> bool:
    """True if no further progress is possible from this state."""
    return state in TERMINAL_STATES


def is_legal(source: CaseState, target: CaseState) -> bool:
    """True if this edge appears in the table."""
    return (source, target) in LEGAL_TRANSITIONS


def rule_for(source: CaseState, target: CaseState) -> TransitionRule | None:
    """The rule for an edge, or ``None`` if the edge is illegal."""
    return LEGAL_TRANSITIONS.get((source, target))


def legal_targets(source: CaseState) -> frozenset[CaseState]:
    """Every state reachable from ``source`` in one legal step."""
    return frozenset(
        target for (src, target) in LEGAL_TRANSITIONS if src == source
    )

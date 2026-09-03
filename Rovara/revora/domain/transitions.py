"""The legal recovery-case transition table, derived rather than hand-listed.

The table is generated from five declarations — the forward edges, the re-entry edge,
the review edge, the terminal states, and the reconciliation edge. That matters because
the property test that checks the case manager reads *this* declaration. If the table
were hand-written, the test would be checking the table against itself.

Termination — the proof
=======================

Termination is guaranteed by construction, and the guarantee does not rest on the graph
having one cycle. It has two: ``WAITING_FOR_OUTCOME -> DECISION_PENDING`` (*we acted, and
now we are deciding again*) and ``POLICY_CHECK -> DECISION_PENDING`` (*we chose not to
act, and now we are looking again*). What the proof rests on is an invariant that holds
of both, and of any cycle a later edge could create:

    Every cycle in the transition graph contains an edge whose target is
    ``DECISION_PENDING``, and all three such edges carry ``decision_cycle_delta = 1``.

The three edges into ``DECISION_PENDING`` are ``DIAGNOSED ->``, ``WAITING_FOR_OUTCOME ->``
and ``POLICY_CHECK ->``; the first is not itself on a cycle, and carries the delta anyway,
which is what makes the invariant a property of the *target state* rather than of a list
of edges. The chain:

1. Any path of unbounded length must traverse a cycle, because the graph is finite.
2. Every cycle contains an edge into ``DECISION_PENDING``, and every edge into
   ``DECISION_PENDING`` increments ``decision_cycle_count`` by exactly one. So one cycle
   traversal costs at least one decision cycle.
3. ``decision_cycle_count`` never decreases. ``apply_locked_transition`` only ever *adds*
   a delta, :meth:`CounterEffects.__post_init__` refuses a negative one, and the
   ``counters_nonnegative`` and ``counters_within_bounds`` constraints on
   ``recovery_case`` back it below the application.
4. Entry to ``DECISION_PENDING`` is refused once the counter has reached
   ``MAX_RECOVERY_ATTEMPTS`` — R30.C10 for a review, and the ``MAX_ATTEMPTS_REACHED``
   policy check for the forward path — and the case is transitioned to a terminal state
   instead.

Therefore cycle traversals per case are at most ``MAX_RECOVERY_ATTEMPTS``, every path is
of bounded length, and every case reaches a terminal state. See Requirement 2 and
Requirement 30.

**The review edge does not touch ``window_end_at``.** No transition in this table writes
that column at all, and R2.C5 makes it immutable once the case is opened, so the base
spec's wall-clock termination bound — ``RECOVERY_WINDOW_DURATION + OUTCOME_WAIT_TIMEOUT +
LIFECYCLE_EVALUATION_INTERVAL`` (P6) — is preserved verbatim rather than widened by the
review loop (R30.C2). P63 is P6 restated under review, and it is the same number.
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
    REVIEW = "REVIEW"
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

_REVIEW: tuple[tuple[CaseState, CaseState, CounterEffects], ...] = (
    (
        CaseState.POLICY_CHECK,
        CaseState.DECISION_PENDING,
        CounterEffects(decision_cycle_delta=1),
    ),
)
"""The edge a case takes when it chose restraint and is being looked at again (R30.C1).

Without it, a case that selected ``DO_NOTHING`` or ``WAIT`` sat at ``POLICY_CHECK`` until
its window closed: correctly non-terminal, and unreachable by any second decision cycle.
Since ``WAITING_FOR_OUTCOME`` is reachable only through a confirmed executed action, the
only re-entry loop that existed was reachable only *after* an intervention — so Revora
re-decided exactly the cases it had already acted on, and never the ones where it had
been right to wait.

The effects are ``decision_cycle_delta=1`` and nothing else, deliberately.
``executed_action_delta``, ``customer_message_delta_if_visible`` and
``sets_last_outbound_at`` stay at their defaults, so a review moves no outbound counter
and does not reset the cooldown clock — looking at a case again is not contacting anyone.

A distinct kind rather than a second ``REENTRY`` member. The two edges answer different
questions — "we acted and are deciding again" against "we chose not to act and are
looking again" — and one kind covering both would make *how often does restraint get
revisited* unanswerable from the record, which is the question Requirement 30 exists to
make askable."""

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

    for source, target, effects in _REVIEW:
        table[(source, target)] = TransitionRule(
            source=source, target=target, kind=TransitionKind.REVIEW, effects=effects
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

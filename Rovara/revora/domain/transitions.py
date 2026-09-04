"""The legal recovery-case transition table, derived rather than hand-listed.

The table is generated from six declarations — the forward edges, the re-entry edges,
the review edge, the terminal states, the reconciliation targets, and the verified-capture
sources. That matters because the property test that checks the case manager reads *this*
declaration. If the table were hand-written, the test would be checking the table against
itself.

Termination — the proof
=======================

Termination is guaranteed by construction, and the guarantee does not rest on the graph
having one cycle. It has three: ``WAITING_FOR_OUTCOME -> DECISION_PENDING`` (*we acted, and
now we are deciding again*), ``POLICY_CHECK -> DECISION_PENDING`` (*we chose not to act, and
now we are looking again*) and ``EXECUTING -> DECISION_PENDING`` (*we tried to act, the
provider refused, and nothing was delivered*). What the proof rests on is an invariant that
holds of all three, and of any cycle a later edge could create:

    Every cycle in the transition graph contains an edge whose target is
    ``DECISION_PENDING``, and all four such edges carry ``decision_cycle_delta = 1``.

The four edges into ``DECISION_PENDING`` are ``DIAGNOSED ->``, ``WAITING_FOR_OUTCOME ->``,
``POLICY_CHECK ->`` and ``EXECUTING ->``; the first is not itself on a cycle, and carries the
delta anyway, which is what makes the invariant a property of the *target state* rather than
of a list of edges. The chain:

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
    """True for every RECONCILIATION edge into RECOVERED, from a terminal state or
    from a mid-pipeline one. A case may be moved to RECOVERED at most once, and only
    on an authoritative provider read that reports the payment captured.

    The one edge into RECOVERED that does *not* carry it is the forward edge from
    WAITING_FOR_OUTCOME, whose caller has already taken that read — see
    :data:`_VERIFIED_CAPTURE_SOURCES`."""

    at_most_once_per_case: bool = False
    """True for every RECONCILIATION edge. See :data:`_VERIFIED_CAPTURE_SOURCES`
    for why the mid-pipeline edges carry it too."""


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
    (
        CaseState.EXECUTING,
        CaseState.DECISION_PENDING,
        CounterEffects(decision_cycle_delta=1),
    ),
)
"""Two ways a case comes back to deciding after an action was attempted.

``WAITING_FOR_OUTCOME ->`` is the original: the effect was confirmed, we waited, and now we
are deciding again.

``EXECUTING ->`` is for an attempt the provider **definitively refused** — a parsed 4xx, a
429, a connect-phase failure. Nothing was delivered, so there is no outcome to wait for, and
the design's degradation ladder says such a case returns to ``DECISION_PENDING`` where its
bounds still permit (Requirement 24). Two alternatives were rejected. Routing through
``WAITING_FOR_OUTCOME`` would have needed no new edge and would have written "waiting for
outcome" into the audit trail of an action that reached nobody — and it would falsify the
thing that makes ``WAITING_FOR_OUTCOME`` worth reading, that it is entered only through a
confirmed effect. Leaving the case in ``EXECUTING`` for the lifecycle sweeper strands it
until its window closes, which is the behaviour the promise follow-up cannot accept: the
whole point of following up on a promise is that it happens near the promised date.

**It is not a retry edge and cannot become one.** Re-entry costs a decision cycle, so the
counter bounds it exactly as it bounds every other loop; the attempt ordinal advances, so the
next attempt derives a *different* idempotency key and can never reuse the refused one; and
the twelve policy checks run again before anything is sent, including ``COOLDOWN_ACTIVE`` and
``MAX_MESSAGES_REACHED`` — which the refused attempt already spent an increment against.
Nothing here decides that another attempt is permitted. It decides only that the question is
worth asking again.

Neither edge moves an outbound counter: those move once, on ``ACTION_SCHEDULED ->
EXECUTING``, and a definitive failure does not give the increment back."""

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

_VERIFIED_CAPTURE_SOURCES: frozenset[CaseState] = frozenset(
    NON_TERMINAL_STATES - {CaseState.WAITING_FOR_OUTCOME}
)
"""Every non-terminal state a verified capture may declare RECOVERED from directly.

A customer can pay at any moment, and the moment is not ours to choose. It can land while
the case is still ``NEW``, while the diagnosis is running, while the optimizer is deciding,
while the provider request is in flight. When the Outcome_Monitor takes an authoritative
read that reports the payment captured, it records the recovery and counts the money — so
the case's state has to be able to follow. Without these edges it could not: RECOVERED was
reachable only from ``WAITING_FOR_OUTCOME`` and from the terminal states, so a mid-pipeline
capture had its money counted, its ``RECOVERY_RECORDED`` written, and its transition refused,
and the case went on to expire. **Measured, not theorised:** a 150-case batch produced 62
``RECOVERY_RECORDED`` records against 39 cases in RECOVERED, with 23 refusals logged.

**Every edge here requires a verified capture.** That is the governing principle of the whole
recovery path and it is not weakened for convenience: recovery is declared only from an
authoritative provider read reporting the capture, never from a webhook, an inference, or a
timer. RECOVERED is also still excluded from the ``TERMINATION`` loop below, so it remains
unreachable as a generic ending — a case cannot be *stopped* into RECOVERED.

``WAITING_FOR_OUTCOME`` is excluded because it already has an edge, the ``FORWARD`` one, and
overwriting it here would change the kind recorded for the ordinary recovery path. Its caller
is the same Outcome_Monitor holding the same read, so nothing is left ungated in practice.

``at_most_once_per_case=True``, matching the terminal edges. RECOVERED is terminal and has no
outgoing edge, so a case can only enter it once whatever this flag says — the flag is not what
makes the money count once. It is set anyway because it is the honest description of the edge:
the claim being made is *this money may be counted once*, which is the same claim the terminal
reconciliation edges make. Setting it ``False`` would say the opposite — that if the flag ever
gains enforcement, a mid-pipeline capture should be exempt from it — and that is the reading
that would let the same capture be counted twice the day RECOVERED stops being a sink.

Kind ``RECONCILIATION`` rather than a sixth ``TransitionKind``. Both situations are the same
act: an authoritative read said the money arrived, and the case is being aligned with it. A
new kind would split the audit trail's answer to *how often does reality overtake the
pipeline* across two names, and neither name would answer it alone.

Two alternatives were rejected. Routing a mid-pipeline capture through ``WAITING_FOR_OUTCOME``
as an intermediate hop would have needed no new edge, and it would have falsified the thing
that makes ``WAITING_FOR_OUTCOME`` worth reading — that it is entered only through a confirmed
effect — while writing "waiting for the payment" into the audit trail of a payment that had
already arrived. Terminating the case first and then reconciling it, so the existing terminal
edges could carry it, would record an ending that never happened and would spend a terminal
reason on a case that recovered."""


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
    # RECOVERED. RECOVERED is excluded here — and stays excluded — because it must
    # never be a generic termination target: a case is not *stopped* into RECOVERED,
    # it is only ever declared recovered against an authoritative read that reports
    # the capture. Every edge into it is either the forward edge above, whose caller
    # holds that read, or one of the guarded reconciliation edges below.
    for source in NON_TERMINAL_STATES:
        for target in TERMINAL_STATES - {CaseState.RECOVERED}:
            table[(source, target)] = TransitionRule(
                source=source, target=target, kind=TransitionKind.TERMINATION
            )

    # Reconciliation, in two halves that differ only in where the case was standing
    # when the money arrived: after it ended for another reason
    # (:data:`_RECONCILIATION_TARGETS`), or while it was still being worked
    # (:data:`_VERIFIED_CAPTURE_SOURCES`). Both are gated on a verified capture.
    for source in _RECONCILIATION_TARGETS | _VERIFIED_CAPTURE_SOURCES:
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

"""Clock plans: generated sequences of time steps that land on the bounds that matter.

Every timing rule in Revora is a comparison between two stored UTC instants — has the cooldown
elapsed, is the recovery window over, has the outcome wait timed out, is this contact past its
retention bound. A property test that cannot move time cannot test any of them, and a test that
moved time by sleeping would be slow and flaky instead of exact. ``ManualClock`` is the lever;
this module decides where to push it.

**Uniform random deltas would almost never land on a boundary.** With a seven-day window and a
one-day cooldown, drawing seconds uniformly from a week gives a vanishing chance of stepping to
exactly the instant a bound is crossed — and "exactly at the bound" is where off-by-one errors
live. ``>=`` and ``>`` differ only there. So the deltas here are drawn from a catalogue built
*from the configured bounds*: just under, exactly, and just over each one.

**Every delta is derived from the configuration rather than hard-coded.** A literal
``timedelta(days=1)`` in this file would silently stop being "the cooldown" the moment somebody
tuned ``COOLDOWN_INTERVAL``, and the test would keep passing while no longer probing the boundary
it was written for. Passing the ``Configuration`` in is what keeps the catalogue honest.

**Deltas are non-negative here.** ``ManualClock.advance`` permits going backwards, and clock skew
is real, but a lifecycle machine that steps time backwards would be checking a different claim —
that the state machine tolerates a retrograde clock — and mixing it in would make every other
counterexample harder to read. The retrograde case belongs in its own focused test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from hypothesis import strategies as st

if TYPE_CHECKING:  # pragma: no cover - typing only
    from revora.platform.config import Configuration

__all__ = [
    "BOUNDARY_OFFSET",
    "ClockPlan",
    "boundary_deltas",
    "clock_plan",
    "clock_step",
]

BOUNDARY_OFFSET: Final[timedelta] = timedelta(seconds=1)
"""How far either side of a bound to step.

One second, which is coarser than the microsecond a strict off-by-one would need and coarser on
purpose: every bound in the configuration is expressed in whole seconds or larger, and a
microsecond step would generate two instants that are "different" only in a way no comparison in
the system can act on. A second is the smallest step that produces genuinely distinct behaviour.
"""

_ZERO: Final[timedelta] = timedelta(0)


@dataclass(frozen=True, slots=True)
class ClockPlan:
    """A sequence of forward time steps, and the bound each one was chosen to probe.

    ``labels`` exists so a failing counterexample says *why* a step was interesting —
    ``"cooldown+1s"`` rather than ``"86401 seconds"``. A Hypothesis shrink report that names the
    boundary it landed on is the difference between a five-minute diagnosis and an hour of
    arithmetic.
    """

    steps: tuple[timedelta, ...]
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.steps) != len(self.labels):
            raise ValueError(
                f"{len(self.steps)} steps but {len(self.labels)} labels; they must correspond"
            )
        if any(step < _ZERO for step in self.steps):
            raise ValueError("clock plans step forward only; see the module docstring")

    @property
    def total(self) -> timedelta:
        """How far the whole plan moves the clock.

        Used by a teardown that has to advance past the worst-case bound: the teardown needs to
        know where the clock already is to work out how much further to go.
        """
        return sum(self.steps, _ZERO)

    def __str__(self) -> str:
        return " -> ".join(self.labels) if self.labels else "(no steps)"


def boundary_deltas(config: Configuration) -> tuple[tuple[str, timedelta], ...]:
    """The catalogue: every configured bound, at just-under, exactly, and just-over.

    Four bounds are included and each one changes a different decision:

    * ``COOLDOWN_INTERVAL`` gates a *second* outbound contact to the same customer (P10). Just
      under it, a confirmed action must be refused; just over, permitted.
    * ``RECOVERY_WINDOW_DURATION`` ends the case (P11, R16.C6). Crossing it must expire every
      non-terminal case and must forbid any further confirmed action.
    * ``OUTCOME_WAIT_TIMEOUT`` decides how long a case may sit in ``WAITING_FOR_OUTCOME`` before
      it stops waiting.
    * ``CUSTOMER_DATA_RETENTION`` is the boundary past which contact data must be redacted —
      while the ``customer_key`` survives, because destroying it would revoke every recorded
      opt-out.
    * ``WAIT_REVIEW_INTERVAL`` is when a case that chose restraint becomes due for a review
      (R30.C5). The Review_Sweeper's predicate is ``next_review_at <= now``, so *exactly* at the
      interval the case must be found and one second before it must not — and at twelve hours it is
      the second-shortest bound here, which means a plan that never targeted it would cross it
      constantly and land on it never.

    ``POLICY_DECISION_VALIDITY`` is here too, and it is the shortest of them at fifteen minutes.
    It is the bound most likely to be crossed by accident in a long plan, which is exactly why it
    needs to be crossed deliberately: an approval that outlived its validity must be discarded and
    audited rather than executed.

    A zero step is included. It is not padding — it is the only way to generate two operations at
    *the same instant*, which is what makes "the audit sequence orders records that share a
    timestamp" a real assertion rather than an accident of execution speed.
    """
    bounds: tuple[tuple[str, timedelta], ...] = (
        ("policy-validity", config.POLICY_DECISION_VALIDITY),
        ("wait-review", config.WAIT_REVIEW_INTERVAL),
        ("cooldown", config.COOLDOWN_INTERVAL),
        ("outcome-wait", config.OUTCOME_WAIT_TIMEOUT),
        ("window", config.RECOVERY_WINDOW_DURATION),
        ("retention", config.CUSTOMER_DATA_RETENTION),
    )

    catalogue: list[tuple[str, timedelta]] = [("same-instant", _ZERO)]
    for name, bound in bounds:
        # Just under, exactly, just over. `max(_ZERO, ...)` guards the degenerate case of a bound
        # configured smaller than the offset — a one-second bound would otherwise generate a
        # negative delta and trip the invariant in `ClockPlan`.
        catalogue.append((f"{name}-1s", max(_ZERO, bound - BOUNDARY_OFFSET)))
        catalogue.append((f"{name}", bound))
        catalogue.append((f"{name}+1s", bound + BOUNDARY_OFFSET))
    return tuple(catalogue)


def clock_step(config: Configuration) -> st.SearchStrategy[tuple[str, timedelta]]:
    """One labelled step, drawn from the boundary catalogue.

    Exposed separately because the lifecycle machine advances the clock as a *rule* — one step at
    a time, interleaved with whatever else Hypothesis chooses — rather than consuming a whole plan
    up front. A machine that took a full plan at the start could not interleave a clock step
    between an execution and its reconciliation, which is where the interesting orderings are.
    """
    return st.sampled_from(boundary_deltas(config))


def clock_plan(
    config: Configuration, *, min_size: int = 1, max_size: int = 6
) -> st.SearchStrategy[ClockPlan]:
    """A whole sequence of boundary-crossing steps.

    For tests that want a fixed schedule rather than an interleaved one — a restart scenario, say,
    where the point is that a known amount of time passed while nothing was running.

    ``max_size`` is six because the plan's *total* matters: five steps of ``retention+1s`` is
    two and a half years of simulated time, and every bound in the system has been crossed several
    times over by then. Longer plans explore no new behaviour and make counterexamples harder to
    read.
    """
    return st.lists(clock_step(config), min_size=min_size, max_size=max_size).map(
        lambda pairs: ClockPlan(
            steps=tuple(delta for _, delta in pairs),
            labels=tuple(label for label, _ in pairs),
        )
    )

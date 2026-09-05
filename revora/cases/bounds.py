"""Whether a case has anything left to decide. One question, one implementation.

:func:`bound_reached` was written in :mod:`revora.execution.resend`, which is where its first
caller was: a definitively refused resend either re-enters ``DECISION_PENDING`` or terminates,
and which of the two is a question about counters and the window. R24.C14 gave it a second
caller on the other side of the layering — a promise moving to ``MISSED`` asks exactly the same
question, and that is discovered by the Outcome_Monitor, which sits on the same layer band as
the execution engine and therefore may not import from it.

So the function moved **down** rather than being copied sideways. ``revora.cases`` is below
``revora.customer``, ``revora.outcome`` and ``revora.execution`` alike, which makes it the one
place all three can read it from, and it is the right home on meaning as well as on layering:
"has this case run out of room" is a fact about the case's lifecycle, and this package owns the
lifecycle. :mod:`revora.execution.resend` re-exports the name so every existing caller and every
existing test import is unchanged.

**It is not a policy decision and must not grow into one.** Entering ``DECISION_PENDING`` is not
an action: it schedules a re-decision, and everything that could follow is still gated by all
twelve policy checks against reloaded state. What this answers is the cheaper question of
whether asking again could possibly lead anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from revora.persistence.models import RecoveryCase
    from revora.platform.config import Configuration

__all__ = [
    "BOUND_DECISION_CYCLE_LIMIT",
    "BOUND_MAX_ATTEMPTS",
    "BOUND_MAX_MESSAGES",
    "BOUND_WINDOW_ELAPSED",
    "bound_reached",
]

BOUND_WINDOW_ELAPSED: Final[str] = "RECOVERY_WINDOW_ELAPSED"
BOUND_DECISION_CYCLE_LIMIT: Final[str] = "DECISION_CYCLE_LIMIT_REACHED"
BOUND_MAX_ATTEMPTS: Final[str] = "MAX_ATTEMPTS_REACHED"
BOUND_MAX_MESSAGES: Final[str] = "MAX_MESSAGES_REACHED"
"""The four answers, as named constants rather than bare strings.

They were literals while one caller compared against them; a second caller in a different
package is the point at which a literal becomes a spelling two files can get differently, and
the comparison that would break is ``bound == "RECOVERY_WINDOW_ELAPSED"`` — the one branch that
must *not* terminate the case. A typo there would terminate a case the lifecycle sweeper owns,
under a terminal reason nobody chose, and it would look like a working branch.

Three of the four match a :class:`~revora.domain.enums.TerminalReason` member's value and one —
``MAX_MESSAGES_REACHED`` — deliberately does not. That enumeration is persisted behind a
``CHECK`` generated from itself, so a member for it would be a migration bought with nothing:
the message bound terminates under ``MAX_ATTEMPTS_REACHED`` and the transition's ``reason``
string carries which bound it actually was."""


def bound_reached(
    case: RecoveryCase, config: Configuration, *, moment: datetime
) -> str | None:
    """The first bound that forbids another decision cycle for this case, or ``None``.

    The checks are the counters and the window only. Nothing here reads consent, ownership,
    fraud flags or Recovery_Memory: those are policy's, R15.C6 forbids the last one outright,
    and a second implementation of any of them here would be the copy that drifts.

    Returns:
        One of :data:`BOUND_WINDOW_ELAPSED`, :data:`BOUND_DECISION_CYCLE_LIMIT`,
        :data:`BOUND_MAX_ATTEMPTS` or :data:`BOUND_MAX_MESSAGES`, or ``None`` where every bound
        still permits a further cycle. The window is checked first because it makes the others
        moot — and because it is the one answer whose caller must *not* terminate the case, since
        window expiry belongs to the lifecycle sweeper and two writers of one rule eventually
        disagree about the boundary.
    """
    if moment >= case.window_end_at:
        return BOUND_WINDOW_ELAPSED
    if int(case.decision_cycle_count) >= int(config.MAX_RECOVERY_ATTEMPTS):
        return BOUND_DECISION_CYCLE_LIMIT
    if int(case.executed_action_count) >= int(config.MAX_RECOVERY_ATTEMPTS):
        return BOUND_MAX_ATTEMPTS
    if int(case.customer_message_count) >= int(config.MAX_CUSTOMER_MESSAGES):
        return BOUND_MAX_MESSAGES
    return None

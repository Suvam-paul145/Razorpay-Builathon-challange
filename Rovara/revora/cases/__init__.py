"""The recovery case manager: the single writer of case state, plus the sweeper.

Public surface:

* :func:`revora.cases.manager.apply_transition` — the only writer of
  ``recovery_case.state``, with optimistic concurrency, monotonic counters and
  atomic audit.
* :func:`revora.cases.sweeper.sweep_expired_cases` — the periodic safety net that
  terminates a case on time without depending on a job.
* :func:`revora.cases.startup.reevaluate_cases_on_startup` — the restart drain of
  windows that elapsed during downtime.
"""

from __future__ import annotations

from revora.cases.manager import TransitionOutcome, TransitionResult, apply_transition
from revora.cases.startup import reevaluate_cases_on_startup
from revora.cases.sweeper import sweep_expired_cases

__all__ = [
    "TransitionOutcome",
    "TransitionResult",
    "apply_transition",
    "reevaluate_cases_on_startup",
    "sweep_expired_cases",
]

"""The recovery case manager: the single writer of case state, plus the sweeper.

Public surface:

* :func:`revora.cases.manager.apply_transition` — the only writer of
  ``recovery_case.state``, with optimistic concurrency, monotonic counters and
  atomic audit.
* :func:`revora.cases.sweeper.sweep_expired_cases` — the periodic safety net that
  terminates a case on time without depending on a job.
* :func:`revora.cases.startup.reevaluate_cases_on_startup` — the restart drain of
  windows that elapsed during downtime.
* :func:`revora.cases.review.sweep_due_reviews` and
  :func:`revora.cases.review.enqueue_case_review` — the review sweep over cases that
  chose restraint, and the one enqueue every Review_Trigger shares.
"""

from __future__ import annotations

from revora.cases.manager import TransitionOutcome, TransitionResult, apply_transition
from revora.cases.review import CASE_REVIEW_KIND, enqueue_case_review, sweep_due_reviews
from revora.cases.startup import reevaluate_cases_on_startup
from revora.cases.sweeper import sweep_expired_cases

__all__ = [
    "CASE_REVIEW_KIND",
    "TransitionOutcome",
    "TransitionResult",
    "apply_transition",
    "enqueue_case_review",
    "reevaluate_cases_on_startup",
    "sweep_due_reviews",
    "sweep_expired_cases",
]

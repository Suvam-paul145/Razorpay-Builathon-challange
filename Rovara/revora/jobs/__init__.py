"""The Postgres job queue, the worker loop, and the periodic scheduler.

Public surface:

* :mod:`revora.jobs.queue` — ``enqueue``, ``claim_one``, ``complete``, ``fail``,
  ``reclaim_stale``.
* :mod:`revora.jobs.worker` — ``run_once`` / ``run_forever`` and the kind-to-handler
  registry.
* :mod:`revora.jobs.scheduler` — periodic-sweep enqueue with dedupe keys.
"""

from __future__ import annotations

from revora.jobs.queue import ClaimedJob, claim_one, complete, enqueue, fail, reclaim_stale
from revora.jobs.worker import build_registry, run_forever, run_once

__all__ = [
    "ClaimedJob",
    "build_registry",
    "claim_one",
    "complete",
    "enqueue",
    "fail",
    "reclaim_stale",
    "run_forever",
    "run_once",
]

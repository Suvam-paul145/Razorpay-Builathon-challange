"""The Postgres job queue, the worker loop, the periodic scheduler, and the clock.

Public surface:

* :mod:`revora.jobs.queue` — ``enqueue``, ``claim_one``, ``complete``, ``fail``,
  ``reclaim_stale``.
* :mod:`revora.jobs.worker` — ``run_once`` / ``run_forever`` and the kind-to-handler
  registry.
* :mod:`revora.jobs.scheduler` — periodic-sweep enqueue with dedupe keys.
* :mod:`revora.jobs.ticker` — ``tick`` / ``run_forever``: the producer that calls the
  scheduler and the lease sweep. Deliberately **not** re-exported here. Its
  ``run_forever`` and the worker's are two different loops with one name, and a package
  that exported both would have to rename one of them at the point where the distinction
  matters least. Import the module.

Two process roles run from this package, each through its own ``main`` module that nothing
else imports: ``revora.jobs.main`` for the worker and ``revora.jobs.ticker_main`` for the
ticker. Both of those modules explain why the entrypoint is separate from the loop, and the
second reason they give is this file — a package ``__init__`` that imports a module makes
``python -m`` on that module load it twice.
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

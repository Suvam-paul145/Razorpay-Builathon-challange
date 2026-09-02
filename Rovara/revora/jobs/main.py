"""The worker entrypoint. ``python -m revora.jobs.main``, which is what the container runs.

PLACEHOLDER
and this module's only job is to decide the things that are properties of the *process*: the worker
identity, the poll interval, and turning a signal into a graceful stop.

**Why this module exists rather than running ``revora.jobs.worker`` directly.** Two reasons, and the
second one is the bug that produced it:

* ``worker.py`` has no ``__main__`` block, so ``python -m revora.jobs.worker`` imported it and
  exited immediately with status 0 — a worker that looked like it started and then silently was not
  there, which presents as "cases open but never progress" with nothing in a log to explain it.
* ``revora/jobs/__init__.py`` imports ``worker`` for its public API, so ``-m revora.jobs.worker``
  loads the module twice and Python warns: *found in sys.modules after import of package
  revora.jobs, but prior to execution*. Under a double import, module-level state exists twice.
  ``revora.jobs.main`` is imported by nothing, so there is one copy.

**A graceful stop, not a kill.** ``SIGTERM`` and ``SIGINT`` set the stop event rather than raising,
so the loop finishes the job it is holding and then exits. That matters because a job killed
mid-flight leaves its row ``RUNNING`` for the lease sweep to reclaim, and an execution killed
between the provider call and the result write is the expensive case reconciliation then has to
resolve by reading. Finishing the current job avoids paying for that on every ordinary deploy.
"""

from __future__ import annotations

import os
import signal
import threading
import uuid
from types import FrameType
from typing import Final

from revora.jobs.worker import run_forever
from revora.platform.logging import get_logger

__all__ = ["main", "worker_id"]

_logger = get_logger(__name__)

ENV_WORKER_ID: Final[str] = "REVORA_WORKER_ID"
ENV_POLL_INTERVAL: Final[str] = "REVORA_WORKER_POLL_SECONDS"

_DEFAULT_POLL_SECONDS: Final[float] = 1.0


def worker_id() -> str:
    """A stable identity for this process, from the environment or generated.

    Written onto every job this process claims, so "which worker is holding this?" is answerable
    from the row. A container orchestrator should supply its own — a pod name is far more useful in
    an incident than a random hex string — so the environment wins where it is set.
    """
    configured = os.environ.get(ENV_WORKER_ID, "").strip()
    return configured or f"worker-{uuid.uuid4().hex[:12]}"


def main() -> None:
    """Poll the queue until told to stop."""
    identity = worker_id()
    poll = float(os.environ.get(ENV_POLL_INTERVAL, _DEFAULT_POLL_SECONDS))
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        # Logged at info: an operator reading the log after a deploy should be able to tell a
        # deliberate shutdown from a crash without inferring it from the absence of a traceback.
        _logger.info("stop requested; finishing the current job", signal=signum)
        stop.set()

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _request_stop)

    _logger.info("worker entrypoint starting", worker_id=identity, poll_seconds=poll)
    run_forever(identity, poll_interval_seconds=poll, stop=stop)
    _logger.info("worker entrypoint stopped", worker_id=identity)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()

"""The ticker entrypoint. ``python -m revora.jobs.ticker_main``, which is what the sidecar runs.

The loop itself is :mod:`revora.jobs.ticker`. This module's only job is to decide the things
that are properties of the *process*: the tick interval, the lease bound, and turning a signal
into a graceful stop.

**Why this module exists rather than running ``revora.jobs.ticker`` directly.** The same two
reasons :mod:`revora.jobs.main` gives for the worker, and they are not hypothetical there — the
second one is a bug that was actually paid for:

* ``ticker.py`` has no ``__main__`` block, so ``python -m revora.jobs.ticker`` would import it
  and exit immediately with status 0 — a schedule that looked like it started and then silently
  was not there. For the worker that presented as "cases open but never progress". For the
  ticker it would present as nothing at all: no sweep would be enqueued, every queue metric
  would read clean, and the symptom would surface days later as cases that never expired.
* ``revora/jobs/__init__.py`` imports for its public API, so running a module of that package
  with ``-m`` loads it twice and Python warns: *found in sys.modules after import of package
  revora.jobs, but prior to execution*. Under a double import, module-level state exists twice.
  ``revora.jobs.ticker_main`` is imported by nothing, so there is one copy.

**A graceful stop, not a kill.** ``SIGTERM`` and ``SIGINT`` set the stop event rather than
raising, so the loop finishes the tick it is in and then exits. The reasoning is the worker's,
applied to a different unit of work: for the worker, a job killed mid-flight leaves its row
``RUNNING`` for the lease sweep to reclaim, and an execution killed between the provider call
and the result write is the expensive case reconciliation then has to resolve by reading. For
the ticker, a tick killed part way down the merchant list skips the remaining merchants for
that interval bucket — and a *missing* tick is the one failure in this role that produces no
error, no dead-letter and no failed job. Finishing the current tick avoids paying for either on
every ordinary deploy.

**Exactly one replica.** The sidecar exists to give the schedule one owner; see
``revora.jobs.ticker`` for the argument. Running two is safe — every enqueue is dedupe-keyed by
kind and interval bucket, so the second one collides and returns ``None`` — but it doubles the
log volume for no gain, and the point of the role was to make "did the tick happen" answerable
by reading one stream.
"""

from __future__ import annotations

import os
import signal
import threading
from datetime import timedelta
from types import FrameType
from typing import Final

from revora.jobs.ticker import DEFAULT_JOB_LEASE, DEFAULT_TICK_SECONDS, run_forever
from revora.platform.logging import get_logger

__all__ = ["main"]

_logger = get_logger(__name__)

ENV_TICK_SECONDS: Final[str] = "REVORA_TICKER_INTERVAL_SECONDS"
ENV_LEASE_SECONDS: Final[str] = "REVORA_JOB_LEASE_SECONDS"
"""Both are process properties, which is why they are environment and not ``app_config`` rows.

R15.C6 puts every *policy* bound in a versioned table with an approving user, and neither of
these is one: how often this container wakes up and how long it waits before presuming another
container is dead are facts about the deployment's shape, not judgements a merchant makes about
their own customers. ``REVORA_WORKER_POLL_SECONDS`` is in the environment for the same reason,
and this follows that precedent rather than inventing a second one. The sweep *intervals* — the
numbers that decide how often a merchant's data is actually swept — are rows, seeded by
migration ``0014``, and are read per merchant inside the loop."""


def _float_from_env(name: str, default: float) -> float:
    """A positive float from the environment, or the default.

    Refuses rather than coerces on a value that will not parse or is not positive. A tick
    interval of ``"thirty"`` silently becoming 30 would be lucky; ``"0"`` silently becoming a
    busy loop that ticks thousands of times a second would not, and neither would a negative
    value that made ``Event.wait`` return immediately forever.

    Raises:
        ValueError: if the variable is set to something unparseable or not positive.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be greater than zero")
    return value


def main() -> None:
    """Tick until told to stop."""
    tick_seconds = _float_from_env(ENV_TICK_SECONDS, DEFAULT_TICK_SECONDS)
    lease = timedelta(
        seconds=_float_from_env(ENV_LEASE_SECONDS, DEFAULT_JOB_LEASE.total_seconds())
    )
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        # Logged at info: an operator reading the log after a deploy should be able to tell a
        # deliberate shutdown from a crash without inferring it from the absence of a traceback.
        _logger.info("stop requested; finishing the current tick", signal=signum)
        stop.set()

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _request_stop)

    _logger.info(
        "ticker entrypoint starting",
        tick_seconds=tick_seconds,
        lease_seconds=lease.total_seconds(),
    )
    run_forever(tick_seconds=tick_seconds, lease=lease, stop=stop)
    _logger.info("ticker entrypoint stopped")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()

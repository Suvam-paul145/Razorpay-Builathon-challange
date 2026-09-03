"""A process-local fixed-window rate limiter, and an honest account of what it is not.

Promoted here from ``revora.ingestion.service``, unchanged, because a second caller
appeared: the customer response surface is the one endpoint in Revora reachable without a
session, and R29.C1 asks it to shed a flood per token and per source identifier. Two callers
sharing one implementation is the point — a reimplementation beside this one would be a
second set of window semantics for one guarantee.

**Its weakness is documented rather than designed around.** Two facts, both true and both
deliberately accepted:

1. **The counter is per process.** Two API replicas each admit the configured rate, so the
   system admits twice it. Four replicas admit four times it.
2. **The window resets rather than sliding.** A caller that spends its whole allowance in the
   last instant of one window and again in the first instant of the next has been admitted at
   twice the rate across that boundary.

So the configured number is a *coarse* bound and not a quota, and it is named as one. That is
acceptable here for one reason, and the reason is not tolerance: **the rate limit is not the
bound that matters.** The durable bound on the customer surface is
``customer_access_token.accepted_submission_count``, incremented under a row lock inside the
same transaction as the signal insert, with the comparison against
``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` living in the ``WHERE`` clause of that ``UPDATE``. No
number of replicas and no window boundary can exceed it, and it is the write path — the one
where exceeding a bound has a consequence for a customer. The rate limit guards the *read*
path, where the failure mode of admitting twice the rate is "somebody refreshed a page too
fast".

A shared limiter — a Postgres counter table, or Redis — was considered and rejected. It would
add a write to every page read in order to tighten a guard whose worst outcome is a few extra
reads, while the thing that actually bounds abuse is already durable and already exact. Redis
would additionally be a new service in the deployment for that one purpose.

**What would change this decision.** If a bound on the *read* path ever became correctness
rather than politeness — a per-token cost, a metered provider call behind a read — the
argument above stops holding and this module is the wrong mechanism, not a mechanism to tune.

Nothing here reads a clock. Every caller passes the instant it already has, so a test that
moves the clock and a caller that does not consult one cannot disagree about which window a
request fell in.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "SOURCE_KEY_SPACE",
    "TOKEN_KEY_SPACE",
    "WINDOW",
    "RateLimiter",
    "shared_limiter",
    "source_key",
    "token_key",
]

WINDOW: Final[timedelta] = timedelta(minutes=1)
"""The fixed window every configured rate is expressed per.

One minute, and every bound in the catalogue that feeds this is written "per minute", so the
unit lives in one place rather than being implied by a variable name at each call site."""

TOKEN_KEY_SPACE: Final[str] = "token"
SOURCE_KEY_SPACE: Final[str] = "src"
"""The two key spaces the customer surface uses (R29.C1).

Prefixed rather than bare, and the prefixes are constants rather than literals at the call
site, because the limiter is a single process-wide dictionary shared by every caller. Two
callers whose keys could collide would silently pool their allowances — a merchant slug that
happened to equal a token handle would make one customer's refreshes count against another
merchant's ingestion budget. Namespacing makes that unrepresentable instead of unlikely."""


def token_key(token_id: str) -> str:
    """The per-token key space of R29.C1. ``token_id`` only — never the secret."""
    return f"{TOKEN_KEY_SPACE}:{token_id}"


def source_key(source: str) -> str:
    """The per-source key space of R29.C1.

    ``source`` is whatever the caller can honestly identify the origin of a request by. On the
    customer surface that is the peer address, which is spoofable behind a proxy and shared by
    everyone behind a NAT — both of which are reasons this rate is a flood guard and not an
    authorization control.
    """
    return f"{SOURCE_KEY_SPACE}:{source}"


class RateLimiter:
    """A process-local per-key fixed-window counter.

    Approximate on purpose; see the module docstring for the two ways it is approximate and
    why both are accepted. The window resets rather than sliding, which can admit up to twice
    the limit across a boundary.

    Thread-safe, because the API runs synchronous handlers in a worker-thread pool and two
    requests for one key genuinely do arrive concurrently. The lock is held for a dictionary
    read and a dictionary write, so it is not a contention point at any rate this guard exists
    to shed.
    """

    __slots__ = ("_counts", "_lock", "_window")

    def __init__(self, window: timedelta = WINDOW) -> None:
        self._counts: dict[str, tuple[datetime, int]] = {}
        self._window = window
        self._lock = threading.Lock()

    def allow(self, key: str, limit_per_minute: int, moment: datetime) -> bool:
        """Whether this request is within ``limit_per_minute`` for ``key``.

        A refusal does not consume allowance — the stored count is written back unchanged — so
        a caller that keeps hammering a refused key does not extend its own window.
        """
        with self._lock:
            start, count = self._counts.get(key, (moment, 0))
            if moment - start >= self._window:
                start, count = moment, 0
            if count >= limit_per_minute:
                self._counts[key] = (start, count)
                return False
            self._counts[key] = (start, count + 1)
            return True

    def reset(self) -> None:
        """Forget every key. For tests, and for nothing else.

        A test that exhausts a rate must be able to leave the process usable by the next one,
        and the alternative — a per-test limiter injected through every caller — would mean
        production code taking an argument only a test supplies.
        """
        with self._lock:
            self._counts.clear()


_SHARED: Final[RateLimiter] = RateLimiter()
"""The one limiter every caller shares.

One instance rather than one per caller, so the memory is bounded by the number of distinct
keys rather than by the number of components that decided to limit something. The key spaces
above are what keep the callers from interfering."""


def shared_limiter() -> RateLimiter:
    """The process-wide limiter. A function so no caller rebinds the name to its own."""
    return _SHARED

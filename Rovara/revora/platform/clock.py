"""The single source of "now".

Every timing bound in Revora is a comparison between two stored instants: is the
recovery window over, has the cooldown elapsed, is this policy decision still
valid, did this event arrive before that one. R16.C9 requires all of those
comparisons to happen on UTC instants, because the moment a local-time or
offset-bearing value enters the system one of those comparisons silently becomes
wrong twice a year.

So there is exactly one function that answers "what time is it" — ``now()`` — and
it returns a timezone-aware UTC ``datetime``. Nothing else in the codebase calls
``datetime.now()``. Two reasons, in order of importance:

1. **Naive datetimes cannot escape.** ``ensure_utc`` is the boundary. A naive
   value is refused rather than assumed to be UTC, because "assume UTC" is how a
   wrong instant becomes a durable row.
2. **Tests can move time.** Every one of those bounds is a timestamp comparison,
   so a property test that cannot advance the clock cannot test the bound. It
   would have to sleep, which makes the test slow and flaky instead of exact.
   ``ManualClock`` is that lever.

Substitution is a process-bootstrap and test-setup operation, not something to do
while requests are in flight — the holder is a plain module global, deliberately,
because a per-context clock would let two coroutines in one process disagree
about the time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = [
    "Clock",
    "FrozenClock",
    "ManualClock",
    "NaiveDatetimeError",
    "SystemClock",
    "ensure_utc",
    "get_clock",
    "now",
    "reset_clock",
    "set_clock",
    "using_clock",
]


class NaiveDatetimeError(ValueError):
    """Raised when a datetime without a usable UTC offset reaches the boundary.

    Deliberately not coerced. A naive value means the producer did not know which
    zone it was in, and guessing on its behalf is what turns an off-by-hours bug
    into a stored fact nobody can later distinguish from a correct one.
    """


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC instant, refusing anything without an offset.

    An aware datetime in another zone is converted — that is lossless and the
    caller has already told us what instant it means. A naive datetime is
    rejected.

    Raises:
        NaiveDatetimeError: if ``value`` carries no usable UTC offset.
        TypeError: if ``value`` is not a ``datetime``.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            "naive datetime rejected at the clock boundary; "
            "attach an explicit tzinfo before passing it in"
        )
    return value.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """Anything that can say what time it is, in UTC."""

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC."""
        ...


class SystemClock:
    """The real clock. The only implementation used in a running process."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


#: Arbitrary but fixed default start for ``ManualClock``. A round instant makes a
#: failing property test's counterexample readable.
DEFAULT_MANUAL_START: datetime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


class ManualClock:
    """A clock that only moves when a test moves it.

    Frozen until advanced, so ``now()`` called twice returns the same instant —
    which is what makes "the cooldown had not elapsed" a deterministic assertion
    rather than a race against test execution speed.
    """

    __slots__ = ("_instant",)

    def __init__(self, start: datetime | None = None) -> None:
        self._instant: datetime = ensure_utc(start) if start is not None else DEFAULT_MANUAL_START

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> datetime:
        """Move forward by ``delta`` and return the new instant.

        Backwards movement is permitted — clock skew and out-of-order provider
        timestamps are real, and a test that cannot reproduce them cannot check
        the ordering rules that exist to handle them.
        """
        if not isinstance(delta, timedelta):
            raise TypeError(f"expected timedelta, got {type(delta).__name__}")
        self._instant = self._instant + delta
        return self._instant

    def set_to(self, instant: datetime) -> datetime:
        """Jump to a specific instant. Named ``set_to`` so it never reads as a setter."""
        self._instant = ensure_utc(instant)
        return self._instant

    def __repr__(self) -> str:
        return f"ManualClock({self._instant.isoformat()})"


FrozenClock = ManualClock
"""Alias. The same object read two ways: frozen until you advance it."""


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """The clock currently installed. Callers should prefer ``now()``."""
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install ``clock`` and return the one it replaced.

    Exists so tests and the process bootstrap can substitute; nothing on a request
    or job path should call it.
    """
    global _clock
    previous = _clock
    _clock = clock
    return previous


def reset_clock() -> None:
    """Restore the real clock."""
    set_clock(SystemClock())


@contextmanager
def using_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the duration of the block, then restore.

    A context manager rather than a bare setter so a failing assertion inside a
    test cannot leave a frozen clock installed for every test that follows it.
    """
    previous = set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


def now() -> datetime:
    """The current UTC instant.

    Routed through ``ensure_utc`` even though every bundled clock already returns
    UTC, because a substituted clock is test-supplied and this is the one place
    that can guarantee a naive value never escapes into a comparison or a row.
    """
    return ensure_utc(_clock.now())

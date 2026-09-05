"""A statement counter and a DB-time clock, off unless somebody asks for them.

Three numbers answer most "why is this page slow" questions, and none of them is visible from
outside the process: how many SQL statements one request issued, how long those statements took,
and how long the request took overall. A handler that issues one query and a handler that issues
two hundred look identical from a browser's timing panel, and the second is the shape this
codebase is most likely to grow — every list endpoint composes a per-row view, so an N+1 arrives
by addition rather than by mistake.

**Off by default, and off means nothing is registered.** :func:`install` consults
``REVORA_SQL_TRACE`` and returns ``False`` without attaching a listener when it is unset. That is
deliberately stronger than a listener that checks a flag and returns early: SQLAlchemy's
``before_cursor_execute`` fires for every statement in the process, so a hook that exists at all
costs a Python function call per statement forever in exchange for a diagnostic nobody is reading.
With the variable unset there is no hook on the hot path to skip, because there is no hook.

**The listeners are attached to the ``Engine`` class, not to one engine.** Two reasons, and the
second is the load-bearing one:

* An engine built later — or replaced, which ``persistence.repositories.engine.set_engine`` does
  at bootstrap and between test modules — is covered without anything having to remember to
  re-register.
* It keeps this module's wiring out of the engine factory entirely. The alternative,
  ``event.listen(get_engine(), ...)``, would have to run *after* the process-wide engine exists,
  which means either editing ``build_engine`` or forcing the engine to be constructed at
  application-factory time. The first couples a diagnostic to the module that owns pool sizing and
  connection lifetime; the second changes when a process first dials the database. Neither is worth
  paying for a counter.

**Per-request accumulation uses a :class:`~contextvars.ContextVar` holding a mutable object, not
``threading.local``.** This is the one detail that decides whether the numbers are right, and it is
forced by where the two halves of a request run. Every handler in this API is a synchronous ``def``,
so Starlette runs it in an ``anyio`` worker thread while the middleware that opens and closes the
measurement sits on the event loop thread. So:

* ``threading.local`` is wrong in both directions. The middleware would write the accumulator into
  the loop thread's slot and the handler would look for it in a worker thread's slot and find
  nothing — every request would report zero. And because the threadpool reuses its threads, a value
  left behind in a worker's slot would be read by the *next* request unlucky enough to land on that
  thread, which is a wrong number rather than an absent one.
* A ``ContextVar`` holding an ``int`` is wrong in one direction. ``anyio.to_thread.run_sync``
  copies the context into the worker thread, so the handler *reads* what the middleware set — but
  the copy means the handler's ``set`` is invisible back on the loop thread, and the middleware
  would still log zero.
* A ``ContextVar`` holding a reference to :class:`SqlTrace` is right. The copy points at the same
  object, the worker thread mutates it in place, and the middleware reads the mutation. That is the
  whole reason this is a small mutable class rather than two integers.

It also covers the async cases for free — the webhook handler is ``async def`` and offloads its
blocking work with ``run_in_threadpool``, and a context var is inherited by ``asyncio`` tasks —
which ``threading.local`` would not.

**Nanoseconds, integers, no division into fractions.** ``time.perf_counter_ns`` throughout, and the
figures stay integral all the way to the log line. ``revora.api``, where they are rendered, is a
module the no-float guard covers; a duration is not the place to start making exceptions to a
convention the money figures depend on.

**Increments are not atomic and do not need to be.** One request's statements are issued
sequentially by one thread, so there is no second writer to race with. Guarding the counters with a
lock would put a lock acquisition on the hot path of the very thing being measured, and would
measure the lock.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Connection, Engine, event

__all__ = [
    "ENV_SQL_TRACE",
    "SqlTrace",
    "current_trace",
    "enabled",
    "install",
    "installed",
    "trace_scope",
]

ENV_SQL_TRACE: Final[str] = "REVORA_SQL_TRACE"
"""The one switch. Absent means the tracer does not exist in this process."""

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
"""What counts as on. Anything else — including the empty string a half-configured deployment
leaves behind — is off, because the safe reading of an unrecognised value for a diagnostic is not
to run it."""

_START_KEY: Final[str] = "revora_sqltrace_starts"
"""Key under which a statement's start time is stashed on ``Connection.info``.

Connection-scoped rather than request-scoped, and that is the concurrency argument: a pooled
connection is checked out to exactly one thread for the life of a transaction, so two requests
running in two threads cannot interleave entries here even though they share a process.

A list rather than a scalar because ``before``/``after`` pairs can nest. It is popped LIFO, which
is what keeps the timings correct even when a statement raises between its two events and leaves
its own start behind: the entry a later statement pops is still its own, and the stale one is
bounded by the number of failed statements on that pooled connection.
"""


@dataclass(slots=True)
class SqlTrace:
    """What one request spent on SQL: how many statements, and how long inside them.

    Mutable, and that is the design rather than an oversight — see this module's docstring for why
    the context var has to hold an object instead of a number.
    """

    statements: int = 0
    db_nanoseconds: int = 0

    @property
    def db_micros(self) -> int:
        """DB time in microseconds. Integer division; a log line does not need the last 1000th."""
        return self.db_nanoseconds // 1_000


_trace: ContextVar[SqlTrace | None] = ContextVar("revora_sqltrace", default=None)

_install_lock = threading.Lock()
_installed = False


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``REVORA_SQL_TRACE`` asks for tracing."""
    source = os.environ if environ is None else environ
    return (source.get(ENV_SQL_TRACE) or "").strip().lower() in _TRUTHY


def installed() -> bool:
    """Whether the cursor listeners are attached in this process.

    Exposed so a test can assert the absence rather than infer it. ``False`` here and
    :func:`enabled` ``False`` together are the whole of the zero-overhead claim.
    """
    return _installed


def install(environ: Mapping[str, str] | None = None) -> bool:
    """Attach the cursor listeners if tracing is on, and say whether it is.

    Returns:
        ``True`` when tracing is on — whether this call is what attached the listeners or an
        earlier one did. The caller uses the return value to decide whether to open per-request
        scopes, so "already installed" has to read the same as "just installed": a second
        application built in the same process still needs its own request boundary.

    Idempotent, because two applications in one process (a test that builds several, or the
    customer sub-application beside the dashboard one) would otherwise attach the listeners twice
    and count every statement twice.
    """
    global _installed
    if not enabled(environ):
        return False
    with _install_lock:
        if not _installed:
            event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
            event.listen(Engine, "after_cursor_execute", _after_cursor_execute)
            _installed = True
    return True


def current_trace() -> SqlTrace | None:
    """The accumulator for the scope in progress, or ``None`` outside any scope."""
    return _trace.get()


@contextmanager
def trace_scope() -> Iterator[SqlTrace]:
    """Accumulate statements and DB time for the duration of the block.

    Restored through the token rather than set back to ``None``, so a nested scope — a script that
    wraps a request that wraps a job — hands the outer accumulator back instead of discarding it.
    """
    trace = SqlTrace()
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


def _before_cursor_execute(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Stamp a start time, but only when somebody is listening.

    The ``None`` check is not the overhead argument — that is settled by not registering this
    function at all when the tracer is off. It is here because the worker, the ticker and the
    startup schema check run statements through the same engine with no scope open, and a start
    time pushed for one of those would be popped by a statement belonging to a request.
    """
    if _trace.get() is None:
        return
    starts: list[int] = conn.info.setdefault(_START_KEY, [])
    starts.append(time.perf_counter_ns())


def _after_cursor_execute(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Add one statement and its duration to the scope in progress."""
    trace = _trace.get()
    if trace is None:
        return
    starts: list[int] | None = conn.info.get(_START_KEY)
    if not starts:
        # The scope opened between this statement's `before` and its `after`. Counting it would
        # charge the scope for time it did not spend, so it is dropped rather than estimated.
        return
    trace.statements += 1
    trace.db_nanoseconds += time.perf_counter_ns() - starts.pop()

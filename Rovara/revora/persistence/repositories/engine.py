"""Engine and session factory, and the one environment variable persistence reads.

Environment holds the connection string. It does not hold any tunable bound — those
live in ``app_config`` and are reached through ``platform.config``, because a policy
change needs a recorded approving user and a redeploy cannot supply one.

Three settings here are decisions rather than defaults:

* **``pool_pre_ping``.** A managed Postgres closes idle connections. Without a ping,
  the first query after an idle period fails, and for the worker that failure lands
  in the middle of a claimed job.
* **Pooled connections, not serverless-style churn.** Advisory locks and ``FOR
  UPDATE SKIP LOCKED`` both want a stable connection for the life of a transaction.
* **``expire_on_commit=False``.** A committed row's attributes stay readable, which
  matters because the audit writer reads back the sequence number it allocated in
  the transaction it just committed.

Transaction boundaries are explicit everywhere in this package. The execution and
audit designs depend on controlling them directly — an audit record and the state
change it explains must commit together or not at all — so nothing here opens a
transaction implicitly on the caller's behalf.
"""

from __future__ import annotations

import contextvars
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "ENV_DATABASE_URL",
    "ENV_SQL_TRACE",
    "ENV_STATEMENT_TIMEOUT_MS",
    "DatabaseNotConfiguredError",
    "SqlTrace",
    "build_engine",
    "build_session_factory",
    "current_sql_trace",
    "database_url",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "install_sql_tracer",
    "set_engine",
    "sql_trace_enabled",
    "sql_trace_scope",
]

ENV_DATABASE_URL: Final[str] = "REVORA_DATABASE_URL"
ENV_STATEMENT_TIMEOUT_MS: Final[str] = "REVORA_STATEMENT_TIMEOUT_MS"
"""Per-connection statement timeout. A worker that hangs on a lock holds a
connection and a claimed job; a timeout turns that into a retryable failure."""

DEFAULT_STATEMENT_TIMEOUT_MS: Final[int] = 30_000


class DatabaseNotConfiguredError(RuntimeError):
    """``REVORA_DATABASE_URL`` is absent or blank.

    Distinct from a connection failure: the remedy is a deployment fix, not a
    retry, and a process that cannot know where its database is should not start.
    """


ENV_SQL_TRACE: Final[str] = "REVORA_SQL_TRACE"
"""Off unless explicitly set, and off means *nothing is installed*.

A statement counter is a diagnostic, not a feature, and the whole reason it is
gated on an environment variable rather than on a log level is that a log level
still pays for the timing call before deciding not to emit it. When this variable
is absent :func:`build_engine` registers no listener at all, so a production
statement runs through the same code path it ran through before this existed —
there is no branch inside the hot path to skip, because there is no hook on it.

The API layer reads the same variable to decide whether to open a per-request
scope. Two readers of one variable rather than two variables, because a tracer
with the counter on and the request boundary off would count statements into a
scope nobody ever closes.
"""

_SQL_TRACE_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
"""Values that mean on. Anything else — including the empty string a
half-configured deploy leaves behind — means off, because the safe reading of an
unrecognised value for a diagnostic is to not run it."""

_SQL_TRACE_START_KEY: Final[str] = "revora_sql_trace_start"
"""Key under which a statement's start time is stashed on ``Connection.info``.

Connection-scoped rather than trace-scoped, and that is the thread-safety
argument: a pooled connection is checked out to exactly one thread for the life of
a transaction, so a stack keyed on the connection cannot interleave with another
request's statements even though both requests are running in the same process.
A list rather than a scalar because a ``before``/``after`` pair can nest (a
``pool_pre_ping`` probe emitted inside an outer execution, for one)."""


@dataclass(slots=True)
class SqlTrace:
    """What one request spent on SQL: how many statements, and how long in them.

    Mutable, and deliberately so. The counters are reached through a
    :class:`~contextvars.ContextVar`, and a sync FastAPI handler runs in a
    Starlette worker thread which receives a *copy* of the context — so a
    ``ContextVar`` holding an ``int`` that the thread incremented would leave the
    middleware reading zero. The variable holds a reference to this object
    instead, the copy points at the same object, and the mutation is visible on
    both sides. That is the whole reason this is a class rather than two integers.

    Nanoseconds, from :func:`time.perf_counter_ns`, because the figures are
    integers all the way to the log line. Money is not the only thing in this
    system that a float would quietly reshape, and ``revora.api`` — where these
    values get rendered — is a no-float module by policy.
    """

    statements: int = 0
    db_nanoseconds: int = 0


_sql_trace: contextvars.ContextVar[SqlTrace | None] = contextvars.ContextVar(
    "revora_sql_trace", default=None
)


def sql_trace_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether SQL tracing is switched on. Read once at engine build and app build."""
    source = os.environ if environ is None else environ
    return (source.get(ENV_SQL_TRACE) or "").strip().lower() in _SQL_TRACE_TRUE


def current_sql_trace() -> SqlTrace | None:
    """The accumulator for the scope in progress, or ``None`` outside any scope."""
    return _sql_trace.get()


@contextmanager
def sql_trace_scope() -> Iterator[SqlTrace]:
    """Accumulate statement counts and DB time for the duration of the block.

    Reset through the token on the way out rather than set back to ``None``, so a
    nested scope — a script that wraps a request that wraps a job — restores the
    outer accumulator instead of discarding it.
    """
    trace = SqlTrace()
    token = _sql_trace.set(trace)
    try:
        yield trace
    finally:
        _sql_trace.reset(token)


def _before_cursor_execute(
    conn: Connection,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Stamp a start time, but only if somebody is listening."""
    if _sql_trace.get() is None:
        return
    starts: list[int] = conn.info.setdefault(_SQL_TRACE_START_KEY, [])
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
    trace = _sql_trace.get()
    if trace is None:
        return
    starts: list[int] | None = conn.info.get(_SQL_TRACE_START_KEY)
    if not starts:
        # The scope opened between this statement's `before` and its `after`.
        # Counting it would attribute time the scope did not spend.
        return
    trace.statements += 1
    trace.db_nanoseconds += time.perf_counter_ns() - starts.pop()


def install_sql_tracer(engine: Engine) -> None:
    """Attach the counting listeners to ``engine``.

    Called by :func:`build_engine` only when :func:`sql_trace_enabled`, which is
    what makes "off costs nothing" a statement about the absence of a hook rather
    than about a cheap branch inside one.
    """
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _after_cursor_execute)


def database_url(environ: dict[str, str] | None = None) -> str:
    """The connection string, refusing a blank value.

    A variable set to the empty string is how a misconfigured deploy usually
    presents, and treating it as absent gives a message that names the cause.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(ENV_DATABASE_URL) or "").strip()
    if not raw:
        raise DatabaseNotConfiguredError(
            f"{ENV_DATABASE_URL} is not set; persistence cannot start without it"
        )
    return raw


def build_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """An engine with the pool settings this system's locking strategy needs."""
    resolved = url or database_url()
    timeout_ms = int(os.environ.get(ENV_STATEMENT_TIMEOUT_MS, DEFAULT_STATEMENT_TIMEOUT_MS))
    engine = create_engine(
        resolved,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        connect_args={"options": f"-c statement_timeout={timeout_ms}"},
    )
    if sql_trace_enabled():
        install_sql_tracer(engine)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory that never begins a transaction on its own."""
    return sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=Session,
    )


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """The process-wide engine, built on first use.

    Lazy so importing this package does not require a reachable database — which
    is what lets the pure and model test tiers import repository code without a
    container.
    """
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """The process-wide session factory."""
    global _factory
    if _factory is None:
        _factory = build_session_factory(get_engine())
    return _factory


def set_engine(engine: Engine) -> None:
    """Install an engine and a matching factory. Bootstrap and tests only."""
    global _engine, _factory
    _engine = engine
    _factory = build_session_factory(engine)


def dispose_engine() -> None:
    """Drop the engine and its pool. Used at shutdown and between test modules."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None

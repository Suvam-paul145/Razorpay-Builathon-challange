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

import os
from typing import Final

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "ENV_DATABASE_URL",
    "ENV_STATEMENT_TIMEOUT_MS",
    "DatabaseNotConfiguredError",
    "build_engine",
    "build_session_factory",
    "database_url",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "set_engine",
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
    return create_engine(
        resolved,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        connect_args={"options": f"-c statement_timeout={timeout_ms}"},
    )


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

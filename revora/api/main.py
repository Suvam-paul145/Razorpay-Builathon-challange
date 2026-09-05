"""The API entrypoint. ``python -m revora.api.main``, which is what the container runs.

Thin on purpose. Everything interesting is in :func:`revora.api.app.create_app`, and this module's
only job is to decide the four things that are properties of the *process* rather than of the
application: which port, how many workers, where CORS origins come from, and whether the SQL tracer
is running. The tracer is on this list rather than in the app factory for the same reason the worker
count is — see :func:`install_sql_trace`.

``module:factory`` is passed to uvicorn as a string rather than an app object, because that is what
lets ``--workers`` fork: uvicorn imports the factory in each child, and handing it an already-built
app would either fail to fork or share one connection pool across processes.

**One worker by default.** The database pool is per-process, so N uvicorn workers is N pools against
the same Postgres, and the sizing of one has to be divided by the other. A single process with a
thread pool is the right shape here — every handler is synchronous and FastAPI already runs those in
threads — and scaling out is a matter of running more containers, where the pool arithmetic is
visible rather than hidden inside one.
"""

from __future__ import annotations

import os
import time
from typing import Final

import uvicorn
from fastapi import FastAPI, Request

from revora.api.app import create_app
from revora.platform import sqltrace
from revora.platform.logging import get_logger

__all__ = ["cors_origins", "install_sql_trace", "main", "make_app"]

_logger = get_logger(__name__)

ENV_HOST: Final[str] = "REVORA_API_HOST"
ENV_PORT: Final[str] = "REVORA_API_PORT"
ENV_WORKERS: Final[str] = "REVORA_API_WORKERS"
ENV_CORS_ORIGINS: Final[str] = "REVORA_API_CORS_ORIGINS"
"""Comma-separated exact origins. Absent means no CORS middleware at all, which is the correct
configuration when the SPA is served from the same origin — and stronger than installing the
middleware with an empty list, because there is then nothing to misconfigure later.

Governs the **dashboard** API only. The customer surface reads ``REVORA_CUSTOMER_ORIGINS`` through
``revora.api.routers.customer.customer_origins``, and the two lists are deliberately separate: the
dashboard is same-origin with this API by deployment and the customer page is not, so one list
would mean widening the second also widened the first."""

_DEFAULT_HOST: Final[str] = "0.0.0.0"
_DEFAULT_PORT: Final[int] = 8000


def cors_origins(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Exact origins from the environment. Never a wildcard.

    A wildcard is refused rather than passed through. This API is called with a bearer token, and
    ``allow_origins=["*"]`` on a credentialed API is the configuration every CORS guide warns about
    — so the one value somebody would reach for in a hurry fails at startup instead of shipping.
    """
    raw = (environ or dict(os.environ)).get(ENV_CORS_ORIGINS, "").strip()
    if not raw:
        return ()
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    if "*" in origins:
        raise ValueError(
            f"{ENV_CORS_ORIGINS} must list exact origins; '*' on a bearer-token API permits any "
            "site to read a merchant's dashboard data"
        )
    return origins


def install_sql_trace(app: FastAPI) -> bool:
    """Wire the SQL tracer onto ``app``, if ``REVORA_SQL_TRACE`` asks for it.

    Both halves of the tracer are decided by one call to
    :func:`revora.platform.sqltrace.install`, and that is on purpose: a counter with the cursor
    listeners attached but no request boundary would accumulate statements into a scope nobody ever
    closes, and a request boundary with no listeners would log three zeroes. One env read, one
    branch, so the two cannot disagree.

    Wired here rather than inside :func:`revora.api.app.create_app` because the tracer is a property
    of the *process* — like the port and the worker count, and unlike anything on the API surface. A
    test that builds an application to assert on a response body has no interest in a timing log
    line, and the endpoints' behaviour must not depend on whether the diagnostic is on.

    **The trace line carries no correlation id, and that is the cost of wiring it here.** Starlette
    treats the last-added middleware as the outermost, so this one wraps ``create_app``'s
    correlation middleware rather than sitting inside it, and the id is only bound further in. The
    trade is deliberate: from out here the wall time includes every middleware the request actually
    passed through, which is the number somebody comparing against a browser's timing panel wants.
    A trace line that could be joined to the audit records of the request it measured would be worth
    more, and getting it means adding the middleware inside ``create_app`` — which is the version to
    write if the tracer ever stops being a measurement tool and starts being something operators
    read.

    Nothing about the response changes. No header, no status, no body: the numbers leave through the
    log stream only, because a timing field on a response is a field a client starts depending on.

    Returns:
        Whether the tracer was wired. ``False`` is the default and means no listener is attached
        anywhere in this process.
    """
    if not sqltrace.install():
        return False

    @app.middleware("http")
    async def _sql_trace(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Count this request's statements, its DB time and its wall time, and log once.

        The scope is opened on the event loop thread and the counters are mutated in the worker
        thread the sync handler runs in. That works because the context var holds a mutable object
        rather than a number — see :class:`revora.platform.sqltrace.SqlTrace`.
        """
        started = time.perf_counter_ns()
        with sqltrace.trace_scope() as trace:
            response = await call_next(request)
            wall_nanoseconds = time.perf_counter_ns() - started
            _logger.info(
                "sql trace",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                sql_statements=trace.statements,
                sql_micros=trace.db_micros,
                wall_micros=wall_nanoseconds // 1_000,
            )
            return response

    _logger.info("sql trace enabled", env_var=sqltrace.ENV_SQL_TRACE)
    return True


def make_app() -> object:
    """The factory uvicorn imports in each worker process."""
    app = create_app(cors_origins=cors_origins())
    install_sql_trace(app)
    return app


def main() -> None:
    """Serve. Reads host, port and worker count from the environment."""
    host = os.environ.get(ENV_HOST, _DEFAULT_HOST)
    port = int(os.environ.get(ENV_PORT, _DEFAULT_PORT))
    workers = int(os.environ.get(ENV_WORKERS, "1"))

    _logger.info("api entrypoint starting", host=host, port=port, workers=workers)
    uvicorn.run(
        "revora.api.main:make_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        # Structured logging is ours; uvicorn's access log would emit a second, unstructured line
        # per request with a different correlation story. The request log lives in the app's
        # correlation middleware.
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()

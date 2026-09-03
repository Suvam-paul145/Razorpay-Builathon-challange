"""The API entrypoint. ``python -m revora.api.main``, which is what the container runs.

Thin on purpose. Everything interesting is in :func:`revora.api.app.create_app`, and this module's
only job is to decide the three things that are properties of the *process* rather than of the
application: which port, how many workers, and where CORS origins come from.

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
from typing import Final

import uvicorn

from revora.api.app import create_app
from revora.platform.logging import get_logger

__all__ = ["cors_origins", "main", "make_app"]

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


def make_app() -> object:
    """The factory uvicorn imports in each worker process."""
    return create_app(cors_origins=cors_origins())


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

"""The FastAPI application. One factory, so there is one place the surface is enumerated.

Reading :func:`create_app` should tell you every route this process serves and which of them are
unauthenticated. There are exactly two: the inbound webhook, which authenticates by HMAC over the
raw body instead of by session, and ``GET /health``, which reveals nothing but liveness. Everything
else depends on :func:`revora.api.auth.authenticate` and therefore on a session row.

**The schema revision is verified at startup and the process refuses to serve on a mismatch.** A
worker or an API running against a schema it was not built for produces wrong numbers rather than
errors, and the investigation starts in the wrong place. Refusing to start is loud, immediate and
cheap; starting and behaving subtly differently is none of those.

**Handlers are sync ``def`` except the webhook, and the webhook offloads its blocking work.** Every
database call in this system is synchronous SQLAlchemy. An ``async def`` handler that blocks holds
the event loop for the whole query, so one slow read stalls every other request in the process —
including the webhook acknowledgement, which has a 1500 ms budget. FastAPI runs a sync handler in a
worker thread, which is the correct place for blocking I/O.

**Exception handling is deliberately thin.** An unhandled exception becomes a 500 with no body,
because the alternative — echoing the exception — is how a stack trace naming a table and a column
reaches somebody who should not have it. The exception is logged with its correlation id, which is
what makes it findable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from revora.api import webhooks
from revora.api.routers import cases, consent, experiments, health, metrics, sessions
from revora.api.spa import mount_spa
from revora.persistence.repositories.engine import get_engine
from revora.persistence.repositories.schema import EXPECTED_REVISION, verify_schema_revision
from revora.platform.logging import correlation_context, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = ["TITLE", "create_app"]

_logger = get_logger(__name__)

TITLE = "Revora"

_DESCRIPTION = """
AI-assisted incremental revenue recovery.

Two things about every response from this API:

* **Money arrives pre-formatted.** Every currency figure is a `{minor, currency, formatted}`
  object. Render `formatted`. Do not do arithmetic on `minor` — the server has already done it,
  and two client-side divisions in two components is how one screen shows two different totals.
* **An absent value is never zero.** A figure that has not been produced yet carries
  `status: NOT_YET_RECORDED` and names the case state; one that could not be computed carries
  `status: DATA_UNAVAILABLE` and names the figure. Substituting zero for either is a false
  financial statement, not a display shortcut.

`incremental_recovered_revenue` is `NOT_ESTABLISHED` unless a completed, adequately powered
experiment reports a lift interval entirely above zero. Observed recovery is never presented as
incremental.
""".strip()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the schema before accepting a request, and say so once at startup."""
    revision = verify_schema_revision(get_engine(), expected=EXPECTED_REVISION)
    _logger.info("api starting", schema_revision=revision)
    yield
    _logger.info("api stopping")


def create_app(
    *,
    cors_origins: Sequence[str] | None = None,
    verify_schema: bool = True,
    serve_dashboard: bool = True,
) -> FastAPI:
    """Build the application.

    Args:
        cors_origins: exact origins permitted to call this API from a browser. A list, never a
            wildcard: this API is called with a bearer token, and ``allow_origins=["*"]`` combined
            with credentialed requests is the configuration every CORS guide warns about. Omit it
            entirely when the SPA is served from the same origin, which is the deployment this is
            built for — no CORS middleware is installed at all in that case, which is stronger than
            installing one with an empty list.
        verify_schema: set ``False`` only in tests that construct the app without a database.
            Production must not, and the parameter is named so a grep for it finds every caller.
        serve_dashboard: mount the built SPA when ``web/dist`` exists. Defaults on, because the
            same-origin deployment is the intended one. A test asserting on the API's 404 behaviour
            sets it ``False`` so a catch-all cannot answer in place of the real response — the
            fallback deliberately refuses the API prefixes, and this is how that guarantee gets
            tested from the other side.
    """
    app = FastAPI(
        title=TITLE,
        description=_DESCRIPTION,
        version="0.1.0",
        lifespan=_lifespan if verify_schema else None,
        # No default 422 body reshaping and no OpenAPI-driven response validation beyond the
        # declared models: the read models in `api.views` are dicts by design, because their shape
        # varies with what has been recorded, and forcing them through a rigid response model would
        # mean either dropping the absent-value markers or declaring every field optional.
    )

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["authorization", "content-type", "x-revora-dashboard-key"],
        )

    # Unauthenticated, both by design and both documented as such.
    app.include_router(webhooks.router)
    app.include_router(health.router)

    # Session-authenticated. Every one of these depends on `authenticate`.
    app.include_router(sessions.router)
    app.include_router(cases.router)
    app.include_router(metrics.router)
    app.include_router(experiments.router)
    app.include_router(consent.router)

    # The built dashboard, same-origin with the API it calls — which is what lets this
    # deployment run with no CORS middleware at all. Mounted *after* every router above,
    # because it registers a catch-all for client-side routing, and a catch-all placed before
    # a real route is how an endpoint starts returning HTML. See `revora.api.spa` for the
    # second guard against exactly that.
    #
    # Nothing is mounted when `web/dist` does not exist, so an API-only deployment is unaffected.
    if serve_dashboard:
        mount_spa(app)

    @app.middleware("http")
    async def _correlate(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Give every request a correlation id and put it on the response.

        The same id the audit records written during the request carry, so a merchant reporting "the
        page said something odd at 14:32" can be answered with one query rather than a search. Read
        from the client's header when supplied and well-formed, so a trace that started in the
        browser stays one trace.
        """
        supplied = request.headers.get("x-correlation-id")
        try:
            existing = str(uuid.UUID(supplied)) if supplied else None
        except ValueError:
            existing = None
        with correlation_context(existing) as correlation:
            response = await call_next(request)
            response.headers["x-correlation-id"] = correlation
            return response

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """A 500 with no detail, and a log line with everything.

        Echoing the exception is how a stack trace naming a table and a column reaches somebody who
        should not have it. The correlation id is returned so the log line is findable — which is
        the only thing the caller actually needs from a 500.
        """
        _logger.error(
            "unhandled api exception",
            path=request.url.path,
            method=request.method,
            error=type(exc).__name__,
        )
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app

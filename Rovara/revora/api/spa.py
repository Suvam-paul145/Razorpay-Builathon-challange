"""Serving the built dashboard from the API process, same-origin with the API it calls.

**Why the same process rather than a CDN or a second container.** The API installs no CORS
middleware at all when ``REVORA_API_CORS_ORIGINS`` is unset, and no middleware is a stronger
position than a middleware with an allowlist — there is nothing to misconfigure later. That is
only available if the SPA and the API share an origin. A separately-hosted SPA would force CORS
on permanently, on an API authenticated with a bearer token, which is the configuration every
CORS guide warns about.

**The dashboard lives under ``/app`` and the API keeps its own namespace.** This is the whole
design and it replaced a catch-all that did not work. Sharing one origin means the SPA's client
routes and the API's paths are drawn from the same space, and they collide: the dashboard wants
``/metrics`` and ``/cases`` as client routes while the API owns ``/metrics/summary`` and
``/cases``. A catch-all with a list of API prefixes to refuse gets both cases wrong — it answered
404 for the dashboard's own ``/metrics`` page, and a browser navigating to ``/cases`` received a
401 JSON body instead of the application.

Prefixing the SPA is the smaller change than prefixing the API. Moving the API under ``/api``
would move the webhook URL, which is configured in the Razorpay dashboard and is the one path
this system does not control unilaterally. So ``/app/*`` is the shell, ``/`` redirects to it, and
there is no shared space left to disambiguate — which also deletes the list of prefixes that
would otherwise go stale the first time a router was added.

**Absent build, absent mount.** If ``web/dist`` has not been built, nothing is mounted and the
API serves exactly what it served before. A half-configured static mount answering 404 for every
asset looks like a broken deployment; not mounting looks like what it is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from revora.platform.logging import get_logger

__all__ = ["APP_PREFIX", "ENV_WEB_ROOT", "default_web_root", "mount_spa"]

_logger = get_logger(__name__)

ENV_WEB_ROOT: Final[str] = "REVORA_WEB_ROOT"
"""Where the built SPA lives. Absent means "look next to the package", which is what the container
image produces."""

APP_PREFIX: Final[str] = "/app"
"""The dashboard's namespace. Must match ``basename`` on the router in ``web/src/main.tsx``.

Exported so a test can assert the two agree rather than discovering the mismatch as a blank page."""

_ASSET_MOUNT: Final[str] = "/assets"
"""Vite emits hashed files under ``assets/`` only, and references them from ``index.html`` as
absolute ``/assets/...`` paths. Mounting that one directory rather than the whole dist keeps
``index.html`` under this module's control, which is what lets the cache headers differ: hashed
assets are immutable, the entry document must never be."""

_INDEX: Final[str] = "index.html"


def default_web_root() -> Path:
    """``web/dist`` relative to the repository, or whatever ``REVORA_WEB_ROOT`` names."""
    configured = os.environ.get(ENV_WEB_ROOT, "").strip()
    if configured:
        return Path(configured)
    # revora/api/spa.py -> revora/api -> revora -> repository root
    return Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def mount_spa(app: FastAPI, *, web_root: Path | None = None) -> bool:
    """Serve the built SPA from ``app`` under :data:`APP_PREFIX`. Returns whether it mounted.

    Order-independent, unlike the catch-all this replaced: every route registered here is under
    ``/app`` or is the exact path ``/``, so no API route can be shadowed no matter when a router is
    added. That is the point of the prefix rather than a happy side effect.
    """
    root = web_root if web_root is not None else default_web_root()
    index = root / _INDEX
    if not index.is_file():
        _logger.info(
            "no built dashboard found; serving the API only",
            web_root=str(root),
            hint="run `npm run build` in web/ to produce it",
        )
        return False

    assets = root / "assets"
    if assets.is_dir():
        # Hashed filenames, so they can be cached indefinitely. This is the whole benefit of letting
        # the bundler hash them, and it is why `index.html` is handled separately below.
        app.mount(_ASSET_MOUNT, StaticFiles(directory=assets), name="assets")

    def shell() -> Response:
        """The entry document, explicitly uncached.

        A cached ``index.html`` is how a browser keeps loading last week's JavaScript against this
        week's API after a deploy: the asset filenames change on every build precisely so they *can*
        be cached, and that only works if the document naming them cannot be.
        """
        return FileResponse(index, media_type="text/html", headers={"cache-control": "no-store"})

    @app.get("/", include_in_schema=False)
    def spa_root() -> Response:
        """Send the bare origin to the dashboard.

        A redirect rather than serving the shell here, so there is exactly one URL the application
        runs at. Two working entry points would mean the router's ``basename`` is right for one of
        them and wrong for the other, and the wrong one fails as a blank page with no error.
        """
        return RedirectResponse(url=f"{APP_PREFIX}/", status_code=307)

    @app.get(APP_PREFIX, include_in_schema=False)
    @app.get(f"{APP_PREFIX}/{{client_route:path}}", include_in_schema=False)
    def spa_shell(client_route: str = "") -> Response:
        """Every client route returns the shell and lets the router take over.

        ``client_route`` is accepted and ignored on purpose: which page to show is the router's
        decision, made in the browser from the same URL. The server's only job is to make a hard
        refresh of a deep link work, so that a case link pasted into a ticket opens the case.
        """
        return shell()

    _logger.info("serving dashboard", web_root=str(root), prefix=APP_PREFIX)
    return True

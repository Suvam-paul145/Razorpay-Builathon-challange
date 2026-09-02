"""The static-mount guarantees. One origin means one URL space, and the two halves must not collide.

The dashboard is served from the API process so the two share an origin, which is what lets the
deployment run with **no CORS middleware at all** — a stronger position than an allowlist, because
there is nothing to misconfigure later. The cost is that the SPA's client routes and the API's paths
are drawn from the same space, and they genuinely collide: the dashboard wants ``/metrics`` and
``/cases`` as pages while the API owns ``/metrics/summary`` and ``/cases``.

The resolution is a prefix. ``/app/*`` is the dashboard, everything else is the API, and ``/``
redirects. These tests hold that boundary from both sides — no API path may return HTML, and no
dashboard route may return JSON — and they pin the two cache decisions, which are not cosmetic:
hashed asset filenames may be cached forever *only because* the document naming them is never
cached, and getting that backwards is how a browser keeps running last week's JavaScript against
this week's API after a deploy.

``pure`` because none of it needs a database — the app is built with ``verify_schema=False`` and no
route that touches Postgres is exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from revora.api.app import create_app
from revora.api.spa import APP_PREFIX, default_web_root, mount_spa

pytestmark = pytest.mark.pure

_INDEX_MARKER = '<div id="root">'


@pytest.fixture
def built_dashboard(tmp_path: Path) -> Path:
    """A minimal stand-in for ``web/dist``.

    Constructed rather than depending on a real ``npm run build``: the guarantees under test are
    the routing and the headers, and requiring a Node toolchain to check a Python routing rule
    would mean the check is skipped exactly when someone changes the routing.
    """
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        f"<!doctype html><html><body>{_INDEX_MARKER}</div></body></html>", encoding="utf-8"
    )
    (root / "assets" / "index-abc123.js").write_text("export const x = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def client(built_dashboard: Path) -> TestClient:
    app = create_app(verify_schema=False, serve_dashboard=False)
    assert mount_spa(app, web_root=built_dashboard) is True
    return TestClient(app)


# ---------------------------------------------------------------------------
# The dashboard side
# ---------------------------------------------------------------------------


def test_the_bare_origin_redirects_to_the_dashboard(client: TestClient) -> None:
    """One URL the application runs at, not two.

    Serving the shell at ``/`` as well would give two working entry points, and the router's
    ``basename`` can only be right for one of them — the wrong one fails as a blank page with no
    error, which is the hardest kind of misconfiguration to diagnose.
    """
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"{APP_PREFIX}/"


@pytest.mark.parametrize(
    "route",
    [
        APP_PREFIX,
        f"{APP_PREFIX}/",
        f"{APP_PREFIX}/metrics",
        f"{APP_PREFIX}/cases",
        f"{APP_PREFIX}/cases/00000000-0000-0000-0000-000000000000",
        f"{APP_PREFIX}/unresolved",
        f"{APP_PREFIX}/experiments",
        f"{APP_PREFIX}/consent",
        f"{APP_PREFIX}/anything/the/router/owns",
    ],
)
def test_every_client_route_receives_the_shell(client: TestClient, route: str) -> None:
    """A deep link must survive a hard refresh.

    ``/app/cases/<uuid>`` is a router path, not a server path. A browser loading it directly has to
    receive ``index.html`` and let the router decide what to show, or every case link an operator
    pastes into a ticket is broken.
    """
    response = client.get(route)
    assert response.status_code == 200, route
    assert _INDEX_MARKER in response.text


def test_the_shell_is_never_cached(client: TestClient) -> None:
    """The entry document must not be cached, and this is why the assets can be.

    Asset filenames carry a content hash, so they are immutable and cacheable forever. That only
    holds if the document naming them is fetched fresh — a cached ``index.html`` points at last
    deploy's hashes, and the browser then runs old JavaScript against a new API, which presents as
    inexplicable field-shape errors rather than as a caching problem.
    """
    response = client.get(f"{APP_PREFIX}/metrics")
    assert response.headers["cache-control"] == "no-store"


def test_assets_are_served(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "export const x" in response.text


# ---------------------------------------------------------------------------
# The API side
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/cases",
        "/cases/00000000-0000-0000-0000-000000000000",
        "/cases/00000000-0000-0000-0000-000000000000/audit",
        "/metrics/summary",
        "/metrics/unresolved",
        "/experiments",
        "/consent",
        "/auth/sessions",
        "/health",
        "/health/webhook",
        "/webhooks/razorpay/some-slug",
        "/openapi.json",
    ],
)
def test_no_api_path_ever_answers_with_the_dashboard_shell(client: TestClient, path: str) -> None:
    """The collision this prefix exists to prevent, asserted path by path.

    Responses vary — 401 unauthenticated, 405 on the wrong method, 200 for the schema — and that
    variety is fine. What must never happen is the *shell* coming back, because a fetch that
    receives HTML with status 200 reports a JSON parse failure from inside a component, several
    layers away from the path that caused it.

    An earlier version of this module used a catch-all with a list of API prefixes to refuse. It got
    this half right and the dashboard half wrong: ``/metrics`` answered 404 instead of the app.
    Prefixing the SPA makes both sides structural rather than list-maintained.
    """
    response = client.get(path)
    assert _INDEX_MARKER not in response.text, (
        f"{path} was answered with the dashboard shell; an API path returning HTML turns a "
        "mistyped fetch into a parse error somewhere unrelated"
    )


def test_an_unknown_top_level_path_is_a_404(client: TestClient) -> None:
    """Nothing outside ``/app`` is claimed by the dashboard.

    With the prefix in place there is no catch-all at all, so an unknown path is a plain 404 and a
    typo in a fetch URL surfaces immediately rather than as HTML that fails to parse.
    """
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert _INDEX_MARKER not in response.text


# ---------------------------------------------------------------------------
# The two halves must agree
# ---------------------------------------------------------------------------


def test_the_router_basename_matches_the_server_prefix() -> None:
    """``APP_PREFIX`` and the router's ``basename`` are one value in two languages.

    A mismatch does not raise anywhere. The server returns the shell, the router sees a path outside
    its basename, matches nothing, and renders nothing — a blank page with a clean console. This is
    the cheapest possible check for the most confusing possible failure.
    """
    main_jsx = Path(__file__).resolve().parents[2] / "web" / "src" / "main.jsx"
    if not main_jsx.is_file():  # pragma: no cover - the frontend is present in this repo
        pytest.skip("web/src/main.jsx not present")
    source = main_jsx.read_text(encoding="utf-8")
    found = re.search(r'basename="([^"]*)"', source)
    assert found is not None, "BrowserRouter has no basename; deep links will not resolve"
    assert found.group(1) == APP_PREFIX, (
        f"the router's basename {found.group(1)!r} does not match APP_PREFIX {APP_PREFIX!r}; "
        "the dashboard will render as a blank page with no error"
    )


def test_nothing_is_mounted_when_the_dashboard_has_not_been_built(tmp_path: Path) -> None:
    """An API-only deployment must be unaffected.

    Returning ``False`` rather than raising, because "the frontend has not been built" is a normal
    state for a backend-only run and for most of this suite. A half-configured mount answering 404
    for every asset looks like a broken deployment; not mounting looks like what it is.
    """
    app = create_app(verify_schema=False, serve_dashboard=False)
    assert mount_spa(app, web_root=tmp_path / "absent") is False
    client = TestClient(app)
    assert client.get(f"{APP_PREFIX}/metrics").status_code == 404
    assert client.get("/", follow_redirects=False).status_code == 404


def test_the_default_web_root_points_at_the_repository_build_directory() -> None:
    """Pins the path arithmetic, which is three ``parent`` hops and easy to get wrong silently.

    A wrong default does not raise — it simply never finds a build, so the dashboard is quietly
    absent and the API looks fine. That is the failure this asserts against.
    """
    root = default_web_root()
    assert root.name == "dist"
    assert root.parent.name == "web"
    assert (root.parent.parent / "pyproject.toml").is_file(), (
        f"default_web_root resolved to {root}, which is not beside the project's pyproject.toml"
    )

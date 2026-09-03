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


# ---------------------------------------------------------------------------
# The second entry: the customer response page
# ---------------------------------------------------------------------------
#
# ``web/`` builds two bundles. ``dist/`` is the dashboard, mounted above under ``APP_PREFIX``;
# ``dist-customer/`` is the customer response page, served by the frontend host at ``/pay/*`` and
# **not mounted here at all** — ``mount_spa`` is unchanged and keeps ``/app``. A second mount at
# ``/pay`` for a single-host deployment would follow the same pattern and is not built.
#
# The two entries exist because a ``/pay/:token`` route inside the dashboard SPA would ship an
# unauthenticated stranger the entire administrative surface as readable source. Nothing in it is a
# secret, but it is a map.
#
# What is asserted here is the one thing the frontend's own tests cannot assert as cheaply: that the
# customer entry has not acquired a router. The ``basename`` test above exists because a mismatch
# between the router's basename and the server's prefix renders a blank page with a clean console
# and no error; the customer entry answers that by having no router, and this keeps it answered.

_CUSTOMER_DIR = "customer"

_ROUTER_PACKAGES = re.compile(r"react-router|@tanstack/react-query")

_IMPORT_SPECIFIER = re.compile(r"""^\s*import\s[^\n]*?['"]([^'"]+)['"]""", re.MULTILINE)


def _customer_entry_root() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "src" / _CUSTOMER_DIR


def _customer_modules() -> list[Path]:
    """The customer entry's own modules, excluding its tests.

    Tests are excluded because they *name* the forbidden packages in order to assert their
    absence, and a check that could not tell a citation from an import would be a check nobody
    can write an assertion about.
    """
    root = _customer_entry_root()
    return sorted(
        path
        for path in root.glob("*.js*")
        if path.suffix in {".js", ".jsx"} and ".test." not in path.name
    )


def test_the_customer_entry_imports_no_router() -> None:
    """The customer page has no router, so it has no ``basename`` to disagree with anything.

    Read over **import specifiers** rather than over the source text, and the distinction is
    load-bearing: those modules document at length why they carry neither ``react-router-dom``
    nor ``@tanstack/react-query``, so a substring search would match the prose explaining the
    absence and the check would fail on the very comment that justifies it.

    Reintroducing a router here is not forbidden — it is required to come with an answer to the
    question this test stands in for, which is what the basename test above pays for the hard way.
    """
    modules = _customer_modules()
    if not modules:  # pragma: no cover - the frontend is present in this repo
        pytest.skip("web/src/customer is not present")

    # Anti-vacuity. Three modules — the entry, the page and the API client — so a glob that
    # quietly matched nothing is a failure rather than a pass.
    assert len(modules) >= 3, f"expected the customer entry's three modules, found {modules}"

    specifiers: list[str] = []
    for module in modules:
        for specifier in _IMPORT_SPECIFIER.findall(module.read_text(encoding="utf-8")):
            specifiers.append(specifier)
            assert not _ROUTER_PACKAGES.search(specifier), (
                f"{module.name} imports {specifier!r}; the customer entry uses no router and no "
                "query client, which is what removes the basename failure mode rather than "
                "adding a second instance of it"
            )

    # The other half of the anti-vacuity check: the pattern found real specifiers. Asserted across
    # the entry rather than per file, because ``api.js`` imports nothing at all.
    assert "react" in specifiers, f"no import of react found in {[m.name for m in modules]}"
    assert "react-dom/client" in specifiers


def test_the_customer_entry_reads_its_token_from_the_path() -> None:
    """The positive half. No router *and* the token comes out of ``window.location.pathname``.

    Without this, the test above is satisfied by a file that imports nothing and does nothing. The
    token is read from the path rather than a query string because a query string is where URLs
    leak — into referrers, access logs and any third-party script on the page — and this URL is a
    bearer credential, which is also why the API sends ``referrer-policy: no-referrer`` and
    ``base-uri 'none'`` on every customer response.
    """
    entry = _customer_entry_root() / "main.jsx"
    if not entry.is_file():  # pragma: no cover - the frontend is present in this repo
        pytest.skip("web/src/customer/main.jsx not present")
    source = entry.read_text(encoding="utf-8")
    assert "window.location.pathname" in source, (
        "the customer entry does not read window.location.pathname; with no router, that is the "
        "only place its token can come from"
    )


def test_the_customer_page_is_not_mounted_on_the_api(client: TestClient) -> None:
    """``/pay/*`` belongs to the frontend host, and the API says so by 404ing.

    Pinned rather than left implicit. The assumed deployment serves the customer page from the
    frontend host, so an API that answered here would mean a second, undeclared static mount had
    appeared — and the customer document carries its own stricter CSP, which the dashboard's mount
    does not send.
    """
    for path in ("/pay", "/pay/", "/pay/acme-tools/rvc_token"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert _INDEX_MARKER not in response.text, (
            f"{path} was answered with the dashboard shell; the customer page is a separate "
            "bundle with a separate document and a separate policy"
        )

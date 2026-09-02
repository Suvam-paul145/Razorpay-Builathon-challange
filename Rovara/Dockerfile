# One image, two process roles. The API container and the worker container run the
# identical build; REVORA_ROLE selects the entrypoint at runtime. Dependency parity
# between the process that receives a webhook and the process that calls the provider
# is the point — there is no drift class of bug where the worker has a different
# library version than the API.
#
# Multi-stage so the runtime image does not carry build tooling or the test/dev
# extras. The estimation ML extra is installed because the worker needs it; the API
# does not, but one image means one dependency set.
#
# Three stages, because the dashboard is built here too. The SPA is served by the API
# process itself so the two share an origin — which is what lets the deployment run
# with no CORS middleware at all, a stronger position than an allowlist because there
# is nothing to misconfigure later. Building it in the image rather than committing
# `web/dist` keeps one source of truth for what is deployed, and Node never reaches
# the runtime image.


FROM node:22-slim AS web

WORKDIR /web

# Lockfile first, so the dependency layer survives a source-only change. `npm ci`
# rather than `npm install`: it installs exactly the lockfile and fails if the two
# have drifted, which is the behaviour a build wants and `install` does not have.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/jsconfig.json web/vite.config.js web/eslint.config.js web/index.html ./
COPY web/src ./src

# The dashboard is plain JavaScript, so there is no type check to fail the build on.
# The checks that guard a money figure are eslint rules — no arithmetic on a `.minor`
# field, no client-side currency formatting, no `?? 0` on a figure — so lint runs here
# and a violation fails the image rather than reaching a merchant.
RUN npm run lint && npm run build


FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy only what pip needs to resolve dependencies first, so the dependency layer
# is cached across source changes.
COPY pyproject.toml ./
COPY revora ./revora

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[ml]"


FROM python:3.12-slim AS runtime

# A non-root runtime user. A process that holds payment credentials and can create
# payment links should not also be able to rewrite its own code.
RUN groupadd --system revora && useradd --system --gid revora --create-home revora

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/revora ./revora
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# The built dashboard. `revora.api.spa.default_web_root` looks for `web/dist` beside
# the package, which this layout satisfies — so no environment variable is needed for
# the normal case, and `REVORA_WEB_ROOT` stays available for an unusual one.
#
# Static assets only: no `node_modules`, no source, no Node runtime. The worker role
# carries these bytes too and never serves them, which is the cost of one image and a
# smaller cost than two images that can drift apart.
COPY --from=web /web/dist ./web/dist

USER revora

# The entrypoint dispatches on REVORA_ROLE. It is defined inline rather than as a
# script file so the image has no shell wrapper to keep in sync with role.py — the
# authoritative role list is revora/platform/role.py, and an unknown value there
# fails loudly rather than defaulting to api.
#
#   api    -> uvicorn serving the FastAPI app + the dashboard (revora.api.main)
#   worker -> the job-queue poll loop (revora.jobs.main)
#
# Both are dedicated `main` modules that nothing else imports. The worker used to be
# `revora.jobs.worker`, which was wrong twice over: that module has no __main__ block
# so it imported and exited 0 — a worker that appeared to start and was not there —
# and `revora/jobs/__init__.py` imports it, so running it with -m loaded it twice and
# Python warned about unpredictable behaviour from the duplicated module state.
#
# The find_spec guard stays: it turns a renamed or removed entrypoint into a clear
# message at container start rather than a traceback from runpy, for one import check.
ENTRYPOINT ["python", "-c", "import sys; from revora.platform.role import current_role, Role; r = current_role(); print(f'revora role={r.value}', flush=True); \
mod = 'revora.api.main' if r is Role.API else 'revora.jobs.main'; \
import importlib, runpy; \
(runpy.run_module(mod, run_name='__main__') if importlib.util.find_spec(mod) else print(f'entrypoint {mod} not found', flush=True))"]

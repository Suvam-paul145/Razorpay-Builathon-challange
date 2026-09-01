# One image, two process roles. The API container and the worker container run the
# identical build; REVORA_ROLE selects the entrypoint at runtime. Dependency parity
# between the process that receives a webhook and the process that calls the provider
# is the point — there is no drift class of bug where the worker has a different
# library version than the API.
#
# Multi-stage so the runtime image does not carry build tooling or the test/dev
# extras. The estimation ML extra is installed because the worker needs it; the API
# does not, but one image means one dependency set.

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

USER revora

# The entrypoint dispatches on REVORA_ROLE. It is defined inline rather than as a
# script file so the image has no shell wrapper to keep in sync with role.py — the
# authoritative role list is revora/platform/role.py, and an unknown value there
# fails loudly rather than defaulting to api.
#
#   api    -> uvicorn serving the FastAPI app (webhook intake + dashboard API)
#   worker -> the job-queue poll loop
#
# Both entrypoint modules now exist. The find_spec guard stays: it is what turns a
# renamed or removed entrypoint into a clear message at container start rather than a
# traceback from runpy, and it costs one import check.
ENTRYPOINT ["python", "-c", "import sys; from revora.platform.role import current_role, Role; r = current_role(); print(f'revora role={r.value}', flush=True); \
mod = 'revora.api.main' if r is Role.API else 'revora.jobs.worker'; \
import importlib, runpy; \
(runpy.run_module(mod, run_name='__main__') if importlib.util.find_spec(mod) else print(f'entrypoint {mod} not built yet (foundation phase)', flush=True))"]

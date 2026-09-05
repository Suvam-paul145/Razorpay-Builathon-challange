"""Which of the three process roles this container is running.

One Docker image, three entrypoints, selected by ``REVORA_ROLE``. The reason for one
image is dependency parity: the process that receives a webhook and the process
that calls the provider have the identical dependency graph, so there is no drift
class of bug where the worker has a different library version than the API.

**The ticker is a third entrypoint, not a third dependency graph.** It runs the same
image as the other two and imports strictly less than the worker does — it enqueues
rows and reclaims leases, and touches no provider. Widening this enum is therefore
the whole of its deployment cost: no second build, no second requirements set, no
second thing that can drift.

**Why the schedule is its own role at all.** ``revora.jobs.ticker`` explains the
choice against the two alternatives at length; the short version is that a loop
inside the worker process runs N times across N replicas, and a cron entry is the
hardest of the three to observe. A sidecar gives the schedule exactly one owner,
which matters because the sweeps are dedupe-keyed by interval bucket: a *double*
tick is harmless by construction, and a *missing* tick is invisible.

An unknown value fails loudly at startup. The alternative — defaulting to ``api``
— produces a deployment with two API processes, no worker, and a queue that fills
up silently while every webhook still returns 200. That failure is invisible for
hours and looks like "recovery stopped working" rather than "the role was
misspelt". The same argument applies with more force to the ticker: a misspelt
``ticker`` that defaulted to ``api`` would leave every periodic sweep unenqueued,
and the only symptom would be cases that never expire and intents that never
reconcile — which reads as a bug in the sweeps rather than as an absent clock.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum, unique
from typing import Final

__all__ = ["ENV_ROLE", "Role", "RoleConfigurationError", "current_role"]

ENV_ROLE: Final[str] = "REVORA_ROLE"


@unique
class Role(StrEnum):
    """The three process roles."""

    API = "api"
    WORKER = "worker"
    TICKER = "ticker"
    """The schedule. Produces the periodic sweep jobs the worker consumes, and
    reclaims the leases of jobs a dead worker left ``RUNNING``. Exactly one
    replica is intended; see :mod:`revora.jobs.ticker`."""


class RoleConfigurationError(RuntimeError):
    """``REVORA_ROLE`` is absent or not one of the known roles."""


def current_role(environ: Mapping[str, str] | None = None) -> Role:
    """Read ``REVORA_ROLE``.

    Case and surrounding whitespace are forgiven — ``"API"`` and ``" worker "`` are
    unambiguous, and refusing them would be pedantry that costs a deploy. Anything
    else is refused with the permitted values named, because the most likely cause
    is a typo and the message should be enough to fix it without reading this file.

    Raises:
        RoleConfigurationError: if unset, blank, or unrecognised.
    """
    source = os.environ if environ is None else environ
    raw = source.get(ENV_ROLE)
    if raw is None or not raw.strip():
        raise RoleConfigurationError(f"{ENV_ROLE} is not set; expected one of {_permitted()}")
    normalized = raw.strip().lower()
    try:
        return Role(normalized)
    except ValueError as exc:
        raise RoleConfigurationError(
            f"{ENV_ROLE}={raw.strip()!r} is not a known role; expected one of {_permitted()}"
        ) from exc


def _permitted() -> str:
    return ", ".join(role.value for role in Role)

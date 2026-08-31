"""Which of the two process roles this container is running.

One Docker image, two entrypoints, selected by ``REVORA_ROLE``. The reason for one
image is dependency parity: the process that receives a webhook and the process
that calls the provider have the identical dependency graph, so there is no drift
class of bug where the worker has a different library version than the API.

An unknown value fails loudly at startup. The alternative — defaulting to ``api``
— produces a deployment with two API processes, no worker, and a queue that fills
up silently while every webhook still returns 200. That failure is invisible for
hours and looks like "recovery stopped working" rather than "the role was
misspelt".
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
    """The two process roles."""

    API = "api"
    WORKER = "worker"


class RoleConfigurationError(RuntimeError):
    """``REVORA_ROLE`` is absent or not one of the two known roles."""


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

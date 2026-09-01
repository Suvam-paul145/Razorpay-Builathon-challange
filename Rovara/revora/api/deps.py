"""One name for the authenticated-session dependency, and why it is a name at all.

``Session = Annotated[AuthenticatedSession, Depends(authenticate)]`` written once, imported
everywhere. Two reasons, and the second is the one that matters.

**It is the modern FastAPI form.** ``current: AuthenticatedSession = Depends(authenticate)`` puts a
function call in a default argument, which every linter flags and which is genuinely surprising —
the call is evaluated once at import and the framework then reinterprets the resulting sentinel.
``Annotated`` keeps the default slot empty and the dependency in the type, where it belongs.

**Every authenticated endpoint declares the same thing, so a missing one is visible.** An endpoint
that forgot the parameter has no session, and therefore no ``merchant_id`` — which under R17.C2 is
not a degraded endpoint, it is an unauthenticated one. Reviewing a router for ``TenantSession`` in
every signature is a glance; reviewing it for a correctly-spelled ``Depends`` call in every default
is not.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from revora.api.auth import AuthenticatedSession, authenticate

__all__ = ["TenantSession"]

TenantSession = Annotated[AuthenticatedSession, Depends(authenticate)]
"""The authenticated session. Present on every endpoint except the webhook and ``GET /health``."""

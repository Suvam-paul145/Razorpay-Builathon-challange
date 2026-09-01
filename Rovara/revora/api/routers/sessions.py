"""Session minting and revocation. The only endpoints that do not require a session.

``POST /auth/sessions`` presents a per-merchant operator key and receives a bearer token.
``DELETE /auth/session`` ends the calling session.

The token is returned in the response body exactly once and never again — only its keyed digest is
stored. It is not set as a cookie, deliberately: a cookie would make every dashboard request a
cross-site-request-forgery target and would need a CSRF token, a ``SameSite`` policy and an origin
allowlist to be safe. A bearer token held in memory by a single-page app has none of those failure
modes, at the cost of not surviving a page reload — for an operator console that is the right trade.

Both handlers are ``def`` rather than ``async def``. Every call below is synchronous SQLAlchemy, and
an ``async def`` handler doing blocking I/O occupies the event loop for the whole query; FastAPI
runs a sync handler in a worker thread instead, which is what keeps one slow query from stalling
every other request in the process.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field

from revora.api.auth import DASHBOARD_KEY_HEADER, mint_session, revoke_session
from revora.api.deps import TenantSession

__all__ = ["router"]

router = APIRouter(tags=["sessions"])


class SessionRequest(BaseModel):
    """What a caller sends to mint a session.

    No merchant *id* field, and that is the point: the id is resolved from the slug against the
    ``merchant`` table, so a caller cannot name a tenant it has no key for. R17.C2 says a merchant
    id in a payload is ignored, and the cleanest way to ignore one is to have nowhere to put it.
    """

    merchant_slug: str = Field(min_length=1, max_length=200)
    email_key: str | None = Field(default=None, max_length=200)
    """The keyed hash of the user to act as. Omit for a single-operator deployment, where the
    merchant's oldest active user is used — still a named user, because every action needs an
    actor."""


class SessionResponse(BaseModel):
    """The minted session. ``token`` appears here and nowhere else, ever."""

    token: str
    session_id: str
    merchant_user_id: str
    expires_at: str


@router.post(
    "/auth/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={401: {"description": "The operator key did not verify."}},
)
def create_session(
    body: SessionRequest,
    dashboard_key: Annotated[str | None, Header(alias=DASHBOARD_KEY_HEADER)] = None,
) -> SessionResponse:
    """Mint a session after verifying the merchant's operator key.

    401 with no body on every failure — wrong key, unknown slug, no active user. One answer,
    because distinguishing them makes this endpoint an oracle for which merchants exist.
    """
    minted = mint_session(
        body.merchant_slug,
        presented_key=dashboard_key,
        email_key=body.email_key,
    )
    return SessionResponse(
        token=minted.token,
        session_id=str(minted.session_id),
        merchant_user_id=str(minted.merchant_user_id),
        expires_at=minted.expires_at.isoformat(),
    )


class RevocationResponse(BaseModel):
    revoked: bool
    detail: str


@router.delete("/auth/session", response_model=RevocationResponse)
def delete_session(
    current: TenantSession,
) -> RevocationResponse:
    """End the calling session.

    Answers 200 whether or not the session was still live. ``revoked`` distinguishes them, and the
    reason it is a field rather than a status code is that "already logged out" is the desired end
    state — answering 409 would make a double-click look like an error to a user who got exactly
    what they asked for.
    """
    revoked = revoke_session(current)
    return RevocationResponse(
        revoked=revoked,
        detail=(
            "session revoked"
            if revoked
            else "session was already revoked or expired; nothing to do"
        ),
    )

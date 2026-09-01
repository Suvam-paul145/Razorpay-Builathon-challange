"""Merchant users and their dashboard sessions.

Two repositories, and the session one is the more interesting of them because it is read on
every single API request. Everything here is scoped by ``merchant_id`` like every other read in
this package, including the session lookup — which is worth pausing on, because a session lookup
scoped to a merchant looks circular: how can it be scoped by the merchant it is meant to
establish?

The answer is that the token *claims* a merchant and this layer *verifies* the claim. The token
carries the merchant slug as a prefix; the API resolves that slug to an id against the ``merchant``
table (the one table with no tenant scope, because it is the tenant), and only then looks the
token digest up inside that merchant. A token whose claimed merchant is wrong finds no row. So the
scoping is not circular, it is the check: authentication fails closed rather than reading another
tenant's session row and believing it.

The token itself never appears here. Callers pass a digest.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import update

from revora.persistence.models.tenancy import MerchantSession, MerchantUser
from revora.persistence.repositories.base import MerchantScopedRepository, rows_affected

__all__ = ["MerchantSessionRepository", "MerchantUserRepository"]


class MerchantUserRepository(MerchantScopedRepository[MerchantUser]):
    """Dashboard users. The subject of an approving-user record and of every session."""

    model = MerchantUser

    def by_email_key(self, merchant_id: uuid.UUID, email_key: str) -> MerchantUser | None:
        """One user by the keyed hash of their address.

        The cleartext address is never stored, so this is the only way to find a user from
        something a person could type. ``None`` for both "no such user" and "not this
        merchant's user", which is the same answer for the same reason it is at the HTTP
        boundary: distinguishing them would make the endpoint an oracle for who has an account.
        """
        statement = self.scoped(merchant_id).where(MerchantUser.email_key == email_key)
        return self.session.execute(statement).scalar_one_or_none()

    def list_active(self, merchant_id: uuid.UUID, *, limit: int) -> Sequence[MerchantUser]:
        """This merchant's active users, oldest first."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(MerchantUser.is_active)
            .order_by(MerchantUser.created_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def record_login(
        self, merchant_id: uuid.UUID, user_id: uuid.UUID, *, moment: datetime
    ) -> None:
        """Stamp ``last_login_at``. Not an audit record — a convenience for the user list.

        The audit trail is where "a session was established" actually lives, because this
        column is overwritten and an overwritable field cannot be evidence of anything.
        """
        self.session.execute(
            update(MerchantUser)
            .where(MerchantUser.merchant_id == merchant_id, MerchantUser.id == user_id)
            .values(last_login_at=moment)
        )


class MerchantSessionRepository(MerchantScopedRepository[MerchantSession]):
    """Dashboard sessions. Read on every request, so the reads here are single-index lookups."""

    model = MerchantSession

    def insert(
        self,
        merchant_id: uuid.UUID,
        *,
        merchant_user_id: uuid.UUID,
        token_digest: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> MerchantSession:
        """Mint one session and flush so its id is available for the audit record."""
        row = MerchantSession(
            merchant_user_id=merchant_user_id,
            token_digest=token_digest,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def live_by_digest(
        self, merchant_id: uuid.UUID, token_digest: str, *, moment: datetime
    ) -> MerchantSession | None:
        """A session that is present, unrevoked and unexpired, or ``None``.

        The three failure modes collapse into one ``None`` here and into one 401 at the
        boundary, because they get the same answer. The *caller* re-reads the row through
        :meth:`by_digest` to record which of them it was, so the audit trail keeps the
        distinction the response deliberately hides.

        Expiry is compared against the stored ``expires_at`` rather than recomputed from
        ``SESSION_LIFETIME``, so shortening the bound cannot retroactively invalidate a session
        that was legitimately issued under the old one.
        """
        statement = self.scoped(merchant_id).where(
            MerchantSession.token_digest == token_digest,
            MerchantSession.revoked_at.is_(None),
            MerchantSession.expires_at > moment,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def by_digest(self, merchant_id: uuid.UUID, token_digest: str) -> MerchantSession | None:
        """A session by digest whatever its state, for the audit record's detail."""
        statement = self.scoped(merchant_id).where(
            MerchantSession.token_digest == token_digest
        )
        return self.session.execute(statement).scalar_one_or_none()

    def touch(
        self, merchant_id: uuid.UUID, session_id: uuid.UUID, *, moment: datetime
    ) -> None:
        """Record that the session was used.

        ``last_seen_at`` only; the expiry is absolute rather than sliding. A sliding window
        would mean a session left open in a browser tab never expires, which turns
        ``SESSION_LIFETIME`` into a description of idle time rather than of session age —
        and R17.C1 bounds the age.
        """
        self.session.execute(
            update(MerchantSession)
            .where(
                MerchantSession.merchant_id == merchant_id,
                MerchantSession.id == session_id,
            )
            .values(last_seen_at=moment)
        )

    def revoke(
        self, merchant_id: uuid.UUID, session_id: uuid.UUID, *, moment: datetime
    ) -> bool:
        """End one session. ``True`` if it was live, ``False`` if it was already ended.

        The boolean matters: revoking twice must not write two audit records claiming two
        revocations of the same session.
        """
        return rows_affected(
            self.session.execute(
                update(MerchantSession)
                .where(
                    MerchantSession.merchant_id == merchant_id,
                    MerchantSession.id == session_id,
                    MerchantSession.revoked_at.is_(None),
                )
                .values(revoked_at=moment)
            )
        ) > 0

    def revoke_all_for_user(
        self, merchant_id: uuid.UUID, merchant_user_id: uuid.UUID, *, moment: datetime
    ) -> int:
        """End every live session for one user. Returns how many were ended."""
        return rows_affected(
            self.session.execute(
                update(MerchantSession)
                .where(
                    MerchantSession.merchant_id == merchant_id,
                    MerchantSession.merchant_user_id == merchant_user_id,
                    MerchantSession.revoked_at.is_(None),
                )
                .values(revoked_at=moment)
            )
        )

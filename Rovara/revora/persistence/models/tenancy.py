"""The tenant, its users, its signing secrets, and customer consent.

``merchant`` is the only table without a ``merchant_id``, because it *is* the
tenant. Everything else hangs off it, and the foreign key is ``ON DELETE
RESTRICT`` rather than ``CASCADE``: deleting a merchant would take its audit
records with it, and an audit log that can be removed by deleting a parent row is
not an audit log.

``customer_consent`` is keyed on ``customer_key``, not on a case. An opt-out is a
statement about a person, so it has to suppress contact on every case of that
person — the one recorded against a failed payment in March must still hold for a
different failed payment in June (R17.C10, and what P8 checks). ``customer_key``
is the keyed, non-reversible hash from ``platform.crypto``, so this table can join
across cases without holding a contact list.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CHAR, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.persistence.models.base import (
    TIMESTAMPTZ,
    Base,
    CreatedAtMixin,
    IdMixin,
    RowBase,
)

__all__ = [
    "CustomerConsent",
    "Merchant",
    "MerchantSession",
    "MerchantUser",
    "WebhookSecret",
]


class Merchant(Base, IdMixin, CreatedAtMixin):
    """A tenant. The root of every isolation mechanism in the schema."""

    __tablename__ = "merchant"

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    """Stable, URL-safe identifier. Also the suffix of the environment variable
    holding this merchant's webhook secrets, which is why it must not change."""

    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    default_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="INR")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")
    reporting_timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    """Presentation only. Every stored instant is UTC; this is how a daily cohort
    boundary is drawn for display."""

    __table_args__ = (
        # A slug appears in configuration keys and in operator commands. Two
        # merchants sharing one would send a webhook to the wrong tenant.
        UniqueConstraint("slug", name="uq_merchant_slug"),
    )


class MerchantUser(RowBase):
    """A dashboard user. The subject of an approving-user record on a promotion.

    Holds a masked email rather than the address: the only thing the system does
    with it is display it and match a session, and a table of real addresses is a
    liability with no compensating use.
    """

    __tablename__ = "merchant_user"

    email_masked: Mapped[str] = mapped_column(Text, nullable=False)
    email_key: Mapped[str] = mapped_column(Text, nullable=False)
    """Keyed hash of the normalized address. What a login looks up on, so the
    cleartext never has to be stored to find the row."""

    role: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)

    __table_args__ = (
        # Scoped to the merchant, not global: the same person may hold accounts
        # with two merchants and they are two users.
        UniqueConstraint("merchant_id", "email_key", name="uq_merchant_user_merchant_id_email_key"),
    )


class MerchantSession(RowBase):
    """One authenticated dashboard session. The only thing an API request's tenant comes from.

    **Stored server-side rather than carried in a signed token**, and the choice is not
    incidental. A stateless token cannot be revoked, and R17 wants a session that stops being
    valid — on expiry, on logout, and on an operator deciding it should. A row can be revoked;
    a signature cannot be un-signed. It also means ``merchant_id`` is read from a row this
    system wrote, which is the literal form of "the merchant is derived from the session and
    from nothing in the request".

    **The token is never stored.** ``token_digest`` is a keyed HMAC of the bearer token under
    ``session_token_secret``. A database disclosure therefore does not hand over live sessions,
    and the digest is keyed rather than plain so an offline attacker with the table cannot
    confirm a guessed token without also holding the secret.

    ``expires_at`` is persisted rather than derived from ``SESSION_LIFETIME`` at read time, for
    the same reason ``recovery_case.window_end_at`` is: shortening a configured bound must not
    retroactively invalidate sessions that were legitimately issued under the old one, and
    lengthening it must not silently resurrect expired ones.
    """

    __tablename__ = "merchant_session"

    merchant_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT"), nullable=False
    )
    """Who the session acts as. Required even though the credential that minted it is a
    per-merchant operator key rather than a user password — an audit trail that cannot name an
    actor is not an audit trail, and "the operator key" is not an actor."""

    token_digest: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)

    __table_args__ = (
        # Global rather than per-merchant. The digest is keyed and 64 hex characters, so a
        # collision is not a practical concern — but the lookup happens before the tenant is
        # established beyond what the token claims, and a per-merchant constraint would leave
        # the same digest usable against two merchants.
        UniqueConstraint("token_digest", name="uq_merchant_session_token_digest"),
        Index("ix_merchant_session_merchant_id_expires_at", "merchant_id", "expires_at"),
    )


class WebhookSecret(RowBase):
    """A webhook signing secret's rotation record — a reference, never the value.

    The value lives in the platform's secret storage; this row records which
    reference is active and when it was retired. Both timestamps matter because
    the provider redelivers a failed event for up to 24 hours: a retired secret
    has to keep verifying until that window closes, or an in-flight retry of a
    real payment failure gets answered as a forgery. See ``platform.secrets``.
    """

    __tablename__ = "webhook_secret"

    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    rotated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "secret_ref", name="uq_webhook_secret_merchant_id_secret_ref"
        ),
        # Verification reads every active secret for the merchant on every inbound
        # request, and that read is inside the ingest acknowledgement budget.
        Index("ix_webhook_secret_merchant_id_activated_at", "merchant_id", "activated_at"),
    )


class CustomerConsent(RowBase):
    """Whether a customer may be contacted, keyed on the customer.

    The policy engine reads this on every decision, before any customer-visible
    action is authorized. It is the second of the twelve checks that can never be
    reached late, which is why the index below exists.
    """

    __tablename__ = "customer_consent"

    customer_key: Mapped[str] = mapped_column(Text, nullable=False)
    """Keyed hash of the normalized contact. Not reversible — this column is the
    most widely spread customer-derived value in the database, and a reversible
    one would make it a contact list."""

    opted_out: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    source: Mapped[str | None] = mapped_column(Text)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    consent_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    """Consent is not perpetual. A missing expiry means indefinite; a past one
    means the ``CONSENT_MISSING`` check fails, which blocks rather than assumes."""

    __table_args__ = (
        # Reason: opt-out check in the policy hot path. Every decision that could
        # produce a customer-visible action does this lookup.
        Index("ix_customer_consent_merchant_id_customer_key", "merchant_id", "customer_key"),
    )

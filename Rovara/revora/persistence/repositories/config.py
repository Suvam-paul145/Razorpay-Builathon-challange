"""Loading the configured bounds, with the seeded defaults as the fallback.

``platform.config`` declares the bounds and the typed accessor and knows nothing
about a database — it sits below persistence, so it cannot import it. This module is
the join: it reads the ``app_config`` rows and hands the raw values to
``Configuration.from_values``.

The lookup is two-layer. A merchant's own active row wins; the sentinel tenant's
seeded row is the fallback. That is what makes the seed migration possible at all —
there is no merchant to attach defaults to when the schema is first created — and it
means a merchant who has changed nothing still gets a complete, versioned
configuration rather than a partial one plus code defaults.

``version`` is taken from the merchant's own rows where any exist, because that is
the version a policy decision should cite. A merchant running entirely on defaults
cites the defaults' version, which is honest: nothing was chosen for them.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from revora.persistence.models import AppConfig
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.platform.config import (
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    Configuration,
)

__all__ = ["ConfigurationRepository"]


class ConfigurationRepository(MerchantScopedRepository[AppConfig]):
    """Reads the active configuration for one merchant."""

    model = AppConfig

    def load(self, merchant_id: uuid.UUID) -> Configuration:
        """The effective configuration for one merchant.

        Never partial: any bound with no row anywhere falls back to its catalogue
        placeholder and is named in ``Configuration.defaulted_keys``, so a missing
        seed shows up as a reportable fact instead of a wrong bound.
        """
        own = self._active_values(merchant_id)
        defaults = self._active_values(DEFAULTS_MERCHANT_ID)
        merged = {**defaults, **own}
        version = self._version(merchant_id, own) or DEFAULT_CONFIG_VERSION
        return Configuration.from_values(merged, version=version)

    def load_strict(self, merchant_id: uuid.UUID) -> Configuration:
        """As :meth:`load`, but refuse when any bound has no row at all.

        For the API and worker bootstrap. A process that starts with a defaulted
        bound is a process whose behaviour does not match the recorded
        configuration, and that discrepancy is invisible until someone asks why a
        case stopped after three attempts.
        """
        own = self._active_values(merchant_id)
        defaults = self._active_values(DEFAULTS_MERCHANT_ID)
        merged = {**defaults, **own}
        version = self._version(merchant_id, own) or DEFAULT_CONFIG_VERSION
        return Configuration.from_values(merged, version=version, strict=True)

    def active_rows(self, merchant_id: uuid.UUID) -> Mapping[str, AppConfig]:
        """The active rows for one merchant, by key.

        For the settings screen, which shows the value, the version, whether it is
        still an assumption placeholder, and who approved it.
        """
        statement = self.scoped(merchant_id).where(AppConfig.is_active)
        return {row.key: row for row in self.session.execute(statement).scalars()}

    def _active_values(self, merchant_id: uuid.UUID) -> dict[str, str]:
        statement = select(AppConfig.key, AppConfig.value).where(
            AppConfig.merchant_id == merchant_id, AppConfig.is_active
        )
        return dict(self.session.execute(statement).all())  # type: ignore[arg-type]

    def _version(self, merchant_id: uuid.UUID, own: Mapping[str, str]) -> str | None:
        if not own:
            return None
        statement = (
            select(AppConfig.config_version)
            .where(AppConfig.merchant_id == merchant_id, AppConfig.is_active)
            .order_by(AppConfig.effective_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()


def load_configuration(session: Session, merchant_id: uuid.UUID) -> Configuration:
    """Convenience wrapper: the effective configuration for one merchant.

    ``merchant_id`` required, like every other read in this package. Configuration is
    tenant data — a merchant's message cap is a fact about that merchant.
    """
    return ConfigurationRepository(session).load(merchant_id)

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

**Both layers are fetched in one statement.** ``merchant_id IN (:own, :defaults)``
rather than two round trips, and the two-layer merge happens in Python. The RLS
policy on ``app_config`` already permits exactly these two tenants — it is the one
table with a sentinel exemption — so a single predicate is not a widening of what
this role can read, and the row count is the same either way.

**And the result is memoized per merchant with a short TTL.** :meth:`load` runs on
every authenticated API request, on every claimed job and on every webhook delivery,
and configuration changes on the order of weeks. A cache keyed by ``merchant_id``
turns the steady state into zero statements. Four things make it safe:

* **Keyed by ``merchant_id`` and by nothing else, in a plain dict.** One merchant's
  configuration cannot be handed to another, because a lookup that misses returns
  ``None`` rather than a neighbour's entry. Configuration is tenant data and this
  is the one place a stale-cache bug could become a tenant leak.
* **Guarded by a lock.** Handlers run in a 40-thread pool, so two requests for two
  merchants genuinely do reach this dict at once. The lock is held for a dict
  operation and never across the database read — a slow load must not serialize
  every other tenant's cache hit — which means two threads can miss for the same
  merchant and both load. That is a duplicated read, not a wrong answer, and it is
  the right trade against holding a lock across a round trip.
* **:class:`Configuration` is frozen.** A cached value handed to several threads is
  not mutable by any of them.
* **A monotonic clock**, not :func:`revora.platform.clock.now`. The domain clock is
  deliberately freezable in tests and a frozen clock would freeze the TTL; a cache
  expiry is not a domain fact.

:func:`invalidate_configuration_cache` is the explicit path for a writer, so an
operator changing a bound does not wait out the TTL. ``load_strict`` deliberately
does **not** read the cache: it runs once at process bootstrap to refuse a start on
a defaulted bound, and a bootstrap check that could be answered from a cache
populated by something else is not a check.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from revora.persistence.models import AppConfig
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.platform.config import (
    DEFAULT_CONFIG_VERSION,
    DEFAULTS_MERCHANT_ID,
    Configuration,
)

__all__ = [
    "CACHE_TTL_SECONDS",
    "ConfigurationRepository",
    "cached_merchant_count",
    "invalidate_configuration_cache",
    "load_configuration",
]

CACHE_TTL_SECONDS: Final[float] = 45.0
"""How long a loaded configuration is reused. Forty-five seconds.

Short enough that an out-of-band change — a bound edited with SQL by an operator, or by
a process that cannot reach :func:`invalidate_configuration_cache` — takes effect inside
a minute without anyone restarting anything. Long enough that a dashboard polling every
few seconds loads configuration once rather than once per request. Not a
``ConfigurationBound``: the bounds live in the table this cache is in front of, so
reading the TTL from there would need a load to decide whether to load."""


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One merchant's configuration and the monotonic instant it stops being reusable."""

    configuration: Configuration
    expires_at: float


_cache_lock: Final[threading.Lock] = threading.Lock()
_cache: dict[uuid.UUID, _CacheEntry] = {}


def invalidate_configuration_cache(merchant_id: uuid.UUID | None = None) -> None:
    """Drop cached configuration for one merchant, or for all of them.

    Call this from anywhere configuration is written, in the same place the write
    happens. ``None`` drops everything, which is what a migration or a test that has
    rewritten rows for several tenants wants — and is also the safe default for a
    caller that does not know whose rows it touched.

    Idempotent and safe to call for a merchant that was never cached.
    """
    with _cache_lock:
        if merchant_id is None:
            _cache.clear()
        else:
            _cache.pop(merchant_id, None)


def cached_merchant_count() -> int:
    """How many merchants currently have a cached configuration. For tests only."""
    with _cache_lock:
        return len(_cache)


def _cached(merchant_id: uuid.UUID) -> Configuration | None:
    """The live entry for one merchant, or ``None`` when absent or expired.

    An expired entry is dropped on the way past rather than left for a sweeper. There
    is no sweeper, and there does not need to be: the key space is the tenant count.
    """
    now_monotonic = time.monotonic()
    with _cache_lock:
        entry = _cache.get(merchant_id)
        if entry is None:
            return None
        if entry.expires_at <= now_monotonic:
            del _cache[merchant_id]
            return None
        return entry.configuration


def _remember(merchant_id: uuid.UUID, configuration: Configuration) -> None:
    """Cache one merchant's configuration for :data:`CACHE_TTL_SECONDS`."""
    entry = _CacheEntry(
        configuration=configuration, expires_at=time.monotonic() + CACHE_TTL_SECONDS
    )
    with _cache_lock:
        _cache[merchant_id] = entry


class ConfigurationRepository(MerchantScopedRepository[AppConfig]):
    """Reads the active configuration for one merchant."""

    model = AppConfig

    def load(self, merchant_id: uuid.UUID) -> Configuration:
        """The effective configuration for one merchant, from cache when it is warm.

        Never partial: any bound with no row anywhere falls back to its catalogue
        placeholder and is named in ``Configuration.defaulted_keys``, so a missing
        seed shows up as a reportable fact instead of a wrong bound.

        Memoized for :data:`CACHE_TTL_SECONDS` per merchant. A caller that has just
        written a configuration row must call
        :func:`invalidate_configuration_cache` — see this module's docstring for why
        the cache is keyed the way it is.
        """
        cached = _cached(merchant_id)
        if cached is not None:
            return cached
        configuration = self._read(merchant_id, strict=False)
        _remember(merchant_id, configuration)
        return configuration

    def load_strict(self, merchant_id: uuid.UUID) -> Configuration:
        """As :meth:`load`, but refuse when any bound has no row at all.

        For the API and worker bootstrap. A process that starts with a defaulted
        bound is a process whose behaviour does not match the recorded
        configuration, and that discrepancy is invisible until someone asks why a
        case stopped after three attempts.

        Reads through the cache in both directions — it neither answers from it nor
        populates it. A startup assertion answered from a cache some earlier caller
        filled is not an assertion about the database, and this one runs once.
        """
        return self._read(merchant_id, strict=True)

    def _read(self, merchant_id: uuid.UUID, *, strict: bool) -> Configuration:
        """Both layers in one statement, merged, typed."""
        own, defaults = self._active_values(merchant_id)
        merged = {**defaults, **own}
        version = self._version(merchant_id, own) or DEFAULT_CONFIG_VERSION
        return Configuration.from_values(merged, version=version, strict=strict)

    def active_rows(self, merchant_id: uuid.UUID) -> Mapping[str, AppConfig]:
        """The active rows for one merchant, by key.

        For the settings screen, which shows the value, the version, whether it is
        still an assumption placeholder, and who approved it.
        """
        statement = self.scoped(merchant_id).where(AppConfig.is_active)
        return {row.key: row for row in self.session.execute(statement).scalars()}

    def _active_values(
        self, merchant_id: uuid.UUID
    ) -> tuple[dict[str, str], dict[str, str]]:
        """This merchant's active values and the sentinel's, in one round trip.

        Returns them split rather than merged, because the caller needs to know
        whether the merchant has any rows of its own — that is what decides whose
        ``config_version`` the loaded configuration cites.

        A merchant *is* the sentinel when the sentinel's own configuration is loaded,
        which the seed check does. The ``IN`` collapses to one id then, and the same
        rows are returned as both layers, which merges to itself.
        """
        statement = select(AppConfig.merchant_id, AppConfig.key, AppConfig.value).where(
            AppConfig.merchant_id.in_((merchant_id, DEFAULTS_MERCHANT_ID)),
            AppConfig.is_active,
        )
        own: dict[str, str] = {}
        defaults: dict[str, str] = {}
        for row_merchant_id, key, value in self.session.execute(statement).all():
            if row_merchant_id == merchant_id:
                own[key] = value
            if row_merchant_id == DEFAULTS_MERCHANT_ID:
                defaults[key] = value
        return own, defaults

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

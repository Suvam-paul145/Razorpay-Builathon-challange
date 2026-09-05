"""The two memoizations on the authentication preamble, checked without a database.

Both exist to remove work from the hottest path in the application — every authenticated
request loaded configuration with two SELECTs and rewrote ``merchant_session.last_seen_at``
with a WAL write — and both are therefore in a position to be *wrong* on every request
rather than on some of them. What is checked here is the part that does not need a server:
the cache's key discipline and expiry, and the staleness predicate.

The tenant-isolation assertion is the one to read first. A configuration cache that could
hand merchant A's bounds to merchant B would be a tenant leak of exactly the kind the RLS
policies and the mandatory ``merchant_id`` argument exist to prevent, and it would be
invisible — the wrong message cap looks like a policy decision, not like a breach.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from revora.persistence.models.tenancy import MerchantSession
from revora.persistence.repositories import config as config_module
from revora.persistence.repositories.config import (
    CACHE_TTL_SECONDS,
    cached_merchant_count,
    invalidate_configuration_cache,
)
from revora.persistence.repositories.users import TOUCH_THRESHOLD, MerchantSessionRepository
from revora.platform.config import default_configuration

pytestmark = pytest.mark.pure


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    """Every test starts with an empty cache and leaves one behind."""
    invalidate_configuration_cache()


# ---------------------------------------------------------------------------
# The configuration cache
# ---------------------------------------------------------------------------


def test_a_remembered_configuration_is_returned_for_the_same_merchant() -> None:
    """The identical object, so nothing is re-parsed and nothing is re-read."""
    merchant_id = uuid.uuid4()
    configuration = default_configuration()

    config_module._remember(merchant_id, configuration)

    assert config_module._cached(merchant_id) is configuration


def test_one_merchants_configuration_is_never_returned_for_another() -> None:
    """The tenant-isolation assertion. A miss is a miss, not a neighbour's entry.

    This is the failure that would matter: a cache keyed loosely enough to collide would
    apply one merchant's message cap, cost ratio and session lifetime to another, and the
    resulting decisions would look deliberate.
    """
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    config_module._remember(mine, default_configuration())

    assert config_module._cached(theirs) is None
    assert cached_merchant_count() == 1


def test_an_expired_entry_is_not_returned_and_is_dropped() -> None:
    """Past its TTL an entry is gone, not merely stale.

    Written by backdating the entry rather than by sleeping: the point is the comparison,
    and a test that waits forty-five seconds to check a forty-five second TTL is a test
    nobody runs.
    """
    merchant_id = uuid.uuid4()
    config_module._remember(merchant_id, default_configuration())
    entry = config_module._cache[merchant_id]
    config_module._cache[merchant_id] = config_module._CacheEntry(
        configuration=entry.configuration, expires_at=entry.expires_at - CACHE_TTL_SECONDS - 1.0
    )

    assert config_module._cached(merchant_id) is None
    assert cached_merchant_count() == 0, "an expired entry is dropped on the way past"


def test_invalidating_one_merchant_leaves_the_others_alone() -> None:
    """The explicit path a writer takes. Narrow, so one merchant's edit is not a global flush."""
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    config_module._remember(mine, default_configuration())
    config_module._remember(theirs, default_configuration())

    invalidate_configuration_cache(mine)

    assert config_module._cached(mine) is None
    assert config_module._cached(theirs) is not None


def test_invalidating_everything_and_invalidating_nothing_are_both_safe() -> None:
    """``None`` clears the lot, and a merchant that was never cached is not an error.

    Both matter for a writer: a migration or a bulk edit does not know whose rows it
    touched, and a caller that invalidates defensively must not have to check first.
    """
    merchant_id = uuid.uuid4()
    config_module._remember(merchant_id, default_configuration())

    invalidate_configuration_cache()
    assert cached_merchant_count() == 0

    invalidate_configuration_cache(uuid.uuid4())
    invalidate_configuration_cache()
    assert cached_merchant_count() == 0


def test_concurrent_readers_and_writers_do_not_corrupt_the_cache() -> None:
    """Handlers run in a forty-thread pool, so this dict genuinely has several writers.

    What is asserted is that every merchant either has its own configuration or has none —
    never another's — and that the structure survives. A lock-free dict would pass this
    most of the time, which is the argument for asserting it rather than reasoning about it.
    """
    merchants = [uuid.uuid4() for _ in range(24)]
    configuration = default_configuration()
    errors: list[BaseException] = []

    def churn(merchant_id: uuid.UUID) -> None:
        try:
            for _ in range(200):
                config_module._remember(merchant_id, configuration)
                config_module._cached(merchant_id)
                invalidate_configuration_cache(merchant_id)
                config_module._remember(merchant_id, configuration)
        except BaseException as exc:  # pragma: no cover - only on a real race
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(m,)) for m in merchants]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert cached_merchant_count() == len(merchants)
    for merchant_id in merchants:
        assert config_module._cached(merchant_id) is configuration


# ---------------------------------------------------------------------------
# The conditional touch
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Counts statements without issuing any. Enough for a predicate that decides not to."""

    def __init__(self) -> None:
        self.executed: int = 0

    def execute(self, *_args: object, **_kwargs: object) -> None:
        self.executed += 1


def _session_row(*, last_seen_at: datetime | None) -> MerchantSession:
    row = MerchantSession(
        merchant_user_id=uuid.uuid4(),
        token_digest="0" * 64,
        issued_at=datetime(2025, 1, 1, tzinfo=UTC),
        expires_at=datetime(2025, 1, 2, tzinfo=UTC),
    )
    row.id = uuid.uuid4()
    row.last_seen_at = last_seen_at
    return row


@pytest.mark.parametrize(
    ("age", "expected_write"),
    [
        (None, True),
        (TOUCH_THRESHOLD * 2, True),
        (TOUCH_THRESHOLD, True),
        (TOUCH_THRESHOLD / 2, False),
        (timedelta(0), False),
    ],
)
def test_the_touch_is_issued_only_when_last_seen_is_stale(
    age: timedelta | None, expected_write: bool
) -> None:
    """One UPDATE per session per threshold, and none in between.

    A session that has never been seen is stale by definition, so the first use of a token
    still stamps the column — which is what keeps the figure meaningful at all. Exactly at
    the threshold it is written: the comparison is strict, so the boundary resolves toward
    writing rather than toward a column that can sit one tick past its own staleness bound
    forever.
    """
    moment = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    row = _session_row(last_seen_at=None if age is None else moment - age)
    session = _RecordingSession()
    repository = MerchantSessionRepository(session)  # type: ignore[arg-type]

    wrote = repository.touch_if_stale(uuid.uuid4(), row, moment=moment)

    assert wrote is expected_write
    assert session.executed == (1 if expected_write else 0)


def test_the_staleness_check_reads_no_row_of_its_own() -> None:
    """The decision comes from the row the caller already holds, so the skip is free.

    If it needed a read it would have replaced one statement with another and saved nothing —
    which is the whole reason the method takes the row rather than an id.
    """
    moment = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    row = _session_row(last_seen_at=moment)
    session = _RecordingSession()

    MerchantSessionRepository(session).touch_if_stale(  # type: ignore[arg-type]
        uuid.uuid4(), row, moment=moment
    )

    assert session.executed == 0

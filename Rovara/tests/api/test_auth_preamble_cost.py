"""What the authentication preamble costs, and that paying less did not change what it decides.

``authenticate`` runs before every dashboard handler, so its cost is the floor under every
endpoint. It used to open three transactions — an untenanted slug lookup, a tenanted read that
loaded configuration with two uncached SELECTs and then rewrote ``last_seen_at``, and a third
for a failure record — which is three connection checkouts and five statements before a handler
saw a single row.

This module pins the shape it has now: **one transaction and three statements on the warm
success path**, a second transaction only when there is a failure to record. The numbers are
asserted with ``<=`` where a smaller number would still be an improvement and with ``==``
where an extra statement would mean a specific regression has come back.

**Counted with a listener on this test's own engine**, not with ``revora.platform.sqltrace``.
The tracer installs listeners on the ``Engine`` *class* for the life of the process and is
deliberately idempotent, so a test that switched it on would leave it on for every test after
it. A listener attached to one engine and removed in teardown measures the same thing and
leaves nothing behind.

The other half of the module is the half that matters more: the same tokens are still accepted
and rejected at the same moments, and the same audit records are still written. A cheaper
preamble that authenticated the wrong caller would be a catastrophic trade.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, event, text

from revora.api.auth import authenticate
from revora.audit.events import AUTHENTICATION_FAILED
from revora.persistence.repositories.config import (
    cached_merchant_count,
    invalidate_configuration_cache,
)
from revora.persistence.repositories.users import TOUCH_THRESHOLD
from revora.platform.clock import now
from tests.api.conftest import Tenant

pytestmark = pytest.mark.pg


@dataclass(slots=True)
class _Cost:
    """Statements issued and connections checked out while the counter was attached."""

    statements: int = 0
    checkouts: int = 0


@contextmanager
def _counted(engine: Engine) -> Iterator[_Cost]:
    """Count statements and pool checkouts on one engine, and detach afterwards."""
    cost = _Cost()

    def before_cursor_execute(*_args: Any, **_kwargs: Any) -> None:
        cost.statements += 1

    def checkout(*_args: Any, **_kwargs: Any) -> None:
        cost.checkouts += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "checkout", checkout)
    try:
        yield cost
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "checkout", checkout)


def _authorization(tenant: Tenant) -> str:
    return f"Bearer {tenant.token}"


def _make_last_seen_stale(engine: Engine, tenant: Tenant) -> None:
    """Backdate ``last_seen_at`` past the staleness threshold, so the next touch is issued."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE merchant_session SET last_seen_at = :stale WHERE merchant_id = :m"
            ),
            {"stale": now() - (TOUCH_THRESHOLD * 10), "m": str(tenant.merchant_id)},
        )


def _last_seen(engine: Engine, tenant: Tenant) -> object:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT last_seen_at FROM merchant_session WHERE merchant_id = :m"),
            {"m": str(tenant.merchant_id)},
        ).scalar_one()


def _failure_reasons(engine: Engine, merchant_id: uuid.UUID) -> list[str | None]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT decision FROM audit_record WHERE merchant_id = :m AND event_type = :e "
                "ORDER BY created_at"
            ),
            {"m": str(merchant_id), "e": AUTHENTICATION_FAILED},
        ).all()
    return [(row[0] or {}).get("reason") for row in rows]


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_the_warm_preamble_is_one_transaction_and_three_statements(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The floor under every authenticated endpoint.

    Three statements: resolve the slug, bind the tenant, find the live session. Configuration
    comes from the per-merchant cache and ``last_seen_at`` is fresh, so neither costs a
    statement. One checkout, because the slug lookup no longer needs a transaction of its own —
    ``merchant`` carries no tenant scope, so it can be read before ``SET LOCAL`` inside the
    same transaction that then reads the session.
    """
    authenticate(_authorization(tenant))  # warms the configuration cache and stamps last_seen

    with _counted(installed_engine) as cost:
        session = authenticate(_authorization(tenant))

    assert session.merchant_id == tenant.merchant_id
    assert cost.statements == 3, (
        "the authenticated floor is slug lookup, SET LOCAL and the session read; anything more "
        "means configuration is being re-read or last_seen_at is being rewritten per request"
    )
    assert cost.checkouts == 1, "one transaction, so one connection checkout"


def test_the_cold_preamble_reads_configuration_and_stamps_last_seen_once(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The worst case, and it is still one transaction.

    Five statements: the three above plus one configuration read — both layers in a single
    ``merchant_id IN (:own, :defaults)`` select rather than two — and the ``last_seen_at``
    update. This merchant has no ``app_config`` rows of its own, so no second read for its
    ``config_version``.
    """
    invalidate_configuration_cache()
    _make_last_seen_stale(installed_engine, tenant)

    with _counted(installed_engine) as cost:
        authenticate(_authorization(tenant))

    assert cost.statements == 5, (
        "cold: slug, SET LOCAL, one configuration read, the session read, one touch"
    )
    assert cost.checkouts == 1


def test_configuration_is_read_once_and_then_not_again(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """Ten requests, one configuration read. That is the whole point of the cache."""
    invalidate_configuration_cache()

    with _counted(installed_engine) as first:
        authenticate(_authorization(tenant))
    with _counted(installed_engine) as rest:
        for _ in range(9):
            authenticate(_authorization(tenant))

    assert first.statements == 5
    assert rest.statements == 9 * 3, (
        "nine warm requests should cost three statements each; a configuration read or a touch "
        f"has leaked back onto the hot path (saw {rest.statements})"
    )


def test_a_failure_costs_a_second_transaction_and_nothing_more(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The one place a second transaction is required, and it is required.

    The lookup transaction has to commit before the failure record is written, or the 401
    rolls back the evidence of itself. Six statements: slug, ``SET LOCAL``, the live-session
    read that finds nothing, the re-read that says *which* failure it was, then ``SET LOCAL``
    and the insert in the record's own transaction.
    """
    authenticate(_authorization(tenant))  # warm the configuration cache

    with _counted(installed_engine) as cost, pytest.raises(HTTPException) as caught:
        authenticate(f"Bearer {tenant.slug}.not-a-real-secret")

    assert caught.value.status_code == 401
    assert cost.checkouts == 2, "the lookup transaction must commit before the record is written"
    assert cost.statements == 6


# ---------------------------------------------------------------------------
# The cache's tenant discipline and its invalidation path
# ---------------------------------------------------------------------------


def test_two_merchants_get_their_own_configuration_through_the_cache(
    installed_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    """P30's configuration corner. A cache that collided here would be a tenant leak.

    One merchant's ``MAX_CUSTOMER_MESSAGES`` is set to one and the other's is left on the
    seeded default of two, and both are authenticated repeatedly in an interleaved order so a
    last-writer-wins cache would be caught rather than accidentally right.
    """
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_config (
                    merchant_id, key, value, value_kind, config_version, is_active,
                    effective_at, is_assumption, created_at
                ) VALUES (
                    :m, 'MAX_CUSTOMER_MESSAGES', '1', 'INTEGER', 'tenant-choice', true,
                    now(), false, now()
                )
                """
            ),
            {"m": str(tenant.merchant_id)},
        )
    invalidate_configuration_cache(tenant.merchant_id)

    for _ in range(4):
        mine = authenticate(_authorization(tenant))
        theirs = authenticate(_authorization(other_tenant))
        assert mine.config.MAX_CUSTOMER_MESSAGES == 1
        assert theirs.config.MAX_CUSTOMER_MESSAGES == 2
        assert mine.config.version == "tenant-choice"
        assert theirs.config.version != "tenant-choice"

    assert cached_merchant_count() >= 2


def test_an_invalidated_merchant_re_reads_and_its_neighbour_does_not(
    installed_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The explicit path an operator's write takes, and its blast radius.

    Without the call the new bound waits out the TTL; with it, the next request sees it. And
    invalidating one merchant must not evict another, or a single settings edit turns into a
    re-read for every tenant in the process.
    """
    authenticate(_authorization(tenant))
    authenticate(_authorization(other_tenant))

    with installed_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_config (
                    merchant_id, key, value, value_kind, config_version, is_active,
                    effective_at, is_assumption, created_at
                ) VALUES (
                    :m, 'MAX_RECOVERY_ATTEMPTS', '1', 'INTEGER', 'operator-edit', true,
                    now(), false, now()
                )
                """
            ),
            {"m": str(tenant.merchant_id)},
        )

    assert authenticate(_authorization(tenant)).config.MAX_RECOVERY_ATTEMPTS == 3, (
        "inside the TTL and with no invalidation, the cached bound is what a request sees"
    )

    invalidate_configuration_cache(tenant.merchant_id)

    with _counted(installed_engine) as after_invalidation:
        assert authenticate(_authorization(tenant)).config.MAX_RECOVERY_ATTEMPTS == 1
    assert after_invalidation.statements == 5, "one configuration read, not two"

    with _counted(installed_engine) as neighbour:
        assert authenticate(_authorization(other_tenant)).config.MAX_RECOVERY_ATTEMPTS == 3
    assert neighbour.statements == 3, "the other merchant's entry must survive"


def test_a_merchant_with_its_own_rows_still_cites_its_own_version(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The two-layer merge survived being folded into one statement.

    A merchant's own row wins over the sentinel's, unchanged bounds still come from the
    defaults, and the cited version is the merchant's — which is what a policy decision
    records, so a decision made under a cap of one stays distinguishable from one made under
    a cap of two.
    """
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_config (
                    merchant_id, key, value, value_kind, config_version, is_active,
                    effective_at, is_assumption, created_at
                ) VALUES (
                    :m, 'MAX_CUSTOMER_MESSAGES', '1', 'INTEGER', '2025.02.0-merchant-choice',
                    true, now(), false, now()
                )
                """
            ),
            {"m": str(tenant.merchant_id)},
        )
    invalidate_configuration_cache(tenant.merchant_id)

    config = authenticate(_authorization(tenant)).config

    assert config.MAX_CUSTOMER_MESSAGES == 1
    assert config.MAX_RECOVERY_ATTEMPTS == 3
    assert config.version == "2025.02.0-merchant-choice"
    assert config.defaulted_keys == frozenset(), "no bound fell back to a code placeholder"


# ---------------------------------------------------------------------------
# The conditional touch, against a real row
# ---------------------------------------------------------------------------


def test_the_first_use_of_a_session_stamps_last_seen(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """A never-seen session is stale by definition, so the column is not left null."""
    with installed_engine.begin() as connection:
        connection.execute(
            text("UPDATE merchant_session SET last_seen_at = NULL WHERE merchant_id = :m"),
            {"m": str(tenant.merchant_id)},
        )

    authenticate(_authorization(tenant))

    assert _last_seen(installed_engine, tenant) is not None


def test_repeated_use_inside_the_threshold_does_not_rewrite_the_row(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The WAL write this change removes. Twenty requests, no second UPDATE."""
    authenticate(_authorization(tenant))
    stamped = _last_seen(installed_engine, tenant)

    for _ in range(20):
        authenticate(_authorization(tenant))

    assert _last_seen(installed_engine, tenant) == stamped


def test_a_stale_session_is_stamped_again(installed_engine: Engine, tenant: Tenant) -> None:
    """Skipping is bounded by the threshold, so the figure stays usable."""
    authenticate(_authorization(tenant))
    _make_last_seen_stale(installed_engine, tenant)
    stale = _last_seen(installed_engine, tenant)

    authenticate(_authorization(tenant))

    refreshed = _last_seen(installed_engine, tenant)
    assert refreshed != stale
    assert refreshed is not None


# ---------------------------------------------------------------------------
# Auth semantics — the constraint most at risk from everything above
# ---------------------------------------------------------------------------


def test_a_valid_token_still_resolves_the_same_session(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """Every field of the authenticated session, including the ones the merchant row supplies.

    The slug lookup moved inside the tenanted transaction, so the three values it contributes —
    the resolved slug, the currency and the reporting timezone — are the ones to check.
    """
    session = authenticate(_authorization(tenant))

    assert session.merchant_id == tenant.merchant_id
    assert session.merchant_slug == tenant.slug
    assert session.merchant_user_id == tenant.user_id
    assert session.default_currency == "INR"
    assert session.reporting_timezone == "UTC"
    assert session.expires_at > now()
    assert session.actor == f"merchant_user:{tenant.user_id}"


def test_a_token_that_cannot_name_a_tenant_is_refused_identically() -> None:
    """No session, no lookup, no record — and no database, which is why there is no fixture.

    Seven malformed headers, and the assertion is that they raise **one** refusal rather than
    seven: the same status, the same detail and no ``WWW-Authenticate`` challenge. A detail that
    varied with the reason would turn the dependency into an oracle for which shapes get further
    than others.
    """
    headers = (
        None,
        "",
        "Bearer",
        "Bearer nodotshere",
        "Token abc.def",
        "Bearer .secret",
        "Basic dXNlcjpwYXNz",
    )
    refusals = set()
    for header in headers:
        with pytest.raises(HTTPException) as caught:
            authenticate(header)
        refusals.add((caught.value.status_code, caught.value.detail, caught.value.headers))

    assert len(refusals) == 1, f"malformed tokens produced {len(refusals)} distinct refusals"
    status_code, _detail, challenge = refusals.pop()
    assert status_code == 401
    assert challenge is None, "a challenge naming a scheme is a varying, informative response"


def test_an_unknown_slug_is_refused_with_no_audit_record(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """``audit_record.merchant_id`` is NOT NULL, so an unattributable failure is logged only.

    Asserted against this tenant's records to show that the refused request did not attach a
    record to some other merchant on its way past.
    """
    before = _failure_reasons(installed_engine, tenant.merchant_id)

    with pytest.raises(HTTPException) as caught:
        authenticate(f"Bearer no-such-merchant-{uuid.uuid4()}.abcdefghijklmnop")

    assert caught.value.status_code == 401
    assert _failure_reasons(installed_engine, tenant.merchant_id) == before


def test_an_unknown_token_is_refused_and_the_record_names_the_reason(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """The record keeps the distinction the 401 hides, and it survives the raise.

    Which is the reason the failure record has a transaction of its own. If it shared the
    lookup transaction, the exception below would roll it back and the one event this record
    exists for would leave no trace.
    """
    with pytest.raises(HTTPException) as caught:
        authenticate(f"Bearer {tenant.slug}.definitely-not-the-secret")

    assert caught.value.status_code == 401
    assert "unknown session token" in _failure_reasons(installed_engine, tenant.merchant_id)


def test_an_expired_session_is_refused_and_the_record_says_so(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """R17.C1. Age, not idleness — and the conditional touch must not turn it into idleness.

    This is the assertion that guards the sliding-window mistake. ``last_seen_at`` is now
    written less often, and if anything had started deriving expiry from it, a session used
    thirteen hours after issue under a twelve-hour bound would still be accepted.
    """
    moment = now()
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE merchant_session SET issued_at = :issued, expires_at = :past, "
                "last_seen_at = :seen WHERE merchant_id = :m"
            ),
            {
                "issued": moment - timedelta(hours=13),
                "past": moment - timedelta(hours=1),
                "seen": moment,
                "m": str(tenant.merchant_id),
            },
        )

    with pytest.raises(HTTPException) as caught:
        authenticate(_authorization(tenant))

    assert caught.value.status_code == 401
    reasons = _failure_reasons(installed_engine, tenant.merchant_id)
    assert "session expired past SESSION_LIFETIME" in reasons


def test_a_revoked_session_is_refused_on_the_very_next_request(
    installed_engine: Engine, tenant: Tenant
) -> None:
    """Revocation is immediate, and no cache stands between the token and its row.

    Configuration is cached; the *session* is not, and this is the assertion that says so. A
    preamble that cached the resolved session to save the lookup would keep a revoked token
    working for up to the TTL, which is the one failure mode this optimisation must not have.
    """
    assert authenticate(_authorization(tenant)).merchant_id == tenant.merchant_id

    with installed_engine.begin() as connection:
        connection.execute(
            text("UPDATE merchant_session SET revoked_at = now() WHERE merchant_id = :m"),
            {"m": str(tenant.merchant_id)},
        )

    with pytest.raises(HTTPException) as caught:
        authenticate(_authorization(tenant))

    assert caught.value.status_code == 401
    assert "session revoked" in _failure_reasons(installed_engine, tenant.merchant_id)


def test_another_merchants_token_shape_finds_nothing(
    installed_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The slug prefix is a routing hint, not a credential, and swapping it fails closed.

    The secret half of one merchant's token presented under another merchant's slug. The
    session lookup is scoped to the merchant the slug resolved to, so it finds no row — and it
    still must, now that the lookup shares a transaction with the slug resolution.
    """
    secret = tenant.token.rsplit(".", 1)[1]

    with pytest.raises(HTTPException) as caught:
        authenticate(f"Bearer {other_tenant.slug}.{secret}")

    assert caught.value.status_code == 401
    assert "unknown session token" in _failure_reasons(
        installed_engine, other_tenant.merchant_id
    )

"""Task 31.4's latency half. The acknowledgement budget, measured through the real HTTP boundary.

Here rather than in ``tests/test_smoke.py`` because it needs the app, a tenant and a signed
delivery, and those fixtures live in this directory's conftest.

Both assertions are tagged ``smoke`` and excluded from the gating selection with ``-m "not smoke"``.
That is not a lack of confidence in them — it is that on a shared runner a latency bound measures
the hardware as much as the code, so a gate that can fail for reasons nobody can act on teaches
everyone to re-run the build until it passes, which is worse than having no gate. Tagged rather than
deleted, because this is the only place the acknowledgement budget appears as a number anyone reads.

**The margins are order-of-magnitude on purpose.** These catch the regression that matters — an
inline provider call, a synchronous read that should have been a job, a missing index — and ignore
the noise a percentage-level bound would drown in.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.platform.config import default_configuration
from tests.integration.conftest import Tenant, deliver, failed_payment_body

pytestmark = [pytest.mark.pg, pytest.mark.smoke]

_CONFIG = default_configuration()


def test_the_webhook_acknowledgement_stays_inside_its_budget(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """``INGEST_ACK_TIMEOUT`` is 1500 ms and one delivery should be nowhere near it.

    The route's whole job is verify, store, enqueue, answer. Anything slower means work has moved
    onto the acknowledgement path — which is the failure this bound exists to name, because a
    provider that times out waiting redelivers, and a handler slow enough to time out under load
    turns one slow request into a redelivery storm.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = failed_payment_body(payment_id, event_id)

    started = time.perf_counter()
    status = deliver(client, tenant.slug, body, event_id)
    elapsed = time.perf_counter() - started

    assert status == 200
    budget = _CONFIG.INGEST_ACK_TIMEOUT.total_seconds()
    assert elapsed < budget * 10, (
        f"acknowledgement took {elapsed:.3f}s against a {budget:.3f}s budget; the handler is "
        "supposed to verify, store and enqueue and nothing else"
    )


def test_the_acknowledgement_does_no_detection_work(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The structural reason the budget holds: the route answers, it does not work.

    A latency number alone would not distinguish "fast enough today" from "fast because the machine
    is idle". This asserts the arrangement that makes it fast: after the acknowledgement there is a
    persisted event and a queued job, and **no case** — detection has not run. If a future change
    moved detection inline, this fails immediately and names the reason, where the timing assertion
    would only drift.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    assert deliver(client, tenant.slug, failed_payment_body(payment_id, event_id), event_id) == 200

    with installed_engine.begin() as connection:
        params = {"m": str(tenant.merchant_id)}
        events = connection.execute(
            text("SELECT count(*) FROM webhook_event WHERE merchant_id = :m"), params
        ).scalar_one()
        jobs = connection.execute(
            text("SELECT count(*) FROM job WHERE merchant_id = :m"), params
        ).scalar_one()
        cases = connection.execute(
            text("SELECT count(*) FROM recovery_case WHERE merchant_id = :m"), params
        ).scalar_one()

    assert int(events) == 1, "the delivery must be durably recorded before it is acknowledged"
    assert int(jobs) >= 1, "the follow-on work must be queued in the same transaction"
    assert int(cases) == 0, (
        "a case exists before any worker ran, so detection is happening on the acknowledgement "
        "path; that is what puts the 1500 ms budget at risk under load"
    )

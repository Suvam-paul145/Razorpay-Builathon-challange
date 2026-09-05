"""The Demonstration_Loader's delivery path: does it seed the batch it says it seeded?

Two claims, both about a defect that had already happened and had already been reported as a
number. A 1 000-case run delivered 820 main-cohort webhooks under one source key inside one rate
window, ``INGEST_RATE_LIMIT`` of 600 refused the last 220 with HTTP 429, and the batch finished:
``seeded_case_count`` 780 against ``case_count`` 1 000, both experiment arms under the 447-per-arm
requirement the loader had computed for itself, and ``observed_recovered_revenue`` printed anyway.

So: (1) the loader must stay inside the configured rate rather than exceeding it, and (2) a short
cohort must fail loudly rather than be reported.

**These run in the fast tier and against the real limiter.** ``revora.platform.ratelimit`` reads
no clock of its own — every caller passes the instant it already has — so the whole rate-window
interaction is pure arithmetic over a substituted clock, and there is nothing here worth deferring
to a nightly tier. The transport below is the only fake: it answers 429 on exactly the condition
``ingest_webhook`` answers 429 on, which is a refused ``RateLimiter.allow`` against
``revora.platform.clock.now()``.

The limit is 10 rather than the configured 600 because the arithmetic under test is "a window's
allowance, then the next window", not the number 600. Sixty plans against a limit of 10 exercises
six windows; 820 against 600 would exercise two and cost a hundred times as much generation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence

import pytest

from revora.platform.clock import ManualClock, now, using_clock
from revora.platform.ratelimit import WINDOW, shared_limiter, source_key
from revora.synthetic import demo

pytestmark = pytest.mark.pure

_LIMIT = 10
"""The per-window allowance the fake endpoint enforces. See the module docstring."""

_CASE_COUNT = 240
_PRIOR = 180
"""The smallest batch ``plan_batch`` accepts at the real prior cohort size: 228 cases are reserved
by the prior cohort and the shaped roles, so 240 leaves a main cohort of 60 — six windows' worth at
:data:`_LIMIT`, and disjoint from the prior cohort's own 180."""

_TENANT = demo.DemoTenant(
    merchant_id=uuid.UUID("00000000-0000-0000-0000-0000000000d1"),
    slug="demo-seeding-tenant",
    webhook_secret="demo-seeding-secret",
    dashboard_headers={},
)


class _RateLimitedTransport:
    """The webhook endpoint reduced to the one decision under test: rate limit, then accept.

    Answers 429 exactly where ``revora.ingestion.service.ingest_webhook`` does — a refused
    ``shared_limiter().allow(source_key(slug), limit, now())`` — and 200 otherwise. Nothing else
    about ingestion is modelled, because nothing else about ingestion is what refused those 220
    deliveries.
    """

    def __init__(self, limit: int = _LIMIT) -> None:
        self.limit = limit
        self.refused = 0
        self.accepted = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> demo.HttpResult:
        del method, path, content, headers
        if not shared_limiter().allow(source_key(_TENANT.slug), self.limit, now()):
            self.refused += 1
            return demo.HttpResult(status_code=429)
        self.accepted += 1
        return demo.HttpResult(status_code=200)


class _RefusingTransport:
    """Refuses a fixed number of deliveries, for a reason no clock advance can fix.

    503 rather than 429 on purpose: a shortfall must fail on its own terms and not only when the
    cause happens to be the rate limit. A signature rejection, an oversized payload or a shed ack
    budget all produce a batch smaller than its design, and the error has to name the status so a
    reader is not sent back to the logs to find out which.
    """

    def __init__(self, refuse: int) -> None:
        self.remaining_refusals = refuse

    def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> demo.HttpResult:
        del method, path, content, headers
        if self.remaining_refusals > 0:
            self.remaining_refusals -= 1
            return demo.HttpResult(status_code=503)
        return demo.HttpResult(status_code=200)


class _CountingWorker:
    """A :class:`~revora.synthetic.demo.DemoWorker` that records rather than works."""

    def __init__(self) -> None:
        self.drains = 0
        self.ticks = 0

    def drain(self) -> int:
        self.drains += 1
        return 1

    def tick(self, kinds: Sequence[str]) -> int:
        del kinds
        self.ticks += 1
        return 0


@pytest.fixture(autouse=True)
def _clean_limiter() -> Iterator[None]:
    """The limiter is process-wide, so a test that spends allowance must give it back.

    Both sides, not just after: a previous test on the same key would otherwise decide how many
    deliveries this one has left.
    """
    shared_limiter().reset()
    yield
    shared_limiter().reset()


def _main_cohort() -> tuple[demo.CasePlan, ...]:
    """The batch's main cohort — the one that overflowed a window in the measured run."""
    plans = demo.plan_batch(case_count=_CASE_COUNT, prior_cohort_size=_PRIOR)
    return tuple(plan for plan in plans if plan.outcome not in demo.PRIOR_OUTCOMES)


def test_seeding_a_cohort_larger_than_the_rate_limit_accepts_every_delivery() -> None:
    """The whole cohort is seeded and the limiter refuses nothing.

    Both halves matter. "Every delivery accepted" alone would pass against a loader that raised
    the limit; "the limiter refused nothing" is the half that says the guard was still doing its
    job while the batch got in.
    """
    plans = _main_cohort()
    assert len(plans) > _LIMIT, "the cohort has to exceed one window's allowance to test anything"

    clock = ManualClock()
    transport = _RateLimitedTransport()
    worker = _CountingWorker()
    with using_clock(clock):
        pacer = demo._IngestPacer(clock.advance, _LIMIT)
        accepted = demo._seed_cohort(transport, _TENANT, plans, worker, pacer=pacer)

    assert accepted == len(plans)
    assert transport.refused == 0
    assert transport.accepted == len(plans)
    assert worker.drains == 1


def test_seeding_advances_only_as_far_as_the_rate_windows_it_needs() -> None:
    """The clock moves one window per exhausted allowance, and not one more.

    The bound is what makes the ageing negligible rather than merely small: a loader that advanced
    per delivery would move a 60-case cohort an hour, and a 1 000-case batch a day — which is
    ``RECOVERY_WINDOW_DURATION`` arithmetic, not a rounding error.
    """
    plans = _main_cohort()
    expected_windows = (len(plans) - 1) // _LIMIT

    clock = ManualClock()
    started = clock.now()
    with using_clock(clock):
        pacer = demo._IngestPacer(clock.advance, _LIMIT)
        demo._seed_cohort(_RateLimitedTransport(), _TENANT, plans, _CountingWorker(), pacer=pacer)
        elapsed = clock.now() - started

    assert pacer.advances == expected_windows
    assert elapsed == expected_windows * (WINDOW + demo._INGEST_WINDOW_MARGIN)


def test_a_refused_delivery_fails_the_batch_and_names_what_it_saw() -> None:
    """A shortfall raises, and the message carries the requested count, the accepted count and
    the statuses observed — the three things a reader needs to act without opening the logs."""
    plans = _main_cohort()
    refused = 7
    clock = ManualClock()
    worker = _CountingWorker()

    with using_clock(clock), pytest.raises(demo.SeedDeliveryShortfallError) as raised:
        demo._seed_cohort(
            _RefusingTransport(refused),
            _TENANT,
            plans,
            worker,
            pacer=demo._IngestPacer(clock.advance, _LIMIT),
        )

    error = raised.value
    assert error.requested == len(plans)
    assert error.accepted == len(plans) - refused
    assert error.statuses == {503: refused}
    message = str(error)
    assert str(len(plans)) in message
    assert str(len(plans) - refused) in message
    assert "HTTP 503: 7" in message
    assert worker.drains == 0, "a cohort that is already the wrong size is not worth draining"


def test_a_fully_seeded_cohort_does_not_raise() -> None:
    """The other side of the shortfall check: it must not fire on a batch that seeded fully.

    Written because a check that raises on the healthy case is indistinguishable from a broken
    loader, and the healthy case is the one every run is supposed to take.
    """
    plans = _main_cohort()
    clock = ManualClock()
    with using_clock(clock):
        accepted = demo._seed_cohort(
            _RefusingTransport(0),
            _TENANT,
            plans,
            _CountingWorker(),
            pacer=demo._IngestPacer(clock.advance, _LIMIT),
        )
    assert accepted == len(plans)


def test_paying_a_test_mode_payment_link_programmatically_is_not_available() -> None:
    """Design open question 15, pinned to the answer this build established.

    The Razorpay Payment Links API has no endpoint that pays a link, so R28.C2's payment step is a
    manual ``RUNBOOK.md`` action. The assertion exists so that a future change claiming the
    capability has to change this line too, rather than quietly turning
    ``verified_test_mode_recoveries`` back into a restatement of its own minimum.
    """
    assert demo.verified_test_mode_capability(object()) is False

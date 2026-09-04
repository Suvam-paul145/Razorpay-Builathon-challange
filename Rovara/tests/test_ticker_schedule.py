"""Every periodic sweep has a configured interval, and an unpriced kind stops the ticker.

This is the assertion that stops the next sweep kind being added without an interval. The
development script the ticker replaced mapped four kinds by hard-coded string literal and gave
everything else a 300-second fallback, so three of the seven sweeps ran on a number nobody
chose and a renamed constant would have silently joined them. Migration ``0014`` seeded the
three intervals that did not exist, ``_bucket_seconds`` keys on the constants, and there is no
fallback left — so a kind added to ``PERIODIC_SWEEP_KINDS`` without a bound fails here rather
than inheriting five minutes. Migration ``0016`` added the eighth kind, the promise sweep of
R23.C13, and both assertions below failed until ``PROMISE_SWEEP_INTERVAL`` existed — which is
the arrangement working rather than the test being brittle.

Pure tier: this is a mapping over the catalogue defaults, with no database and no clock.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from revora.jobs.scheduler import (
    CALIBRATION_REPORT_KIND,
    CASE_REVIEW_KIND,
    CUSTOMER_DATA_RETENTION_KIND,
    DETECTION_GAP_BACKFILL_KIND,
    EXECUTION_RECONCILIATION_KIND,
    LIFECYCLE_EVALUATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
    PERIODIC_SWEEP_KINDS,
    PROMISE_SWEEP_KIND,
)
from revora.jobs.ticker import UnscheduledSweepKindError, _bucket_seconds, _selected_kinds
from revora.platform.config import default_configuration

CONFIG = default_configuration()
"""The catalogue defaults, with nothing read from a database."""

EXPECTED_BOUND: dict[str, str] = {
    LIFECYCLE_EVALUATION_KIND: "LIFECYCLE_EVALUATION_INTERVAL",
    EXECUTION_RECONCILIATION_KIND: "EXECUTION_RECONCILIATION_INTERVAL",
    PAYMENT_STATE_RECONCILIATION_KIND: "PAYMENT_STATE_RECONCILIATION_INTERVAL",
    DETECTION_GAP_BACKFILL_KIND: "DETECTION_GAP_BACKFILL_INTERVAL",
    CALIBRATION_REPORT_KIND: "CALIBRATION_REPORT_INTERVAL",
    CUSTOMER_DATA_RETENTION_KIND: "CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL",
    CASE_REVIEW_KIND: "REVIEW_SWEEP_INTERVAL",
    PROMISE_SWEEP_KIND: "PROMISE_SWEEP_INTERVAL",
}
"""Which bound each sweep kind is scheduled on, written out a second time on purpose.

Deliberate duplication of the mapping inside ``_bucket_seconds``. A test that asked the
implementation which bound it used would agree with the implementation however the
implementation changed — including the specific change worth catching, where the retention
sweep is accidentally keyed on ``CUSTOMER_DATA_RETENTION`` (a 180-day retention *period*)
instead of ``CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL`` (an hourly sweep). Both are
``DURATION_SECONDS`` bounds on the same subject, so nothing but a second written-down mapping
distinguishes them."""


@pytest.mark.pure
def test_every_sweep_kind_has_an_expectation_here() -> None:
    """The table above covers ``PERIODIC_SWEEP_KINDS`` exactly.

    Without this, a further kind could be added and the parametrized test below would simply
    not cover it — a test suite that grows quieter as the system grows. It has already earned
    its keep once: the promise sweep of R23.C13 was added as the eighth kind and this is the
    assertion that failed until ``PROMISE_SWEEP_INTERVAL`` was seeded and mapped.
    """
    assert set(EXPECTED_BOUND) == set(PERIODIC_SWEEP_KINDS)


@pytest.mark.pure
@pytest.mark.parametrize("kind", PERIODIC_SWEEP_KINDS)
def test_every_sweep_kind_resolves_to_its_configured_bound_with_no_fallback(kind: str) -> None:
    """All eight are priced from configuration, and from the *right* bound.

    ``> 0`` matters separately from the equality: the resolved number is a divisor in the
    dedupe bucket arithmetic, and a zero would put every tick in one bucket forever — a sweep
    that runs once and then never again, which is indistinguishable from a working dedupe key.
    """
    expected: timedelta = getattr(CONFIG, EXPECTED_BOUND[kind])
    resolved = _bucket_seconds(kind, CONFIG)

    assert resolved == int(expected.total_seconds())
    assert resolved > 0


@pytest.mark.pure
def test_the_retention_sweep_is_not_scheduled_on_the_retention_period() -> None:
    """The one confusion this key was named to prevent, asserted rather than trusted.

    ``CUSTOMER_DATA_RETENTION`` is 180 days. A sweep on that interval would miss R17.C11's
    24-hour deadline by half a year, and because both bounds are durations about customer data
    the mistake type-checks, parses, seeds and runs.
    """
    resolved = timedelta(seconds=_bucket_seconds(CUSTOMER_DATA_RETENTION_KIND, CONFIG))

    assert resolved != CONFIG.CUSTOMER_DATA_RETENTION
    assert resolved < timedelta(hours=24), (
        "the retention sweep cannot meet R17.C11's 24-hour deadline on this interval"
    )


@pytest.mark.pure
def test_an_unpriced_sweep_kind_raises_rather_than_taking_a_default() -> None:
    with pytest.raises(UnscheduledSweepKindError) as caught:
        _bucket_seconds("an_eighth_sweep", CONFIG)
    assert "an_eighth_sweep" in str(caught.value)


@pytest.mark.pure
def test_a_bound_configured_at_zero_still_quantizes() -> None:
    """A merchant row of ``0`` must not collapse every tick into one bucket forever.

    Clamped rather than refused, matching ``enqueue_sweep``'s own ``max(1, ...)``: the two
    clamps have to agree, or the key the ticker computes and the key the scheduler stores would
    come from different divisors.
    """
    degenerate = dataclasses.replace(CONFIG, REVIEW_SWEEP_INTERVAL=timedelta(0))

    assert _bucket_seconds(CASE_REVIEW_KIND, degenerate) == 1


@pytest.mark.pure
def test_no_kind_filter_means_every_kind() -> None:
    assert _selected_kinds(None) == PERIODIC_SWEEP_KINDS


@pytest.mark.pure
def test_an_unknown_requested_kind_is_refused() -> None:
    """A typo in ``--only`` must not read as "every bucket was already occupied"."""
    with pytest.raises(ValueError, match="case_reviw"):
        _selected_kinds(("case_reviw",))

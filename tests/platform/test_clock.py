"""Clock: no naive datetime escapes, and the manual clock moves predictably."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from revora.platform.clock import (
    DEFAULT_MANUAL_START,
    FrozenClock,
    ManualClock,
    NaiveDatetimeError,
    SystemClock,
    ensure_utc,
    get_clock,
    now,
    reset_clock,
    set_clock,
    using_clock,
)


@pytest.mark.pure
def test_system_clock_returns_aware_utc() -> None:
    instant = SystemClock().now()
    assert instant.tzinfo is not None
    assert instant.utcoffset() == timedelta(0)


@pytest.mark.pure
def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2025, 6, 1, 12, 0, 0))


@pytest.mark.pure
def test_ensure_utc_converts_offset_bearing_value() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    converted = ensure_utc(datetime(2025, 6, 1, 17, 30, 0, tzinfo=ist))
    assert converted == datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert converted.utcoffset() == timedelta(0)


@pytest.mark.pure
def test_ensure_utc_rejects_non_datetime() -> None:
    with pytest.raises(TypeError):
        ensure_utc("2025-06-01T12:00:00Z")  # type: ignore[arg-type]


@pytest.mark.pure
def test_manual_clock_is_frozen_until_advanced() -> None:
    manual = ManualClock()
    first = manual.now()
    assert first == DEFAULT_MANUAL_START
    assert manual.now() == first

    returned = manual.advance(timedelta(hours=3))
    assert returned == first + timedelta(hours=3)
    assert manual.now() == first + timedelta(hours=3)


@pytest.mark.pure
def test_manual_clock_advances_cumulatively() -> None:
    manual = ManualClock(datetime(2025, 3, 1, tzinfo=UTC))
    for _ in range(4):
        manual.advance(timedelta(minutes=15))
    assert manual.now() == datetime(2025, 3, 1, 1, 0, tzinfo=UTC)


@pytest.mark.pure
def test_manual_clock_can_move_backwards_for_skew_scenarios() -> None:
    manual = ManualClock(datetime(2025, 3, 1, tzinfo=UTC))
    manual.advance(timedelta(seconds=-30))
    assert manual.now() == datetime(2025, 2, 28, 23, 59, 30, tzinfo=UTC)


@pytest.mark.pure
def test_manual_clock_rejects_naive_start_and_naive_set() -> None:
    with pytest.raises(NaiveDatetimeError):
        ManualClock(datetime(2025, 1, 1))
    manual = ManualClock()
    with pytest.raises(NaiveDatetimeError):
        manual.set_to(datetime(2025, 1, 1))


@pytest.mark.pure
def test_manual_clock_rejects_non_timedelta_advance() -> None:
    with pytest.raises(TypeError):
        ManualClock().advance(60)  # type: ignore[arg-type]


@pytest.mark.pure
def test_frozen_clock_is_the_manual_clock() -> None:
    assert FrozenClock is ManualClock


@pytest.mark.pure
def test_module_now_uses_the_installed_clock_and_restores_it() -> None:
    manual = ManualClock(datetime(2025, 7, 4, 9, 30, tzinfo=UTC))
    original = get_clock()
    with using_clock(manual):
        assert now() == datetime(2025, 7, 4, 9, 30, tzinfo=UTC)
        manual.advance(timedelta(days=1))
        assert now() == datetime(2025, 7, 5, 9, 30, tzinfo=UTC)
    assert get_clock() is original


@pytest.mark.pure
def test_using_clock_restores_even_after_failure() -> None:
    original = get_clock()
    with pytest.raises(RuntimeError), using_clock(ManualClock()):
        raise RuntimeError("boom")
    assert get_clock() is original


@pytest.mark.pure
def test_no_naive_datetime_escapes_a_substituted_clock() -> None:
    class BadClock:
        def now(self) -> datetime:
            return datetime(2025, 1, 1, 0, 0, 0)

    previous = set_clock(BadClock())
    try:
        with pytest.raises(NaiveDatetimeError):
            now()
    finally:
        set_clock(previous)


@pytest.mark.pure
def test_reset_clock_reinstalls_the_system_clock() -> None:
    previous = set_clock(ManualClock())
    try:
        reset_clock()
        assert isinstance(get_clock(), SystemClock)
    finally:
        set_clock(previous)

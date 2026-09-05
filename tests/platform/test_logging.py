"""Structured logging: the correlation id rides along, and contacts do not."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from revora.platform.clock import ManualClock, using_clock
from revora.platform.logging import (
    CORRELATION_ID_FIELD,
    JsonFormatter,
    RevoraLogger,
    clear_correlation_id,
    configure_logging,
    correlation_context,
    current_correlation_id,
    new_correlation_id,
    set_correlation_id,
)

CONTACT = "+919876543210"
SHORT_URL = "https://rzp.io/i/aBcD1234"
FIXED_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def captured() -> Iterator[tuple[RevoraLogger, io.StringIO]]:
    """A logger writing JSON into a buffer, with the real root config restored after."""
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        configure_logging(level=logging.DEBUG, stream=stream)
        yield RevoraLogger(logging.getLogger("revora.test")), stream
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)
        clear_correlation_id()


def emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.mark.pure
def test_record_is_json_with_the_expected_envelope(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured
    with using_clock(ManualClock(FIXED_NOW)), correlation_context("corr-1"):
        logger.info("case detected", case_id="case-1")

    (record,) = emitted(stream)
    assert record["level"] == "INFO"
    assert record["logger"] == "revora.test"
    assert record["message"] == "case detected"
    assert record["case_id"] == "case-1"
    assert record["timestamp"] == "2025-06-01T12:00:00+00:00"


@pytest.mark.pure
def test_record_carries_the_ambient_correlation_id(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured
    with correlation_context("corr-abc"):
        logger.info("first")
        logger.warning("second")

    records = emitted(stream)
    assert [record[CORRELATION_ID_FIELD] for record in records] == ["corr-abc", "corr-abc"]


@pytest.mark.pure
def test_outside_a_context_the_id_is_explicitly_unset(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured
    logger.info("no context here")
    (record,) = emitted(stream)
    assert record[CORRELATION_ID_FIELD] == "unset"


@pytest.mark.pure
def test_context_is_restored_after_nesting() -> None:
    outer = set_correlation_id("outer")
    try:
        with correlation_context("inner"):
            assert current_correlation_id() == "inner"
        assert current_correlation_id() == "outer"
    finally:
        clear_correlation_id(outer)


@pytest.mark.pure
def test_asynchronously_scheduled_work_inherits_the_id(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured

    async def scenario() -> None:
        with correlation_context("corr-async"):
            await asyncio.gather(
                asyncio.create_task(_log_later(logger, "scheduled-a")),
                asyncio.create_task(_log_later(logger, "scheduled-b")),
            )

    asyncio.run(scenario())

    # The event loop logs about itself at DEBUG; only our own records are the subject.
    records = [record for record in emitted(stream) if record["logger"] == "revora.test"]
    assert len(records) == 2
    assert {record[CORRELATION_ID_FIELD] for record in records} == {"corr-async"}


async def _log_later(logger: RevoraLogger, message: str) -> None:
    await asyncio.sleep(0)
    logger.info(message)


@pytest.mark.pure
def test_a_contact_passed_as_a_field_is_masked(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured
    with correlation_context("corr-mask"):
        logger.info("link created", contact=CONTACT, provider_short_url=SHORT_URL)

    raw = stream.getvalue()
    assert CONTACT not in raw
    assert SHORT_URL not in raw

    (record,) = emitted(stream)
    assert record["contact"].endswith("3210")
    assert record["contact"].startswith("XXXX")
    assert set(record["provider_short_url"]) == {"X"}


@pytest.mark.pure
def test_nested_fields_are_masked(captured: tuple[RevoraLogger, io.StringIO]) -> None:
    logger, stream = captured
    logger.info(
        "canonicalized",
        payload={"payment": {"entity": {"id": "pay_ABC", "contact": CONTACT}}},
    )
    raw = stream.getvalue()
    assert CONTACT not in raw
    assert "pay_ABC" in raw


@pytest.mark.pure
def test_stdlib_extra_is_masked_too(captured: tuple[RevoraLogger, io.StringIO]) -> None:
    _, stream = captured
    # A call site that bypasses RevoraLogger must not bypass masking.
    logging.getLogger("revora.legacy").info("direct call", extra={"email": "buyer@example.com"})
    raw = stream.getvalue()
    assert "buyer@example.com" not in raw
    assert json.loads(raw)["email"].endswith(".com")


@pytest.mark.pure
def test_correlation_id_survives_masking() -> None:
    # A uuid ends in hex characters; the masker must not treat the trace key as
    # a sensitive value and destroy the join.
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="revora.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="anything",
        args=(),
        exc_info=None,
    )
    correlation_id = new_correlation_id()
    token = set_correlation_id(correlation_id)
    try:
        payload = json.loads(formatter.format(record))
    finally:
        clear_correlation_id(token)
    assert payload[CORRELATION_ID_FIELD] == correlation_id


@pytest.mark.pure
def test_exception_is_reduced_to_type_and_message(
    captured: tuple[RevoraLogger, io.StringIO],
) -> None:
    logger, stream = captured
    try:
        raise ValueError("provider refused")
    except ValueError:
        logger.exception("execution failed", case_id="case-9")

    (record,) = emitted(stream)
    assert record["error_type"] == "ValueError"
    assert record["error_message"] == "provider refused"
    assert "Traceback" not in stream.getvalue()

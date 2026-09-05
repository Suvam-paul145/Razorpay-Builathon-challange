"""The SQL tracer: off unless asked, and correct across the boundary that makes it hard.

Two claims are worth a test here and the rest is arithmetic.

**Off means nothing is registered.** The whole justification for gating a statement counter on an
environment variable rather than on a log level is that an unread diagnostic should cost nothing at
all, and "nothing at all" is a claim about whether a listener exists — not about how cheaply it
returns. So the assertion is against SQLAlchemy's own registry rather than against a flag of ours.

**The counters survive the threadpool hop.** Every handler in this API is a synchronous ``def``,
which Starlette runs in a worker thread while the middleware that opens the measurement stays on the
event loop thread. ``anyio`` gives the worker a *copy* of the context, so a context var holding an
integer would be incremented in the copy and read as zero by the middleware. The tests below
reproduce that hop with :func:`contextvars.copy_context` — which is the mechanism ``anyio`` itself
uses — rather than by standing up an event loop, so the property is checked at the level it is
actually decided.

The listeners are invoked directly with a stub connection. A real engine would need a real database
to execute against, and what is under test is the bookkeeping, not whether SQLAlchemy emits the
events it documents.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, event

from revora.platform import sqltrace
from revora.platform.sqltrace import ENV_SQL_TRACE, SqlTrace


class _StubConnection:
    """Just the ``info`` mapping the listeners use, because that is all they touch."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


def _fire(connection: _StubConnection, *, nested: int = 0) -> None:
    """One statement's ``before``/``after`` pair, optionally with ``nested`` inside it."""
    sqltrace._before_cursor_execute(connection, None, "SELECT 1", None, None, False)  # type: ignore[arg-type]
    for _ in range(nested):
        _fire(connection)
    sqltrace._after_cursor_execute(connection, None, "SELECT 1", None, None, False)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _uninstalled() -> Iterator[None]:
    """Leave the process as the tracer found it: no listeners, module flag cleared.

    Reaches for the private names deliberately. The tracer has no uninstall on purpose — a
    production module does not need one, and adding one so a test can undo itself would put a
    supported way to detach the listeners mid-process into the API. The alternative is this fixture,
    where the reversal is visibly test-only.
    """
    yield
    for name, listener in (
        ("before_cursor_execute", sqltrace._before_cursor_execute),
        ("after_cursor_execute", sqltrace._after_cursor_execute),
    ):
        if event.contains(Engine, name, listener):
            event.remove(Engine, name, listener)
    sqltrace._installed = False


@pytest.mark.pure
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " on "])
def test_the_switch_accepts_the_spellings_somebody_would_reach_for(raw: str) -> None:
    assert sqltrace.enabled({ENV_SQL_TRACE: raw}) is True


@pytest.mark.pure
@pytest.mark.parametrize(
    "environ",
    [{}, {ENV_SQL_TRACE: ""}, {ENV_SQL_TRACE: "   "}, {ENV_SQL_TRACE: "0"},
     {ENV_SQL_TRACE: "false"}, {ENV_SQL_TRACE: "off"}, {ENV_SQL_TRACE: "maybe"}],
)
def test_anything_unrecognised_reads_as_off(environ: dict[str, str]) -> None:
    """Including a value nobody meant. The safe reading of a garbled diagnostic switch is off."""
    assert sqltrace.enabled(environ) is False


@pytest.mark.pure
def test_with_the_switch_off_no_listener_is_registered_at_all() -> None:
    """The zero-overhead claim, asserted against SQLAlchemy's registry rather than against a flag.

    A listener that checked a flag and returned early would pass a test that only looked at the
    counters. This is the assertion that would fail if somebody replaced the gate with one.
    """
    assert sqltrace.install({}) is False
    assert sqltrace.installed() is False
    assert not event.contains(Engine, "before_cursor_execute", sqltrace._before_cursor_execute)
    assert not event.contains(Engine, "after_cursor_execute", sqltrace._after_cursor_execute)


@pytest.mark.pure
def test_with_the_switch_on_both_listeners_are_registered_once() -> None:
    """Twice-installed would count every statement twice, and two apps in one process is normal."""
    assert sqltrace.install({ENV_SQL_TRACE: "1"}) is True
    assert sqltrace.install({ENV_SQL_TRACE: "1"}) is True
    assert sqltrace.installed() is True
    assert event.contains(Engine, "before_cursor_execute", sqltrace._before_cursor_execute)
    assert event.contains(Engine, "after_cursor_execute", sqltrace._after_cursor_execute)

    # ``contains`` answers the same whether the listener was attached once or twice, and a double
    # registration would count every statement twice. One removal is what distinguishes them: it
    # detaches one registration, so ``contains`` goes false only if there was exactly one.
    event.remove(Engine, "before_cursor_execute", sqltrace._before_cursor_execute)
    assert not event.contains(Engine, "before_cursor_execute", sqltrace._before_cursor_execute)


@pytest.mark.pure
def test_outside_a_scope_nothing_is_recorded() -> None:
    """The worker, the ticker and the startup schema check all run statements with no scope open."""
    connection = _StubConnection()
    _fire(connection)
    assert sqltrace.current_trace() is None
    assert connection.info.get("revora_sqltrace_starts", []) == []


@pytest.mark.pure
def test_a_scope_counts_its_statements_and_charges_time_to_them() -> None:
    connection = _StubConnection()
    with sqltrace.trace_scope() as trace:
        _fire(connection)
        _fire(connection)
        assert sqltrace.current_trace() is trace
    assert trace.statements == 2
    assert trace.db_nanoseconds > 0
    assert trace.db_micros == trace.db_nanoseconds // 1_000


@pytest.mark.pure
def test_nested_statements_are_counted_and_the_stack_unwinds() -> None:
    """A ``before``/``after`` pair can nest, which is why the start times are a stack."""
    connection = _StubConnection()
    with sqltrace.trace_scope() as trace:
        _fire(connection, nested=2)
    assert trace.statements == 3
    assert connection.info["revora_sqltrace_starts"] == []


@pytest.mark.pure
def test_the_scope_is_restored_rather_than_cleared_on_the_way_out() -> None:
    """A nested scope hands the outer accumulator back; discarding it would lose the outer count."""
    connection = _StubConnection()
    with sqltrace.trace_scope() as outer:
        _fire(connection)
        with sqltrace.trace_scope() as inner:
            _fire(connection)
        assert sqltrace.current_trace() is outer
    assert outer.statements == 1
    assert inner.statements == 1
    assert sqltrace.current_trace() is None


@pytest.mark.pure
def test_counters_mutated_in_a_worker_thread_are_visible_to_the_scope_that_opened_it() -> None:
    """The property the whole design rests on, reproduced at the level ``anyio`` decides it.

    ``contextvars.copy_context()`` then ``Context.run`` in another thread is what
    ``anyio.to_thread.run_sync`` does, and therefore what Starlette does with every synchronous
    handler in this API. A context var holding an ``int`` would be rebound inside the copy and this
    assertion would read zero.
    """
    connection = _StubConnection()
    with sqltrace.trace_scope() as trace:
        context = contextvars.copy_context()

        def in_worker() -> None:
            assert sqltrace.current_trace() is trace
            _fire(connection)
            _fire(connection)

        worker = threading.Thread(target=lambda: context.run(in_worker))
        worker.start()
        worker.join()

        assert trace.statements == 2


@pytest.mark.pure
def test_a_bare_thread_sees_no_scope_which_is_why_threading_local_was_rejected() -> None:
    """The rejected alternative, recorded as a test rather than only as a comment.

    A thread that did not inherit the context finds nothing — so a ``threading.local`` written by
    the middleware on the event loop thread would be invisible to the handler's worker thread, and
    every request would report zero statements.
    """
    seen: list[SqlTrace | None] = []
    with sqltrace.trace_scope():
        worker = threading.Thread(target=lambda: seen.append(sqltrace.current_trace()))
        worker.start()
        worker.join()
    assert seen == [None]


@pytest.mark.pure
def test_a_statement_whose_after_lands_outside_its_own_scope_is_dropped() -> None:
    """Counting it would charge a scope for time it did not spend."""
    connection = _StubConnection()
    sqltrace._before_cursor_execute(connection, None, "SELECT 1", None, None, False)  # type: ignore[arg-type]
    with sqltrace.trace_scope() as trace:
        sqltrace._after_cursor_execute(connection, None, "SELECT 1", None, None, False)  # type: ignore[arg-type]
    assert trace.statements == 0

"""Shared test configuration.

Two things happen here that matter for the whole suite.

**The clock is reset between tests.** ``platform.clock`` holds the installed clock
in a module global, deliberately, so a test that installs a ``ManualClock`` and
then fails part way through would otherwise leave a frozen clock installed for
every test after it. That produces a cascade of unrelated failures whose real cause
is three files away.

**Hypothesis gets explicit profiles.** The design's Cost Tiers table says the pure
arithmetic properties run at 500 examples because they are microsecond-cheap, and
the database-backed ones run with no deadline because a container round-trip is not
a timing signal. Encoding that here keeps the numbers out of every individual test
and makes the tiering a property of the suite rather than of whoever wrote the last
test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, Verbosity, settings

from revora.platform import clock

pytest_plugins = ("tests.pg_support",)
"""The migrated-PostgreSQL fixtures, registered suite-wide.

Here rather than in a per-directory conftest because two tiers need them — the persistence
constraint tests and the synthetic evidence harness — and a conftest's fixtures are visible only
to tests below it. Registering the plugin costs nothing at collection: every fixture in it is
session-scoped and lazy, so a run that touches no ``pg`` test never contacts a database.

Plugin registration has to happen in the *root* conftest; pytest rejects ``pytest_plugins`` in a
nested one."""

settings.register_profile(
    "default",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "pure",
    max_examples=500,
    deadline=None,
)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "debug",
    max_examples=20,
    verbosity=Verbosity.verbose,
    deadline=None,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _restore_real_clock() -> Iterator[None]:
    """Guarantee every test starts and ends on the real clock."""
    clock.reset_clock()
    yield
    clock.reset_clock()


@pytest.fixture
def manual_clock() -> Iterator[clock.ManualClock]:
    """A frozen clock, installed for the duration of the test."""
    manual = clock.ManualClock()
    with clock.using_clock(manual):
        yield manual

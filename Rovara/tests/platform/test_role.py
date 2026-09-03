"""Role: three known values, forgiving about presentation, unforgiving about spelling.

The forgiveness and the refusal are the same decision looked at from two sides, and both
matter enough to assert. ``" TICKER "`` is unambiguous and refusing it would cost a deploy for
nothing. ``"tickr"`` is a typo, and defaulting it to anything at all produces a deployment
whose failure is silent for hours: an extra API process and no schedule looks exactly like a
healthy system until somebody notices that no case has expired since the release.
"""

from __future__ import annotations

import pytest

from revora.platform.role import ENV_ROLE, Role, RoleConfigurationError, current_role


@pytest.mark.pure
def test_three_roles_are_declared() -> None:
    """The enum is the authoritative role list; the Dockerfile dispatches on it.

    Asserted as an exact set rather than a membership check, so adding a role without
    adding its entrypoint fails here — which is the cheap place to find out.
    """
    assert {role.value for role in Role} == {"api", "worker", "ticker"}


@pytest.mark.pure
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api", Role.API),
        ("worker", Role.WORKER),
        ("ticker", Role.TICKER),
        ("TICKER", Role.TICKER),
        ("Ticker", Role.TICKER),
        ("  ticker  ", Role.TICKER),
        ("\tticker\n", Role.TICKER),
    ],
)
def test_every_role_round_trips_with_case_and_whitespace_forgiven(
    raw: str, expected: Role
) -> None:
    assert current_role({ENV_ROLE: raw}) is expected


@pytest.mark.pure
def test_the_ticker_value_is_what_compose_and_the_dockerfile_write() -> None:
    """``REVORA_ROLE: ticker`` in ``docker-compose.yml`` has to be this exact string.

    Written out rather than read off the enum, deliberately: an assertion that asked the enum
    what its value was would agree with the enum however the enum changed, including a rename
    that left the compose file and the deployment pointing at a role that no longer exists.
    """
    assert Role.TICKER.value == "ticker"


@pytest.mark.pure
@pytest.mark.parametrize("raw", ["tickr", "TICKERS", "scheduler", "cron", "api,worker", "0"])
def test_an_unknown_role_is_refused_and_the_message_names_the_permitted_values(
    raw: str,
) -> None:
    with pytest.raises(RoleConfigurationError) as caught:
        current_role({ENV_ROLE: raw})
    message = str(caught.value)
    assert "api" in message and "worker" in message and "ticker" in message


@pytest.mark.pure
@pytest.mark.parametrize("environ", [{}, {ENV_ROLE: ""}, {ENV_ROLE: "   "}])
def test_an_absent_or_blank_role_is_refused(environ: dict[str, str]) -> None:
    """Refused rather than defaulted to ``api``. See the module docstring in ``role.py``."""
    with pytest.raises(RoleConfigurationError):
        current_role(environ)

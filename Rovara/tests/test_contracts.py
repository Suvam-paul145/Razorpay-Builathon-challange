"""Structural contracts that no runtime test can establish.

Three claims in the design are about the *shape* of the codebase rather than its
behaviour, so no amount of exercising the system can verify them:

* The policy engine cannot read AI output, because it cannot import the module that
  produces it. That is Property 2's structural half.
* The domain layer depends on nothing but the standard library, which is what makes
  the money arithmetic testable with zero setup.
* No currency-bearing module contains a float.

They are checked here as tests, not only as CI steps, so a developer who breaks one
finds out from the same command that runs everything else rather than from a build
failure twenty minutes later.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.pure
def test_import_contracts_hold() -> None:
    """``lint-imports`` passes.

    This is the mechanism behind the claim that no AI-produced field can reach a
    policy decision. The policy module is forbidden from importing the reasoning,
    estimation, optimizer and memory packages, so the isolation is a property of the
    dependency graph rather than a convention someone has to remember.
    """
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "import contracts are broken — the policy engine's AI isolation or the "
        f"domain layer's purity has been compromised:\n\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.pure
def test_no_float_in_currency_modules() -> None:
    """No ``float`` appears in a module that computes money.

    ``mypy --strict`` would accept a function that takes a ``float`` and returns a
    ``float``; the annotations are consistent. This catches what the type checker
    cannot object to.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_no_float.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"a float has entered a currency-bearing module:\n\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.pure
def test_domain_imports_only_stdlib() -> None:
    """The domain package imports nothing from ``revora`` outside ``revora.domain``.

    Checked directly as well as through ``lint-imports``, because this one is worth a
    failure message that names the offending file and line rather than a contract id.
    """
    domain = REPO_ROOT / "revora" / "domain"
    offences: list[str] = []
    for path in sorted(domain.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "revora." in stripped and "revora.domain" not in stripped:
                offences.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}: {stripped}")
    assert not offences, (
        "revora.domain must import only the standard library and its own submodules:\n"
        + "\n".join(offences)
    )

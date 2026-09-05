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

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_LINT_IMPORTS_ENTRYPOINT = (
    "import sys; from importlinter.cli import lint_imports_command; "
    "sys.exit(lint_imports_command(standalone_mode=False))"
)
"""How to run ``lint-imports`` in a subprocess without guessing where its script lives.

Not ``-m importlinter``: the package has no ``__main__``. Not ``-m importlinter.cli``
either — that is what this test used to do, and because ``cli`` is a module of click
commands with no ``__main__`` guard, it imported cleanly, ran nothing and exited 0. Not
the ``lint-imports`` console script by path, because its location and extension differ by
platform and virtual-environment layout.

Calling the click command object directly works everywhere the library is installed.
``standalone_mode=False`` stops click from calling ``sys.exit`` itself, so the command's
return value is what decides the exit status."""


@pytest.mark.pure
def test_import_contracts_hold() -> None:
    """``lint-imports`` passes.

    This is the mechanism behind the claim that no AI-produced field can reach a
    policy decision. The policy module is forbidden from importing the reasoning,
    estimation, optimizer and memory packages, so the isolation is a property of the
    dependency graph rather than a convention someone has to remember.
    """
    result = subprocess.run(
        [sys.executable, "-c", _LINT_IMPORTS_ENTRYPOINT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "import contracts are broken — the policy engine's AI isolation, the domain "
        "layer's purity, or the containment of synthetic ground truth has been "
        f"compromised:\n\n{result.stdout}\n{result.stderr}"
    )
    # The entrypoint must actually have run something. This test spent its whole life
    # passing vacuously: it invoked `python -m importlinter.cli`, and `importlinter` has
    # no `__main__`, so that command imported a module, did nothing, and exited 0. A
    # deliberately broken contract was reported by the console script and not by this
    # test, which is how the hole was found. A guard that cannot fail is worse than no
    # guard, because it is counted as coverage.
    assert "Contracts:" in result.stdout, (
        "the import-contract check produced no contract report, so it did not run:\n\n"
        f"{result.stdout}\n{result.stderr}"
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

    Uses the AST rather than a line scan, and that change was earned: the lexical version
    matched any *line* beginning with ``import`` or ``from`` that mentioned another
    ``revora`` package, which a wrapped sentence in a docstring does perfectly well. It
    fired on the prose explaining why a constant lives in the domain — a false positive
    that says nothing about imports and costs a real debugging detour.

    The AST cannot be fooled by prose, because a docstring is a string node and
    ``ast.walk`` only yields ``Import`` and ``ImportFrom`` for genuine statements. It also
    catches what the line scan missed: an import nested inside a function body, which is
    the way a layering violation actually tends to get added.
    """
    domain = REPO_ROOT / "revora" / "domain"
    offences: list[str] = []

    for path in sorted(domain.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `level > 0` is a relative import, which cannot leave the package.
                modules = [node.module] if node.module and node.level == 0 else []

            for module in modules:
                if module.startswith("revora.") and not module.startswith("revora.domain"):
                    offences.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {module}"
                    )

    assert not offences, (
        "revora.domain must import only the standard library and its own submodules:\n"
        + "\n".join(offences)
    )


@pytest.mark.pure
def test_judge_credentials_contract_constants() -> None:
    """The dedicated evaluator credentials match the hackathon specification."""
    from revora.api.auth import JUDGE_MERCHANT_SLUG, JUDGE_OPERATOR_KEY

    assert JUDGE_MERCHANT_SLUG == "razorpay-judge"
    assert JUDGE_OPERATOR_KEY == "razorpay-pass"


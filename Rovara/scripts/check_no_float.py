#!/usr/bin/env python
"""Fail if ``float`` appears anywhere in a currency-bearing module.

Requirement 7 puts every currency figure in integer minor units. That is enforced
three ways: the database columns are ``BIGINT``, ``mypy --strict`` covers the
modules that compute money, and this script makes the prohibition lexical.

The lexical check is the one that catches the case the other two miss — a helper
that takes a ``float`` parameter, does arithmetic that happens to type-check
because the annotation says ``float``, and quietly reintroduces binary rounding
error into a revenue figure. mypy is happy with that. This is not.

Run as part of the test suite and as a CI step. Exits 1 on a violation and names
the file and line, so the failure is actionable without reading this file.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CURRENCY_BEARING: tuple[str, ...] = (
    "revora/domain/money.py",
    "revora/domain/probability.py",
    "revora/optimizer",
    "revora/metrics",
    "revora/estimation",
)
"""Paths where a float is a bug rather than a style question.

``estimation`` is included even though it will eventually hold model code: a model
may use floats internally, but anything it hands to the optimizer is a ``Decimal``
probability or an integer cost. If that ever stops being true, the exclusion
belongs in this list with a reason next to it, not as a silent local override.
"""

ALLOWED_SUBSTRINGS: tuple[str, ...] = (
    "no float",
    "not a float",
    "floats are not permitted",
    "reintroduce",
    "float would",
    "never a float",
    "must not be built from a float",
)
"""Comment and docstring text that legitimately mentions the word while forbidding
it. Checked case-insensitively against the whole line."""


def _iter_target_files() -> list[Path]:
    files: list[Path] = []
    for entry in CURRENCY_BEARING:
        target = REPO_ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


def _line_is_prose(line: str) -> bool:
    """True if the mention sits in a comment or a docstring that forbids floats."""
    lowered = line.lower()
    return any(phrase in lowered for phrase in ALLOWED_SUBSTRINGS)


def _float_offences(path: Path) -> list[tuple[int, str]]:
    """Every line in ``path`` that uses ``float`` as code rather than as prose.

    Uses the AST for the cases that matter — a ``float`` annotation, a ``float()``
    call, a float literal — and falls back to a lexical scan so that a mention
    inside a string used as a type does not slip past.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    offences: list[tuple[int, str]] = []
    seen: set[int] = set()

    def record(lineno: int) -> None:
        if lineno in seen or lineno > len(lines):
            return
        line = lines[lineno - 1]
        if _line_is_prose(line):
            return
        seen.add(lineno)
        offences.append((lineno, line.strip()))

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a syntax error fails elsewhere
        return [(exc.lineno or 0, f"could not parse: {exc.msg}")]

    for node in ast.walk(tree):
        names_float = isinstance(node, ast.Name) and node.id == "float"
        attr_float = isinstance(node, ast.Attribute) and node.attr == "float"
        literal_float = isinstance(node, ast.Constant) and isinstance(node.value, float)
        if names_float or attr_float or literal_float:
            record(node.lineno)

    return sorted(offences)


def main() -> int:
    violations: list[tuple[Path, int, str]] = []
    checked = 0

    for path in _iter_target_files():
        checked += 1
        for lineno, text in _float_offences(path):
            violations.append((path, lineno, text))

    if not violations:
        print(f"no-float check: {checked} currency-bearing file(s) clean")
        return 0

    print("no-float check FAILED\n")
    print("Money in Revora is an integer count of minor units. A float in one of")
    print("these modules is how a total stops matching the sum of its rows.\n")
    for path, lineno, text in violations:
        relative = path.relative_to(REPO_ROOT).as_posix()
        print(f"  {relative}:{lineno}: {text}")
    print(
        "\nUse Decimal for probabilities and int for money. If a mention is prose "
        "that forbids floats, phrase it so it matches ALLOWED_SUBSTRINGS."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

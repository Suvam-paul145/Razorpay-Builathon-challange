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
    "revora/domain/segments.py",
    "revora/optimizer",
    "revora/metrics",
    "revora/estimation",
    "revora/execution",
    "revora/outcome",
    "revora/providers/payment_link.py",
    "revora/experiment",
    "revora/memory",
    "revora/synthetic",
    "revora/api",
    "revora/customer",
    "revora/reasoning",
    "revora/timeline",
)
"""Paths where a float is a bug rather than a style question.

``estimation`` is included even though it will eventually hold model code: a model
may use floats internally, but anything it hands to the optimizer is a ``Decimal``
probability or an integer cost. If that ever stops being true, the exclusion
belongs in this list with a reason next to it, not as a silent local override.

``execution``, ``outcome`` and ``providers/payment_link.py`` were added when Phase 3
landed, and the omission is worth recording because a probe caught it rather than a
reader: the guard reported "12 files clean" while the three modules that actually
send an amount to a payment provider and record a recovered amount were outside its
scope. These are the last places a float could do damage — ``payment_link`` puts the
figure on the wire, ``execution`` decides what to send, and ``outcome`` decides what
to report as recovered — so a rounding error here is a customer charged the wrong
amount or a revenue figure that does not match its own rows.

``domain/segments.py`` is listed explicitly rather than by directory, and that is the
lesson from moving it: it was covered while it sat under ``revora/estimation`` and
silently stopped being covered the moment it moved to ``revora/domain``, where only two
named files are checked. The count in this script's own output is what caught it — 23
files became 22 with no file deleted. A path-based guard needs its paths re-checked
whenever code moves, so the count is worth reading rather than skimming.

``experiment`` and ``memory`` joined in Phase 4. ``experiment`` is arguably the most
important entry in this list: it computes the lift interval, and a float there could
narrow an interval enough to exclude zero — which is the single condition that unlocks
a claim of incremental revenue. A rounding error elsewhere misplaces a paisa; one here
manufactures a causal claim.

``synthetic`` joined with it, and its inclusion is about a subtler failure than a wrong
amount. The generator compares an integer draw against an integer probability to decide
which side of its own ground truth a case falls on. In floating point, a case sitting
exactly on a boundary could be recorded as recovering while the true-lift arithmetic
counted it as not — so the answer sheet and the world it describes would disagree, and
every measured-versus-true comparison built on them would be quietly wrong in a
direction nobody could predict. The design sketched ``numpy.random.default_rng`` here;
this entry is what keeps the integer implementation from drifting back toward it.

``api`` joined with the dashboard, and it is the last place a currency figure is touched
before it leaves the system. ``api/rendering.py`` divides minor units by a power of ten
to produce the string a merchant reads, and that is exactly the operation somebody would
"simplify" into ``value / 100``. The integer division and ``divmod`` there are what make
the rendered string agree with the stored integer beside it; a float would let the two
disagree in the last digit, on the surface where the disagreement is most visible and
least explicable.

``customer`` joined with the customer response loop, and it is listed *before* it holds a
currency figure rather than after. The customer page presents an amount, formatted on the
server from the stored integer, to the one person who is being asked to pay it — so this is
the surface where a rounding error is read by the payer rather than by an operator. A guard
added once the projection exists is a guard added after the commit that could have needed it.

``reasoning`` joined with the customer response loop too, and the figure it protects is not a
currency one — it is ``confidence``. ``revora/reasoning/schemas.py`` already parses every
response body with ``parse_float=Decimal`` precisely so a model's confidence never passes
through binary form, and the reason that matters is comparison: a confidence compared against
``AI_CONFIDENCE_CEILING`` and against ``DIAGNOSIS_CONFIDENCE_FLOOR`` must give the same answer
twice, and a value that went through binary representation on the way in cannot promise that.
The module says so in prose; this entry is what keeps the promise after somebody "simplifies"
the parse.

``timeline`` joined in task 50, when the package it names came into existence — and the two
halves of that sentence are the entry's whole justification. A path naming a missing directory
is silently skipped by :func:`_iter_target_files`, so listing ``revora/timeline`` while it was
empty would have grown this list without growing the guard, and the file count below would not
have moved to say so. It moved from 62 to 65 when the package landed, which is the only
evidence available that the addition took effect. The figure it protects is a currency one at
one remove: the timeline presents no amount it computed, only the pre-formatted string the
server's renderer produced — and *that* is the reason a float here would be hard to notice.
A helper that took the minor units and divided them to build its own sentence would produce a
figure disagreeing with the one beside it on the same screen, and every other guard in the
system would stay green, because the disagreement would be between two presentations rather
than between a total and its rows.
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

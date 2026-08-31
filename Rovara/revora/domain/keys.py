"""The execution key. One construction, in the one place both callers can reach.

This string has three names and is a single value:

* the **``Idempotency_Key``** minted on a policy decision and stored on the execution
  intent,
* the provider's **``reference_id``** on the payment link,
* the **query parameter** reconciliation uses to ask "does this effect already exist?".

Exactly-once execution rests entirely on those three being the same string. If they
ever diverge, a retried execution asks the provider about an object that cannot exist,
concludes nothing was created, and creates a second payment link — a customer charged
or messaged twice, with nothing in the system noticing.

**Why this lives in ``domain`` rather than beside the provider client.** Two modules
need it: ``revora.policy``, which mints the key when it approves an action, and
``revora.providers``, which puts it on the wire. The ``policy-isolation`` import
contract forbids ``revora.policy`` from importing ``revora.providers`` — deliberately,
because the policy engine's purity is what makes Property 2 and R8.C14 checkable. So
the key cannot be defined in ``providers`` and imported by ``policy``, and defining it
twice is precisely the duplication that "single source of truth" exists to prevent.
``revora.domain`` is importable from everywhere, and this module needs only ``hashlib``
from the standard library, so it satisfies the domain-purity contract without
exception.

**The format**, from the design's Outbound Contract:

    ``rv_`` + the first 16 hex characters of ``SHA-256(case_id ‖ action ‖ ordinal)``

19 characters, comfortably inside the provider's verified 40-character ``reference_id``
limit. Derived rather than random, because a random key regenerated after a crash would
be a different key and the reconciliation read would answer about the wrong object.
Truncated to 16 hex characters — 64 bits — which is ample: the values being
distinguished are the attempts of one merchant's cases, not a global namespace, and a
collision would require two distinct triples hashing to the same 64 bits.

The separator is ``\\x1f``, the ASCII unit separator, and it is load-bearing. Bare
concatenation is not an injective encoding: case ``…1`` at ordinal 12 and case ``…11``
at ordinal 2 produce identical bytes and therefore the same key, which would be two
attempts sharing one provider object. ``\\x1f`` cannot occur in a UUID, in a
``CandidateAction`` member name, or in a decimal integer, so the encoding is
unambiguous.
"""

from __future__ import annotations

import hashlib
from typing import Final

__all__ = [
    "KEY_HEX_LENGTH",
    "KEY_SEPARATOR",
    "MAX_REFERENCE_ID_LENGTH",
    "REFERENCE_ID_PREFIX",
    "ExecutionKeyError",
    "execution_key",
]

REFERENCE_ID_PREFIX: Final[str] = "rv_"
"""Marks the key as Revora's in the provider dashboard, where a human debugging a
payment link sees ``reference_id`` and nothing else identifying its origin."""

KEY_HEX_LENGTH: Final[int] = 16
"""Hex characters of digest retained — 64 bits. See the module docstring on why
truncation is safe here."""

KEY_SEPARATOR: Final[str] = "\x1f"
"""ASCII unit separator, so the hashed input is an injective encoding of the triple
rather than an ambiguous concatenation of it."""

MAX_REFERENCE_ID_LENGTH: Final[int] = 40
"""The provider's verified ``reference_id`` limit. Asserted against rather than
assumed: the derived key is 19 characters, and this is what would catch a future format
change that pushed it over."""


class ExecutionKeyError(ValueError):
    """The key could not be derived from the values supplied.

    Raised rather than returning a sentinel, because every caller is in a position where
    a malformed key is a programming error discovered *before* any external call — the
    policy engine minting an approval, or the execution engine building a request while
    it still holds the case lock. A raise there costs a rolled-back transaction; a
    silently wrong key costs a duplicate payment link.

    ``rule`` names the failed validation so an audit record can state it without
    re-deriving it from a message.
    """

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        super().__init__(f"{rule}: {detail}" if detail else rule)


def execution_key(case_id: object, action: str, attempt_ordinal: int) -> str:
    """The deterministic execution key for one ``(case, action, attempt)`` triple.

    Pure, and stable across processes, machines and Python versions: SHA-256 over UTF-8
    of an unambiguous encoding. No salt, no clock, no randomness — a key that changed
    between two executions of the same ordinal would defeat the entire mechanism.

    Args:
        case_id: the recovery case identifier. A ``UUID`` or its string form; rendered
            with ``str`` so both yield the same key for the same case, because the
            engine holds a ``UUID`` and the provider ``notes`` field holds a string.
        action: the action's value — the ``str`` of a ``CandidateAction`` member. Taken
            as a plain string rather than the enum so this module needs no import from
            ``domain.actions``, keeping it dependency-free within ``domain`` itself.
        attempt_ordinal: 1 for the first attempt on this case, advancing only on a
            further ``APPROVED`` decision — never on a retry of the same attempt, which
            is exactly why a retry recomputes the same key.

    Returns:
        The key, 19 characters.

    Raises:
        ExecutionKeyError: if ``attempt_ordinal`` is below 1, if ``case_id`` renders
            empty, if ``action`` is blank, or if any input contains the separator byte.
            Each would produce a key that could collide with another triple's, and a
            silent collision here is two attempts sharing one provider object.
    """
    if attempt_ordinal < 1:
        raise ExecutionKeyError(
            "attempt_ordinal_out_of_range", f"expected at least 1, got {attempt_ordinal}"
        )
    rendered_case = str(case_id).strip()
    if not rendered_case:
        raise ExecutionKeyError("case_id_empty", "case id rendered to an empty string")
    rendered_action = action.strip()
    if not rendered_action:
        raise ExecutionKeyError("action_empty", "action rendered to an empty string")
    # The separator's whole job is to make the encoding injective. An input containing
    # it would reintroduce the ambiguity it exists to remove, so it is refused rather
    # than escaped — no current caller can produce one, and a future caller that could
    # should fail loudly here rather than mint a colliding key.
    if KEY_SEPARATOR in rendered_case or KEY_SEPARATOR in rendered_action:
        raise ExecutionKeyError(
            "separator_in_input", "an input contains the reserved separator byte"
        )

    material = KEY_SEPARATOR.join((rendered_case, rendered_action, str(attempt_ordinal)))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:KEY_HEX_LENGTH]
    key = f"{REFERENCE_ID_PREFIX}{digest}"
    if len(key) > MAX_REFERENCE_ID_LENGTH:  # pragma: no cover - 19 chars by construction
        raise ExecutionKeyError(
            "reference_id_too_long",
            f"derived key is {len(key)} characters, limit is {MAX_REFERENCE_ID_LENGTH}",
        )
    return key

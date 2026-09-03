"""The three Prompt_Contracts: what may go onto the wire, and nothing else.

A Prompt_Contract is an **allow-list of field names**, not a description of one. The
adapter builds every request payload by iterating :attr:`PromptContract.fields`, so a
field that is not in the set has no path onto the wire — there is no branch to forget
and no ``if`` to delete. That is what makes R27.C2 a property of the construction
rather than of a reviewer's attention.

R27.C3 forbids transmitting a customer contact identifier, a payment instrument
reference, a ``Customer_Access_Token``, an authentication secret or a ``Merchant_User``
identifier. It holds here for the plainest possible reason: **no contract names one**.
:data:`FORBIDDEN_NAME_FRAGMENTS` states that as a checkable rule and
:func:`_reject_forbidden_names` runs it at import time, so a future contract that adds
``customer_contact`` fails when the module loads rather than when a test happens to
generate the right adversarial input. P53 checks the same claim from the outside.

**This module may import only** :mod:`revora.domain` and :mod:`revora.platform` — the
``reasoning-containment`` contract in ``.importlinter`` enforces it. That is why the
adapter cannot read a case row: not by convention, but because the persistence package
is unreachable from here. It also means a bound that lives outside those two packages
cannot be read here. ``DELAY_NOTE_MAX_LENGTH`` is exactly that case: it lives in
``revora.persistence.models.customer``, so this module names *which* fields are
length-bounded (:attr:`PromptContract.truncated_fields`) and the numeric limit is passed
into the adapter as an argument. R20.C11's truncation happens in the adapter, at the
point that has both the value and the bound.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from revora.domain.enums import ReasoningCallKind

__all__ = [
    "CAUSE_HYPOTHESIS_CONTRACT",
    "CONTRACTS",
    "DECISION_EXPLANATION_CONTRACT",
    "FORBIDDEN_NAME_FRAGMENTS",
    "LINK_DESCRIPTION_CONTRACT",
    "PromptContract",
    "UnknownCallKindError",
    "contract_for",
]


class UnknownCallKindError(LookupError):
    """An invocation named a Reasoning_Call_Kind that has no Prompt_Contract.

    R27.C1 permits exactly three call kinds and requires an invocation outside that
    enumeration to be refused *without issuing a request*. Raising here, from the
    lookup the adapter performs before it builds anything, is what makes "without
    issuing a request" structural: there is no payload to send because there was no
    contract to build one from.
    """


FORBIDDEN_NAME_FRAGMENTS: Final[frozenset[str]] = frozenset(
    {
        # Customer contact identifier (R27.C3).
        "contact",
        "email",
        "phone",
        "mobile",
        "whatsapp",
        # Payment instrument reference (R27.C3).
        "instrument",
        "card",
        "vpa",
        "upi_id",
        "payment_method",
        # Customer_Access_Token, and any authentication secret (R27.C3).
        "token",
        "secret",
        "credential",
        "password",
        "api_key",
        "signature",
        # Merchant_User identifier (R27.C3).
        "merchant_user",
        "user_id",
    }
)
"""Substrings that may not appear in any declared field name.

Substrings rather than exact names, because the failure this guards against is a
*variant* — ``customer_contact_masked`` is as much a contact identifier as ``contact``
is, and an exact-match list would let the variant through while looking thorough.

Deliberately restricted to the five categories R27.C3 actually names. ``payment_id`` and
``order_id`` are absent because the requirement does not forbid them; adding them here
would make this list a private opinion about scope wearing a requirement's citation.

Note what the fragments do **not** catch, and must not: ``merchant_display_name``
contains ``merchant`` and ``payment_amount_formatted`` contains ``payment``. Both are
transmitted on purpose, so the fragments are ``merchant_user`` and ``payment_method``
rather than the bare words.
"""


@dataclass(frozen=True, slots=True)
class PromptContract:
    """One call kind's transmitted field set, versioned.

    ``fields`` is a ``frozenset`` rather than a tuple or a list because the allow-list
    question the adapter asks is a set question — *is this name declared?* — and because
    a mutable declaration is a declaration that a caller can widen at runtime.

    ``ordered_field_names`` is derived from ``fields``, never declared alongside it. A
    second declaration could disagree with the first, and the wire payload would then be
    built from whichever one the adapter happened to read. One source, one derivation.
    """

    call_kind: ReasoningCallKind
    contract_id: str
    """The recorded Prompt_Contract version, e.g. ``cause-hypothesis/1``. Persisted with
    every invocation (R27.C2, R27.C12), so a stored request can be read back against the
    field set that was in force when it was sent."""

    fields: frozenset[str]
    truncated_fields: frozenset[str] = frozenset()
    """Declared fields whose value is truncated before transmission, by name only.

    The limits themselves are not here. ``DELAY_NOTE_MAX_LENGTH`` lives in
    ``revora.persistence``, which ``reasoning-containment`` makes unreachable from this
    package, so the adapter receives the number as an argument. Naming the field is still
    worth doing: it records *that* a bound applies, which a caller passing limits in can
    check against, instead of leaving the truncation implicit in the adapter's body.
    """

    @property
    def ordered_field_names(self) -> tuple[str, ...]:
        """The declared names in a fixed order, for building a reproducible payload.

        Sorted rather than authored, because ``frozenset`` iteration order is not stable
        across interpreter runs and a request body whose key order changes between
        processes is a request nobody can diff against a stored one.
        """
        return tuple(sorted(self.fields))

    def declares(self, name: str) -> bool:
        """True where ``name`` is in the allow-list."""
        return name in self.fields

    def undeclared(self, names: Iterable[str]) -> frozenset[str]:
        """The subset of ``names`` this contract does not declare.

        Non-empty means the caller assembled a payload holding a field outside the
        contract, which R27.C2 answers by blocking transmission and continuing on the
        deterministic fallback path. Returned as a set rather than a boolean so the
        Audit_Record can name the offending fields instead of reporting that there were
        some.
        """
        return frozenset(names) - self.fields


# ---------------------------------------------------------------------------
# The three contracts. Field sets are transcribed from design.md's Prompt_Contract
# table and are the authoritative statement of what each call may see.
# ---------------------------------------------------------------------------

CAUSE_HYPOTHESIS_CONTRACT: Final[PromptContract] = PromptContract(
    call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
    contract_id="cause-hypothesis/1",
    # The provider's own error fields plus what the customer said about the delay. All
    # six describe the failure; none of them identifies the person who suffered it.
    fields=frozenset(
        {
            "provider_error_code",
            "provider_error_reason",
            "provider_error_source",
            "provider_error_step",
            "delay_reason",
            "delay_reason_note",
        }
    ),
    # Free text a customer typed, so it is the one field that can carry anything at all.
    # Truncated to DELAY_NOTE_MAX_LENGTH in the adapter (R20.C11).
    truncated_fields=frozenset({"delay_reason_note"}),
)

DECISION_EXPLANATION_CONTRACT: Final[PromptContract] = PromptContract(
    call_kind=ReasoningCallKind.DECISION_EXPLANATION,
    contract_id="decision-explanation/1",
    # The comparison that already happened: the winner, the runner-up, both values and
    # the recorded reason. The model is asked to phrase a decision, not to make one, so
    # it sees the arithmetic's output rather than any of its inputs beyond the baseline.
    fields=frozenset(
        {
            "risk_cause",
            "baseline_probability",
            "selected_action",
            "selected_net_recovery_value",
            "runner_up_action",
            "runner_up_net_recovery_value",
            "selection_reason",
            "currency",
        }
    ),
)

LINK_DESCRIPTION_CONTRACT: Final[PromptContract] = PromptContract(
    call_kind=ReasoningCallKind.LINK_DESCRIPTION,
    contract_id="link-description/1",
    # Four fields, and the amount is pre-formatted. The adapter never transmits the raw
    # minor-unit integer here, because the returned description is checked for amount
    # equality against the case's payment_amount (R27.C9) and the string it must match is
    # the rendered one.
    fields=frozenset(
        {
            "merchant_display_name",
            "payment_amount_formatted",
            "currency",
            "risk_cause",
        }
    ),
)

CONTRACTS: Mapping[ReasoningCallKind, PromptContract] = MappingProxyType(
    {
        ReasoningCallKind.CAUSE_HYPOTHESIS: CAUSE_HYPOTHESIS_CONTRACT,
        ReasoningCallKind.DECISION_EXPLANATION: DECISION_EXPLANATION_CONTRACT,
        ReasoningCallKind.LINK_DESCRIPTION: LINK_DESCRIPTION_CONTRACT,
    }
)
"""Every Prompt_Contract, by call kind. A ``MappingProxyType`` so a caller cannot
register a fourth kind at runtime — R27.C1's enumeration is closed."""


def contract_for(call_kind: ReasoningCallKind) -> PromptContract:
    """The Prompt_Contract for ``call_kind``.

    Raises:
        UnknownCallKindError: where no contract is declared for the kind. Reachable
            despite the enum, because a value can arrive from a database row or a
            deserialized job payload that predates or postdates this enumeration.
    """
    contract = CONTRACTS.get(call_kind)
    if contract is None:
        raise UnknownCallKindError(
            f"no Prompt_Contract declared for reasoning call kind {call_kind!r}; "
            "R27.C1 permits exactly "
            f"{', '.join(sorted(kind.value for kind in CONTRACTS))}"
        )
    return contract


def _reject_forbidden_names() -> None:
    """Fail at import time if any contract declares a name R27.C3 forbids.

    An explicit raise rather than ``assert``, because ``assert`` is removed under ``-O``
    and a privacy guarantee that depends on an interpreter flag is not one.
    """
    for contract in CONTRACTS.values():
        for name in sorted(contract.fields):
            lowered = name.lower()
            offending = sorted(f for f in FORBIDDEN_NAME_FRAGMENTS if f in lowered)
            if offending:
                raise RuntimeError(
                    f"Prompt_Contract {contract.contract_id!r} declares field {name!r}, "
                    f"which matches forbidden name fragment(s) {offending}. R27.C3 "
                    "forbids transmitting a contact identifier, an instrument "
                    "reference, a Customer_Access_Token, an authentication secret or a "
                    "Merchant_User identifier in any reasoning request."
                )
        undeclared_truncations = contract.truncated_fields - contract.fields
        if undeclared_truncations:
            raise RuntimeError(
                f"Prompt_Contract {contract.contract_id!r} marks "
                f"{sorted(undeclared_truncations)} as truncated but does not declare "
                "them, so nothing would ever transmit them"
            )


_reject_forbidden_names()

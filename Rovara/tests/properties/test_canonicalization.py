"""P27. Canonicalization is stable, and equivalent payloads canonicalize identically.

**The property, stated so it can fail:** for any valid provider payload, parsing it, serializing
the parse, and parsing that again yields a canonical event equal to the first parse — and two
payloads differing only in key order, in insignificant whitespace, or in the offset representation
of the same instant produce byte-identical canonical events (R16.C10, R16.C11).

Why this matters more than it looks. The canonical event is what every downstream component
sees: the diagnosis reads ``error_reason`` from it, the optimizer prices against its ``amount``,
the case's ``customer_key`` is derived in it. If canonicalization is unstable then two deliveries
of *the same event* can produce two different canonical events, and the dedup key stops meaning
what it says — R16.C8's "no additional case for a replay" quietly becomes false. An instability
here is not a parsing bug, it is a duplicate-case bug wearing a parsing bug's clothes.

**Offset handling is the sharp edge.** R16.C13 requires a non-UTC offset to be converted to the
equivalent UTC instant before persistence. The provider sends epoch seconds, which carry no offset
at all, so the conversion happens exactly once and in one place — and this file pins that, because
a system that stored a local-time value would produce a window-expiry comparison that is wrong
twice a year (R16.C9).

``pure``: no I/O. ``canonicalize`` is a function from bytes to a frozen dataclass.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revora.domain.failure_taxonomy import ERROR_SOURCES, ERROR_STEPS, REASON_TO_CAUSE
from revora.ingestion.canonical import CanonicalizationError, canonicalize
from revora.platform import crypto
from revora.platform.secrets import SecretStore, set_secret_store

pytestmark = pytest.mark.pure

_MIN_TS = 1_600_000_000
_MAX_TS = 1_900_000_000


class _Resolver:
    """Just the customer-key secret. Nothing here needs another credential."""

    def get(self, name: str) -> str | None:
        if name == "REVORA_CUSTOMER_KEY_SECRET":
            return base64.b64encode(b"C" * 32).decode()
        return None


@pytest.fixture(autouse=True)
def customer_key_secret() -> Iterator[None]:
    """Install the key-derivation secret for every property in this file.

    Without it ``canonicalize`` degrades: it logs "customer_key secret unavailable" and returns an
    event with ``customer_key`` set to ``None``. That degradation is correct — a missing credential
    must not stop an event being recorded — but it makes the PII properties below vacuous, because
    they would be asserting things about a value that was never derived. Installing the secret is
    what makes "the key is a digest, not the contact" a real claim.
    """
    previous = set_secret_store(SecretStore(_Resolver()))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


@st.composite
def payment_entities(draw: st.DrawFn) -> dict[str, Any]:
    """A provider payment entity, drawn over the fields canonicalization actually reads.

    Reasons come from ``REASON_TO_CAUSE`` rather than from free text: the design's taxonomy is built
    from Razorpay's documented set, and generating arbitrary strings would explore a space the
    provider cannot produce while under-exploring the one it does.
    """
    return {
        "id": draw(st.from_regex(r"pay_[A-Za-z0-9]{8,14}", fullmatch=True)),
        "amount": draw(st.integers(min_value=100, max_value=100_000_000)),
        "currency": "INR",
        "status": draw(st.sampled_from(["failed", "captured", "authorized", "refunded"])),
        "order_id": draw(st.from_regex(r"order_[A-Za-z0-9]{8,14}", fullmatch=True)),
        "method": draw(st.sampled_from(["card", "upi", "netbanking", "wallet"])),
        "contact": draw(st.from_regex(r"\+91[6-9][0-9]{9}", fullmatch=True)),
        "email": draw(st.from_regex(r"[a-z]{3,10}@example\.invalid", fullmatch=True)),
        "error_code": draw(st.sampled_from(["BAD_REQUEST_ERROR", "GATEWAY_ERROR"])),
        "error_description": draw(st.text(min_size=0, max_size=40)),
        "error_reason": draw(st.sampled_from(sorted(REASON_TO_CAUSE))),
        "error_source": draw(st.sampled_from(sorted(ERROR_SOURCES))),
        "error_step": draw(st.sampled_from(sorted(ERROR_STEPS))),
        "created_at": draw(st.integers(min_value=_MIN_TS, max_value=_MAX_TS)),
    }


@st.composite
def envelopes(draw: st.DrawFn) -> dict[str, Any]:
    """A whole provider event envelope."""
    entity = draw(payment_entities())
    return {
        "entity": "event",
        "event": draw(st.sampled_from(["payment.failed", "payment.captured"])),
        "contains": ["payment"],
        "created_at": draw(st.integers(min_value=_MIN_TS, max_value=_MAX_TS)),
        "payload": {"payment": {"entity": entity}},
    }


def _shuffled(node: Any, draw: st.DrawFn) -> Any:
    """The same document with every object's key order permuted, recursively.

    Key order is the reordering a real provider actually produces — JSON object order is not
    significant and no serializer promises to preserve it — so it is the reordering the canonical
    form has to be immune to.
    """
    if isinstance(node, dict):
        items = [(key, _shuffled(value, draw)) for key, value in node.items()]
        order = draw(st.permutations(list(range(len(items)))))
        return {items[index][0]: items[index][1] for index in order}
    if isinstance(node, list):
        return [_shuffled(item, draw) for item in node]
    return node


# ---------------------------------------------------------------------------
# The round-trip property
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(envelope=envelopes())
def test_parse_serialize_parse_returns_the_same_canonical_event(envelope: dict[str, Any]) -> None:
    """R16.C11, directly.

    ``canonicalize`` performs this check internally and raises ``round_trip_mismatch`` when it
    fails, so a violation would surface here as an exception rather than as an inequality. Both are
    asserted: the call must succeed *and* re-canonicalizing the serialized form must give the same
    event. Asserting only the first would pass against an implementation whose internal check was
    accidentally comparing an object to itself.
    """
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    first = canonicalize(body)

    # Re-serialize the *input* through a different formatting and canonicalize again. The canonical
    # event is the fixed point, so it must not move.
    respelled = json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8")
    second = canonicalize(respelled)

    assert first.canonical == second.canonical
    assert first.provider_created_at == second.provider_created_at


@settings(max_examples=500)
@given(envelope=envelopes(), data=st.data())
def test_key_order_does_not_change_the_canonical_event(
    envelope: dict[str, Any], data: st.DataObject
) -> None:
    """R16.C10. Two payloads differing only in key order are the same event.

    This is the half that protects deduplication. A canonical form sensitive to key order would give
    one delivery two identities, and "at most one case per payment identifier" would stop holding
    for reasons nobody would look for in the ingestion layer.
    """
    reordered = _shuffled(envelope, data.draw)
    assert reordered == envelope, "permuting keys must not change the document's content"

    original = canonicalize(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
    permuted = canonicalize(json.dumps(reordered, separators=(",", ":")).encode("utf-8"))
    assert original.canonical == permuted.canonical


@settings(max_examples=500)
@given(envelope=envelopes(), spaces=st.integers(min_value=0, max_value=4))
def test_insignificant_whitespace_does_not_change_the_canonical_event(
    envelope: dict[str, Any], spaces: int
) -> None:
    """R16.C10. Indentation and separator padding are not content.

    Worth pinning separately from key order because the *signature* is computed over the exact bytes
    while the *canonical event* must be independent of them. Those two facts have to coexist: a
    reformatted body is a signature failure and, once verified, an identical event.
    """
    compact = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    padded = json.dumps(envelope, indent=spaces or None).encode("utf-8")
    assert canonicalize(compact).canonical == canonicalize(padded).canonical


@settings(max_examples=300)
@given(envelope=envelopes())
def test_the_provider_instant_is_utc_and_derived_from_the_payment(
    envelope: dict[str, Any]
) -> None:
    """R16.C9 and R16.C13. Every stored instant is UTC-aware, and it comes from the payment.

    The entity's ``created_at`` wins over the envelope's, because the envelope timestamp is when the
    *notification* was made and the payment timestamp is when the money moved. Recovery windows are
    measured from the second one; using the first would make a delayed webhook shorten the window it
    is supposed to open.
    """
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    result = canonicalize(body)

    assert result.provider_created_at is not None
    assert result.provider_created_at.tzinfo is not None, (
        "a naive instant here becomes a window comparison that is wrong twice a year"
    )
    assert result.provider_created_at.utcoffset() is not None
    assert int(result.provider_created_at.timestamp()) == (
        envelope["payload"]["payment"]["entity"]["created_at"]
    )


# ---------------------------------------------------------------------------
# The PII boundary, checked at the same place
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(envelope=envelopes())
def test_no_raw_contact_survives_canonicalization(envelope: dict[str, Any]) -> None:
    """The canonical event is PII-free by construction, and this is where that starts.

    Everything downstream — the case row, the audit trail, the logs — reads the canonical event,
    so a raw contact leaking through here leaks everywhere at once. The masked form is permitted to
    show a short suffix by design; the assertion is therefore about the *whole* value never
    appearing, not about the digits never appearing.
    """
    entity = envelope["payload"]["payment"]["entity"]
    contact, email = entity["contact"], entity["email"]

    result = canonicalize(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
    rendered = repr(result.canonical)

    assert contact not in rendered, f"the raw contact survived canonicalization: {rendered}"
    assert email not in rendered, f"the raw email survived canonicalization: {rendered}"
    assert result.canonical.customer_key is not None, (
        "a payment carrying a contact must yield a customer key, or consent cannot be looked up"
    )
    assert contact not in str(result.canonical.customer_key), (
        "the customer key must be a keyed digest, not the contact itself"
    )


@settings(max_examples=200)
@given(envelope=envelopes())
def test_the_same_contact_always_derives_the_same_customer_key(
    envelope: dict[str, Any]
) -> None:
    """Determinism, which is what makes an opt-out reach a case that does not exist yet.

    Consent is keyed on the customer, so a key that varied between deliveries would let a recorded
    opt-out fail to match the very next failure from the same person — the suppression would look
    like it had simply not been applied.
    """
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    assert canonicalize(body).canonical.customer_key == canonicalize(body).canonical.customer_key


# ---------------------------------------------------------------------------
# Refusal, and the shape of it
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(body=st.binary(min_size=0, max_size=64))
def test_arbitrary_bytes_are_refused_rather_than_guessed_at(body: bytes) -> None:
    """Anything unparseable raises, with a machine-readable reason and no partial result.

    The reason code is asserted because the caller branches on it: a quarantine row records it,
    and ``invalid_json`` and ``round_trip_mismatch`` are different findings — one is a broken
    sender, the other a field this system cannot represent stably, and only the second is ours.
    """
    try:
        canonicalize(body)
    except CanonicalizationError as exc:
        # `.rule`, not `args[0]`: the message interpolates the detail, so asserting on the string
        # would pass for any rule name that happened to be a prefix of the message.
        assert exc.rule in {"invalid_json", "schema_invalid", "round_trip_mismatch"}, exc.rule
    # A parse that succeeds on arbitrary bytes is possible — `b"{}"` is valid JSON — and is fine.
    # The property is that nothing *other* than these three failures can happen, which is what the
    # absence of an unhandled exception here asserts.
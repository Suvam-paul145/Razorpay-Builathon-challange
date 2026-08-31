"""Masking applied by the writer, never by the reader.

R17.C6 and R11.C8 require customer contact identifiers and instrument references
to be stored masked. The load-bearing detail is *where* the masking happens: this
module is called by the audit writer and by the log formatter, at the moment of
emission, so there is no code path on which an unmasked value reaches durable
storage and is masked later on the way out. A reader-side mask is a display
convention; a writer-side mask is a property (P32).

Cleartext contact exists in exactly two places in the whole system: inside the
AES-GCM ciphertext of ``webhook_event.raw_payload`` (see ``crypto``), and
transiently in memory during an execution call to the provider. Everything else
goes through here.

``PROVIDER_SHORT_URL`` is in the sensitive set because a payment link is a bearer
capability — whoever holds the URL can pay the invoice. The dashboard shows it,
because the merchant needs it; a log line must not, because logs are copied,
shipped and read by more people than the dashboard is.

Two rules worth stating because they are the ones a careless change would break:

- **Never reveal the whole value.** A value no longer than the disclosure length
  is masked completely. Otherwise a short contact would round-trip in clear,
  which is exactly the accident P32 exists to catch.
- **Mask the tail only, never a recognisable prefix.** The design sketches
  ``+91XXXXXX7890``, but a preserved ``+91`` is still characters of the original,
  and P32 caps what may be revealed at ``MASK_DISCLOSURE_LENGTH`` characters
  total. So everything but the trailing window is replaced.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Final

from revora.domain.enums import SENSITIVE_FIELD_KINDS, FieldKind

__all__ = [
    "DEFAULT_FIELD_KINDS",
    "FIELD_KIND_METADATA_KEY",
    "MASK_CHARACTER",
    "MASK_DISCLOSURE_LENGTH",
    "field_kind_for",
    "mask_record",
    "mask_value",
    "sensitive",
]

MASK_DISCLOSURE_LENGTH: Final[int] = 4
"""How many trailing characters a masked value may reveal.

**Placeholder.** The design records the permitted disclosure length as
[EVIDENCE INSUFFICIENT] — no documentary basis was found for 4. It lives here as a
module default until the configuration loader (task 5.6) can supply it from
``app_config`` with a recorded configuration version, at which point callers pass
it in and this constant is the fallback only.
"""

MASK_CHARACTER: Final[str] = "X"
"""Fixed masking character. Length is preserved, so a masked value still shows how
long the original was — accepted: the length of a phone number is not a secret,
and preserving it keeps masked values legible in a log."""

FIELD_KIND_METADATA_KEY: Final[str] = "revora_field_kind"
"""Dataclass field metadata key carrying the declared ``FieldKind``."""

_MASKED_SUFFIX: Final[str] = "_masked"

DEFAULT_FIELD_KINDS: Final[Mapping[str, FieldKind]] = {
    # Contact
    "contact": FieldKind.CONTACT,
    "contact_number": FieldKind.CONTACT,
    "customer_contact": FieldKind.CONTACT,
    "customer_email": FieldKind.CONTACT,
    "customer_phone": FieldKind.CONTACT,
    "email": FieldKind.CONTACT,
    "mobile": FieldKind.CONTACT,
    "notify_contact": FieldKind.CONTACT,
    "phone": FieldKind.CONTACT,
    # Instrument
    "account_number": FieldKind.INSTRUMENT,
    "bank_account": FieldKind.INSTRUMENT,
    "card": FieldKind.INSTRUMENT,
    "card_id": FieldKind.INSTRUMENT,
    "card_number": FieldKind.INSTRUMENT,
    "instrument": FieldKind.INSTRUMENT,
    "last4": FieldKind.INSTRUMENT,
    "payment_instrument": FieldKind.INSTRUMENT,
    "upi_id": FieldKind.INSTRUMENT,
    "vpa": FieldKind.INSTRUMENT,
    # Bearer capability
    "link_url": FieldKind.PROVIDER_SHORT_URL,
    "payment_link_url": FieldKind.PROVIDER_SHORT_URL,
    "provider_short_url": FieldKind.PROVIDER_SHORT_URL,
    "short_url": FieldKind.PROVIDER_SHORT_URL,
}
"""Field names that are sensitive wherever they appear.

A name-based registry rather than a per-call declaration, because the values that
leak are the ones nobody remembered to declare. A caller that knows better passes
``field_kinds`` to override or extend this.
"""

#: Kinds that reveal nothing at all. A payment link's trailing characters are part
#: of the capability, and no operator needs them to debug, so the disclosure window
#: is zero rather than four.
_ZERO_DISCLOSURE_KINDS: Final[frozenset[FieldKind]] = frozenset({FieldKind.PROVIDER_SHORT_URL})


def sensitive(kind: FieldKind) -> dict[str, FieldKind]:
    """Dataclass field metadata declaring a ``FieldKind``.

    Usage: ``contact: str = dataclasses.field(metadata=sensitive(FieldKind.CONTACT))``.
    Declaring it on the dataclass keeps the declaration next to the field instead
    of in a table that drifts away from it.
    """
    return {FIELD_KIND_METADATA_KEY: kind}


def field_kind_for(name: str, *, field_kinds: Mapping[str, FieldKind] | None = None) -> FieldKind:
    """The declared kind for a field name, defaulting to ``NON_SENSITIVE``.

    A name ending in ``_masked`` is treated as non-sensitive: the column already
    holds a masked value, and masking it again would eat the four characters the
    merchant is meant to see.
    """
    lowered = name.lower()
    if lowered.endswith(_MASKED_SUFFIX):
        return FieldKind.NON_SENSITIVE
    if field_kinds is not None:
        declared = field_kinds.get(lowered)
        if declared is not None:
            return declared
    registered = DEFAULT_FIELD_KINDS.get(lowered)
    if registered is not None:
        return registered
    # A plural of a registered name is the same kind: ``contacts`` holds contacts.
    # Cheap, and it closes the case where a list field escaped the registry because
    # whoever added it only thought of the singular.
    if lowered.endswith("s"):
        return DEFAULT_FIELD_KINDS.get(lowered[:-1], FieldKind.NON_SENSITIVE)
    return FieldKind.NON_SENSITIVE


def mask_value(
    value: object,
    kind: FieldKind,
    *,
    disclosure_length: int = MASK_DISCLOSURE_LENGTH,
) -> object:
    """Mask ``value`` according to ``kind``.

    ``None`` stays ``None`` and an empty string stays empty — neither reveals
    anything, and substituting a placeholder would make "absent" and "masked"
    indistinguishable in an audit record. A non-string sensitive value is rendered
    to text first, because an instrument reference that arrived as an int is still
    an instrument reference.

    Raises:
        ValueError: if ``disclosure_length`` is negative.
    """
    if disclosure_length < 0:
        raise ValueError("disclosure_length must not be negative")
    if kind not in SENSITIVE_FIELD_KINDS:
        return value
    if value is None:
        return None

    if isinstance(value, bytes | bytearray):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    if not text:
        return text

    window = 0 if kind in _ZERO_DISCLOSURE_KINDS else disclosure_length
    # A value no longer than the window would otherwise survive in full.
    if len(text) <= window:
        window = 0
    hidden = len(text) - window
    if window == 0:
        return MASK_CHARACTER * hidden
    return MASK_CHARACTER * hidden + text[hidden:]


def mask_record(
    record: object,
    *,
    field_kinds: Mapping[str, FieldKind] | None = None,
    disclosure_length: int = MASK_DISCLOSURE_LENGTH,
) -> Any:
    """Walk a record and mask every field whose declared kind is sensitive.

    Handles mappings, dataclass instances, and sequences of either, recursively —
    nested structures are where an unmasked value hides, because the provider's
    payload nests ``payment.entity.contact`` two levels down and a top-level-only
    walk would ship it.

    Dataclasses come back as dicts: the output of this function is bound for
    ``json.dumps`` or a ``JSONB`` column, and rebuilding a frozen dataclass with a
    masked value in a typed field would either fail validation or lie about the
    field's contents.
    """
    return _mask_node(record, FieldKind.NON_SENSITIVE, field_kinds, disclosure_length)


def _mask_node(
    node: object,
    kind: FieldKind,
    field_kinds: Mapping[str, FieldKind] | None,
    disclosure_length: int,
) -> Any:
    if isinstance(node, Mapping):
        return {
            key: _mask_node(
                item,
                field_kind_for(str(key), field_kinds=field_kinds),
                field_kinds,
                disclosure_length,
            )
            for key, item in node.items()
        }

    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        masked: dict[str, Any] = {}
        for spec in dataclasses.fields(node):
            declared = spec.metadata.get(FIELD_KIND_METADATA_KEY)
            resolved = (
                declared
                if isinstance(declared, FieldKind)
                else field_kind_for(spec.name, field_kinds=field_kinds)
            )
            masked[spec.name] = _mask_node(
                getattr(node, spec.name), resolved, field_kinds, disclosure_length
            )
        return masked

    if isinstance(node, str | bytes | bytearray):
        return mask_value(node, kind, disclosure_length=disclosure_length)

    if isinstance(node, Sequence):
        # A sensitive kind propagates into the elements: a list of contacts is a
        # list of contacts, not an opaque container.
        return [_mask_node(item, kind, field_kinds, disclosure_length) for item in node]

    if isinstance(node, set | frozenset):
        ordered = sorted(node, key=repr)
        return [_mask_node(item, kind, field_kinds, disclosure_length) for item in ordered]

    return mask_value(node, kind, disclosure_length=disclosure_length)

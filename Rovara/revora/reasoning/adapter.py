"""The reasoning transport: one POST, four gates, and no authority whatsoever.

``POST {base}/v1beta/models/{model}:generateContent`` with the credential from the
existing secret store. Nothing here decides anything: every method returns an advisory
result the caller may use or discard, and no path in this module writes a row, opens a
session or reaches a Recovery_Case. It cannot — ``reasoning-containment`` in
``.importlinter`` leaves this package able to see only :mod:`revora.platform` and
:mod:`revora.domain`, so "the adapter cannot read a case row" is a property of what is
reachable rather than of what the code happens to do today. Callers pass data in.

#### Hand-written ``httpx``, and *not* for the payment client's reason

The payment client is hand-written because an SDK erases the difference between "the
external effect definitely did not happen" and "it might have happened", and exactly-once
execution is built on that difference. **That argument does not apply here at all.** A
reasoning call produces no external effect, so an ambiguous outcome costs exactly one
deterministic fallback and nothing else. Claiming the same justification twice would be
dressing a dependency preference up as a correctness requirement.

The real reasons are smaller, and worth less:

1. ``httpx`` is already a dependency, with a call-budget split and a masking-aware logger
   already built around it. A vendored SDK adds a transitive tree to a code path that
   makes one POST with a JSON body.
2. ``.importlinter`` cannot analyse inside a vendored SDK, and the whole structural claim
   of R27.C11 is about what the contract checker can see. Surface it cannot analyse is
   surface the claim does not cover.
3. The response must be validated independently of provider enforcement anyway (R27.C15),
   so the SDK's parsing is work we are not permitted to trust.

An SDK would be a legitimate choice here, and if retry, backoff and streaming were needed
it would be the better one. They are not needed: one call, one budget, one JSON body.

#### The four gates, in the order they run

1. **Field allow-list (R27.C2).** :func:`build_request_payload` iterates the contract's
   frozen field-name set. A name outside it has no path onto the wire — and a caller that
   supplies one gets :class:`Unavailable` with reason ``PROMPT_CONTRACT_VIOLATION``,
   *before* a credential is resolved, so the request is never issued at all.
2. **TLS with certificate validation (R27.C14).** ``verify=True`` is passed explicitly so
   a reader can see it was a decision. A handshake or certificate failure returns
   :class:`Unavailable` with reason ``TRANSPORT_SECURITY_FAILED`` and is **never
   retried**. The "before any case field is transmitted" half is the transport's: the
   handshake completes before the request body is written, so a failed handshake means the
   body never left the process.
3. **Output schema validation (R27.C5, R27.C15).** The request sets
   ``generationConfig.responseMimeType`` and ``generationConfig.responseSchema``, *and*
   the body is validated against the Pydantic model in :mod:`revora.reasoning.schemas`
   regardless. Provider-side constraint is an optimization; a component that treats it as
   a guarantee has no fallback the day it changes. Failure yields
   :class:`RejectedSchema`, whose caller records ``RiskCause.UNKNOWN`` with confidence
   ``0.0`` and the method ``REJECTED_AI_OUTPUT``, with the raw body retained to
   ``AI_RAW_CAPTURE_LIMIT``.
4. **Content validation (R27.C9, R27.C10).** ``LINK_DESCRIPTION`` only. Length and control
   characters go through the existing ``providers.payment_link.validate_description``,
   which this package cannot import — so it arrives as a **required** argument rather than
   an optional one, which is what makes "sent without passing that validator" unreachable
   instead of merely discouraged. The placeholder, amount-equality and single-link rules
   are :func:`validate_link_description_content`, which is pure and takes the case's
   values as arguments.

#### Timeout, retry, and the absent credential

``REASONING_TIMEOUT`` bounds one attempt and ``REASONING_RETRY_COUNT`` permits additional
ones, under a hard total budget of :data:`TOTAL_WAIT_MULTIPLE` times the timeout (R27.C6).
The
budget is enforced against a monotonic reading rather than derived from the attempt count,
so a misconfigured retry count cannot widen it.

**Retry policy is the exact opposite of the payment client's, and for a reason that is
worth stating rather than inferring.** There, a read timeout is never retried because the
provider may already have created a payment link. Here there is no effect to duplicate, so
a timeout *is* retried. What is not retried: a certificate failure (it will not fix itself
inside one processing step) and a rejected response (it is an answer, and re-asking a model
that just answered badly spends budget to receive the same rejection).

An absent or unreadable credential returns :class:`Unavailable` with **no request issued
and no wait at all** (R27.C7). Nothing in this module blocks, sleeps or polls, which is the
implementation half of "no state in the machine means waiting for the model": there is no
state to introduce, because every method returns before it could need one.

``DELAY_NOTE_MAX_LENGTH`` truncation happens here (R20.C11), not at the call site — this is
the component that holds both the value and the bound, and a truncation performed by each
caller is a truncation one caller will forget.
"""

from __future__ import annotations

import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum, unique
from types import MappingProxyType, TracebackType
from typing import Final, Protocol

import httpx

from revora.domain.actions import CandidateAction
from revora.domain.enums import ReasoningCallKind, RiskCause
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.platform.config import Configuration, default_configuration
from revora.platform.logging import get_logger
from revora.platform.secrets import (
    CREDENTIAL_UNAVAILABLE,
    CredentialUnavailableError,
    get_secret_store,
)
from revora.reasoning.contracts import PromptContract, UnknownCallKindError, contract_for
from revora.reasoning.schemas import (
    OUTPUT_MODELS,
    CauseHypothesisOutput,
    DecisionExplanationOutput,
    LinkDescriptionOutput,
    ReasoningOutputError,
    parse_cause_hypothesis,
    parse_decision_explanation,
    parse_link_description,
)

__all__ = [
    "API_KEY_HEADER",
    "CONTENT_REJECTED",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "GENERATE_CONTENT_PATH",
    "MODEL_FLASH",
    "MODEL_PRO",
    "PERMITTED_PLACEHOLDERS",
    "PROMPT_CONTRACT_VIOLATION",
    "RESPONSE_SCHEMAS",
    "TOTAL_WAIT_MULTIPLE",
    "TRANSPORT_SECURITY_FAILED",
    "TRUNCATION_MARKER",
    "UNKNOWN_CONTRACT_ID",
    "VERIFIED_MODELS",
    "Accepted",
    "ContentRule",
    "DescriptionValidator",
    "PromptContractViolationError",
    "ReasoningAdapter",
    "ReasoningResult",
    "ReasoningVerdict",
    "RejectedContent",
    "RejectedSchema",
    "TimedOut",
    "Unavailable",
    "UnavailableReason",
    "audit_event_type_for",
    "build_request_payload",
    "credential_available",
    "extract_transmitted_payload",
    "response_schema_for",
    "validate_link_description_content",
    "verdict_of",
]

_logger = get_logger(__name__)

DEFAULT_BASE_URL: Final[str] = "https://generativelanguage.googleapis.com/v1beta"
"""**[VERIFIED]** — reachable, and both models below are listed under it.

The API version is part of the base URL rather than of the path template because it is the
half a correction would change: ``v1beta`` becoming ``v1`` is a configuration edit, and the
``/models/{model}:generateContent`` shape is stable across both."""

GENERATE_CONTENT_PATH: Final[str] = "/models/{model}:generateContent"

MODEL_FLASH: Final[str] = "gemini-2.5-flash"
MODEL_PRO: Final[str] = "gemini-2.5-pro"
VERIFIED_MODELS: Final[frozenset[str]] = frozenset({MODEL_FLASH, MODEL_PRO})
"""The two models verified as listed. An unlisted model is refused at construction.

Refused *at construction* rather than per call, deliberately: a model name that does not
exist is a deployment mistake, and discovering it on the first advisory call would spend a
processing step's whole reasoning budget on a 404 before falling back. Failing at startup
costs one restart and names the problem."""

DEFAULT_MODEL: Final[str] = MODEL_FLASH
"""Flash rather than Pro. All three calls are short, structured and advisory — a cause
label with a confidence, one sentence of prose, one sentence of customer-visible text — and
none of them is improved by a model that reasons longer at several times the latency, when
the alternative to any answer at all is a deterministic fallback that already works."""

API_KEY_HEADER: Final[str] = "x-goog-api-key"
"""The credential goes in a header, never in the query string. A query parameter would be
recorded by every proxy and access log between here and the provider, and the credential is
the whole account."""

TRANSPORT_SECURITY_FAILED: Final[str] = "TRANSPORT_SECURITY_FAILED"
"""Audit event type for a TLS or certificate failure (R27.C14, R17.C5, R17.C14)."""

PROMPT_CONTRACT_VIOLATION: Final[str] = "PROMPT_CONTRACT_VIOLATION"
"""Audit event type for a payload holding a field the contract does not declare (R27.C2).

Named here rather than imported from ``revora.audit.events``, which
``reasoning-containment`` makes unreachable. Two constants in two packages is the cost of
the containment that carries R27.C11; the string is the contract between them, and the
event-type check in the audit tests is what keeps them spelled the same."""

CONTENT_REJECTED: Final[str] = "CONTENT_REJECTED"
"""Audit event type for a ``LINK_DESCRIPTION`` draft that failed a content rule (R27.C10)."""

UNKNOWN_CONTRACT_ID: Final[str] = "unknown/0"
"""Recorded where the call kind itself had no contract, so no version was in force.

``ai_invocation.prompt_contract_id`` is ``NOT NULL`` and the row must still be written
(R27.C12), so the absence needs a spelling. A sentinel rather than an empty string, because
an empty string in that column reads as a bug in the writer."""

TRUNCATION_MARKER: Final[str] = "\u2026[truncated]"

TOTAL_WAIT_MULTIPLE: Final[int] = 2
"""R27.C6's bound: total reasoning wait per processing step stays within two multiples of
``REASONING_TIMEOUT``. Enforced against a monotonic reading rather than inferred from the
attempt count, so a ``REASONING_RETRY_COUNT`` of nine widens nothing."""

MAX_CONNECT_SHARE: Final[timedelta] = timedelta(seconds=5)
_CONNECT_SHARE_DENOMINATOR: Final[int] = 5

_MAX_EXCEPTION_CHAIN_DEPTH: Final[int] = 12

_CERTIFICATE_MARKERS: Final[tuple[str, ...]] = (
    "certificate",
    "ssl",
    "tls",
    "hostname mismatch",
    "self signed",
)
"""Substrings that identify a certificate failure when the exception chain does not.

The chain walk in :func:`_is_certificate_failure` is the real check; this is the fallback
for a transport that reports the failure as a plain message rather than as a wrapped
``ssl.SSLError``. False positives are affordable in a way false negatives are not: a
mislabelled transport failure costs one deterministic fallback either way, while a
certificate failure recorded as an ordinary timeout loses the one record R27.C14 asks for."""


@unique
class ReasoningVerdict(StrEnum):
    """Which gate the invocation reached, in R27.C12's exact five words.

    The vocabulary belongs to this layer — ``ai_invocation.verdict`` is deliberately free
    text in the persistence model, with a note saying the reasoning layer would pin it.
    This is that pinning, and it is closed: a sixth verdict is a change to what R27.C12
    requires be recorded, not an extension of an enumeration.

    Note what is *not* here. A certificate failure and a refused payload are both
    ``UNAVAILABLE`` rather than verdicts of their own, because R27.C12 names five and the
    distinction between them lives in :class:`UnavailableReason` and in the audit event
    type. One column answering "did this invocation contribute" and another answering "why
    not" is cheaper to query than one column trying to answer both.
    """

    ACCEPTED = "ACCEPTED"
    REJECTED_SCHEMA = "REJECTED_SCHEMA"
    REJECTED_CONTENT = "REJECTED_CONTENT"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class UnavailableReason(StrEnum):
    """Why no usable response exists, at the granularity a person debugging needs.

    Every member means the same thing to the caller — take the deterministic path — which
    is why they share one result variant instead of being five. They differ only in what
    gets recorded, and :func:`audit_event_type_for` is the mapping.
    """

    CREDENTIAL_ABSENT = "CREDENTIAL_ABSENT"
    """R27.C7. No request issued, and nothing waited."""

    PROMPT_CONTRACT_VIOLATION = "PROMPT_CONTRACT_VIOLATION"
    """R27.C2. A payload field outside the declared set; transmission blocked."""

    TRANSPORT_SECURITY_FAILED = "TRANSPORT_SECURITY_FAILED"
    """R27.C14. TLS establishment or certificate validation failed."""

    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    """A connect error, a protocol error, or a failure mode this module did not
    anticipate. Distinct from the security failure because the response differs: this one
    is worth retrying inside the budget, and that one is not."""

    PROVIDER_REFUSED = "PROVIDER_REFUSED"
    """A non-2xx answer. The provider was reached and declined to answer."""

    UNKNOWN_CALL_KIND = "UNKNOWN_CALL_KIND"
    """R27.C1. A kind with no declared contract, refused without a request."""


@unique
class ContentRule(StrEnum):
    """The content rule a ``LINK_DESCRIPTION`` draft violated (R27.C9, R27.C10).

    R27.C10 requires the ``CONTENT_REJECTED`` record to *name the violated rule*, so this
    is an enumeration rather than a message. One member is returned, not a list: the first
    violation is the reason, and a caller that wanted all of them would be building a
    report for a draft nobody is going to send.
    """

    DESCRIPTION_REFUSED = "DESCRIPTION_REFUSED"
    """The injected ``validate_description`` refused it: blank, over ``MAX_MESSAGE_LENGTH``,
    or carrying control characters."""

    PLACEHOLDER_NOT_PERMITTED = "PLACEHOLDER_NOT_PERMITTED"
    PLACEHOLDER_UNRESOLVED = "PLACEHOLDER_UNRESOLVED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    AMOUNT_UNVERIFIABLE = "AMOUNT_UNVERIFIABLE"
    """The case's own formatted amount held no figure to compare against, so the
    amount-equality rule could not be evaluated. Refused rather than skipped: a rule that
    silently passes when its reference value is missing is not a rule."""

    UNAPPROVED_LINK = "UNAPPROVED_LINK"
    MULTIPLE_LINKS = "MULTIPLE_LINKS"


class PromptContractViolationError(ValueError):
    """A payload named a field the Prompt_Contract does not declare (R27.C2).

    Raised by :func:`build_request_payload`, which runs before a credential is resolved, so
    the guarantee is not "we removed the field" but "no request was ever built". ``fields``
    names the offenders so the Audit_Record can list them rather than reporting that there
    were some.
    """

    def __init__(self, contract_id: str, fields: frozenset[str]) -> None:
        self.contract_id = contract_id
        self.fields = fields
        super().__init__(
            f"Prompt_Contract {contract_id!r} does not declare {sorted(fields)}; "
            "transmission blocked (R27.C2)"
        )


class DescriptionValidator(Protocol):
    """``providers.payment_link.validate_description`` with its bound already bound.

    A Protocol rather than an import, because ``reasoning-containment`` forbids this package
    from reaching :mod:`revora.providers` — which is the point, not an obstacle. The call
    site binds ``max_length`` and passes the result in, so there is one implementation of
    the length and control-character rules and this module holds none of it.

    Returns the validated text; raises anything at all on refusal. The adapter reads a
    ``rule`` attribute off the exception where one is present, which is how a
    ``PaymentLinkRequestError``'s own rule name reaches the audit record without this
    module knowing the type.
    """

    def __call__(self, description: str, /) -> str: ...


# ---------------------------------------------------------------------------
# The five results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Accepted[OutputT]:
    """A response that passed every gate that applies to its call kind.

    Advisory, still. ``Accepted`` means "this survived validation", never "act on this" —
    the confidence cap of R27.C4, the explanation-only storage of R27.C8 and the template
    substitution of R27.C10 all belong to callers, because they are the components that
    know what the value would be used for.
    """

    output: OutputT
    call_kind: ReasoningCallKind
    contract_id: str
    model_id: str
    model_version: str | None
    latency_ms: int
    http_status: int


@dataclass(frozen=True, slots=True)
class RejectedSchema:
    """A response arrived and failed the declared output schema (R27.C5).

    ``raw_response`` is retained, truncated to ``AI_RAW_CAPTURE_LIMIT``, because "the model
    returned something we refused" is only diagnosable if the something is visible. It is
    provider-controlled text of unknown shape, so it travels on this result to a
    length-bounded, masked audit record and is never a log field.
    """

    call_kind: ReasoningCallKind
    contract_id: str
    reason: str
    raw_response: str
    model_id: str
    model_version: str | None
    latency_ms: int
    http_status: int


@dataclass(frozen=True, slots=True)
class RejectedContent:
    """A schema-valid ``LINK_DESCRIPTION`` that failed a content rule (R27.C9, R27.C10).

    The draft is retained and the rule is named. The caller substitutes the deterministic
    template, leaves the customer-message counter unchanged by the suppression, and writes
    ``CONTENT_REJECTED`` — substitution rather than non-execution, because a payment link
    carrying a template description is a complete action.
    """

    call_kind: ReasoningCallKind
    contract_id: str
    rule: ContentRule
    detail: str
    draft: str
    model_id: str
    model_version: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class TimedOut:
    """No response inside the step's budget (R27.C6).

    ``attempts`` and ``waited_ms`` are both recorded because they answer different
    questions: whether the retry allowance was used, and whether the two-multiple bound
    held. A caller recording a ``CAUSE_HYPOTHESIS`` step that ended here writes the method
    ``FALLBACK_UNKNOWN`` — one invocation attempted, no answer, which is a different fact
    from ``REJECTED_AI_OUTPUT``.
    """

    call_kind: ReasoningCallKind
    contract_id: str
    detail: str
    attempts: int
    waited_ms: int


@dataclass(frozen=True, slots=True)
class Unavailable:
    """No usable response, and the reason says whether anything was sent.

    ``request_issued`` is a field rather than something derived from the reason, because it
    is the fact R27.C7 and R27.C2 actually assert — no request for an absent credential, no
    transmission for a refused payload — and a reader checking that claim should not have
    to consult a mapping table to do it.
    """

    call_kind: ReasoningCallKind | None
    contract_id: str
    reason: UnavailableReason
    detail: str
    request_issued: bool
    attempts: int = 0
    waited_ms: int = 0
    offending_fields: frozenset[str] = frozenset()
    raw_response: str | None = None
    http_status: int | None = None


type ReasoningResult[OutputT] = (
    Accepted[OutputT] | RejectedSchema | RejectedContent | TimedOut | Unavailable
)
"""Exactly one of the five. No public method in this module raises."""


def verdict_of(result: ReasoningResult[object]) -> ReasoningVerdict:
    """R27.C12's verdict for ``result``.

    One function rather than a ``match`` in the invocation-recording code, so that the
    mapping from result to recorded verdict exists once. Task 49.4 writes the row; this is
    the column's value.
    """
    match result:
        case Accepted():
            return ReasoningVerdict.ACCEPTED
        case RejectedSchema():
            return ReasoningVerdict.REJECTED_SCHEMA
        case RejectedContent():
            return ReasoningVerdict.REJECTED_CONTENT
        case TimedOut():
            return ReasoningVerdict.TIMEOUT
        case _:
            return ReasoningVerdict.UNAVAILABLE


_AUDIT_EVENT_TYPES: Final[Mapping[UnavailableReason, str]] = MappingProxyType(
    {
        UnavailableReason.CREDENTIAL_ABSENT: CREDENTIAL_UNAVAILABLE,
        UnavailableReason.PROMPT_CONTRACT_VIOLATION: PROMPT_CONTRACT_VIOLATION,
        UnavailableReason.TRANSPORT_SECURITY_FAILED: TRANSPORT_SECURITY_FAILED,
    }
)


def credential_available() -> bool:
    """Whether a reasoning credential is configured. Issues nothing and waits for nothing.

    **This function exists so that ``llm_credential()`` is resolved in exactly one file.** A
    caller that wants to know whether the reasoning layer is usable at all — the job pipeline,
    deciding whether to construct an adapter for a processing step — would otherwise reach the
    secret store itself, and ``tests/test_smoke.py`` pins the set of callers of that accessor to
    this module precisely because a component resolving the credential for itself would be one
    step from reaching a model outside the four gates. Asking here keeps the set at one entry
    and puts the presence question where the credential knowledge already lives.

    It answers a *different* question from :class:`Unavailable` with
    ``CREDENTIAL_ABSENT``, and both are needed. That result means "this invocation could not
    happen"; this means "no invocation should be attempted in this step at all", which is what
    lets the caller skip building a client, a payload and a row rather than building all three
    and discarding them. R27.C7's "nothing waits" is satisfied either way; this is the cheaper
    of the two, and it is the branch the deployed reality takes.

    The value is resolved and discarded. Nothing is cached and nothing is returned, so a
    rotation or a newly added credential takes effect on the next call.
    """
    try:
        get_secret_store().llm_credential()
    except CredentialUnavailableError:
        return False
    return True


def audit_event_type_for(result: ReasoningResult[object]) -> str | None:
    """The additional Audit_Record type ``result`` requires, if it requires one.

    ``CREDENTIAL_UNAVAILABLE`` (R17.C4), ``PROMPT_CONTRACT_VIOLATION`` (R27.C2),
    ``TRANSPORT_SECURITY_FAILED`` (R27.C14) or ``CONTENT_REJECTED`` (R27.C10). ``None``
    everywhere else — the ``ai_invocation`` row of R27.C12 is written for every invocation
    regardless, and these are the four cases that additionally name an event type.

    Exposed here so no caller re-derives the mapping and gets one of the four subtly wrong.
    """
    match result:
        case Unavailable(reason=reason):
            return _AUDIT_EVENT_TYPES.get(reason)
        case RejectedContent():
            return CONTENT_REJECTED
        case _:
            return None


# ---------------------------------------------------------------------------
# Gate 1: the payload is built from the allow-list, or it is not built
# ---------------------------------------------------------------------------


def build_request_payload(
    call_kind: ReasoningCallKind,
    values: Mapping[str, object],
    *,
    delay_note_limit: int,
) -> Mapping[str, object]:
    """The transmitted data object, built by iterating the contract's frozen field set.

    The loop is the enforcement. Every key in the returned mapping came from
    :attr:`PromptContract.ordered_field_names`, so the transmitted key set is a subset of
    the declared set by construction and not by inspection — which is the claim P53 checks
    from the outside with adversarial case rows.

    A field present in ``values`` but absent from the contract raises rather than being
    dropped. Dropping would be worse than it looks: the caller would believe it had
    transmitted something it had not, and R27.C2 asks for blocked transmission and a record
    naming the offending field, not for silent filtering.

    A declared field whose value is ``None`` is omitted. Absence is a real state for four of
    the six ``CAUSE_HYPOTHESIS`` fields — a delay note nobody typed, a provider error step
    the provider did not report — and transmitting ``null`` would ask the model to
    distinguish "unknown" from "the string 'None'".

    Args:
        call_kind: which of the three sanctioned kinds.
        values: candidate field values, keyed by declared name.
        delay_note_limit: ``Configuration.DELAY_NOTE_MAX_LENGTH``. Applied to every field
            the contract marks truncated (R20.C11). Required rather than defaulted, so a
            caller cannot get an untruncated note by omitting an argument.

    Raises:
        UnknownCallKindError: no contract for ``call_kind``.
        PromptContractViolationError: ``values`` holds an undeclared name.
        ValueError: ``delay_note_limit`` is not positive.
    """
    if delay_note_limit < 1:
        raise ValueError(f"delay_note_limit must be at least 1, got {delay_note_limit}")
    contract = contract_for(call_kind)
    undeclared = contract.undeclared(values.keys())
    if undeclared:
        raise PromptContractViolationError(contract.contract_id, undeclared)

    payload: dict[str, object] = {}
    for name in contract.ordered_field_names:
        value = values.get(name)
        if value is None:
            continue
        wire = _wire_value(value)
        if name in contract.truncated_fields and isinstance(wire, str):
            wire = wire[:delay_note_limit]
        payload[name] = wire
    return MappingProxyType(payload)


def _wire_value(value: object) -> object:
    """One declared field's value, as JSON can carry it and money survives.

    Three coercions and a fallback:

    * a ``StrEnum`` member becomes its ``value``, so ``RiskCause.ABANDONMENT`` transmits as
      ``"ABANDONMENT"`` rather than as whatever ``str()`` produces this release;
    * a ``Decimal`` becomes its exact string. **Never a numeric conversion** — a baseline
      probability that went through binary form on the way out is a different number from
      the one the estimator computed, and the whole point of holding probabilities as
      ``Decimal`` is that the two agree;
    * an ``int`` — which is what ``Minor`` is — stays an integer, because minor units are
      already the unit the wire wants;
    * anything else is stringified rather than refused. A refusal would have to become an
      exception on a path whose whole contract is that it does not raise at the caller, and
      a string is always transmittable. R27.C3 is not weakened by this: it holds because no
      contract *declares* a forbidden name, and this function never sees a name at all.
    """
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Gate 3, provider half: the schema sent alongside the request
# ---------------------------------------------------------------------------

_CAUSE_HYPOTHESIS_SCHEMA: Final[Mapping[str, object]] = MappingProxyType(
    {
        "type": "OBJECT",
        "properties": {
            "cause": {"type": "STRING", "enum": [cause.value for cause in RiskCause]},
            "confidence": {"type": "NUMBER"},
            "evidence_summary": {"type": "STRING"},
        },
        "required": ["cause", "confidence", "evidence_summary"],
        "propertyOrdering": ["cause", "confidence", "evidence_summary"],
    }
)

_DECISION_EXPLANATION_SCHEMA: Final[Mapping[str, object]] = MappingProxyType(
    {
        "type": "OBJECT",
        "properties": {"explanation": {"type": "STRING"}},
        "required": ["explanation"],
        "propertyOrdering": ["explanation"],
    }
)

_LINK_DESCRIPTION_SCHEMA: Final[Mapping[str, object]] = MappingProxyType(
    {
        "type": "OBJECT",
        "properties": {"description": {"type": "STRING"}},
        "required": ["description"],
        "propertyOrdering": ["description"],
    }
)

RESPONSE_SCHEMAS: Final[Mapping[ReasoningCallKind, Mapping[str, object]]] = MappingProxyType(
    {
        ReasoningCallKind.CAUSE_HYPOTHESIS: _CAUSE_HYPOTHESIS_SCHEMA,
        ReasoningCallKind.DECISION_EXPLANATION: _DECISION_EXPLANATION_SCHEMA,
        ReasoningCallKind.LINK_DESCRIPTION: _LINK_DESCRIPTION_SCHEMA,
    }
)
"""``generationConfig.responseSchema`` per call kind, in the provider's own dialect.

Written out rather than generated from the Pydantic models, and the reason is specific: the
generated JSON Schema carries ``$defs`` and ``$ref`` for the ``RiskCause`` enumeration and
lowercase type names, neither of which this endpoint accepts. A conversion layer between
them would be a third thing to keep correct.

The cost of writing them out is drift, and :func:`_reject_schema_drift` is the answer to it
— it asserts at import that each schema's property names and required set match the output
model's fields exactly. No length bounds appear here on purpose: two of the three are
configured values this package cannot read, so a ``maxLength`` here would be a number
invented to look thorough while disagreeing with the bound actually enforced."""


def response_schema_for(call_kind: ReasoningCallKind) -> Mapping[str, object]:
    """The provider-side schema for ``call_kind``.

    Raises:
        UnknownCallKindError: where the kind has no declared schema, so an unsanctioned
            kind cannot reach the wire by way of a missing schema either.
    """
    schema = RESPONSE_SCHEMAS.get(call_kind)
    if schema is None:
        raise UnknownCallKindError(f"no response schema declared for {call_kind!r}")
    return schema


def _reject_schema_drift() -> None:
    """Fail at import if a provider schema and its output model disagree on fields.

    An explicit raise rather than ``assert``, because ``assert`` is removed under ``-O`` and
    a check that disappears under an interpreter flag is not one.
    """
    for call_kind, schema in RESPONSE_SCHEMAS.items():
        model = OUTPUT_MODELS[call_kind]
        declared = frozenset(model.model_fields)
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise RuntimeError(f"response schema for {call_kind.value} declares no properties")
        sent = frozenset(properties)
        if sent != declared:
            raise RuntimeError(
                f"response schema for {call_kind.value} names {sorted(sent)} but "
                f"{model.__name__} declares {sorted(declared)}; the provider would be asked "
                "to satisfy a shape the validator does not check"
            )
        required = schema.get("required")
        if not isinstance(required, list) or frozenset(required) != declared:
            raise RuntimeError(
                f"response schema for {call_kind.value} must require every field "
                f"{sorted(declared)}; an optional field is one the model may omit and the "
                "validator will then reject"
            )


_reject_schema_drift()


# ---------------------------------------------------------------------------
# Gate 4: content validation for LINK_DESCRIPTION
# ---------------------------------------------------------------------------

PERMITTED_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"{merchant}"})
"""The declared permitted placeholder set (R27.C9).

One member, matching the single substitution the approved templates in
``revora.execution.messages`` perform. Declared here as well because this package cannot
import that one, and the duplication is narrow enough to be checkable by eye: a second
placeholder is another way for a sentence to say something untrue at render time, so the
set growing is a decision somebody should have to make twice."""

_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{[^{}]*\}")

_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_LINK_TRAILING_PUNCTUATION: Final[str] = ".,;:!?)\"'"

_CURRENCY_SYMBOLS: Final[str] = "\u20b9$\u20ac\u00a3\u00a5"

_MONEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<lead>[" + _CURRENCY_SYMBOLS + r"]|\b[A-Z]{3}\b)?"
    r"[ \u00a0]{0,2}"
    r"(?P<digits>\d{1,3}(?:[,\u00a0]\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)"
    r"[ \u00a0]{0,2}"
    r"(?P<trail>[" + _CURRENCY_SYMBOLS + r"]|\b[A-Z]{3}\b)?"
)
"""Candidate money expressions in customer-visible text.

A numeric run counts as *an amount* only where it carries a money marker: an adjacent
currency symbol or three-letter code, a thousands separator, or a two-place fraction. A
bare integer is left alone, and that boundary is a judgement worth stating rather than
burying. Treating every integer as an amount would refuse "reply within 2 days" and make
the gate reject nearly every plausible sentence, which turns the whole call into a cost
with no benefit; treating none of them as amounts would let a wrong figure through. The
residual risk is a bare integer a customer reads as money — mitigated by the fact that the
contract transmits ``payment_amount_formatted`` already rendered, so a model has the
correct formatted figure in front of it and no reason to invent an unmarked one."""


def validate_link_description_content(
    description: str,
    *,
    payment_amount_formatted: str,
    approved_link: str,
    length_validator: DescriptionValidator,
    permitted_placeholders: frozenset[str] = PERMITTED_PLACEHOLDERS,
) -> tuple[ContentRule, str] | None:
    """R27.C9's rules over one draft. ``None`` means every rule passed.

    Pure: no clock, no configuration, no case row. Everything it compares against is an
    argument, which is what lets the caller be the component that knows the case and lets
    this be a function a property test can hammer.

    Order is deliberate. Length and control characters go first, through
    ``length_validator``, because a draft that is not sendable at all should be refused
    under the rule that says so rather than under whichever content rule happens to match a
    substring of it.

    Args:
        description: the schema-valid draft, as it would be sent.
        payment_amount_formatted: the case's amount, already rendered — the same string the
            Prompt_Contract transmitted. Compared as an exact decimal, never a binary
            number, so "1,234" and "1234.00" are equal and neither has been through a
            lossy representation to get there.
        approved_link: the single Policy_Engine-approved link. Under the recorded Shape B
            decision this is the Customer_Response_Page URL of the case, and the payment
            link lives on that page.
        length_validator: ``validate_description`` with ``MAX_MESSAGE_LENGTH`` bound in.
        permitted_placeholders: the declared permitted set.

    Returns:
        The violated rule and a detail naming what violated it, or ``None``.
    """
    try:
        length_validator(description)
    except Exception as exc:
        # Any refusal is a refusal — see :class:`DescriptionValidator`. Catching broadly is
        # the point: this module does not know the validator's exception type and must not
        # let an unrecognized one become a crash on a path whose fallback already works.
        rule_name = getattr(exc, "rule", None)
        detail = rule_name if isinstance(rule_name, str) else type(exc).__name__
        return ContentRule.DESCRIPTION_REFUSED, detail

    found = _PLACEHOLDER_PATTERN.findall(description)
    for placeholder in found:
        if placeholder not in permitted_placeholders:
            return ContentRule.PLACEHOLDER_NOT_PERMITTED, placeholder
    if found:
        # Every placeholder here is permitted, and every one of them is still unresolved:
        # this is the text as it would be sent, so nothing downstream will substitute it.
        # R27.C9 asks for zero unresolved placeholders remaining, and "remaining" is exactly
        # the state a draft at this point is in.
        return ContentRule.PLACEHOLDER_UNRESOLVED, found[0]

    expected = _first_amount(payment_amount_formatted)
    if expected is None:
        return ContentRule.AMOUNT_UNVERIFIABLE, payment_amount_formatted
    for amount, rendered in _amounts_in(description):
        if amount != expected:
            return ContentRule.AMOUNT_MISMATCH, rendered

    links = _links_in(description)
    target = _normalized_link(approved_link)
    for link in links:
        if _normalized_link(link) != target:
            return ContentRule.UNAPPROVED_LINK, link
    if len(links) > 1:
        # Every link is the approved one, and there is more than one of them. R27.C9 permits
        # "the single Policy_Engine-approved link", and a sentence carrying it twice is not
        # what "single" means. Refusing costs one template substitution.
        return ContentRule.MULTIPLE_LINKS, links[1]
    return None


def _is_amount(match: re.Match[str]) -> bool:
    """True where a numeric run carries a money marker. See :data:`_MONEY_PATTERN`."""
    digits = match.group("digits")
    if match.group("lead") or match.group("trail"):
        return True
    return "," in digits or "\u00a0" in digits or "." in digits


def _as_exact(digits: str) -> Decimal | None:
    """A digit run as an exact decimal, or ``None`` where it is not one."""
    cleaned = digits.replace(",", "").replace("\u00a0", "").replace(" ", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _first_amount(text: str) -> Decimal | None:
    """The first money figure in ``text``, exactly. Used on the case's own rendering.

    Falls back to any numeric run when no marked amount is found, because a formatted
    amount is the one string in this function's world that is *known* to be money — the
    marker heuristic exists to avoid false positives in prose, and there is no prose here.
    """
    for match in _MONEY_PATTERN.finditer(text):
        if _is_amount(match):
            value = _as_exact(match.group("digits"))
            if value is not None:
                return value
    for match in _MONEY_PATTERN.finditer(text):
        value = _as_exact(match.group("digits"))
        if value is not None:
            return value
    return None


def _amounts_in(text: str) -> list[tuple[Decimal, str]]:
    """Every money figure in ``text``, with the substring it was read from."""
    amounts: list[tuple[Decimal, str]] = []
    for match in _MONEY_PATTERN.finditer(text):
        if not _is_amount(match):
            continue
        value = _as_exact(match.group("digits"))
        if value is not None:
            amounts.append((value, match.group(0).strip()))
    return amounts


def _links_in(text: str) -> list[str]:
    """Every link-looking run in ``text``, with trailing sentence punctuation removed."""
    return [
        match.group(0).rstrip(_LINK_TRAILING_PUNCTUATION) for match in _LINK_PATTERN.finditer(text)
    ]


def _normalized_link(link: str) -> str:
    """A link in the form two spellings of the same URL share.

    Case-folded scheme and host, and no trailing slash. Not a full URL normalization, and
    deliberately not: anything cleverer would start deciding that two different URLs are
    the same one, which is the mistake this rule exists to catch.
    """
    return link.strip().rstrip("/").lower()


# ---------------------------------------------------------------------------
# The instruction, and the wire envelope
# ---------------------------------------------------------------------------

_INSTRUCTIONS: Final[Mapping[ReasoningCallKind, str]] = MappingProxyType(
    {
        ReasoningCallKind.CAUSE_HYPOTHESIS: (
            "You classify why a card payment failed or was delayed. The user message is a "
            "JSON object of provider error fields and, where the customer stated one, a "
            "delay reason. Answer with the single most likely cause from the permitted "
            "enumeration, a confidence between 0 and 1, and a summary of at most 200 "
            "characters naming the evidence you used. Answer UNKNOWN where the evidence "
            "does not support a cause."
        ),
        ReasoningCallKind.DECISION_EXPLANATION: (
            "You phrase a decision that has already been made. The user message is a JSON "
            "object holding the selected action, the runner-up, both computed values and "
            "the recorded reason. Write one plain sentence explaining the selection to an "
            "operator. Do not recommend a different action, do not recompute anything, and "
            "do not introduce a figure the object does not contain."
        ),
        ReasoningCallKind.LINK_DESCRIPTION: (
            "You write the single customer-visible sentence attached to a payment request. "
            "The user message is a JSON object holding the merchant name, the amount "
            "already formatted, the currency and the failure cause. Use the formatted "
            "amount exactly as given, include no link of any kind, leave no placeholder in "
            "the text, and be courteous and brief."
        ),
    }
)
"""Static, Revora-authored instructions. One per call kind, interpolating nothing.

Interpolating nothing is the property that matters: an instruction that substituted a case
value would be a second route onto the wire, and the allow-list claim of R27.C2 would then
be about only half of the request. The transmitted case data is the single JSON object in
``contents``, which is what :func:`extract_transmitted_payload` reads back."""


def _wire_body(
    call_kind: ReasoningCallKind, payload: Mapping[str, object]
) -> dict[str, object]:
    """The request body: a static system instruction and exactly one data part."""
    return {
        "systemInstruction": {"parts": [{"text": _INSTRUCTIONS[call_kind]}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _plain(response_schema_for(call_kind)),
            # Zero, as an integer. The three calls want the most probable phrasing of a
            # fixed input, not variety, and a deterministic setting also means a rejected
            # response is reproducible from the stored request.
            "temperature": 0,
            "candidateCount": 1,
        },
    }


def _plain(value: object) -> object:
    """A ``MappingProxyType`` tree as plain containers, so ``json`` can serialize it."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def extract_transmitted_payload(body: bytes | str) -> Mapping[str, object]:
    """The case data object out of a recorded request body. The builder's inverse.

    Exists for the property test and for anybody reading a stored request: R27.C2 and P53
    are claims about the transmitted *field set*, and reading it back should not require
    knowing where in the provider's envelope the data part sits.

    Raises:
        ValueError: where ``body`` is not a request this module built. Raising beats
            returning an empty mapping, which a test asserting a subset relation would
            pass trivially.
    """
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"request body is not JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("request body is not a JSON object")
    contents = decoded.get("contents")
    if not isinstance(contents, list) or not contents:
        raise ValueError("request body carries no contents")
    first = contents[0]
    if not isinstance(first, Mapping):
        raise ValueError("request body's first content entry is not an object")
    parts = first.get("parts")
    if not isinstance(parts, list) or len(parts) != 1:
        raise ValueError("expected exactly one transmitted data part")
    part = parts[0]
    if not isinstance(part, Mapping) or not isinstance(part.get("text"), str):
        raise ValueError("transmitted data part carries no text")
    payload = json.loads(part["text"], parse_float=Decimal)
    if not isinstance(payload, Mapping):
        raise ValueError("transmitted data part is not a JSON object")
    return MappingProxyType({str(key): value for key, value in payload.items()})


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Received:
    """A 2xx whose envelope yielded text. Not yet validated against anything."""

    text: str
    raw_body: str
    model_version: str | None
    latency_ms: int
    http_status: int


def _split_call_budget(total: timedelta) -> tuple[int, int]:
    """Split one attempt's budget into ``(connect_ms, read_ms)`` summing to it exactly.

    A local copy of the provider client's split, and the duplication is forced rather than
    chosen: ``providers.split_timeout`` lives in :mod:`revora.providers`, which
    ``reasoning-containment`` makes unreachable from here. Promoting it to
    :mod:`revora.platform` would be the tidier answer and is a change to a module the
    payment path depends on, which is not a change this task should make quietly.

    One fifth to the connect phase, capped at :data:`MAX_CONNECT_SHARE`, remainder to the
    read. Integer milliseconds throughout so the halves sum to the whole and the two-multiple
    bound of R27.C6 is compared against a number rather than an approximation.
    """
    total_ms = int(total.total_seconds() * 1_000)
    if total_ms < 2:
        raise ValueError("attempt budget must be at least 2 milliseconds")
    ceiling_ms = int(MAX_CONNECT_SHARE.total_seconds() * 1_000)
    connect_ms = max(min(total_ms // _CONNECT_SHARE_DENOMINATOR, ceiling_ms), 1)
    return connect_ms, total_ms - connect_ms


def _is_certificate_failure(exc: BaseException) -> bool:
    """True where ``exc`` is a TLS or certificate failure rather than an ordinary one.

    Walks the exception chain looking for an ``ssl.SSLError``, because ``httpx`` reports a
    certificate rejection as a ``ConnectError`` raised *from* one. The message fallback
    exists for a transport that does not preserve the cause; see
    :data:`_CERTIFICATE_MARKERS` on why the failure direction is chosen this way. Depth is
    bounded and identities are remembered, so a cyclic ``__context__`` cannot spin here.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < _MAX_EXCEPTION_CHAIN_DEPTH:
        if id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ssl.SSLError | ssl.CertificateError):
            return True
        current = current.__cause__ or current.__context__
        depth += 1
    message = str(exc).lower()
    return any(marker in message for marker in _CERTIFICATE_MARKERS)


class ReasoningAdapter:
    """Three bounded advisory calls. Returns results, never decisions.

    Construct one per process and share it: the connection pool is the point. Thread-safe,
    because the only state is an ``httpx.Client`` — documented as safe to share — and three
    immutable configuration values.

    **The credential is resolved per call and never held on the instance.** So a repr of
    this object, a pickle of it or a heap dump cannot contain it, and a rotation takes
    effect on the next call without a restart. There is no concurrency cap here, unlike the
    payment client: that cap exists so an undocumented provider rate limit cannot produce
    responses whose *uncertainty* is expensive, and a throttled reasoning call produces a
    deterministic fallback instead.
    """

    __slots__ = ("_client", "_config", "_model")

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        config: Configuration | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build an adapter.

        Args:
            base_url: the provider API root, version segment included.
            model: one of :data:`VERIFIED_MODELS`.
            config: the merchant's configuration. Defaults to the placeholder set, which is
                what the pure and model test tiers use; a running process passes the
                merchant's own so this adapter and the step that budgets around it agree on
                ``REASONING_TIMEOUT``.
            transport: an ``httpx.BaseTransport`` in place of the real network. This is how
                every gate is exercised without a socket, and it is a constructor argument
                rather than a monkeypatch so the production path holds no test-only branch.

        Raises:
            ValueError: where ``model`` is not verified as listed. See
                :data:`VERIFIED_MODELS` on why this is a construction-time refusal.
        """
        if model not in VERIFIED_MODELS:
            raise ValueError(
                f"model {model!r} is not one of the verified models "
                f"{sorted(VERIFIED_MODELS)}; an unlisted model spends a step's whole "
                "reasoning budget on a 404"
            )
        self._model = model
        self._config = config if config is not None else default_configuration()
        connect_ms, read_ms = _split_call_budget(self._config.REASONING_TIMEOUT)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=connect_ms / 1_000,
                read=read_ms / 1_000,
                write=read_ms / 1_000,
                pool=connect_ms / 1_000,
            ),
            # Gate 2, stated as a decision rather than inherited as a default. Turning this
            # off would make the credential header interceptable and would put a customer's
            # delay note on the wire to whoever answered.
            verify=True,
            # A redirect is not a hop to follow: it would replay a credentialed POST at an
            # unverified location. Arriving as a 3xx it becomes PROVIDER_REFUSED, which is
            # the honest answer.
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "revora/0.1"},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Release the connection pool."""
        self._client.close()

    def __enter__(self) -> ReasoningAdapter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        # No credential to redact, because none is held. That is the point of resolving it
        # per call, and this repr is the visible evidence of it.
        return f"ReasoningAdapter(model={self._model!r})"

    @property
    def model(self) -> str:
        """The model id recorded on every invocation of this adapter."""
        return self._model

    # -- the three calls ---------------------------------------------------

    def propose_cause(
        self,
        *,
        provider_error_code: str | None = None,
        provider_error_reason: str | None = None,
        provider_error_source: str | None = None,
        provider_error_step: str | None = None,
        delay_reason: StrEnum | str | None = None,
        delay_reason_note: str | None = None,
        case_id: str | None = None,
    ) -> ReasoningResult[CauseHypothesisOutput]:
        """``CAUSE_HYPOTHESIS``: a proposed ``RiskCause`` with a confidence.

        Every argument is optional and every one of them is a description of the failure
        rather than of the person who suffered it. ``delay_reason_note`` is free text a
        customer typed, so it is the one field that can carry anything at all; it is
        truncated to ``DELAY_NOTE_MAX_LENGTH`` here (R20.C11).

        The caller records ``AI_ASSISTED`` with the confidence capped at ``0.99`` on
        :class:`Accepted`, ``REJECTED_AI_OUTPUT`` with ``UNKNOWN`` and ``0.0`` on
        :class:`RejectedSchema`, and ``FALLBACK_UNKNOWN`` on anything else. R27.C16 means
        this should not be called at all for a deterministic diagnosis.

        Args:
            case_id: a log field only. It is not a contract-declared name, so gate 1 gives
                it no path onto the wire.
        """
        outcome = self._exchange(
            ReasoningCallKind.CAUSE_HYPOTHESIS,
            {
                "provider_error_code": provider_error_code,
                "provider_error_reason": provider_error_reason,
                "provider_error_source": provider_error_source,
                "provider_error_step": provider_error_step,
                "delay_reason": delay_reason,
                "delay_reason_note": delay_reason_note,
            },
            case_id=case_id,
        )
        if not isinstance(outcome, _Received):
            return outcome
        return self._validated(
            ReasoningCallKind.CAUSE_HYPOTHESIS, outcome, parse_cause_hypothesis
        )

    def explain_decision(
        self,
        *,
        risk_cause: RiskCause,
        baseline_probability: Probability | Decimal,
        selected_action: CandidateAction | str,
        selected_net_recovery_value: Minor | int,
        selection_reason: str,
        currency: str,
        explanation_max_length: int,
        runner_up_action: CandidateAction | str | None = None,
        runner_up_net_recovery_value: Minor | int | None = None,
        case_id: str | None = None,
    ) -> ReasoningResult[DecisionExplanationOutput]:
        """``DECISION_EXPLANATION``: prose for a decision that was already made.

        The model sees the comparison's *output* — the winner, the runner-up, both values
        and the recorded reason — and never its inputs beyond the baseline, because it is
        asked to phrase a decision rather than to make one. The caller stores the result in
        a field marked explanation-only and records that it held no influence (R27.C8).

        Args:
            explanation_max_length: ``REASONING_EXPLANATION_MAX_LENGTH``. **Required, and
                deliberately not defaulted.** That bound has no entry in
                ``Configuration`` yet, and inventing one here would put a number this
                module made up in front of an operator. Passing it in keeps the bound the
                caller's, which is the same rule ``schemas.py`` already follows.
        """
        probability = (
            baseline_probability.value
            if isinstance(baseline_probability, Probability)
            else baseline_probability
        )
        outcome = self._exchange(
            ReasoningCallKind.DECISION_EXPLANATION,
            {
                "risk_cause": risk_cause,
                "baseline_probability": probability,
                "selected_action": selected_action,
                "selected_net_recovery_value": int(selected_net_recovery_value),
                "runner_up_action": runner_up_action,
                "runner_up_net_recovery_value": (
                    None
                    if runner_up_net_recovery_value is None
                    else int(runner_up_net_recovery_value)
                ),
                "selection_reason": selection_reason,
                "currency": currency,
            },
            case_id=case_id,
        )
        if not isinstance(outcome, _Received):
            return outcome
        return self._validated(
            ReasoningCallKind.DECISION_EXPLANATION,
            outcome,
            lambda text: parse_decision_explanation(text, max_length=explanation_max_length),
        )

    def draft_link_description(
        self,
        *,
        merchant_display_name: str,
        payment_amount_formatted: str,
        currency: str,
        risk_cause: RiskCause,
        approved_link: str,
        length_validator: DescriptionValidator,
        permitted_placeholders: frozenset[str] = PERMITTED_PLACEHOLDERS,
        case_id: str | None = None,
    ) -> ReasoningResult[LinkDescriptionOutput]:
        """``LINK_DESCRIPTION``: customer-visible text, schema- *and* content-validated.

        The only call whose result passes gate 4. ``length_validator`` is a required
        argument rather than an optional one, which is the whole design: there is no way to
        obtain an :class:`Accepted` description from this method without the existing
        ``validate_description`` having accepted it, so "sent without the length and
        control-character rules" is unreachable rather than discouraged.

        On :class:`RejectedContent` the caller suppresses the draft, substitutes the
        deterministic template, leaves the customer-message counter unchanged by the
        suppression and writes ``CONTENT_REJECTED`` naming the rule (R27.C10).

        Args:
            approved_link: the Customer_Response_Page URL of the case — the single
                Policy_Engine-approved link under the recorded Shape B decision.
        """
        outcome = self._exchange(
            ReasoningCallKind.LINK_DESCRIPTION,
            {
                "merchant_display_name": merchant_display_name,
                "payment_amount_formatted": payment_amount_formatted,
                "currency": currency,
                "risk_cause": risk_cause,
            },
            case_id=case_id,
        )
        if not isinstance(outcome, _Received):
            return outcome
        validated = self._validated(
            ReasoningCallKind.LINK_DESCRIPTION,
            outcome,
            lambda text: parse_link_description(
                text, max_length=self._config.MAX_MESSAGE_LENGTH
            ),
        )
        if not isinstance(validated, Accepted):
            return validated
        violation = validate_link_description_content(
            validated.output.description,
            payment_amount_formatted=payment_amount_formatted,
            approved_link=approved_link,
            length_validator=length_validator,
            permitted_placeholders=permitted_placeholders,
        )
        if violation is None:
            return validated
        rule, detail = violation
        _logger.warning(
            # The rule, not the offending substring. ``detail`` is model-authored text about
            # a specific case; it travels to the length-bounded, masked audit record on the
            # result and is not a log field, because a log line travels further.
            "reasoning content rejected",
            call_kind=ReasoningCallKind.LINK_DESCRIPTION.value,
            rule=rule.value,
            case_id=case_id,
        )
        return RejectedContent(
            call_kind=ReasoningCallKind.LINK_DESCRIPTION,
            contract_id=validated.contract_id,
            rule=rule,
            detail=detail,
            # The draft is retained rather than logged. It is model-authored text about a
            # specific case, and a log line travels further than an audit record does.
            draft=self._truncate(validated.output.description),
            model_id=validated.model_id,
            model_version=validated.model_version,
            latency_ms=validated.latency_ms,
        )

    # -- gate 3, and the one request path ---------------------------------

    def _validated[OutputT](
        self,
        call_kind: ReasoningCallKind,
        received: _Received,
        parse: Callable[[str], OutputT],
    ) -> Accepted[OutputT] | RejectedSchema:
        """Gate 3: the Pydantic model over the returned text, provider promises aside.

        The provider was asked for this shape via ``responseSchema`` and the answer is
        checked here anyway (R27.C15). Both, always — that is the difference between having
        a fallback and discovering one is needed.
        """
        contract = contract_for(call_kind)
        try:
            output = parse(received.text)
        except ReasoningOutputError as exc:
            _logger.warning(
                "reasoning response rejected by output schema",
                call_kind=call_kind.value,
                contract_id=contract.contract_id,
                reason=exc.reason,
            )
            return RejectedSchema(
                call_kind=call_kind,
                contract_id=contract.contract_id,
                reason=exc.reason,
                raw_response=self._truncate(received.raw_body),
                model_id=self._model,
                model_version=received.model_version,
                latency_ms=received.latency_ms,
                http_status=received.http_status,
            )
        return Accepted(
            output=output,
            call_kind=call_kind,
            contract_id=contract.contract_id,
            model_id=self._model,
            model_version=received.model_version,
            latency_ms=received.latency_ms,
            http_status=received.http_status,
        )

    def _exchange(
        self,
        call_kind: ReasoningCallKind,
        values: Mapping[str, object],
        *,
        case_id: str | None,
    ) -> _Received | RejectedSchema | TimedOut | Unavailable:
        """Gates 1 and 2, the budget, and the retry. The only place a request is issued.

        Order is not arbitrary:

        1. **The contract**, so an unsanctioned kind is refused with nothing built (R27.C1).
        2. **The payload**, so a stray field blocks transmission before a credential is even
           looked up (R27.C2). Cheaper than it sounds and better placed than it looks: gate
           1 stays checkable in a deployment that has no credential at all, which is the
           deployed reality.
        3. **The credential**, whose absence returns immediately with nothing sent and
           nothing waited (R27.C7).
        4. **The request**, inside the two-multiple budget.
        """
        try:
            contract = contract_for(call_kind)
        except UnknownCallKindError as exc:
            return Unavailable(
                call_kind=None,
                contract_id=UNKNOWN_CONTRACT_ID,
                reason=UnavailableReason.UNKNOWN_CALL_KIND,
                detail=str(exc),
                request_issued=False,
            )

        try:
            payload = build_request_payload(
                call_kind, values, delay_note_limit=self._config.DELAY_NOTE_MAX_LENGTH
            )
        except PromptContractViolationError as exc:
            _logger.error(
                "reasoning request blocked: field outside the prompt contract",
                call_kind=call_kind.value,
                contract_id=contract.contract_id,
                offending_fields=sorted(exc.fields),
                case_id=case_id,
            )
            return Unavailable(
                call_kind=call_kind,
                contract_id=contract.contract_id,
                reason=UnavailableReason.PROMPT_CONTRACT_VIOLATION,
                detail=f"undeclared field(s) {sorted(exc.fields)}",
                request_issued=False,
                offending_fields=exc.fields,
            )

        try:
            credential = get_secret_store().llm_credential()
        except CredentialUnavailableError as exc:
            # No request, no wait, no state. The deployed reality is that this branch is the
            # common one, so it is the branch that must cost nothing.
            _logger.info(
                "reasoning unavailable: credential not configured",
                call_kind=call_kind.value,
                credential=exc.credential,
            )
            return Unavailable(
                call_kind=call_kind,
                contract_id=contract.contract_id,
                reason=UnavailableReason.CREDENTIAL_ABSENT,
                detail=exc.credential,
                request_issued=False,
            )

        headers = {API_KEY_HEADER: credential.reveal(), "Content-Type": "application/json"}
        body = _wire_body(call_kind, payload)
        return self._attempt_within_budget(
            call_kind=call_kind,
            contract=contract,
            body=body,
            headers=headers,
            case_id=case_id,
        )

    def _attempt_within_budget(
        self,
        *,
        call_kind: ReasoningCallKind,
        contract: PromptContract,
        body: Mapping[str, object],
        headers: Mapping[str, str],
        case_id: str | None,
    ) -> _Received | RejectedSchema | TimedOut | Unavailable:
        """Issue the request, retrying inside two multiples of ``REASONING_TIMEOUT``.

        The budget is the bound, not the attempt count. ``REASONING_RETRY_COUNT`` says how
        many additional requests are permitted and the clock says whether there is room for
        one, so R27.C6 holds for any configured retry count rather than for the assumed
        value of one.

        **Retrying is safe here in a way it is not on the payment path**, and that is the
        single most important difference between this loop and the provider client's: a
        reasoning call leaves no external effect, so a timed-out request that the provider
        may well have processed costs nothing to repeat. What is not repeated is a
        certificate failure — it will not resolve inside one step — and a rejected response,
        which is an answer rather than a failure to answer.
        """
        timeout = self._config.REASONING_TIMEOUT
        budget = timeout * TOTAL_WAIT_MULTIPLE
        permitted = 1 + max(self._config.REASONING_RETRY_COUNT, 0)
        path = GENERATE_CONTENT_PATH.format(model=self._model)
        started = time.monotonic_ns()
        attempts = 0
        last_detail = "no attempt was issued inside the budget"
        refusal: Unavailable | None = None

        while attempts < permitted:
            elapsed = timedelta(microseconds=(time.monotonic_ns() - started) // 1_000)
            remaining = budget - elapsed
            if remaining <= timedelta(0):
                break
            attempt_budget = min(timeout, remaining)
            attempts += 1
            connect_ms, read_ms = _split_call_budget(attempt_budget)
            try:
                response = self._client.post(
                    path,
                    json=body,
                    headers=dict(headers),
                    timeout=httpx.Timeout(
                        connect=connect_ms / 1_000,
                        read=read_ms / 1_000,
                        write=read_ms / 1_000,
                        pool=connect_ms / 1_000,
                    ),
                )
            except Exception as exc:
                # The catch-all is why no caller needs a ``try``. A reasoning failure must
                # never be the reason a processing step crashes, because the step has a
                # deterministic path that works without any of this.
                classified = self._classify_transport_failure(
                    exc,
                    call_kind=call_kind,
                    contract=contract,
                    attempts=attempts,
                    waited_ms=self._waited_ms(started),
                    case_id=case_id,
                )
                if classified is not None:
                    return classified
                refusal = None
                last_detail = type(exc).__name__
                continue

            if response.status_code // 100 == 2:
                return self._read_success(
                    response,
                    call_kind=call_kind,
                    contract=contract,
                    started=started,
                )
            last_detail = f"HTTP {response.status_code}"
            refusal = Unavailable(
                call_kind=call_kind,
                contract_id=contract.contract_id,
                reason=UnavailableReason.PROVIDER_REFUSED,
                detail=last_detail,
                request_issued=True,
                attempts=attempts,
                waited_ms=self._waited_ms(started),
                raw_response=self._truncate(_response_text(response)),
                http_status=response.status_code,
            )
            if not _worth_retrying(response.status_code):
                return refusal

        waited_ms = self._waited_ms(started)
        _logger.warning(
            "reasoning produced no usable response inside the step budget",
            call_kind=call_kind.value,
            contract_id=contract.contract_id,
            attempts=attempts,
            waited_ms=waited_ms,
            detail=last_detail,
            case_id=case_id,
        )
        if refusal is not None:
            # The budget ran out while the provider was answering, and answering with a
            # refusal. Reporting that as ``TIMEOUT`` would put a provider outage in the
            # column a reader consults to find out whether the model is slow.
            return refusal
        return TimedOut(
            call_kind=call_kind,
            contract_id=contract.contract_id,
            detail=last_detail,
            attempts=attempts,
            waited_ms=waited_ms,
        )

    def _classify_transport_failure(
        self,
        exc: BaseException,
        *,
        call_kind: ReasoningCallKind,
        contract: PromptContract,
        attempts: int,
        waited_ms: int,
        case_id: str | None,
    ) -> Unavailable | None:
        """Gate 2, and the decision about whether another attempt is permitted.

        ``None`` means "retryable, keep going inside the budget". A returned
        :class:`Unavailable` ends the invocation immediately.
        """
        if _is_certificate_failure(exc):
            # Abandoned before any case field reached the wire: the handshake precedes the
            # request body, so a failed handshake means the body was never written.
            _logger.error(
                "reasoning request abandoned: TLS or certificate validation failed",
                call_kind=call_kind.value,
                contract_id=contract.contract_id,
                audit_event_type=TRANSPORT_SECURITY_FAILED,
                error_type=type(exc).__name__,
                case_id=case_id,
            )
            return Unavailable(
                call_kind=call_kind,
                contract_id=contract.contract_id,
                reason=UnavailableReason.TRANSPORT_SECURITY_FAILED,
                detail=type(exc).__name__,
                request_issued=False,
                attempts=attempts,
                waited_ms=waited_ms,
            )
        if isinstance(exc, httpx.HTTPError):
            # A timeout, a connect error, a protocol error. All retryable inside the budget,
            # because there is no effect to duplicate.
            return None
        _logger.exception(
            "unanticipated reasoning transport failure",
            call_kind=call_kind.value,
            contract_id=contract.contract_id,
        )
        return Unavailable(
            call_kind=call_kind,
            contract_id=contract.contract_id,
            reason=UnavailableReason.TRANSPORT_FAILED,
            detail=type(exc).__name__,
            request_issued=True,
            attempts=attempts,
            waited_ms=waited_ms,
        )

    def _read_success(
        self,
        response: httpx.Response,
        *,
        call_kind: ReasoningCallKind,
        contract: PromptContract,
        started: int,
    ) -> _Received | RejectedSchema:
        """A 2xx, read into either usable text or a schema rejection.

        **A 2xx whose envelope carries no text part is a rejection, not a retry.** The
        provider answered; what it sent was unusable. Retrying would spend the rest of the
        budget re-asking a provider whose envelope this module does not recognize, and
        losing the raw body in the process — which is the one thing R27.C5 wants kept.
        """
        raw = _response_text(response)
        text = _extract_text(raw)
        if text is not None:
            return _Received(
                text=text,
                raw_body=raw,
                model_version=_extract_model_version(raw),
                latency_ms=self._waited_ms(started),
                http_status=response.status_code,
            )
        _logger.warning(
            "reasoning response carried no text part",
            call_kind=call_kind.value,
            contract_id=contract.contract_id,
            http_status=response.status_code,
        )
        return RejectedSchema(
            call_kind=call_kind,
            contract_id=contract.contract_id,
            reason="response envelope carried no text part",
            raw_response=self._truncate(raw),
            model_id=self._model,
            model_version=_extract_model_version(raw),
            latency_ms=self._waited_ms(started),
            http_status=response.status_code,
        )

    @staticmethod
    def _waited_ms(started: int) -> int:
        """Elapsed milliseconds since ``started``, from a monotonic reading.

        Monotonic rather than wall-clock: this number is compared against a configured
        bound, and a wall-clock adjustment mid-request would make the comparison report
        something other than how long the step waited. Integer division, so the latency
        recorded on an ``ai_invocation`` row is exactly the number computed here.
        """
        return (time.monotonic_ns() - started) // 1_000_000

    def _truncate(self, text: str) -> str:
        """Keep at most ``AI_RAW_CAPTURE_LIMIT`` characters, marking the cut (R27.C5)."""
        limit = self._config.AI_RAW_CAPTURE_LIMIT
        if limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + TRUNCATION_MARKER


_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})


def _worth_retrying(status: int) -> bool:
    """True where another attempt inside the budget could plausibly answer differently.

    A 4xx that is not 408 or 429 is the provider stating that the request itself is wrong,
    and re-sending an identical body is a way to spend a budget learning the same thing
    twice. A 401 in particular means the credential is wrong, which no retry fixes.
    """
    return status in _RETRYABLE_STATUSES


def _response_text(response: httpx.Response) -> str:
    """The body as text, or an empty string where it will not decode.

    A body that will not decode is still evidence that a response arrived, so this never
    raises: the caller treats an empty string as "nothing usable came back", which is the
    same disposition and one fewer failure mode.
    """
    try:
        return response.text
    except Exception:
        return ""


def _extract_text(raw: str) -> str | None:
    """The concatenated text parts of the first candidate, or ``None``.

    Written against the envelope rather than trusting it: every level is checked, and a
    shape this does not recognize returns ``None`` instead of raising. Provider envelope
    drift then lands on the deterministic path with the raw body retained, which is the
    failure direction R27.C15 exists to keep available.
    """
    try:
        decoded = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    candidates = decoded.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, Mapping):
        return None
    content = first.get("content")
    if not isinstance(content, Mapping):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    chunks = [
        part["text"]
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    ]
    if not chunks:
        return None
    return "".join(chunks)


def _extract_model_version(raw: str) -> str | None:
    """``modelVersion`` from the envelope, or ``None`` where the provider did not send one.

    ``None`` rather than the requested model id, and the difference matters on an
    ``ai_invocation`` row: ``model_id`` is what Revora asked for and ``model_version`` is
    what answered. Filling the second from the first would make a silent provider-side
    version change invisible in exactly the table built to make it visible.
    """
    try:
        decoded = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    version = decoded.get("modelVersion")
    return version if isinstance(version, str) else None

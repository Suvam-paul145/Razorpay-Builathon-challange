"""Five classifications, and the one distinction everything else is built on.

Exactly-once execution does not need to know whether a provider call *succeeded*. It
needs to know whether the external effect **definitely did not happen** or **might
have happened**, because those two answers demand opposite responses: the first
permits another attempt, the second forbids one forever and hands the case to
reconciliation. Every other question is downstream of that one.

The official SDK cannot answer it. It raises on error and normalizes responses, so a
read timeout after the provider created a payment link and a connection refused
before a byte left the process arrive at the caller looking the same. One of those is
a link a customer can pay; the other is nothing at all. Collapsing them is how one
payment link becomes two.

So there are five results and nothing else:

* ``Success(entity)`` — a 2xx whose body validated against the verified field set.
  The effect **exists**.
* ``ClientError(code, reason)`` — a 4xx carrying a parseable provider error object,
  or a local refusal before anything was sent. The effect **does not exist**.
* ``ServerError`` — a 5xx. The effect is **unknown**: a provider that answers 500 may
  still have committed the write.
* ``Timeout`` — a transport failure, carrying the *phase* it happened in. Phase is
  the whole value of this variant: ``CONNECT`` and ``NOT_SENT`` are definitive
  (nothing reached the server), ``AFTER_SEND`` is not.
* ``Unclassifiable(raw)`` — anything else, including a 2xx whose body did not
  validate. The effect is **unknown**.

**A 200 with an unexpected shape is not a success.** The response models below are
strict about the fields the design verified, so provider field drift becomes
``Unclassifiable`` → ``UNCERTAIN`` → reconciliation, rather than a confirmed intent
holding a link id that is not a link id. The failure direction is chosen: a false
``Unclassifiable`` costs a reconciliation read, a false ``Success`` costs a customer
being told a payment link exists when it does not.

**The connect-phase failure is the only network error treated as definitive**, and it
keeps the ``Timeout`` classification rather than being promoted to ``ClientError``.
The design's Response Classification table assigns exactly that: classification
``Timeout``, intent state ``FAILED``, "Does not exist — nothing left the process".
``ClientError`` is wrong for it because ``ClientError.code`` is a provider code that
gets recorded on the intent and shown to a merchant, and there is no provider code
here — the provider never saw the request. Callers read
:func:`effect_certainty` rather than pattern-matching on the phase, so the definitive
set can grow without every caller needing to learn about it.

**The resend has its own table, and it is the create's table plus two deliberate
overrides.** ``POST /v1/payment_links/:id/notify_by/:medium`` answers with a success
boolean and no provider object, so there is no identifier to confirm against — and, the
part that decides everything downstream, **no endpoint that reports whether a
notification was sent.** An ``UNCERTAIN`` resend is therefore not slow to resolve; it is
unresolvable, because no read answers the question. :func:`classify_resend_response` is
that table. It reuses :func:`classify_response` for every band and changes exactly two
things:

* the entity is :class:`PaymentLinkResendAck`, which validates ``{"success": true}`` and
  nothing else, so ``success`` absent, false or non-boolean lands in ``Unclassifiable``
  rather than being read as a message that reached somebody;
* **429 is a definitive** ``ClientError``, not the ``Unclassifiable`` an unparseable 4xx
  would otherwise become. A 429 is the provider's own gateway stating that it declined to
  act, and a rejection delivered nothing. The classification therefore does not read the
  429 body at all — its shape is unverified, and not depending on it is the point.

Everything below that is unchanged, which is why there is no second certainty function:
``ServerError`` and a post-send ``Timeout`` are unknown for a resend for the same reason
they are unknown for a create, and a connect-phase failure is definitive for a resend for
the same reason too. :func:`effect_certainty` already maps the whole resend table onto the
three certainties, and :func:`revora.execution.intents.classify_into_intent_state` already
maps those onto the three intent states.

Nothing in this module raises. ``ValidationError`` from Pydantic is caught at the one
boundary that produces a classification, because a caller that has to wrap a provider
call in ``try`` is a caller that will one day forget, and a crash mid-execution is the
exact window exactly-once has to survive.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from revora.domain.payment_event import PaymentStatus
from revora.platform.secrets import CREDENTIAL_UNAVAILABLE

__all__ = [
    "CONCURRENCY_CAP_REJECTED",
    "CREDENTIAL_UNAVAILABLE_CODE",
    "INVALID_REQUEST_REFUSED",
    "LOCAL_REFUSAL_CODES",
    "MAX_RETAINED_RAW_BODY",
    "PAYMENT_LINK_ID_PREFIX",
    "PAYMENT_LINK_STATUSES",
    "PAYMENT_STATUSES",
    "RATE_LIMITED",
    "RATE_LIMITED_REASON",
    "RATE_LIMITED_STATUS",
    "TRUNCATION_MARKER",
    "CallPhase",
    "ClientError",
    "EffectCertainty",
    "PaymentEntity",
    "PaymentLinkEntity",
    "PaymentLinkList",
    "PaymentLinkResendAck",
    "PaymentList",
    "ProviderResult",
    "ResultSource",
    "ServerError",
    "Success",
    "Timeout",
    "Unclassifiable",
    "classify_resend_response",
    "classify_response",
    "effect_certainty",
    "extract_entity_list",
    "is_definitive_failure",
    "parse_payment",
    "parse_payment_link",
    "parse_payment_link_list",
    "parse_payment_link_resend",
    "parse_payment_list",
    "refused_for_concurrency_cap",
    "refused_for_credential",
    "refused_for_invalid_request",
    "truncate_raw",
]

MAX_RETAINED_RAW_BODY: Final[int] = 2_000
"""How much of an unclassifiable body is kept, in characters. **[ASSUMPTION]** — no
configured bound covers it. Chosen well inside ``MAX_AUDIT_FIELD_LENGTH`` (8000),
because this string is written to an audit field and a value that gets truncated
again on the way into the record would be truncated twice, at two different lengths,
by two components that disagree about what was retained."""

TRUNCATION_MARKER: Final[str] = "…[truncated]"
"""Appended to a truncated body so a diagnosis does not read the tail of a cut-off
JSON document as a malformed document."""

PAYMENT_LINK_ID_PREFIX: Final[str] = "plink_"
"""Verified prefix of a Payment Link id. Checked, because a 2xx whose ``id`` is not a
payment link id is not a created payment link, whatever else the body contains."""

PAYMENT_LINK_STATUSES: Final[frozenset[str]] = frozenset(
    {"created", "partially_paid", "expired", "cancelled", "paid"}
)
"""The verified Payment Link status enumeration. A value outside it is treated as
field drift rather than accepted, which routes the call to reconciliation instead of
letting an unrecognized state be reported as a working link."""

PAYMENT_STATUSES: Final[frozenset[str]] = frozenset(member.value for member in PaymentStatus)
"""The verified payment status enumeration, reused from ``domain.payment_event`` so
the provider adapter and the detection path cannot disagree about what a status is."""

RATE_LIMITED_STATUS: Final[int] = 429
"""The one status code this module treats differently for a resend than for a create.

Named rather than inlined because the difference it makes is a policy decision, not a
band boundary: see :func:`classify_resend_response`."""

RATE_LIMITED: Final[str] = "RATE_LIMITED"
"""``ClientError.code`` on a 429 answering a resend.

Minted here rather than taken from the response body, and deliberately *not* in
:data:`LOCAL_REFUSAL_CODES`: the provider really did reject the request, so the source is
``PROVIDER`` and a merchant reading this code is reading a fact about the provider. What is
not read from the provider is the *body* — the 429 body shape is unverified, so a
classification that parsed it would be a guarantee resting on an assumption."""

RATE_LIMITED_REASON: Final[str] = "provider declined the notification: rate limited"
"""The recorded reason for a rate-limited resend. A fixed string, because the alternative
is provider-controlled prose from an unverified body in a merchant-visible field."""

CREDENTIAL_UNAVAILABLE_CODE: Final[str] = CREDENTIAL_UNAVAILABLE
"""Code on a refusal caused by an unresolvable credential (R17.C4).

Deliberately the same token as the audit event type the platform already defines, so
the caller's audit record and the classification cannot drift apart."""

CONCURRENCY_CAP_REJECTED: Final[str] = "CONCURRENCY_CAP_REJECTED"
"""Code on a refusal caused by the client's self-imposed concurrency cap."""

INVALID_REQUEST_REFUSED: Final[str] = "INVALID_REQUEST_REFUSED"
"""Code on a call the client refused to issue because the caller's arguments violate a
bound the provider documents. Distinct from the provider's own ``BAD_REQUEST_ERROR`` so
"we never asked" stays distinguishable from "the provider said no"."""

LOCAL_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        CREDENTIAL_UNAVAILABLE_CODE,
        CONCURRENCY_CAP_REJECTED,
        INVALID_REQUEST_REFUSED,
    }
)
"""Every code this client mints itself. Enumerated so a reader can tell at a glance
which ``ClientError.code`` values did not come from the provider."""


@unique
class EffectCertainty(StrEnum):
    """What is known about the external effect. The design's fourth column.

    Three values, not two. "Unknown" is not a hedge — it is the state the whole
    reconciliation machinery exists to resolve, and a type that could not express it
    would force every caller to guess.
    """

    EXISTS = "EXISTS"
    DOES_NOT_EXIST = "DOES_NOT_EXIST"
    UNKNOWN = "UNKNOWN"


@unique
class CallPhase(StrEnum):
    """How far the request got before the transport failed.

    ``NOT_SENT`` — the client refused or never obtained a connection. ``CONNECT`` — the
    connection could not be established, so no bytes reached the server.
    ``AFTER_SEND`` — bytes may have reached the server, which includes every read
    timeout. The first two are definitive; the third is the dangerous one.
    """

    NOT_SENT = "NOT_SENT"
    CONNECT = "CONNECT"
    AFTER_SEND = "AFTER_SEND"


@unique
class ResultSource(StrEnum):
    """Whether a result reflects a provider response or a local decision.

    Present on ``ClientError`` only, and it matters: a merchant-visible failure reason
    of ``CREDENTIAL_UNAVAILABLE`` is a statement about Revora's configuration, not
    about the provider rejecting anything, and a record that cannot tell those apart
    sends someone to read the wrong logs.
    """

    PROVIDER = "PROVIDER"
    CLIENT = "CLIENT"


# ---------------------------------------------------------------------------
# Response models. Strict on the verified surface, tolerant of additions.
# ---------------------------------------------------------------------------


class PaymentLinkEntity(BaseModel):
    """A Payment Link, restricted to the verified response fields.

    ``extra="ignore"`` so a field the provider adds later does not start quarantining
    live executions, matching ``ingestion.canonical``. Required-ness is the actual
    gate: ``id``, ``short_url`` and ``status`` are what make a link a link, so a body
    missing any of them is drift rather than a success.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    short_url: str
    status: str
    reference_id: str | None = None
    amount: int | None = None
    amount_paid: int | None = None
    currency: str | None = None
    description: str | None = None
    expire_by: int | None = None
    notes: Mapping[str, str] | None = None

    @field_validator("id")
    @classmethod
    def _id_is_a_payment_link_id(cls, value: str) -> str:
        if not value.startswith(PAYMENT_LINK_ID_PREFIX):
            raise ValueError(f"payment link id must start with {PAYMENT_LINK_ID_PREFIX!r}")
        return value

    @field_validator("status")
    @classmethod
    def _status_is_verified(cls, value: str) -> str:
        if value not in PAYMENT_LINK_STATUSES:
            raise ValueError(f"unverified payment link status {value!r}")
        return value

    @field_validator("short_url")
    @classmethod
    def _short_url_is_present(cls, value: str) -> str:
        # A blank short_url is a link nobody can pay. Reported as drift so the case
        # goes to reconciliation rather than to a customer.
        if not value.strip():
            raise ValueError("short_url must not be blank")
        return value


class PaymentEntity(BaseModel):
    """A payment, restricted to the verified response fields of the authoritative read.

    ``status``, ``captured``, ``amount`` and ``amount_refunded`` are required because
    the design verifies all four as returned, and because the recovery decision reads
    them: ``captured`` is what separates real recovery from ``authorized``, where the
    money has not moved. ``currency`` is optional — it is not named in the verified
    fetch-payment response, and requiring a field on the strength of an assumption
    would turn every authoritative read into a coin toss about provider drift.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    status: str
    captured: bool
    amount: int
    amount_refunded: int
    created_at: int | None = None
    """UNIX seconds, **verified** in the Fetch All Payments response parameters as the
    instant the payment was created.

    Optional, and the outcome monitor treats it as a best-available proxy rather than a
    capture timestamp, because that is what it is. The verified surface exposes no
    ``captured_at``. For a recovered case the captured payment is a *new* payment made
    through the payment link, so its creation instant is within seconds of the customer
    paying — close enough to report, and the monitor records which source it used so the
    figure is never mistaken for something more precise than it is."""

    contact: str | None = None
    email: str | None = None
    """**PII. Transient, and never persisted in clear from this object.**

    Present because the detection-gap backfill needs them: it synthesizes a canonical event
    from an API read, and the canonicalizer derives the non-reversible ``customer_key`` and
    the masked contact from exactly these two fields. Without them a backfilled payment
    cannot produce a case at all — ``recovery_case.customer_key`` is ``NOT NULL``.

    The design's PII resolution is that the encrypted raw event store is the *only* holder of
    cleartext contact. These fields honour that: the backfill puts them into the encrypted
    payload and nowhere else, and :func:`revora.outcome.reads.persist_read` excludes them
    explicitly when it retains a read's response as JSONB. A test asserts that exclusion,
    because a field that is safe only by convention will one day be copied somewhere it
    is not."""

    currency: str | None = None
    order_id: str | None = None
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_reason: str | None = None
    error_source: str | None = None
    error_step: str | None = None

    @field_validator("status")
    @classmethod
    def _status_is_verified(cls, value: str) -> str:
        if value not in PAYMENT_STATUSES:
            raise ValueError(f"unverified payment status {value!r}")
        return value


class PaymentLinkResendAck(BaseModel):
    """The entire verified resend response: one boolean, and no provider object.

    ``{"success": true}`` is the whole documented body of
    ``POST /v1/payment_links/:id/notify_by/:medium``. There is no notification identifier
    in it, and no endpoint reports whether a notification was sent — which is why a resend
    intent persists a Revora-composed token in ``provider_response_id`` and why an
    ``UNCERTAIN`` resend is never reconciled by reading.

    **Strict, unlike every other model here, and the strictness is most of the model.** A
    create response carries ten fields and an ``id`` whose prefix is checked, so a body
    that is nearly right is still recognizably a payment link. This response carries one
    field. If that field is not a JSON boolean — ``"true"``, ``1``, ``"yes"``, all of which
    pydantic's lax mode accepts — then the body is not the verified shape, and reading it as
    a delivered message would confirm an intent on the strength of a coercion. ``strict``
    routes it to ``Unclassifiable`` instead, and for a resend that means the case escalates
    to a person. That is the expensive direction; the cheap direction is a customer who
    never received the message being recorded as messaged.

    ``success: false`` is refused by the validator rather than accepted as a parsed
    negative, because it is not one. No ``false`` response is documented, so a ``false``
    body is unverified surface — and "the provider says it did not send" is only different
    from "we do not know what the provider did" if the field can be trusted, which is
    exactly what an undocumented value cannot be. Both therefore become ``Unclassifiable``,
    the honest answer, at the cost of escalating a case that may well have delivered
    nothing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    success: bool

    @field_validator("success")
    @classmethod
    def _success_is_true(cls, value: bool) -> bool:
        if not value:
            raise ValueError("resend response reports success false")
        return value


@dataclass(frozen=True, slots=True)
class PaymentList:
    """A page of payments from the fetch-all endpoint, for the detection-gap backfill.

    A distinct type from :class:`PaymentLinkList` for the same reason that one is a type
    rather than a bare tuple: an empty page is a real answer — "no payments in this
    window" — and it must be impossible to confuse with a response that could not be
    parsed. The backfill decides whether Revora missed a failed payment on the strength
    of this, and reading drift as "nothing to backfill" would leave the detection gap the
    backfill exists to close.

    ``page_count`` is the provider's own ``count`` field, retained because the backfill
    paginates: the endpoint caps a page at 100, so a lookback window busier than that
    needs ``skip``, and the only way to know a page was full is to compare against what
    was asked for.
    """

    payments: tuple[PaymentEntity, ...]
    page_count: int = 0

    @property
    def is_empty(self) -> bool:
        """True when the provider answered and the window held no payments."""
        return not self.payments

    def failed_payments(self) -> tuple[PaymentEntity, ...]:
        """Only the failed ones — the payments the backfill may need to ingest.

        Filtered here rather than by the caller so the definition of "failed" is the
        provider's own status token in one place, and so the backfill cannot accidentally
        ingest a captured payment as revenue at risk.
        """
        return tuple(
            payment
            for payment in self.payments
            if payment.status == PaymentStatus.FAILED.value
        )


@dataclass(frozen=True, slots=True)
class PaymentLinkList:
    """The result of the reconciliation read: the links found under one ``reference_id``.

    A distinct type rather than a bare tuple so that ``Success(PaymentLinkList(()))``
    is unmistakably "the provider answered, and no such link exists" — which is a
    real, load-bearing answer — and can never be confused with "the response did not
    parse", which is ``Unclassifiable`` and means the opposite.
    """

    links: tuple[PaymentLinkEntity, ...]

    @property
    def exists(self) -> bool:
        """True if the effect this ``reference_id`` names already exists."""
        return bool(self.links)

    @property
    def first(self) -> PaymentLinkEntity | None:
        """The single link a unique ``reference_id`` should yield, or ``None``."""
        return self.links[0] if self.links else None


# ---------------------------------------------------------------------------
# The five results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Success[EntityT]:
    """A 2xx whose body validated. The effect exists.

    Generic over the entity so ``fetch_payment`` returning a payment and
    ``create_payment_link`` returning a link are the same shape to a caller and
    different types to a type checker.
    """

    entity: EntityT
    http_status: int


@dataclass(frozen=True, slots=True)
class ClientError:
    """A definitive failure: the effect does not exist.

    Two origins, distinguished by ``source``. From the provider, it is a 4xx carrying
    a parseable error object and ``code`` is the provider's own code
    (``BAD_REQUEST_ERROR``, ``GATEWAY_ERROR``, ``SERVER_ERROR``). From this client, it
    is a refusal made before anything was sent — an unresolvable credential, or the
    concurrency cap — and ``code`` is one of :data:`LOCAL_REFUSAL_CODES`.

    Both are definitive for the same reason, which is why they share a variant: in the
    provider case the request was rejected, in the local case it was never issued.
    Neither leaves an effect behind.
    """

    code: str
    reason: str
    source: ResultSource = ResultSource.PROVIDER
    http_status: int | None = None
    description: str | None = None
    raw: str | None = None

    @property
    def is_local_refusal(self) -> bool:
        """True if this client minted the code rather than the provider."""
        return self.source is ResultSource.CLIENT

    @property
    def audit_event_type(self) -> str | None:
        """The audit event type the caller must record, if this refusal names one.

        ``CREDENTIAL_UNAVAILABLE`` for an unresolvable credential (R17.C4 requires
        that exact record), ``None`` otherwise. Exposed here so the execution engine
        does not have to reimplement the mapping and get it subtly wrong.
        """
        if self.code == CREDENTIAL_UNAVAILABLE_CODE:
            return CREDENTIAL_UNAVAILABLE_CODE
        return None


@dataclass(frozen=True, slots=True)
class ServerError:
    """A 5xx. The effect is unknown.

    A provider that answers 500 may have committed the write and failed afterwards.
    Retrying is exactly what must not happen, which is why this is a classification
    and not an exception with a retry decorator over it.
    """

    http_status: int
    raw: str | None = None


@dataclass(frozen=True, slots=True)
class Timeout:
    """A transport failure. ``phase`` says whether it is definitive.

    ``CONNECT`` and ``NOT_SENT`` mean nothing reached the server, so the effect does
    not exist. ``AFTER_SEND`` — every read timeout, every mid-stream protocol error —
    means it might. **A read timeout is never retried.** That single rule is the
    difference between one payment link and two.
    """

    phase: CallPhase
    detail: str
    attempts: int = 1
    """How many times the request was issued. ``2`` only ever on the connect path,
    where the retry is safe because no bytes left the process."""


@dataclass(frozen=True, slots=True)
class Unclassifiable:
    """Everything else, including a 2xx that did not validate. The effect is unknown.

    ``raw`` is retained, truncated, because this is the variant a human has to read to
    find out what the provider actually sent. Discarding the body here would leave a
    field-drift incident with no evidence in it.
    """

    raw: str
    detail: str
    http_status: int | None = None


type ProviderResult[EntityT] = (
    Success[EntityT] | ClientError | ServerError | Timeout | Unclassifiable
)
"""Exactly one of the five. Every client method returns this and never raises."""


_DEFINITIVE_PHASES: Final[frozenset[CallPhase]] = frozenset(
    {CallPhase.NOT_SENT, CallPhase.CONNECT}
)


def effect_certainty(result: ProviderResult[object]) -> EffectCertainty:
    """What is known about the external effect after ``result``.

    The design's Response Classification table, as a function. Callers branch on this
    rather than on the variant, so that "which results permit another attempt" is
    defined in one place instead of in every caller's ``match`` statement.
    """
    match result:
        case Success():
            return EffectCertainty.EXISTS
        case ClientError():
            return EffectCertainty.DOES_NOT_EXIST
        case Timeout(phase=phase) if phase in _DEFINITIVE_PHASES:
            return EffectCertainty.DOES_NOT_EXIST
        case _:
            return EffectCertainty.UNKNOWN


def is_definitive_failure(result: ProviderResult[object]) -> bool:
    """True if the effect definitely does not exist, so a further attempt is permitted."""
    return effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST


def truncate_raw(text: str, limit: int = MAX_RETAINED_RAW_BODY) -> str:
    """Keep at most ``limit`` characters, marking the cut.

    Raises:
        ValueError: if ``limit`` is negative. A negative retention limit is a
            programming error, and silently clamping it would hide the mistake in the
            one field a diagnosis depends on.
    """
    if limit < 0:
        raise ValueError("limit must not be negative")
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def refused_for_credential(credential: str) -> ClientError:
    """The classification for a call refused because a credential would not resolve.

    R17.C4's contract for the caller: change no state, make no call, audit
    ``CREDENTIAL_UNAVAILABLE``. ``reason`` names the credential and never its value —
    ``CredentialUnavailableError`` is built to the same rule.
    """
    return ClientError(
        code=CREDENTIAL_UNAVAILABLE_CODE,
        reason=credential,
        source=ResultSource.CLIENT,
    )


def refused_for_concurrency_cap(operation: str) -> ClientError:
    """The classification for a call refused by the self-imposed concurrency cap.

    Definitive, because the request was never issued. The caller should treat it as a
    transient operational condition and let the job queue re-present the work, not as
    a provider rejection.
    """
    return ClientError(
        code=CONCURRENCY_CAP_REJECTED,
        reason=operation,
        source=ResultSource.CLIENT,
    )


def refused_for_invalid_request(operation: str, detail: str) -> ClientError:
    """The classification for a call the client refused to issue as malformed.

    Used where the provider documents a bound that the client can check first — the
    payment-window and pagination bounds on the fetch-all-payments endpoint. Sending a
    knowingly invalid request would earn a documented 400, which classifies as a
    definitive provider ``ClientError``; refusing locally keeps a caller's arithmetic
    mistake distinguishable from the provider's own verdict on a well-formed request.

    Definitive, because nothing was sent. ``detail`` carries the offending value, which
    is safe: these are timestamps and page offsets, never customer data.
    """
    return ClientError(
        code=INVALID_REQUEST_REFUSED,
        reason=f"{operation}: {detail}",
        source=ResultSource.CLIENT,
    )


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


def parse_payment_link(payload: object) -> PaymentLinkEntity:
    """Validate a decoded body as a Payment Link.

    Raises:
        ValidationError: on anything that is not one. The caller turns this into
            ``Unclassifiable``.
    """
    return PaymentLinkEntity.model_validate(payload)


def parse_payment(payload: object) -> PaymentEntity:
    """Validate a decoded body as a payment.

    Raises:
        ValidationError: on anything that is not one.
    """
    return PaymentEntity.model_validate(payload)


def extract_entity_list(payload: object) -> Sequence[object] | None:
    """Find the entity list in a provider collection response, or ``None``.

    **The envelope field names of the fetch-all Payment Links response are not in the
    design's verified surface.** The design verifies that the endpoint supports
    querying by ``reference_id``; it does not state what the list is called. So this
    does not assume a name. It accepts a bare JSON array, an ``items`` array (the
    collection convention the verification spikes also assumed, marked unverified
    there too), or — if neither is present — the sole list-valued field in the object,
    which covers a ``payment_links`` key without hard-coding one.

    ``None`` when no list can be found, and that distinction is the load-bearing part
    of this function. Returning an empty list for an envelope we failed to understand
    would let reconciliation read provider drift as "the link does not exist" and mark
    an intent ``FAILED`` while a customer is holding a payable link. ``None`` becomes
    ``Unclassifiable``, which escalates to a human instead.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    items = payload.get("items")
    if isinstance(items, list):
        return items
    lists = [value for value in payload.values() if isinstance(value, list)]
    if len(lists) == 1:
        return lists[0]
    return None


def parse_payment_list(payload: object) -> PaymentList:
    """Validate a decoded fetch-all-payments body as a page of payments.

    The envelope is **verified**: ``{"entity": "collection", "count": N, "items": [...]}``
    per the official Fetch All Payments documentation.

    Deliberately stricter than :func:`extract_entity_list`, and the asymmetry is the
    point. That function accepts a bare JSON array because the payment-*links* envelope
    is unverified, so refusing an unnamed shape there would reject a response that is
    probably valid. Here the shape is known, so anything else is drift — and this parser
    requires ``items`` on a mapping and nothing else.

    Strictness runs in the safe direction. The two functions' failure modes are mirror
    images: for links, guessing wrong risks concluding "no link exists" while a customer
    holds a payable one; for payments, guessing wrong risks concluding "no payments in
    this window" while failed payments sit there undetected. A bare ``[]`` is the sharpest
    case — under a permissive reading it reports an empty window, which tells the backfill
    that Revora missed nothing and leaves the detection gap it exists to close wide open.
    Refusing sends it to ``Unclassifiable``, which escalates to a human.

    Raises:
        ValidationError: if any member is not a payment.
        ValueError: if the body is not the documented envelope, which becomes
            ``Unclassifiable`` rather than an empty page.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("payments collection response is not an object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("payments collection response has no 'items' array")
    raw_count = payload.get("count")
    page_count = (
        raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    )
    return PaymentList(
        payments=tuple(PaymentEntity.model_validate(item) for item in items),
        page_count=page_count,
    )


def parse_payment_link_list(payload: object) -> PaymentLinkList:
    """Validate a decoded collection body as a list of Payment Links.

    Raises:
        ValidationError: if any member is not a Payment Link.
        ValueError: if no entity list could be located at all — see
            :func:`extract_entity_list` for why that is not the same as an empty one.
    """
    items = extract_entity_list(payload)
    if items is None:
        raise ValueError("no entity list found in collection response")
    return PaymentLinkList(tuple(PaymentLinkEntity.model_validate(item) for item in items))


def parse_payment_link_resend(payload: object) -> PaymentLinkResendAck:
    """Validate a decoded body as a resend acknowledgement.

    Raises:
        ValidationError: on anything that is not ``{"success": true}`` — an absent field, a
            false one, a coercible non-boolean, or a body that is not an object at all. The
            caller turns this into ``Unclassifiable``, which for a resend is terminal.
    """
    return PaymentLinkResendAck.model_validate(payload)


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

_SUCCESS_BAND: Final[range] = range(200, 300)
_CLIENT_BAND: Final[range] = range(400, 500)
_SERVER_BAND: Final[range] = range(500, 600)


def classify_response[EntityT](
    *,
    http_status: int,
    body_text: str,
    parse: Callable[[object], EntityT],
    raw_limit: int = MAX_RETAINED_RAW_BODY,
) -> ProviderResult[EntityT]:
    """Turn one HTTP response into exactly one of the five results. Never raises.

    Args:
        http_status: the response status code.
        body_text: the response body as text. Decoded by the caller, which owns the
            transport, so this function stays pure and directly testable.
        parse: validates the decoded JSON into the entity this operation expects.
            Expected to raise ``ValidationError`` or ``ValueError`` on drift.
        raw_limit: retention limit for a body kept as evidence.

    The bands, and why each is drawn where it is:

    * **2xx** — the success band, not only 200. The design's table names 200 because
      that is what the documented responses return, but classifying a hypothetical 201
      carrying a valid link as ``Unclassifiable`` would send a real creation to
      reconciliation. The entity validation is the gate that matters.
    * **4xx** — ``ClientError`` *only* with a parseable error object. That qualifier is
      the design's, and it is load-bearing: a 4xx whose body is an HTML page from an
      intermediary tells us nothing about whether the provider ever saw the request,
      so it falls through to ``Unclassifiable`` and its "unknown" certainty rather
      than being recorded as a definitive rejection with a code nobody sent.
    * **5xx** — ``ServerError``. Unknown by definition.
    * **anything else**, including 1xx and 3xx — ``Unclassifiable``. Redirects are not
      followed, so a 3xx is an unexpected surface, not a hop.
    """
    if http_status in _SUCCESS_BAND:
        decoded = _decode_json(body_text)
        if decoded is _UNDECODABLE:
            return Unclassifiable(
                raw=truncate_raw(body_text, raw_limit),
                detail="body is not valid JSON",
                http_status=http_status,
            )
        try:
            entity = parse(decoded)
        except (ValidationError, ValueError) as exc:
            return Unclassifiable(
                raw=truncate_raw(body_text, raw_limit),
                detail=_validation_detail(exc),
                http_status=http_status,
            )
        return Success(entity=entity, http_status=http_status)

    if http_status in _CLIENT_BAND:
        error = _parse_error_object(body_text)
        if error is None:
            return Unclassifiable(
                raw=truncate_raw(body_text, raw_limit),
                detail="4xx without a parseable error object",
                http_status=http_status,
            )
        return ClientError(
            code=error.code,
            reason=error.reason,
            source=ResultSource.PROVIDER,
            http_status=http_status,
            description=error.description,
            raw=truncate_raw(body_text, raw_limit),
        )

    if http_status in _SERVER_BAND:
        return ServerError(http_status=http_status, raw=truncate_raw(body_text, raw_limit))

    return Unclassifiable(
        raw=truncate_raw(body_text, raw_limit),
        detail=f"unexpected http status {http_status}",
        http_status=http_status,
    )


def classify_resend_response[EntityT](
    *,
    http_status: int,
    body_text: str,
    parse: Callable[[object], EntityT],
    raw_limit: int = MAX_RETAINED_RAW_BODY,
) -> ProviderResult[EntityT]:
    """Turn one resend response into exactly one of the five results. Never raises.

    Signature-compatible with :func:`classify_response` on purpose: the client threads one
    of the two through its single request path, so the resend's table is exercised by the
    same code that issues the call rather than by a branch beside it.

    The table, outcome by outcome, with the intent state each certainty produces:

    ===================================== ==================== =============
    Provider outcome                      ``ProviderResult``   Intent state
    ===================================== ==================== =============
    200, body validates ``success: true``  ``Success``          ``CONFIRMED``
    200, ``success`` absent/false/other    ``Unclassifiable``   ``UNCERTAIN``
    200, body unparseable                  ``Unclassifiable``   ``UNCERTAIN``
    **429**                                ``ClientError``      ``FAILED``
    4xx with a parseable error object       ``ClientError``      ``FAILED``
    4xx without one                         ``Unclassifiable``   ``UNCERTAIN``
    5xx                                    ``ServerError``      ``UNCERTAIN``
    Read timeout / reset (``AFTER_SEND``)   ``Timeout``          ``UNCERTAIN``
    Connect-phase failure                   ``Timeout``          ``FAILED``
    ===================================== ==================== =============

    Only the 429 row is written here. Every other row is :func:`classify_response`
    unchanged, and the intent-state column is :func:`effect_certainty` unchanged — the
    resend does not get a second certainty function, because the question "did the effect
    happen" has the same answer shape for both effects.

    **Why 429 is definitive when an unparseable 4xx is not.** The generic rule sends an
    unparseable 4xx to ``Unclassifiable`` because a 4xx whose body is an HTML page from an
    intermediary says nothing about whether the provider ever saw the request. A 429 is
    different in kind: it is the provider's own gateway stating that it declined to act, and
    a rejection delivered nothing. So the body is not consulted — its shape is unverified,
    and a classification that depended on it would be resting a customer-facing guarantee on
    an assumption.

    **What a 429 costs.** The customer-message counter moves on the single
    ``ACTION_SCHEDULED -> EXECUTING`` edge, before the request, and a definitive failure does
    not give it back. A rate-limited resend therefore spends one message increment for
    nothing. That is a recorded deviation from R24.C12's "increment on ``CONFIRMED``", and it
    is the deliberate direction: a design in which a rejected attempt is free is a design in
    which a loop against a rate limit burns no budget until the window closes.

    **``UNCERTAIN`` here is terminal.** Every ``UNCERTAIN`` row above names an outcome no
    read can settle, because the resend response carries no identifier and no endpoint
    reports whether a notification was sent. See
    :func:`revora.execution.resend.settle_resend_result`, which escalates once with
    ``EXECUTION_RESULT_UNVERIFIABLE`` and issues no further external call.

    Args:
        http_status: the response status code.
        body_text: the response body as text, decoded by the caller.
        parse: normally :func:`parse_payment_link_resend`. A parameter rather than a
            hard-coded call so this stays substitutable for :func:`classify_response`.
        raw_limit: retention limit for a body kept as evidence.
    """
    if http_status == RATE_LIMITED_STATUS:
        return ClientError(
            code=RATE_LIMITED,
            reason=RATE_LIMITED_REASON,
            source=ResultSource.PROVIDER,
            http_status=http_status,
            raw=truncate_raw(body_text, raw_limit),
        )
    return classify_response(
        http_status=http_status, body_text=body_text, parse=parse, raw_limit=raw_limit
    )


class _ErrorObject(BaseModel):
    """The verified provider error object.

    ``code`` is the only required field; ``reason`` and the rest are documented as
    present but a rejection that named a code and nothing else is still a definitive
    rejection, and refusing to classify it would turn a clear "no" into an "unknown"
    that halts the case.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    code: str
    description: str | None = None
    reason: str | None = None
    source: str | None = None
    step: str | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class _ExtractedError:
    code: str
    reason: str
    description: str | None


_UNDECODABLE: Final[object] = object()
"""Sentinel. ``None`` cannot serve: ``null`` is a valid JSON document, and a body of
``null`` is drift rather than a decode failure — different classifications."""


def _decode_json(body_text: str) -> object:
    try:
        return json.loads(body_text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _UNDECODABLE


def _parse_error_object(body_text: str) -> _ExtractedError | None:
    """The provider error object from a 4xx body, or ``None`` if there is not one.

    The documented envelope is ``{"error": {...}}``; a bare error object is accepted
    too, because the point of this function is to recognize a definitive rejection and
    an envelope difference does not make a rejection less definitive.
    """
    decoded = _decode_json(body_text)
    if decoded is _UNDECODABLE or not isinstance(decoded, Mapping):
        return None
    candidate = decoded.get("error", decoded)
    if not isinstance(candidate, Mapping):
        return None
    try:
        parsed = _ErrorObject.model_validate(candidate)
    except ValidationError:
        return None
    return _ExtractedError(
        code=parsed.code,
        # ``reason`` is what the diagnosis table keys on, ``description`` is prose for
        # a human. Falling back keeps the field populated without inventing a token:
        # an empty reason would read as "the provider gave no reason", which is a
        # different fact from "the provider gave prose instead of a code".
        reason=parsed.reason or parsed.description or parsed.code,
        description=parsed.description,
    )


def _validation_detail(exc: Exception) -> str:
    """A short, value-free description of why a body did not validate.

    Field locations and messages only. The offending value is already retained in
    ``raw``; repeating it here would put it in a second field that a caller might log
    without knowing it is provider-controlled text.
    """
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if not errors:  # pragma: no cover - pydantic always populates
            return "response did not validate"
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid"))
        return f"{location}: {message}" if location else message
    return str(exc)

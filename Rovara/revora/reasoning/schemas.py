"""Output validation, independent of anything the provider promised (R27.C15).

The adapter asks the provider to constrain its response to a schema *and* validates the
body here regardless. That is not belt-and-braces caution; it is the difference between
having a fallback and not having one. Provider-side constraint is an optimization, and a
component that treats it as a guarantee has nothing to do the day it changes.

So validation is a **pure function of the response body**. Nothing in this module reads
configuration, a database row or a clock. Given the same bytes it returns the same model
or raises the same rejection, which is what lets P53's and R27.C5's checks be tests
rather than integration runs.

Three deliberate choices worth the words:

* **``extra="forbid"``**, which is the opposite of :mod:`revora.providers.classification`.
  There, an unexpected field must be tolerated because rejecting it would quarantine a
  live execution that already moved money. Here, rejection costs one deterministic
  fallback and nothing else, so the stricter reading wins: a body carrying a key the
  contract never declared is drift, and drift on this path is free to refuse.
* **No floats, ever.** ``confidence`` is a ``Decimal`` and the before-validator accepts
  only ``Decimal``, ``int`` or ``str``. :func:`parse_cause_hypothesis` parses the JSON
  with ``parse_float=Decimal`` so the value is exact from the first moment it exists —
  a confidence that went through binary representation on the way in cannot be compared
  against ``AI_CONFIDENCE_CEILING`` and give the same answer twice.
* **Length bounds arrive as arguments.** ``REASONING_EXPLANATION_MAX_LENGTH`` and
  ``MAX_MESSAGE_LENGTH`` are configured bounds, and the ``reasoning-containment`` import
  contract keeps this package away from everything except ``revora.domain`` and
  ``revora.platform``. Rather than reach for a config accessor, the two bounded parse
  functions take ``max_length`` and refuse to validate without it, so the bound cannot be
  quietly skipped. ``evidence_summary``'s 200 is a schema literal from design.md, not a
  configured bound, so it lives here as a constant.

The confidence range is ``0.0`` to ``1.0`` **inclusive**, exactly as R27.C5 states. The
cap to ``0.99`` for an ``AI_ASSISTED`` diagnosis is R27.C4's and belongs to the caller
that records the Diagnosis; a schema that rejected ``1.0`` would make R27.C5's own
wording untestable and would hide a model claiming certainty behind a validation error
instead of recording it and capping it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

from revora.domain.enums import ReasoningCallKind, RiskCause
from revora.domain.probability import Confidence

__all__ = [
    "CONFIDENCE_MAX",
    "CONFIDENCE_MIN",
    "EVIDENCE_SUMMARY_MAX_LENGTH",
    "MAX_LENGTH_CONTEXT_KEY",
    "OUTPUT_MODELS",
    "CauseHypothesisOutput",
    "DecisionExplanationOutput",
    "LinkDescriptionOutput",
    "ReasoningOutputError",
    "parse_cause_hypothesis",
    "parse_decision_explanation",
    "parse_link_description",
]

EVIDENCE_SUMMARY_MAX_LENGTH: Final[int] = 200
"""A schema literal from design.md's Prompt_Contract table, not a configured bound.

It bounds a field nobody is shown: ``evidence_summary`` is kept as the reason a cause was
proposed. 200 characters is enough to name the evidence and too short to smuggle a second
opinion into a field the Policy_Engine cannot see anyway.
"""

CONFIDENCE_MIN: Final[Decimal] = Decimal(0)
CONFIDENCE_MAX: Final[Decimal] = Decimal(1)

MAX_LENGTH_CONTEXT_KEY: Final[str] = "max_length"
"""The validation-context key the bounded models read their limit from.

Passed through ``model_validate(..., context={...})`` rather than held as a module global,
because a global would be process state and two merchants can configure different bounds
in the same worker.
"""


class ReasoningOutputError(ValueError):
    """A response body did not satisfy the declared output schema.

    Carries the failing call kind and a human-readable reason. The raw body is *not*
    carried: the adapter already holds it and is the component that truncates it to
    ``AI_RAW_CAPTURE_LIMIT`` for the Audit_Record (R27.C5). Putting it here too would
    create a second copy of a model response in an exception message, which is exactly
    the kind of thing that ends up in a log line nobody meant to write.
    """

    def __init__(self, call_kind: ReasoningCallKind, reason: str) -> None:
        self.call_kind = call_kind
        self.reason = reason
        super().__init__(f"{call_kind.value} response rejected: {reason}")


def _require_non_blank(value: str, *, label: str) -> str:
    """Refuse a field that validates structurally and says nothing."""
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def _within_context_bound(value: str, info: ValidationInfo, *, label: str) -> str:
    """Bound ``value`` by the ``max_length`` supplied in the validation context.

    Raises where the context is missing or unusable, rather than defaulting. A default
    here would mean a caller that forgot to pass the configured bound still got a model
    back, bounded by a number this module invented — and the first symptom would be a
    customer-visible string longer than the provider's ``description`` field accepts.
    """
    context = info.context if isinstance(info.context, Mapping) else None
    limit = context.get(MAX_LENGTH_CONTEXT_KEY) if context is not None else None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError(
            f"{label} cannot be validated without a positive "
            f"{MAX_LENGTH_CONTEXT_KEY!r} in the validation context; the bound is "
            "configuration and this package cannot read it for itself"
        )
    if len(value) > limit:
        raise ValueError(f"{label} is {len(value)} characters, limit is {limit}")
    return _require_non_blank(value, label=label)


class CauseHypothesisOutput(BaseModel):
    """A proposed ``RiskCause`` with a confidence and the evidence behind it.

    ``cause`` is typed as :class:`RiskCause`, so "names a cause outside the enumeration"
    from R27.C5 is a validation failure rather than a check somebody has to remember to
    write. Every one of R27.C5's four rejection conditions — schema failure, unknown
    cause, out-of-range confidence, missing required field — is refused by this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: RiskCause
    confidence: Decimal
    evidence_summary: str

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_is_exact(cls, value: object) -> Decimal:
        # An allow-list of input types rather than a check against the one type we
        # refuse. Same effect for a JSON number, but it also refuses a list, a dict and
        # None with the same message instead of accepting whatever Decimal() tolerates.
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool):
            raise ValueError("confidence must be a number, not a boolean")
        if isinstance(value, int | str):
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"confidence is not a decimal number: {value!r}") from exc
        raise ValueError(
            "confidence must be an exact decimal; parse the response body with "
            "parse_float=Decimal so the value never passes through binary form"
        )

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: Decimal) -> Decimal:
        if value.is_nan() or value.is_infinite():
            raise ValueError(f"confidence must be a finite number, got {value}")
        if value < CONFIDENCE_MIN or value > CONFIDENCE_MAX:
            raise ValueError(f"confidence must be between 0 and 1 inclusive, got {value}")
        return value

    @field_validator("evidence_summary")
    @classmethod
    def _evidence_summary_is_bounded(cls, value: str) -> str:
        if len(value) > EVIDENCE_SUMMARY_MAX_LENGTH:
            raise ValueError(
                f"evidence_summary is {len(value)} characters, limit is "
                f"{EVIDENCE_SUMMARY_MAX_LENGTH}"
            )
        return _require_non_blank(value, label="evidence_summary")

    def as_confidence(self) -> Confidence:
        """The returned confidence as a domain :class:`Confidence`.

        Quantized to three places by ``Confidence`` itself. Uncapped on purpose — the
        ``0.99`` ceiling for an ``AI_ASSISTED`` diagnosis is applied where the Diagnosis
        is recorded (R27.C4), because that is the only place that knows the method.
        """
        return Confidence.of(self.confidence)


class DecisionExplanationOutput(BaseModel):
    """Prose for a decision that was already made.

    One field, because there is nothing else this call is permitted to contribute. The
    selected action stays derived from the integer ``net_recovery_value`` comparison and
    this string is stored explanation-only (R27.C8) — a second field here would be the
    first step toward a model having an opinion the optimizer could read.

    The bound is ``REASONING_EXPLANATION_MAX_LENGTH``, supplied through the validation
    context. Use :func:`parse_decision_explanation`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    explanation: str

    @field_validator("explanation")
    @classmethod
    def _explanation_is_bounded(cls, value: str, info: ValidationInfo) -> str:
        return _within_context_bound(value, info, label="explanation")


class LinkDescriptionOutput(BaseModel):
    """Customer-visible description text, schema-valid but not yet permitted to send.

    Passing this model is necessary and nowhere near sufficient. R27.C9's content rules —
    permitted placeholders with none unresolved, every amount equal to the case's
    ``payment_amount``, zero links other than the Customer_Response_Page URL — need the
    case to check against, and this module has no case and cannot get one.

    They run in the Execution_Engine, against the existing
    ``providers.payment_link.validate_description``, which is where R27.C9 and R27.C10 put
    them. Not here, and not in the adapter either: ``reasoning-containment`` forbids
    ``revora.reasoning`` from importing ``revora.providers``, so this package could not
    reach that validator if it wanted to.

    The bound is ``MAX_MESSAGE_LENGTH``. Use :func:`parse_link_description`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str

    @field_validator("description")
    @classmethod
    def _description_is_bounded(cls, value: str, info: ValidationInfo) -> str:
        return _within_context_bound(value, info, label="description")


OUTPUT_MODELS: Mapping[ReasoningCallKind, type[BaseModel]] = MappingProxyType(
    {
        ReasoningCallKind.CAUSE_HYPOTHESIS: CauseHypothesisOutput,
        ReasoningCallKind.DECISION_EXPLANATION: DecisionExplanationOutput,
        ReasoningCallKind.LINK_DESCRIPTION: LinkDescriptionOutput,
    }
)
"""The output model per call kind. Read by the adapter when it records which schema a
response was judged against; the parse functions below are the way to actually validate,
because two of the three need a bound the model cannot fetch for itself."""


def _decoded(body: str | bytes, call_kind: ReasoningCallKind) -> object:
    """JSON-decode ``body`` with exact decimals, or reject it.

    ``parse_float=Decimal`` is the whole point of routing every parse through here: it is
    the one place where a number arrives, and making it exact at that point means no
    downstream code has to be trusted to avoid binary arithmetic.
    """
    try:
        return json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReasoningOutputError(call_kind, f"body is not valid JSON: {exc}") from exc


def _validate[ModelT: BaseModel](
    model: type[ModelT],
    decoded: object,
    call_kind: ReasoningCallKind,
    context: Mapping[str, object] | None = None,
) -> ModelT:
    """Run ``model`` over ``decoded``, flattening Pydantic's error list into one reason.

    Generic in the model so the parse functions below return their concrete type without
    an ``isinstance`` narrowing step — a runtime check for something the type system
    already knows would be a third copy of the same guarantee.
    """
    try:
        return model.model_validate(decoded, context=context)
    except ValidationError as exc:
        reason = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ReasoningOutputError(call_kind, reason) from exc


def parse_cause_hypothesis(body: str | bytes) -> CauseHypothesisOutput:
    """Validate a ``CAUSE_HYPOTHESIS`` response body.

    Takes no bound: both of this schema's limits — the ``RiskCause`` enumeration and the
    200-character ``evidence_summary`` — are declared, not configured.

    Raises:
        ReasoningOutputError: on any of R27.C5's conditions. The caller records
            ``RiskCause.UNKNOWN`` with confidence ``0.0`` and the method
            ``REJECTED_AI_OUTPUT``, retaining the raw body to ``AI_RAW_CAPTURE_LIMIT``.
    """
    kind = ReasoningCallKind.CAUSE_HYPOTHESIS
    return _validate(CauseHypothesisOutput, _decoded(body, kind), kind)


def parse_decision_explanation(body: str | bytes, *, max_length: int) -> DecisionExplanationOutput:
    """Validate a ``DECISION_EXPLANATION`` response body against ``max_length``.

    Args:
        body: the raw response body.
        max_length: ``Configuration.REASONING_EXPLANATION_MAX_LENGTH``. Required, because
            this package cannot read configuration for itself — see the module docstring.

    Raises:
        ReasoningOutputError: on a malformed body, a missing or blank explanation, an
            explanation longer than ``max_length``, or an unexpected field.
    """
    kind = ReasoningCallKind.DECISION_EXPLANATION
    return _validate(
        DecisionExplanationOutput,
        _decoded(body, kind),
        kind,
        {MAX_LENGTH_CONTEXT_KEY: max_length},
    )


def parse_link_description(body: str | bytes, *, max_length: int) -> LinkDescriptionOutput:
    """Validate a ``LINK_DESCRIPTION`` response body against ``max_length``.

    Args:
        body: the raw response body.
        max_length: ``Configuration.MAX_MESSAGE_LENGTH`` (300). Required for the same
            reason as above.

    Raises:
        ReasoningOutputError: on a malformed body, a missing or blank description, a
            description longer than ``max_length``, or an unexpected field. Content
            validation (R27.C9) is a separate, later gate.
    """
    kind = ReasoningCallKind.LINK_DESCRIPTION
    return _validate(
        LinkDescriptionOutput,
        _decoded(body, kind),
        kind,
        {MAX_LENGTH_CONTEXT_KEY: max_length},
    )

"""Task 49.3 and 49.4, at the level that needs no database and no socket.

Three claims live here because they are claims about *pure functions and signatures*, and
proving them against Postgres would prove the same thing more slowly while making the
failure message worse:

* **The five result variants map onto exactly three diagnosis methods**, with the R27.C4
  ceiling applied to the one that carries a usable confidence. A confidence of exactly 1.000
  stays reachable only through ``DETERMINISTIC``, which is what lets a reader of the
  ``diagnosis`` table read the method off the confidence.
* **Every column R27.C12 names is derivable from every variant**, including the two that
  carry no model id and the one where no request was issued. A row that could not be built
  for a timeout would be a row the timeout never got.
* **Every pure component takes the reasoning result as an ``| None`` argument.** Asserted from
  the signatures, because that is where the property lives: with no credential configured the
  argument is ``None`` on every path, so "identical with every response removed" is a call
  rather than a mock.

There is deliberately no ``httpx.MockTransport`` in this module. The adapter's own gates are
tested in ``tests/test_reasoning_adapter.py``; what is under test here is the wiring around
them, and constructing a fake provider to exercise a mapping table would be a slower way to
assert the same thing.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from revora.diagnosis.service import (
    AiCauseProposal,
    capped_ai_confidence,
    resolve_ai_diagnosis,
    resolve_recorded_diagnosis,
    run_diagnosis,
)
from revora.domain.enums import DiagnosisMethod, ReasoningCallKind, RiskCause
from revora.domain.probability import AI_CONFIDENCE_CEILING
from revora.execution.engine import execute_approved_action
from revora.jobs import pipeline
from revora.platform.config import default_configuration
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.reasoning.adapter import (
    Accepted,
    ContentRule,
    ReasoningVerdict,
    RejectedContent,
    RejectedSchema,
    TimedOut,
    Unavailable,
    UnavailableReason,
    validate_link_description_content,
    verdict_of,
)
from revora.reasoning.schemas import CauseHypothesisOutput

pytestmark = pytest.mark.pure

FLOOR = Decimal("0.60")
_MODEL = "gemini-2.5-flash"


def _accepted(confidence: str) -> Accepted[CauseHypothesisOutput]:
    return Accepted(
        output=CauseHypothesisOutput(
            cause=RiskCause.ABANDONMENT,
            confidence=Decimal(confidence),
            evidence_summary="customer said they forgot",
        ),
        call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
        contract_id="cause-hypothesis/1",
        model_id=_MODEL,
        model_version="gemini-2.5-flash-001",
        latency_ms=412,
        http_status=200,
    )


def _rejected_schema() -> RejectedSchema:
    return RejectedSchema(
        call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
        contract_id="cause-hypothesis/1",
        reason="cause: not a permitted enumeration member",
        raw_response='{"cause": "VIBES"}',
        model_id=_MODEL,
        model_version=None,
        latency_ms=380,
        http_status=200,
    )


def _rejected_content() -> RejectedContent:
    return RejectedContent(
        call_kind=ReasoningCallKind.LINK_DESCRIPTION,
        contract_id="link-description/1",
        rule=ContentRule.AMOUNT_MISMATCH,
        detail="INR 9,999.00",
        draft="Pay INR 9,999.00 to Acme.",
        model_id=_MODEL,
        model_version="gemini-2.5-flash-001",
        latency_ms=290,
    )


def _timed_out() -> TimedOut:
    return TimedOut(
        call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
        contract_id="cause-hypothesis/1",
        detail="read timeout",
        attempts=2,
        waited_ms=19_400,
    )


def _unavailable(reason: UnavailableReason, *, issued: bool, waited_ms: int = 0) -> Unavailable:
    return Unavailable(
        call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
        contract_id="cause-hypothesis/1",
        reason=reason,
        detail="llm_credential" if not issued else "503",
        request_issued=issued,
        waited_ms=waited_ms,
    )


# ---------------------------------------------------------------------------
# R27.C4 — the ceiling, and what 1.000 still means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        ("1.0", "0.990"),
        ("0.995", "0.990"),
        ("0.990", "0.990"),
        ("0.870", "0.870"),
        ("0.000", "0.000"),
    ],
)
def test_an_ai_confidence_is_capped_at_the_ceiling(returned: str, expected: str) -> None:
    """``min(returned, 0.99)``, and never a rounding of it.

    The cap is applied to what the model returned rather than enforced by the output schema,
    because R27.C5's permitted range is 0 to 1 *inclusive*: a model claiming certainty is
    recorded and then capped, not hidden behind a validation error that would report a
    confident answer as a malformed one.
    """
    assert capped_ai_confidence(Decimal(returned)) == Decimal(expected)


def test_only_a_deterministic_diagnosis_can_reach_a_confidence_of_one() -> None:
    """R3.C10 and R27.C4 together, as a property of the two resolvers.

    The deterministic path is the only route to 1.000, so a reader of the ``diagnosis`` table
    can read the method off the confidence. That is worth more than it looks: it means a
    row whose method column was mis-set is detectable from a second column rather than
    only from the audit trail.
    """
    deterministic = resolve_recorded_diagnosis(
        cause=RiskCause.INSUFFICIENT_FUNDS,
        confidence=Decimal("1.000"),
        method=DiagnosisMethod.DETERMINISTIC,
        confidence_floor=FLOOR,
    )
    assert deterministic.confidence == Decimal("1.000")

    certain_model = resolve_ai_diagnosis(
        AiCauseProposal(
            cause=RiskCause.ABANDONMENT,
            confidence=Decimal("1.000"),
            method=DiagnosisMethod.AI_ASSISTED,
            invoked=True,
        ),
        confidence_floor=FLOOR,
    )
    assert certain_model.method is DiagnosisMethod.AI_ASSISTED
    assert certain_model.confidence == AI_CONFIDENCE_CEILING.value
    assert certain_model.confidence < Decimal("1.000")


def test_a_proposal_may_not_claim_the_deterministic_method() -> None:
    """The one construction the type refuses, and the reason it refuses it.

    A proposal claiming ``DETERMINISTIC`` would carry a model's answer past the ceiling and
    into the confidence R3.C10 reserves for the provider's own error field. Refused at
    construction rather than checked at the write, so the mistake cannot exist as a value.
    """
    with pytest.raises(ValueError, match="DETERMINISTIC"):
        AiCauseProposal(
            cause=RiskCause.ABANDONMENT,
            confidence=Decimal("1.000"),
            method=DiagnosisMethod.DETERMINISTIC,
            invoked=True,
        )


def test_a_low_confidence_accepted_cause_is_substituted_and_did_not_influence() -> None:
    """R3.C8 still governs the AI path, and the invocation row agrees with the diagnosis.

    Both callers of :func:`resolve_ai_diagnosis` read the same answer — the pipeline to decide
    whether the ``ai_invocation`` row may claim ``influenced_recommendation``, and
    ``run_diagnosis`` to decide what to write. One implementation is what stops the row and
    the row it describes from disagreeing.
    """
    recorded = resolve_ai_diagnosis(
        AiCauseProposal(
            cause=RiskCause.ABANDONMENT,
            confidence=Decimal("0.400"),
            method=DiagnosisMethod.AI_ASSISTED,
            invoked=True,
        ),
        confidence_floor=FLOOR,
    )
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.substituted_to_unknown is True
    assert recorded.original_cause is RiskCause.ABANDONMENT
    assert recorded.confidence == Decimal("0.400"), (
        "the recorded confidence is a fact about the answer produced; rewriting it to zero "
        "would erase the evidence that a near-threshold answer existed"
    )


# ---------------------------------------------------------------------------
# The five variants onto three methods
# ---------------------------------------------------------------------------


def test_an_accepted_response_is_an_ai_assisted_proposal() -> None:
    proposal = pipeline._proposal_from(_accepted("0.870"))
    assert proposal.method is DiagnosisMethod.AI_ASSISTED
    assert proposal.cause is RiskCause.ABANDONMENT
    assert proposal.confidence == Decimal("0.870")
    assert proposal.invoked is True


def test_a_schema_rejection_is_unknown_at_zero_with_the_rejected_method() -> None:
    """R27.C5's disposition, exactly: ``UNKNOWN``, ``0.0``, ``REJECTED_AI_OUTPUT``.

    ``REJECTED_AI_OUTPUT`` rather than ``FALLBACK_UNKNOWN``, and the distinction is the whole
    reason both members exist: a request was sent and its answer thrown away, which is a
    different operational fact from no answer having arrived.
    """
    proposal = pipeline._proposal_from(_rejected_schema())
    assert proposal.method is DiagnosisMethod.REJECTED_AI_OUTPUT
    assert proposal.cause is RiskCause.UNKNOWN
    assert proposal.confidence == Decimal("0.000")
    assert proposal.invoked is True


def test_a_timeout_is_a_fallback_and_still_counts_as_an_invocation() -> None:
    proposal = pipeline._proposal_from(_timed_out())
    assert proposal.method is DiagnosisMethod.FALLBACK_UNKNOWN
    assert proposal.invoked is True, (
        "a request left the process and no answer came back; R3.C7 asks whether one was "
        "issued, not whether one was useful"
    )


@pytest.mark.parametrize(
    ("reason", "issued"),
    [
        (UnavailableReason.CREDENTIAL_ABSENT, False),
        (UnavailableReason.PROMPT_CONTRACT_VIOLATION, False),
        (UnavailableReason.PROVIDER_REFUSED, True),
        (UnavailableReason.TRANSPORT_SECURITY_FAILED, True),
    ],
)
def test_unavailable_reports_invocation_from_request_issued_not_from_the_reason(
    reason: UnavailableReason, *, issued: bool
) -> None:
    """R3.C7 read off the fact R27.C2 and R27.C7 actually assert.

    An absent credential and a refused payload sent nothing, so neither is an invocation. A
    provider refusal and a certificate failure did reach the wire, so both are. Deriving this
    from the reason would put the mapping in two places; the adapter carries the fact instead.
    """
    proposal = pipeline._proposal_from(_unavailable(reason, issued=issued))
    assert proposal.method is DiagnosisMethod.FALLBACK_UNKNOWN
    assert proposal.invoked is issued


# ---------------------------------------------------------------------------
# R27.C12 — every variant produces a complete row
# ---------------------------------------------------------------------------


def test_every_variant_yields_a_verdict_and_a_contract_id() -> None:
    """One row per invocation means every variant has to be writable (R27.C12).

    ``prompt_contract_id`` is ``NOT NULL``, so a variant that could not name one would be a
    variant whose row silently went unwritten — and the deterministic-fallback rate would
    then be computed over the successes only.
    """
    variants = [
        (_accepted("0.870"), ReasoningVerdict.ACCEPTED),
        (_rejected_schema(), ReasoningVerdict.REJECTED_SCHEMA),
        (_rejected_content(), ReasoningVerdict.REJECTED_CONTENT),
        (_timed_out(), ReasoningVerdict.TIMEOUT),
        (
            _unavailable(UnavailableReason.PROVIDER_REFUSED, issued=True),
            ReasoningVerdict.UNAVAILABLE,
        ),
    ]
    for result, expected in variants:
        columns = pipeline._invocation_columns(result, fallback_model_id=_MODEL)
        assert verdict_of(result) is expected
        assert columns["contract_id"]
        assert columns["call_kind"] in {kind.value for kind in ReasoningCallKind}


def test_a_timeout_records_what_it_waited_and_the_model_it_asked() -> None:
    """``latency_ms`` from ``waited_ms``, and ``model_id`` from the adapter's own model.

    A timeout carries no ``model_id`` of its own because nothing answered — but a request
    *was* issued to a known model, and a row that left the column null would make "which
    model times out" unanswerable.
    """
    columns = pipeline._invocation_columns(_timed_out(), fallback_model_id=_MODEL)
    assert columns["latency_ms"] == 19_400
    assert columns["model_id"] == _MODEL
    assert columns["model_version"] is None, (
        "model_version is what answered; filling it from what was asked for would hide a "
        "silent provider-side version change in the table built to expose it"
    )


def test_nothing_sent_records_no_latency_and_no_model() -> None:
    """An absent credential waited for nothing, and the row says so rather than saying zero."""
    columns = pipeline._invocation_columns(
        _unavailable(UnavailableReason.CREDENTIAL_ABSENT, issued=False),
        fallback_model_id=_MODEL,
    )
    assert columns["latency_ms"] is None
    assert columns["model_id"] is None


def test_a_rejected_response_and_a_rejected_draft_are_both_retained() -> None:
    """R27.C5 keeps the refused body; R27.C10 keeps the refused draft.

    Both land in ``raw_response_truncated``, which is the column whose whole purpose is that
    "the model returned something we refused" stays diagnosable. The draft is retained rather
    than logged, because a log line travels further than an audit record does.
    """
    schema = pipeline._invocation_columns(_rejected_schema(), fallback_model_id=_MODEL)
    assert schema["retained"] == '{"cause": "VIBES"}'

    content = pipeline._invocation_columns(_rejected_content(), fallback_model_id=_MODEL)
    assert content["retained"] == "Pay INR 9,999.00 to Acme."
    assert content["detail"] == ContentRule.AMOUNT_MISMATCH.value


def test_the_content_rejection_audit_payload_names_the_rule_and_keeps_the_draft() -> None:
    """R27.C10 wants the violated rule named, not a count of violations."""
    result = _rejected_content()
    evidence = pipeline._reasoning_audit_evidence(
        result, pipeline._invocation_columns(result, fallback_model_id=_MODEL)
    )
    assert evidence["violated_rule"] == ContentRule.AMOUNT_MISMATCH.value
    assert evidence["retained_draft"] == "Pay INR 9,999.00 to Acme."


def test_a_refused_payload_audit_payload_names_the_offending_fields() -> None:
    """R27.C2 asks for the offending fields listed, so the record lists them."""
    result = Unavailable(
        call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS,
        contract_id="cause-hypothesis/1",
        reason=UnavailableReason.PROMPT_CONTRACT_VIOLATION,
        detail="undeclared field(s) ['customer_contact']",
        request_issued=False,
        offending_fields=frozenset({"customer_contact", "card_last4"}),
    )
    evidence = pipeline._reasoning_audit_evidence(
        result, pipeline._invocation_columns(result, fallback_model_id=_MODEL)
    )
    assert evidence["offending_fields"] == ["card_last4", "customer_contact"]


# ---------------------------------------------------------------------------
# R27.C13 — the bound, derived rather than invented
# ---------------------------------------------------------------------------


def test_the_per_case_bound_is_the_call_kinds_times_the_recovery_attempts() -> None:
    """Derived from a configured value, so the two cannot drift.

    An operator who raises the decision-cycle cap raises the reasoning allowance in step, and
    one who lowers it does not leave a case permitted more model calls than decisions.
    """
    config = default_configuration()
    assert pipeline.reasoning_call_bound(config) == 3 * config.MAX_RECOVERY_ATTEMPTS
    assert len(ReasoningCallKind) == 3, (
        "R27.C1's enumeration is closed at three; a fourth kind changes the bound and should "
        "have to be noticed here"
    )


# ---------------------------------------------------------------------------
# R27.C9 — the formatted amount the content rule compares against
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (250_000, "INR", "INR 2,500.00"),
        (1_234_567_89, "INR", "INR 12,34,567.89"),
        (250_000, "usd", "USD 2,500.00"),
    ],
)
def test_the_transmitted_amount_is_rendered_without_a_symbol_table(
    amount: int, currency: str, expected: str
) -> None:
    """The currency code leads the figure, so this module needs no symbol table.

    Two things follow. The pipeline cannot render a figure that disagrees with the server's
    own, because it has no second opinion about what money looks like; and the leading code
    is what makes the figure unambiguously *an amount* to the content rule's money pattern.
    """
    assert pipeline._formatted_amount(amount, currency) == expected


def test_a_draft_repeating_the_rendered_amount_passes_the_equality_rule() -> None:
    """The rendering and the rule agree, which is what keeps ``AMOUNT_UNVERIFIABLE`` unreached.

    A formatted amount the money pattern could not read would make the amount-equality rule
    unevaluable, and an unevaluable rule is refused rather than skipped — so every draft would
    fall back for a reason that was really a formatting mistake here.
    """
    formatted = pipeline._formatted_amount(250_000, "INR")
    assert (
        validate_link_description_content(
            f"Your payment of {formatted} to Acme did not go through.",
            payment_amount_formatted=formatted,
            approved_link=pipeline.NO_APPROVED_LINK,
            length_validator=lambda text: text,
        )
        is None
    )


def test_any_link_at_all_is_unapproved_while_no_response_page_url_exists() -> None:
    """``NO_APPROVED_LINK`` is the strict reading of R27.C9, not a placeholder.

    Nothing in ``revora`` composes a Customer_Response_Page URL yet and the approved template
    carries no link, so "zero links other than the Customer_Response_Page URL" means zero
    links — and the empty reference is what makes the rule say that.
    """
    violation = validate_link_description_content(
        "Pay at https://not-us.example/pay",
        payment_amount_formatted="INR 2,500.00",
        approved_link=pipeline.NO_APPROVED_LINK,
        length_validator=lambda text: text,
    )
    assert violation is not None
    assert violation[0] is ContentRule.UNAPPROVED_LINK


# ---------------------------------------------------------------------------
# The ``| None`` argument, as a property of the signatures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("function", "parameter"),
    [
        (run_diagnosis, "ai_proposal"),
        (execute_approved_action, "ai_description"),
    ],
)
def test_every_pure_component_takes_the_result_as_an_optional_argument(
    function: object, parameter: str
) -> None:
    """The shape Properties 49 to 51 depend on, checked where it actually lives.

    A component that *held* an adapter could not be run without one, so "identical with every
    response removed" would need a mocked provider and would then be a claim about the mock.
    With the result as a defaulted ``| None`` argument it is a claim about a call: pass
    ``None`` and the component is the component it was before this layer existed.
    """
    signature = inspect.signature(function)  # type: ignore[arg-type]
    assert parameter in signature.parameters, (
        f"{getattr(function, '__name__', function)} no longer takes {parameter}; the reasoning "
        "result has become something other than an argument"
    )
    argument = signature.parameters[parameter]
    assert argument.default is None, (
        f"{parameter} must default to None, so a caller that knows nothing about the reasoning "
        "layer gets the deterministic behaviour without asking for it"
    )
    assert argument.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_three_handlers_accept_an_adapter_and_default_to_resolving_one() -> None:
    """All three invocation sites are reachable from a test without a network.

    The adapter is a handler argument rather than a module lookup at the call site, which is
    what lets a test supply an ``httpx.MockTransport``-backed one and exercise every gate
    without a socket — and leaves the production path holding no test-only branch.
    """
    handlers = (
        pipeline.handle_diagnosis,
        pipeline.handle_optimizer,
        pipeline.handle_execution,
    )
    for handler in handlers:
        parameter = inspect.signature(handler).parameters["reasoning"]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_no_adapter_is_constructed_without_a_credential() -> None:
    """R27.C7 at the coarsest granularity available: nothing is built at all.

    The deployed reality is that ``REVORA_LLM_CREDENTIAL`` is unset, so this is the branch
    that must cost nothing — no client, no payload, no wait, no ``ai_invocation`` row. Every
    caller short-circuits on the ``None``, which is why "no state means waiting for the model"
    needs no state to be introduced.
    """
    previous = set_secret_store(SecretStore(EnvironmentSecretResolver({})))
    try:
        assert pipeline.reasoning_adapter() is None
        assert pipeline._resolve_adapter(None) is None
    finally:
        set_secret_store(previous)

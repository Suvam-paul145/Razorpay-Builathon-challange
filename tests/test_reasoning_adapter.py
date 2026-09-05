"""The reasoning adapter's four gates, with no socket opened anywhere in this file.

``httpx.MockTransport`` is injected through the adapter's constructor, which is why the
adapter takes a ``transport`` argument at all — a monkeypatch would leave the production
path untested and this one approximately tested.

Two assertions recur, and they are the two the pipeline depends on:

* **nothing raises.** Every path returns one of the five results, so a caller never needs a
  ``try``. A reasoning failure must not be the reason a processing step crashes, because
  the step's deterministic path works without any of this.
* **the handler's call count.** For the absent credential and for a payload holding an
  undeclared field, the claim is not "the field was removed" but "no request was issued",
  and the only externally visible evidence of that is that the transport was never reached.

The gated property tests for reasoning authority belong to task 49.5. These are the unit
checks for the transport itself.
"""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from functools import partial

import httpx
import pytest

from revora.domain.enums import ReasoningCallKind, RiskCause
from revora.platform.config import Configuration, default_configuration
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.providers.payment_link import validate_description
from revora.reasoning.adapter import (
    API_KEY_HEADER,
    CONTENT_REJECTED,
    PROMPT_CONTRACT_VIOLATION,
    TRANSPORT_SECURITY_FAILED,
    TRUNCATION_MARKER,
    Accepted,
    ContentRule,
    PromptContractViolationError,
    ReasoningAdapter,
    ReasoningVerdict,
    RejectedContent,
    RejectedSchema,
    TimedOut,
    Unavailable,
    UnavailableReason,
    audit_event_type_for,
    build_request_payload,
    extract_transmitted_payload,
    validate_link_description_content,
    verdict_of,
)
from revora.reasoning.contracts import CAUSE_HYPOTHESIS_CONTRACT, LINK_DESCRIPTION_CONTRACT

pytestmark = pytest.mark.pure

_FAKE_CREDENTIAL = "fake-llm-credential"
_APPROVED_LINK = "https://pay.revora.test/r/abc123"
_FORMATTED_AMOUNT = "\u20b92,000.00"

_VALID_CAUSE_BODY = json.dumps(
    {
        "cause": RiskCause.ABANDONMENT.value,
        "confidence": "0.87",
        "evidence_summary": "customer stated they forgot",
    }
)


def _envelope(text: str, *, model_version: str | None = "gemini-2.5-flash") -> dict[str, object]:
    """The provider's response envelope around one text part."""
    body: dict[str, object] = {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}]
    }
    if model_version is not None:
        body["modelVersion"] = model_version
    return body


def _config(**overrides: str) -> Configuration:
    """The placeholder configuration with named bounds overridden.

    Built from ``as_raw`` and reparsed rather than mutated, so an override goes through the
    same parser a stored row would and a bound expressed wrongly fails here.
    """
    raw = dict(default_configuration().as_raw())
    raw.update(overrides)
    return Configuration.from_values(raw, version="test", strict=True)


@pytest.fixture(autouse=True)
def _fake_credential() -> Iterator[None]:
    """Install a store holding an obvious fake, and restore the real one afterwards.

    The store is a module global, so leaving a fake installed would leak into every test
    that followed. Resolver-backed rather than environment-backed, which is also what keeps
    these tests passing in the deployed reality where ``REVORA_LLM_CREDENTIAL`` is unset.
    """
    resolver = EnvironmentSecretResolver({"REVORA_LLM_CREDENTIAL": _FAKE_CREDENTIAL})
    previous = set_secret_store(SecretStore(resolver))
    yield
    set_secret_store(previous)


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: Configuration | None = None,
) -> ReasoningAdapter:
    """An adapter whose every request is answered by ``handler``. No network."""
    return ReasoningAdapter(
        transport=httpx.MockTransport(handler),
        config=config if config is not None else default_configuration(),
    )


def _responder(
    status: int, body: object, seen: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return handler


def _cause_call(adapter: ReasoningAdapter) -> object:
    return adapter.propose_cause(
        provider_error_code="BAD_REQUEST_ERROR",
        provider_error_reason="payment_failed",
        delay_reason_note="card was declined twice",
    )


def _link_call(adapter: ReasoningAdapter) -> object:
    return adapter.draft_link_description(
        merchant_display_name="Acme",
        payment_amount_formatted=_FORMATTED_AMOUNT,
        currency="INR",
        risk_cause=RiskCause.ABANDONMENT,
        approved_link=_APPROVED_LINK,
        length_validator=partial(validate_description, max_length=300),
    )


# ---------------------------------------------------------------------------
# Gate 1 — the field allow-list
# ---------------------------------------------------------------------------


def test_payload_is_built_from_the_declared_set_only() -> None:
    payload = build_request_payload(
        ReasoningCallKind.CAUSE_HYPOTHESIS,
        {"provider_error_code": "GATEWAY_ERROR", "delay_reason_note": "note"},
        delay_note_limit=500,
    )

    assert frozenset(payload) <= CAUSE_HYPOTHESIS_CONTRACT.fields
    # A declared field with no value is omitted rather than transmitted as null.
    assert "provider_error_step" not in payload


def test_undeclared_field_raises_rather_than_being_dropped() -> None:
    with pytest.raises(PromptContractViolationError) as caught:
        build_request_payload(
            ReasoningCallKind.LINK_DESCRIPTION,
            {"merchant_display_name": "Acme", "customer_contact": "+911234567890"},
            delay_note_limit=500,
        )

    assert caught.value.fields == frozenset({"customer_contact"})
    assert caught.value.contract_id == LINK_DESCRIPTION_CONTRACT.contract_id


def test_delay_note_is_truncated_in_the_adapter() -> None:
    payload = build_request_payload(
        ReasoningCallKind.CAUSE_HYPOTHESIS,
        {"delay_reason_note": "x" * 900},
        delay_note_limit=500,
    )

    assert payload["delay_reason_note"] == "x" * 500


def test_transmitted_payload_is_a_subset_of_the_declared_set() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(200, _envelope(_VALID_CAUSE_BODY), seen)) as adapter:
        _cause_call(adapter)

    transmitted = extract_transmitted_payload(seen[0].content)
    assert frozenset(transmitted) <= CAUSE_HYPOTHESIS_CONTRACT.fields


def test_request_carries_the_response_schema_and_the_credential_header() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(200, _envelope(_VALID_CAUSE_BODY), seen)) as adapter:
        _cause_call(adapter)

    body = json.loads(seen[0].content)
    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"]["required"] == [
        "cause",
        "confidence",
        "evidence_summary",
    ]
    assert seen[0].headers[API_KEY_HEADER] == _FAKE_CREDENTIAL
    assert _FAKE_CREDENTIAL not in str(seen[0].url)


# ---------------------------------------------------------------------------
# Gate 2 — TLS with certificate validation
# ---------------------------------------------------------------------------


def test_certificate_failure_abandons_without_retrying() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLCertVerificationError as exc:
            raise httpx.ConnectError("handshake refused", request=request) from exc

    with _adapter(handler, config=_config(REASONING_RETRY_COUNT="3")) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, Unavailable)
    assert result.reason is UnavailableReason.TRANSPORT_SECURITY_FAILED
    assert result.request_issued is False
    assert audit_event_type_for(result) == TRANSPORT_SECURITY_FAILED
    # Not retried: a certificate failure will not resolve inside one processing step.
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Gate 3 — output schema validation, independent of provider enforcement
# ---------------------------------------------------------------------------


def test_valid_response_is_accepted_with_an_exact_confidence() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(200, _envelope(_VALID_CAUSE_BODY), seen)) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, Accepted)
    assert result.output.cause is RiskCause.ABANDONMENT
    assert result.output.confidence == Decimal("0.87")
    assert result.model_version == "gemini-2.5-flash"
    assert verdict_of(result) is ReasoningVerdict.ACCEPTED


def test_schema_failure_retains_the_raw_body_to_the_capture_limit() -> None:
    seen: list[httpx.Request] = []
    drifted = json.dumps({"cause": "NOT_A_CAUSE", "confidence": "0.5"})
    with _adapter(
        _responder(200, _envelope(drifted), seen), config=_config(AI_RAW_CAPTURE_LIMIT="20")
    ) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, RejectedSchema)
    assert verdict_of(result) is ReasoningVerdict.REJECTED_SCHEMA
    assert result.raw_response.endswith(TRUNCATION_MARKER)
    assert len(result.raw_response) == 20 + len(TRUNCATION_MARKER)


def test_confidence_outside_the_range_is_rejected_not_clamped() -> None:
    seen: list[httpx.Request] = []
    body = json.dumps({"cause": "ABANDONMENT", "confidence": "1.4", "evidence_summary": "x"})
    with _adapter(_responder(200, _envelope(body), seen)) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, RejectedSchema)


def test_two_hundred_with_no_text_part_is_a_schema_rejection() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(200, {"candidates": []}, seen)) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, RejectedSchema)
    # The provider answered; retrying would spend the budget and lose the evidence.
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Gate 4 — content validation on LINK_DESCRIPTION
# ---------------------------------------------------------------------------


def _description(text: str) -> object:
    seen: list[httpx.Request] = []
    body = json.dumps({"description": text})
    with _adapter(_responder(200, _envelope(body), seen)) as adapter:
        return _link_call(adapter)


def test_clean_description_passes_every_content_rule() -> None:
    result = _description(f"Complete your payment of {_FORMATTED_AMOUNT} to Acme.")

    assert isinstance(result, Accepted)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Complete your payment to {merchant}.", ContentRule.PLACEHOLDER_UNRESOLVED),
        ("Complete your payment for {order_id}.", ContentRule.PLACEHOLDER_NOT_PERMITTED),
        ("Complete your payment of \u20b92,500.00 to Acme.", ContentRule.AMOUNT_MISMATCH),
        (f"Pay at {_APPROVED_LINK} or https://evil.test/x", ContentRule.UNAPPROVED_LINK),
        (f"Pay at {_APPROVED_LINK} or {_APPROVED_LINK}", ContentRule.MULTIPLE_LINKS),
        # Control characters are the rule ``validate_description`` holds alone. A blank or
        # over-long draft never reaches gate 4 at all: gate 3 bounds the description by the
        # same ``MAX_MESSAGE_LENGTH`` and refuses a blank one, so those two land in
        # ``RejectedSchema``. The overlap is deliberate — two gates enforcing one bound — and
        # this is the one rule only the injected validator knows.
        ("Pay now\u0007 to Acme.", ContentRule.DESCRIPTION_REFUSED),
    ],
)
def test_content_rules_name_the_rule_they_violated(text: str, expected: ContentRule) -> None:
    result = _description(text)

    assert isinstance(result, RejectedContent)
    assert result.rule is expected
    assert result.draft.startswith(text[:10])
    assert verdict_of(result) is ReasoningVerdict.REJECTED_CONTENT
    assert audit_event_type_for(result) == CONTENT_REJECTED


@pytest.mark.parametrize("text", ["   ", "x" * 400])
def test_blank_or_overlong_description_is_refused_before_gate_four(text: str) -> None:
    """The length bound is enforced twice, and gate 3 gets there first."""
    assert isinstance(_description(text), RejectedSchema)


def test_amount_equality_compares_exact_decimals_not_renderings() -> None:
    # "2000" and "2,000.00" are the same amount, and neither passed through a lossy
    # representation to be compared.
    violation = validate_link_description_content(
        "Please pay INR 2000 today.",
        payment_amount_formatted=_FORMATTED_AMOUNT,
        approved_link=_APPROVED_LINK,
        length_validator=partial(validate_description, max_length=300),
    )

    assert violation is None


def test_a_bare_small_integer_is_not_treated_as_an_amount() -> None:
    violation = validate_link_description_content(
        "Complete your payment within 2 days.",
        payment_amount_formatted=_FORMATTED_AMOUNT,
        approved_link=_APPROVED_LINK,
        length_validator=partial(validate_description, max_length=300),
    )

    assert violation is None


# ---------------------------------------------------------------------------
# The absent credential, and the budget
# ---------------------------------------------------------------------------


def test_absent_credential_issues_no_request_and_waits_for_nothing() -> None:
    seen: list[httpx.Request] = []
    previous = set_secret_store(SecretStore(EnvironmentSecretResolver({})))
    try:
        with _adapter(_responder(200, _envelope(_VALID_CAUSE_BODY), seen)) as adapter:
            started = time.monotonic_ns()
            result = _cause_call(adapter)
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
    finally:
        set_secret_store(previous)

    assert isinstance(result, Unavailable)
    assert result.reason is UnavailableReason.CREDENTIAL_ABSENT
    assert result.request_issued is False
    assert result.waited_ms == 0
    assert verdict_of(result) is ReasoningVerdict.UNAVAILABLE
    assert seen == []
    # Nothing sleeps, polls or backs off on the path that is the deployed reality.
    assert elapsed_ms < 100


def test_undeclared_field_blocks_transmission_before_the_credential_is_read() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(200, _envelope(_VALID_CAUSE_BODY), seen)) as adapter:
        # ``propose_cause`` cannot express this; the payload path can, which is what makes
        # gate 1 a check rather than a claim about the typed wrappers.
        result = adapter._exchange(
            ReasoningCallKind.CAUSE_HYPOTHESIS,
            {"provider_error_code": "GATEWAY_ERROR", "customer_contact": "+911234567890"},
            case_id=None,
        )

    assert isinstance(result, Unavailable)
    assert result.reason is UnavailableReason.PROMPT_CONTRACT_VIOLATION
    assert result.offending_fields == frozenset({"customer_contact"})
    assert result.request_issued is False
    assert audit_event_type_for(result) == PROMPT_CONTRACT_VIOLATION
    assert seen == []


def test_timeout_is_retried_within_the_retry_allowance() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    with _adapter(
        handler, config=_config(REASONING_TIMEOUT="0.05", REASONING_RETRY_COUNT="1")
    ) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, TimedOut)
    assert verdict_of(result) is ReasoningVerdict.TIMEOUT
    # One initial attempt plus REASONING_RETRY_COUNT additional ones, and no more.
    assert result.attempts == 2
    assert len(seen) == 2


def test_the_budget_stops_the_retry_loop_before_the_allowance_does() -> None:
    """The bound R27.C6 states is the wait, not the attempt count."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        time.sleep(0.03)
        raise httpx.ReadTimeout("timed out", request=request)

    with _adapter(
        handler, config=_config(REASONING_TIMEOUT="0.02", REASONING_RETRY_COUNT="9")
    ) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, TimedOut)
    # Nine additional requests were permitted and the budget allowed two attempts, so the
    # clock is what ended the loop. A retry count of nine widens nothing.
    assert result.attempts < 10
    assert len(seen) == result.attempts


def test_provider_refusal_is_not_reported_as_a_timeout() -> None:
    seen: list[httpx.Request] = []
    with _adapter(_responder(401, {"error": {"message": "invalid key"}}, seen)) as adapter:
        result = _cause_call(adapter)

    assert isinstance(result, Unavailable)
    assert result.reason is UnavailableReason.PROVIDER_REFUSED
    assert result.http_status == 401
    # A 401 means the credential is wrong, which no retry fixes.
    assert len(seen) == 1
    assert audit_event_type_for(result) is None


def test_an_unlisted_model_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="not one of the verified models"):
        ReasoningAdapter(model="gemini-1.0-nonexistent")

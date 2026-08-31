"""Every provider outcome becomes one of five results, and none of them raises.

No socket is opened anywhere in this file. ``httpx.MockTransport`` is injected through
the client's constructor, which is why the client takes a ``transport`` argument at all
— a monkeypatch would leave the production code path untested and this one
approximately tested.

The assertion repeated in every test here is the one the execution engine depends on:
**no exception escapes**. A caller that had to wrap a provider call in ``try`` would one
day forget, and the moment it forgot would be a crash between the committed intent and
the recorded result — the one window exactly-once has to survive. So each case checks
both the classification and, implicitly by returning at all, that nothing was raised;
:func:`test_no_classification_path_raises` checks it explicitly across every path at
once.

Two cases are worth reading closely:

* a 200 with a drifted body is **not** ``Success``. Field drift routes to
  reconciliation, never to a confirmed intent holding an id that is not a link id.
* a connect error is retried exactly once and a read timeout is never retried. The
  attempt count is asserted, because that is the only externally visible evidence of
  the rule.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from datetime import timedelta

import httpx
import pytest
from hypothesis import given

from revora.domain.payment_event import PaymentStatus
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.providers.classification import (
    CREDENTIAL_UNAVAILABLE_CODE,
    CallPhase,
    ClientError,
    EffectCertainty,
    ResultSource,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    effect_certainty,
)
from revora.providers.razorpay import (
    CONNECT_ATTEMPT_LIMIT,
    MAX_CONNECT_TIMEOUT,
    OPERATION_CREATE_PAYMENT_LINK,
    OPERATION_FETCH_PAYMENT,
    OPERATION_FIND_PAYMENT_LINKS,
    RazorpayClient,
    split_timeout,
)
from tests.fakes.razorpay import (
    CreateOutcome,
    FakeRazorpay,
    ProviderBehaviour,
    as_provider_client,
    provider_behaviour,
)

pytestmark = pytest.mark.pure

_KEY_ID = "rzp_test_fake_key_id"
_KEY_SECRET = "fake_key_secret"

_VALID_LINK = {
    "id": "plink_FAKE0000000001",
    "short_url": "https://fake.invalid/plink/abc123",
    "status": "created",
    "reference_id": "rv_0123456789abcdef",
    "amount": 200_000,
    "amount_paid": 0,
    "currency": "INR",
    "description": "Complete your payment",
    "expire_by": 1_760_000_000,
}

_VALID_PAYMENT = {
    "id": "pay_FAKE0000000001",
    "status": "captured",
    "captured": True,
    "amount": 200_000,
    "amount_refunded": 0,
    "currency": "INR",
    "method": "card",
}


@pytest.fixture(autouse=True)
def _fake_credentials() -> Iterator[None]:
    """Install a resolver holding fake credentials, and restore the real store after.

    The store is a module global, so leaving a fake one installed would leak into every
    test that followed. Values are obvious fakes; the point of the fixture is that the
    auth path is exercised, not that a key is realistic.
    """
    resolver = EnvironmentSecretResolver(
        {"REVORA_RAZORPAY_KEY_ID": _KEY_ID, "REVORA_RAZORPAY_KEY_SECRET": _KEY_SECRET}
    )
    previous = set_secret_store(SecretStore(resolver))
    yield
    set_secret_store(previous)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> RazorpayClient:
    """A client whose every request is answered by ``handler``. No network."""
    return RazorpayClient(transport=httpx.MockTransport(handler), base_url="https://fake.invalid")


def _responder(status: int, body: object, *, text: str | None = None):
    """A handler returning one fixed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return handler


def _raiser(exception: BaseException, calls: list[httpx.Request]):
    """A handler that always raises, recording each attempt so retries are countable."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise exception

    return handler


def _request():
    from datetime import UTC, datetime

    from revora.domain.actions import CandidateAction
    from revora.domain.money import Minor
    from revora.providers.payment_link import CustomerContact, build_payment_link_request

    moment = datetime(2025, 1, 1, tzinfo=UTC)
    return build_payment_link_request(
        case_id="8f14e45f-ea0d-4c2b-9f7e-000000000001",
        action=CandidateAction.PAYMENT_LINK,
        attempt_ordinal=1,
        amount=Minor(200_000),
        currency="INR",
        description="Complete your payment",
        customer=CustomerContact(contact="+919876500000", email="buyer@example.com"),
        window_end=moment + timedelta(hours=168),
        now=moment,
        max_message_length=300,
    )


# ---------------------------------------------------------------------------
# The five classifications
# ---------------------------------------------------------------------------


def test_valid_two_hundred_is_success() -> None:
    result = _client(_responder(200, _VALID_LINK)).create_payment_link(_request())

    assert isinstance(result, Success)
    assert result.entity.id == "plink_FAKE0000000001"
    assert result.entity.status == "created"
    assert effect_certainty(result) is EffectCertainty.EXISTS


@pytest.mark.parametrize(
    ("drift", "why"),
    [
        ({"short_url": None}, "a link with no URL is not payable"),
        ({"id": "order_FAKE01"}, "an id that is not a payment link id is not a payment link"),
        ({"status": "brand_new_status"}, "an unverified status value is field drift"),
        ({"short_url": "   "}, "a blank URL is a link nobody can pay"),
    ],
)
def test_two_hundred_with_drifted_body_is_unclassifiable_not_success(
    drift: dict[str, object], why: str
) -> None:
    """A 200 with an unexpected shape is not success. This is the whole point of the
    Pydantic models: drift must route to reconciliation, never to a confirmed intent."""
    body = {**_VALID_LINK, **drift}

    result = _client(_responder(200, body)).create_payment_link(_request())

    assert isinstance(result, Unclassifiable), why
    assert effect_certainty(result) is EffectCertainty.UNKNOWN
    assert result.http_status == 200
    assert result.raw, "the body is retained as evidence for diagnosis"


def test_missing_required_field_is_unclassifiable() -> None:
    body = {key: value for key, value in _VALID_LINK.items() if key != "short_url"}

    result = _client(_responder(200, body)).create_payment_link(_request())

    assert isinstance(result, Unclassifiable)


def test_malformed_json_is_unclassifiable() -> None:
    result = _client(
        _responder(200, None, text='{"id": "plink_1", "short_url"')
    ).create_payment_link(_request())

    assert isinstance(result, Unclassifiable)
    assert result.detail == "body is not valid JSON"


def test_four_xx_with_error_object_is_client_error() -> None:
    body = {
        "error": {
            "code": "BAD_REQUEST_ERROR",
            "description": "The amount must be at least INR 1.00",
            "reason": "input_validation_failed",
            "source": "business",
            "step": "payment_initiation",
        }
    }

    result = _client(_responder(400, body)).create_payment_link(_request())

    assert isinstance(result, ClientError)
    assert result.code == "BAD_REQUEST_ERROR"
    assert result.reason == "input_validation_failed"
    assert result.description == "The amount must be at least INR 1.00"
    assert result.http_status == 400
    assert result.source is ResultSource.PROVIDER
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST


def test_four_xx_without_parseable_error_object_is_unclassifiable() -> None:
    """The design's qualifier — "4xx *with a parseable error object*" — is load-bearing.

    An HTML page from an intermediary says nothing about whether the provider ever saw
    the request, so it must not be recorded as a definitive rejection carrying a code
    nobody sent."""
    result = _client(
        _responder(403, None, text="<html><body>Forbidden</body></html>")
    ).create_payment_link(_request())

    assert isinstance(result, Unclassifiable)
    assert result.detail == "4xx without a parseable error object"
    assert effect_certainty(result) is EffectCertainty.UNKNOWN


def test_five_xx_is_server_error() -> None:
    result = _client(
        _responder(500, {"error": {"code": "SERVER_ERROR"}})
    ).create_payment_link(_request())

    assert isinstance(result, ServerError)
    assert result.http_status == 500
    assert effect_certainty(result) is EffectCertainty.UNKNOWN


def test_read_timeout_is_uncertain_and_never_retried() -> None:
    """The single most important rule in the client.

    A read timeout means the request reached the server and may have been processed.
    Retrying it is how one payment link becomes two, so the attempt count must be one."""
    attempts: list[httpx.Request] = []

    result = _client(_raiser(httpx.ReadTimeout("read timed out"), attempts)).create_payment_link(
        _request()
    )

    assert isinstance(result, Timeout)
    assert result.phase is CallPhase.AFTER_SEND
    assert result.attempts == 1
    assert len(attempts) == 1, "a read timeout must never be retried"
    assert effect_certainty(result) is EffectCertainty.UNKNOWN


def test_connect_error_is_definitive_and_retried_exactly_once() -> None:
    """The only network failure treated as definitive, because no bytes reached the
    server — and therefore the only one that may be retried."""
    attempts: list[httpx.Request] = []

    result = _client(_raiser(httpx.ConnectError("refused"), attempts)).create_payment_link(
        _request()
    )

    assert isinstance(result, Timeout)
    assert result.phase is CallPhase.CONNECT
    assert result.attempts == CONNECT_ATTEMPT_LIMIT == 2
    assert len(attempts) == 2, "exactly one retry, no more"
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST


def test_connect_error_that_clears_on_the_retry_succeeds() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        return httpx.Response(200, json=_VALID_LINK)

    result = _client(handler).create_payment_link(_request())

    assert isinstance(result, Success)
    assert len(attempts) == 2


def test_pool_timeout_is_definitive_and_not_retried() -> None:
    """No connection was obtained, so nothing was sent — but a second immediate attempt
    would compete with the calls that saturated the pool, so there is no retry."""
    attempts: list[httpx.Request] = []

    result = _client(_raiser(httpx.PoolTimeout("no connection"), attempts)).create_payment_link(
        _request()
    )

    assert isinstance(result, Timeout)
    assert result.phase is CallPhase.NOT_SENT
    assert len(attempts) == 1
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST


def test_unanticipated_transport_failure_is_unclassifiable_not_an_exception() -> None:
    """The catch-all. An escaping exception here would crash the execution path at the
    one moment the system cannot afford to lose track of an attempt."""
    attempts: list[httpx.Request] = []

    result = _client(_raiser(RuntimeError("transport bug"), attempts)).create_payment_link(
        _request()
    )

    assert isinstance(result, Unclassifiable)
    assert result.detail == "unanticipated transport failure"


def test_three_xx_is_unclassifiable() -> None:
    """Redirects are not followed: replaying an authenticated POST at an unverified
    location is worse than reporting that we do not understand the response."""
    result = _client(_responder(302, None, text="")).create_payment_link(_request())

    assert isinstance(result, Unclassifiable)


def test_no_classification_path_raises() -> None:
    """Every failure mode, in one place, asserting the contract explicitly.

    A caller must never need a ``try`` block around a provider call, so this walks the
    whole catalogue and fails if any of them raises rather than classifying."""
    handlers: list[Callable[[httpx.Request], httpx.Response]] = [
        _responder(200, _VALID_LINK),
        _responder(200, {"nonsense": True}),
        _responder(200, None, text="not json at all"),
        _responder(400, {"error": {"code": "BAD_REQUEST_ERROR"}}),
        _responder(401, None, text="<html/>"),
        _responder(500, {"error": {"code": "SERVER_ERROR"}}),
        _responder(302, None, text=""),
        _raiser(httpx.ReadTimeout("x"), []),
        _raiser(httpx.ConnectError("x"), []),
        _raiser(httpx.PoolTimeout("x"), []),
        _raiser(httpx.WriteTimeout("x"), []),
        _raiser(httpx.RemoteProtocolError("x"), []),
        _raiser(RuntimeError("x"), []),
    ]

    for handler in handlers:
        client = _client(handler)
        # Not wrapped in pytest.raises-anything: if any of these raises, the test fails
        # with that exception, which is exactly the report we want.
        assert client.create_payment_link(_request()) is not None
        assert client.find_payment_links_by_reference_id("rv_0123456789abcdef") is not None
        assert client.fetch_payment("pay_FAKE0000000001") is not None


# ---------------------------------------------------------------------------
# Credentials and the refusal path
# ---------------------------------------------------------------------------


def test_missing_credential_refuses_without_making_a_call() -> None:
    """R17.C4: refuse the call, change no state, audit ``CREDENTIAL_UNAVAILABLE``.

    The zero-call assertion is the load-bearing half. A refusal that still opened a
    connection would be a refusal in name only."""
    set_secret_store(SecretStore(EnvironmentSecretResolver({})))
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        attempts.append(request)
        return httpx.Response(200, json=_VALID_LINK)

    result = _client(handler).create_payment_link(_request())

    assert isinstance(result, ClientError)
    assert result.code == CREDENTIAL_UNAVAILABLE_CODE
    assert result.is_local_refusal
    assert result.audit_event_type == "CREDENTIAL_UNAVAILABLE"
    assert result.reason == "razorpay_key_id"
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST
    assert attempts == [], "no request may be issued when a credential will not resolve"


def test_basic_auth_header_is_built_from_the_secret_store_at_call_time() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=_VALID_LINK)

    _client(handler).create_payment_link(_request())

    expected = base64.b64encode(f"{_KEY_ID}:{_KEY_SECRET}".encode()).decode("ascii")
    assert seen == [f"Basic {expected}"]


def test_client_repr_holds_no_credential() -> None:
    """Credentials are resolved per call and never stored on the instance, so there is
    nothing here to redact — and this test is what keeps that true."""
    text = repr(_client(_responder(200, _VALID_LINK)))

    assert _KEY_SECRET not in text
    assert _KEY_ID not in text


# ---------------------------------------------------------------------------
# The reconciliation read
# ---------------------------------------------------------------------------


def test_empty_listing_is_a_successful_answer_not_a_failure() -> None:
    """"No such link" is a real, load-bearing answer, and it must be distinguishable
    from "we could not read the response"."""
    result = _client(_responder(200, {"count": 0, "items": []})).find_payment_links_by_reference_id(
        "rv_0123456789abcdef"
    )

    assert isinstance(result, Success)
    assert result.entity.links == ()
    assert result.entity.exists is False


def test_listing_finds_the_link_under_an_unnamed_envelope_key() -> None:
    """The collection envelope field name is not in the design's verified surface, so
    the client locates the entity list without assuming a name."""
    result = _client(
        _responder(200, {"count": 1, "payment_links": [_VALID_LINK]})
    ).find_payment_links_by_reference_id("rv_0123456789abcdef")

    assert isinstance(result, Success)
    assert result.entity.exists is True
    assert result.entity.first is not None
    assert result.entity.first.id == "plink_FAKE0000000001"


def test_listing_with_no_locatable_entity_list_is_unclassifiable() -> None:
    """The distinction this test exists for: an envelope we do not understand must not
    be read as "empty". Doing so would let reconciliation mark an intent ``FAILED``
    while a customer holds a payable link."""
    result = _client(_responder(200, {"count": 0})).find_payment_links_by_reference_id(
        "rv_0123456789abcdef"
    )

    assert isinstance(result, Unclassifiable)
    assert effect_certainty(result) is EffectCertainty.UNKNOWN


def test_listing_sends_the_reference_id_as_a_query_parameter() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"count": 0, "items": []})

    _client(handler).find_payment_links_by_reference_id("rv_0123456789abcdef")

    assert seen == ["https://fake.invalid/v1/payment_links?reference_id=rv_0123456789abcdef"]


# ---------------------------------------------------------------------------
# The authoritative payment read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(member.value for member in PaymentStatus))
def test_every_verified_payment_status_parses(status: str) -> None:
    body = {**_VALID_PAYMENT, "status": status, "captured": status == "captured"}

    result = _client(_responder(200, body)).fetch_payment("pay_FAKE0000000001")

    assert isinstance(result, Success)
    assert result.entity.status == status


def test_authorized_without_capture_is_not_reported_as_captured() -> None:
    """``authorized`` alone is not recovery: the money has not moved. The client does not
    decide that, but it must not lose the field the decision rests on."""
    body = {**_VALID_PAYMENT, "status": "authorized", "captured": False}

    result = _client(_responder(200, body)).fetch_payment("pay_FAKE0000000001")

    assert isinstance(result, Success)
    assert result.entity.captured is False


def test_unverified_payment_status_is_unclassifiable() -> None:
    body = {**_VALID_PAYMENT, "status": "settled"}

    result = _client(_responder(200, body)).fetch_payment("pay_FAKE0000000001")

    assert isinstance(result, Unclassifiable)


def test_fetch_payment_uses_the_verified_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_VALID_PAYMENT)

    _client(handler).fetch_payment("pay_FAKE0000000001")

    assert seen == ["/v1/payments/pay_FAKE0000000001"]


# ---------------------------------------------------------------------------
# Timeout budget and the request body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [2, 5, 15, 30, 120])
def test_connect_and_read_budgets_sum_to_the_call_timeout(seconds: int) -> None:
    total = timedelta(seconds=seconds)

    connect_ms, read_ms = split_timeout(total)

    assert connect_ms + read_ms == int(total.total_seconds() * 1_000)
    assert connect_ms >= 1
    assert read_ms >= 1
    assert connect_ms <= int(MAX_CONNECT_TIMEOUT.total_seconds() * 1_000)


def test_concurrency_cap_refuses_definitively_without_issuing_a_request() -> None:
    """The self-imposed cap, exercised by re-entering the client from inside a handler.

    A refusal for saturation is definitive in the same way a connect failure is: the
    request was never issued. The caller should let the job queue re-present the work
    rather than treat it as a provider rejection, and the certainty is what tells it so.

    The cap is an [ASSUMPTION] — no published rate limit was found for these endpoints —
    but the *behaviour* under it is not, so it is tested.
    """
    inner: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not inner:
            inner.append(client.fetch_payment("pay_FAKE0000000001"))
        return httpx.Response(200, json=_VALID_LINK)

    client = RazorpayClient(
        transport=httpx.MockTransport(handler),
        base_url="https://fake.invalid",
        max_concurrent_calls=1,
        call_timeout=timedelta(milliseconds=50),
    )

    outer = client.create_payment_link(_request())

    assert isinstance(outer, Success), "the call holding the only permit still completes"
    refused = inner[0]
    assert isinstance(refused, ClientError)
    assert refused.code == "CONCURRENCY_CAP_REJECTED"
    assert refused.is_local_refusal
    assert refused.audit_event_type is None, "saturation is not a credential incident"
    assert effect_certainty(refused) is EffectCertainty.DOES_NOT_EXIST


def test_created_request_body_is_the_verified_field_set() -> None:
    """What actually goes on the wire, asserted at the transport rather than at the
    builder — the two could drift, and only this end matters to the provider."""
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_VALID_LINK)

    _client(handler).create_payment_link(_request())

    assert set(bodies[0]) == {
        "amount",
        "currency",
        "description",
        "reference_id",
        "customer",
        "notify",
        "reminder_enable",
        "expire_by",
        "accept_partial",
        "notes",
    }
    assert bodies[0]["accept_partial"] is False
    # Property 9: provider reminders are customer-visible messages that
    # MAX_CUSTOMER_MESSAGES does not count. This must reach the wire as false.
    assert bodies[0]["reminder_enable"] is False
    assert isinstance(bodies[0]["amount"], int)


# ---------------------------------------------------------------------------
# The fake, against the design's Fake Providers table
# ---------------------------------------------------------------------------
#
# The fake is test infrastructure, which is exactly why it is tested: task 20's
# Property 3 assertions are only as trustworthy as the fake they run against. A fake
# that quietly failed to create the effect on TIMEOUT_EFFECT_CREATED would make the
# critical case pass for the wrong reason.


def test_fake_satisfies_the_client_protocol() -> None:
    client = as_provider_client(FakeRazorpay())

    assert client.fetch_payment("pay_1") is not None


def test_fake_success_creates_a_findable_link() -> None:
    fake = FakeRazorpay()
    request = _request()

    created = fake.create_payment_link(request)
    found = fake.find_payment_links_by_reference_id(request.reference_id)

    assert isinstance(created, Success)
    assert isinstance(found, Success)
    assert found.entity.exists is True
    assert found.entity.first is not None
    assert found.entity.first.id == created.entity.id


def test_fake_timeout_with_effect_created_is_the_critical_case() -> None:
    """The caller sees an uncertain timeout; the link exists. Reconciliation must find it
    by ``reference_id`` and confirm, and must never issue a second create."""
    fake = FakeRazorpay(ProviderBehaviour.timeout_with_effect_created())
    request = _request()

    result = fake.create_payment_link(request)

    assert isinstance(result, Timeout)
    assert result.phase is CallPhase.AFTER_SEND
    assert effect_certainty(result) is EffectCertainty.UNKNOWN
    assert fake.created_link_exists(request.reference_id) is True

    reconciled = fake.find_payment_links_by_reference_id(request.reference_id)
    assert isinstance(reconciled, Success)
    assert reconciled.entity.exists is True
    assert fake.create_call_count_for(request.reference_id) == 1


def test_fake_timeout_with_no_effect_is_indistinguishable_to_the_caller() -> None:
    """Same classification, opposite ground truth. This pair is what Property 3 lives on."""
    fake = FakeRazorpay(ProviderBehaviour.timeout_with_no_effect())
    request = _request()

    result = fake.create_payment_link(request)

    assert isinstance(result, Timeout)
    assert result.phase is CallPhase.AFTER_SEND
    assert fake.created_link_exists(request.reference_id) is False

    reconciled = fake.find_payment_links_by_reference_id(request.reference_id)
    assert isinstance(reconciled, Success)
    assert reconciled.entity.exists is False


def test_fake_listing_is_empty_then_non_empty() -> None:
    """The read-after-write lag the design marks [EVIDENCE INSUFFICIENT], and the reason
    reconciliation may treat an empty result as failure only on its final attempt."""
    fake = FakeRazorpay(ProviderBehaviour.listing_lag(empty_reads=2))
    request = _request()
    fake.create_payment_link(request)

    first = fake.find_payment_links_by_reference_id(request.reference_id)
    second = fake.find_payment_links_by_reference_id(request.reference_id)
    third = fake.find_payment_links_by_reference_id(request.reference_id)

    assert isinstance(first, Success) and first.entity.exists is False
    assert isinstance(second, Success) and second.entity.exists is False
    assert isinstance(third, Success) and third.entity.exists is True


def test_fake_covers_every_create_outcome() -> None:
    expected: dict[CreateOutcome, type] = {
        CreateOutcome.SUCCESS: Success,
        CreateOutcome.CLIENT_ERROR: ClientError,
        CreateOutcome.SERVER_ERROR: ServerError,
        CreateOutcome.TIMEOUT_EFFECT_CREATED: Timeout,
        CreateOutcome.TIMEOUT_NO_EFFECT: Timeout,
        CreateOutcome.CONNECT_ERROR: Timeout,
        CreateOutcome.UNCLASSIFIABLE: Unclassifiable,
    }
    assert set(expected) == set(CreateOutcome), "every outcome must be covered"

    for outcome, variant in expected.items():
        fake = FakeRazorpay(ProviderBehaviour(create_outcomes=(outcome,)))
        assert isinstance(fake.create_payment_link(_request()), variant), outcome


def test_fake_connect_error_creates_nothing_and_is_definitive() -> None:
    fake = FakeRazorpay(ProviderBehaviour(create_outcomes=(CreateOutcome.CONNECT_ERROR,)))
    request = _request()

    result = fake.create_payment_link(request)

    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST
    assert fake.created_link_exists(request.reference_id) is False


@pytest.mark.parametrize("status", list(PaymentStatus))
def test_fake_serves_each_of_the_five_payment_statuses(status: PaymentStatus) -> None:
    fake = FakeRazorpay(ProviderBehaviour.payment_status(status))

    result = fake.fetch_payment("pay_FAKE0000000001")

    assert isinstance(result, Success)
    assert result.entity.status == status.value
    assert result.entity.captured is (status is PaymentStatus.CAPTURED)


def test_fake_read_can_disagree_with_a_success_webhook() -> None:
    """R10.C13's conflict path: a webhook claimed success, the authoritative read says
    the payment failed. The read wins, so no recovery may be declared."""
    fake = FakeRazorpay(ProviderBehaviour.read_disagreeing_with_success_webhook())

    result = fake.fetch_payment("pay_FAKE0000000001")

    assert isinstance(result, Success)
    assert result.entity.captured is False
    assert result.entity.status == "failed"


def test_fake_serves_n_consecutive_unavailable_reads_then_answers() -> None:
    fake = FakeRazorpay(ProviderBehaviour.unavailable_payment_reads(3))

    results = [fake.fetch_payment("pay_FAKE0000000001") for _ in range(4)]

    assert [isinstance(result, ServerError) for result in results] == [True, True, True, False]
    assert isinstance(results[3], Success)


def test_fake_delivers_delayed_success_after_a_terminal_state() -> None:
    """R10.C14: recovery arriving after the case reached a terminal state."""
    fake = FakeRazorpay(ProviderBehaviour.delayed_success(failed_reads=2))

    statuses = [fake.fetch_payment("pay_FAKE0000000001") for _ in range(3)]

    assert [result.entity.status for result in statuses if isinstance(result, Success)] == [
        "failed",
        "failed",
        "captured",
    ]


def test_fake_records_every_call_so_zero_calls_can_be_asserted() -> None:
    fake = FakeRazorpay()
    assert fake.calls == (), "a fresh fake has made no calls"

    request = _request()
    fake.create_payment_link(request)
    fake.find_payment_links_by_reference_id(request.reference_id)
    fake.fetch_payment("pay_FAKE0000000001")

    assert [call.operation for call in fake.calls] == [
        OPERATION_CREATE_PAYMENT_LINK,
        OPERATION_FIND_PAYMENT_LINKS,
        OPERATION_FETCH_PAYMENT,
    ]
    assert [call.sequence for call in fake.calls] == [1, 2, 3]
    assert fake.calls_for(OPERATION_FETCH_PAYMENT)[0].arguments == {
        "provider_payment_id": "pay_FAKE0000000001"
    }


def test_fake_call_log_holds_no_customer_contact() -> None:
    """A test fixture is still somewhere a PII habit can form, and recorded arguments are
    the part of a fake most likely to be printed on a failure."""
    fake = FakeRazorpay()
    fake.create_payment_link(_request())

    rendered = repr(fake.calls)
    assert "+919876500000" not in rendered
    assert "buyer@example.com" not in rendered


def test_fake_counts_creates_per_reference_id() -> None:
    """The counter task 20's Property 3 asserts against: at most one create per key."""
    fake = FakeRazorpay()
    request = _request()

    assert fake.create_call_count_for(request.reference_id) == 0
    fake.create_payment_link(request)
    fake.create_payment_link(request)

    assert fake.create_call_count_for(request.reference_id) == 2
    assert fake.create_call_count_for("rv_never_used_00000") == 0


def test_fake_create_outcomes_are_consumed_in_order_with_the_last_repeating() -> None:
    fake = FakeRazorpay(
        ProviderBehaviour(
            create_outcomes=(CreateOutcome.SERVER_ERROR, CreateOutcome.SUCCESS),
        )
    )
    request = _request()

    results = [fake.create_payment_link(request) for _ in range(3)]

    assert isinstance(results[0], ServerError)
    assert isinstance(results[1], Success)
    assert isinstance(results[2], Success)


def test_fake_listing_unavailability_does_not_consume_the_lag_budget() -> None:
    """Order matters: an unavailable read is a read that did not happen."""
    fake = FakeRazorpay(
        ProviderBehaviour(
            create_outcomes=(CreateOutcome.TIMEOUT_EFFECT_CREATED,),
            listing_unavailable_reads=1,
            listing_empty_reads=1,
        )
    )
    request = _request()
    fake.create_payment_link(request)

    first = fake.find_payment_links_by_reference_id(request.reference_id)
    second = fake.find_payment_links_by_reference_id(request.reference_id)
    third = fake.find_payment_links_by_reference_id(request.reference_id)

    assert isinstance(first, ServerError)
    assert isinstance(second, Success) and second.entity.exists is False
    assert isinstance(third, Success) and third.entity.exists is True


@given(behaviour=provider_behaviour())
def test_scripted_behaviour_never_makes_the_fake_raise(behaviour: ProviderBehaviour) -> None:
    """The fake must honour the same no-exception contract as the real client, or a
    property test would fail on the fake's behaviour rather than on the system's."""
    fake = FakeRazorpay(behaviour)
    request = _request()

    for _ in range(4):
        assert fake.create_payment_link(request) is not None
        assert fake.find_payment_links_by_reference_id(request.reference_id) is not None
        assert fake.fetch_payment("pay_FAKE0000000001") is not None

"""The resend's own classification table, row by row, through the code that ships.

Nine rows, and the table is the whole test. Every row is asserted at three levels — the
``ProviderResult`` variant, the effect certainty, and the intent state that follows — because a
classification is only correct if the state it produces is correct, and those are three
functions in three modules that a change could put out of step.

**No socket is opened.** ``httpx.MockTransport`` goes in through the client's constructor, so
every row runs through :meth:`RazorpayClient.notify_by`, the shared request path and
:func:`classify_resend_response` in the order production uses them. Calling the classifier
directly would leave the wiring untested, and the wiring is where a per-operation table gets
attached to the wrong operation.

Two rows carry the design decisions and are asserted more than once:

* **429 is definitive for a resend and not for a create.** The same status through the same
  client is ``ClientError``/``FAILED`` on the resend path and ``Unclassifiable``/``UNCERTAIN`` on
  the create path when its body does not parse. That asymmetry is deliberate — a 429 is the
  provider's gateway saying it declined to act — and the pair of assertions is what stops a
  future simplification collapsing the two tables into one.
* **A 200 that is not exactly ``{"success": true}`` is not a success.** Absent, false, and a
  coercible ``"true"`` all land ``UNCERTAIN``, which for a resend means the case escalates to a
  person. That is the expensive direction, chosen because the cheap direction records a customer
  who never got the message as messaged.

The local refusals are here too, and they are the enforcement of a claim made in prose
elsewhere: the Revora-composed ``"<plink_id>#notify_by:<medium>"`` token cannot be fed back to
an endpoint as though it were a provider identifier. That is only true if something refuses it,
so something does, and this asserts that nothing was sent when it did.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from revora.domain.enums import IntentState
from revora.execution.intents import classify_into_intent_state
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.providers.classification import (
    RATE_LIMITED,
    RATE_LIMITED_STATUS,
    ClientError,
    EffectCertainty,
    PaymentLinkResendAck,
    ProviderResult,
    ResultSource,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    classify_resend_response,
    classify_response,
    effect_certainty,
    parse_payment_link,
    parse_payment_link_resend,
)
from revora.providers.payment_link import (
    NotifyMedium,
    PaymentLinkRequestError,
    is_resend_response_id,
    resend_response_id,
)
from revora.providers.razorpay import (
    OPERATION_NOTIFY_BY,
    PATH_PAYMENT_LINK_NOTIFY,
    RazorpayClient,
)

pytestmark = pytest.mark.pure

_KEY_ID = "rzp_test_fake_key_id"
_KEY_SECRET = "fake_key_secret"
_LINK_ID = "plink_FAKE0000000001"

_HTML_BODY = "<html><head><title>429 Too Many Requests</title></head></html>"
"""What an intermediary sends, not what the provider documents. The 429 row must not depend on
the body, and the only way to assert that is to classify one that carries nothing useful."""


@pytest.fixture(autouse=True)
def _fake_credentials() -> Iterator[None]:
    """Fake credentials in a fake store, restored afterwards.

    The store is a module global; leaving a fake one installed would leak into every test that
    followed. The auth path is exercised, which is the point — a resend goes out under the same
    Basic header as a create.
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


def _responder(status: int, text: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return handler


def _raiser(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_ROWS: tuple[tuple[str, int, str, type, EffectCertainty, IntentState], ...] = (
    (
        "200 with success true",
        200,
        '{"success": true}',
        Success,
        EffectCertainty.EXISTS,
        IntentState.CONFIRMED,
    ),
    (
        "200 with success false",
        200,
        '{"success": false}',
        Unclassifiable,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
    (
        "200 with success absent",
        200,
        '{"entity": "payment_link"}',
        Unclassifiable,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
    (
        "200 with a coercible non-boolean",
        200,
        '{"success": "true"}',
        Unclassifiable,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
    (
        "200 with an unparseable body",
        200,
        '{"success": ',
        Unclassifiable,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
    (
        "429 with an unparseable body",
        429,
        _HTML_BODY,
        ClientError,
        EffectCertainty.DOES_NOT_EXIST,
        IntentState.FAILED,
    ),
    (
        "4xx with a parseable provider error",
        400,
        '{"error": {"code": "BAD_REQUEST_ERROR", "description": "not notifiable"}}',
        ClientError,
        EffectCertainty.DOES_NOT_EXIST,
        IntentState.FAILED,
    ),
    (
        "4xx without a parseable error object",
        404,
        _HTML_BODY,
        Unclassifiable,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
    (
        "5xx",
        503,
        '{"error": {"code": "SERVER_ERROR"}}',
        ServerError,
        EffectCertainty.UNKNOWN,
        IntentState.UNCERTAIN,
    ),
)


@pytest.mark.parametrize(
    ("label", "status", "body", "expected", "certainty", "intent_state"),
    _ROWS,
    ids=[row[0] for row in _ROWS],
)
def test_the_resend_table_row_by_row(
    label: str,
    status: int,
    body: str,
    expected: type,
    certainty: EffectCertainty,
    intent_state: IntentState,
) -> None:
    """Each documented outcome becomes one result, one certainty and one intent state.

    Asserted through the client rather than the classifier, so the row proves the resend's table
    is the one actually attached to ``notify_by``.
    """
    with _client(_responder(status, body)) as client:
        result = client.notify_by(_LINK_ID, NotifyMedium.SMS)

    assert isinstance(result, expected), f"{label}: classified {type(result).__name__}"
    assert effect_certainty(result) is certainty, label
    assert classify_into_intent_state(result) is intent_state, label


def test_a_read_timeout_after_sending_is_uncertain_and_never_retried() -> None:
    """The dangerous row. Bytes went out, so the message may have gone out too.

    ``UNCERTAIN`` here is terminal for a resend — there is no read that could settle it — so the
    absence of a retry is not a performance choice, it is the reason a customer does not get the
    same message twice. ``attempts`` is asserted because it is the only externally visible
    evidence that no retry happened.
    """
    with _client(_raiser(httpx.ReadTimeout("read timed out"))) as client:
        result = client.notify_by(_LINK_ID, NotifyMedium.SMS)

    assert isinstance(result, Timeout)
    assert result.attempts == 1, "a post-send timeout must never be retried"
    assert effect_certainty(result) is EffectCertainty.UNKNOWN
    assert classify_into_intent_state(result) is IntentState.UNCERTAIN


def test_a_connect_failure_is_definitive_and_retried_once() -> None:
    """The one network failure that means nothing was sent, so a further attempt is permitted."""
    with _client(_raiser(httpx.ConnectError("refused"))) as client:
        result = client.notify_by(_LINK_ID, NotifyMedium.EMAIL)

    assert isinstance(result, Timeout)
    assert result.attempts == 2, "a connect failure is the only retryable one"
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST
    assert classify_into_intent_state(result) is IntentState.FAILED


# ---------------------------------------------------------------------------
# The 429, which is the one place the resend leaves the generic rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ['{"error": {"code": "RATE_LIMIT_ERROR"}}', _HTML_BODY, "", "null"],
    ids=["provider error object", "html from an intermediary", "empty", "json null"],
)
def test_the_429_classification_does_not_depend_on_the_body(body: str) -> None:
    """Whatever the 429 carries, it is a definitive rejection with Revora's own code.

    The 429 body shape is unverified, and this is what "not depending on it" means as an
    assertion. The code is minted locally and the source stays ``PROVIDER``: the provider really
    did reject the request, so a merchant reading ``RATE_LIMITED`` is reading a fact about the
    provider — what is not read from the provider is the prose.
    """
    result = classify_resend_response(
        http_status=RATE_LIMITED_STATUS, body_text=body, parse=parse_payment_link_resend
    )

    assert isinstance(result, ClientError)
    assert result.code == RATE_LIMITED
    assert result.source is ResultSource.PROVIDER
    assert result.http_status == RATE_LIMITED_STATUS
    assert not result.is_local_refusal
    assert classify_into_intent_state(result) is IntentState.FAILED


def test_the_creates_table_is_unchanged_by_the_resends() -> None:
    """The create's 429 still follows the generic rule. The two tables stay two tables.

    A 429 whose body is not a provider error object is ``Unclassifiable`` for a create, because
    an HTML page from an intermediary says nothing about whether the provider saw the request —
    and a create that may have happened must go to reconciliation. The resend cannot go to
    reconciliation at all, which is why it needs the stronger reading of the same status.
    """
    created = classify_response(
        http_status=RATE_LIMITED_STATUS, body_text=_HTML_BODY, parse=parse_payment_link
    )
    resent = classify_resend_response(
        http_status=RATE_LIMITED_STATUS, body_text=_HTML_BODY, parse=parse_payment_link_resend
    )

    assert isinstance(created, Unclassifiable)
    assert effect_certainty(created) is EffectCertainty.UNKNOWN
    assert isinstance(resent, ClientError)
    assert effect_certainty(resent) is EffectCertainty.DOES_NOT_EXIST


# ---------------------------------------------------------------------------
# The composed identifier, and the refusals that make it un-fetchable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("medium", list(NotifyMedium), ids=[m.value for m in NotifyMedium])
def test_the_composed_token_is_not_a_provider_identifier(medium: NotifyMedium) -> None:
    """It records the link and the medium, and it cannot be mistaken for a provider id.

    The separator is the load-bearing part: no Razorpay identifier contains it, so the token is
    structurally unusable as one rather than merely unlikely to be misread.
    """
    token = resend_response_id(_LINK_ID, medium)

    assert token == f"{_LINK_ID}#notify_by:{medium.value}"
    assert is_resend_response_id(token)
    assert not is_resend_response_id(_LINK_ID)
    # A payment entity id is validated on its prefix, so the guard cannot be the prefix.
    assert token.startswith(_LINK_ID)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "pay_FAKE0000000001", f"{_LINK_ID}#notify_by:sms"],
    ids=["blank", "whitespace", "a payment id", "an already composed token"],
)
def test_composing_refuses_anything_that_is_not_a_link_id(value: str) -> None:
    """Rejected before any call, naming the rule, because the defect is Revora-side."""
    with pytest.raises(PaymentLinkRequestError) as raised:
        resend_response_id(value, NotifyMedium.SMS)

    assert raised.value.rule in {
        "payment_link_id_blank",
        "payment_link_id_composed",
        "payment_link_id_not_a_link_id",
    }


def test_the_client_refuses_a_composed_token_without_sending_anything() -> None:
    """The enforcement of the claim. A stored ``provider_response_id`` fed back in goes nowhere.

    Definitive, and nothing was sent — which is the difference that matters. A request that
    reached the provider and earned a 404 would be a definitive rejection too, but it would be
    one recorded against a case for a call Revora should never have made.
    """
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        sent.append(request)
        return httpx.Response(200, text='{"success": true}')

    with _client(handler) as client:
        result = client.notify_by(resend_response_id(_LINK_ID, NotifyMedium.SMS), NotifyMedium.SMS)

    assert sent == [], "a composed token must never reach the provider"
    assert isinstance(result, ClientError)
    assert result.is_local_refusal
    assert OPERATION_NOTIFY_BY in result.reason
    assert effect_certainty(result) is EffectCertainty.DOES_NOT_EXIST


def test_the_client_refuses_an_identifier_that_is_not_a_payment_link() -> None:
    """A payment id, an order id or a blank string is a Revora-side mistake, caught locally."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        sent.append(request)
        return httpx.Response(200, text='{"success": true}')

    with _client(handler) as client:
        result = client.notify_by("pay_FAKE0000000001", NotifyMedium.EMAIL)

    assert sent == []
    assert isinstance(result, ClientError)
    assert result.is_local_refusal


# ---------------------------------------------------------------------------
# The request itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("medium", list(NotifyMedium), ids=[m.value for m in NotifyMedium])
def test_the_request_goes_to_the_verified_path_with_no_body(medium: NotifyMedium) -> None:
    """``POST /v1/payment_links/:id/notify_by/:medium``, authenticated, with nothing to send.

    The path is compared against the template rather than a hand-written string, so a change to
    the constant cannot leave this test asserting the old shape. The body is asserted empty
    because the endpoint takes none — sending one would be inventing unverified surface.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text='{"success": true}')

    with _client(handler) as client:
        result = client.notify_by(_LINK_ID, medium)

    assert isinstance(result, Success)
    assert result.entity == PaymentLinkResendAck(success=True)
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == PATH_PAYMENT_LINK_NOTIFY.format(
        payment_link_id=_LINK_ID, medium=medium.value
    )
    assert request.content == b""
    assert request.headers["Authorization"].startswith("Basic ")


def test_no_resend_path_raises() -> None:
    """Every row, plus both transports, and nothing escapes as an exception.

    The engine records a resend's outcome between a committed intent and a moved case. An
    exception on that path is a crash in the one window where the system must not lose track of
    an attempt — and for a resend the loss is unrecoverable, because no read can reconstruct it.
    """
    handlers: list[Callable[[httpx.Request], httpx.Response]] = [
        _responder(status, body) for _, status, body, *_ in _ROWS
    ]
    handlers.extend(
        [
            _raiser(httpx.ReadTimeout("read")),
            _raiser(httpx.ConnectError("connect")),
            _raiser(httpx.PoolTimeout("pool")),
            _raiser(httpx.RemoteProtocolError("protocol")),
            _raiser(RuntimeError("something nobody anticipated")),
        ]
    )

    for handler in handlers:
        with _client(handler) as client:
            result: ProviderResult[PaymentLinkResendAck] = client.notify_by(
                _LINK_ID, NotifyMedium.SMS
            )
        assert result is not None

"""Task 54.4. The three new degradation-ladder rows, tested at the rung.

A ladder is only worth writing down if each rung is checked where it claims to hold. Every row
of the design's table is a promise that something breaks and the system keeps working in a
specific, *reduced* way — so almost every assertion here is a **negative**: no case left
waiting, no further external call, no counter moved, no case stranded outside R2.C12's
termination bound. Negatives need a witness, which is why the provider is
:class:`tests.fakes.razorpay.FakeRazorpay` (it logs every call it receives) and why the broken
reasoning layer is an ``httpx.MockTransport`` that either raises or answers rubbish rather than
an absence of configuration.

**Row 1 — the reasoning layer is broken.** Four rungs, one mechanism each: the credential is
absent (the deployed default here, since nothing in ``revora`` loads ``.env``), the transport
times out, the model answers something the output schema refuses, and the per-case call bound of
R27.C13 is already spent. The claim is P51 in its fullest form, the one the property file names
as the thing it cannot reach from the ``model`` tier: *rows*. Four independent runs of the whole
pipeline over a signed webhook must produce the same case state, the same diagnosis, the same
selection, the same execution, the same recovered amount and the same projected timeline —
and the sentence that reached the customer must be the approved template in all four.

**Row 2 — the customer page is unreachable.** The row costs more than it looks, and that is the
most valuable test in this file. With the token signing secret unresolvable, ``PAYMENT_LINK``
and ``CUSTOMER_MESSAGE`` **also stop**, because R18.C13 abandons the execution in its first
transaction rather than sending a message carrying no response-page URL. Asserted as three
absences — no execution-intent row, no counter movement, no provider call — plus the presence
of the ``CUSTOMER_TOKEN_ISSUE_FAILED`` record that keeps the abandonment legible. And the
decision loop is unaffected: a case that chose restraint still gets its ``next_review_at``,
``SCHEDULED_REVIEW`` still re-decides it, and ``EVENT_ATTACHED`` still does, with the signing
secret missing throughout.

**Row 3 — the resend is unavailable.** Two of its three clauses are already asserted, in
``tests/persistence/test_resend_disposition.py`` (the 5xx escalation and the 429 that spends an
increment and returns the case to ``DECISION_PENDING``) and in
``tests/properties/test_promise.py`` (the withdrawn capability, where ``PAYMENT_LINK`` and the
null actions still compete). Neither is duplicated here. The gap those two leave — the *read
timeout*, which is a different classification arriving at the same disposition — is filled
beside them in the file that owns the resend's settlement, not here.

**Cost.** Everything is ``pg`` because everything asserted is a row, and the tier runs on every
push, so there is no demo batch, no thousand-case run and no real sleep anywhere. The reasoning
faults are instantaneous: ``MockTransport`` raises immediately, so the adapter's retry allowance
is spent without waiting on the ten-second budget it is bounded by.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.audit.events import CUSTOMER_TOKEN_ISSUE_FAILED, EVENT_ATTACHED_TO_CASE
from revora.cases.review import sweep_due_reviews
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, DiagnosisMethod, ReasoningCallKind
from revora.domain.transitions import TERMINAL_STATES
from revora.execution.messages import description_for
from revora.jobs import pipeline
from revora.jobs.pipeline import EXECUTION_JOB_KIND, enqueue_next
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.platform.secrets import SecretStore, set_secret_store
from revora.reasoning.adapter import ReasoningAdapter
from tests.fakes.razorpay import FakeRazorpay
from tests.integration.conftest import (
    Tenant,
    case_state,
    deliver,
    drain,
    drive_to_case,
    failed_payment_body,
    registry_without_executor,
    secret_store_with,
    secret_store_without,
)

pytestmark = pytest.mark.pg

_MERCHANT_NAME = "Integration merchant"
"""What ``tests.integration.conftest.tenant`` names the merchant. Restated rather than read
back, because it is the input to the approved template this file compares against, and reading
it from the same row the engine reads it from would let both be wrong together."""

_SIGNING_SECRET = "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS"

_LLM_CREDENTIAL = "fake-llm-credential"
"""An obvious fake. Present only so a reasoning fault other than the absent credential is
reachable at all — the adapter resolves the credential before it touches its transport."""

NULL_ACTION_AMOUNT = 50_000
"""₹500. Below the amount at which any action clears ``MIN_NET_VALUE_THRESHOLD``, so the
optimizer selects a Null_Action with ``NO_POSITIVE_VALUE`` and the case rests at
``POLICY_CHECK`` carrying a ``next_review_at``.

Derived from the same priors ``LINK_PATH_AMOUNT`` is derived from rather than picked: at
``LINK_PATH_AMOUNT`` (₹1,000) a payment link nets ₹70 and is selected, and the null actions win
below roughly ₹700. Naming the amount here rather than forcing restraint by writing a
recommendation row directly is what makes the restraint half of row 2 a claim about the
*pipeline* choosing to wait."""


# ---------------------------------------------------------------------------
# Reading the loop back
# ---------------------------------------------------------------------------


def _rows(engine: Engine, sql: str, params: dict[str, object]) -> list[tuple]:
    with engine.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def _count(engine: Engine, sql: str, params: dict[str, object]) -> int:
    return int(_rows(engine, sql, params)[0][0])


@dataclass(frozen=True, slots=True)
class _LoopOutcome:
    """Everything P51 says two runs must agree on, as one comparable value.

    A dataclass rather than a sequence of separate assertions per rung, because the property is
    an *equality between runs* and four rungs each asserting their own expected values would be
    four chances to write down the same wrong expectation. Compared as a whole so a divergence
    anywhere shows up as a diff rather than as whichever assertion happened to be first.

    ``description`` is the sentence that reached the customer, read through the fake's
    out-of-band oracle. It is in here because "the approved template carries the load" is the
    one clause of the reasoning row that is invisible in every other column: a case that
    recovered on model-authored prose and a case that recovered on the template are otherwise
    identical rows.
    """

    state: str
    terminal_reason: str | None
    diagnosis_method: str
    risk_cause: str
    selected_action: str
    selection_reason: str
    executed_actions: int
    customer_messages: int
    intents: tuple[tuple[str, str], ...]
    create_calls: int
    description: str | None
    ai_explanation: str | None
    timeline: tuple[tuple[str, str], ...]


def _outcome(
    engine: Engine,
    client: TestClient,
    tenant: Tenant,
    case_id: uuid.UUID,
    fake: FakeRazorpay,
) -> _LoopOutcome:
    case = _rows(
        engine,
        "SELECT state, terminal_reason, executed_action_count, customer_message_count "
        "FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )[0]
    diagnosis = _rows(
        engine,
        "SELECT method, cause FROM diagnosis WHERE case_id = :c ORDER BY created_at LIMIT 1",
        {"c": str(case_id)},
    )
    recommendation = _rows(
        engine,
        "SELECT selected_action, selection_reason, ai_explanation_text FROM recommendation "
        "WHERE case_id = :c ORDER BY created_at LIMIT 1",
        {"c": str(case_id)},
    )
    intents = _rows(
        engine,
        "SELECT idempotency_key, state FROM execution_intent WHERE case_id = :c "
        "ORDER BY attempt_ordinal",
        {"c": str(case_id)},
    )
    projected = client.get(f"/cases/{case_id}/timeline", headers=tenant.auth).json()
    assert projected["available"] is True, projected
    stages = projected["timeline"]["stages"]
    keys = [str(key) for key, _ in intents]
    return _LoopOutcome(
        state=str(case[0]),
        terminal_reason=None if case[1] is None else str(case[1]),
        diagnosis_method="" if not diagnosis else str(diagnosis[0][0]),
        risk_cause="" if not diagnosis else str(diagnosis[0][1]),
        selected_action="" if not recommendation else str(recommendation[0][0]),
        selection_reason="" if not recommendation else str(recommendation[0][1]),
        executed_actions=int(case[2]),
        customer_messages=int(case[3]),
        # The idempotency key is derived from the case id, so it cannot be compared across runs.
        # Its *ordinal position and state* can, and that is the part the property is about.
        intents=tuple((f"attempt-{index}", str(state)) for index, (_, state) in enumerate(intents)),
        create_calls=sum(fake.create_call_count_for(key) for key in keys),
        description=next(
            (fake.sent_description_for(key) for key in keys if fake.sent_description_for(key)),
            None,
        ),
        ai_explanation=(
            None
            if not recommendation or recommendation[0][2] is None
            else str(recommendation[0][2])
        ),
        timeline=tuple((str(stage["stage"]), str(stage["status"])) for stage in stages),
    )


# ---------------------------------------------------------------------------
# The four broken reasoning layers
# ---------------------------------------------------------------------------


def _timing_out() -> ReasoningAdapter:
    """A transport that raises ``ReadTimeout`` on every attempt.

    Raised rather than slept, and the difference is the whole reason this test belongs in a tier
    that runs on every push: the adapter's budget is ``REASONING_TIMEOUT`` times
    ``TOTAL_WAIT_MULTIPLE``, and an honest timeout staged by *waiting* would spend twenty
    seconds per invocation site. An immediately raised ``ReadTimeout`` is the same
    classification arriving at the same result, instantly — and it exercises the retry allowance
    for real, because the adapter retries a timeout rather than giving up on the first one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("reasoning transport read timeout", request=request)

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


def _answering_rubbish() -> ReasoningAdapter:
    """A 200 whose body no declared output schema accepts.

    Well-formed at the envelope level and wrong inside it, which is the failure R27.C5 is
    written for: a provider that stopped honouring ``responseSchema`` would look exactly like
    this, and the point of validating independently is that this path is not a crash.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"nothing": "the schema knows"}'}]}}
                ],
                "modelVersion": "gemini-2.5-flash-001",
            },
        )

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


def _exploding(record: list[str]) -> ReasoningAdapter:
    """An adapter that appends to ``record`` if it is ever asked for anything.

    The witness for the bound rung. An adapter that merely returned nothing would let the bound
    stop being checked without anybody noticing, because the deterministic outcome is identical
    either way — which is exactly how a short-circuit stops short-circuiting silently.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        record.append(str(request.url))
        raise AssertionError("unreachable")

    return ReasoningAdapter(transport=httpx.MockTransport(handler))


@contextmanager
def _installed(store: SecretStore) -> Iterator[None]:
    """Swap the process secret store for the duration of a drain, then put it back.

    Safe mid-test because neither credential this file removes is cached: the token signing
    secrets are resolved on every mint and every verification (a cache there would make a
    rotation take a restart), and the payload cipher's material is unchanged, so nothing needs
    invalidating.
    """
    previous = set_secret_store(store)
    try:
        yield
    finally:
        set_secret_store(previous)


@contextmanager
def _reasoning(adapter: ReasoningAdapter | None) -> Iterator[None]:
    """Substitute the process-wide adapter the pipeline resolves for itself.

    The handlers take ``reasoning`` as an optional argument, but the worker registry does not
    pass one — production resolves it from the module, which is the path that has to be under
    test here, because "the loop still works" is a claim about the worker draining a queue and
    not about a handler called with a convenient argument. Patching the resolver leaves that
    path intact and substitutes only what it resolves.
    """
    previous = pipeline.reasoning_adapter
    pipeline.reasoning_adapter = lambda: adapter  # type: ignore[assignment]
    try:
        yield
    finally:
        pipeline.reasoning_adapter = previous  # type: ignore[assignment]


def _spend_the_reasoning_bound(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> int:
    """Commit ``reasoning_call_bound`` invocation rows for one case. Returns the bound.

    Inserted directly, which is the point: nothing in this process has counted anything, so a
    bound held in memory would read zero. R27.C13 counts committed rows precisely so a crash
    loop cannot reset the allowance, and this is what that looks like from the outside.
    """
    bound = pipeline.reasoning_call_bound(default_configuration())
    with engine.begin() as connection:
        for _ in range(bound):
            connection.execute(
                text(
                    """
                    INSERT INTO ai_invocation (
                        id, merchant_id, case_id, call_kind, prompt_contract_id,
                        verdict, influenced_recommendation, created_at
                    ) VALUES (:id, :m, :c, :kind, 'link-description/1', 'TIMEOUT', false, now())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "kind": ReasoningCallKind.LINK_DESCRIPTION.value,
                },
            )
    return bound


# ---------------------------------------------------------------------------
# Row 1 — the reasoning layer is broken
# ---------------------------------------------------------------------------


def test_four_broken_reasoning_layers_produce_one_identical_loop(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Row 1, and P51 as a claim about rows rather than about pure functions.

    Three faults plus the baseline, each driven through the whole pipeline from a signed webhook
    to a recovered case, and every one of them must land on the *same* :class:`_LoopOutcome`.
    The baseline is the absent credential, which is not a contrivance: nothing in ``revora``
    loads ``.env``, so it is the path this deployment actually takes, and the contrast is
    therefore staged by *adding* a credential rather than by removing one.

    Four assertions carry the row, and three of them are negative:

    1. **Every rung agrees with the baseline, field for field.** State, terminal reason,
       diagnosis method and cause, selection and its reason, both counters, the intent states,
       the create count, the stored explanation and all nine projected timeline stages.
    2. **The sentence that went out is the approved template**, in all four — not merely
       "something went out". A schema-rejected draft is a *substitution* under R27.C10, so the
       failure mode this catches is a rejected draft being sent anyway.
    3. **No case waits on a model.** Every run ends terminal, so there is no state in the
       machine that means "waiting for a reasoning response" — R27.C7's clause, checked by the
       states that exist rather than by the absence of one.
    4. **The absent-credential run wrote no ``ai_invocation`` row at all**, while the two
       fault runs wrote some. Without that contrast the whole test would pass having taken the
       ``None`` branch four times, which is precisely the shape of a reasoning test that has
       quietly stopped reaching the reasoning layer.
    """
    template = description_for(CandidateAction.PAYMENT_LINK, merchant_name=_MERCHANT_NAME)
    assert template, "the payment link's approved template is the thing under test"

    outcomes: dict[str, _LoopOutcome] = {}
    invocations: dict[str, int] = {}
    for label, adapter in (
        ("credential absent", None),
        ("transport times out", _timing_out()),
        ("schema rejects the answer", _answering_rubbish()),
    ):
        fake = FakeRazorpay()
        with ExitStack() as stack:
            # A credential only where a fault other than its absence is being staged. The
            # baseline rung deliberately leaves the store as the deployment has it.
            if adapter is not None:
                stack.enter_context(
                    _installed(secret_store_with(REVORA_LLM_CREDENTIAL=_LLM_CREDENTIAL))
                )
            stack.enter_context(_reasoning(adapter))
            case_id, _ = drive_to_case(installed_engine, client, tenant, fake)
            drain(fake)
        outcomes[label] = _outcome(installed_engine, client, tenant, case_id, fake)
        invocations[label] = _count(
            installed_engine,
            "SELECT count(*) FROM ai_invocation WHERE case_id = :c",
            {"c": str(case_id)},
        )

    baseline = outcomes["credential absent"]
    assert baseline.state == CaseState.RECOVERED.value, (
        "the deployed path — no reasoning credential at all — must still recover a case end to "
        f"end; got {baseline.state}"
    )
    assert baseline.diagnosis_method == DiagnosisMethod.DETERMINISTIC.value
    assert baseline.selected_action == CandidateAction.PAYMENT_LINK.value
    assert baseline.create_calls == 1

    for label, outcome in outcomes.items():
        assert outcome == baseline, (
            f"the {label!r} rung of the reasoning row produced a different system:\n"
            f"  baseline={baseline}\n  {label}={outcome}"
        )
        assert outcome.description == template, (
            f"the {label!r} rung sent {outcome.description!r} rather than the approved template; "
            "a draft that failed a gate must be substituted, never sent"
        )
        assert outcome.ai_explanation is None, (
            f"the {label!r} rung stored model prose on the recommendation"
        )

    # No case waits on a model. Read against the state machine's own terminal set rather than
    # against a listed one, so this cannot disagree with it, and against every case this merchant
    # has rather than the three under test — a fault that stranded a *fourth* case would be the
    # same defect.
    waiting = [
        (case_id, state)
        for case_id, state in _rows(
            installed_engine,
            "SELECT id, state FROM recovery_case WHERE merchant_id = :m",
            {"m": str(tenant.merchant_id)},
        )
        if CaseState(str(state)) not in TERMINAL_STATES
    ]
    assert not waiting, (
        "a reasoning fault left a case non-terminal; there is no state that means 'waiting for "
        f"the model' and nothing may invent one: {waiting}"
    )

    # The contrast that stops this test passing for the wrong reason.
    assert invocations["credential absent"] == 0, (
        "an absent credential must issue no request and write no invocation row"
    )
    for label in ("transport times out", "schema rejects the answer"):
        assert invocations[label] > 0, (
            f"the {label!r} rung reached the reasoning layer zero times, so it asserted nothing "
            "about a broken reasoning layer; the credential is not being installed"
        )


def test_a_spent_reasoning_bound_sends_the_template_without_asking(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Row 1's fourth rung: ``MAX_REASONING_CALLS_PER_CASE`` reached (R27.C13, R27.C7).

    Kept apart from the other three because it is the one rung whose evidence is an **absence of
    a request** rather than a bad response, and the two need different witnesses. The case is
    parked at ``ACTION_SCHEDULED`` with the executor unavailable, its whole reasoning allowance
    is then committed as rows, and the executor comes back holding an adapter that fails loudly
    if it is asked for anything.

    Three claims:

    1. **Zero requests.** The bound is checked before the call, not after.
    2. **The allowance did not move**, so nothing wrote a row for a call it never made.
    3. **The action still executed, carrying the approved template.** A bound on advisory calls
       that could stop a payment link from going out would be a bound on recoveries.
    """
    template = description_for(CandidateAction.PAYMENT_LINK, merchant_name=_MERCHANT_NAME)
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine,
        client,
        tenant,
        fake,
        registry=registry_without_executor(fake),
    )
    assert case_state(installed_engine, case_id) == CaseState.ACTION_SCHEDULED.value

    bound = _spend_the_reasoning_bound(installed_engine, tenant.merchant_id, case_id)

    asked: list[str] = []
    with (
        _installed(secret_store_with(REVORA_LLM_CREDENTIAL=_LLM_CREDENTIAL)),
        _reasoning(_exploding(asked)),
    ):
        with tenant_transaction(tenant.merchant_id) as session:
            enqueue_next(
                session,
                tenant.merchant_id,
                kind=EXECUTION_JOB_KIND,
                case_id=case_id,
                correlation_id=None,
            )
        drain(fake)

    assert asked == [], f"the per-case bound did not stop the request: {asked}"
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM ai_invocation WHERE case_id = :c",
            {"c": str(case_id)},
        )
        == bound
    ), "a call that was never issued wrote an invocation row"

    creates = fake.calls_for("create_payment_link")
    assert len(creates) == 1, (
        "a spent advisory-call allowance stopped the payment link; the bound is on reasoning, "
        f"not on recoveries: {[call.operation for call in fake.calls]}"
    )
    key = str(creates[0].arguments["reference_id"])
    assert fake.sent_description_for(key) == template


# ---------------------------------------------------------------------------
# Row 2 — the customer page is unreachable
# ---------------------------------------------------------------------------


def test_an_unresolvable_signing_secret_abandons_the_execution_entirely(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Row 2's expensive consequence, and the most valuable assertion in this task (R18.C13).

    The customer page being unreachable sounds like it costs only the customer page. It does
    not: with the token signing secret unresolvable, ``PAYMENT_LINK`` and ``CUSTOMER_MESSAGE``
    stop too, because the mint shares the execution's first transaction and a failed mint rolls
    that transaction back rather than sending a message carrying no response-page URL. That is
    the correct direction — a payment message whose only link is dead is worse than no message —
    and it is the one place this row costs more than the table suggests.

    **Everything asserted here is an absence, and every absence has a witness.** The case is
    parked at ``ACTION_SCHEDULED`` with the executor unavailable, exactly one credential is then
    withdrawn — subtractively, so the payload cipher and the customer key derivation are
    untouched and cannot be what failed — and the executor comes back:

    1. **No provider call**, against the fake's own log rather than against the absence of an
       exception.
    2. **No execution-intent row.** The intent is reserved in the same transaction as the mint,
       so a row here would mean an idempotency key was durably spent on an attempt that never
       happened — and the next legitimate attempt would derive a different key.
    3. **Neither counter moved.** The ``ACTION_SCHEDULED -> EXECUTING`` edge is what moves them
       and it rolled back with everything else. A spent increment here is a customer message the
       case is charged for and never received.
    4. **The state is unchanged**, so the approval is still there to be executed when the
       credential comes back.
    5. **No token row**, which is the difference between "could not mint" and "minted and lost".

    And one presence, because a case that stops with no effect and no record is indistinguishable
    from a case nothing happened to: ``CUSTOMER_TOKEN_ISSUE_FAILED``, naming the reason.
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine,
        client,
        tenant,
        fake,
        registry=registry_without_executor(fake),
    )
    assert case_state(installed_engine, case_id) == CaseState.ACTION_SCHEDULED.value
    assert fake.call_count == 0, "the executor was supposed to be the component that was down"

    with _installed(secret_store_without(_SIGNING_SECRET)):
        with tenant_transaction(tenant.merchant_id) as session:
            enqueue_next(
                session,
                tenant.merchant_id,
                kind=EXECUTION_JOB_KIND,
                case_id=case_id,
                correlation_id=None,
            )
        drain(fake)

    assert fake.call_count == 0, (
        "an approved customer-visible action was sent with no response-page URL behind it: "
        f"{[call.operation for call in fake.calls]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id, state FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "an abandoned execution reserved an intent, so an idempotency key was spent for nothing"

    counters = _rows(
        installed_engine,
        "SELECT executed_action_count, customer_message_count, last_outbound_at "
        "FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )[0]
    assert int(counters[0]) == 0, "the executed-action counter moved on an abandoned execution"
    assert int(counters[1]) == 0, (
        "the customer-message counter moved on an execution that sent nothing; the case is now "
        "charged for a message the customer never received"
    )
    assert counters[2] is None, "the most-recent-outbound timestamp moved with no outbound action"

    assert case_state(installed_engine, case_id) == CaseState.ACTION_SCHEDULED.value, (
        "R18.C13 leaves the state unchanged, so the approval is still executable once the "
        "credential is resolvable again"
    )
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM customer_access_token WHERE case_id = :c",
            {"c": str(case_id)},
        )
        == 0
    ), "a token exists for an execution that was abandoned because no token could be minted"

    refusals = _rows(
        installed_engine,
        "SELECT decision FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
        {"c": str(case_id), "e": CUSTOMER_TOKEN_ISSUE_FAILED},
    )
    assert refusals, (
        "the abandonment was silent; a case that stops with no effect and no record cannot be "
        "told apart from a case nothing happened to"
    )
    assert "CREDENTIAL_UNAVAILABLE" in str(refusals[-1]), refusals[-1]


def test_restraint_is_still_revisited_with_the_customer_page_gone(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Row 2's other half: the decision loop is unaffected (R30.C5, R2.C12).

    The signing secret is absent for the *whole* test, so nothing here could have minted a token
    even if it wanted to — which is what makes this a statement about the review loop rather than
    about a happy path that happened to avoid the broken component. The case is driven at
    :data:`NULL_ACTION_AMOUNT`, so the pipeline chooses restraint on its own economics and rests
    at ``POLICY_CHECK`` with a ``next_review_at``.

    Then both surviving triggers are exercised, in the order the table lists them:

    * ``SCHEDULED_REVIEW`` — the sweep finds the due instant and enqueues one cycle, the worker
      applies it, and the case is re-decided.
    * ``EVENT_ATTACHED`` — a review enqueued the way the detection service enqueues one when a
      fresh failure lands on an open case, applied by the same worker.

    The negatives are the substance:

    * **Zero provider calls throughout.** Restraint is restraint; a review that re-decided its
      way into an action would have been stopped by the missing secret anyway, and this asserts
      it never got that far.
    * **``window_end_at`` is unchanged** across every review, which is what keeps R2.C12's
       termination bound provable. A review that extended the window would remove the guarantee.
    * **Every persisted ``next_review_at`` is at or before ``window_end_at``**, so no review is
      ever scheduled past the point the case can act.
    * **The decision-cycle counter is inside ``MAX_RECOVERY_ATTEMPTS``**, so the loop the reviews
      re-enter is the bounded one and not a second, unbounded one.
    """
    config = default_configuration()
    fake = FakeRazorpay()

    with _installed(secret_store_without(_SIGNING_SECRET)):
        case_id, payment_id = drive_to_case(
            installed_engine, client, tenant, fake, amount=NULL_ACTION_AMOUNT
        )
        drain(fake)

        opened = _rows(
            installed_engine,
            "SELECT state, window_end_at, next_review_at, decision_cycle_count "
            "FROM recovery_case WHERE id = :c",
            {"c": str(case_id)},
        )[0]
        assert str(opened[0]) == CaseState.POLICY_CHECK.value, (
            f"expected the pipeline to choose restraint at {NULL_ACTION_AMOUNT}, got {opened[0]}"
        )
        assert opened[2] is not None, (
            "a Null_Action selection must persist a next_review_at, or choosing to wait is "
            "choosing to abandon"
        )
        window_end = opened[1]
        cycles_after_first = int(opened[3])

        # ``SCHEDULED_REVIEW``. The sweep reads persisted columns alone, so the due instant is
        # moved into the past rather than the clock being moved forward.
        with installed_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE recovery_case SET next_review_at = now() - interval '1 minute' "
                    "WHERE id = :c"
                ),
                {"c": str(case_id)},
            )
        assert sweep_due_reviews(tenant.merchant_id) == 1, (
            "the review sweep found nothing due, so restraint is never revisited"
        )
        drain(fake)

        after_sweep = _rows(
            installed_engine,
            "SELECT state, window_end_at, next_review_at, decision_cycle_count "
            "FROM recovery_case WHERE id = :c",
            {"c": str(case_id)},
        )[0]
        assert int(after_sweep[3]) > cycles_after_first, (
            "the scheduled review did not re-decide the case; SCHEDULED_REVIEW is the trigger "
            "the whole restraint loop rests on"
        )
        assert after_sweep[1] == window_end, (
            "a scheduled review extended the recovery window, which removes R2.C12's "
            "termination guarantee"
        )

        # ``EVENT_ATTACHED``, delivered as a second signed ``payment.failed`` for the *same*
        # payment. Not a call into ``enqueue_case_review``: the trigger under test is detection
        # finding an already-open case, and the honest way to stage a customer whose retry failed
        # again is to send the webhook that says so. A fresh payment id would open a second case
        # and assert nothing about attachment.
        cycles_after_sweep = int(after_sweep[3])
        assert str(after_sweep[0]) == CaseState.POLICY_CHECK.value, (
            "the reviewed case did not choose restraint a second time, so the trigger below is "
            f"being applied to a case in a different situation: {after_sweep[0]}"
        )
        assert cycles_after_sweep < config.MAX_RECOVERY_ATTEMPTS, (
            f"the case reached the decision-cycle cap after one review ({cycles_after_sweep} of "
            f"{config.MAX_RECOVERY_ATTEMPTS}), so what follows would assert the cap rather than "
            "EVENT_ATTACHED; that is R30.C7's own test, not this one"
        )

        # The default contact is the one ``drive_to_case`` recorded consent against, so the
        # attach lands on a case whose consent decision has already been made.
        repeat_event = f"evt_{uuid.uuid4().hex[:16]}"
        body = failed_payment_body(payment_id, repeat_event, amount=NULL_ACTION_AMOUNT)
        assert deliver(client, tenant.slug, body, repeat_event) == 200
        drain(fake)

        attached = _count(
            installed_engine,
            "SELECT count(*) FROM audit_record WHERE case_id = :c AND event_type = :e",
            {"c": str(case_id), "e": EVENT_ATTACHED_TO_CASE},
        )
        assert attached == 1, (
            "the second failure did not attach to the open case, so what follows is about a "
            "different case and says nothing about EVENT_ATTACHED"
        )
        after_event = _rows(
            installed_engine,
            "SELECT window_end_at, decision_cycle_count FROM recovery_case WHERE id = :c",
            {"c": str(case_id)},
        )[0]
        assert int(after_event[1]) > cycles_after_sweep, (
            "an attached event did not re-decide the case, so a customer whose retry failed "
            "again gets no second look"
        )
        assert after_event[0] == window_end, "an attached event extended the recovery window"

    assert fake.call_count == 0, (
        "a case that chose restraint reached the provider: "
        f"{[call.operation for call in fake.calls]}"
    )
    final = _rows(
        installed_engine,
        "SELECT window_end_at, next_review_at, decision_cycle_count, state "
        "FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )[0]
    assert final[0] == window_end, "the recovery window moved at some point during the reviews"
    if final[1] is not None:
        assert final[1] <= window_end, (
            "a review was scheduled past the window end, so the case would be revisited after "
            "the last instant it could act"
        )
    assert int(final[2]) <= config.MAX_RECOVERY_ATTEMPTS, (
        f"the review loop ran {final[2]} decision cycles against a cap of "
        f"{config.MAX_RECOVERY_ATTEMPTS}; the new edge must increment the same bounded counter "
        "the two existing edges into DECISION_PENDING increment"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "a case that chose restraint reserved an execution intent"

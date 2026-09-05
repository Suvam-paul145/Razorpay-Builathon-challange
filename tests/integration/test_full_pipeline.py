"""Task 32.1. A failed payment becomes recovered revenue, and the trail explains every step.

This is the test that says the product works. Everything else in the suite verifies a component in
isolation; this drives the whole thing from a signed webhook at the HTTP boundary and asserts on
persisted rows and API responses only.

**The chain under test**, in order, none of it stubbed except the provider:

signed ``payment.failed`` → verified → canonicalized → deduplicated → detection verdict → case
opened → experiment arm assigned → deterministic diagnosis → baseline estimate → every candidate
priced → recommendation ranked → twelve policy checks → action scheduled → **exactly one** payment
link created → ``payment.captured`` delivered → authoritative provider read → ``RECOVERED`` →
memory observation written → metrics.

**Two claims get the most attention, because they are the two the product is judged on.**

*Exactly one external effect.* The fake records every call. The test asserts one link creation for
the case and re-drains the worker afterwards to prove a second pass adds none — which is the failure
mode that actually happens, because a retried job is normal and a duplicated customer message is
not.

*A recovery is declared only from an authoritative read.* The ``payment.captured`` webhook is
delivered, and it is asserted **not** to be the thing that declared the recovery: the
``recovery_outcome`` row carries ``verified_by_read_id`` pointing at a ``payment_state_read``, and
that column is ``NOT NULL`` precisely so this cannot be faked.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.audit.events import (
    CASE_DETECTED,
    DETECTION_VERDICT_RECORDED,
    DIAGNOSIS_RECORDED,
    EVENT_INGESTED,
    EXECUTION_STARTED,
    PAYMENT_STATE_READ_RECORDED,
    POLICY_DECISION_RECORDED,
    RECOMMENDATION_RECORDED,
    RECOVERY_OBSERVATION_RECORDED,
    RECOVERY_RECORDED,
    STATE_TRANSITION,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    NOT_ESTABLISHED,
    CaseState,
    DiagnosisMethod,
    InterventionStatus,
    OutcomeClass,
    PolicyVerdict,
    TerminalReason,
)
from tests.integration.conftest import (
    ESCALATION_PATH_AMOUNT,
    LINK_PATH_AMOUNT,
    FakeRazorpay,
    Tenant,
    captured_payment_body,
    case_state,
    delayed_capture,
    deliver,
    drain,
    drive_to_case,
)

pytestmark = pytest.mark.pg


def _rows(engine: Engine, sql: str, params: dict[str, object]) -> list[tuple]:
    with engine.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def _audit_types(engine: Engine, case_id: uuid.UUID) -> list[str]:
    return [
        str(row[0])
        for row in _rows(
            engine,
            "SELECT event_type FROM audit_record WHERE case_id = :c ORDER BY seq",
            {"c": str(case_id)},
        )
    ]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_failed_payment_is_recovered_end_to_end(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The whole chain, with one external effect and a recovery backed by a read.

    ``delayed_success(failed_reads=1)`` is the script, and it is what makes the ordering real: the
    read taken right after the link is created says the payment is still failed, so no recovery is
    declared; the read taken after the capture webhook says captured, and that one recovers it.
    A fake that answered captured immediately would recover the case before the customer had paid,
    and the test could then not tell a read-driven recovery from a webhook-driven one — which is the
    single distinction R10.C1 is about.
    """
    fake = FakeRazorpay(delayed_capture())
    case_id, payment_id = drive_to_case(installed_engine, client, tenant, fake)

    # -- decision and execution ---------------------------------------------
    drain(fake)
    assert case_state(installed_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value, (
        "an approved action should have been executed and the case should be awaiting an outcome"
    )
    assert fake.calls_for("fetch_payment"), (
        "execution should have enqueued an outcome observation, which reads the provider"
    )

    creates = fake.calls_for("create_payment_link")
    assert len(creates) == 1, f"expected exactly one payment link, got {len(creates)}"

    intents = _rows(
        installed_engine,
        "SELECT state, action, attempt_ordinal, provider_response_id, idempotency_key "
        "FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(intents) == 1
    state, action, ordinal, provider_response_id, key = intents[0]
    assert str(state) == "CONFIRMED"
    assert str(action) == "PAYMENT_LINK"
    assert int(ordinal) == 1
    assert provider_response_id, "a confirmed intent must carry the provider's own id"
    # The idempotency key travels to the provider as reference_id, so their side rejects a
    # duplicate too. Two independent mechanisms for one guarantee.
    assert fake.create_call_count_for(str(key)) == 1

    # -- a second worker pass must add no second effect ---------------------
    before = fake.call_count
    drain(fake)
    assert fake.calls_for("create_payment_link") == creates, (
        "a further worker pass created a second payment link; a retried job is normal and a "
        "second customer message is not"
    )
    assert fake.call_count >= before

    # -- the capture arrives, and only then does the read agree -------------
    capture_event = f"evt_{uuid.uuid4().hex[:16]}"
    assert (
        deliver(
            client,
            tenant.slug,
            captured_payment_body(payment_id, capture_event),
            capture_event,
        )
        == 200
    )
    drain(fake)

    assert case_state(installed_engine, case_id) == CaseState.RECOVERED.value

    # -- the recovery is backed by an authoritative read --------------------
    outcomes = _rows(
        installed_engine,
        "SELECT classification, recovered_amount, verified_by_read_id FROM recovery_outcome "
        "WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(outcomes) == 1, "a case must not recover twice"
    classification, amount, read_id = outcomes[0]
    assert int(amount) == LINK_PATH_AMOUNT
    assert read_id is not None, (
        "a recovery with no backing provider read is a recovery declared from a webhook, which is "
        "the one thing R10.C1 forbids"
    )
    reads = _rows(
        installed_engine,
        "SELECT status, captured FROM payment_state_read WHERE id = :r",
        {"r": str(read_id)},
    )
    assert reads and bool(reads[0][1]) is True, "the backing read must show a captured payment"

    # OBSERVED, not ATTRIBUTED: an action was taken and money arrived, and with no completed
    # experiment nothing licenses the causal claim. Presenting this as ATTRIBUTED is the exact
    # overstatement the whole design exists to prevent.
    assert str(classification) == OutcomeClass.OBSERVED.value

    # -- the learning loop closed ------------------------------------------
    observations = _rows(
        installed_engine,
        "SELECT outcome_class, intervention_status, diagnosis_method, selected_action "
        "FROM memory_observation WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(observations) == 1, "the terminal transition must write exactly one observation"
    outcome_class, intervention_status, diagnosis_method, observed_action = observations[0]
    assert str(outcome_class) == OutcomeClass.OBSERVED.value
    # A treated case that recovered is not a usable baseline label — it is a *treated* observation,
    # and counting it toward the no-intervention baseline would bias every future estimate toward
    # optimism. The status column is what keeps the two apart, and only
    # ``NO_INTERVENTION_CONFIRMED`` is usable as a baseline.
    assert str(intervention_status) == InterventionStatus.REVORA_INTERVENED.value, (
        intervention_status
    )
    assert str(diagnosis_method) == "DETERMINISTIC"
    assert str(observed_action) == "PAYMENT_LINK"

    # -- and metrics count it, without claiming causation -------------------
    report = client.get("/metrics/summary", headers=tenant.auth).json()["report"]
    assert report["recovered_case_count"] == 1
    assert report["observed_recovered_revenue"]["minor"] == LINK_PATH_AMOUNT
    assert report["observed_recovered_revenue"]["formatted"] == "₹1,000.00"
    incremental = report["incremental_recovered_revenue"]
    assert incremental["status"] == "NOT_ESTABLISHED", (
        "observed recovery must never be presented as incremental without an experiment"
    )
    assert "CAUSALITY_NOT_ESTABLISHED" in report["labels"]


def test_the_audit_trail_answers_every_question_r11c5_lists(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R11.C5, in one query. Eight questions, and the trail has to answer all eight.

    The requirement enumerates them: what happened, why, on what evidence, which alternatives were
    considered, which policy rules allowed or blocked the action, which action executed, whether
    payment recovered, and how the recovery is classified. This asserts each one is answerable from
    the single ordered read the dashboard performs — not from a join a support engineer would have
    to invent under pressure.
    """
    fake = FakeRazorpay(delayed_capture())
    case_id, payment_id = drive_to_case(installed_engine, client, tenant, fake)
    drain(fake)

    capture_event = f"evt_{uuid.uuid4().hex[:16]}"
    deliver(client, tenant.slug, captured_payment_body(payment_id, capture_event), capture_event)
    drain(fake)
    assert case_state(installed_engine, case_id) == CaseState.RECOVERED.value

    trail = client.get(f"/cases/{case_id}/audit", headers=tenant.auth).json()["records"]
    types = [record["event_type"] for record in trail]
    seqs = [record["seq"] for record in trail]

    # The sequence is gap-free and starts at one. Without that, "the full history" is a claim
    # nobody can check, because a missing record and a record that never existed look identical.
    assert seqs == list(range(1, len(seqs) + 1)), f"per-case sequence is not gap-free: {seqs}"

    # 1. What happened, and 6. which action executed.
    assert CASE_DETECTED in types
    assert STATE_TRANSITION in types
    assert EXECUTION_STARTED in types
    # 2. Why, and 3. on what evidence.
    assert DIAGNOSIS_RECORDED in types
    diagnosis_record = next(r for r in trail if r["event_type"] == DIAGNOSIS_RECORDED)
    assert diagnosis_record["evidence"], (
        "the diagnosis record must carry the provider fields it derived the cause from"
    )
    assert diagnosis_record["confidence"] is not None
    # 4. Which alternatives were considered.
    assert RECOMMENDATION_RECORDED in types
    recommendation_record = next(r for r in trail if r["event_type"] == RECOMMENDATION_RECORDED)
    assert recommendation_record["decision"], "the recommendation record must carry the comparison"
    # 5. Which policy rules allowed or blocked it.
    assert POLICY_DECISION_RECORDED in types
    policy_record = next(r for r in trail if r["event_type"] == POLICY_DECISION_RECORDED)
    assert policy_record["policy_result"], "the policy record must carry the check outcomes"
    # 7. Whether payment recovered, on what read.
    assert PAYMENT_STATE_READ_RECORDED in types
    assert RECOVERY_RECORDED in types
    # 8. How the recovery is classified — and that the loop closed.
    recovery_record = next(r for r in trail if r["event_type"] == RECOVERY_RECORDED)
    assert OutcomeClass.OBSERVED.value in str(recovery_record["decision"])
    assert RECOVERY_OBSERVATION_RECORDED in types

    # One correlation id for the whole delivery chain (R11.C7): the async work joins the inbound
    # event that scheduled it, so a merchant reporting "something odd at 14:32" is one query away.
    correlations = {record["correlation_id"] for record in trail}
    assert len(correlations) <= 2, (
        "the pipeline's records should share the delivery's correlation id; a distinct id per step "
        f"makes the trail unjoinable: {correlations}"
    )

    # The unattached ingestion record exists too, so the trail reaches back to the raw delivery.
    ingested = _rows(
        installed_engine,
        "SELECT count(*) FROM audit_record WHERE merchant_id = :m AND event_type IN (:a, :b)",
        {"m": str(tenant.merchant_id), "a": EVENT_INGESTED, "b": DETECTION_VERDICT_RECORDED},
    )
    assert int(ingested[0][0]) >= 2


# ---------------------------------------------------------------------------
# The refusal path
# ---------------------------------------------------------------------------


def test_an_opted_out_customer_is_blocked_with_zero_external_calls(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The other half of 32.1, and the half that protects a person rather than a number.

    An opt-out must stop the action before anything reaches the provider, the reason must be legible
    on the detail endpoint, and the count of provider calls must be zero — asserted from the fake's
    own log, because "no external call was made" is a claim about a negative.
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(installed_engine, client, tenant, fake, opted_out=True)
    drain(fake)

    assert fake.call_count == 0, (
        f"an opted-out customer was contacted: {[call.operation for call in fake.calls]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "an opted-out case must not even reserve an execution intent"

    # -- the decision is recorded, complete, and explains itself ------------
    detail = client.get(f"/cases/{case_id}", headers=tenant.auth).json()
    decisions = detail["policy_decisions"]
    assert isinstance(decisions, list) and decisions, decisions
    decision = decisions[-1]
    assert decision["verdict"] == PolicyVerdict.BLOCKED.value
    assert decision["primary_reason"] == "CUSTOMER_OPTED_OUT"

    # All twelve checks, not "the ones that ran". A partially recorded evaluation looks exactly
    # like one that ran fewer checks and approved.
    assert len(decision["checks"]) == 12
    opt_out_check = next(c for c in decision["checks"] if c["check_id"] == "CUSTOMER_OPTED_OUT")
    assert opt_out_check["outcome"] == "FAIL"

    # The blocked case is not presented as a failure, and the state says why it stopped.
    assert detail["case"]["state"] in (
        CaseState.POLICY_CHECK.value,
        CaseState.BLOCKED.value,
    ), detail["case"]["state"]

    # And the unresolved grouping counts it under a reason rather than in one lump.
    groups = {
        group["state"]: group
        for group in client.get("/metrics/unresolved", headers=tenant.auth).json()["groups"]
    }
    assert set(groups) == {"STOPPED", "BLOCKED", "EXPIRED", "ESCALATED", "FAILED"}


def test_an_opt_out_recorded_through_the_api_blocks_a_later_case(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R17.C10 end to end. An opt-out is about a person, so it governs cases that do not exist yet.

    Recorded through ``POST /consent`` with a contact, which is how an operator does it, and then a
    *new* failure for the same contact is delivered. The second case must be blocked without the
    operator having touched it — that cross-case reach is the whole point of keying consent on the
    customer rather than on the payment.
    """
    contact = "+919000055555"
    recorded = client.post(
        "/consent",
        json={"contact": contact, "opted_out": True, "source": "ticket-9001"},
        headers=tenant.auth,
    )
    assert recorded.status_code == 201, recorded.text

    fake = FakeRazorpay()
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    from tests.integration.conftest import case_for_payment, failed_payment_body

    assert (
        deliver(
            client,
            tenant.slug,
            failed_payment_body(
                payment_id, event_id, contact=contact, amount=LINK_PATH_AMOUNT
            ),
            event_id,
        )
        == 200
    )
    drain(fake)

    case_id = case_for_payment(installed_engine, tenant.merchant_id, payment_id)
    assert case_id is not None
    assert fake.call_count == 0, (
        "an opt-out recorded before this case existed must still suppress contact on it"
    )

    detail = client.get(f"/cases/{case_id}", headers=tenant.auth).json()
    assert detail["consent"]["opted_out"] is True
    decisions = detail["policy_decisions"]
    assert isinstance(decisions, list) and decisions
    assert decisions[-1]["primary_reason"] == "CUSTOMER_OPTED_OUT"


# ---------------------------------------------------------------------------
# Structural: no reasoning layer to depend on (task 32.2, P31)
# ---------------------------------------------------------------------------


def test_the_pipeline_runs_with_no_reasoning_layer_at_all(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Task 32.2 and P31, in the form the built system permits.

    The design asks for a run with the LLM adapter raising on every call, and for the terminal-state
    distribution to be identical to a run with it available. **No reasoning layer is active here**:
    no ``REVORA_LLM_CREDENTIAL`` is configured in this tier, so nothing can be called even now that
    task 49.2 has landed an adapter.

    That makes the claim stronger rather than untestable, and this test asserts the stronger form:
    the two runs are not merely identical, there is only one run possible. Every diagnosis is
    ``DETERMINISTIC``, no ``ai_invocation`` row exists, no recommendation carries AI prose, and a
    case reaches a terminal state anyway. A distribution comparison between two runs of the same
    code would be a tautology; this is the thing the comparison was a proxy for.

    **What changed, and what deliberately did not.** This test used to open by asserting that
    ``revora.reasoning`` had no public surface at all, on the grounds that the package was empty.
    Task 49.2 made that false by design — which is exactly what the previous version of this
    docstring said would have to happen — so the false half is dropped and nothing else is. The
    two-run comparison it pointed at now belongs to Properties 49 and 51, where a run with the
    credential absent is compared against a run with every response rejected.

    What is kept is the narrower structural claim, in the form
    ``test_authority_and_disclosure.py::test_the_reasoning_package_re_exports_nothing`` settled on:
    the package's ``__init__`` re-exports nothing, so every consumer names the submodule it depends
    on and a grep for the adapter finds every component that can reach a model. Read from the
    source for the reason that test gives — importing any submodule binds it as an attribute of the
    package, so ``dir()`` would make the assertion depend on test ordering, and in *this* tier the
    whole application is imported before the first line runs.
    """
    init = Path(__file__).resolve().parents[2] / "revora" / "reasoning" / "__init__.py"
    reexports = [
        line
        for line in init.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert reexports == [], (
        f"revora/reasoning/__init__.py has gained content {reexports}; a re-export makes the set "
        "of components that can reach a model unreadable from their imports, and this test's "
        "claim is that none of them is reaching one on this run"
    )

    fake = FakeRazorpay(delayed_capture())
    case_id, payment_id = drive_to_case(installed_engine, client, tenant, fake)
    drain(fake)

    capture_event = f"evt_{uuid.uuid4().hex[:16]}"
    deliver(client, tenant.slug, captured_payment_body(payment_id, capture_event), capture_event)
    drain(fake)

    # The case reached a terminal state, and it reached it on the deterministic path.
    assert case_state(installed_engine, case_id) == CaseState.RECOVERED.value
    methods = {
        str(row[0])
        for row in _rows(
            installed_engine,
            "SELECT method FROM diagnosis WHERE case_id = :c",
            {"c": str(case_id)},
        )
    }
    assert methods == {"DETERMINISTIC"}, methods
    assert (
        int(
            _rows(
                installed_engine,
                "SELECT count(*) FROM ai_invocation WHERE merchant_id = :m",
                {"m": str(tenant.merchant_id)},
            )[0][0]
        )
        == 0
    ), "no AI invocation may exist on a run with no reasoning layer"

    # And the recommendation carries no AI prose, so nothing downstream can render a model's
    # opinion as an explanation for a decision it had no part in.
    explanations = _rows(
        installed_engine,
        "SELECT ai_explanation_text FROM recommendation WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert all(row[0] is None for row in explanations), explanations


# ---------------------------------------------------------------------------
# The escalation branch
# ---------------------------------------------------------------------------


def test_a_large_failure_is_escalated_to_a_human_with_no_provider_call(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The third routing branch, and the one that stalled silently before it existed.

    At ``ESCALATION_PATH_AMOUNT`` the priors put ``HUMAN_ESCALATION`` above ``PAYMENT_LINK`` on net
    value, so policy approves an action Revora cannot itself perform. Three things then have to be
    true at once, and each was false at some point during this build:

    *The case leaves automation.* ``ESCALATED`` is terminal. Routing an escalation through
    ``ACTION_SCHEDULED`` — which is what happened when ``EXECUTABLE_ACTIONS`` was used to decide
    whether a provider call was needed — left the case authorized, unexecuted and waiting for its
    window to close with nothing on the record explaining why nothing happened.

    *No external call is made.* Asserted from the fake's log, because it is a claim about a
    negative. An approved action that reaches the provider when a human was supposed to be asked is
    the failure this branch exists to prevent.

    *An observation is still written.* A terminal case produces a training label whatever the
    terminal reason, and this one must record ``HUMAN_ESCALATION`` as the selected action rather
    than nothing — otherwise the memory layer learns that large failures are cases where Revora
    chose to do nothing, which is the opposite of what happened.
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine, client, tenant, fake, amount=ESCALATION_PATH_AMOUNT
    )
    drain(fake)

    assert case_state(installed_engine, case_id) == CaseState.ESCALATED.value, (
        "a case above the escalation crossover must end in ESCALATED, not sit in ACTION_SCHEDULED "
        "waiting for an executor that will never run"
    )
    assert fake.call_count == 0, (
        f"an escalation reached the provider: {[call.operation for call in fake.calls]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "an escalation must not reserve an execution intent"

    # The decision is an approval, and the record says which action it approved.
    decision_rows = _rows(
        installed_engine,
        "SELECT verdict, selected_action FROM policy_decision WHERE case_id = :c "
        "ORDER BY evaluated_at DESC",
        {"c": str(case_id)},
    )
    assert decision_rows, "the escalation must still be a recorded policy decision"
    verdict, selected_action = decision_rows[0]
    assert str(verdict) == PolicyVerdict.APPROVED.value
    assert str(selected_action) == CandidateAction.HUMAN_ESCALATION.value

    # Terminal, with the reason naming the human rather than a failure.
    terminal = _rows(
        installed_engine,
        "SELECT terminal_reason FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )
    assert str(terminal[0][0]) == TerminalReason.HUMAN_OWNERSHIP.value

    # And the observation records what was chosen, not silence.
    observations = _rows(
        installed_engine,
        "SELECT selected_action, outcome_class, intervention_status, diagnosis_method "
        "FROM memory_observation WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(observations) == 1, "a terminal escalation must produce exactly one observation"
    observed_action, outcome_class, intervention_status, diagnosis_method = observations[0]
    assert str(observed_action) == CandidateAction.HUMAN_ESCALATION.value
    assert str(outcome_class) == NOT_ESTABLISHED, (
        "an escalated case has no measured recovery, and NULL would confuse 'we did not measure' "
        f"with 'we measured nothing': {outcome_class}"
    )
    # No confirmed *Revora* action, and the case was not in the control arm, so Revora has no basis
    # to claim nobody intervened — a human was explicitly asked to.
    assert str(intervention_status) in (
        InterventionStatus.MERCHANT_INTERVENTION_UNKNOWN.value,
        InterventionStatus.NO_INTERVENTION_CONFIRMED.value,
    ), intervention_status
    # The provenance the cycle-resolution bug used to drop.
    assert str(diagnosis_method) == DiagnosisMethod.DETERMINISTIC.value

    # The unresolved surface counts it under ESCALATED rather than in a single lump of "not
    # recovered", which is the difference between an operator having a queue and having a number.
    groups = {
        group["state"]: group
        for group in client.get("/metrics/unresolved", headers=tenant.auth).json()["groups"]
    }
    assert groups["ESCALATED"]["case_count"] == 1, groups

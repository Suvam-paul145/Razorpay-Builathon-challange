"""Task 54.3. The customer response loop, composed — not step by step, but end to end.

Every individual step of this loop already has coverage somewhere, and none of that coverage
says the steps **compose**. This file asserts the joins:

* the Customer_Access_Token minted inside the execution's first transaction is the one the
  customer's HTTP request verifies — asserted through ``customer_signal.token_id``, which is the
  handle the public endpoint wrote after verifying a presentation against the persisted hash;
* the payment link the customer sees on the page is the one the first execution created —
  ``projection.pay_url`` against ``execution_intent.provider_short_url``;
* the cause the customer stated is the cause the **next** decision cycle reads — the second
  cycle's ``diagnosis`` row carries the mapped cause, the stated reason, the signal's own id and
  ``CUSTOMER_STATED_CAUSE_CONFIDENCE`` rather than the provider's;
* the promise clamps against the same ``window_end_at`` the case was created with —
  ``promise_to_pay.window_end_at_snapshot`` against ``recovery_case.window_end_at``, and
  ``follow_up_at`` against the clamp computed from the live configuration;
* the resend goes to the link the first execution created rather than to a second one — one
  ``create_payment_link`` call for the whole case and one ``notify_by`` against *that* link id;
* and the audit trail answers every question R11.C5 lists **in one query**.

That last one is the most valuable assertion in the task, and it is written as a single
aggregate ``SELECT`` over ``audit_record`` for the case — see :data:`_R11C5`. The existing
``test_full_pipeline.py::test_the_audit_trail_answers_every_question_r11c5_lists`` walks the
``/cases/{id}/audit`` response and checks each event type is present, which is a claim about the
*records*; this is a claim about the **read**. A trail that needs five queries and a join the
reader has to invent is not the trail R11.C5 asks for, so the query is the assertion.

The nine timeline stages are the second: ``revora.timeline`` projects the whole loop from the
reader's side, and at the end of the happy path all nine must be ``DONE`` — which is the only
place in the suite where that is true of a real case rather than of a constructed record set.

**Cost.** Three tests, all ``pg``, no demo batch, no real sleep. Time is moved with a
:class:`~revora.platform.clock.ManualClock`, started at the real instant rather than at
``ManualClock``'s 2025 default: consent is recorded by the fixture with the *database's*
``now()``, and a clock a year behind it would make every consent record land in the future and
every customer-visible action fail policy check 6 for a reason that has nothing to do with this
task.

**What is deliberately not here.** P1, P35, P39 and P63 are asserted over generated inputs in
``tests/properties/``; this file asserts that the components those properties describe line up
in one run, and restates none of them. The restraint test does not re-assert the window
immutability or the ``EVENT_ATTACHED`` trigger — ``test_customer_loop_degradation.py::
test_restraint_is_still_revisited_with_the_customer_page_gone`` already owns both, and a second
copy would be a second thing to update. What it adds is the two claims that file does not make:
the ``CASE_REVIEWED`` record showing ``WAIT`` was chosen *again*, and the case appearing in no
ended grouping on the API surface at any point.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.audit.events import (
    CASE_DETECTED,
    CASE_ESCALATED,
    CASE_REVIEWED,
    CUSTOMER_SIGNAL_RECORDED,
    CUSTOMER_TOKEN_ISSUED,
    DIAGNOSIS_RECORDED,
    EXECUTION_STARTED,
    PAYMENT_STATE_READ_RECORDED,
    POLICY_DECISION_RECORDED,
    PROMISE_RECORDED,
    RECOMMENDATION_RECORDED,
    RECOVERY_OBSERVATION_RECORDED,
    RECOVERY_RECORDED,
)
from revora.cases.review import sweep_due_reviews
from revora.customer.promises import sweep_due_promises
from revora.customer.signals import cause_for_delay_reason
from revora.customer.suppression import suppression_scope_key
from revora.diagnosis.service import (
    EVIDENCE_CAUSE_REFINED,
    EVIDENCE_CUSTOMER_SIGNAL_ID,
    EVIDENCE_STATED_REASON,
)
from revora.domain.actions import NULL_ACTIONS, CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    DiagnosisEvidenceSource,
    HardStopReason,
    InterventionStatus,
    OutcomeClass,
    PolicyVerdict,
    PromiseStatus,
    TerminalReason,
    TimelineStage,
    TimelineStageStatus,
    TokenRevocationReason,
)
from revora.domain.failure_taxonomy import EVIDENCE_SOURCE
from revora.domain.payment_event import PaymentStatus
from revora.domain.transitions import TERMINAL_STATES
from revora.platform.clock import ManualClock, using_clock
from revora.platform.config import default_configuration
from revora.providers.payment_link import NotifyMedium
from revora.synthetic.demo import capturing_customer_tokens
from revora.timeline.stages import STAGE_ORDER
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour
from tests.integration.conftest import (
    DASHBOARD_KEY,
    Tenant,
    captured_payment_body,
    case_state,
    customer_key_of,
    deliver,
    drain,
    drive_to_case,
)
from tests.integration.test_customer_loop_degradation import NULL_ACTION_AMOUNT

pytestmark = pytest.mark.pg

FOLLOW_UP_PATH_AMOUNT = 300_000
"""₹3,000. The amount at which *both* halves of this loop are selectable.

``LINK_PATH_AMOUNT`` (₹1,000) is derived for the first cycle only, and it is too tight for the
second: ``PROMISE_FOLLOW_UP_PRIOR_PROBABILITY`` is 0.06, so a follow-up on ₹1,000 nets roughly
₹53 against a ``MIN_NET_VALUE_THRESHOLD`` of ₹50 — positive, and close enough that any future
adjustment to the message or customer cost priors would turn this test's subject into a
``NO_POSITIVE_VALUE`` exclusion rather than into a failure anybody could read.

₹3,000 sits inside the band the demonstration loader independently verified selects
``PAYMENT_LINK`` (``DEMO_LINK_AMOUNT_RANGE``, ₹1,500 to ₹11,000) and well below the ₹12,000
crossover where ``HUMAN_ESCALATION`` overtakes the link, so the first cycle still chooses the
link; and it puts the follow-up's net value near ₹170, an order of magnitude clear of the
threshold. Derived from the priors, like every other amount constant in this tier — not tuned
until a test passed.
"""

_ORDER = "order_composed_loop"
"""One order id shared by the refusal path's two failures.

The Suppression_Scope is ``sha256(customer_key ‖ order_id)`` where the Payment_Event carried an
order, so "a dispute suppresses this order" is only observable across two failures that agree
about the order. ``failed_payment_body`` otherwise derives a distinct order per event, which is
the right default and the reason this has to be said out loud.
"""


# ---------------------------------------------------------------------------
# Reading rows back
# ---------------------------------------------------------------------------


def _rows(engine: Engine, sql: str, params: dict[str, object]) -> list[tuple]:
    with engine.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def _one(engine: Engine, sql: str, params: dict[str, object]) -> tuple:
    found = _rows(engine, sql, params)
    assert found, f"expected exactly one row from {sql!r}, got none"
    return found[0]


def _case(engine: Engine, case_id: uuid.UUID) -> tuple:
    return _one(
        engine,
        "SELECT state, terminal_reason, window_end_at, next_review_at, decision_cycle_count, "
        "executed_action_count, customer_message_count, payment_amount, customer_key, "
        "provider_order_id FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )


# ---------------------------------------------------------------------------
# R11.C5, as one query
# ---------------------------------------------------------------------------


_R11C5 = """
SELECT
    array_agg(event_type ORDER BY seq)                                       AS narrative,
    array_agg(seq ORDER BY seq)                                              AS sequence,
    (array_agg(diagnosis  ORDER BY seq) FILTER (WHERE event_type = :diag))[1] AS why,
    (array_agg(evidence   ORDER BY seq) FILTER (WHERE event_type = :diag))[1] AS on_what_evidence,
    (array_agg(confidence ORDER BY seq) FILTER (WHERE event_type = :diag))[1] AS how_confident,
    (array_agg(decision   ORDER BY seq) FILTER (WHERE event_type = :rec))[1]  AS alternatives,
    (array_agg(policy_result ORDER BY seq) FILTER (WHERE event_type = :pol))[1] AS policy_rules,
    array_agg(action ORDER BY seq) FILTER (WHERE event_type = :exec)          AS actions_executed,
    array_agg(decision ORDER BY seq) FILTER (WHERE event_type = :read)        AS reads,
    (array_agg(decision ORDER BY seq) FILTER (WHERE event_type = :recov))[1]  AS classification,
    (array_agg(decision ORDER BY seq) FILTER (WHERE event_type = :sig))[1]    AS customer_said,
    (array_agg(decision ORDER BY seq) FILTER (WHERE event_type = :prom))[1]   AS promise
FROM audit_record
WHERE case_id = :c
"""
"""R11.C5 in one ordered aggregate over ``audit_record``. Twelve columns, no joins.

The requirement enumerates eight questions a reader must be able to establish about any case, and
each is a column here:

1. *what happened* — ``narrative``, the event types in per-case sequence order, plus ``sequence``
   so the reader can see the run is gap-free rather than take it on trust;
2. *why* — ``why``, the ``diagnosis`` payload of the first ``DIAGNOSIS_RECORDED``;
3. *on what evidence* — ``on_what_evidence`` and ``how_confident``, from the same record;
4. *which alternatives were considered* — ``alternatives``, the recommendation's comparison;
5. *which policy rules allowed or blocked it* — ``policy_rules``, the twelve check outcomes;
6. *which action executed* — ``actions_executed``, every ``EXECUTION_STARTED``'s action in order,
   an array rather than a scalar because a case that resent a link executed twice;
7. *whether payment recovered* — ``reads``, every authoritative read's status and captured flag,
   which is where that question is actually settled (R10.C1);
8. *how the recovery is classified* — ``classification``, the ``RECOVERY_RECORDED`` payload.

``customer_said`` and ``promise`` are the two the customer response loop adds. They are in the
same query rather than beside it because the point being made is that the trail is *one* read: a
merchant explaining this case to the person on the other end of it asks all ten questions at
once or the claim is not the claim.

Aggregates rather than one row per record so the assertion is a statement about a single result
value. ``(array_agg(...) FILTER (...))[1]`` is "the first record of this type, in sequence
order", which is the same rule ``revora.timeline.stages._by_event_type`` applies for the same
reason: a case reviewed twice has two recommendations and the question is about the first.
"""


def _explain(engine: Engine, case_id: uuid.UUID) -> dict[str, Any]:
    """Run :data:`_R11C5` once and name its columns."""
    with engine.begin() as connection:
        row = connection.execute(
            text(_R11C5),
            {
                "c": str(case_id),
                "diag": DIAGNOSIS_RECORDED,
                "rec": RECOMMENDATION_RECORDED,
                "pol": POLICY_DECISION_RECORDED,
                "exec": EXECUTION_STARTED,
                "read": PAYMENT_STATE_READ_RECORDED,
                "recov": RECOVERY_RECORDED,
                "sig": CUSTOMER_SIGNAL_RECORDED,
                "prom": PROMISE_RECORDED,
            },
        ).mappings().one()
    return dict(row)


# ---------------------------------------------------------------------------
# The customer's own two requests
# ---------------------------------------------------------------------------


def _read_page(client: TestClient, slug: str, token: str) -> tuple[int, dict[str, Any]]:
    response = client.get(
        f"/customer/{slug}/case", headers={"Authorization": f"Bearer {token}"}
    )
    body = {} if not response.content else response.json()
    return response.status_code, body


def _submit(
    client: TestClient, slug: str, token: str, route: str, payload: dict[str, object]
) -> tuple[int, dict[str, Any]]:
    """One customer write, through the mounted public surface exactly as a browser makes it.

    ``Content-Type: application/json`` is what ``TestClient(json=...)`` sets, and it is not
    optional: :class:`~revora.api.routers.customer.CustomerSurfaceGuards` answers 415 before the
    body is bound without it, so a submission missing it fails for a reason that says nothing
    about the submission.
    """
    response = client.post(
        f"/customer/{slug}/{route}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    body = {} if not response.content else response.json()
    return response.status_code, body


def _fresh_session(client: TestClient, tenant: Tenant) -> dict[str, str]:
    """A new dashboard session for the same merchant, and why one is needed.

    A Merchant_User session has a lifetime, and this loop deliberately spans most of a recovery
    window of clock — so the session the ``tenant`` fixture established before the failure was
    even delivered has expired by the time the case recovers, and every dashboard read after the
    advance answers 401. That is the session bound working, not a defect: the operator who reads
    the outcome reads it with a session established when they sit down, which is what this mints.
    """
    response = client.post(
        "/auth/sessions",
        json={"merchant_slug": tenant.slug},
        headers={DASHBOARD_KEY_HEADER: DASHBOARD_KEY},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _ended_groupings(
    client: TestClient, tenant: Tenant, case_id: uuid.UUID, auth: dict[str, str] | None = None
) -> dict[str, object]:
    """Every way the API surface presents an *ended* case, checked against one case id.

    Two surfaces, because they answer different questions and a case could appear in either.
    ``/metrics/unresolved`` groups unresolved revenue under the five ended states, and
    ``/cases?state=`` lists the cases in one state — so this returns the total count across the
    five groups and every terminal listing this case appears in. R30.C12 and R30.C13: a case that
    chose restraint is *waiting*, and presenting it as stopped, blocked, expired, escalated or
    failed would tell an operator to go and look at something that is working.
    """
    headers = tenant.auth if auth is None else auth
    groups = client.get("/metrics/unresolved", headers=headers).json()["groups"]
    listed = []
    for state in sorted(state.value for state in TERMINAL_STATES):
        page = client.get(f"/cases?state={state}", headers=headers).json()["cases"]
        if any(str(row["case_id"]) == str(case_id) for row in page):
            listed.append(state)
    return {
        "group_case_counts": {str(g["state"]): int(g["case_count"]) for g in groups},
        "terminal_listings": listed,
    }


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_the_whole_customer_loop_composes_end_to_end(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """One case, from a signed ``payment.failed`` to ``RECOVERED`` with a promise ``KEPT``.

    Nothing is called directly except the two sweeps and the worker, which is what production
    calls. Every other input is a signed webhook or an HTTP request a customer could make, and
    every assertion is a persisted row or an API response.

    **The joins this asserts, in order of what they would cost to get wrong.**

    1. *The token minted at execution is the one the request verifies.*
       ``customer_signal.token_id`` is written by the public endpoint after verifying a
       presentation against ``customer_access_token.secret_hash``; comparing it to the handle of
       the row the execution minted is the only way to state that without trusting either side.
    2. *The page shows the link the first execution created.* A projection composing its own
       ``pay_url`` would be a second link the customer could pay through, and the exactly-once
       guarantee would be about the wrong object.
    3. *The cause the customer stated is the cause the next cycle reads.* The refinement is
       visible even though ``SALARY_OR_CASHFLOW_TIMING`` maps to the cause the provider already
       gave, because the recorded confidence becomes ``CUSTOMER_STATED_CAUSE_CONFIDENCE`` and the
       evidence names the signal — so this cannot pass by the second cycle simply re-running the
       taxonomy.
    4. *The promise clamps against the case's own window.* Asserted as the identity
       ``follow_up_at == min(promise_date + offset, window_end - margin)`` computed from the live
       configuration, against ``window_end_at_snapshot``, against ``recovery_case.window_end_at``.
       The promise date is chosen so the **window** branch of that ``min`` is the one taken.
    5. *The resend goes to the first link.* One ``create_payment_link`` for the whole case and one
       ``notify_by`` against that link's provider id.
    6. *The trail answers R11.C5 in one query*, and the timeline projects nine stages ``DONE``.

    **A finding about the ordering, recorded here because the test is where it is provable.**
    The task's chain reads "resend confirmed → ``payment.captured`` → authoritative read →
    ``RECOVERED`` → promise ``KEPT``". That exact order is **not reachable**, and it is not a
    staging problem. A ``CONFIRMED`` execution enqueues its own outcome observation
    (``revora.jobs.pipeline.handle_execution``), so the read that immediately follows a confirmed
    follow-up is the read R23.C11 names: a confirmed ``PROMISE_TO_PAY_FOLLOW_UP`` plus a read
    reporting not-captured *is* a missed promise, so the promise moves to ``MISSED`` and
    ``apply_missed_disposition`` returns the case to ``DECISION_PENDING``. Any capture arriving
    after that read finds the promise already ``MISSED``, and ``resolve_kept`` — whose
    ``expected_statuses`` are the two pending ones plus ``BEYOND_WINDOW_ESCALATED`` — correctly
    declines to overwrite a measurement. The demonstration batch agrees: its four ``KEPT``
    promises capture in phase 5, *before* ``_run_sweeps``, so no follow-up is ever sent for them.

    So the reachable composition is the one staged below: the money is at the provider when the
    reminder's own confirming read fires, the read declares the recovery and keeps the promise,
    and the ``payment.captured`` webhook that follows is absorbed idempotently — asserted, since
    a duplicate capture that read the provider again or wrote a second outcome row would be the
    defect R10.C1's short-circuit exists to prevent.
    """
    config = default_configuration()
    clock = ManualClock(datetime.now(UTC))
    # Failed on every read until this test says otherwise. Not ``delayed_capture()``: that
    # answers captured from the second read onwards, and this loop takes several reads before
    # the customer has paid — the first of which would recover the case before the page had
    # even been opened.
    fake = FakeRazorpay(
        ProviderBehaviour(
            payment_statuses=(PaymentStatus.FAILED,), payment_amount=FOLLOW_UP_PATH_AMOUNT
        )
    )

    with using_clock(clock):
        # -- the failure, the case, the decision, the link, and the token ----------------
        with capturing_customer_tokens() as tokens:
            case_id, payment_id = drive_to_case(
                installed_engine, client, tenant, fake, amount=FOLLOW_UP_PATH_AMOUNT
            )
            drain(fake)

        assert case_state(installed_engine, case_id) == CaseState.WAITING_FOR_OUTCOME.value
        assert case_id in tokens, (
            "no Customer_Access_Token was minted for an executed customer-visible action; the "
            "whole loop below is about the credential this step produces"
        )
        wire_token = tokens[case_id]

        creates = fake.calls_for("create_payment_link")
        assert len(creates) == 1, f"expected exactly one payment link, got {len(creates)}"
        intent = _one(
            installed_engine,
            "SELECT id, state, action, provider_response_id, provider_short_url, "
            "attempt_ordinal FROM execution_intent WHERE case_id = :c ORDER BY attempt_ordinal",
            {"c": str(case_id)},
        )
        assert str(intent[1]) == "CONFIRMED"
        assert str(intent[2]) == CandidateAction.PAYMENT_LINK.value
        link_id = str(intent[3])
        short_url = str(intent[4])

        # Join 1a: the mint shares the execution's first transaction. Both records are written
        # under the case row lock that transaction holds, and the per-case sequence is allocated
        # there — so adjacent sequence numbers mean nothing else committed a record for this case
        # between the mint and the execution. Exactly what R18.C1 buys, read off the trail.
        token_row = _one(
            installed_engine,
            "SELECT token_id, approved_action, revoked_at, accepted_submission_count, expires_at "
            "FROM customer_access_token WHERE case_id = :c",
            {"c": str(case_id)},
        )
        minted_handle = str(token_row[0])
        assert str(token_row[1]) == CandidateAction.PAYMENT_LINK.value
        assert token_row[2] is None, "the token was revoked before the customer could use it"
        issued_seq = _one(
            installed_engine,
            "SELECT seq FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
            {"c": str(case_id), "e": CUSTOMER_TOKEN_ISSUED},
        )[0]
        started_seq = _one(
            installed_engine,
            "SELECT seq FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
            {"c": str(case_id), "e": EXECUTION_STARTED},
        )[0]
        assert int(started_seq) == int(issued_seq) + 1, (
            "CUSTOMER_TOKEN_ISSUED and EXECUTION_STARTED are not adjacent in the case's audit "
            f"sequence ({issued_seq} then {started_seq}), so the mint and the execution did not "
            "share one transaction and R18.C13's rollback guarantee is not what it claims"
        )

        # -- the customer opens the page ------------------------------------------------
        status, page = _read_page(client, tenant.slug, wire_token)
        assert status == 200, page
        assert page["amount"]["minor"] == FOLLOW_UP_PATH_AMOUNT
        assert page["signals_remaining"] == config.CUSTOMER_TOKEN_MAX_SUBMISSIONS
        assert page["promise"] is None
        # Join 2: the link on the page is the link the execution created.
        assert page["pay_url"] == short_url, (
            "the page offers a URL the first execution did not create; a second payable link is "
            "the exactly-once guarantee applied to the wrong object"
        )
        window_end = _case(installed_engine, case_id)[2]
        assert page["window_end_at"] == window_end.isoformat()

        # -- the customer says why ------------------------------------------------------
        status, body = _submit(
            client,
            tenant.slug,
            wire_token,
            "delay-reason",
            {
                "delay_reason": DelayReason.SALARY_OR_CASHFLOW_TIMING.value,
                "note": "My salary lands on the 30th.",
            },
        )
        assert status == 201, body
        assert body["signals_remaining"] == config.CUSTOMER_TOKEN_MAX_SUBMISSIONS - 1

        signal = _one(
            installed_engine,
            "SELECT id, token_id, kind, delay_reason FROM customer_signal WHERE case_id = :c",
            {"c": str(case_id)},
        )
        signal_id = uuid.UUID(str(signal[0]))
        # Join 1b: the handle the endpoint verified is the handle the execution minted.
        assert str(signal[1]) == minted_handle, (
            "the accepted submission was verified against a different token than the one the "
            "execution minted, so nothing here says the credential the customer received works"
        )
        assert str(signal[2]) == CustomerSignalKind.DELAY_REASON.value
        assert str(signal[3]) == DelayReason.SALARY_OR_CASHFLOW_TIMING.value

        # R30.C8's review trigger does **not** fire here, and that is the system being right
        # rather than a gap. A case holding a live token has been acted on, so it is standing in
        # ``WAITING_FOR_OUTCOME`` and not at ``POLICY_CHECK`` — which is the only state the
        # signal-triggered review is enqueued from. The cycle that reads this reason is the one
        # the promise sweep enqueues below, under ``SCHEDULED_REVIEW``. Asserted rather than
        # assumed, because a reader of the task's chain would expect the opposite.
        recorded = _one(
            installed_engine,
            "SELECT decision FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
            {"c": str(case_id), "e": CUSTOMER_SIGNAL_RECORDED},
        )[0]
        assert recorded["case_state"] == CaseState.WAITING_FOR_OUTCOME.value, recorded
        assert recorded["review_enqueued"] is False, recorded

        # -- and when they will pay -----------------------------------------------------
        #
        # Late enough that the window, not the promised date, decides the Follow_Up_Instant, so
        # the branch of the clamp this loop exercises is the one R23.C3 exists for.
        promise_date = window_end - timedelta(hours=12)
        status, body = _submit(
            client,
            tenant.slug,
            wire_token,
            "promise",
            {"promise_date": promise_date.isoformat()},
        )
        assert status == 201, body

        promise = _one(
            installed_engine,
            "SELECT id, status, promise_date, follow_up_at, window_end_at_snapshot, "
            "customer_signal_id FROM promise_to_pay WHERE case_id = :c",
            {"c": str(case_id)},
        )
        assert str(promise[1]) == PromiseStatus.RECORDED.value
        # Join 4: the clamp, against the window the case was created with and not a copy.
        assert promise[4] == window_end, (
            "the promise snapshotted a window end that is not the case's; R23.C4 and R2.C5 mean "
            "these two values can never differ, and a difference here is a second window"
        )
        expected_follow_up = min(
            promise[2] + config.PROMISE_FOLLOW_UP_OFFSET,
            window_end - config.PROMISE_WINDOW_SAFETY_MARGIN,
        )
        assert promise[3] == expected_follow_up, (
            f"follow_up_at is {promise[3]} and the clamp computes {expected_follow_up}"
        )
        assert expected_follow_up == window_end - config.PROMISE_WINDOW_SAFETY_MARGIN, (
            "this promise date was chosen so the window end decides the Follow_Up_Instant; if "
            "the promised date is deciding it, the clamp is not the branch under test"
        )
        assert promise[3] < window_end, "R23.C3's follow-up must be strictly inside the window"

        # -- the sweep reaches the Follow_Up_Instant -------------------------------------
        clock.advance((promise[3] - clock.now()) + timedelta(minutes=5))

        # The money is at the provider by the time the reminder's confirming read fires. See
        # this test's docstring for why any later arrival makes the promise MISSED instead.
        fake.script_payment(
            payment_id, amount=FOLLOW_UP_PATH_AMOUNT, statuses=(PaymentStatus.CAPTURED,)
        )

        tally = sweep_due_promises(tenant.merchant_id)
        assert tally.scheduled == 1, (
            f"the promise sweep did not schedule the due follow-up: {tally}"
        )
        assert tally.reviews_enqueued == 1, (
            "the sweep scheduled a follow-up and queued no decision cycle to consider it, which "
            f"is the R24.C13 gap FOLLOW_UP_REVIEW_STATES exists to close: {tally}"
        )
        drain(fake)

        # -- the second cycle reads the customer's reason -------------------------------
        diagnoses = _rows(
            installed_engine,
            "SELECT decision_cycle, cause, confidence, evidence FROM diagnosis "
            "WHERE case_id = :c ORDER BY decision_cycle",
            {"c": str(case_id)},
        )
        assert len(diagnoses) >= 2, (
            f"the review ran no second diagnosis, so nothing read the stated reason: {diagnoses}"
        )
        refined = diagnoses[-1]
        stated_cause = cause_for_delay_reason(DelayReason.SALARY_OR_CASHFLOW_TIMING)
        assert stated_cause is not None
        # Join 3. The cause, the source, the signal's own id, and the confidence — four fields
        # because the mapped cause happens to equal the provider's here, so the cause alone
        # would be satisfied by the second cycle merely re-running the taxonomy.
        assert str(refined[1]) == stated_cause.value
        assert Decimal(str(refined[2])) == config.CUSTOMER_STATED_CAUSE_CONFIDENCE, (
            "the refined diagnosis kept the provider's confidence, so the customer's account is "
            "not what the recorded cause rests on"
        )
        evidence = refined[3]
        assert evidence[EVIDENCE_SOURCE] == DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value
        assert evidence[EVIDENCE_STATED_REASON] == DelayReason.SALARY_OR_CASHFLOW_TIMING.value
        assert evidence[EVIDENCE_CAUSE_REFINED] is True
        assert evidence[EVIDENCE_CUSTOMER_SIGNAL_ID] == str(signal_id), (
            "the refined diagnosis names a different Customer_Signal than the one submitted"
        )

        # -- the follow-up was selected, approved, and resent on the first link ---------
        selections = [
            str(row[0])
            for row in _rows(
                installed_engine,
                "SELECT selected_action FROM recommendation WHERE case_id = :c "
                "ORDER BY created_at",
                {"c": str(case_id)},
            )
        ]
        assert selections[-1] == CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value, (
            f"the cycle a due follow-up caused selected {selections[-1]!r}; R24.C2 makes the "
            "follow-up selectable exactly here and there is nothing else this cycle is for"
        )
        intents = _rows(
            installed_engine,
            "SELECT action, state, attempt_ordinal, provider_response_id FROM execution_intent "
            "WHERE case_id = :c ORDER BY attempt_ordinal",
            {"c": str(case_id)},
        )
        assert len(intents) == 2, f"expected the link and its resend, got {intents}"
        assert str(intents[1][0]) == CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value
        assert str(intents[1][1]) == "CONFIRMED", (
            f"the follow-up did not confirm, so no resend reached the customer: {intents[1]}"
        )
        # Join 5. One creation for the whole case, and the resend aimed at *that* link.
        assert fake.calls_for("create_payment_link") == creates, (
            "the follow-up created a second payment link instead of resending the first; the "
            "customer would then hold two payable URLs for one debt"
        )
        assert fake.notify_call_count_for(link_id, NotifyMedium.SMS) == 1, (
            f"expected exactly one resend against {link_id}, got "
            f"{[call.arguments for call in fake.calls_for('notify_by')]}"
        )

        # -- the read declares the recovery, and the promise is kept -------------------
        assert case_state(installed_engine, case_id) == CaseState.RECOVERED.value
        outcome = _one(
            installed_engine,
            "SELECT classification, recovered_amount, verified_by_read_id FROM recovery_outcome "
            "WHERE case_id = :c",
            {"c": str(case_id)},
        )
        assert str(outcome[0]) == OutcomeClass.OBSERVED.value
        assert int(outcome[1]) == FOLLOW_UP_PATH_AMOUNT
        assert outcome[2] is not None, (
            "a recovery with no backing provider read is a recovery declared from a webhook"
        )

        kept = _one(
            installed_engine,
            "SELECT status, kept_at, seconds_promise_to_payment, promise_date, missed_at "
            "FROM promise_to_pay WHERE case_id = :c",
            {"c": str(case_id)},
        )
        assert str(kept[0]) == PromiseStatus.KEPT.value, (
            f"the promise did not end KEPT: {kept}. See this test's docstring on the ordering "
            "that makes a confirmed follow-up followed by a not-captured read a MISSED promise"
        )
        assert kept[4] is None, "a kept promise must carry no missed instant"
        seconds = kept[2]
        assert isinstance(seconds, int), (
            f"seconds_promise_to_payment is {type(seconds).__name__}; R23.C10's interval is a "
            "signed count of whole seconds and never a float duration"
        )
        assert seconds == int((kept[1] - kept[3]).total_seconds()), (
            "the recorded interval does not match the two instants recorded beside it"
        )

        # -- the capture webhook arrives, and is absorbed ------------------------------
        reads_before = len(fake.calls_for("fetch_payment"))
        capture_event = f"evt_{uuid.uuid4().hex[:16]}"
        assert (
            deliver(
                client,
                tenant.slug,
                captured_payment_body(payment_id, capture_event, amount=FOLLOW_UP_PATH_AMOUNT),
                capture_event,
            )
            == 200
        )
        drain(fake)
        assert len(fake.calls_for("fetch_payment")) == reads_before, (
            "a capture signal for an already-recovered case read the provider again; the "
            "monitor's short-circuit is what makes a duplicate delivery free"
        )
        assert (
            len(
                _rows(
                    installed_engine,
                    "SELECT id FROM recovery_outcome WHERE case_id = :c",
                    {"c": str(case_id)},
                )
            )
            == 1
        ), "a case must not recover twice"

        # -- the learning loop closed, and the metrics counted it ----------------------
        observation = _one(
            installed_engine,
            "SELECT outcome_class, intervention_status, selected_action FROM memory_observation "
            "WHERE case_id = :c",
            {"c": str(case_id)},
        )
        assert str(observation[0]) == OutcomeClass.OBSERVED.value
        assert str(observation[1]) == InterventionStatus.REVORA_INTERVENED.value
        auth = _fresh_session(client, tenant)
        summary = client.get("/metrics/summary", headers=auth)
        assert summary.status_code == 200, summary.text
        report = summary.json()["report"]
        assert report["recovered_case_count"] == 1
        assert report["observed_recovered_revenue"]["minor"] == FOLLOW_UP_PATH_AMOUNT
        assert report["incremental_recovered_revenue"]["status"] == "NOT_ESTABLISHED"

        # -- the nine stages, from the reader's side ----------------------------------
        projected = client.get(f"/cases/{case_id}/timeline", headers=auth).json()
        assert projected["available"] is True, projected
        timeline = projected["timeline"]
        assert timeline["stage_count"] == 9
        stages = {str(s["stage"]): str(s["status"]) for s in timeline["stages"]}
        assert [str(s["stage"]) for s in timeline["stages"]] == [
            stage.value for stage in STAGE_ORDER
        ], "the nine stages are not presented in the declared order"
        assert stages == {
            stage.value: TimelineStageStatus.DONE.value for stage in STAGE_ORDER
        }, (
            "a loop that ran every stage must project all nine DONE; anything else is a stage "
            f"whose completing record the projection did not find: {stages}"
        )
        # Named individually for the two the customer loop owns, so a failure above says which.
        assert stages[TimelineStage.CUSTOMER_RESPONDED.value] == TimelineStageStatus.DONE.value
        assert stages[TimelineStage.OUTCOME_VERIFIED.value] == TimelineStageStatus.DONE.value
        assert timeline["audit_sequence"]["complete"] is True, timeline["audit_sequence"]

        # -- and the whole story, in one query ----------------------------------------
        trail = _explain(installed_engine, case_id)
        narrative = list(trail["narrative"])
        assert list(trail["sequence"]) == list(range(1, len(narrative) + 1)), (
            f"the per-case sequence is not gap-free: {trail['sequence']}"
        )
        assert narrative[0] == CASE_DETECTED, (
            f"the trail does not begin with the detection that opened the case: {narrative[:3]}"
        )
        assert trail["why"], "question 2 unanswered: no diagnosis payload"
        assert trail["on_what_evidence"], "question 3 unanswered: no diagnosis evidence"
        assert trail["how_confident"] is not None, "question 3 unanswered: no confidence"
        assert trail["alternatives"], "question 4 unanswered: no candidate comparison"
        assert trail["policy_rules"], "question 5 unanswered: no policy check outcomes"
        assert list(trail["actions_executed"]) == [
            CandidateAction.PAYMENT_LINK.value,
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value,
        ], f"question 6 answered wrongly: {trail['actions_executed']}"
        reads = list(trail["reads"])
        assert reads, "question 7 unanswered: no authoritative read on the record"
        assert reads[-1]["captured"] is True, reads[-1]
        assert OutcomeClass.OBSERVED.value in str(trail["classification"]), (
            f"question 8 unanswered: {trail['classification']}"
        )
        assert (
            trail["customer_said"]["values"]["delay_reason"]
            == DelayReason.SALARY_OR_CASHFLOW_TIMING.value
        ), trail["customer_said"]
        assert trail["promise"], "the promise is not answerable from the same read"
        assert RECOVERY_OBSERVATION_RECORDED in narrative, (
            "the trail does not record that the loop closed into the training set"
        )


# ---------------------------------------------------------------------------
# The restraint path
# ---------------------------------------------------------------------------


def test_restraint_is_re_decided_and_never_presented_as_an_ending(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """A case that chose to wait, reviewed, choosing to wait again — and ended nowhere (R30.C11).

    ``test_customer_loop_degradation.py::test_restraint_is_still_revisited_with_the_customer_page_gone``
    already asserts that the sweep re-decides such a case, that ``window_end_at`` never moves and
    that both surviving triggers work. None of that is repeated. What this adds is the two claims
    that file does not make:

    * **The ``CASE_REVIEWED`` record shows ``WAIT`` was chosen again.** R30.C11 asks for one
      record per completed review, and the record's whole purpose is to answer "was restraint
      re-examined" — so the assertion is on ``previous_selected_action``,
      ``new_selected_action`` and the ``selection_changed`` boolean the handler computes rather
      than leaves a reader to derive. A review that produced the same answer for a *different*
      reason, or one that recorded no answer at all, is indistinguishable from this without it.

      The task's text names ``WAIT`` as the action re-chosen. At :data:`NULL_ACTION_AMOUNT` the
      optimizer in fact selects ``DO_NOTHING`` — both null actions cost nothing, ``DO_NOTHING``
      is *definitional* at exactly zero net value, and ``WAIT``'s incremental value over the
      baseline is not positive this early in the window, so the tie goes to the definitional
      one. The assertion below is therefore "the same Null_Action was chosen again" rather than
      the literal ``WAIT``: which of the two restraint means is the optimizer's to decide, and an
      assertion naming one would have to be paid for by tuning an amount until it came out, which
      is the dishonesty the derivations on these constants exist to prevent.
    * **The case appears in no ended grouping, at any point.** Checked before the review, after
      it, and at the end, across both surfaces that present an ending: the five
      ``/metrics/unresolved`` groups and every ``/cases?state=<terminal>`` listing. R30.C12 and
      R30.C13 — restraint is a decision to revisit, and an operator shown it under ``STOPPED``
      goes and looks at something that is working.
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine, client, tenant, fake, amount=NULL_ACTION_AMOUNT
    )
    drain(fake)

    opened = _case(installed_engine, case_id)
    assert str(opened[0]) == CaseState.POLICY_CHECK.value, (
        f"expected the pipeline to choose restraint at {NULL_ACTION_AMOUNT}, got {opened[0]}"
    )
    assert opened[3] is not None, (
        "a Null_Action selection must persist a next_review_at, or choosing to wait is choosing "
        "to abandon"
    )
    cycles_before = int(opened[4])
    first_selection = str(
        _one(
            installed_engine,
            "SELECT selected_action FROM recommendation WHERE case_id = :c ORDER BY created_at",
            {"c": str(case_id)},
        )[0]
    )
    assert CandidateAction(first_selection) in NULL_ACTIONS, first_selection

    ended_at_rest = _ended_groupings(client, tenant, case_id)
    assert ended_at_rest["terminal_listings"] == [], ended_at_rest
    assert set(ended_at_rest["group_case_counts"].values()) == {0}, (
        f"a case resting on restraint is counted as unresolved revenue: {ended_at_rest}"
    )

    # The sweep reads persisted columns alone, so the due instant is moved into the past rather
    # than the clock forward — the same lever the degradation test uses, for the same reason.
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET next_review_at = now() - interval '1 minute' "
                "WHERE id = :c"
            ),
            {"c": str(case_id)},
        )
    assert sweep_due_reviews(tenant.merchant_id) == 1
    drain(fake)

    reviewed = _rows(
        installed_engine,
        "SELECT decision FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
        {"c": str(case_id), "e": CASE_REVIEWED},
    )
    assert len(reviewed) == 1, (
        f"R30.C11 asks for exactly one record per completed review; got {len(reviewed)}"
    )
    record = reviewed[0][0]
    assert record["previous_selected_action"] == first_selection, record
    assert record["new_selected_action"] == first_selection, (
        "the review changed the selection, so this case is no longer the one under test — the "
        f"claim is that restraint was chosen *again*: {record}"
    )
    assert CandidateAction(str(record["new_selected_action"])) in NULL_ACTIONS, record
    assert record["selection_changed"] is False, record
    assert record["decision_cycle_count"] > cycles_before, (
        f"the review did not spend a decision cycle: {record}"
    )
    assert record["unresolved_amount"] == NULL_ACTION_AMOUNT, (
        "R30.C10's unresolved amount must be the case's own payment_amount in minor units"
    )

    after = _case(installed_engine, case_id)
    assert str(after[0]) == CaseState.POLICY_CHECK.value, (
        f"a re-decided case that chose restraint again must rest at POLICY_CHECK: {after[0]}"
    )
    assert after[1] is None, "a case that chose to wait must hold no terminal reason"

    ended_after = _ended_groupings(client, tenant, case_id)
    assert ended_after["terminal_listings"] == [], ended_after
    assert set(ended_after["group_case_counts"].values()) == {0}, (
        f"a re-decided case that chose restraint is presented as ended: {ended_after}"
    )
    assert fake.call_count == 0, (
        f"a case that chose restraint reached the provider: {[c.operation for c in fake.calls]}"
    )


# ---------------------------------------------------------------------------
# The refusal path
# ---------------------------------------------------------------------------


def test_a_dispute_ends_the_case_and_blocks_the_next_one_on_the_same_order(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """``DISPUTES_THE_CHARGE``: escalate, revoke, suppress — and refuse the next decision cycle.

    An objection to the debt is not a payment problem, so the answer is not one more message. The
    accepted submission writes the suppression and revokes the case's tokens in its own
    transaction (R21.C1, R21.C10), the worker applies the escalation (R21.C4), and the
    *suppression* — not a consent change — is what refuses the next customer-visible action.

    **The composition here is the reach of the suppression across cases.** A dispute about one
    order suppresses ``sha256(customer_key ‖ order_id)``, and the only way to observe that is a
    second ``payment.failed`` that agrees about the order. So the second failure is delivered for
    the same order and a *different* payment, and the decision cycle it causes must produce
    ``BLOCKED`` with ``CUSTOMER_OPTED_OUT`` — check five of twelve, ahead of the window and all
    three counters, because an absolute prohibition belongs before every bound (R21.C3).

    Three negatives carry it, each with a witness:

    * **Zero further provider calls.** The fake logs every call, and the count after the dispute
      must equal the count after it — a suppressed customer contacted anyway is the one failure
      this whole requirement exists to prevent.
    * **The revoked token is dead.** The customer's next request answers 410, not 200, so the
      revocation is a fact about the credential and not only about a column.
    * **``customer_consent.opted_out`` is unchanged** (R21.C9). Objecting to one debt is not
      withdrawing consent to be contacted at all, and collapsing the two would be both wrong and
      unrecoverable — there would be no record left of which the customer said.
    """
    fake = FakeRazorpay(
        ProviderBehaviour(
            payment_statuses=(PaymentStatus.FAILED,), payment_amount=FOLLOW_UP_PATH_AMOUNT
        )
    )

    with capturing_customer_tokens() as tokens:
        first_case, _ = drive_to_case(
            installed_engine,
            client,
            tenant,
            fake,
            amount=FOLLOW_UP_PATH_AMOUNT,
            order_id=_ORDER,
        )
        drain(fake)
    assert case_state(installed_engine, first_case) == CaseState.WAITING_FOR_OUTCOME.value
    wire_token = tokens[first_case]
    calls_before_dispute = fake.call_count

    status, body = _submit(
        client,
        tenant.slug,
        wire_token,
        "delay-reason",
        {
            "delay_reason": DelayReason.DISPUTES_THE_CHARGE.value,
            "note": "This charge is not mine.",
        },
    )
    assert status == 201, body

    # -- the suppression, written in the accepting request's own transaction -------------
    customer_key = customer_key_of(installed_engine, first_case)
    expected_scope = suppression_scope_key(
        customer_key=customer_key, order_id=_ORDER, case_id=first_case
    )
    suppression = _one(
        installed_engine,
        "SELECT scope_key, hard_stop_reason, origin_case_id, customer_signal_id, released_at "
        "FROM contact_suppression WHERE merchant_id = :m",
        {"m": str(tenant.merchant_id)},
    )
    assert str(suppression[0]) == expected_scope, (
        "the persisted Suppression_Scope is not the one derived from this case's customer key "
        "and order id, so a later read of the same scope will not find it"
    )
    assert str(suppression[1]) == HardStopReason.DISPUTES_THE_CHARGE.value
    assert uuid.UUID(str(suppression[2])) == first_case
    assert suppression[4] is None, "a fresh suppression must not be released"

    # -- the token is revoked, and revoked means dead ----------------------------------
    revoked = _one(
        installed_engine,
        "SELECT revoked_at, revocation_reason FROM customer_access_token WHERE case_id = :c",
        {"c": str(first_case)},
    )
    assert revoked[0] is not None, "R21.C10: a suppressed case's tokens are revoked with it"
    assert str(revoked[1]) == TokenRevocationReason.CONTACT_SUPPRESSED.value
    status, _ = _read_page(client, tenant.slug, wire_token)
    assert status == 410, (
        f"a revoked token still reads the case (HTTP {status}); the revocation is a column and "
        "not a control"
    )

    # -- and consent is untouched ------------------------------------------------------
    consent = _rows(
        installed_engine,
        "SELECT opted_out FROM customer_consent WHERE merchant_id = :m AND customer_key = :k",
        {"m": str(tenant.merchant_id), "k": customer_key},
    )
    assert consent and not any(bool(row[0]) for row in consent), (
        "the dispute set the customer-wide opt-out; R21.C9 keeps 'I object to this debt' and "
        f"'do not contact me at all' distinguishable: {consent}"
    )

    # -- the worker applies the escalation --------------------------------------------
    drain(fake)
    ended = _case(installed_engine, first_case)
    assert str(ended[0]) == CaseState.ESCALATED.value, ended
    assert str(ended[1]) == TerminalReason.CUSTOMER_DISPUTED_CHARGE.value
    escalated = _one(
        installed_engine,
        "SELECT decision FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
        {"c": str(first_case), "e": CASE_ESCALATED},
    )[0]
    assert escalated["hard_stop_reason"] == HardStopReason.DISPUTES_THE_CHARGE.value
    assert escalated["unresolved_amount"] == FOLLOW_UP_PATH_AMOUNT, escalated

    # -- a second failure on the same order ------------------------------------------
    second_case, _ = drive_to_case(
        installed_engine,
        client,
        tenant,
        fake,
        amount=FOLLOW_UP_PATH_AMOUNT,
        order_id=_ORDER,
    )
    drain(fake)
    assert second_case != first_case, "the second failure attached to the disputed case"

    persisted = _one(
        installed_engine,
        "SELECT verdict, primary_reason FROM policy_decision WHERE case_id = :c "
        "ORDER BY evaluated_at DESC",
        {"c": str(second_case)},
    )
    assert str(persisted[0]) == PolicyVerdict.BLOCKED.value, (
        f"the cycle after a dispute produced {persisted[0]}; the suppression must refuse the "
        "next customer-visible action on the same scope"
    )
    assert str(persisted[1]) == "CUSTOMER_OPTED_OUT", persisted

    # The twelve outcomes are read through the detail endpoint rather than off the row, because
    # they are their own table and the endpoint is where an operator sees them — a verdict that
    # is right in the database and unreadable on the page is not a legible refusal.
    detail = client.get(f"/cases/{second_case}", headers=tenant.auth).json()
    decision = detail["policy_decisions"][-1]
    assert decision["verdict"] == PolicyVerdict.BLOCKED.value
    assert decision["primary_reason"] == "CUSTOMER_OPTED_OUT"
    checks = decision["checks"]
    assert len(checks) == 12, (
        f"a partially recorded evaluation is indistinguishable from one that approved: {checks}"
    )
    opt_out = next(c for c in checks if c["check_id"] == "CUSTOMER_OPTED_OUT")
    assert opt_out["outcome"] == "FAIL"
    assert "suppress" in str(opt_out).lower(), (
        "the failing check does not say the suppression is what refused it, so a reader cannot "
        f"tell this apart from a consent opt-out: {opt_out}"
    )
    assert detail["consent"]["opted_out"] is False, (
        "the detail view presents this refusal as a consent opt-out; the customer objected to one "
        "debt and did not withdraw consent (R21.C9)"
    )

    assert fake.call_count == calls_before_dispute, (
        "a suppressed customer was contacted again: "
        f"{[c.operation for c in fake.calls[calls_before_dispute:]]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(second_case)},
    ), "a blocked case must not even reserve an execution intent"

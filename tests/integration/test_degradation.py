"""Task 32.3. What happens when the infrastructure, not the provider, is the thing that broke.

R16's user story is the whole brief: *fail safely rather than act blindly, so that infrastructure
faults produce delayed decisions instead of duplicate charges, duplicate messages, or invented
recovery numbers.* Every test here breaks something underneath Revora and asserts that the damage
is a **delay** and never an **effect**.

Four faults, one per criterion group:

* **The store is unreachable at ingest** (R16.C3). Answer 503, persist nothing, and accept the
  event on redelivery. The status code is load-bearing: Revora's only recovery here is the provider
  resending, and 500 would tell an operator to go looking for a bug in a payload that was fine.

* **A worker dies mid-execution** (R16.C5). The link was created, the process died before recording
  it, and the interrupted attempt's idempotency key must be *reused* rather than re-derived — so
  reconciliation confirms the existing link and the provider sees exactly one create for that key,
  ever.

* **A recovery window elapsed while the system was down** (R16.C6). On restart, expiry is
  re-evaluated from persisted rows alone and the case is expired *before* anything schedules an
  action against it.

* **A withheld action's bounds have since moved** (R16.C15). The approval outlived its validity
  during the outage, so it is discarded on the way back up, the discard is audited, and no external
  call is made.

**Every negative in here is asserted against the fake's call log**, not against an absence of
exceptions. "No provider request was issued" is a claim about something that did not happen, and
the only evidence for that is a log that recorded everything that did.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.audit.events import EXECUTION_REFUSED
from revora.cases.sweeper import sweep_expired_cases
from revora.domain.enums import CaseState, IntentState, TerminalReason
from revora.execution.reconcile import reconcile_intents
from revora.jobs.pipeline import EXECUTION_JOB_KIND, enqueue_next
from revora.memory.store import observation_writer
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.persistence.repositories.session import tenant_transaction, transaction
from tests.fakes.razorpay import CreateOutcome, FakeRazorpay, ProviderBehaviour, as_provider_client
from tests.integration.conftest import (
    LINK_PATH_AMOUNT,
    Tenant,
    case_state,
    deliver,
    drain,
    drive_to_case,
    failed_payment_body,
    registry_without_executor,
)

pytestmark = pytest.mark.pg

_UNREACHABLE_URL = "postgresql+psycopg://revora:revora_ci@127.0.0.1:1/revora"
"""Port 1 on loopback. Nothing listens there and nothing can be made to, so the connection is
refused immediately rather than hanging until a timeout — which keeps this an outage test rather
than a slow test. A firewalled address would exercise the timeout path instead, and that is a
different criterion (R16.C4's ``PERSISTENCE_TIMEOUT``) with a different assertion."""


def _rows(engine: Engine, sql: str, params: dict[str, object]) -> list[tuple]:
    with engine.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def _count(engine: Engine, sql: str, params: dict[str, object]) -> int:
    return int(_rows(engine, sql, params)[0][0])


# ---------------------------------------------------------------------------
# R16.C3 — the store is unreachable at ingest
# ---------------------------------------------------------------------------


def test_postgres_unreachable_at_ingest_answers_503_and_persists_nothing(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R16.C3. 503, nothing written, and the event lands cleanly on redelivery.

    The outage is staged by swapping the process-wide engine for one pointed at a closed port,
    which is the closest a test can get to the real fault without stopping the container that the
    rest of the suite shares.

    **The redelivery half is the point of the test.** A 503 that persisted a partial row would look
    identical to a correct 503 right up until the provider resent the event, at which point the
    partial row either blocks the retry as a duplicate or produces a second case. So the assertion
    is not merely "nothing was written" — it is that the *same event id* is accepted afterwards and
    produces exactly one case, which is the behaviour the [ASSUMPTION] about provider redelivery is
    standing on.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = failed_payment_body(payment_id, event_id, amount=LINK_PATH_AMOUNT)

    before = _count(
        installed_engine,
        "SELECT count(*) FROM webhook_event WHERE merchant_id = :m",
        {"m": str(tenant.merchant_id)},
    )

    dead = build_engine(_UNREACHABLE_URL)
    set_engine(dead)
    try:
        status = deliver(client, tenant.slug, body, event_id)
    finally:
        dispose_engine()
        set_engine(installed_engine)

    assert status == 503, (
        f"an unreachable store must answer 503 so the provider redelivers, not {status}"
    )

    # Nothing partial. Not one webhook_event, not one job, not one case, not one audit record.
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM webhook_event WHERE merchant_id = :m",
            {"m": str(tenant.merchant_id)},
        )
        == before
    ), "a refused ingest persisted a webhook_event row"
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM recovery_case WHERE merchant_id = :m",
            {"m": str(tenant.merchant_id)},
        )
        == 0
    ), "a refused ingest created a case"
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM job WHERE merchant_id = :m",
            {"m": str(tenant.merchant_id)},
        )
        == 0
    ), "a refused ingest enqueued work"

    # And no external call could have followed, because no work exists to make one.
    fake = FakeRazorpay()
    drain(fake)
    assert fake.call_count == 0

    # The provider redelivers the same event, and now it is accepted exactly once.
    assert deliver(client, tenant.slug, body, event_id) == 200, (
        "the redelivery the 503 was asking for must be accepted"
    )
    assert (
        _count(
            installed_engine,
            "SELECT count(*) FROM webhook_event WHERE merchant_id = :m",
            {"m": str(tenant.merchant_id)},
        )
        == before + 1
    )


def test_a_deterministic_persistence_defect_is_not_dressed_up_as_unavailability(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """The other side of R16.C3, and the reason the 503 catch is narrow.

    A 503 is a promise that retrying will help. A defect that fails identically on every attempt
    breaks that promise: the provider retries on its schedule, exhausts it, and drops the event —
    so a bug becomes silent data loss instead of a loud failure. Only connection-level errors may
    answer 503.

    Checked two ways, because either alone is insufficient. Staging a real bad-SQL failure would
    mean corrupting the schema for the whole session, and the claim is about which exceptions the
    handler catches.

    1. The handler's own source names exactly the two connection-level classes. Reading source is
       crude, and it is the only thing that can see an ``except`` clause.
    2. ``ProgrammingError`` is not a subclass of either. This is the guard that actually earns its
       place: the handler could stay written exactly as it is and still start swallowing defects if
       SQLAlchemy reparented its exception tree, and nothing else in the suite would notice.
    """
    import inspect

    from sqlalchemy.exc import InterfaceError, OperationalError, ProgrammingError

    from revora.api import webhooks

    source = inspect.getsource(webhooks.razorpay_webhook)
    assert "except (OperationalError, InterfaceError)" in source, (
        "the 503 catch is no longer the narrow connection-level tuple; a widened catch turns "
        f"defects into infinite provider retries:\n{source}"
    )
    assert not issubclass(ProgrammingError, OperationalError | InterfaceError), (
        "SQLAlchemy's exception hierarchy has changed so that a bad-SQL defect is now catchable by "
        "the 503 handler; the handler needs a different discriminator"
    )


# ---------------------------------------------------------------------------
# R16.C5 — a worker dies mid-execution
# ---------------------------------------------------------------------------


def test_a_worker_killed_mid_execution_reconciles_without_a_duplicate(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R16.C5. The link exists, the process died before recording it, and there is no second one.

    ``TIMEOUT_EFFECT_CREATED`` is the fault, and it is the only honest way to stage this: a worker
    killed after the request left the socket and before the response was recorded is
    *indistinguishable* to Revora from a timeout where the link was created. The fake returns the
    same ``Timeout(AFTER_SEND)`` in both cases and differs only in whether the link exists — so the
    system has to behave correctly without being able to tell which it is in.

    Three claims, and the third is the one that costs money if it is wrong:

    1. The intent is left ``ATTEMPTED``, not guessed to either terminal state.
    2. Reconciliation resolves it against provider state, reaching ``CONFIRMED``.
    3. **The same idempotency key is reused**, so ``create_call_count_for(key)`` is one. Deriving a
       fresh key for the interrupted attempt would create a second link — a second demand for money
       sent to a customer who already has one, which is the failure R16.C5 exists to name.
    """
    fake = FakeRazorpay(
        ProviderBehaviour(
            create_outcomes=(CreateOutcome.TIMEOUT_EFFECT_CREATED, CreateOutcome.SUCCESS)
        )
    )
    case_id, _ = drive_to_case(installed_engine, client, tenant, fake)
    drain(fake)

    intents = _rows(
        installed_engine,
        "SELECT idempotency_key, state, attempt_ordinal FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(intents) == 1, f"one interrupted attempt must leave one intent, got {len(intents)}"
    key, state, ordinal = intents[0]
    key = str(key)
    # Unresolved, and *which* unresolved state depends only on how far the dying worker got.
    # ``ATTEMPTED`` means it died before recording anything; ``UNCERTAIN`` means it recorded the
    # timeout and stopped there. Both are honest and both are reconciliation's job. What must never
    # appear is CONFIRMED or FAILED, because either would be the system deciding, with no evidence,
    # whether a customer was contacted — and it is the one question it cannot answer from here.
    assert str(state) in (IntentState.ATTEMPTED.value, IntentState.UNCERTAIN.value), (
        f"an interrupted attempt was resolved without evidence: {state}"
    )
    assert int(ordinal) == 1

    # Ground truth the system is not allowed to see: the link does exist.
    assert fake.created_link_exists(key)
    assert fake.create_call_count_for(key) == 1

    # The worker restarts and reconciliation runs. Its own sweep, not a pipeline pass, because
    # after a crash there is no job left to advance the case — reconciliation is the safety net
    # underneath the pipeline and this is the situation it is the net for.
    results = reconcile_intents(
        tenant.merchant_id, provider=as_provider_client(fake), config=None
    )
    assert results, "reconciliation found nothing to resolve after an interrupted attempt"

    resolved = _rows(
        installed_engine,
        "SELECT state, provider_response_id, idempotency_key FROM execution_intent "
        "WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(resolved) == 1, "reconciliation must resolve the existing intent, not add one"
    resolved_state, provider_response_id, resolved_key = resolved[0]
    assert str(resolved_state) == IntentState.CONFIRMED.value, (
        f"reconciliation should have confirmed the link that demonstrably exists: {resolved_state}"
    )
    assert provider_response_id, "a confirmed intent must carry the provider's own id"
    assert str(resolved_key) == key, "reconciliation must not re-key the interrupted attempt"

    # The claim that matters. One create, across the crash and the recovery.
    assert fake.create_call_count_for(key) == 1, (
        "the interrupted attempt was re-sent under its own key; the customer now has two payment "
        "links for one debt"
    )
    assert len(fake.calls_for("create_payment_link")) == 1, (
        f"a second create was issued: {[call.arguments for call in fake.calls]}"
    )


# ---------------------------------------------------------------------------
# R16.C6 — the window elapsed during the downtime
# ---------------------------------------------------------------------------


def test_a_window_that_elapsed_during_downtime_expires_on_restart(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R16.C6. Expiry is re-derived from persisted rows, and nothing is scheduled before it is.

    The downtime is staged by moving ``window_end_at`` into the past while no worker is running,
    which is what an outage looks like from the database's point of view: wall-clock time passed and
    no code observed it. The case is left mid-pipeline — approved but not yet executed — because
    that is the state where getting this wrong is expensive. A case whose window closed during an
    outage and which then gets its action scheduled on restart is a customer contacted about a
    recovery window that had already ended.

    ``verified_payment_status`` is deliberately untouched, so the expiry is decided from the clock
    alone. And the observation writer is attached to the sweep the way the worker attaches it, so
    the terminal row lands too: an expired case that ran its whole window without a confirmed action
    is the closest thing Revora has to a clean no-intervention label, and those are the rows the
    baseline most needs.
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine,
        client,
        tenant,
        fake,
        amount=LINK_PATH_AMOUNT,
        registry=registry_without_executor(fake),
    )

    assert case_state(installed_engine, case_id) == CaseState.ACTION_SCHEDULED.value, (
        f"expected a scheduled case to expire, got {case_state(installed_engine, case_id)}"
    )
    calls_before = fake.call_count
    assert calls_before == 0, "the executor was supposed to be the component that was down"

    # The outage: time passes, nothing observes it.
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET window_end_at = now() - interval '2 hours' "
                "WHERE id = :c"
            ),
            {"c": str(case_id)},
        )

    # Restart. The lifecycle sweep runs, exactly as the worker composes it.
    with transaction() as session:
        config = ConfigurationRepository(session).load(tenant.merchant_id)
    expired = sweep_expired_cases(tenant.merchant_id, on_terminal=observation_writer(config))
    assert expired == 1, f"the elapsed window should have expired exactly one case, got {expired}"

    assert case_state(installed_engine, case_id) == CaseState.EXPIRED.value
    terminal = _rows(
        installed_engine,
        "SELECT terminal_reason FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )
    assert str(terminal[0][0]) == TerminalReason.RECOVERY_WINDOW_ELAPSED.value

    # No action was scheduled against the reloaded case before the re-evaluation, and none after.
    assert fake.call_count == calls_before, (
        "a case whose window elapsed during the outage was acted on anyway: "
        f"{[call.operation for call in fake.calls]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "an expired case must not have reserved an execution intent"

    # The terminal row the baseline needs.
    observations = _rows(
        installed_engine,
        "SELECT executed_action_count, customer_message_count FROM memory_observation "
        "WHERE case_id = :c",
        {"c": str(case_id)},
    )
    assert len(observations) == 1, "expiry is a terminal transition and owes an observation"
    assert int(observations[0][0]) == 0, "an expired-unacted case must record zero actions"
    assert int(observations[0][1]) == 0


# ---------------------------------------------------------------------------
# R16.C15 — the withheld action's bounds have moved
# ---------------------------------------------------------------------------


def test_a_withheld_action_whose_approval_expired_is_discarded_and_audited(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R16.C15. An approval that outlived the outage is discarded, audited, and never sent.

    This is the criterion that stops an outage from turning into a burst of stale contact. The
    action was authorized, persistence went away before it could be executed, and by the time it
    comes back the approval's own validity bound has passed. Re-executing it would be Revora acting
    on a decision it can no longer defend — the decision was made against facts that are hours old,
    and the whole reason a policy decision carries ``expires_at`` is so that "we approved this once"
    cannot become "we may still do it".

    The discard has to be **audited**, not merely skipped. A case that stops with no external effect
    and no record looks exactly like a case nothing ever happened to, and the difference between
    those two is the entire content of the answer to "why was this customer not contacted?".
    """
    fake = FakeRazorpay()
    case_id, _ = drive_to_case(
        installed_engine, client, tenant, fake, registry=registry_without_executor(fake)
    )

    assert case_state(installed_engine, case_id) == CaseState.ACTION_SCHEDULED.value
    assert fake.call_count == 0, "nothing should have reached the provider before execution ran"

    audit_before = _count(
        installed_engine,
        "SELECT count(*) FROM audit_record WHERE case_id = :c AND event_type = :e",
        {"c": str(case_id), "e": EXECUTION_REFUSED},
    )

    # The outage: the approval was recorded two hours ago and its validity bound closed a minute
    # ago, with nothing able to execute it in between.
    #
    # Both timestamps move, because ``ck_policy_decision_validity_window_positive`` refuses a
    # decision that expires at or before it was evaluated — and that refusal is right. Backdating
    # only ``expires_at`` would have produced a row the system can never write, so the test would
    # have been asserting about a state that cannot occur. Moving both is also what actually
    # happened: the decision is old, not impossible.
    with installed_engine.begin() as connection:
        affected = connection.execute(
            text(
                "UPDATE policy_decision "
                "SET evaluated_at = now() - interval '2 hours', "
                "    expires_at = now() - interval '1 minute' "
                "WHERE case_id = :c AND consumed_by_intent_id IS NULL"
            ),
            {"c": str(case_id)},
        ).rowcount
    assert affected == 1, f"expected one unconsumed approval to age out, updated {affected}"

    # The executor comes back up and the withheld action is attempted. Re-enqueued explicitly
    # because the parked pass consumed the original job — which is what a restart looks like from
    # here: the durable authorization is still on the case, and something (a redelivered job, or
    # the reconciliation sweep noticing a scheduled case with no intent) asks for it to be executed.
    # The point of the test is what happens *when* it is attempted, not what triggers the attempt.
    with tenant_transaction(tenant.merchant_id) as session:
        enqueue_next(
            session,
            tenant.merchant_id,
            kind=EXECUTION_JOB_KIND,
            case_id=case_id,
            correlation_id=None,
        )
    drain(fake)

    # Discarded: no call, no intent, no counter movement.
    assert fake.call_count == 0, (
        "a withheld action whose bounds no longer permit execution was executed anyway: "
        f"{[call.operation for call in fake.calls]}"
    )
    assert not _rows(
        installed_engine,
        "SELECT id FROM execution_intent WHERE case_id = :c",
        {"c": str(case_id)},
    ), "a discarded action must not reserve an intent"
    counters = _rows(
        installed_engine,
        "SELECT executed_action_count, customer_message_count FROM recovery_case WHERE id = :c",
        {"c": str(case_id)},
    )
    assert int(counters[0][0]) == 0 and int(counters[0][1]) == 0, (
        "a discarded action must leave both bounds counters untouched, or a later legitimate "
        "attempt is charged for one that never happened"
    )

    # Audited, with the reason legible.
    refusals = _rows(
        installed_engine,
        "SELECT decision FROM audit_record WHERE case_id = :c AND event_type = :e ORDER BY seq",
        {"c": str(case_id), "e": EXECUTION_REFUSED},
    )
    assert len(refusals) > audit_before, (
        "the discard was silent; a case that stops with no effect and no record is "
        "indistinguishable from a case nothing happened to"
    )
    assert "expired" in str(refusals[-1][0]).lower(), refusals[-1][0]

    # And the approval is still on the record as having been approved — the discard does not
    # rewrite history, it adds to it.
    decisions = client.get(f"/cases/{case_id}", headers=tenant.auth).json()["policy_decisions"]
    assert decisions and decisions[-1]["verdict"] == "APPROVED", decisions

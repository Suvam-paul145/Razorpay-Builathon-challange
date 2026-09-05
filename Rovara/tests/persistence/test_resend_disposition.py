"""What happens to a case after a resend, against a real database and a real index.

Three claims, and none of them can be checked without Postgres.

**An ``UNCERTAIN`` resend is invisible to the reconciliation sweep.** Not skipped by a branch —
absent from the set the sweep reads. The mechanism is a partial index predicate plus the matching
``WHERE`` clause in the candidate query, and both are database behaviour: against a fake session
this file would assert that the code calls the functions it calls. The positive control is what
makes the negative assertion mean anything. A create intent in the identical state *is* claimed,
so "the sweep found nothing" is never passing because the sweep was broken.

**A rate-limited resend spends a customer-message increment.** The counter moves on the single
``ACTION_SCHEDULED -> EXECUTING`` edge, before the request, and a definitive failure does not
refund it. Asserted through the real transition writer rather than by hand-writing counters,
because the claim is about that edge and its counter effects, not about arithmetic.

**Revora's own bound binds before the provider's.** The provider documents a per-link,
per-medium resend rate limit whose magnitude is unknown, so the only defence is that
``COOLDOWN_INTERVAL`` and ``MAX_CUSTOMER_MESSAGES`` make Revora run out first. That ordering
holds only while the cooldown is the larger number, and a configuration change that inverted it
would leave the provider's limit deciding when a customer stops hearing from us. It is asserted
here so such a change fails in a test rather than in production.

**No provider call is made by anything in this file except the fake**, and the fake counts every
call it receives — which is how "no read was attempted" is provable rather than assumed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.cases.manager import apply_locked_transition
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    ExecutionEffectKind,
    IntentState,
    TerminalReason,
)
from revora.domain.keys import execution_key
from revora.execution.intents import reserve_intent
from revora.execution.reconcile import (
    promote_stale_intents,
    reconcile_intents,
    unresolved_intent_count,
)
from revora.execution.resend import (
    PROVIDER_IDENTIFIER_ABSENT,
    RESEND_RECONCILIATION_ATTEMPT_BOUND,
    ResendDisposition,
    ResendTarget,
    settle_resend_result,
)
from revora.persistence.models import ExecutionIntent, RecoveryCase
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.providers.classification import RATE_LIMITED
from revora.providers.payment_link import NotifyMedium, resend_response_id
from tests.fakes.razorpay import (
    FakeRazorpay,
    ProviderBehaviour,
    ResendOutcome,
    as_provider_client,
)
from tests.persistence.conftest import insert_policy_decision

pytestmark = pytest.mark.pg

_LINK_ID = "plink_RESEND00000001"
_SHORT_URL = "https://fake.invalid/plink/resend"
_ACTION = CandidateAction.PROMISE_TO_PAY_FOLLOW_UP
_ORDINAL = 1


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One test's universe: a case mid-execution, and the intent that authorized it."""

    merchant_id: uuid.UUID
    case_id: uuid.UUID
    idempotency_key: str
    intent_id: uuid.UUID


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """Sessions on the migrated database."""
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


def _insert_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    state: CaseState,
    executed_actions: int = 0,
    customer_messages: int = 0,
    decision_cycles: int = 1,
    window_hours: int = 168,
) -> uuid.UUID:
    """A recovery case with the counters a bounds test needs to control.

    Local rather than the conftest builder because the counters are the subject here: the whole
    difference between "returns to DECISION_PENDING" and "stops at a bound" is which numbers this
    row starts with.
    """
    case_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, detected_at, window_end_at,
                    executed_action_count, customer_message_count, decision_cycle_count,
                    created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 250000,
                    'INR', :customer_key, :detected_at, :window_end_at,
                    :executed_actions, :customer_messages, :decision_cycles, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": f"ck-{case_id}",
                "detected_at": moment,
                "window_end_at": moment + timedelta(hours=window_hours),
                "executed_actions": executed_actions,
                "customer_messages": customer_messages,
                "decision_cycles": decision_cycles,
            },
        )
    return case_id


def _reserve_and_execute(
    factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_id: uuid.UUID,
    *,
    effect_kind: ExecutionEffectKind = ExecutionEffectKind.PAYMENT_LINK_RESEND,
    ordinal: int = _ORDINAL,
    advance_to_executing: bool = True,
) -> _Scenario:
    """The engine's first transaction, reduced to what this file needs.

    The same two writers in the same order: the intent is reserved, then the case takes the one
    counter-bearing edge. Reduced rather than replaced — the resend's own execution path is task
    47's, and what these tests need is a durably committed resend intent on a case that has
    already spent its increment, which is exactly what those two calls produce.
    """
    key = execution_key(case_id, _ACTION.value, ordinal)
    with tenant_transaction(merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
        assert case is not None
        reserved = reserve_intent(
            session,
            merchant_id,
            case_id=case_id,
            policy_decision_id=decision_id,
            idempotency_key=key,
            action=_ACTION,
            attempt_ordinal=ordinal,
            effect_kind=effect_kind,
        )
        assert reserved.may_call
        if advance_to_executing:
            _result, rejection = apply_locked_transition(
                session,
                merchant_id,
                case,
                expected_version=int(case.version),
                target_state=CaseState.EXECUTING,
                reason="approved action execution",
                actor="execution_engine",
                action=_ACTION,
            )
            assert rejection is None, rejection
        assert reserved.intent_id is not None
        return _Scenario(merchant_id, case_id, key, reserved.intent_id)


@contextmanager
def _settling(
    factory: sessionmaker[Session], scenario: _Scenario
) -> Iterator[tuple[Session, RecoveryCase, ExecutionIntent]]:
    """Open the engine's second transaction: the case locked, the intent loaded."""
    with tenant_transaction(scenario.merchant_id, factory) as session:
        case = RecoveryCaseRepository(session).lock_for_update(
            scenario.merchant_id, scenario.case_id
        )
        intent = ExecutionIntentRepository(session).get_by_idempotency_key(
            scenario.merchant_id, scenario.idempotency_key
        )
        assert case is not None and intent is not None
        yield session, case, intent


def _case_row(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT state, terminal_reason, executed_action_count, customer_message_count,
                       decision_cycle_count, last_outbound_at
                FROM recovery_case WHERE id = :case_id
                """
            ),
            {"case_id": str(case_id)},
        ).one()
    return dict(row._mapping)


def _intent_row(engine: Engine, merchant_id: uuid.UUID, key: str) -> dict[str, object]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT state, effect_kind, provider_response_id, provider_short_url,
                       provider_failure_code, counter_applied, resolved_at,
                       reconciliation_attempts
                FROM execution_intent
                WHERE merchant_id = :merchant_id AND idempotency_key = :key
                """
            ),
            {"merchant_id": str(merchant_id), "key": key},
        ).one()
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# PAYMENT_LINK_RESEND now has a writer
# ---------------------------------------------------------------------------


def test_a_resend_intent_is_written_with_the_resend_effect_kind(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The column stops being theoretical.

    Until this path existed every row took the server default, so ``PAYMENT_LINK_RESEND`` was a
    value the schema permitted and nothing produced — which also made migration ``0008``'s
    refusal assertion about resend rows unreachable. ``reserve_intent`` writes the kind
    explicitly rather than leaning on a default that would be wrong here.
    """
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)

    scenario = _reserve_and_execute(factory, merchant_id, case_id, decision_id)

    row = _intent_row(owner_engine, merchant_id, scenario.idempotency_key)
    assert row["effect_kind"] == ExecutionEffectKind.PAYMENT_LINK_RESEND.value
    assert row["state"] == IntentState.ATTEMPTED.value


# ---------------------------------------------------------------------------
# 46.3 — UNCERTAIN is terminal, and the sweep cannot reach it
# ---------------------------------------------------------------------------


def test_an_uncertain_resend_escalates_once_and_no_sweep_can_see_it(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """A 5xx resend ends the case, and nothing ever reads the provider about it again.

    Six assertions, in the order the failure would matter:

    1. The case is ``ESCALATED`` carrying ``EXECUTION_RESULT_UNVERIFIABLE`` — a person owns it.
    2. The intent stays ``UNCERTAIN``. Forcing it ``FAILED`` would license another attempt under
       a new key while a message may already have reached a customer.
    3. The attempt bound recorded is zero, which is the difference from a create's six: the
       attempts would be reads that cannot answer.
    4. The reconciliation sweep's candidate query does not return the row, while an identical
       ``PAYMENT_LINK_CREATE`` intent on another case does. The predicate excludes it; the sweep
       does not skip it.
    5. ``unresolved_intent_count`` does not count it, so the stranded-intent alarm counts only
       intents that can be resolved.
    6. Running the sweep issues **zero** provider calls for it — no read, and above all no
       second notification.
    """
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    scenario = _reserve_and_execute(factory, merchant_id, case_id, decision_id)

    # A 5xx: the provider may have queued the notification and failed afterwards, and no read
    # can tell. Scripted rather than hand-built so the result is the one the client would return.
    fake = FakeRazorpay(ProviderBehaviour(resend_outcomes=(ResendOutcome.SERVER_ERROR,)))
    result = fake.notify_by(_LINK_ID, NotifyMedium.SMS)

    config = default_configuration()
    with _settling(factory, scenario) as (session, case, intent):
        settlement = settle_resend_result(
            session,
            merchant_id,
            case,
            intent,
            result,
            target=ResendTarget(_LINK_ID, NotifyMedium.SMS, short_url=_SHORT_URL),
            config=config,
        )

    assert settlement.disposition is ResendDisposition.ESCALATED_UNVERIFIABLE
    assert settlement.intent_state is IntentState.UNCERTAIN
    assert settlement.is_terminal_for_case

    case_row = _case_row(owner_engine, case_id)
    assert case_row["state"] == CaseState.ESCALATED.value
    assert case_row["terminal_reason"] == TerminalReason.EXECUTION_RESULT_UNVERIFIABLE.value

    intent_row = _intent_row(owner_engine, merchant_id, scenario.idempotency_key)
    assert intent_row["state"] == IntentState.UNCERTAIN.value
    assert intent_row["resolved_at"] is None
    assert intent_row["reconciliation_attempts"] == RESEND_RECONCILIATION_ATTEMPT_BOUND
    assert intent_row["provider_response_id"] == resend_response_id(_LINK_ID, NotifyMedium.SMS)

    # The alarm does not ring on a row nobody can act on.
    assert unresolved_intent_count(merchant_id, factory=factory) == 0

    # The positive control: same merchant, same state, a create. If this were not claimed the
    # negative assertion above would be passing because the sweep is broken.
    control_case = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    control_decision = insert_policy_decision(owner_engine, merchant_id, control_case)
    control = _reserve_and_execute(
        factory,
        merchant_id,
        control_case,
        control_decision,
        effect_kind=ExecutionEffectKind.PAYMENT_LINK_CREATE,
    )
    with tenant_transaction(merchant_id, factory) as session:
        claimed = ExecutionIntentRepository(session).claim_unresolved(
            merchant_id, started_before=now(), limit=100
        )
        claimed_keys = {row.idempotency_key for row in claimed}
    assert control.idempotency_key in claimed_keys, "the sweep's own query is broken"
    assert scenario.idempotency_key not in claimed_keys

    # And the sweep, run for real, never asks the provider anything about the resend.
    reconcile_intents(merchant_id, provider=as_provider_client(fake), factory=factory)
    assert fake.notify_call_count_for(_LINK_ID, NotifyMedium.SMS) == 1, (
        "a resend must be issued at most once, ever; nothing may re-send it"
    )
    read_keys = {
        call.arguments.get("reference_id")
        for call in fake.calls_for("find_payment_links_by_reference_id")
    }
    assert scenario.idempotency_key not in read_keys, (
        "a resend has no provider object to read, so no read may be issued for its key"
    )


def test_the_startup_promotion_also_cannot_reach_a_resend(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """``promote_stale_intents`` reads the same predicate, so it inherits the exclusion.

    Worth its own test because it is the other caller. An abandoned ``ATTEMPTED`` create is
    promoted to ``UNCERTAIN`` so a read will settle it; promoting an abandoned resend would put
    it into a set that will never settle anything, and the promotion record would suggest to a
    reader that something is coming for it. Nothing is: the case is escalated instead, by the
    engine, at the moment the classification is recorded.
    """
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    scenario = _reserve_and_execute(factory, merchant_id, case_id, decision_id)

    # Backdate the attempt well past any provider-call timeout, so staleness is not the reason
    # it is left alone.
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_intent SET attempt_started_at = :moment "
                "WHERE idempotency_key = :key"
            ),
            {"moment": now() - timedelta(days=2), "key": scenario.idempotency_key},
        )

    promoted = promote_stale_intents(merchant_id, factory=factory)

    assert scenario.intent_id not in promoted
    assert _intent_row(owner_engine, merchant_id, scenario.idempotency_key)["state"] == (
        IntentState.ATTEMPTED.value
    )


def test_the_index_predicate_carries_the_effect_kind_clause(owner_engine: Engine) -> None:
    """The exclusion is in the schema, where a code change cannot quietly remove it.

    Read from ``pg_indexes`` rather than from the model, because the model is what this codebase
    believes and the index is what the planner uses. Migration ``0008`` dropped and recreated the
    index for exactly this clause, and a future migration that rebuilt it without the clause
    would return resend rows to the scanned set — which is the one change that could produce a
    duplicate customer message.
    """
    with owner_engine.begin() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = "
                "'ix_execution_intent_unresolved'"
            )
        ).scalar_one()

    assert "effect_kind" in definition
    assert ExecutionEffectKind.PAYMENT_LINK_CREATE.value in definition
    assert ExecutionEffectKind.PAYMENT_LINK_RESEND.value not in definition


# ---------------------------------------------------------------------------
# 46.4 — a 429 spends a customer-message increment
# ---------------------------------------------------------------------------


def test_a_rate_limited_resend_spends_an_increment_and_returns_to_deciding(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The recorded deviation from R24.C12, asserted so it cannot be quietly reversed.

    The counter moved on the edge into ``EXECUTING``, before the request, and the 429 does not
    give it back. A design where a rejected attempt is free is a design where a loop against a
    rate limit burns no budget until the window closes — so the increment is spent, the intent is
    ``FAILED`` because nothing was delivered, and the case goes back to ``DECISION_PENDING`` to
    weigh the follow-up again against everything else.

    Going back to deciding is not a retry. It costs a decision cycle, the next attempt derives a
    different idempotency key, and all twelve policy checks run again — including the message
    bound this attempt just spent an increment against.
    """
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    scenario = _reserve_and_execute(factory, merchant_id, case_id, decision_id)

    after_edge = _case_row(owner_engine, case_id)
    assert after_edge["state"] == CaseState.EXECUTING.value
    assert after_edge["executed_action_count"] == 1
    assert after_edge["customer_message_count"] == 1, (
        "the customer-message counter moves on the edge into EXECUTING, before the request"
    )
    assert after_edge["last_outbound_at"] is not None

    fake = FakeRazorpay(ProviderBehaviour.resend_rate_limited())
    result = fake.notify_by(_LINK_ID, NotifyMedium.SMS)

    with _settling(factory, scenario) as (session, case, intent):
        settlement = settle_resend_result(
            session,
            merchant_id,
            case,
            intent,
            result,
            target=ResendTarget(_LINK_ID, NotifyMedium.SMS, short_url=_SHORT_URL),
            config=default_configuration(),
        )

    assert settlement.disposition is ResendDisposition.RETURNED_TO_DECISION
    assert settlement.intent_state is IntentState.FAILED

    intent_row = _intent_row(owner_engine, merchant_id, scenario.idempotency_key)
    assert intent_row["state"] == IntentState.FAILED.value
    assert intent_row["provider_failure_code"] == RATE_LIMITED
    assert intent_row["resolved_at"] is not None
    assert intent_row["counter_applied"] is True
    # The composed token is written even on failure: it is derived from the request, so it is
    # equally true whatever the provider said, and a null here would leave the row unreadable.
    assert intent_row["provider_response_id"] == resend_response_id(_LINK_ID, NotifyMedium.SMS)
    assert intent_row["provider_short_url"] == _SHORT_URL, "a resend creates no new link"

    settled = _case_row(owner_engine, case_id)
    assert settled["state"] == CaseState.DECISION_PENDING.value
    assert settled["terminal_reason"] is None
    assert settled["executed_action_count"] == 1
    assert settled["customer_message_count"] == 1, "a rejected attempt is not refunded"
    assert settled["decision_cycle_count"] == after_edge["decision_cycle_count"] + 1, (
        "re-deciding costs a decision cycle, which is what bounds the loop"
    )


def test_a_failed_resend_at_the_message_bound_stops_the_case(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """With no messages left there is nothing to decide, so the case ends instead of looping.

    ``MAX_MESSAGES_REACHED`` terminates under ``TerminalReason.MAX_ATTEMPTS_REACHED``, with the
    precise bound in the transition's reason string: ``terminal_reason`` is persisted behind a
    ``CHECK`` generated from the enum, so a member of its own would be a migration bought to make
    one rare record read slightly better.
    """
    config = default_configuration()
    case_id = _insert_case(
        owner_engine,
        merchant_id,
        state=CaseState.ACTION_SCHEDULED,
        executed_actions=config.MAX_CUSTOMER_MESSAGES - 1,
        customer_messages=config.MAX_CUSTOMER_MESSAGES - 1,
    )
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    scenario = _reserve_and_execute(
        factory, merchant_id, case_id, decision_id, ordinal=config.MAX_CUSTOMER_MESSAGES
    )

    assert _case_row(owner_engine, case_id)["customer_message_count"] == (
        config.MAX_CUSTOMER_MESSAGES
    )

    fake = FakeRazorpay(ProviderBehaviour.resend_rate_limited())
    result = fake.notify_by(_LINK_ID, NotifyMedium.EMAIL)

    with _settling(factory, scenario) as (session, case, intent):
        settlement = settle_resend_result(
            session,
            merchant_id,
            case,
            intent,
            result,
            target=ResendTarget(_LINK_ID, NotifyMedium.EMAIL),
            config=config,
        )

    assert settlement.disposition is ResendDisposition.STOPPED_AT_BOUND
    assert settlement.is_terminal_for_case
    row = _case_row(owner_engine, case_id)
    assert row["state"] == CaseState.STOPPED.value
    assert row["terminal_reason"] == TerminalReason.MAX_ATTEMPTS_REACHED.value


def test_revoras_own_bound_is_reached_before_any_plausible_provider_limit() -> None:
    """The ordering that keeps the provider's rate limit from ever being what stops a message.

    The provider documents a per-link, per-medium resend limit and not its magnitude, so this is
    the only defence available: with ``COOLDOWN_INTERVAL`` at 24 hours and
    ``MAX_CUSTOMER_MESSAGES`` at 2, Revora cannot issue two resends against one link inside a
    day, and cannot issue more than two across the case at all. A configuration change that
    inverted the ordering would hand the decision about when a customer stops hearing from us to
    an undocumented provider threshold — so it fails here, in a test that names the reason,
    rather than in production as a 429 nobody expected.
    """
    config = default_configuration()

    assert timedelta(hours=24) <= config.COOLDOWN_INTERVAL, (
        "the cooldown must be the binding constraint on how often one link is re-notified"
    )
    assert config.MAX_CUSTOMER_MESSAGES <= 2, (
        "the message cap must be the binding constraint on how many times in total"
    )
    # Outbound actions Revora can issue inside any 24-hour window, from the configuration
    # alone: one per cooldown gap, capped by the message bound.
    gaps_in_a_day = int(timedelta(hours=24) // config.COOLDOWN_INTERVAL)
    within_a_day = min(config.MAX_CUSTOMER_MESSAGES, gaps_in_a_day)
    assert within_a_day <= 1, (
        "Revora must not be able to issue two customer-visible actions against one case inside "
        "a day; if it can, the provider's undocumented limit is the thing that stops the second"
    )


def test_the_started_audit_fields_say_the_identifier_is_not_the_providers() -> None:
    """``EXECUTION_STARTED`` for a resend has to admit what it is carrying.

    A pure assertion in a ``pg`` file because it belongs beside the row it describes: the fields
    below are what makes ``provider_response_id`` on that row readable. The link URL is
    deliberately absent — a payment link URL is a bearer capability and is never an audit field.
    """
    target = ResendTarget(_LINK_ID, NotifyMedium.SMS, short_url=_SHORT_URL)

    fields = target.started_audit_fields(attempt_ordinal=_ORDINAL)

    assert fields[PROVIDER_IDENTIFIER_ABSENT] is True
    assert fields["provider_response_id"] == resend_response_id(_LINK_ID, NotifyMedium.SMS)
    assert fields["effect_kind"] == ExecutionEffectKind.PAYMENT_LINK_RESEND.value
    assert fields["medium"] == NotifyMedium.SMS.value
    assert _SHORT_URL not in str(fields)


# ---------------------------------------------------------------------------
# What "a live payment link" means, and the two things that read it
# ---------------------------------------------------------------------------


def _confirm(engine: Engine, key: str, *, link_id: str) -> None:
    """Settle an intent as the provider acknowledging a created link.

    Written by hand rather than through ``record_result`` because the subject here is the *query*,
    and what it reads is three committed columns. The create path's own settlement has its own
    tests; borrowing it would make this file's assertions depend on them.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_intent SET state = :state, provider_response_id = :link, "
                "resolved_at = now() WHERE idempotency_key = :key"
            ),
            {"state": IntentState.CONFIRMED.value, "link": link_id, "key": key},
        )


def test_only_a_confirmed_link_creation_counts_as_a_live_payment_link(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """``live_payment_link`` is the one definition of "this case already holds a link".

    It has two readers on layers that cannot see each other, and that is why it is asserted here
    against real rows rather than at either of them. The execution engine routes R24.C10's resend
    branch on it — re-notify the link the case has, create no second one — and
    :mod:`revora.estimation.candidates` withholds ``PAYMENT_LINK`` from the candidate set with
    ``LIVE_PAYMENT_LINK_EXISTS`` on the same answer. A second, subtly different notion of "live"
    is precisely how the optimizer comes to exclude a link the engine then creates anyway.

    Four negatives and two positives, and the negatives are the ones that would strand a case:

    * **no intent at all** — a case on its first cycle. ``PAYMENT_LINK`` must stay available.
    * **an ``ATTEMPTED`` creation** — nothing acknowledged, so there is no object the customer
      can pay and none to re-notify. Treating it as live would leave the case with nothing.
    * **a ``FAILED`` creation** — the same, permanently. This is also the shape of "the link went
      away": a case whose only link is not live must be able to create one, or it is stranded for
      the rest of its window with no payable object.
    * **a ``CONFIRMED`` resend** — created no object, so it is not a link the case holds. It is
      excluded by ``effect_kind``, which is also what makes the composed resend token
      unreachable through this query.

    and then a confirmed creation *is* live, and where a case holds two, the **newest** wins —
    the customer's most recent link is the one they still have, and re-notifying the older one
    would point somebody at a link they were given a replacement for.

    Expiry is deliberately not a clause. ``clamp_expire_by`` pins a link's ``expire_by`` to the
    case's ``window_end_at``, and a case whose window has closed is expired by the lifecycle
    sweeper rather than decided again — so there is no state in which a case is choosing actions
    while holding a link that has expired on the provider's side.
    """
    live_link = "plink_LIVE000000001"
    newer_link = "plink_LIVE000000002"

    def live_for(case_id: uuid.UUID) -> str | None:
        with tenant_transaction(merchant_id, factory) as session:
            intent = ExecutionIntentRepository(session).live_payment_link(merchant_id, case_id)
            return None if intent is None else str(intent.provider_response_id)

    # -- no intent at all -----------------------------------------------------------------
    bare = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    assert live_for(bare) is None, "a case with no intents cannot be holding a link"

    # -- an unacknowledged creation -------------------------------------------------------
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    attempted = _reserve_and_execute(
        factory,
        merchant_id,
        case_id,
        decision_id,
        effect_kind=ExecutionEffectKind.PAYMENT_LINK_CREATE,
    )
    assert live_for(case_id) is None, (
        "an ATTEMPTED creation was read as a live link; nothing was acknowledged, so there is "
        "no object to pay and none to re-notify"
    )

    # -- a definitively failed creation: the case must still be able to create one ---------
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_intent SET state = :state, resolved_at = now() "
                "WHERE idempotency_key = :key"
            ),
            {"state": IntentState.FAILED.value, "key": attempted.idempotency_key},
        )
    assert live_for(case_id) is None, (
        "a FAILED creation was read as a live link; a case whose link never existed would be "
        "stranded with nothing payable for the rest of its window"
    )

    # -- the same intent, acknowledged: now it is live ------------------------------------
    _confirm(owner_engine, attempted.idempotency_key, link_id=live_link)
    assert live_for(case_id) == live_link

    # -- a confirmed resend on the same case changes nothing ------------------------------
    resend = _reserve_and_execute(
        factory,
        merchant_id,
        case_id,
        insert_policy_decision(owner_engine, merchant_id, case_id),
        effect_kind=ExecutionEffectKind.PAYMENT_LINK_RESEND,
        ordinal=2,
        advance_to_executing=False,
    )
    _confirm(
        owner_engine,
        resend.idempotency_key,
        link_id=resend_response_id(live_link, NotifyMedium.SMS),
    )
    assert live_for(case_id) == live_link, (
        "a resend row won the newest-first ordering; it created no object, and its "
        "provider_response_id is a Revora-composed token that would 404 as a link id"
    )

    # -- two confirmed creations: the newest is the one the customer still holds -----------
    second = _reserve_and_execute(
        factory,
        merchant_id,
        case_id,
        insert_policy_decision(owner_engine, merchant_id, case_id),
        effect_kind=ExecutionEffectKind.PAYMENT_LINK_CREATE,
        ordinal=3,
        advance_to_executing=False,
    )
    _confirm(owner_engine, second.idempotency_key, link_id=newer_link)
    assert live_for(case_id) == newer_link

# ---------------------------------------------------------------------------
# 54.4 — the resend row of the degradation ladder, the half not covered above
# ---------------------------------------------------------------------------


def test_a_read_timeout_resend_escalates_with_zero_further_external_calls(
    owner_engine: Engine, factory: sessionmaker[Session], merchant_id: uuid.UUID
) -> None:
    """The design's resend row names *two* faults that escalate; only one was asserted.

    ``test_an_uncertain_resend_escalates_once_and_no_sweep_can_see_it`` above stages a **5xx**,
    where the provider answered and the answer was useless. This stages a **read timeout**, where
    the request left the socket and nothing ever came back — a different classification
    (``Timeout(AFTER_SEND)`` rather than ``ServerError``) reaching the same disposition, and the
    reason ``ResendOutcome`` has no ``TIMEOUT_MESSAGE_SENT``/``TIMEOUT_NOT_SENT`` pair: for a
    resend the distinction is not observable by the caller, by a later read, or by the fake. The
    system is asserted under *permanent* ignorance here rather than under a bad answer.

    Written as a companion rather than a copy: what is asserted is the classification-to-
    disposition mapping and the zero-further-calls claim, and the parts the 5xx test already owns
    — the partial index predicate, the positive control, ``unresolved_intent_count`` — are not
    repeated. Both faults collapsing onto one disposition is the point; a change that made the
    timeout ``FAILED`` instead would license a second attempt under a new key while a message may
    already have reached a customer, and only this test would catch it.

    The negatives, and there are three:

    * **The intent stays ``UNCERTAIN`` and unresolved.** ``resolved_at`` is null because nothing
      resolved it and nothing ever will.
    * **The recorded reconciliation attempt budget is zero**, not the create's six. Attempts
      would be reads that cannot answer, so spending them would be pretending.
    * **Running both reconciliation entry points issues no further call for it** — not a second
      notification, and not even a read, because a resend has no provider object to read.
    """
    case_id = _insert_case(owner_engine, merchant_id, state=CaseState.ACTION_SCHEDULED)
    decision_id = insert_policy_decision(owner_engine, merchant_id, case_id)
    scenario = _reserve_and_execute(factory, merchant_id, case_id, decision_id)

    fake = FakeRazorpay(ProviderBehaviour.resend_unverifiable())
    result = fake.notify_by(_LINK_ID, NotifyMedium.SMS)

    with _settling(factory, scenario) as (session, case, intent):
        settlement = settle_resend_result(
            session,
            merchant_id,
            case,
            intent,
            result,
            target=ResendTarget(_LINK_ID, NotifyMedium.SMS, short_url=_SHORT_URL),
            config=default_configuration(),
        )

    assert settlement.disposition is ResendDisposition.ESCALATED_UNVERIFIABLE, (
        "a read timeout on a resend must reach the same disposition a 5xx does; anything that "
        "resolved it would license a second message to a customer who may already have one"
    )
    assert settlement.intent_state is IntentState.UNCERTAIN
    assert settlement.is_terminal_for_case

    case_row = _case_row(owner_engine, case_id)
    assert case_row["state"] == CaseState.ESCALATED.value
    assert case_row["terminal_reason"] == TerminalReason.EXECUTION_RESULT_UNVERIFIABLE.value
    assert case_row["customer_message_count"] == 1, (
        "the increment was spent on the edge into EXECUTING and a message may have gone out, so "
        "an unverifiable resend is not refunded either"
    )

    intent_row = _intent_row(owner_engine, merchant_id, scenario.idempotency_key)
    assert intent_row["state"] == IntentState.UNCERTAIN.value
    assert intent_row["resolved_at"] is None
    assert intent_row["reconciliation_attempts"] == RESEND_RECONCILIATION_ATTEMPT_BOUND

    # Both entry points into the resolution machinery, run for real. Neither may touch it.
    promoted = promote_stale_intents(merchant_id, factory=factory)
    assert scenario.intent_id not in promoted
    reconcile_intents(merchant_id, provider=as_provider_client(fake), factory=factory)

    assert fake.notify_call_count_for(_LINK_ID, NotifyMedium.SMS) == 1, (
        "a resend was issued more than once; after a read timeout nothing — not a sweep, not a "
        "restart, not a further decision cycle — may send that message again"
    )
    assert fake.call_count == 1, (
        "a further external call was issued for a case the ladder says gets zero: "
        f"{[call.operation for call in fake.calls]}"
    )

"""Task 28. One state machine, eleven history-level invariants, checked after every step.

The eleven properties this file carries are all statements about *the same object's history*. Eleven
separate harnesses would each drive the system down its own narrow corridor — one that only ever
delivers events, one that only ever crashes — and the interesting failures live where those
corridors cross: an opt-out recorded between an approval and its execution, a window that closes
while a reconciliation is in flight, a duplicate delivery arriving after a case has already
recovered. So there is one machine, Hypothesis chooses the interleaving, and all eleven invariants
are asserted after every single step.

**What the rules can do**, and each one is a real entry point rather than a test-only shortcut:
deliver a signed webhook (new, duplicate, or a capture), advance the clock onto a configured
boundary, run the worker, run each of the three reconciliation and lifecycle sweeps, record an
opt-out, take and release human ownership, and restart the process. Nothing reaches into a module
to make something happen. The only substitution is the payment provider, and the only reason it is
substituted is that the alternative is charging real money.

**The clock is the interesting input.** ``tests/strategies/clocks.py`` draws steps from a catalogue
built out of the configured bounds — just under, exactly, and just over each of the cooldown, the
policy validity window, the outcome wait, the recovery window and the retention bound. A uniform
random delta would essentially never land on a boundary, and a boundary is where ``>=`` and ``>``
differ.

**The teardown is an assertion, not cleanup.** It advances the clock past the worst-case bound,
drains every sweep, and requires that *every* case has reached a terminal state. That is the
termination half of P6: a lifecycle that can leave a case alive forever is a lifecycle that leaks
cases, and no per-step invariant can catch it because at any given step "still running" is legal.

A note on cost. This is a stateful test against real Postgres running the real pipeline, so each
step is several round trips. The settings are deliberately modest and the invariant block is
deliberately built from one batch of queries per step rather than one query per invariant — the
alternative is a test so slow that it gets excluded from CI, which is the same as not having it.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.cases.sweeper import sweep_expired_cases
from revora.domain.actions import CandidateAction, is_customer_visible
from revora.domain.enums import CaseState, IntentState
from revora.domain.payment_event import PaymentStatus
from revora.domain.transitions import LEGAL_TRANSITIONS, TERMINAL_STATES
from revora.execution.reconcile import promote_stale_intents, reconcile_intents
from revora.memory.store import observation_writer
from revora.outcome.monitor import sweep_payment_state
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.persistence.repositories.session import transaction
from revora.platform import crypto
from revora.platform.clock import ManualClock, set_clock
from revora.platform.config import default_configuration
from revora.platform.logging import correlation_context
from revora.platform.secrets import SecretStore, set_secret_store
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour, as_provider_client
from tests.pg_support import insert_merchant
from tests.strategies.clocks import clock_step

pytestmark = pytest.mark.pg

_CONFIG = default_configuration()

_DASHBOARD_KEY = "lifecycle-operator-key"
_WEBHOOK_SECRET = "lifecycle-webhook-secret"

_CONTACTS: tuple[str, ...] = ("+919800000001", "+919800000002")
"""Two customers, so an opt-out recorded for one must not suppress contact for the other.

One contact would make P8 pass against an implementation that suppressed *everything* after any
opt-out, which is safe and wrong — it would silently stop recovering for every other customer."""

_AMOUNTS: tuple[int, ...] = (100_000, 2_000_000)
"""₹1,000 and ₹20,000: either side of the escalation crossover.

The priors put a human escalation above a payment link on net value from about ₹12,000, so these
two amounts drive the two different terminal routes — one through the provider, one straight to
``ESCALATED``. A single amount would leave half the state graph unvisited."""

_LEGAL_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (source.value, target.value) for source, target in LEGAL_TRANSITIONS
)
"""The transition table's own keys, as bare strings.

Derived from ``LEGAL_TRANSITIONS`` rather than restated, because a hand-written copy of the edge
list in a test is a copy that drifts — and it would drift in the direction of whatever the code
started doing, which is the opposite of what a property test is for."""
_TERMINAL_VALUES: frozenset[str] = frozenset(state.value for state in TERMINAL_STATES)
_ALL_STATES: frozenset[str] = frozenset(state.value for state in CaseState)

_CUSTOMER_VISIBLE: frozenset[str] = frozenset(
    action.value for action in CandidateAction if is_customer_visible(action)
)


# ---------------------------------------------------------------------------
# What one step reads out of the database
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Everything the eleven invariants need, from one batch of queries.

    Gathered in a batch and then asserted against in pure Python, rather than one query per
    invariant. Eleven queries per step times a few hundred steps is the difference between a test
    that runs in CI and one that gets excluded from it — and an excluded invariant is not an
    invariant.
    """

    cases: tuple[dict[str, object], ...]
    intents: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]
    audit: tuple[dict[str, object], ...]
    reads: tuple[dict[str, object], ...]
    consent: tuple[dict[str, object], ...]
    transitions: tuple[dict[str, object], ...]


@dataclass
class _Tracker:
    """Bookkeeping the database cannot answer.

    ``deliveries`` and ``sweeps`` bound the number of distinct correlation ids a case may carry
    (P13): every pipeline record inherits the correlation id of the delivery that scheduled it, and
    every sweep introduces one of its own. Counting them here is the only way to state that bound,
    because "how many independent operations have touched this case" is a fact about the test run
    rather than about any row.
    """

    deliveries: int = 0
    sweeps: int = 0
    correlation_ids: set[str] = field(default_factory=set)
    correlation_ids: set[str] = field(default_factory=set)
    event_ids: list[str] = field(default_factory=list)
    payment_ids: list[str] = field(default_factory=list)
    opted_out_keys: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


class RecoveryLifecycleMachine(RuleBasedStateMachine):
    """A whole Revora, driven through its real entry points, with time under test control."""

    def __init__(self, migrated_url: str) -> None:
        super().__init__()
        self._url = migrated_url
        self._clock = ManualClock()
        self._previous_clock = set_clock(self._clock)
        self._previous_secrets = set_secret_store(SecretStore(_Resolver()))
        crypto.reset_cached_material()

        self._engine: Engine = build_engine(migrated_url)
        set_engine(self._engine)

        self._fake = FakeRazorpay(
            # Failed on the first read, captured afterwards. The ordering is the claim: a read taken
            # immediately after a link is created must find the payment still failed, so only a
            # later read can declare a recovery.
            ProviderBehaviour(payment_statuses=(PaymentStatus.FAILED, PaymentStatus.CAPTURED))
        )
        self._merchant_id = insert_merchant(self._engine, display_name="Lifecycle machine")
        with self._engine.begin() as connection:
            self._slug = str(
                connection.execute(
                    text("SELECT slug FROM merchant WHERE id = :m"),
                    {"m": str(self._merchant_id)},
                ).scalar_one()
            )
        self._grant_consent_for_every_contact()

        self._app = create_app(verify_schema=False, serve_dashboard=False)
        self._client = TestClient(self._app)
        self._user_id = self._insert_user()
        self._token = self._mint_session()
        self._tracker = _Tracker()

    # -- setup helpers -----------------------------------------------------

    def _grant_consent_for_every_contact(self) -> None:
        """Consent on record for both customers, effective before anything happens.

        Recorded up front because ``run_once`` drains a merchant's whole queue in one pass, so the
        first worker rule runs detection *through* policy — consent recorded afterwards would be
        recorded after the decision it was meant to govern. The opt-out rule then supersedes this
        for one customer at a time, which is the transition P8 is about.
        """
        with self._engine.begin() as connection:
            for contact in _CONTACTS:
                connection.execute(
                    text(
                        """
                        INSERT INTO customer_consent (
                            merchant_id, customer_key, opted_out, source, effective_at, created_at
                        ) VALUES (:m, :ck, false, 'lifecycle', :when, :when)
                        """
                    ),
                    {
                        "m": str(self._merchant_id),
                        "ck": crypto.customer_key(contact),
                        "when": self._clock.now() - timedelta(days=1),
                    },
                )

    def _insert_user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO merchant_user (
                        id, merchant_id, email_masked, email_key, role, is_active, created_at
                    ) VALUES (:id, :m, '****ops@example.invalid', :key, 'operator', true, now())
                    """
                ),
                {"id": str(user_id), "m": str(self._merchant_id), "key": f"emailkey-{user_id}"},
            )
        return user_id

    def _mint_session(self) -> str:
        response = self._client.post(
            "/auth/sessions",
            json={"merchant_slug": self._slug},
            headers={DASHBOARD_KEY_HEADER: _DASHBOARD_KEY},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["token"])

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _operator_request(
        self, method: str, path: str, *, json_body: dict[str, object] | None = None
    ) -> int:
        """Call an operator endpoint, signing in again if the session has expired.

        **A 401 here is the system being right.** ``SESSION_LIFETIME`` is twelve hours and this
        machine routinely advances the clock by days, so a session minted at the start is genuinely
        unauthenticated by the time a later rule fires — R17.C1 requires exactly that. The first
        version of this machine asserted 201 and failed on it, which was the test asserting that a
        security bound did not exist.

        So an expired session is answered the way an operator answers it: sign in again and retry
        once. One retry, not a loop — a second 401 immediately after minting a fresh token is a real
        authentication defect and must surface rather than being papered over.
        """
        for attempt in (1, 2):
            response = self._client.request(
                method, path, headers=self._auth, json=json_body
            )
            if response.status_code != 401:
                return response.status_code
            if attempt == 1:
                self._token = self._mint_session()
        raise AssertionError(
            f"{method} {path} answered 401 with a session minted moments earlier; the session "
            "table or the digest lookup is broken"
        )

    # -- rules: inbound events --------------------------------------------

    @initialize()
    def start(self) -> None:
        """Deliver one failure so the machine never runs entirely on an empty universe."""
        self._deliver_failure(_AMOUNTS[0], _CONTACTS[0])

    @rule(amount=st.sampled_from(_AMOUNTS), contact=st.sampled_from(_CONTACTS))
    def deliver_failed_payment(self, amount: int, contact: str) -> None:
        """A new ``payment.failed``. The only way a case comes into existence."""
        self._deliver_failure(amount, contact)

    @precondition(lambda self: bool(self._tracker.event_ids))
    @rule(data=st.data())
    def redeliver_an_event(self, data: st.DataObject) -> None:
        """The same ``provider_event_id`` again (R16.C8, P4).

        A provider redelivers on any 5xx and sometimes on a timeout it caused itself, so duplicates
        are ordinary traffic rather than an attack. Nothing may change: no second case, no second
        transition, no second external call.
        """
        event_id = data.draw(st.sampled_from(self._tracker.event_ids))
        payment_id = self._tracker.payment_ids[self._tracker.event_ids.index(event_id)]
        body = _failed_body(payment_id, event_id, amount=_AMOUNTS[0], contact=_CONTACTS[0])
        # 200 either way: an already-seen delivery is *acknowledged*, not rejected, or the provider
        # would keep retrying something Revora has already stored.
        assert self._deliver(body, event_id) == 200
        self._tracker.deliveries += 1

    @precondition(lambda self: bool(self._tracker.payment_ids))
    @rule(data=st.data())
    def deliver_capture(self, data: st.DataObject) -> None:
        """A ``payment.captured`` for a payment that already failed.

        A *claim*, not evidence. It prompts an authoritative read and the read decides, which is the
        whole of R10.C1 — and this rule may fire for a payment whose case has already ended, which
        is exactly the late-capture reconciliation path.
        """
        payment_id = data.draw(st.sampled_from(self._tracker.payment_ids))
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        assert self._deliver(_captured_body(payment_id, event_id), event_id) == 200
        self._tracker.deliveries += 1

    # -- rules: time -------------------------------------------------------

    @rule(step=clock_step(_CONFIG))
    def advance_clock(self, step: tuple[str, timedelta]) -> None:
        """Move onto a configured boundary. See ``tests/strategies/clocks.py``."""
        _, delta = step
        self._clock.advance(delta)

    # -- rules: the worker and the sweeps ---------------------------------

    @rule()
    def run_worker(self) -> None:
        """Drain the job queue. Diagnosis through execution through outcome observation."""
        self._drain()

    @rule()
    def run_lifecycle_sweep(self) -> None:
        """Expire every non-terminal case whose window has closed (R16.C6).

        Composed with the observation writer exactly as the worker composes it, because expiry is a
        terminal transition and every terminal transition owes the training set a row.
        """
        with transaction() as session:
            config = ConfigurationRepository(session).load(self._merchant_id)
        with self._as_worker():
            sweep_expired_cases(self._merchant_id, on_terminal=observation_writer(config))

    @rule()
    def run_execution_reconciliation(self) -> None:
        """Resolve unresolved intents by reading. Never repeats a create."""
        with self._as_worker():
            reconcile_intents(self._merchant_id, provider=as_provider_client(self._fake))

    @rule()
    def run_outcome_reconciliation(self) -> None:
        """Re-read every case waiting on an outcome, so none depends on a webhook arriving."""
        with self._as_worker():
            sweep_payment_state(self._merchant_id, provider=as_provider_client(self._fake))

    @contextmanager
    def _as_worker(self) -> Iterator[None]:
        """Run a sweep the way the worker runs it: inside one correlation context.

        **Not cosmetic, and the first version of this machine got it wrong.** Outside any
        correlation context, ``AuditWriter`` mints a *fresh* id per record — deliberately, because a
        record with a generated id is still traceable while a dropped record is not. The real worker
        always wraps a handler in ``correlation_context(...)``, so a sweep's records share one id.
        Calling the sweeps bare made every record its own correlation, which looked exactly like the
        P13 failure it was supposed to detect. A harness that is less faithful than production
        reports differences between itself and production as defects.
        """
        with correlation_context() as correlation:
            self._tracker.sweeps += 1
            self._tracker.correlation_ids.add(correlation)
            yield

    # -- rules: operator actions ------------------------------------------

    @rule(contact=st.sampled_from(_CONTACTS))
    def record_opt_out(self, contact: str) -> None:
        """A customer asks not to be contacted (R17.C10, P8).

        Through the real endpoint, which derives ``customer_key`` the same way ingestion does — that
        derivation is what lets an opt-out govern cases that do not exist yet.
        """
        # One second first, so the opt-out's effective instant is strictly later than every action
        # that preceded it. Without this the clock is frozen, an action executed earlier in the same
        # step carries the *same* timestamp as the opt-out, and P8 cannot tell "acted before the
        # opt-out existed" (fine) from "acted after it" (a violation). Stepping the clock makes
        # equality mean the second thing only, which is what lets the invariant stay strict — see
        # `_p8_no_customer_contact_after_opt_out`.
        self._clock.advance(timedelta(seconds=1))
        status = self._operator_request(
            "POST",
            "/consent",
            json_body={"contact": contact, "opted_out": True, "source": "lifecycle-machine"},
        )
        assert status == 201, f"recording an opt-out answered {status}"
        self._tracker.opted_out_keys.add(crypto.customer_key(contact))

    @precondition(lambda self: bool(self._open_case_ids()))
    @rule(data=st.data())
    def assign_human_owner(self, data: st.DataObject) -> None:
        """An operator takes a case, which suspends automation on it (R14.C11)."""
        case_id = data.draw(st.sampled_from(self._open_case_ids()))
        # The status is not asserted: a case that reached a terminal state between the precondition
        # and here legitimately refuses ownership, and that race is ordinary rather than a fault.
        self._operator_request("POST", f"/cases/{case_id}/owner")

    @precondition(lambda self: bool(self._owned_case_ids()))
    @rule(data=st.data())
    def release_human_owner(self, data: st.DataObject) -> None:
        """And gives it back, which must let automation resume rather than stranding the case."""
        case_id = data.draw(st.sampled_from(self._owned_case_ids()))
        self._operator_request("DELETE", f"/cases/{case_id}/owner")

    # -- rules: restart ----------------------------------------------------

    @rule()
    def restart_process(self) -> None:
        """Tear the engine down and bring it back (R16.C6).

        Everything in-process is discarded — the connection pool, the app, the session — and the
        state is re-derived from persisted rows alone. ``promote_stale_intents`` is the startup step
        that routes an attempt interrupted by the previous process to a read rather than to a second
        call, which is the difference between a restart and a duplicate charge.
        """
        self._client.close()
        dispose_engine()

        self._engine = build_engine(self._url)
        set_engine(self._engine)
        promote_stale_intents(self._merchant_id)
        self._app = create_app(verify_schema=False, serve_dashboard=False)
        self._client = TestClient(self._app)
        # The session row survives a restart — it is a row, not process state — so the token is
        # still valid and re-minting would hide a bug where it was not.
        self._tracker.sweeps += 1

    # ---------------------------------------------------------------------
    # The eleven invariants
    # ---------------------------------------------------------------------

    @invariant()
    def invariants_hold(self) -> None:
        """All eleven, after every step, against one batch of reads."""
        snapshot = self._snapshot()
        self._p5_states_and_legal_pairs(snapshot)
        self._p6_nothing_after_terminal_but_verified_reconciliation(snapshot)
        self._p9_caps_and_monotonic_counters(snapshot)
        self._p10_cooldown_between_outbound_actions(snapshot)
        self._p11_confirmed_actions_inside_the_window(snapshot)
        self._p3_at_most_one_call_per_key(snapshot)
        self._p8_no_customer_contact_after_opt_out(snapshot)
        self._p7_no_action_after_a_verified_capture(snapshot)
        self._p12_audit_sequence_is_gap_free(snapshot)
        self._p13_one_correlation_id_per_delivery(snapshot)
        self._p1_every_confirmed_intent_has_an_approval(snapshot)

    # P5 -------------------------------------------------------------------

    def _p5_states_and_legal_pairs(self, snap: _Snapshot) -> None:
        """Every case is in one of the fourteen states, and every recorded move is a legal edge.

        The second half is the one that needs the audit trail: reading only the *current* state
        cannot see that a case went ``DETECTED -> RECOVERED`` without passing through anything, and
        an illegal edge is how a case skips the policy check that was supposed to authorize it.
        """
        for case in snap.cases:
            assert str(case["state"]) in _ALL_STATES, case

        for record in snap.transitions:
            previous, new = str(record["previous_state"]), str(record["new_state"])
            assert (previous, new) in _LEGAL_PAIRS, (
                f"case {record['case_id']} recorded an illegal transition {previous} -> {new}; "
                "the transition table is the only authority for what may follow what"
            )

    # P6 -------------------------------------------------------------------

    def _p6_nothing_after_terminal_but_verified_reconciliation(self, snap: _Snapshot) -> None:
        """A terminal case stays terminal, with exactly one permitted exception.

        The exception is real and necessary: a capture that arrives after the window closed must be
        able to move an ``EXPIRED`` case to ``RECOVERED``, or Revora would report money as lost
        because it arrived late. What must never happen is a terminal case moving anywhere *else* —
        that would mean the lifecycle restarted, and every bound it had already spent would be
        spendable again.
        """
        by_case: dict[str, list[dict[str, object]]] = {}
        for record in snap.transitions:
            by_case.setdefault(str(record["case_id"]), []).append(record)

        for case_id, records in by_case.items():
            ordered = sorted(records, key=lambda row: int(row["seq"]))  # type: ignore[arg-type]
            seen_terminal: str | None = None
            for record in ordered:
                previous, new = str(record["previous_state"]), str(record["new_state"])
                if seen_terminal is not None:
                    assert new == CaseState.RECOVERED.value, (
                        f"case {case_id} left terminal {seen_terminal} for {new}; only a verified "
                        "capture may reopen a terminal case, and only to RECOVERED"
                    )
                    assert previous == seen_terminal
                    seen_terminal = None  # the one permitted reconciliation, now spent
                if new in _TERMINAL_VALUES:
                    seen_terminal = new

    # P9 -------------------------------------------------------------------

    def _p9_caps_and_monotonic_counters(self, snap: _Snapshot) -> None:
        """Counters stay within their caps and never move down.

        Monotonicity is checked structurally rather than by remembering previous values: the schema
        forbids a negative counter, and a *decrement* would have to pass through a value lower than
        the number of confirmed intents backing it. So the assertion is that each counter is at
        least as large as the durable evidence for it and no larger than its cap — which a decrement
        cannot satisfy.
        """
        confirmed_by_case: dict[str, list[dict[str, object]]] = {}
        for intent in snap.intents:
            if str(intent["state"]) == IntentState.CONFIRMED.value:
                confirmed_by_case.setdefault(str(intent["case_id"]), []).append(intent)

        for case in snap.cases:
            case_id = str(case["id"])
            executed = int(case["executed_action_count"])  # type: ignore[arg-type]
            messages = int(case["customer_message_count"])  # type: ignore[arg-type]
            cycles = int(case["decision_cycle_count"])  # type: ignore[arg-type]

            assert 0 <= executed <= _CONFIG.MAX_RECOVERY_ATTEMPTS, case
            assert 0 <= messages <= _CONFIG.MAX_CUSTOMER_MESSAGES, case
            assert messages <= executed, (
                f"case {case_id} counts {messages} customer messages against {executed} executed "
                "actions; every message is an action"
            )
            assert cycles >= 0, case

            confirmed = confirmed_by_case.get(case_id, [])
            assert executed >= len(confirmed), (
                f"case {case_id} has {len(confirmed)} confirmed intents but counts {executed} "
                "executed actions; the counter went down or was never applied"
            )

    # P10 ------------------------------------------------------------------

    def _p10_cooldown_between_outbound_actions(self, snap: _Snapshot) -> None:
        """Two customer-visible actions on one case are at least a cooldown apart.

        The bound exists to stop Revora being the reason a customer stops answering, so it is about
        *customer-visible* actions specifically — two internal retries in a minute cost the customer
        nothing.
        """
        outbound: dict[str, list[datetime]] = {}
        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            if str(intent["action"]) not in _CUSTOMER_VISIBLE:
                continue
            started = intent["attempt_started_at"]
            assert isinstance(started, datetime)
            outbound.setdefault(str(intent["case_id"]), []).append(started)

        for case_id, moments in outbound.items():
            ordered = sorted(moments)
            for earlier, later in pairwise(ordered):
                assert later - earlier >= _CONFIG.COOLDOWN_INTERVAL, (
                    f"case {case_id} contacted the customer twice {later - earlier} apart, inside "
                    f"the {_CONFIG.COOLDOWN_INTERVAL} cooldown"
                )

    # P11 ------------------------------------------------------------------

    def _p11_confirmed_actions_inside_the_window(self, snap: _Snapshot) -> None:
        """No confirmed action was started after its case's recovery window closed.

        Acting outside the window is acting on a case Revora had already decided to stop working,
        and the customer has no way to know the difference — they receive a demand for a payment
        the merchant considers closed.
        """
        windows = {str(case["id"]): case["window_end_at"] for case in snap.cases}
        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            window_end = windows.get(str(intent["case_id"]))
            started = intent["attempt_started_at"]
            assert isinstance(window_end, datetime) and isinstance(started, datetime)
            assert started <= window_end, (
                f"intent {intent['idempotency_key']} was started at {started}, after its window "
                f"closed at {window_end}"
            )

    # P3 -------------------------------------------------------------------

    def _p3_at_most_one_call_per_key(self, snap: _Snapshot) -> None:
        """One create call per idempotency key, ever — counted at the provider.

        Asserted against the fake's own log rather than against the intent rows, because the rows
        are what the engine *believes*. The claim is about what the customer received, and only the
        provider side knows that.
        """
        for intent in snap.intents:
            key = str(intent["idempotency_key"])
            calls = self._fake.create_call_count_for(key)
            assert calls <= 1, (
                f"idempotency key {key} was sent to the provider {calls} times; the customer now "
                "holds more than one demand for the same debt"
            )

        keys = [str(intent["idempotency_key"]) for intent in snap.intents]
        assert len(keys) == len(set(keys)), f"duplicate idempotency keys recorded: {keys}"

    # P8 -------------------------------------------------------------------

    def _p8_no_customer_contact_after_opt_out(self, snap: _Snapshot) -> None:
        """Once a customer opts out, no customer-visible action is confirmed for them again.

        Scoped to the customer, not the case, because that is how consent is keyed — an opt-out
        governs cases that did not exist when it was recorded, and this invariant is what holds that
        claim across every case belonging to that person.

        **The comparison is strict, and ``record_opt_out`` steps the clock to earn that.** Under a
        frozen clock an action executed just before an opt-out shares its timestamp, so ``<=`` would
        have to permit equality — and equality is also what a genuine violation looks like when the
        clock has not moved. Stepping the clock by a second before recording the opt-out separates
        the legitimate case, which leaves equality meaning only one thing: contact at or after the
        instant the customer's refusal took effect.
        """
        effective: dict[str, datetime] = {}
        for row in snap.consent:
            if not bool(row["opted_out"]):
                continue
            key = str(row["customer_key"])
            moment = row["effective_at"]
            assert isinstance(moment, datetime)
            # The earliest opt-out is the binding one: a later superseding record cannot
            # retroactively permit contact that the earlier one forbade.
            if key not in effective or moment < effective[key]:
                effective[key] = moment

        if not effective:
            return

        keys_by_case = {str(case["id"]): str(case["customer_key"]) for case in snap.cases}
        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            if str(intent["action"]) not in _CUSTOMER_VISIBLE:
                continue
            customer_key = keys_by_case.get(str(intent["case_id"]))
            if customer_key is None or customer_key not in effective:
                continue
            started = intent["attempt_started_at"]
            assert isinstance(started, datetime)
            assert started < effective[customer_key], (
                f"a customer-visible action was confirmed at {started} for a customer who opted "
                f"out effective {effective[customer_key]}"
            )

    # P7 -------------------------------------------------------------------

    def _p7_no_action_after_a_verified_capture(self, snap: _Snapshot) -> None:
        """Once a read proves the payment is captured, no *new* action may be confirmed.

        The exception is an intent that was already in flight, which is flagged ``is_post_payment``
        and counted as an unnecessary action. That flag is not an excuse — it is how the cost of the
        race gets reported rather than hidden. A confirmed action after a verified capture with the
        flag *unset* would mean Revora chased a customer who had already paid and did not notice.
        """
        first_capture: dict[str, datetime] = {}
        for read in snap.reads:
            if not bool(read["captured"]):
                continue
            case_id = str(read["case_id"])
            moment = read["read_at"]
            assert isinstance(moment, datetime)
            if case_id not in first_capture or moment < first_capture[case_id]:
                first_capture[case_id] = moment

        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            captured_at = first_capture.get(str(intent["case_id"]))
            if captured_at is None:
                continue
            started = intent["attempt_started_at"]
            assert isinstance(started, datetime)
            if started > captured_at:
                assert bool(intent["is_post_payment"]), (
                    f"intent {intent['idempotency_key']} was confirmed at {started}, after a "
                    f"captured read at {captured_at}, without being flagged post-payment"
                )

    # P12 ------------------------------------------------------------------

    def _p12_audit_sequence_is_gap_free(self, snap: _Snapshot) -> None:
        """Per case, the audit sequence is exactly ``1..n`` with no duplicates.

        Without this, "the full history" is a claim nobody can check, because a missing record and a
        record that never existed look identical. The sequence is allocated inside the transaction
        that writes the record and under the case's row lock, so a gap means a write was lost and a
        duplicate means the lock did not hold.
        """
        by_case: dict[str, list[int]] = {}
        for record in snap.audit:
            case_id = record["case_id"]
            if case_id is None:
                continue
            seq = record["seq"]
            assert seq is not None, f"a case-attached audit record has no sequence: {record}"
            by_case.setdefault(str(case_id), []).append(int(seq))  # type: ignore[arg-type]

        for case_id, sequences in by_case.items():
            ordered = sorted(sequences)
            assert len(ordered) == len(set(ordered)), (
                f"case {case_id} has duplicate audit sequence numbers: {ordered}"
            )
            assert ordered == list(range(1, len(ordered) + 1)), (
                f"case {case_id} has a gapped audit sequence: {ordered}"
            )

    # P13 ------------------------------------------------------------------

    def _p13_one_correlation_id_per_delivery(self, snap: _Snapshot) -> None:
        """Every record carries a correlation id, and a case accumulates no more than it should.

        The strong form — "these records all share the id of the delivery that scheduled them" —
        needs the id of each delivery, which the pipeline deliberately does not expose. So this is
        the checkable form of the same claim: no record is unattributable, and the count of distinct
        ids on a case is bounded by the number of independent operations that touched it. An
        implementation generating a fresh id per *step* would blow through that bound immediately,
        and that is the failure that makes a trail unjoinable.
        """
        by_case: dict[str, set[str]] = {}
        for record in snap.audit:
            assert record["correlation_id"] is not None, (
                f"audit record {record['event_type']} has no correlation id; the async work cannot "
                "be joined to the delivery that scheduled it"
            )
            if record["case_id"] is not None:
                by_case.setdefault(str(record["case_id"]), set()).add(str(record["correlation_id"]))

        bound = self._tracker.deliveries + self._tracker.sweeps + 1
        for case_id, ids in by_case.items():
            assert len(ids) <= bound, (
                f"case {case_id} carries {len(ids)} distinct correlation ids across "
                f"{self._tracker.deliveries} deliveries and {self._tracker.sweeps} sweeps; a "
                "distinct id per step makes the trail unjoinable"
            )

    # P1 -------------------------------------------------------------------

    def _p1_every_confirmed_intent_has_an_approval(self, snap: _Snapshot) -> None:
        """No confirmed action without an approval recorded before it.

        A foreign key, then an ordering check. The key means an intent cannot exist without naming a
        decision; the ordering means the decision was not written to justify an action already
        taken. Together they are the structural form of "Revora never acts without authority".
        """
        approvals = {str(row["id"]): row for row in snap.decisions}
        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            decision_id = intent["policy_decision_id"]
            assert decision_id is not None, (
                f"intent {intent['idempotency_key']} is CONFIRMED with no policy decision; an "
                "action was taken with no recorded authority"
            )
            decision = approvals.get(str(decision_id))
            assert decision is not None, (
                f"intent {intent['idempotency_key']} names decision {decision_id}, which does not "
                "exist"
            )
            assert str(decision["verdict"]) == "APPROVED", (
                f"intent {intent['idempotency_key']} was executed on a "
                f"{decision['verdict']} decision"
            )
            assert str(decision["selected_action"]) == str(intent["action"]), (
                f"intent {intent['idempotency_key']} executed {intent['action']} on an approval "
                f"for {decision['selected_action']}"
            )
            evaluated, started = decision["evaluated_at"], intent["attempt_started_at"]
            assert isinstance(evaluated, datetime) and isinstance(started, datetime)
            assert evaluated <= started, (
                f"intent {intent['idempotency_key']} started at {started} on an approval evaluated "
                f"at {evaluated}; the authority was recorded after the action"
            )

    # ---------------------------------------------------------------------
    # Teardown: the termination half of P6
    # ---------------------------------------------------------------------

    def teardown(self) -> None:
        """Advance past every bound, drain, and require that no case is still running.

        This is an assertion and not cleanup. No per-step invariant can catch a lifecycle that
        leaks cases, because at any given step "still running" is legal — the leak is only visible
        once time has run out and the case is *still* alive. So the clock jumps past the worst-case
        bound and the sweeps run until nothing changes.

        The provider is left scripted to answer captured, so a case waiting on an outcome resolves
        rather than being stranded by a fake that stopped cooperating. A case that cannot terminate
        even with a cooperative provider and unlimited time is a leak.

        **The clock advances inside the loop, and that is not a detail.** Draining can *create* a
        case: a webhook delivered late in the run may still be waiting for detection, and detection
        opens a case whose window starts at the current instant. Advancing time only once before the
        loop therefore leaves a brand-new case with a window that has not closed, and six sweeps
        later it is still open — which the first version of this teardown reported as a leak when it
        was an artefact of the teardown's own ordering. Advancing after each drain gives every case,
        including the ones the drain just created, more than a full window to end in.
        """
        try:
            with transaction() as session:
                config = ConfigurationRepository(session).load(self._merchant_id)

            beyond_every_bound = (
                _CONFIG.RECOVERY_WINDOW_DURATION + _CONFIG.OUTCOME_WAIT_TIMEOUT + timedelta(days=1)
            )
            for _ in range(_TEARDOWN_PASSES):
                self._drain()
                self._clock.advance(beyond_every_bound)
                with self._as_worker():
                    reconcile_intents(self._merchant_id, provider=as_provider_client(self._fake))
                    sweep_payment_state(self._merchant_id, provider=as_provider_client(self._fake))
                    sweep_expired_cases(self._merchant_id, on_terminal=observation_writer(config))
                if not self._open_case_ids():
                    break

            final = self._snapshot()
            lingering = [
                (str(case["id"]), str(case["state"]))
                for case in final.cases
                if str(case["state"]) not in _TERMINAL_VALUES
            ]
            assert not lingering, (
                f"cases still running {_TEARDOWN_PASSES} passes after every bound elapsed: "
                f"{lingering}. A lifecycle that cannot end is a lifecycle that leaks cases."
            )

            # Reported, not asserted. Several invariants — P3, P7, P10, P11 — are statements about
            # confirmed actions, and an example that produced none passes them without testing
            # anything. Per-example that is legitimate: a run of three steps may never reach
            # execution. Across a run it is not, and these events make the difference visible
            # instead of leaving a vacuous suite looking green.
            #
            # Guarded because `event` refuses to run outside a Hypothesis test, and this machine is
            # deliberately usable from a plain script when diagnosing a counterexample. Without the
            # guard that refusal would be raised *after* the assertion above had already passed,
            # turning a successful teardown into a confusing error.
            terminal_states = {str(case["state"]) for case in final.cases}
            confirmed = sum(
                1
                for intent in final.intents
                if str(intent["state"]) == IntentState.CONFIRMED.value
            )
            with suppress(InvalidArgument):
                event(f"terminal states reached: {sorted(terminal_states)}")
                event(f"confirmed intents: {'some' if confirmed else 'none'}")
                event(f"cases: {len(final.cases)}")
        finally:
            # Restored in `finally` so a failing assertion cannot leave a frozen clock, a fake
            # secret store or a disposed engine installed for every test that follows.
            self._client.close()
            dispose_engine()
            set_secret_store(self._previous_secrets)
            set_clock(self._previous_clock)
            crypto.reset_cached_material()

    # ---------------------------------------------------------------------
    # Driving and reading
    # ---------------------------------------------------------------------

    def _deliver_failure(self, amount: int, contact: str) -> None:
        payment_id = f"pay_{uuid.uuid4().hex[:16]}"
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        body = _failed_body(payment_id, event_id, amount=amount, contact=contact)
        assert self._deliver(body, event_id) == 200
        self._tracker.event_ids.append(event_id)
        self._tracker.payment_ids.append(payment_id)
        self._tracker.deliveries += 1

    def _deliver(self, body: bytes, event_id: str) -> int:
        return self._client.post(
            f"/webhooks/razorpay/{self._slug}",
            content=body,
            headers={
                "X-Razorpay-Signature": hmac.new(
                    _WEBHOOK_SECRET.encode("utf-8"), body, sha256
                ).hexdigest(),
                "X-Razorpay-Event-Id": event_id,
                "content-type": "application/json",
            },
        ).status_code

    def _drain(self) -> None:
        """Work the queue to empty, bounded.

        Rebuilt from the fake each time rather than captured once, so a behaviour scripted between
        drains takes effect. Bounded rather than ``while True``: a pipeline that fails to advance
        should fail the test instead of hanging it.
        """
        from revora.jobs.worker import build_registry, run_once

        handlers = build_registry(provider=as_provider_client(self._fake))
        for _ in range(_DRAIN_PASSES):
            if run_once("lifecycle-worker", registry=handlers) == 0:
                return

    def _open_case_ids(self) -> list[str]:
        with self._engine.begin() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    text(
                        "SELECT id FROM recovery_case WHERE merchant_id = :m "
                        "AND state <> ALL(:terminal)"
                    ),
                    {"m": str(self._merchant_id), "terminal": sorted(_TERMINAL_VALUES)},
                )
            ]

    def _owned_case_ids(self) -> list[str]:
        with self._engine.begin() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    text(
                        "SELECT id FROM recovery_case WHERE merchant_id = :m "
                        "AND human_owner_user_id IS NOT NULL"
                    ),
                    {"m": str(self._merchant_id)},
                )
            ]

    def _snapshot(self) -> _Snapshot:
        """One batch of reads. See :class:`_Snapshot` for why it is a batch."""
        params = {"m": str(self._merchant_id)}
        with self._engine.begin() as connection:

            def rows(sql: str) -> tuple[dict[str, object], ...]:
                return tuple(
                    dict(row) for row in connection.execute(text(sql), params).mappings()
                )

            audit = rows(
                "SELECT case_id, seq, event_type, correlation_id, previous_state, new_state "
                "FROM audit_record WHERE merchant_id = :m"
            )
            return _Snapshot(
                cases=rows(
                    "SELECT id, state, executed_action_count, customer_message_count, "
                    "decision_cycle_count, detected_at, window_end_at, customer_key, "
                    "terminal_reason, version FROM recovery_case WHERE merchant_id = :m"
                ),
                intents=rows(
                    "SELECT id, case_id, action, state, attempt_ordinal, idempotency_key, "
                    "attempt_started_at, is_post_payment, policy_decision_id "
                    "FROM execution_intent WHERE merchant_id = :m"
                ),
                decisions=rows(
                    "SELECT id, case_id, verdict, selected_action, evaluated_at, "
                    "consumed_by_intent_id FROM policy_decision WHERE merchant_id = :m"
                ),
                audit=audit,
                reads=rows(
                    "SELECT case_id, captured, read_at, status "
                    "FROM payment_state_read WHERE merchant_id = :m"
                ),
                consent=rows(
                    "SELECT customer_key, opted_out, effective_at "
                    "FROM customer_consent WHERE merchant_id = :m"
                ),
                transitions=tuple(
                    record
                    for record in audit
                    if record["event_type"] == "STATE_TRANSITION"
                    and record["previous_state"] is not None
                    and record["new_state"] is not None
                ),
            )


# ---------------------------------------------------------------------------
# Supporting values and the pytest entry point
# ---------------------------------------------------------------------------

_DRAIN_PASSES = 24
_TEARDOWN_PASSES = 6


class _Resolver:
    """Every credential the pipeline needs, and deliberately no LLM one."""

    _FIXED: ClassVar[dict[str, str]] = {
        "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"L" * 32).decode(),
        "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"M" * 32).decode(),
        "REVORA_SESSION_TOKEN_SECRET": base64.b64encode(b"N" * 32).decode(),
        "REVORA_RAZORPAY_KEY_ID": "rzp_test_lifecycle",
        "REVORA_RAZORPAY_KEY_SECRET": "lifecycle-secret",
    }

    def get(self, name: str) -> str | None:
        if name.startswith("REVORA_DASHBOARD_KEYS_"):
            return _DASHBOARD_KEY
        if name.startswith("REVORA_WEBHOOK_SECRETS_"):
            return _WEBHOOK_SECRET
        return self._FIXED.get(name)


def _failed_body(
    payment_id: str, event_id: str, *, amount: int, contact: str
) -> bytes:
    """A verified-shape ``payment.failed``. ``insufficient_funds`` maps deterministically."""
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "created_at": 1_700_000_500,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": f"order_{event_id}",
                    "method": "card",
                    "contact": contact,
                    "email": "buyer@example.invalid",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "insufficient balance",
                    "error_reason": "insufficient_funds",
                    "error_source": "issuer_bank",
                    "error_step": "payment_authorization",
                    "created_at": 1_700_000_500,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _captured_body(payment_id: str, event_id: str) -> bytes:
    """A ``payment.captured`` — the success *signal*, never the proof."""
    payload = {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "created_at": 1_700_003_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 100_000,
                    "amount_refunded": 0,
                    "captured": True,
                    "currency": "INR",
                    "status": "captured",
                    "method": "card",
                    "created_at": 1_700_003_000,
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


_SETTINGS = settings(
    max_examples=20,
    stateful_step_count=14,
    deadline=None,
    # Every example builds its own merchant, engine and clock inside the machine's `__init__`, so
    # the session-scoped `migrated_url` fixture is read once and never mutated per example. The
    # health check cannot know that.
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
"""Deliberately modest.

Each step is several Postgres round trips plus an eleven-invariant snapshot, so this is a slow
test by construction. Eight examples of ten steps explores roughly eighty interleavings per run,
which is enough to have found the ordering bugs it was written for while staying inside a CI job's
patience. Raising either number is the right move when investigating a specific failure — and the
wrong move as a default, because a test that gets excluded for slowness enforces nothing.
"""


def test_the_recovery_lifecycle_holds_its_eleven_invariants(migrated_url: str) -> None:
    """Run the machine.

    Driven through ``run_state_machine_as_test`` rather than the ``TestCase`` attribute, because the
    machine needs the session-scoped migrated database and Hypothesis's generated ``TestCase`` takes
    no fixtures.
    """
    run_state_machine_as_test(
        lambda: RecoveryLifecycleMachine(migrated_url), settings=_SETTINGS
    )

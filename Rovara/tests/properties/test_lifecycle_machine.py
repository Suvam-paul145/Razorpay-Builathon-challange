"""Tasks 28 and 38.4. One state machine, fourteen history-level invariants, checked every step.

The fourteen properties this file carries are all statements about *the same object's history*.
Fourteen separate harnesses would each drive the system down its own narrow corridor — one that only
ever delivers events, one that only ever crashes — and the interesting failures live where those
corridors cross: an opt-out recorded between an approval and its execution, a window that closes
while a reconciliation is in flight, a duplicate delivery arriving after a case has already
recovered, a review triggered on a case that reached its cycle cap one step earlier. So there is one
machine, Hypothesis chooses the interleaving, and all fourteen invariants are asserted after every
single step.

**Task 38.4's three properties are here rather than in a machine of their own**, and the reason is
the same reason the first eleven share one: P63, P64 and P66 are claims about a case's history under
an *arbitrary interleaving of review triggers with everything else*. A second machine would have to
re-derive the clock, the provider, the merchant and the worker, and it would explore reviews against
a universe where nothing else was happening — while the failures worth finding are a review racing
an expiry, a review of a case a human has just taken, an attach arriving on a case that reached its
cap one step earlier. One model of the lifecycle, one place an invariant is checked. The cost is
that all three run in the ``pg`` tier, since this machine needs a migrated database; the design's
Testing Strategy calls them ``model``, and the honest placement is the one where they actually run.

**What the rules can do**, and each one is a real entry point rather than a test-only shortcut:
deliver a signed webhook (new, duplicate, a capture, or a second failure on a payment that already
has a case), advance the clock onto a configured boundary, run the worker, run each of the four
reconciliation, lifecycle and review sweeps, record an opt-out, take and release human ownership,
and restart the process. Nothing reaches into a module to make something happen. The only
substitution is the payment provider, and the only reason it is substituted is that the alternative
is charging real money.

**One trigger is deliberately absent, and after task 40.3 the reason has changed.**
``ReviewTrigger.CUSTOMER_SIGNAL`` now has a producer and an HTTP endpoint; what it does not have is
an obtainable credential. The wire token exists only inside ``execute_approved_action``'s
transaction and is never persisted, logged or returned (R18.C3, R18.C11), so no rule here can
authenticate as a customer without minting its own — which is the test-only entry point this
discipline forbids. The seam below says so at length, names what would close it, and points at
``tests/properties/test_review_loop.py``, where R30.C8 is driven over real HTTP in a file whose
discipline permits a fixture to mint one.

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

**Four graph assertions live at the end of the file, in the ``pure`` tier** (task 37.4). They read
``LEGAL_TRANSITIONS`` and nothing else. Three carry the half of Property 64 that is a fact about
the transition table rather than about any history: every cycle in the graph costs a decision cycle,
every edge into ``DECISION_PENDING`` supplies one, and no cycle passes through a terminal state.
They are here rather than in a file of their own because they are the static form of the same
termination claim this machine's teardown checks dynamically, and a reader who doubts the teardown
should find the proof's premises next to it.

The fourth is about reachability rather than termination: ``RECOVERED`` must be reachable from every
state a case can be sitting in when the customer pays, and every edge into it except the forward one
must require a verified capture. It sits with the others because it reads the same table, and
because it corrects a claim the other three were written alongside — see its docstring.
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
from revora.api.rendering import CASE_STATE_LABELS, WAITING_AND_WATCHING
from revora.api.views import case_summary
from revora.cases.review import CASE_REVIEW_KIND, sweep_due_reviews
from revora.cases.sweeper import sweep_expired_cases
from revora.customer.suppression import suppression_scope_key
from revora.customer.tokens import TokenService
from revora.domain.actions import NULL_ACTIONS, CandidateAction, is_customer_visible
from revora.domain.enums import (
    CaseState,
    DelayReason,
    HardStopReason,
    IntentState,
    TerminalReason,
)
from revora.domain.payment_event import PaymentStatus
from revora.domain.transitions import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    TransitionKind,
    legal_targets,
)
from revora.execution.reconcile import promote_stale_intents, reconcile_intents
from revora.memory.store import observation_writer
from revora.metrics.unresolved import UNRESOLVED_STATES, unresolved_groups
from revora.outcome.monitor import sweep_payment_state
from revora.persistence.models import RecoveryCase
from revora.persistence.repositories.cases import RecoveryCaseRepository
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

# No module-level tier marker. The stateful machine is `pg` — it takes a migrated database and runs
# the real pipeline — while the three graph assertions at the end of this file read nothing but the
# transition table and belong in the microsecond tier. A module-level `pg` would drag them into the
# slow selection and, worse, out of the one that runs on every commit.

_CONFIG = default_configuration()

_DASHBOARD_KEY = "lifecycle-operator-key"
_WEBHOOK_SECRET = "lifecycle-webhook-secret"

_CONTACTS: tuple[str, ...] = ("+919800000001", "+919800000002")
"""Two customers, so an opt-out recorded for one must not suppress contact for the other.

One contact would make P8 pass against an implementation that suppressed *everything* after any
opt-out, which is safe and wrong — it would silently stop recovering for every other customer."""

_AMOUNTS: tuple[int, ...] = (5_000, 100_000, 2_000_000)
"""₹50, ₹1,000 and ₹20,000: three amounts, three different endings.

₹1,000 and ₹20,000 sit either side of the escalation crossover — the priors put a human escalation
above a payment link on net value from about ₹12,000 — so they drive the two terminal routes, one
through the provider and one straight to ``ESCALATED``. A single amount would leave half the state
graph unvisited.

**₹50 is the review route, and it was added for task 38.4.** At that amount no intervention is worth
its cost, so the optimizer selects a Null_Action, the case rests at ``POLICY_CHECK`` carrying a
``next_review_at``, and the review rules below have something to act on. Measured rather than
assumed: with the previous two amounts the whole review loop was unreachable from this machine, so
P63, P64 and P66 would have passed on a universe containing no reviewable case — which is the
vacuous pass this file's teardown events exist to expose.

It is ``_AMOUNTS[0]`` on purpose, because :meth:`RecoveryLifecycleMachine.start` seeds that amount:
every example therefore begins with one case that will choose restraint, rather than depending on
``deliver_failed_payment`` happening to sample it."""

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

_NULL_ACTION_VALUES: frozenset[str] = frozenset(action.value for action in NULL_ACTIONS)
"""``DO_NOTHING`` and ``WAIT``, from the domain's own set rather than restated.

Two members today, and P66 is about what they mean rather than how many there are — a third null
action added tomorrow inherits the same presentation guarantee from this set."""

_UNRESOLVED_STATE_VALUES: frozenset[str] = frozenset(
    state.value for state in UNRESOLVED_STATES
)
"""The five states the unresolved-revenue grouping scans, read off the production tuple.

Read from :data:`revora.metrics.unresolved.UNRESOLVED_STATES` rather than listed, because P66's
claim is *this case is absent from that grouping* and a hand-written copy would be a claim about a
grouping this test invented."""


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
    suppressions: tuple[dict[str, object], ...]
    """Every ``contact_suppression`` row for this merchant (P39, task 42.4).

    An eighth query. It has to be a query rather than tracker state, because P39 is a claim about
    the *persisted* suppression surviving restarts — a set held in the machine's memory would be
    re-created by the restart rule and would prove the opposite of what P39 asserts.

    Both released and unreleased rows are read. The invariant only constrains the unreleased ones,
    and reading both is what lets it say so: a released suppression permits contact again, and an
    invariant that could not see the release would have to either ignore releases or forbid contact
    forever, and one of those two is wrong."""

    pending_reviews: tuple[dict[str, object], ...]
    """Unclaimed ``case_review`` jobs, by case (task 38.4).

    A seventh query, added because P64's second clause is partly about the *queue*: a review that
    finds the counter at the cap must transition the case to ``STOPPED`` and enqueue **no** decision
    cycle, and no row on ``recovery_case`` can answer whether a cycle is waiting. Reading it here
    rather than per-case keeps the invariant block one batch, which is the whole reason
    :class:`_Snapshot` exists."""


@dataclass
class _Tracker:
    """Bookkeeping the database cannot answer.

    Three counters together bound the number of distinct correlation ids a case may carry (P13),
    and each one counts a *kind of independent operation* that enters the system through its own
    correlation context:

    * ``deliveries`` — one webhook delivery. Every pipeline record the delivery schedules inherits
      its id, so a delivery contributes one id no matter how many steps it causes.
    * ``sweeps`` — one sweep or one restart, wrapped by :meth:`RecoveryLifecycleMachine._as_worker`
      exactly as the worker wraps a handler, so a sweep's records share one id of its own.
    * ``operator_requests`` — one operator HTTP request. Taking ownership of a case, giving it back,
      and recording an opt-out each arrive as a separate request and each gets its own id, because
      that is what a correlation id is for: an operator action is not part of the delivery that
      opened the case.
    * ``customer_requests`` — one operation on the public customer surface (task 42.4). A customer
      submitting a hard stop is an independent operation by exactly the same argument as an operator
      taking a case: it arrives on its own request, in its own correlation context, and it is not
      part of the delivery that opened the case. The out-of-band token mint counts too, and counts
      separately, because it is a second write with a correlation context of its own — in production
      the mint happens inside ``execute_approved_action``, which is *already* inside the delivery's
      context, so there it contributes no id and here it does. That difference is an artefact of the
      machine having to mint its own credential, and pretending otherwise would make the bound
      wrong rather than making the artefact go away.

    Counting them here is the only way to state the bound, because "how many independent operations
    have touched this case" is a fact about the test run rather than about any row.

    **``operator_requests`` was missing, and its absence was a defect in the bound rather than in
    the system.** ``assign_human_owner`` and ``release_human_owner`` go through the API in their own
    correlation contexts and increment neither of the other two counters, so three ownership
    operations against a case opened by one delivery produced four ids against a bound of three.
    That is the invariant's own docstring — *the count of distinct ids on a case is bounded by the
    number of independent operations that touched it* — not being applied to two of the kinds of
    independent operation the machine can perform. Widening the constant instead, or dropping to
    ``<= len(records)``, would have stopped the bound catching the failure it exists for.

    **``customer_requests`` was missing for the same reason and was found the same way.** Adding
    ``submit_hard_stop`` failed P13 on its first run — three ids on a case opened by one delivery,
    against a bound of two — because a customer submission and its preceding mint are two
    independent operations that incremented neither existing counter. The fix is the fourth counter
    and not a wider constant, for the reason stated above: the bound is only worth having while it
    counts operations rather than accommodating them.
    """

    deliveries: int = 0
    sweeps: int = 0
    operator_requests: int = 0
    customer_requests: int = 0
    correlation_ids: set[str] = field(default_factory=set)
    event_ids: list[str] = field(default_factory=list)
    payment_ids: list[str] = field(default_factory=list)
    opted_out_keys: set[str] = field(default_factory=set)

    windows: dict[str, datetime] = field(default_factory=dict)
    """The first ``window_end_at`` this machine ever observed per case (P63, task 38.4).

    R2.C5 and R30.C2 both say the Recovery_Window end never moves, and the database cannot answer
    "did this value change" — it holds one value and no history of it. So the first observation is
    recorded here and every later step compares against it.

    First-observed is not literally the creation value, because the step that created the case may
    also have driven the pipeline several states forward before the invariant ran. P63 closes that
    gap a second way, arithmetically: ``window_end_at == detected_at + RECOVERY_WINDOW_DURATION`` is
    exactly what detection writes, so the pair of assertions pins the value to its creation formula
    *and* to stability afterwards. Either alone would leave a hole — a write that moved both columns
    consistently, or a write that happened inside the creating step."""

    reviews_enqueued: int = 0
    """How many decision cycles the review rules enqueued across this example.

    Reported as a Hypothesis event in the teardown rather than asserted. P63, P64 and P66 are
    statements about a case that chose restraint, and an example that never produced one satisfies
    them all without testing anything — which was the actual situation before ``_AMOUNTS`` gained a
    small amount, and is invisible without counting."""

    hard_stops: int = 0
    """How many hard stops the ``submit_hard_stop`` rule successfully recorded (P39, task 42.4).

    Reported at teardown, not asserted, for the reason ``reviews_enqueued`` is: **P39 is vacuous
    without one.** "Zero customer-visible actions after the hard stop" is trivially true of a run
    that never produced a hard stop, and that failure mode is invisible — the invariant passes,
    the suite is green, and nothing was tested. This machine has already been through that once
    with the review properties, which asserted nothing across 21 examples until ``start`` was
    changed to drain the queue. Counting is the cheapest defence against the second occurrence."""

    retries_in_suppressed_scope: int = 0
    """How many fresh payment failures were delivered into an already-suppressed scope (R21.C8).

    Counted separately from ``hard_stops`` because it is a different clause and it can be zero
    while hard stops are plentiful. R21.C8 is *a case created for a payment whose Suppression_Scope
    holds a suppression is blocked irrespective of its cause and recommendation*, and the only way
    to exercise it is to open a genuinely new case in a scope that is already suppressed. Without
    this rule firing, P39's "and newly created cases in that scope" clause proves nothing, and the
    clause is the one that distinguishes a scope-keyed suppression from a flag on a case row."""

    reviews_applied: int = 0
    """How many ``CASE_REVIEWED`` records exist at teardown. Also reported, not asserted.

    Distinct from ``reviews_enqueued`` on purpose: an enqueued review that no ``run_worker`` step
    ever claimed proves nothing about ``handle_review``. The gap between the two numbers is how a
    reader tells "the review path ran" from "a job was created"."""


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

        **Every operator request is counted here, per attempt rather than per call.** This is the
        one funnel every operator action passes through, so counting it here is what makes P13's
        bound cover ownership and consent as well as deliveries and sweeps. Per *attempt*, since the
        retry below is a second HTTP request with a correlation context of its own — counting the
        logical operation would leave the bound one short exactly on the steps where a session
        expired, which is a flake rather than a finding.
        """
        for attempt in (1, 2):
            self._tracker.operator_requests += 1
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
        """Deliver one failure and work it, so the machine never starts on an empty universe.

        The drain was added for task 38.4, and it is a reachability fix with a measurement behind
        it. Every property about a case that chose restraint needs a case *resting at*
        ``POLICY_CHECK``, which takes a delivery plus a worker pass plus a clock step plus a review
        sweep — four specific rules in order. With sixteen rules and fourteen steps each rule
        appears less than once per example on average, and a measured run of twenty-one examples
        produced **zero** reviews. Working the seeded delivery here removes one of the four, and it
        costs no coverage: ``deliver_failed_payment`` still creates undetected cases, so "a case
        sitting undetected while other things happen" is still explored — just not as the only
        starting state.

        ``_AMOUNTS[0]`` is the amount at which no intervention is worth its cost, so what this
        leaves behind is one case at ``POLICY_CHECK`` carrying a ``next_review_at``: the exact
        object P63, P64 and P66 are about.
        """
        self._deliver_failure(_AMOUNTS[0], _CONTACTS[0])
        self._drain()

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
    @rule(data=st.data(), amount=st.sampled_from(_AMOUNTS))
    def attach_event(self, data: st.DataObject, amount: int) -> None:
        """A *second* failure on a payment that already has an open case (R30.C7, task 38.4).

        The same ``provider_payment_id`` under a **new** ``provider_event_id``, which is what makes
        this an attach rather than a redelivery: ``redeliver_an_event`` repeats the event id and is
        deduplicated at the webhook boundary, so it never reaches detection and can never attach
        anything. This one does reach detection, finds the open case, writes
        ``EVENT_ATTACHED_TO_CASE``, and — where that case is resting at ``POLICY_CHECK`` — enqueues
        one decision cycle in the same transaction.

        **This is the only trigger that can reach R30.C10.** The Review_Sweeper's query excludes
        cases already at ``MAX_RECOVERY_ATTEMPTS`` and ``_enqueue_one`` re-checks the same bound
        under the lock, so a capped case is never enqueued by a sweep and ``handle_review``'s
        ``at_cap`` branch is unreachable that way. An attach carries no cap check — deliberately,
        because a new failure on a capped case still deserves a recorded conclusion rather than
        silence — so it is what drives the case to ``STOPPED`` with
        ``DECISION_CYCLE_LIMIT_REACHED``. Without this rule the second clause of P64 would pass
        vacuously on every example.

        Verified before the rule was written: attaching a fresh event to three capped cases moved
        all three to ``STOPPED``/``DECISION_CYCLE_LIMIT_REACHED`` with ``next_review_at`` cleared,
        while the sweep alone left them sitting at ``POLICY_CHECK``.

        The amount is drawn rather than fixed and it is deliberately allowed to differ from the
        case's own: R30.C7 requires the persisted ``payment_amount`` and detection timestamp to stay
        unchanged by an attach, and an attach that always carried the same amount could not tell a
        correct implementation from one that overwrote them with an identical value.
        """
        payment_id = data.draw(st.sampled_from(self._tracker.payment_ids))
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        body = _failed_body(payment_id, event_id, amount=amount, contact=_CONTACTS[0])
        assert self._deliver(body, event_id) == 200
        # Both lists, in step. `redeliver_an_event` pairs them positionally —
        # `payment_ids[event_ids.index(event_id)]` — so appending to one alone desynchronises them
        # and the redelivery rule raises `IndexError` a few steps later, which is what the first
        # version of this rule did. Appending both also makes an attached event redeliverable, and
        # that is a case worth having: a redelivered attach must not attach a second time.
        self._tracker.event_ids.append(event_id)
        self._tracker.payment_ids.append(payment_id)
        self._tracker.deliveries += 1

    @rule()
    def sweep_review(self) -> None:
        """One worker tick carrying the Review_Sweeper (R30.C5, task 38.4).

        **The sweeper itself only enqueues**, and that is asserted rather than assumed: it applies
        no transition, evaluates no policy and calls no provider, and every consequence goes through
        ``apply_transition``, which stays the only writer of ``recovery_case.state``.

        The drain after it is not a shortcut past that separation — it is what one ``run_once`` pass
        does. In production the scheduler enqueues a ``case_review`` job with no ``case_id``, the
        worker claims it, ``_handle_case_review`` calls ``sweep_due_reviews``, and the **same** pass
        keeps claiming from the same merchant's queue, so the per-case reviews the sweep just
        enqueued are served by the tick that created them. A rule that stopped at the enqueue would
        model a worker that processes one job per pass, which is not the worker Revora runs.

        The enqueue/apply *separation* is still explored, by ``attach_event``: that rule enqueues a
        review inside a webhook's transaction and leaves it for a later ``run_worker``, so
        Hypothesis can put a clock step, a restart or an opt-out in between. Keeping the separation
        here as well was measured and rejected — at fourteen steps over sixteen rules it made the
        whole review loop unreachable, and the properties would have passed on a universe containing
        no review at all.

        Composed inside ``_as_worker`` and handed that context's correlation id, exactly as
        ``_handle_case_review`` does. The id has to be passed explicitly rather than inherited:
        ``JobRepository.enqueue`` writes the column it is given and does not consult the ambient
        context, so a sweep run bare would enqueue jobs with a null correlation and each would mint
        a fresh id when the worker claimed it — the P13 failure this machine checks for, arriving
        through the test rather than through the system.
        """
        with self._as_worker() as correlation:
            self._tracker.reviews_enqueued += sweep_due_reviews(
                self._merchant_id, correlation_id=uuid.UUID(correlation)
            )
        self._drain()

    # A ``submit_signal`` rule still belongs here and is **still** deliberately absent, and after
    # task 40.3 the reason has changed — which is worth recording, because the old reason has been
    # removed and the seam has not.
    #
    # The producer now exists: ``revora.customer.signals.record_signal``, reachable over HTTP at
    # ``POST /customer/{merchant_slug}/delay-reason`` and mounted in the app this machine already
    # drives. What is missing is not the endpoint. **It is the credential.**
    #
    # A Customer_Signal write requires the wire token ``rvc_<token_id>.<secret>``, and that string
    # exists exactly once, inside ``execute_approved_action``'s first transaction, in the local
    # variable the mint returned. It is never persisted — R18.C3 forbids any reversible copy, and
    # ``secret_hash`` is an HMAC — never logged (R18.C11 permits only the ``token_id``), and never
    # returned to any caller outside that transaction. The customer receives it because the message
    # carries it; nothing else in the system can ever see it again.
    #
    # So this machine cannot obtain one through any real entry point. It could mint its own by
    # calling ``TokenService`` directly, and that is exactly the test-only entry point this file's
    # discipline exists to forbid — every rule here is something the outside world can do, and
    # "reach into the token service and mint a credential" is not. It could read the ``token_id``
    # out of ``customer_access_token``, which it is welcome to, and that is not enough to
    # authenticate.
    #
    # **What would close this seam properly:** the execution engine putting the token URL into the
    # message it sends, at which point ``tests/fakes/razorpay.py`` records it on the create request
    # and the machine can read it the way a customer reads an SMS. That is the faithful path, it is
    # the one the design describes (``https://<frontend-host>/pay/rvc_…``), and it belongs to the
    # frontend and message-content work rather than to task 40 — ``build_payment_link_request`` is
    # called *above* the mint today, so the URL is not available to it.
    #
    # Until then, R30.C8's customer trigger is driven end to end in
    # ``tests/properties/test_review_loop.py``, over real HTTP against a real case, in a file whose
    # discipline is fixture construction rather than "every rule is a real entry point". That is the
    # honest split: the trigger is covered, and it is covered where minting a token is a fixture
    # step rather than a rule.

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
    def _as_worker(self) -> Iterator[str]:
        """Run a sweep the way the worker runs it: inside one correlation context.

        Yields the id, because one sweep needs it as an *argument* rather than as ambient state:
        ``sweep_due_reviews`` writes rows into the ``job`` table, and a queued row's correlation is
        a column the enqueue is handed, not something the audit writer picks up from the context.
        Every other sweep here writes only audit records and needs the id ambient, so they ignore
        the yielded value.

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
            yield correlation

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

    # -- rules: the customer's hard stop (task 42.4) -----------------------
    #
    # These two rules are where this machine crosses its own discipline, and the crossing is
    # narrow, deliberate and worth reading before it is copied.
    #
    # Every other rule here is something the outside world can do. ``submit_hard_stop`` mints a
    # Customer_Access_Token by calling ``TokenService`` directly, which the seam note above
    # forbids — the wire token exists only inside ``execute_approved_action``'s transaction, is
    # never persisted (R18.C3) and never logged (R18.C11), so a customer credential is not
    # obtainable through any real entry point until the execution engine puts the token URL on the
    # provider request and ``tests/fakes/razorpay.py`` can record it the way a customer reads an
    # SMS. That remains the fix, and it remains somebody else's task.
    #
    # The exception is taken here rather than declined, on these grounds. The discipline exists so
    # that coverage is not faked: a rule that reaches into a module to *make the thing under test
    # happen* reports a guarantee nothing delivers. That is not this. The thing under test is P39 —
    # a suppression's permanence across subsequent events, decision cycles, restarts and new cases
    # in its scope — and none of that is the token path. The mint is setup for a different
    # property, and the token path has its own end-to-end coverage in
    # ``tests/properties/test_customer_token.py`` and ``tests/api/test_customer_surface.py``.
    # Declining the exception would not make P39 stronger; it would mean P39's stateful clause was
    # not tested at all, and it is the clause the requirement leans on hardest.
    #
    # Everything downstream of the mint is real: the submission goes over HTTP through the mounted
    # customer router, so the verification, the row lock, the submission cap, the case-signal cap,
    # the suppression write and the enqueue all run exactly as they do in production.

    @precondition(lambda self: bool(self._open_case_ids()))
    @rule(
        data=st.data(),
        reason=st.sampled_from(
            [DelayReason(member.value) for member in HardStopReason]
        ),
    )
    def submit_hard_stop(self, data: st.DataObject, reason: DelayReason) -> None:
        """A customer disputes the charge, or says they no longer want the order (R21.C1).

        Drawn from ``HardStopReason``'s two members rather than from all six ``DelayReason``s. The
        other four are payment problems and suppress nothing, so drawing them would spend most
        steps on a rule that cannot make the property it exists for non-vacuous — and R20's
        properties already cover them.

        **The clock advances one second first**, for the reason ``record_opt_out`` does it: with a
        frozen clock an action confirmed earlier in the same step carries the *same* instant as the
        suppression, and P39 could not then tell "acted before the hard stop existed", which is
        fine, from "acted after it", which is a violation. Stepping the clock makes equality mean
        only the second, which is what lets the invariant stay strict.

        The status is checked but not required to be 201. A 409 is the case having reached a
        terminal state between the precondition and here, and a 429 is a submission cap the
        customer has already spent — both ordinary, both R19's own answers. Only an accepted
        submission counts towards ``hard_stops``, because only an accepted one wrote a suppression.
        """
        case_id = uuid.UUID(data.draw(st.sampled_from(self._open_case_ids())))
        self._clock.advance(timedelta(seconds=1))

        wire = self._mint_customer_token(case_id)
        if wire is None:
            return
        # Two independent operations, counted as two: the mint above and the submission below each
        # write in their own correlation context. See `_Tracker.customer_requests` for why the mint
        # counts here and does not count in production.
        self._tracker.customer_requests += 2
        response = self._client.post(
            f"/customer/{self._slug}/delay-reason",
            headers={"Authorization": f"Bearer {wire}", "Content-Type": "application/json"},
            json={"delay_reason": reason.value},
        )
        assert response.status_code in {201, 409, 410, 429}, response.text
        if response.status_code == 201:
            self._tracker.hard_stops += 1

    @precondition(lambda self: bool(self._suppressed_scopes()))
    @rule(data=st.data())
    def deliver_retry_in_suppressed_scope(self, data: st.DataObject) -> None:
        """A fresh payment failure on an order whose scope is already suppressed (R21.C8).

        A real thing that happens: the customer disputed the charge on order X, then the same order
        is retried and that payment fails too. A brand-new ``recovery_case`` is opened — a
        different ``provider_payment_id``, so ``one_open_case_per_payment`` does not refuse it —
        and it derives the *same* Suppression_Scope, because the scope is
        ``sha256(customer_key ‖ order_id)`` and both halves are unchanged.

        This rule exists because without it P39's "and newly created cases in that scope" clause is
        unreachable, and that clause is the entire difference between a scope-keyed suppression and
        a boolean on a case row. A flag copied onto each case at creation would satisfy every other
        clause of P39 and fail this one.

        The retry is delivered and **not drained**, so whether the new case gets as far as a policy
        evaluation before the next step is Hypothesis's choice rather than this rule's.
        """
        order_id, contact = data.draw(st.sampled_from(self._suppressed_scopes()))
        self._deliver_failure(
            data.draw(st.sampled_from(_AMOUNTS)), contact, order_id=order_id
        )
        self._tracker.retries_in_suppressed_scope += 1

    # -- rules: restart ----------------------------------------------------

    @rule()
    def restart_process(self) -> None:
        """Tear the engine down and bring it back, then do what a fresh worker does (R16.C6).

        Everything in-process is discarded — the connection pool, the app, the session — and the
        state is re-derived from persisted rows alone. ``promote_stale_intents`` is the startup step
        that routes an attempt interrupted by the previous process to a read rather than to a second
        call, which is the difference between a restart and a duplicate charge.

        **The review sweep runs here too, and that is R30.C6 as a per-step claim** (task 38.4). A
        restarted worker's first tick runs its periodic sweeps, and the Review_Sweeper's whole input
        is three persisted columns — ``state``, ``next_review_at`` and ``decision_cycle_count`` — so
        a sweep on the far side of a restart must find exactly the due set it would have found
        without one. Running it in this rule rather than in a second restart rule is deliberate:
        restarts are the most expensive step in this machine, and two rules that both dispose an
        engine would double that cost to state one extra claim. This is the design's
        ``restart_worker``.

        Both startup steps run inside ``_as_worker``. They used to run bare, which meant
        ``promote_stale_intents``'s records each minted their own correlation id against a bound
        that counted the restart as one operation — tolerable while it was one sweep, and not once
        a review sweep here can enqueue a job per due case.
        """
        self._client.close()
        dispose_engine()

        self._engine = build_engine(self._url)
        set_engine(self._engine)
        with self._as_worker() as correlation:
            promote_stale_intents(self._merchant_id)
            self._tracker.reviews_enqueued += sweep_due_reviews(
                self._merchant_id, correlation_id=uuid.UUID(correlation)
            )
        self._app = create_app(verify_schema=False, serve_dashboard=False)
        self._client = TestClient(self._app)
        # The session row survives a restart — it is a row, not process state — so the token is
        # still valid and re-minting would hide a bug where it was not. `_as_worker` above already
        # counted this restart as one independent operation, which is what the trailing
        # `sweeps += 1` used to do.

    # ---------------------------------------------------------------------
    # The fourteen invariants
    # ---------------------------------------------------------------------

    @invariant()
    def invariants_hold(self) -> None:
        """All fourteen, after every step, against one batch of reads.

        The last three of the fourteen are task 38.4's. They read two extra things — the pending
        review jobs, in the same batch, and the read model for the handful of cases that are
        actively waiting — and both are skipped when the universe holds nothing they apply to.

        **P39 is the fifteenth** (task 42.4), and it reads an eighth thing: the
        ``contact_suppression`` rows. It returns immediately when none is in force, which is most
        steps, so its cost on a run that produced no hard stop is one query.
        """
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
        self._p63_the_window_never_moves_and_reviews_stay_inside_it(snapshot)
        self._p64_decision_cycles_stay_within_the_cap(snapshot)
        self._p66_a_waiting_case_is_never_presented_as_ended(snapshot)
        self._p39_no_customer_contact_after_a_hard_stop(snapshot)

    # P39 ------------------------------------------------------------------

    def _p39_no_customer_contact_after_a_hard_stop(self, snap: _Snapshot) -> None:
        """**Property 39.** A hard stop at ``T`` ends customer-visible action in its scope.

        For every Contact_Suppression still in force, no confirmed customer-visible action on any
        case in that Suppression_Scope was *initiated* after the suppression instant — across
        subsequent events, decision cycles, sweeps, restarts and cases created afterwards in the
        same scope (R21.C1, R21.C3, R21.C8).

        **The assertion is on ``attempt_started_at``, not on ``resolved_at``, and the difference is
        R21.C7 rather than a weakening.** The design's property table says "confirmation later than
        ``T``", and read literally that contradicts the requirement it is meant to check: R21.C7
        says an intent already ``ATTEMPTED`` when the suppression lands issues no *further* call and
        *resolves through the existing reconciliation path*. So a confirmation timestamp after ``T``
        for an action attempted before ``T`` is the requirement working, not a violation — the
        reconciliation sweep read the provider and found the link it had already created. What must
        never happen is a call *starting* after ``T``, and ``attempt_started_at`` is the instant the
        intent row is written, before the provider request goes out. That is the initiation instant
        and it is the one the claim is about.

        The carve-out is not taken on trust. Any confirmed intent whose resolution crossed ``T``
        must carry a ``POST_SUPPRESSION_ACTION`` record naming its idempotency key, which is
        R21.C7's other half — so an intent that quietly confirmed after a suppression with nothing
        recorded fails here rather than passing under the carve-out.

        Released suppressions are skipped. A release is a named person deciding contact may resume
        (R21.C2), so contact after one is permitted; an invariant that forbade it forever would be
        asserting something the requirement does not say.

        **Scope keys are computed with the production derivation**,
        :func:`revora.customer.suppression.suppression_scope_key`, and not with a copy of it. A
        second derivation in the test would make this property pass for a system whose writer and
        reader disagreed about the scope — which is the single most likely way a scope-keyed control
        silently stops applying.
        """
        in_force = {
            str(row["scope_key"]): _as_datetime(row["suppressed_at"])
            for row in snap.suppressions
            if row["released_at"] is None
        }
        if not in_force:
            return

        suppressed_case_ids: dict[str, datetime] = {}
        for case in snap.cases:
            order_id = case["provider_order_id"]
            scope = suppression_scope_key(
                customer_key=str(case["customer_key"]),
                order_id=None if order_id is None else str(order_id),
                case_id=uuid.UUID(str(case["id"])),
            )
            instant = in_force.get(scope)
            if instant is not None:
                suppressed_case_ids[str(case["id"])] = instant

        assert suppressed_case_ids, (
            f"{len(in_force)} suppression(s) are in force and no case maps to any of their scope "
            "keys. The writer and the reader are deriving different scopes, which would mean the "
            "policy check can never find a suppression that was persisted"
        )

        post_suppression_keys = {
            str(record["idempotency_key"])
            for record in snap.audit
            if str(record["event_type"]) == "POST_SUPPRESSION_ACTION"
            and record["idempotency_key"] is not None
        }

        for intent in snap.intents:
            if str(intent["state"]) != IntentState.CONFIRMED.value:
                continue
            if not is_customer_visible(CandidateAction(str(intent["action"]))):
                continue
            instant = suppressed_case_ids.get(str(intent["case_id"]))
            if instant is None:
                continue

            started = _as_datetime(intent["attempt_started_at"])
            assert started <= instant, (
                f"case {intent['case_id']} confirmed the customer-visible action "
                f"{intent['action']} with the attempt started at {started.isoformat()}, after a "
                f"Contact_Suppression covering its scope took effect at {instant.isoformat()}. "
                "R21.C3 makes check 5 refuse every customer-visible action on a suppressed scope, "
                "so nothing should have been able to start this call"
            )

            resolved = intent["resolved_at"]
            if resolved is not None and _as_datetime(resolved) > instant:
                assert str(intent["idempotency_key"]) in post_suppression_keys, (
                    f"intent {intent['idempotency_key']} was in flight when the suppression "
                    f"landed at {instant.isoformat()} and confirmed afterwards, with no "
                    "POST_SUPPRESSION_ACTION record. R21.C7 requires the Outcome_Monitor to record "
                    "it, and without that record the carve-out this invariant grants for an "
                    "already-attempted action would be unearned"
                )

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

        **The bound counts every kind of independent operation, and one used to be missing.** See
        :class:`_Tracker`: deliveries, sweeps and operator requests, because an operator taking
        ownership of a case is an independent operation that legitimately gets its own id. The
        trailing ``+ 1`` is for the case's own opening records, written by detection before any of
        the three counters can claim them.

        The bound stays *tight* deliberately — it is one id per counted operation and no slack per
        step. A per-step implementation writes several records per drained job under several ids, so
        it exceeds this bound on the first worker run rather than eventually. That tightness is the
        whole value of the invariant; a constant large enough to absorb a per-step id would leave
        nothing being checked.
        """
        by_case: dict[str, set[str]] = {}
        for record in snap.audit:
            assert record["correlation_id"] is not None, (
                f"audit record {record['event_type']} has no correlation id; the async work cannot "
                "be joined to the delivery that scheduled it"
            )
            if record["case_id"] is not None:
                by_case.setdefault(str(record["case_id"]), set()).add(str(record["correlation_id"]))

        tracker = self._tracker
        bound = (
            tracker.deliveries
            + tracker.sweeps
            + tracker.operator_requests
            + tracker.customer_requests
            + 1
        )
        for case_id, ids in by_case.items():
            assert len(ids) <= bound, (
                f"case {case_id} carries {len(ids)} distinct correlation ids across "
                f"{tracker.deliveries} deliveries, {tracker.sweeps} sweeps, "
                f"{tracker.operator_requests} operator requests and "
                f"{tracker.customer_requests} customer requests; a distinct id per step makes the "
                "trail unjoinable"
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

    # P63 ------------------------------------------------------------------

    def _p63_the_window_never_moves_and_reviews_stay_inside_it(self, snap: _Snapshot) -> None:
        """**Property 63.** A review buys a case no time at all.

        R30's whole safety argument rests on this. The review loop adds an edge back into
        ``DECISION_PENDING``, which means a case can now go round more than once without ever having
        acted — and the only thing keeping that from being unbounded in *wall clock* terms is that
        ``window_end_at`` is immutable and every review instant is clamped inside it. Widen the
        window by one review and the base spec's termination bound
        (``RECOVERY_WINDOW_DURATION + OUTCOME_WAIT_TIMEOUT + LIFECYCLE_EVALUATION_INTERVAL``) stops
        being a bound, quietly, for exactly the cases Revora keeps deciding to wait on.

        Four clauses, and the third is the one that would catch a forgotten line rather than a wrong
        one:

        1. ``window_end_at == detected_at + RECOVERY_WINDOW_DURATION``, which is the formula
           detection writes. This pins the *creation* value, which no stability check can.
        2. ``window_end_at`` equals the first value this machine observed for the case. This pins
           stability, which no formula check can — a write that moved both columns together would
           satisfy clause 1.
        3. ``next_review_at`` is non-null **only** while the case is at ``POLICY_CHECK`` (R30.C4).
           The clear happens in ``apply_locked_transition``, keyed on the source state rather than
           on a list of edges, so a future edge out of ``POLICY_CHECK`` inherits it — and if
           somebody ever moves the clear back to the call sites, this is what notices. A stale
           instant on a terminal case is not harmless: it is a row the sweeper's index predicate
           would match except for the state column, one predicate change from reviewing dead cases.
        4. Every persisted ``next_review_at <= window_end_at``. Also a CHECK constraint on the
           table, and asserted anyway — the constraint is the mechanism and this is the claim, and a
           migration that dropped the constraint should fail a property test rather than pass one.

        The termination clause of P63 is not here. It cannot be: at any single step "still running"
        is legal, so it lives in :meth:`teardown`, which advances past every bound and requires that
        nothing is still alive. Adding review rules is precisely the change that could break it — a
        review that kept re-enqueuing itself would show up there and nowhere else.
        """
        for case in snap.cases:
            case_id = str(case["id"])
            detected_at, window_end = case["detected_at"], case["window_end_at"]
            assert isinstance(detected_at, datetime) and isinstance(window_end, datetime)

            assert window_end == detected_at + _CONFIG.RECOVERY_WINDOW_DURATION, (
                f"case {case_id} has a window ending at {window_end}, which is not "
                f"{detected_at} + {_CONFIG.RECOVERY_WINDOW_DURATION}; the window is assigned once "
                "from the detection instant and R2.C5 forbids extending or resetting it"
            )

            first_seen = self._tracker.windows.setdefault(case_id, window_end)
            assert window_end == first_seen, (
                f"case {case_id} had its window moved from {first_seen} to {window_end}. Every "
                "termination bound in the system is measured against this column, so moving it "
                "does not delay a case — it removes the guarantee that the case ends at all"
            )

            state = str(case["state"])
            review_at = case["next_review_at"]
            if review_at is None:
                continue
            assert isinstance(review_at, datetime)
            assert state == CaseState.POLICY_CHECK.value, (
                f"case {case_id} is {state} and still carries next_review_at={review_at}; R30.C4 "
                "clears it in the same transaction as every edge out of POLICY_CHECK, so this is "
                "either a missed clear or a write from somewhere that is not the case manager"
            )
            assert review_at <= window_end, (
                f"case {case_id} is scheduled for review at {review_at}, after its window closes "
                f"at {window_end}; the review would arrive on a case the lifecycle sweep has "
                "already ended, and the clamp in `_review_instant` exists to make that impossible"
            )

    # P64 ------------------------------------------------------------------

    def _p64_decision_cycles_stay_within_the_cap(self, snap: _Snapshot) -> None:
        """**Property 64.** Reviews spend from the same allowance every other cycle spends.

        R30 adds a third edge into ``DECISION_PENDING`` and claims it adds no new bound category:
        the review increments the same counter the two existing edges increment, so
        ``MAX_RECOVERY_ATTEMPTS`` bounds all three together. This is that claim, counted two ways.

        **The bound asserted here is ``MAX_RECOVERY_ATTEMPTS``, and it was measured rather than
        taken from the requirement.** The arithmetic, which is worth having written down because it
        is not obvious and the obvious reading is wrong:

        * ``DIAGNOSED -> DECISION_PENDING`` fires **at most once per case**. ``DIAGNOSED`` is
          reachable only from ``DETECTED`` and ``DETECTED`` only from ``NEW``, so nothing returns to
          it. That edge takes the counter from 0 to 1 and is not gated by the counter at all.
        * ``POLICY_CHECK -> DECISION_PENDING`` is gated: ``handle_review`` refuses when
          ``decision_cycle_count >= MAX_RECOVERY_ATTEMPTS``, so it only ever moves ``n -> n+1`` for
          ``n <= MAX_RECOVERY_ATTEMPTS - 1``.
        * ``WAITING_FOR_OUTCOME -> DECISION_PENDING`` **now has two callers, and both are gated
          by the same counter.** It had none until R24.C13 gave the promise sweep a reason to
          enqueue a decision cycle for a case waiting on an outcome: ``handle_review`` takes this
          edge from that state and refuses at the identical
          ``decision_cycle_count >= MAX_RECOVERY_ATTEMPTS`` gate it applies to the review edge,
          and ``apply_missed_disposition`` takes it when a promise becomes ``MISSED``, asking
          ``bound_reached`` — which reads the same counter against the same bound — and stopping
          the case where it is spent. So the arithmetic below is unchanged: this edge, like the
          other two, only ever moves ``n -> n+1`` for ``n <= MAX_RECOVERY_ATTEMPTS - 1``.

        Starting from 1 and stepping to at most ``MAX_RECOVERY_ATTEMPTS``, the ceiling is exactly
        ``MAX_RECOVERY_ATTEMPTS`` — measured at 3 against the seeded default of 3 by driving three
        cases to exhaustion. **``MAX_ATTEMPTS_REACHED`` in the policy engine is not what holds this
        bound**: it compares ``executed_action_count``, not the cycle counter, and it is not on the
        review path at all. The gate in ``handle_review`` is the whole of it, which is why the
        assertion is worth having rather than being implied by the policy engine.

        One caveat, stated because the assertion is stricter than the system: the forward edge is
        ungated, so a merchant configured with ``MAX_RECOVERY_ATTEMPTS = 0`` would reach cycle 1
        against a cap of 0. The true invariant is ``<= max(1, MAX_RECOVERY_ATTEMPTS)``. This machine
        runs on the seeded configuration where the cap is 3, so the stricter form is the right one
        to assert here — a zero cap is a configuration that forbids Revora from deciding anything,
        and it deserves its own test rather than a weaker bound in this one.

        The second count is the interesting one. ``decision_cycle_count`` is compared against the
        number of recorded ``STATE_TRANSITION`` records whose ``new_state`` is ``DECISION_PENDING``,
        which is what makes the cap a bound on *entries* rather than on a number some code chose to
        write. A counter that stopped incrementing would pass the cap check and fail this one.

        Then R30.C10, over history rather than over a moment: a case stopped for
        ``DECISION_CYCLE_LIMIT_REACHED`` is ``STOPPED``, sits exactly at the cap, has its review
        instant cleared, and has **no enqueued decision cycle**. The last clause is why
        :attr:`_Snapshot.pending_reviews` exists — a queued job for a case that has been told there
        are no cycles left is a cycle that will be spent after the case ended.
        """
        cap = _CONFIG.MAX_RECOVERY_ATTEMPTS
        entries: dict[str, int] = {}
        for record in snap.transitions:
            if str(record["new_state"]) == CaseState.DECISION_PENDING.value:
                entries[str(record["case_id"])] = entries.get(str(record["case_id"]), 0) + 1

        queued = {str(job["case_id"]) for job in snap.pending_reviews}

        for case in snap.cases:
            case_id = str(case["id"])
            cycles = int(case["decision_cycle_count"])  # type: ignore[arg-type]
            assert cycles <= cap, (
                f"case {case_id} has entered a decision cycle {cycles} times against a cap of "
                f"{cap}. The review edge spends from the same allowance as the other two edges "
                "into DECISION_PENDING, so exceeding it means one of the three is ungated"
            )
            assert cycles == entries.get(case_id, 0), (
                f"case {case_id} counts {cycles} decision cycles but the audit trail records "
                f"{entries.get(case_id, 0)} transitions into DECISION_PENDING. The cap is only a "
                "bound on re-entry if the counter is the count of re-entries"
            )

            if str(case["terminal_reason"] or "") != TerminalReason.DECISION_CYCLE_LIMIT_REACHED:
                continue

            # R30.C10, all four consequences.
            assert str(case["state"]) == CaseState.STOPPED.value, case
            assert cycles == cap, (
                f"case {case_id} was stopped for DECISION_CYCLE_LIMIT_REACHED at {cycles} cycles "
                f"against a cap of {cap}; the reason names a bound the case had not reached"
            )
            assert case["next_review_at"] is None, (
                f"case {case_id} was stopped at the decision-cycle cap and still carries a review "
                "instant; the sweeper is one index-predicate change away from picking it up again"
            )
            assert case_id not in queued, (
                f"case {case_id} was stopped at the decision-cycle cap with a review still queued. "
                "R30.C10 requires the terminating review to enqueue no cycle, and a job that "
                "outlives the case is a cycle that gets spent after the case ended"
            )

    # P66 ------------------------------------------------------------------

    def _p66_a_waiting_case_is_never_presented_as_ended(self, snap: _Snapshot) -> None:
        """**Property 66.** Restraint is presented as an appointment, not as a conclusion.

        R30.C13 is a requirement about the *read model*, so this asserts against the read model
        rather than against the rows: :func:`revora.api.views.case_summary` is what the dashboard
        renders, and :func:`revora.metrics.unresolved.unresolved_groups` is the grouping the
        requirement says a waiting case must be absent from.

        This is the presentation half of the defect R30 documents. The pipeline half — a case that
        chose restraint having no route to a second decision cycle — is fixed by the review loop.
        The presentation half is that a merchant looking at such a case saw a state name, an empty
        executed-action cell and nothing about the future, and drew the conclusion the old
        implementation had actually reached. Fixing the pipeline without fixing the screen would
        leave the product doing the right thing and reporting the wrong one.

        Three clauses per actively-waiting case — ``POLICY_CHECK``, a ``next_review_at`` later than
        now, and a counter below the cap, which are exactly R30.C13's conditions:

        1. the summary carries a ``waiting`` block naming the instant and the selected Null_Action;
        2. its state is not one of the five the unresolved grouping scans, and its label is not any
           Terminal_State's label — so it is in no ended grouping and no Terminal_State grouping;
        3. the grouping's total case count equals the number of cases actually in a scanned state,
           which is the *absence* stated as an equality rather than as a search. An equality catches
           a case counted twice as well as one counted wrongly, and it costs one query for the whole
           snapshot rather than one per case.

        And the converse, because a block that is always present says nothing: every terminal case's
        summary carries no ``waiting`` block at all.

        Cost. The grouping is one query per step; the summaries are several small indexed reads per
        *actively waiting* case, of which there are typically none to three. Both are skipped
        entirely when the universe holds no cases, so an example that never reaches ``POLICY_CHECK``
        pays nothing — which is also why the teardown reports how many reviews an example actually
        drove.
        """
        if not snap.cases:
            return

        waiting_ids = [
            str(case["id"])
            for case in snap.cases
            if str(case["state"]) == CaseState.POLICY_CHECK.value
            and case["next_review_at"] is not None
            and _as_datetime(case["next_review_at"]) > self._clock.now()
            and int(case["decision_cycle_count"]) < _CONFIG.MAX_RECOVERY_ATTEMPTS  # type: ignore[arg-type]
        ]
        terminal_ids = [
            str(case["id"]) for case in snap.cases if str(case["state"]) in _TERMINAL_VALUES
        ]
        in_a_scanned_state = sum(
            1 for case in snap.cases if str(case["state"]) in _UNRESOLVED_STATE_VALUES
        )

        earliest = min(_as_datetime(case["detected_at"]) for case in snap.cases)
        with transaction() as session:
            config = ConfigurationRepository(session).load(self._merchant_id)
            groups = unresolved_groups(
                session,
                self._merchant_id,
                start=earliest - timedelta(days=1),
                end=self._clock.now() + _CONFIG.RECOVERY_WINDOW_DURATION + timedelta(days=1),
            )
            grouped = sum(group.case_count for group in groups)

            repository = RecoveryCaseRepository(session)
            summaries = {
                case_id: case_summary(
                    session,
                    self._merchant_id,
                    _require_case(repository, self._merchant_id, uuid.UUID(case_id)),
                    config=config,
                )
                for case_id in [*waiting_ids, *terminal_ids]
            }

        assert grouped == in_a_scanned_state, (
            f"the unresolved grouping counts {grouped} cases against {in_a_scanned_state} in one "
            f"of {sorted(_UNRESOLVED_STATE_VALUES)}. The grouping selects on that state list, so a "
            "disagreement means either a non-terminal case is being reported as unresolved revenue "
            "or an unresolved one is missing from it"
        )

        terminal_labels = {
            CASE_STATE_LABELS[state.value] for state in TERMINAL_STATES
        }
        for case_id in waiting_ids:
            summary = summaries[case_id]
            waiting = summary["waiting"]
            assert isinstance(waiting, dict), (
                f"case {case_id} is at POLICY_CHECK with a future review instant and a counter "
                "below the cap, and the case list presents no waiting block for it. That is the "
                "state R30 was written about being shown as though nothing will happen next"
            )
            assert waiting["next_review_at"] == _as_datetime(
                next(
                    case["next_review_at"]
                    for case in snap.cases
                    if str(case["id"]) == case_id
                )
            ).isoformat(), waiting
            assert waiting["selected_action"] in _NULL_ACTION_VALUES, (
                f"case {case_id} is presented as waiting on the selected action "
                f"{waiting['selected_action']!r}, which is not a Null_Action. Only DO_NOTHING and "
                "WAIT rest at POLICY_CHECK by choice; anything else resting there was refused"
            )
            assert waiting["selected_action_label"] == WAITING_AND_WATCHING, waiting
            assert str(summary["state"]) not in _UNRESOLVED_STATE_VALUES, summary
            assert summary["state_label"] not in terminal_labels, (
                f"case {case_id} is actively waiting and its state label "
                f"{summary['state_label']!r} is one of the Terminal_State labels; R26.C14 keeps "
                "those three labels distinct precisely so a non-terminal case cannot borrow one"
            )

        for case_id in terminal_ids:
            assert summaries[case_id]["waiting"] is None, (
                f"terminal case {case_id} is presented as actively waiting. A case that has ended "
                "with an appointment on the screen is the opposite failure to the one R30.C13 "
                "exists for, and just as misleading"
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
            # The review path, counted for the same reason (task 38.4). P63, P64 and P66 are all
            # statements about a case that chose restraint, so an example that produced none passes
            # every one of them without testing anything — and that was the real situation before
            # `_AMOUNTS` gained an amount small enough for the optimizer to decline. `enqueued`
            # against `applied` distinguishes "a job was created" from "handle_review ran".
            self._tracker.reviews_applied = sum(
                1 for record in final.audit if str(record["event_type"]) == "CASE_REVIEWED"
            )
            capped = sum(
                1
                for case in final.cases
                if str(case["terminal_reason"] or "")
                == TerminalReason.DECISION_CYCLE_LIMIT_REACHED
            )
            # Counted from `terminal_reason` rather than from the tracker, so this reports what the
            # *worker* did with a hard stop and not merely what the endpoint accepted. The gap
            # between `hard_stops` and this number is the same distinction `reviews_enqueued`
            # against `reviews_applied` draws: a suppression written is not a suppression applied.
            suppression_escalations = sum(
                1
                for case in final.cases
                if str(case["terminal_reason"] or "")
                in {
                    TerminalReason.CUSTOMER_DISPUTED_CHARGE.value,
                    TerminalReason.CUSTOMER_CANCELLED_ORDER.value,
                }
            )
            suppressions_in_force = sum(
                1 for row in final.suppressions if row["released_at"] is None
            )
            with suppress(InvalidArgument):
                event(f"terminal states reached: {sorted(terminal_states)}")
                event(f"confirmed intents: {'some' if confirmed else 'none'}")
                event(f"cases: {len(final.cases)}")
                event(f"reviews enqueued: {'some' if self._tracker.reviews_enqueued else 'none'}")
                event(f"reviews applied: {'some' if self._tracker.reviews_applied else 'none'}")
                event(f"cases stopped at the decision-cycle cap: {'some' if capped else 'none'}")
                # P39's vacuity guards (task 42.4). "No contact after the hard stop" is trivially
                # true of a run with no hard stop, and this machine has already shipped one
                # vacuous stateful property — the review properties asserted nothing across 21
                # examples until `start` was changed to drain. Three counts, because the property
                # has three clauses that can each be independently unexercised: the suppression
                # existing at all, its consequences having been applied by the worker, and a new
                # case having been opened in an already-suppressed scope.
                event(f"hard stops submitted: {'some' if self._tracker.hard_stops else 'none'}")
                event(
                    "hard stops applied: "
                    f"{'some' if suppression_escalations else 'none'}"
                )
                event(
                    "retries into a suppressed scope: "
                    f"{'some' if self._tracker.retries_in_suppressed_scope else 'none'}"
                )
                event(f"suppressions in force at teardown: {suppressions_in_force}")
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

    def _deliver_failure(
        self, amount: int, contact: str, *, order_id: str | None = None
    ) -> None:
        """Deliver one signed ``payment.failed``, optionally reusing an existing order id.

        ``order_id`` defaults to a fresh one derived from the event id, which is what an unrelated
        failure looks like. Passing an existing one is how ``deliver_retry_in_suppressed_scope``
        opens a second case in a scope that is already suppressed — the payment id is always fresh,
        so the two cases are genuinely distinct and only the *scope* is shared.
        """
        payment_id = f"pay_{uuid.uuid4().hex[:16]}"
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        body = _failed_body(
            payment_id, event_id, amount=amount, contact=contact, order_id=order_id
        )
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

    def _mint_customer_token(self, case_id: uuid.UUID) -> str | None:
        """A wire token for one case, or ``None`` if the case cannot hold one.

        The discipline exception the rules above describe, isolated in one method so it is one
        place to delete when the token URL starts travelling on the provider request.

        ``None`` rather than an exception on the two ordinary refusals. A case whose window has
        already closed gets no token, because expiry is the earlier of the lifetime and
        ``window_end_at`` and a token expiring at mint is not a credential — and a case that
        already holds a live token has that one reused, which is R18.C14 and returns no wire form,
        because no reversible copy of an existing secret exists anywhere. Both mean *this step
        cannot submit* rather than *the system is broken*.
        """
        with self._engine.begin() as connection:
            row = connection.execute(
                text("SELECT window_end_at FROM recovery_case WHERE id = :c"),
                {"c": str(case_id)},
            ).first()
        if row is None:
            return None
        window_end_at = _as_datetime(row[0])
        if window_end_at <= self._clock.now():
            return None
        with transaction() as session:
            minted = TokenService.on_session(session, _CONFIG).mint(
                self._merchant_id,
                case_id=case_id,
                window_end_at=window_end_at,
                approved_action=CandidateAction.PAYMENT_LINK,
                moment=self._clock.now(),
            )
        if minted.token is None:
            return None
        return minted.token.wire_token

    def _suppressed_scopes(self) -> list[tuple[str, str]]:
        """``(order_id, contact)`` for every scope currently suppressed and retryable.

        The pair a retry needs: the order identifier fixes one half of the scope key and the
        contact fixes the other, since ``customer_key`` is derived from it. Read back from the
        originating case rather than held in tracker state, because a suppression that only the
        test remembers is not a suppression P39 can claim anything about.

        Only unreleased suppressions, and only contacts this machine actually uses — a scope whose
        contact is not in ``_CONTACTS`` could not be retried through ``_deliver_failure``, which
        builds its payload from one of them.
        """
        with self._engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.provider_order_id, c.customer_key FROM contact_suppression s "
                    "JOIN recovery_case c ON c.id = s.origin_case_id "
                    "WHERE s.merchant_id = :m AND s.released_at IS NULL "
                    "AND c.provider_order_id IS NOT NULL"
                ),
                {"m": str(self._merchant_id)},
            ).all()
        by_key = {crypto.customer_key(contact): contact for contact in _CONTACTS}
        return [
            (str(order_id), by_key[str(customer_key)])
            for order_id, customer_key in rows
            if str(customer_key) in by_key
        ]

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
                "SELECT case_id, seq, event_type, correlation_id, previous_state, new_state, "
                "idempotency_key FROM audit_record WHERE merchant_id = :m"
            )
            return _Snapshot(
                cases=rows(
                    "SELECT id, state, executed_action_count, customer_message_count, "
                    "decision_cycle_count, detected_at, window_end_at, next_review_at, "
                    "customer_key, provider_order_id, terminal_reason, version "
                    "FROM recovery_case WHERE merchant_id = :m"
                ),
                intents=rows(
                    "SELECT id, case_id, action, state, attempt_ordinal, idempotency_key, "
                    "attempt_started_at, resolved_at, is_post_payment, policy_decision_id "
                    "FROM execution_intent WHERE merchant_id = :m"
                ),
                # P39 needs the persisted suppression rather than a set this machine remembers:
                # the claim is that the suppression survives a restart, and tracker state would be
                # re-created by the restart rule and prove the opposite.
                suppressions=rows(
                    "SELECT scope_key, origin_case_id, hard_stop_reason, suppressed_at, "
                    "released_at FROM contact_suppression WHERE merchant_id = :m"
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
                # Unclaimed review jobs only. A claimed one is a cycle already being applied, and
                # `PENDING` is exactly the set the `one_pending_job_per_dedupe_key` partial unique
                # index covers — so this reads the same rows R30.C9's mechanism constrains.
                pending_reviews=tuple(
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT case_id, dedupe_key FROM job "
                            "WHERE merchant_id = :m AND kind = :kind AND state = 'PENDING' "
                            "AND case_id IS NOT NULL"
                        ),
                        {**params, "kind": CASE_REVIEW_KIND},
                    ).mappings()
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


def _as_datetime(value: object) -> datetime:
    """Narrow a snapshot column to a ``datetime``, loudly.

    The snapshot rows are ``dict[str, object]`` because they come from seven different ``SELECT``
    lists, so every timestamp needs narrowing at the point of use. A helper rather than an
    ``assert isinstance`` at each site: P63 and P66 between them read five timestamp columns, and
    the repeated two-line narrowing was longer than the assertions it guarded.
    """
    assert isinstance(value, datetime), f"expected a timestamp, got {value!r}"
    return value


def _require_case(
    repository: RecoveryCaseRepository, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> RecoveryCase:
    """Load a case the snapshot has already seen. Its absence would be a deleted row.

    ``get`` returns ``Optional`` because a caller may name a case that does not exist or belongs to
    another tenant. Neither can be true here — the id came from this merchant's own snapshot one
    query ago — so the ``None`` branch is an assertion rather than a fallback. Nothing in Revora
    deletes a ``recovery_case``, so reaching it would be a finding.
    """
    case = repository.get(merchant_id, case_id)
    assert case is not None, (
        f"case {case_id} was in the snapshot and is not readable now; nothing deletes a "
        "recovery_case, so this is a lost row rather than a missing one"
    )
    return case


class _Resolver:
    """Every credential the pipeline needs, and deliberately no LLM one."""

    _FIXED: ClassVar[dict[str, str]] = {
        "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"L" * 32).decode(),
        "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"M" * 32).decode(),
        "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
        + base64.b64encode(b"M" * 32).decode(),
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
    payment_id: str,
    event_id: str,
    *,
    amount: int,
    contact: str,
    order_id: str | None = None,
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
                    "order_id": (
                        f"order_{event_id}" if order_id is None else order_id
                    ),
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
    stateful_step_count=20,
    deadline=None,
    # Every example builds its own merchant, engine and clock inside the machine's `__init__`, so
    # the session-scoped `migrated_url` fixture is read once and never mutated per example. The
    # health check cannot know that.
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
"""Deliberately modest, and the step count was raised once with a measurement behind it.

Each step is several Postgres round trips plus a fourteen-invariant snapshot, so this is a slow test
by construction. Twenty examples of twenty steps explores about four hundred interleavings per run,
which is enough to have found the ordering bugs it was written for while staying inside a CI job's
patience. Raising either number is the right move when investigating a specific failure — and the
wrong move as a default, because a test that gets excluded for slowness enforces nothing.

**Fourteen to twenty, for task 38.4.** Two new rules brought the count to sixteen, and with fourteen
steps each rule appears less than once per example on average — which makes any property needing an
*ordered pair* of specific rules rare. Measured, on twenty-one examples each time:

* fourteen steps, review rules added but ``start`` not draining: **zero** examples reached a review;
* fourteen steps, with the two reachability fixes (``start`` drains, ``sweep_review`` is a whole
  worker tick): three reached one through the Review_Sweeper, ten through an attached event;
* twenty steps, same fixes: five through the sweeper, six through an attach.

So twenty is where both routes into R30's loop are exercised in a useful fraction of examples rather
than by luck. The cost is about forty seconds on this machine — thirty-eight seconds became
seventy-six. The alternative was leaving P63, P64 and P66 asserting nothing about a review, and a
property that never reaches its subject is worse than a slower one that does. The teardown reports
both counts as Hypothesis events, so the next person to change these numbers can see immediately
whether they broke that coverage.
"""


@pytest.mark.pg
def test_the_recovery_lifecycle_holds_its_fourteen_invariants(migrated_url: str) -> None:
    """Run the machine.

    Driven through ``run_state_machine_as_test`` rather than the ``TestCase`` attribute, because the
    machine needs the session-scoped migrated database and Hypothesis's generated ``TestCase`` takes
    no fixtures.
    """
    run_state_machine_as_test(
        lambda: RecoveryLifecycleMachine(migrated_url), settings=_SETTINGS
    )


# ---------------------------------------------------------------------------
# Property 64 — the graph-level half, and the only part of this file that needs no database
# ---------------------------------------------------------------------------


def _simple_cycles() -> tuple[tuple[tuple[CaseState, CaseState], ...], ...]:
    """Every simple cycle in ``LEGAL_TRANSITIONS``, each as its ordered list of edges.

    Enumerated rather than listed. The graph has two cycles today and the termination proof does not
    depend on that number, so a test that named them would go on passing while a third cycle was
    added without a counter effect — which is precisely the change the proof cannot survive.

    Each cycle is found exactly once, from its lowest-indexed state: the walk refuses any successor
    that comes before ``start`` in ``CaseState`` declaration order, so the same loop is not reported
    once per rotation. Recursion is safe at this size — fourteen states, and the terminal ones are
    one step from a sink.
    """
    order = {state: index for index, state in enumerate(CaseState)}
    successors = {
        state: tuple(sorted(legal_targets(state), key=order.__getitem__)) for state in CaseState
    }
    found: list[tuple[tuple[CaseState, CaseState], ...]] = []

    def walk(start: CaseState, path: list[CaseState], on_path: set[CaseState]) -> None:
        for successor in successors[path[-1]]:
            if order[successor] < order[start]:
                continue
            if successor == start:
                found.append(tuple(pairwise([*path, start])))
            elif successor not in on_path:
                path.append(successor)
                on_path.add(successor)
                walk(start, path, on_path)
                path.pop()
                on_path.remove(successor)

    for state in CaseState:
        walk(state, [state], {state})
    return tuple(found)


def _describe(cycle: tuple[tuple[CaseState, CaseState], ...]) -> str:
    return " -> ".join([source.value for source, _ in cycle] + [cycle[0][0].value])


@pytest.mark.pure
def test_every_cycle_in_the_transition_graph_spends_a_decision_cycle() -> None:
    """**Property 64, graph half.** Every cycle contains an edge that increments the counter.

    This is the executable replacement for a claim that used to be a comment: the transition
    module's docstring asserted the graph had *exactly one* cycle, and the review edge
    ``POLICY_CHECK -> DECISION_PENDING`` (task 37.1) made that false. Nothing had ever checked it,
    which is how a stated invariant goes stale without a single test turning red. It was also never
    the fact termination rested on. What termination rests on is this: a cycle costs at least one
    decision cycle, ``decision_cycle_count`` never decreases, and entry to ``DECISION_PENDING`` is
    refused at ``MAX_RECOVERY_ATTEMPTS``. So the number of times a case can go round is bounded and
    every path is finite — and the cycle *count* is free to change without weakening any of that.

    Asserted over the enumeration rather than over a named list, because the failure this is here to
    catch is a *future* edge closing a loop with no counter effect — an edge that costs nothing to
    traverse, which is a case that can run forever inside a window that never closes it.

    See the termination proof in ``revora/domain/transitions.py``; this is that proof's second step,
    executable.
    """
    cycles = _simple_cycles()

    # Guard against a vacuous pass. A broken enumeration returning nothing would satisfy every
    # "for each cycle" claim below while checking none of them, and the graph provably has cycles —
    # the recovery lifecycle re-decides cases, which is what a decision cycle is.
    assert cycles, (
        "the cycle enumeration found no cycles at all; the transition graph has at least the "
        "re-entry loop, so this is a broken enumeration silently passing every assertion below"
    )

    for cycle in cycles:
        deltas = [
            LEGAL_TRANSITIONS[(source, target)].effects.decision_cycle_delta
            for source, target in cycle
        ]
        assert any(delta >= 1 for delta in deltas), (
            f"the cycle {_describe(cycle)} can be traversed without incrementing "
            "decision_cycle_count, so a case can go round it forever; every cycle must spend a "
            "decision cycle or termination is not bounded"
        )


@pytest.mark.pure
def test_every_edge_into_decision_pending_spends_a_decision_cycle() -> None:
    """**Property 64, the stronger form the proof actually uses.** Per target state, not per cycle.

    Worth having *alongside* the enumeration, and the two are not redundant:

    * The enumeration is the property termination needs, stated exactly. It catches any zero-cost
      loop, including one that avoids ``DECISION_PENDING`` altogether — say a future
      ``WAITING_FOR_OUTCOME -> EXECUTING`` retry edge, which no per-edge rule about
      ``DECISION_PENDING`` would notice.
    * This one is what makes a *new* cycle safe by construction. Every cycle in this graph passes
      through ``DECISION_PENDING`` because that is where re-deciding happens, so if every edge into
      that state carries the delta, an edge added tomorrow inherits the guarantee without anybody
      re-running an enumeration or reading this file. It is the invariant stated as a property of
      the target state, which is why ``DIAGNOSED -> DECISION_PENDING`` carries the delta despite not
      lying on any cycle.

    Neither implies the other, and the cheap one is the one that catches the mistake early.
    """
    entries = [
        (source, target)
        for (source, target) in LEGAL_TRANSITIONS
        if target == CaseState.DECISION_PENDING
    ]
    assert entries, "no edge enters DECISION_PENDING; the lifecycle cannot decide anything"

    for source, target in entries:
        rule = LEGAL_TRANSITIONS[(source, target)]
        assert rule.effects.decision_cycle_delta >= 1, (
            f"{source.value} -> {target.value} enters DECISION_PENDING without spending a decision "
            "cycle; the termination proof reads the delta off the target state, so an entry edge "
            "without it makes every cycle through DECISION_PENDING free"
        )


@pytest.mark.pure
def test_every_state_can_reach_recovered_and_only_against_a_verified_capture() -> None:
    """A customer can pay at any moment, so RECOVERED must be reachable from every state.

    **This assertion replaces a claim this file and the transition module both used to make** —
    that RECOVERED was reachable only from ``WAITING_FOR_OUTCOME`` on the forward path or from a
    terminal state by reconciliation. That was true of the table and it was a hole, not an
    invariant: a capture read while the case sat at ``DECISION_PENDING`` or ``EXECUTING`` had its
    ``RECOVERY_RECORDED`` written and its money counted, and then its transition refused, so the
    case expired with its amount already in the recovery figure. A 150-case batch produced 62
    recovery records against 39 cases in RECOVERED. The old text is gone from
    ``revora/domain/transitions.py`` rather than merely qualified, because a comment that states
    the opposite of the table is worse than no comment.

    What replaces it is the guard, asserted here in both directions:

    * every non-terminal state has an edge to RECOVERED, and ``DECISION_PENDING`` and
      ``EXECUTING`` are named explicitly because those are the two the measured failures came
      from;
    * every edge into RECOVERED requires a verified capture, **except** the forward edge from
      ``WAITING_FOR_OUTCOME``, whose caller holds the same read. Nothing else may declare
      recovery — not a webhook, not a timer, not a policy decision.

    The second clause is the one that matters. Reachability without it would be an invitation to
    close the hole by adding an ungated edge, which would trade "money counted for a case that
    never recovered" for "a case declared recovered on a webhook", and the second is the failure
    this whole subsystem is built to prevent.
    """
    non_terminal = frozenset(CaseState) - TERMINAL_STATES
    missing = sorted(
        state.value
        for state in non_terminal
        if (state, CaseState.RECOVERED) not in LEGAL_TRANSITIONS
    )
    assert not missing, (
        f"no edge to RECOVERED from {missing}; a customer who pays while a case is in one of "
        "those states has their money recorded and counted while the case goes on to expire"
    )
    for state in (CaseState.DECISION_PENDING, CaseState.EXECUTING):
        assert (state, CaseState.RECOVERED) in LEGAL_TRANSITIONS, state.value

    for (source, target), edge in LEGAL_TRANSITIONS.items():
        if target is not CaseState.RECOVERED:
            continue
        if source is CaseState.WAITING_FOR_OUTCOME:
            assert edge.kind is TransitionKind.FORWARD, (
                "the ordinary recovery path stopped being the FORWARD edge, so the audit trail "
                "no longer distinguishes it from a reconciliation"
            )
            continue
        assert edge.requires_verified_capture, (
            f"{source.value} -> RECOVERED does not require a verified capture; recovery is "
            "declared only from an authoritative provider read reporting the capture, and an "
            "ungated edge lets a webhook alone put money in the recovery figure"
        )

    for source in TERMINAL_STATES:
        terminal_edge = LEGAL_TRANSITIONS.get((source, CaseState.RECOVERED))
        if source is CaseState.RECOVERED:
            assert terminal_edge is None, "RECOVERED gained an edge to itself"
            continue
        assert terminal_edge is not None and terminal_edge.at_most_once_per_case, (
            f"{source.value} -> RECOVERED may now be taken more than once per case, which is a "
            "second count of the same money"
        )


@pytest.mark.pure
def test_no_cycle_passes_through_a_terminal_state() -> None:
    """A cycle through a terminal state would break termination in a way the counter cannot bound.

    Asked because the enumeration runs over the *whole* table, including the forty termination edges
    and every reconciliation edge — so it is fair to ask whether it can produce such a cycle, and
    what it would mean if it did.

    It cannot, and the reason is structural rather than lucky. Termination edges are declared only
    from ``NON_TERMINAL_STATES``, so no terminal state has an outgoing edge except the
    reconciliation edge to ``RECOVERED``; and ``RECOVERED`` is excluded from the reconciliation
    sources, so it has no outgoing edge at all. Every terminal state is therefore a sink or one step
    from one.

    If a cycle through a terminal state ever appeared, the decision-cycle bound would not save it.
    A terminal case has already spent its outcome — the observation row is written, the metrics have
    counted it — and re-entering the lifecycle from there would make every bound it had already
    spent spendable again. That is the failure
    :meth:`RecoveryLifecycleMachine._p6_nothing_after_terminal_but_verified_reconciliation` checks
    for at runtime; this is the same claim one level up, about the table rather than a history.
    """
    for cycle in _simple_cycles():
        states = {source for source, _ in cycle}
        assert not states & TERMINAL_STATES, (
            f"the cycle {_describe(cycle)} passes through the terminal state(s) "
            f"{sorted(state.value for state in states & TERMINAL_STATES)}; a terminal case that "
            "can re-enter the lifecycle can spend every bound it has already spent"
        )

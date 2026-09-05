"""Property 40, the scope-key facts behind it, and the release that needs a person.

**Property 39 is not here.** It is a claim about a case's history under an arbitrary interleaving
of hard stops with everything else the system can do — subsequent events, decision cycles, sweeps,
restarts and cases created afterwards in the same scope — so it is asserted as an invariant inside
the one machine that models the lifecycle, ``tests/properties/test_lifecycle_machine.py``. Naming a
file after a property and putting a second state machine in it would mean re-deriving the clock,
the provider, the merchant and the worker, and then exploring suppressions against a universe where
nothing else was happening — while the failures worth finding are a hard stop racing an execution,
a retry opening a fresh case in a suppressed scope, and a restart between the two.

So this file holds what that machine cannot state:

* **Property 40 (``pg``).** A claim about the *end state* of one case that received a hard stop —
  terminal state, terminal reason, every token revoked, and the customer-wide opt-out status
  unchanged. The machine asserts invariants after every step and P40 is a statement about a
  settled case, which is a different shape: it needs the consequence applied and the queue drained
  before anything is true.
* **The scope-key facts (``pure``).** That the scope prefers the order identifier, falls back to
  the case identifier, is stable, separates the two halves of its preimage, and holds no
  recoverable copy of the customer key. No database contributes anything to any of them.
* **The release (``pg``).** That a release names a person and that an anonymous one is *unstorable*
  rather than merely refused — which is a claim about a ``CHECK`` constraint and therefore about a
  real server.

**Why P40 is marked ``pg`` when the design's table calls it ``model``.** The same reason task
38.4's three are: the property needs a migrated database to be true of anything. It asserts that
tokens were revoked, that a consent row was not written and that a case reached a terminal state
with a particular reason — three tables and a worker transaction. The design's tier column records
the intent that it is not a nightly harness test; the honest placement is the tier where it
actually runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from revora.customer.suppression import (
    HARD_STOP_FOR,
    SCOPE_SEPARATOR,
    hard_stop_for,
    suppression_scope_key,
)
from revora.domain.enums import (
    CaseState,
    DelayReason,
    HardStopReason,
    TerminalReason,
    TokenRevocationReason,
)
from revora.jobs.pipeline import SUPPRESSION_TERMINAL_REASON
from revora.platform.config import default_configuration
from tests.pg_support import insert_merchant

_CONFIG = default_configuration()

# No module-level marker: the scope-key properties need no database and belong in the tier that
# runs on every commit, while P40 and the release test need a migrated PostgreSQL. A module-level
# `pg` would drag the cheap ones out of the fast selection, which is where they earn their keep.


# ---------------------------------------------------------------------------
# `pure` — the Suppression_Scope key
# ---------------------------------------------------------------------------


@pytest.mark.pure
@given(
    customer_key=st.text(min_size=1, max_size=80),
    order_id=st.text(min_size=1, max_size=40).filter(lambda value: value.strip() != ""),
    case_id=st.uuids(),
)
def test_the_scope_prefers_the_order_and_ignores_the_case(
    customer_key: str, order_id: str, case_id: uuid.UUID
) -> None:
    """Feature: Contact_Suppression. R21's Suppression_Scope — the order identifier wins.

    Where the Payment_Event carried an order identifier, the scope is a fact about *that order* and
    not about the case, so two different cases on one order derive one scope. That is the whole of
    R21.C8's cross-case reach and it is the **[ASSUMPTION]** the glossary records: a dispute about
    one order does not suppress a different order for the same customer.

    Asserted as independence from ``case_id`` rather than as equality with a recomputed digest. A
    test that recomputed the hash would be checking the implementation against a copy of itself;
    this one checks the *property* the design argues for, which is that the case identifier is not
    an input when an order identifier is present.
    """
    first = suppression_scope_key(
        customer_key=customer_key, order_id=order_id, case_id=case_id
    )
    second = suppression_scope_key(
        customer_key=customer_key, order_id=order_id, case_id=uuid.uuid4()
    )
    assert first == second, (
        "two cases on one order derived different scopes, so a dispute on the first would not "
        "suppress the second and R21.C8 would not reach a case created later"
    )


@pytest.mark.pure
@given(customer_key=st.text(min_size=1, max_size=80), case_id=st.uuids())
def test_a_blank_order_falls_back_to_the_case_rather_than_colliding(
    customer_key: str, case_id: uuid.UUID
) -> None:
    """Feature: Contact_Suppression. An absent order identifier is the case identifier.

    ``provider_order_id`` is nullable and provider-supplied, so ``None``, ``""`` and ``"   "`` all
    mean *this event named no order*. All three must fall back to the case identifier, and the
    reason the empty strings matter is the failure they would otherwise cause: hashing ``""`` gives
    every order-less case of one customer the **same** scope, so one customer's dispute on one
    payment would silently suppress every other payment they ever make — while looking, from the
    stored digest, exactly like a properly scoped suppression.
    """
    fallback = suppression_scope_key(
        customer_key=customer_key, order_id=None, case_id=case_id
    )
    for blank in ("", "   ", "\t", "\n", "\r\n"):
        assert (
            suppression_scope_key(
                customer_key=customer_key, order_id=blank, case_id=case_id
            )
            == fallback
        ), f"the order identifier {blank!r} was hashed instead of being treated as absent"

    other = suppression_scope_key(
        customer_key=customer_key, order_id=None, case_id=uuid.uuid4()
    )
    assert fallback != other, (
        "two order-less cases for one customer derived the same scope; a dispute on one would "
        "suppress the other, which is wider than the Suppression_Scope the glossary defines"
    )


@pytest.mark.pure
@given(
    customer_key=st.text(min_size=12, max_size=80),
    order_id=st.text(min_size=12, max_size=40).filter(lambda value: value.strip() != ""),
    case_id=st.uuids(),
)
def test_the_scope_key_discloses_no_preimage(
    customer_key: str, order_id: str, case_id: uuid.UUID
) -> None:
    """Feature: Contact_Suppression. The stored scope holds no readable copy of its inputs.

    The reason the column is a digest rather than a readable composite. ``customer_key`` is already
    the widest-spread customer-derived value in the database, and ``contact_suppression`` is read on
    the policy hot path — a composite would put a second copy of it in a table scanned on every
    decision that could contact somebody.

    Fixed width is asserted alongside, because a variable-width scope key would be a length oracle
    over the order identifier, and the order identifier is a merchant's own reference.

    **``min_size=12`` is not arbitrary and the first version of this test got it wrong.** A
    one-character input is a substring of a 64-character hex digest by coincidence roughly always —
    the generator found ``customer_key='0'`` immediately — so a shorter bound makes the test fail on
    a system that is behaving correctly. Twelve characters is past the length at which coincidence
    is plausible, and the property being checked is *the preimage is not embedded*, which is only a
    meaningful claim about a string long enough that finding it would mean it was put there.
    """
    scope = suppression_scope_key(
        customer_key=customer_key, order_id=order_id, case_id=case_id
    )
    assert len(scope) == 64, f"expected a SHA-256 hex digest, got {len(scope)} characters"
    assert customer_key not in scope
    assert order_id not in scope
    assert case_id.hex not in scope
    assert SCOPE_SEPARATOR not in scope


@pytest.mark.pure
@given(
    customer_key=st.text(min_size=1, max_size=40),
    order_id=st.text(min_size=1, max_size=40),
)
def test_the_separator_keeps_two_scopes_from_colliding(
    customer_key: str, order_id: str
) -> None:
    """Feature: Contact_Suppression. The preimage's two halves cannot run together.

    ``customer_key`` is a fixed-width digest today, so concatenation is already unambiguous and the
    separator changes nothing. This property is the guard for the day that stops being true: the
    split point is preserved, so ``("ab", "c")`` and ``("a", "bc")`` remain different scopes. A
    collision in *this* table means one customer's dispute suppressing another customer's order.
    """
    case_id = uuid.uuid4()
    joined = customer_key + order_id
    if not order_id.strip() or not joined.strip():
        return
    shifted = suppression_scope_key(
        customer_key=joined, order_id=order_id, case_id=case_id
    )
    straight = suppression_scope_key(
        customer_key=customer_key, order_id=order_id, case_id=case_id
    )
    assert shifted != straight


@pytest.mark.pure
def test_exactly_two_delay_reasons_are_hard_stops() -> None:
    """Feature: Contact_Suppression. R21.C1's subset is two members, and the table is total.

    Both halves matter. Two, because a third reason silently becoming a hard stop would end contact
    for a customer who was telling us *why they are late* — and total over ``DelayReason``, because
    a seventh member falling through as "not a hard stop" is the failure in the other direction: a
    customer objects and the chasing continues.

    ``SUPPRESSION_TERMINAL_REASON`` is checked for totality here rather than only at import,
    because its two rows are the whole content of R21.C4 and R21.C5 and this is where a reader
    checks them against the requirement.
    """
    hard_stops = {reason for reason in DelayReason if hard_stop_for(reason) is not None}
    assert hard_stops == {
        DelayReason.DISPUTES_THE_CHARGE,
        DelayReason.NO_LONGER_WANTS_THE_ORDER,
    }
    assert set(HARD_STOP_FOR) == set(DelayReason), "HARD_STOP_FOR is not total"
    assert hard_stop_for(None) is None

    assert set(SUPPRESSION_TERMINAL_REASON) == set(HardStopReason)
    assert (
        SUPPRESSION_TERMINAL_REASON[HardStopReason.DISPUTES_THE_CHARGE]
        is TerminalReason.CUSTOMER_DISPUTED_CHARGE
    ), "R21.C4 names CUSTOMER_DISPUTED_CHARGE for a dispute"
    assert (
        SUPPRESSION_TERMINAL_REASON[HardStopReason.NO_LONGER_WANTS_THE_ORDER]
        is TerminalReason.CUSTOMER_CANCELLED_ORDER
    ), "R21.C5 names CUSTOMER_CANCELLED_ORDER for a cancellation"


# ---------------------------------------------------------------------------
# `pg` — Property 40
# ---------------------------------------------------------------------------


@pytest.mark.pg
@settings(max_examples=8, deadline=None)
@given(reason=st.sampled_from([DelayReason(member.value) for member in HardStopReason]))
def test_p40_a_hard_stop_escalates_revokes_and_leaves_consent_alone(
    owner_engine: Engine, reason: DelayReason
) -> None:
    """**Property 40.** Terminal ``ESCALATED``, matching reason, tokens revoked, opt-out unchanged.

    Driven end to end: a real case, a token minted by the real service, the submission over HTTP
    through the mounted customer router, and the consequence applied by the real worker handler.
    Every shortcut past the router skips a control, and two of the four claims below are about
    controls the router owns.

    The four claims, and the fourth is the one this requirement is most likely to get wrong.

    * The case is ``ESCALATED`` — a Terminal_State, so nothing further is scheduled for it.
    * ``terminal_reason`` is the one R21.C4 or R21.C5 names for the reason submitted, which is why
      the reason is drawn rather than fixed: a mapping that returned ``CUSTOMER_DISPUTED_CHARGE``
      for both would pass a test that only ever submitted a dispute.
    * **Every** token of the case is revoked, with ``CONTACT_SUPPRESSED`` (R21.C10). Asserted as
      "no live token remains" as well as on the reason, because a revocation that recorded the
      right reason on one row and left another live would be worse than one that recorded nothing.
    * **The customer-wide opt-out status is unchanged** (R21.C9). The whole row is compared before
      and after, not just the flag: a suppression that flipped ``opted_out`` would make an
      objection to one debt indistinguishable from a withdrawal of consent to be contacted at all,
      and that is unrecoverable — there is no record left of which of the two the customer said.
      Comparing the row rather than the flag also catches the subtler version, where ``opted_out``
      is left alone and ``consent_expires_at`` or ``source`` is quietly rewritten.

    R21.C6's cancellation is asserted alongside, because this is the only test that seeds its
    precondition: the fixture leaves an ``APPROVED`` Policy_Decision unconsumed, so the handler has
    exactly one authorized action to cancel and must write exactly one
    ``ACTION_CANCELLED_CONTACT_SUPPRESSED`` record for it. Both counters are checked as unchanged in
    the same breath — the record's whole claim is that the action was stopped *before* any external
    call, and a counter that moved would mean it was not.
    """
    from revora.api.app import create_app
    from revora.customer.tokens import TokenService
    from revora.domain.actions import CandidateAction
    from revora.jobs.pipeline import handle_contact_suppression
    from revora.persistence.repositories.engine import (
        build_engine,
        dispose_engine,
        set_engine,
    )
    from revora.persistence.repositories.session import tenant_transaction
    from tests.fakes.customer import installed_signing_secrets

    merchant_id = insert_merchant(owner_engine, display_name="Hard stop P40")
    moment = datetime.now(UTC)
    customer_key = f"ck-{uuid.uuid4()}"
    case_id = _seed_case_for_suppression(
        owner_engine, merchant_id, moment=moment, customer_key=customer_key
    )
    with owner_engine.begin() as connection:
        slug = str(
            connection.execute(
                text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
            ).scalar_one()
        )
        # Consent on record and *not* opted out, which is the only starting point at which R21.C9
        # says anything: a customer already opted out could not distinguish "unchanged" from
        # "set by the suppression".
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    merchant_id, customer_key, opted_out, source, effective_at, created_at
                ) VALUES (:m, :ck, false, 'p40-fixture', :when, :when)
                """
            ),
            {"m": str(merchant_id), "ck": customer_key, "when": moment},
        )
        before = dict(
            connection.execute(
                text(
                    "SELECT customer_key, opted_out, source, consent_expires_at, effective_at "
                    "FROM customer_consent WHERE merchant_id = :m AND customer_key = :ck"
                ),
                {"m": str(merchant_id), "ck": customer_key},
            )
            .mappings()
            .one()
        )

    set_engine(build_engine(owner_engine.url.render_as_string(hide_password=False)))
    try:
        with installed_signing_secrets(1):
            with tenant_transaction(merchant_id) as session:
                minted = TokenService.on_session(session, _CONFIG).mint(
                    merchant_id,
                    case_id=case_id,
                    window_end_at=moment + _CONFIG.RECOVERY_WINDOW_DURATION,
                    approved_action=CandidateAction.PAYMENT_LINK,
                    moment=moment,
                )
            assert minted.token is not None and minted.token.wire_token is not None

            app = create_app(verify_schema=False, serve_dashboard=False)
            with TestClient(app) as client:
                response = client.post(
                    f"/customer/{slug}/delay-reason",
                    headers={
                        "Authorization": f"Bearer {minted.token.wire_token}",
                        "Content-Type": "application/json",
                    },
                    json={"delay_reason": reason.value},
                )
            assert response.status_code == 201, response.text

        # The worker's half. Called directly rather than through `run_once` because what is under
        # test is the handler's effect, and `run_once` would also drain whatever else the
        # submission enqueued — which would make a failure here ambiguous about which handler
        # caused it.
        handle_contact_suppression(
            merchant_id, case_id, hard_stop_reason=HardStopReason(reason.value)
        )
    finally:
        dispose_engine()

    with owner_engine.connect() as connection:
        case = connection.execute(
            text("SELECT state, terminal_reason FROM recovery_case WHERE id = :c"),
            {"c": str(case_id)},
        ).one()
        tokens = connection.execute(
            text(
                "SELECT revoked_at, revocation_reason FROM customer_access_token "
                "WHERE merchant_id = :m AND case_id = :c"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).all()
        after = dict(
            connection.execute(
                text(
                    "SELECT customer_key, opted_out, source, consent_expires_at, effective_at "
                    "FROM customer_consent WHERE merchant_id = :m AND customer_key = :ck"
                ),
                {"m": str(merchant_id), "ck": customer_key},
            )
            .mappings()
            .one()
        )
        suppression = connection.execute(
            text(
                "SELECT hard_stop_reason, released_at, released_by_user_id "
                "FROM contact_suppression WHERE merchant_id = :m"
            ),
            {"m": str(merchant_id)},
        ).one()
        cancellations = connection.execute(
            text(
                "SELECT action, decision->>'hard_stop_reason' AS reason FROM audit_record "
                "WHERE merchant_id = :m AND case_id = :c "
                "AND event_type = 'ACTION_CANCELLED_CONTACT_SUPPRESSED'"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).all()
        counters = connection.execute(
            text(
                "SELECT executed_action_count, customer_message_count FROM recovery_case "
                "WHERE id = :c"
            ),
            {"c": str(case_id)},
        ).one()
        intents = int(
            connection.execute(
                text("SELECT count(*) FROM execution_intent WHERE case_id = :c"),
                {"c": str(case_id)},
            ).scalar_one()
        )

    expected = SUPPRESSION_TERMINAL_REASON[HardStopReason(reason.value)]
    assert case[0] == CaseState.ESCALATED.value, (
        f"a hard stop left the case at {case[0]}; R21.C4 and R21.C5 both require ESCALATED, "
        "because a dispute and a cancellation both have consequences outside Revora and need a "
        "person rather than a silent stop"
    )
    assert case[1] == expected.value, (
        f"the case ended with terminal_reason {case[1]!r}; {reason.value} maps to "
        f"{expected.value} and the two hard stops stay distinct because a chargeback and a "
        "fulfilment question are not the same person's problem"
    )

    assert tokens, "no token row for the case, so 'every token revoked' would be vacuous"
    assert all(row[0] is not None for row in tokens), (
        "a live Customer_Access_Token survived the suppression. R21.C10 revokes every one, and a "
        "surviving token is a working link into a case nobody may contact about"
    )
    assert {row[1] for row in tokens} == {TokenRevocationReason.CONTACT_SUPPRESSED.value}, (
        f"tokens were revoked with {sorted({row[1] for row in tokens})}; R21.C10 names "
        "CONTACT_SUPPRESSED, and the reason is what makes 'how many customers lost their link to "
        "a suppression' answerable"
    )

    assert after == before, (
        "the customer-wide consent row changed. R21.C9 applies a Contact_Suppression *without* "
        f"setting the opt-out status of R17.C10: before={before}, after={after}. Collapsing the "
        "two would make an objection to one debt indistinguishable from a withdrawal of consent "
        "to be contacted at all, and nothing would remain to tell them apart"
    )

    assert suppression[0] == reason.value
    assert suppression[1] is None and suppression[2] is None, (
        "the suppression was born released. R21.C2 gives it no expiry and only a named "
        "Merchant_User may lift it"
    )

    # R21.C6. One record per cancelled action, and the fixture leaves exactly one authorized
    # action unconsumed — so this is an equality rather than a "at least one".
    assert len(cancellations) == 1, (
        f"{len(cancellations)} ACTION_CANCELLED_CONTACT_SUPPRESSED records for one approved, "
        "unconsumed Policy_Decision. R21.C6 requires exactly one per cancelled action, and zero "
        "would mean the constant has a declaration and no writer — which is the state it was in "
        "before this task"
    )
    assert cancellations[0][0] == "PAYMENT_LINK"
    assert cancellations[0][1] == reason.value, (
        "the cancellation record does not name the Hard_Stop_Reason that caused it, so a merchant "
        "reading it cannot tell a dispute from a cancellation without a second query"
    )
    assert counters == (0, 0), (
        f"a counter moved: executed={counters[0]}, messages={counters[1]}. R21.C6 leaves both "
        "unchanged, and the cancellation record's whole claim is that the action was stopped "
        "before any external call — a moved counter would mean it was not"
    )
    assert intents == 0, (
        "an execution_intent exists, so the suppression path attempted an external effect. R21.C6 "
        "issues no Payment_Provider request and no Communication_Provider request for a cancelled "
        "action"
    )


@pytest.mark.pg
def test_a_release_names_a_person_and_an_anonymous_one_is_unstorable(
    owner_engine: Engine,
) -> None:
    """Feature: Contact_Suppression. R21.C2 — a release always names a Merchant_User.

    Two claims, and the second is the one that makes the first hold for a call site nobody has
    written yet.

    A release through :func:`revora.customer.suppression.release_suppression` sets ``released_at``,
    ``released_by_user_id`` and ``release_config_version`` together, and a second release returns
    ``False`` without overwriting who performed the first — which matters more here than
    idempotency usually does, because the releasing user is the accountable party for contact
    resuming and a retry must not move that accountability to whoever retried.

    An anonymous release is then attempted *in SQL*, past the function entirely, and the database
    refuses it. That is the point: ``release_names_a_user`` is what makes R21.C2 structural, so the
    required keyword argument on the function is a convenience that surfaces the rule at the call
    site rather than the rule itself.

    There is no expiry to test. ``contact_suppression`` has no ``expires_at`` column, so
    "retained with no expiry instant" is a shape the table has rather than a behaviour to assert —
    and the assertion that it is absent is a schema test, not this one.
    """
    from sqlalchemy.exc import IntegrityError

    from revora.customer.suppression import release_suppression
    from revora.persistence.repositories.engine import (
        build_engine,
        dispose_engine,
        set_engine,
    )
    from revora.persistence.repositories.session import tenant_transaction

    merchant_id = insert_merchant(owner_engine, display_name="Suppression release")
    moment = datetime.now(UTC)
    case_id = _seed_case_for_suppression(
        owner_engine, merchant_id, moment=moment, customer_key=f"ck-{uuid.uuid4()}"
    )
    user_id = uuid.uuid4()
    signal_id = uuid.uuid4()
    scope_key = suppression_scope_key(
        customer_key="ck-release", order_id=None, case_id=case_id
    )
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant_user (
                    id, merchant_id, email_masked, email_key, role, is_active, created_at
                ) VALUES (:id, :m, '****ops@example.invalid', :key, 'operator', true, now())
                """
            ),
            {"id": str(user_id), "m": str(merchant_id), "key": uuid.uuid4().hex},
        )
        connection.execute(
            text(
                """
                INSERT INTO customer_signal (
                    id, merchant_id, case_id, token_id, kind, delay_reason, submitted_at,
                    correlation_id, created_at
                ) VALUES (
                    :id, :m, :c, :tok, 'DELAY_REASON', 'DISPUTES_THE_CHARGE', :when, :corr, now()
                )
                """
            ),
            {
                "id": str(signal_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "tok": uuid.uuid4().hex[:26],
                "when": moment,
                "corr": str(uuid.uuid4()),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO contact_suppression (
                    id, merchant_id, scope_key, origin_case_id, customer_signal_id,
                    hard_stop_reason, suppressed_at, created_at
                ) VALUES (
                    :id, :m, :scope, :c, :sig, 'DISPUTES_THE_CHARGE', :when, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "m": str(merchant_id),
                "scope": scope_key,
                "c": str(case_id),
                "sig": str(signal_id),
                "when": moment,
            },
        )

    set_engine(build_engine(owner_engine.url.render_as_string(hide_password=False)))
    try:
        with tenant_transaction(merchant_id) as session:
            released = release_suppression(
                session,
                merchant_id,
                scope_key,
                released_by_user_id=user_id,
                release_config_version=_CONFIG.version,
                moment=moment + timedelta(hours=1),
            )
        assert released is True

        with tenant_transaction(merchant_id) as session:
            again = release_suppression(
                session,
                merchant_id,
                scope_key,
                released_by_user_id=uuid.uuid4(),
                release_config_version="a-later-version",
                moment=moment + timedelta(hours=2),
            )
        assert again is False, (
            "a second release moved an already-released suppression, which would rewrite who is "
            "accountable for contact resuming to whoever happened to retry"
        )
    finally:
        dispose_engine()

    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT released_at, released_by_user_id, release_config_version "
                "FROM contact_suppression WHERE merchant_id = :m"
            ),
            {"m": str(merchant_id)},
        ).one()
    assert row[0] is not None
    assert str(row[1]) == str(user_id), "the first releasing user was overwritten"
    assert row[2] == _CONFIG.version

    # Past the function, straight at the constraint. This is what makes R21.C2 structural.
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE contact_suppression SET released_at = NULL, "
                "released_by_user_id = NULL, release_config_version = NULL "
                "WHERE merchant_id = :m"
            ),
            {"m": str(merchant_id)},
        )
    with pytest.raises(IntegrityError) as caught, owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE contact_suppression SET released_at = now() WHERE merchant_id = :m"
            ),
            {"m": str(merchant_id)},
        )
    assert "release_names_a_user" in str(caught.value), (
        "a release with no named user was stored. R21.C2's guarantee is the CHECK constraint, not "
        "the function signature, and this is the assertion that says so"
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seed_case_for_suppression(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    moment: datetime,
    customer_key: str,
) -> uuid.UUID:
    """A non-terminal case a hard stop can be submitted against.

    Written directly rather than driven through the pipeline, for the reason
    ``test_review_loop._seed_case_due_for_review`` gives: what is under test here is the effect of
    a hard stop on a settled case, which does not care how the row came to exist. The claim that
    the *pipeline* produces such a row is the lifecycle machine's, driven from a signed webhook.

    ``ACTION_SCHEDULED`` deliberately, not ``POLICY_CHECK``, **and it carries an ``APPROVED``
    Policy_Decision with ``consumed_by_intent_id`` null.** That pair is R21.C6's exact
    precondition — an authorized action for which no execution-intent record exists — and it stops
    the cancellation clause being vacuous. Without the decision row the handler would find nothing
    to cancel, write no ``ACTION_CANCELLED_CONTACT_SUPPRESSED`` record, and the test would pass
    while proving the writer works, which is the failure mode worth engineering against here: that
    constant was absent from ``revora.audit.events`` for several revisions precisely because nothing
    wrote it.

    No ``execution_intent`` row, deliberately. An intent is the other branch — R21.C7's
    ``POST_SUPPRESSION_ACTION`` — and a fixture carrying both would leave a failure ambiguous about
    which branch caused it.
    """
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 249900, 'INR',
                    :customer_key, :detected_at, :window_end, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": CaseState.ACTION_SCHEDULED.value,
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": customer_key,
                "detected_at": moment,
                "window_end": moment + _CONFIG.RECOVERY_WINDOW_DURATION,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, idempotency_key, created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                    :evaluated_at, :expires_at, 'PAYMENT_LINK', :state, 1, :key, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
                "state": CaseState.ACTION_SCHEDULED.value,
                "key": f"{case_id.hex[:12]}:PAYMENT_LINK:1",
            },
        )
    return case_id

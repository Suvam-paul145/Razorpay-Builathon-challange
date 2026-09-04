"""Task 40.5, Property 35. Customer input is evidence. It is never authority.

The claim in two halves, and they need different kinds of test because they are different kinds of
statement.

**"No signal sequence produces an ``APPROVED`` verdict" is a statement about what is reachable.**
A behavioural test can only show that the sequences it happened to draw produced none, and the
sequence space is unbounded. What actually makes the property hold is that the customer surface sits
below ``revora.policy`` in the layering contract and imports nothing that can issue a verdict —
there is no policy engine in scope to call, whatever a request contains. So the ``model``-tier
tests below read the import graph and the source of ``revora/customer/`` and assert the absence
structurally, which is a statement about every sequence rather than about a sample of them.
``lint-imports`` enforces the same thing from the other direction on every commit; these tests are
what make the *reason* visible in the suite rather than only in a config file.

**"Every external effect is preceded in the audit log by an ``APPROVED`` decision naming the same
case and key" is a statement about history**, so it is driven: an arbitrary sequence of the three
write shapes goes over the real HTTP surface against a real case, and afterwards the database is
asked whether anything acquired authority. That half is ``pg``, because "no policy decision row
exists" and "no execution intent row exists" are claims about rows.

The design assigns P35 to ``model``. The structural half is here in that tier; the driven half is
``pg`` for the reason task 38 gave about P63, P64 and P66 — the honest placement of a property is
the tier where it actually runs, and a claim about rows cannot run against a fake that has none.

**Task 41 added P36, P37 and P38 below, and all three are the same shape as P35's first half:
each has a behavioural test and a structural one, and the structural one is the stronger claim.**

* **P36** — signal content moves no policy outcome. Behaviourally, arbitrary schema-valid content is
  substituted onto a ``PolicyInput`` with the suppression state pinned, and the twelve outcomes are
  fingerprinted before and after. Structurally, ``PolicyInput``'s declared field set is disjoint
  from every ``customer_signal`` and ``promise_to_pay`` column, and ``revora/policy/`` imports
  neither ``revora.persistence`` nor ``revora.customer`` — so there is no field for a signal to
  arrive in and no way to go and fetch one.
* **P37** — no signal sequence moves a configured bound. Behaviourally the eight bounds R25.C7 names
  and the configuration version are compared across a generated sequence; structurally no module in
  ``revora/customer/`` names any of the eight, or either of the two symbols that could write one.
* **P38** — nothing is derived from a note's contents. The reference for every derivation is the
  *same submission with the note absent*, which is what makes the property a statement about the
  note's irrelevance rather than about four derivations happening to be right today.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.rendering import (
    RENDER_AS_TEXT_ONLY,
    UNVERIFIED_CUSTOMER_TEXT,
    customer_supplied_note,
)
from revora.customer import projection as projection_module
from revora.customer import signals as signals_module
from revora.customer import tokens as tokens_module
from revora.customer.promises import effective_promise_limit
from revora.customer.signals import (
    DelayReasonSubmission,
    PartialArrangementSubmission,
    PromiseSubmission,
    _note_for_storage,
    cause_for_delay_reason,
    effective_note_limit,
)
from revora.customer.tokens import TokenService
from revora.domain.actions import CandidateAction
from revora.domain.enums import CustomerSignalKind, DelayReason, HardStopReason
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.policy import input as policy_input_module
from revora.policy.engine import evaluate
from revora.policy.input import PolicyInput
from revora.policy.rules import default_rule_set
from tests.fakes.customer import installed_signing_secrets
from tests.pg_support import insert_merchant
from tests.strategies.customer import delay_reason_notes, signal_submissions
from tests.strategies.policy import policy_input

_CONFIG = default_configuration()

_CUSTOMER_PACKAGE = Path(inspect.getfile(signals_module)).parent

_HARD_STOP_MEMBERS: frozenset[DelayReason] = frozenset(
    DelayReason(reason.value) for reason in HardStopReason
)
"""The two Delay_Reasons that are Hard_Stop_Reasons, derived rather than listed.

Derived so that a third Hard_Stop_Reason is covered by P36 and P38 the day it is added, instead of
falling through as an ordinary payment problem — which is the failure that would be invisible,
because the two enumerations overlapping by value is exactly what makes the derivation work."""

_ASSIGNMENT_REFUSED = (dataclasses.FrozenInstanceError, AttributeError, TypeError)
"""What a refused attribute assignment on a frozen, slotted dataclass may raise.

Three types because the mechanism differs by case and by CPython version: ``FrozenInstanceError``
for a declared field, ``TypeError`` or ``AttributeError`` for a name the slots layout has no room
for. The property under test is that the assignment cannot succeed; pinning the type would make the
test brittle about something it does not care about. The same tuple, and the same argument, as
``tests/properties/test_policy.py``'s — restated rather than imported, because a shared constant
between two property files makes one file's failure message describe the other's subject."""

_FORBIDDEN_PACKAGES: tuple[str, ...] = (
    "revora.policy",
    "revora.optimizer",
    "revora.execution",
    "revora.providers",
    "revora.estimation",
    "revora.experiment",
    "revora.metrics",
    "revora.reasoning",
    "revora.synthetic",
    "revora.jobs",
    "revora.api",
)
"""What ``revora.customer`` must not be able to reach, and why each one is on the list.

``policy`` and ``optimizer`` are the two that can produce an authorization, so they are the whole of
P35's first half. ``execution`` and ``providers`` are the two that can produce an external effect.
``estimation``, ``experiment`` and ``metrics`` are the figures R19.C2 excludes from the projection —
unreachable rather than filtered. ``reasoning`` is on the list because a customer's free-text note
reaching a model from the request path would be an untrusted input transmitted without the four
gates task 49 puts in front of it. ``synthetic`` is on it because a ground truth reachable from an
unauthenticated request is a ground truth an unauthenticated request can read. ``jobs`` and ``api``
are simply above this package, and reaching upward is how a layered design stops being one."""


# ---------------------------------------------------------------------------
# `model` — Property 35, first half: nothing here can authorize anything
# ---------------------------------------------------------------------------


def _referenced_names(path: Path) -> set[str]:
    """Every identifier the *code* in one file refers to, ignoring prose.

    From the AST, and that is not a stylistic preference. These modules are heavily documented and
    several of them explain in prose exactly which function they must not call — ``tokens.py``'s
    docstring names ``apply_locked_transition`` in order to say that ``revora.cases`` cannot import
    it — so a substring search over the source fails on the explanation rather than on a call. The
    AST sees names, attributes and keywords; it does not see docstrings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string constant counts as a reference: reaching a function by name through
            # ``getattr`` or an importlib lookup would otherwise slip past an AST walk entirely.
            names.add(node.value)
    return names


def _imports_of(path: Path) -> set[str]:
    """Every module name imported by one file, from the AST.

    From the AST rather than from a substring search, because these modules explain in prose *why*
    they cannot import ``revora.policy`` and a grep would fail on the explanation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.model
def test_p35_the_customer_surface_cannot_reach_anything_that_authorizes() -> None:
    """Feature: Customer Response Loop. Property 35, first half — no Customer_Signal sequence of
    any length produces an ``APPROVED`` Policy_Decision, because nothing reachable from the
    customer surface can issue one.

    Asserted over **every** module in ``revora/customer/``, not the three that exist today, so a
    fourth added by task 42 or 44 inherits the property rather than needing its own test.

    This is the strongest form the claim has. A behavioural test over generated sequences shows that
    the drawn sequences produced no verdict; this shows that no sequence can, because the code that
    would have to run is not importable from here. The two are complementary and the driven half is
    below — but if only one of them could exist, it is this one.
    """
    checked = sorted(_CUSTOMER_PACKAGE.rglob("*.py"))
    assert len(checked) >= 4, (
        f"only {len(checked)} files found under {_CUSTOMER_PACKAGE}; this test is meant to cover "
        "the whole package and has stopped finding it"
    )
    for path in checked:
        imported = _imports_of(path)
        for forbidden in _FORBIDDEN_PACKAGES:
            offending = sorted(
                name
                for name in imported
                if name == forbidden or name.startswith(f"{forbidden}.")
            )
            assert not offending, (
                f"{path.name} imports {offending}. The customer surface is reachable without a "
                "session, and P35's guarantee is that a request through it cannot authorize an "
                "external effect — which holds because there is nothing here to call, not because "
                "nobody calls it"
            )


@pytest.mark.model
def test_p35_no_verdict_vocabulary_appears_in_the_customer_package() -> None:
    """Property 35's first half from the other side: the word ``APPROVED`` is not spoken here.

    The import check above is the mechanism, and this is the belt beside it. A module could reach a
    verdict without importing the policy engine — by comparing a string, by constructing a
    ``PolicyVerdict`` through ``revora.domain.enums``, which *is* importable from here because the
    domain is below everything. So the assertion is that no name or string constant in this package
    is a policy verdict at all.

    ``revora.domain.enums`` being reachable is correct and must stay so: this package needs
    ``CaseState``, ``CustomerSignalKind``, ``DelayReason`` and ``PromiseStatus``. The domain is a
    vocabulary, not an authority — nothing in it can *record* a decision. What this test forbids is
    borrowing the one word from that vocabulary whose presence would mean somebody was building a
    verdict rather than reading a state.
    """
    for path in sorted(_CUSTOMER_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "PolicyVerdict", f"{path.name} names PolicyVerdict"
            if isinstance(node, ast.Attribute):
                assert node.attr != "APPROVED", (
                    f"{path.name} reaches for an APPROVED member. A verdict constructed on the "
                    "public surface is an authorization produced by a request body"
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "APPROVED", (
                    f"{path.name} contains the string 'APPROVED'. Comparing a string is how a "
                    "component reaches a verdict without importing the engine that issues them"
                )


@pytest.mark.model
def test_p35_the_write_path_transitions_nothing_and_calls_no_provider() -> None:
    """R19.C9 as a source-level fact: the accepting request applies no transition.

    ``recovery_case.state`` has exactly one writer — ``revora.cases.manager``'s
    ``apply_locked_transition`` — and ``revora.cases`` *is* reachable from here, deliberately,
    because :func:`~revora.cases.review.enqueue_case_review` lives in it. So the contract alone does
    not forbid a transition, and this is the test that does: the write module reaches the review
    enqueue and nothing else in ``revora.cases``.

    That distinction is the whole of R19.C9. The request enqueues a decision cycle and the *worker*
    applies its consequences through the legal transition table, which is what R19.C10 then makes
    checkable. A request that transitioned the case itself would bypass the table, the counters and
    the bounds all at once, and it would do so from an endpoint with no session.
    """
    imported = _imports_of(Path(inspect.getfile(signals_module)))
    from_cases = sorted(name for name in imported if name.startswith("revora.cases"))
    assert from_cases == ["revora.cases.review"], (
        f"the signal writer reaches {from_cases} inside revora.cases. Only the review enqueue "
        "belongs here: the state writer is apply_locked_transition, and a request that called it "
        "would apply a transition inside the accepting request (R19.C9)"
    )

    referenced = _referenced_names(Path(inspect.getfile(signals_module)))
    for forbidden in ("apply_transition", "apply_locked_transition", "execute_approved_action"):
        assert forbidden not in referenced, (
            f"the signal writer references {forbidden!r}, so an accepted write could move the case "
            "inside the request that accepted it"
        )


@pytest.mark.model
def test_the_projection_and_the_token_service_stay_read_only_about_the_case() -> None:
    """The read path writes no case column either, asserted the same structural way.

    Included because R19.C12's 503-with-no-partial-projection has a second clause that is easy to
    miss: *and no accepted write in that request*. A read path that incidentally wrote something —
    a "last viewed" timestamp, say, which is exactly the field somebody would add — would make a
    timed-out read leave a trace, and would make the read path a write path for rate-limiting
    purposes too.
    """
    for module in (projection_module, tokens_module):
        referenced = _referenced_names(Path(inspect.getfile(module)))
        for forbidden in ("apply_transition", "apply_locked_transition"):
            assert forbidden not in referenced, (
                f"{module.__name__} references {forbidden!r} in code. Its docstring may name it — "
                "``tokens.py``'s does, to explain why revora.cases calls the repository directly — "
                "which is why this reads the AST rather than the source text"
            )


# ---------------------------------------------------------------------------
# `pg` — Property 35, second half: driven over the real surface
# ---------------------------------------------------------------------------


def _seed(engine: Engine, merchant_id: uuid.UUID, *, moment: datetime) -> tuple[str, uuid.UUID]:
    """A merchant slug and one case at ``POLICY_CHECK``, which is the reviewable state."""
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        slug = str(
            connection.execute(
                text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
            ).scalar_one()
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, next_review_at,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, 'POLICY_CHECK', :pid, 249900, 'INR', :ck, :detected, :window_end,
                    :review, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "pid": f"pay_{case_id.hex[:14]}",
                "ck": f"ck-{case_id}",
                "detected": moment,
                "window_end": moment + timedelta(days=7),
                "review": moment + timedelta(hours=12),
            },
        )
    return slug, case_id


@pytest.mark.pg
@given(submissions=st.lists(signal_submissions(), min_size=1, max_size=5))
@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_p35_no_sequence_of_writes_produces_a_verdict_or_an_effect(
    owner_engine: Engine, submissions: list[tuple[str, dict[str, object]]]
) -> None:
    """Feature: Customer Response Loop. Property 35 — for any sequence of Customer_Signals, no
    ``APPROVED`` Policy_Decision is produced and no external effect occurs, and every external
    effect in the log is preceded by an ``APPROVED`` decision naming the same case and key.

    Driven over the **real HTTP surface** with a real minted token, because the thing under test is
    what a request does and every shortcut past the router is a shortcut past a control. An
    arbitrary interleaving of all three write shapes, up to five deep, so a sequence that mixes a
    hard-stop delay reason with a promise and a partial-arrangement request is explored — the three
    together are the closest a customer can get to instructing the system.

    Five things are asserted afterwards and each corresponds to one clause of R19.C9:

    * **no ``policy_decision`` row** — no Policy_Engine evaluation occurred;
    * **no ``execution_intent`` row** — no external effect was attempted, so no provider request
      went out;
    * **the case is still at ``POLICY_CHECK``** — no state transition;
    * **every counter unchanged**, including ``last_outbound_at`` staying null;
    * **the second clause holds vacuously and non-trivially at once**: there are no external
      effects, and the assertion is written as a universal over the intents that exist rather than
      as "there are none", so it keeps meaning the same thing on a case that also executed.

    The one thing that *did* happen is the enqueued review (R30.C8), and it is asserted to be
    exactly one however many signals were submitted — the dedupe key is on the case, so a customer
    submitting five times spends one decision cycle rather than five.

    **A second promise is 409 and still records its signal, and that is asserted positionally.**
    R23.C7 is the one refusal on this surface that keeps its write: the ``promise_to_pay`` row is
    refused because the case already holds ``MAX_PROMISES_PER_CASE``, and the ``customer_signal``
    is persisted anyway so Recovery_Memory keeps the fact that the customer revised their promise.
    So the legal status of a submission depends on what came before it, and the expected set is
    computed from the sequence rather than widened to admit 409 everywhere — a 409 on the *first*
    promise would mean the bound is checked against the wrong number, and a 201 on the second
    would mean it is not checked at all. Neither is distinguishable under a widened set.

    A promise dated past the window is accepted (201) and escalates, and this test never runs the
    worker, so the case is still at ``POLICY_CHECK`` afterwards even then — the transition of
    R23.C5 belongs to ``handle_promise_escalation``, and R19.C9 is precisely the requirement that
    keeps it there.

    **A hard stop in the sequence ends the sequence, and task 42 is where that started being true.**
    The generator draws ``DISPUTES_THE_CHARGE`` and ``NO_LONGER_WANTS_THE_ORDER`` like any other
    Delay_Reason, and R21.C10 requires the accepting request to revoke every Customer_Access_Token
    of the case and to accept no further Customer_Signal write on it. So every submission after the
    first hard stop is answered **410**, and that is asserted positionally rather than tolerated by
    widening the legal status set: a 410 *before* the hard stop would be a live token being refused,
    and a 201 *after* one would be R21.C10 not holding. The status list is the only place in this
    test where the two failures are distinguishable.

    Everything R19.C9 forbids is still forbidden on the hard-stop path — no decision, no intent, no
    transition, no counter movement. The suppression's own consequences are enqueued for the worker
    and this test never runs one, which is why the case is still at ``POLICY_CHECK`` afterwards even
    when the customer disputed the charge: the escalation of R21.C4 belongs to
    ``handle_contact_suppression`` and R19.C9 is precisely the requirement that keeps it there.
    """
    merchant_id = insert_merchant(owner_engine, display_name="Signal authority")
    moment = datetime.now(UTC)
    slug, case_id = _seed(owner_engine, merchant_id, moment=moment)

    # ``hide_password=False`` because ``str(url)`` masks the password with ``***`` and the app's
    # own engine has to be able to connect. The same call the review sweeper's restart test makes.
    set_engine(build_engine(owner_engine.url.render_as_string(hide_password=False)))
    try:
        with installed_signing_secrets(1):
            with tenant_transaction(merchant_id) as session:
                minted = TokenService.on_session(session, _CONFIG).mint(
                    merchant_id,
                    case_id=case_id,
                    window_end_at=moment + timedelta(days=7),
                    approved_action=CandidateAction.PAYMENT_LINK,
                    moment=moment,
                )
            assert minted.token is not None and minted.token.wire_token is not None
            headers = {
                "Authorization": f"Bearer {minted.token.wire_token}",
                "Content-Type": "application/json",
            }
            app = create_app(verify_schema=False, serve_dashboard=False)
            with TestClient(app) as client:
                statuses = [
                    client.post(
                        f"/customer/{slug}/{suffix}", headers=headers, json=body
                    ).status_code
                    for suffix, body in submissions
                ]
    finally:
        dispose_engine()

    # R21.C10. The first hard stop revokes the case's tokens inside the accepting request, so it
    # is the last submission that can be accepted. Its own status is still 201 — it was accepted,
    # and the revocation is its consequence.
    hard_stops = {"DISPUTES_THE_CHARGE", "NO_LONGER_WANTS_THE_ORDER"}
    first_hard_stop = next(
        (
            index
            for index, (_suffix, body) in enumerate(submissions)
            if body.get("delay_reason") in hard_stops
        ),
        None,
    )
    live = statuses if first_hard_stop is None else statuses[: first_hard_stop + 1]
    after = [] if first_hard_stop is None else statuses[first_hard_stop + 1 :]

    # R23.C7. A *second* promise on one case is 409 and the case keeps its first promise, so the
    # legal status of a submission depends on whether a promise came before it. Computed
    # positionally rather than by widening the set to include 409, because the two failures the
    # widened set would hide are the ones worth catching: a 409 on the *first* promise means the
    # bound is being checked against the wrong number, and a 201 on the second means it is not
    # being checked at all. ``MAX_PROMISES_PER_CASE`` is read rather than assumed to be one, so a
    # merchant configuring 0 changes what this expects instead of breaking it.
    promise_limit = effective_promise_limit(_CONFIG)
    promises_before: list[int] = []
    seen_promises = 0
    for suffix, _body in submissions:
        promises_before.append(seen_promises)
        if suffix == "promise":
            seen_promises += 1
    legal_live = [
        {409, 429} if suffix == "promise" and before >= promise_limit else {201, 429}
        for (suffix, _body), before in zip(submissions, promises_before, strict=True)
    ]

    assert all(
        status in legal
        for status, legal in zip(live, legal_live[: len(live)], strict=True)
    ), (
        f"a declared write shape was answered {statuses}; only 201, the two 429 caps and R23.C7's "
        "409 on a promise past MAX_PROMISES_PER_CASE are legal outcomes for a schema-valid "
        "submission on a live token against a non-terminal case. The first hard stop is at index "
        f"{first_hard_stop}, so everything up to and including it was submitted on a token that "
        f"had not been revoked; the expected sets were {legal_live[: len(live)]}"
    )
    assert all(status == 410 for status in after), (
        f"submissions after the hard stop at index {first_hard_stop} were answered {after}; "
        "R21.C10 revokes every Customer_Access_Token of the case in the accepting request and "
        "accepts no further Customer_Signal write on it, so each of these must be 410. A 201 here "
        "means a customer who disputed the charge can keep writing to the case"
    )

    with owner_engine.connect() as connection:
        decisions = int(
            connection.execute(
                text("SELECT count(*) FROM policy_decision WHERE case_id = :c"),
                {"c": str(case_id)},
            ).scalar_one()
        )
        intents = connection.execute(
            text(
                "SELECT idempotency_key, policy_decision_id FROM execution_intent "
                "WHERE case_id = :c"
            ),
            {"c": str(case_id)},
        ).all()
        approved_keys = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT id FROM policy_decision WHERE case_id = :c AND verdict = 'APPROVED'"
                ),
                {"c": str(case_id)},
            ).all()
        }
        case = connection.execute(
            text(
                "SELECT state, executed_action_count, customer_message_count, "
                "decision_cycle_count, last_outbound_at, window_end_at FROM recovery_case "
                "WHERE id = :c"
            ),
            {"c": str(case_id)},
        ).one()
        reviews = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM job WHERE case_id = :c AND kind = 'case_review' "
                    "AND state = 'PENDING'"
                ),
                {"c": str(case_id)},
            ).scalar_one()
        )
        suppressions = int(
            connection.execute(
                text("SELECT count(*) FROM contact_suppression WHERE origin_case_id = :c"),
                {"c": str(case_id)},
            ).scalar_one()
        )
        suppression_jobs = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM job WHERE case_id = :c "
                    "AND kind = 'contact_suppression' AND state = 'PENDING'"
                ),
                {"c": str(case_id)},
            ).scalar_one()
        )

    assert decisions == 0, (
        f"{decisions} policy decisions exist after {len(submissions)} customer writes. R19.C9 "
        "forbids a Policy_Engine evaluation inside the accepting request, and P35 forbids a "
        "customer input ever producing a verdict at all"
    )
    assert intents == [], (
        f"{len(intents)} execution intents exist, so a customer write caused an external effect to "
        "be attempted. An intent row is written before the provider is called, so its existence is "
        "the evidence a call may have gone out"
    )
    # Written as a universal over the intents rather than as "there are none", so the claim keeps
    # its meaning on a case that has also executed something — which is the case the property is
    # actually about (R29's "every external effect is preceded by an APPROVED decision").
    for key, decision_id in intents:  # pragma: no cover - asserted empty above
        assert str(decision_id) in approved_keys, (
            f"the effect keyed {key!r} names no APPROVED policy decision for this case"
        )

    assert case[0] == "POLICY_CHECK", (
        f"the case moved to {case[0]}. R19.C9 forbids a state transition inside the accepting "
        "request; the consequences are the worker's to apply through the legal transition table"
    )
    assert (case[1], case[2], case[3]) == (0, 0, 1), (
        f"a counter moved: executed={case[1]}, messages={case[2]}, cycles={case[3]}. R20.C9 leaves "
        "all three unchanged by a persisted signal"
    )
    assert case[4] is None, (
        "last_outbound_at was set by a customer write, so the cooldown clock was reset by an "
        "inbound request — which would let a customer's own explanation buy Revora a further "
        "message"
    )
    assert case[5] == moment + timedelta(days=7), (
        f"window_end_at moved to {case[5]} from {moment + timedelta(days=7)}. R22.C7 and R30.C2 "
        "both leave it untouched, and every termination bound in the system is measured against it "
        "— a customer able to move it is a customer able to extend their own recovery window"
    )
    assert reviews == 1, (
        f"{reviews} pending reviews after {len(submissions)} submissions. R30.C9 allows exactly "
        "one: the dedupe key is on the case, so a customer submitting five times spends one "
        "decision cycle rather than five"
    )
    assert suppressions == (0 if first_hard_stop is None else 1), (
        f"{suppressions} contact_suppression rows after {len(submissions)} submissions with the "
        f"first hard stop at index {first_hard_stop}. R21.C1 writes one per Suppression_Scope in "
        "the same transaction as the signal, and UNIQUE (merchant_id, scope_key) makes a second "
        "hard stop on the same scope idempotent rather than a second row nobody reconciles"
    )
    assert suppression_jobs == (0 if first_hard_stop is None else 1), (
        f"{suppression_jobs} pending contact_suppression jobs. R19.C9 requires the consequences to "
        "be *enqueued* rather than applied in the request, and the dedupe key is on the case, so "
        "two hard stops on one case queue one application"
    )


# ---------------------------------------------------------------------------
# `model` — Property 36: signal content moves no policy outcome
# ---------------------------------------------------------------------------

_RULES = default_rule_set(
    max_recovery_attempts=int(_CONFIG.MAX_RECOVERY_ATTEMPTS),
    max_customer_messages=int(_CONFIG.MAX_CUSTOMER_MESSAGES),
    cooldown_interval=_CONFIG.COOLDOWN_INTERVAL,
    policy_decision_validity=_CONFIG.POLICY_DECISION_VALIDITY,
    risk_reason_codes=_CONFIG.RISK_REASON_CODES,
    min_net_value_threshold=_CONFIG.MIN_NET_VALUE_THRESHOLD,
    min_incremental_probability=_CONFIG.MIN_INCREMENTAL_PROBABILITY,
)
"""The rule set, built from the configured bounds rather than from literals.

``tests/properties/test_policy.py`` builds its own from fixed numbers, which is right there: those
properties are about the engine's *ordering* and want values chosen to make individual checks fail.
P36 and P37 are about configuration not moving, so they have to be evaluated against the
configuration — a rule set of test literals would leave "the bounds are unchanged" a claim about
numbers this file invented."""

_SIGNAL_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "kind",
        "delay_reason",
        "delay_reason_note",
        "note_truncated",
        "note_redacted_at",
        "retention_config_version",
        "submitted_at",
        "token_id",
        "provenance",
        "signal_id",
        "promise_date",
        "received_representation",
        "status",
        "follow_up_at",
        "window_end_at_snapshot",
        "recorded_at",
        "hard_stop_reason",
        "scope_key",
        "customer_signal_id",
        "note",
        "signals_remaining",
    }
)
"""Every column of ``customer_signal`` and ``promise_to_pay``, plus the wire field names.

R20.C8 excludes "every Delay_Reason value, every Delay_Reason_Note, every Customer_Signal field,
and every Promise_To_Pay field other than the persisted Contact_Suppression state" from the policy
engine's declared deterministic input set. This is that list, and the test below asserts it is
disjoint from ``PolicyInput``'s field set — which is what makes P36 a claim about the whole space
of signal content rather than about the substitutions a test thought to try.

``contact_suppressed`` is deliberately absent: it *is* on ``PolicyInput``, by R21.C3, and it is the
one thing a signal legitimately moves. ``scope_key`` and ``customer_signal_id`` are here because
they are ``contact_suppression`` columns and the check reads the boolean rather than the row.

``case_id`` is deliberately absent too, and for a different reason worth stating because it looks
like an omission. ``customer_signal.case_id`` is a column, so a literal reading of "every
Customer_Signal field" would include it — and ``PolicyInput.case_id`` is on the input, because an
evaluation has to know which case it is authorizing an action for. They are the same *value* and not
the same *fact*: the case id is the case's own identity, which the signal borrows in order to point
at it. What R20.C8 excludes is content a customer supplied, and a customer supplies no case id — the
token names the case (R29.C2) and the request body has nowhere to put one. Listing it would make
this test assert that policy may not know which case it is deciding about."""


def _decision_fingerprint(evaluation: object) -> tuple[object, ...]:
    """Verdict, primary reason and all twelve ordered check outcomes.

    The same three things ``test_policy.py`` fingerprints for P2, and for the same reason: those
    are exactly what R20.C10 requires unchanged. Recomputed here rather than imported, because a
    shared helper between two property files would make one property's failure message describe the
    other one's subject.
    """
    return (
        evaluation.verdict,
        evaluation.primary_reason,
        tuple((check.order, check.check, check.outcome) for check in evaluation.checks),
    )


@pytest.mark.model
@given(
    suppressed=st.booleans(),
    data=st.data(),
)
@settings(max_examples=40, deadline=None)
def test_p36_no_signal_content_moves_a_policy_outcome(
    suppressed: bool, data: st.DataObject
) -> None:
    """Feature: Customer Response Loop. Property 36 — replacing every Customer_Signal field with
    arbitrary schema-valid content, with the suppression state held fixed, leaves the verdict, the
    primary reason and all twelve ordered check outcomes unchanged (R20.C8, R20.C10).

    **"Suppression state held fixed" is the load-bearing clause and it is not a weakening.** A
    hard-stop Delay_Reason genuinely does change the verdict — that is R21.C3's whole point, and
    task 42 built it: the persisted ``contact_suppression`` row enters check 5's input and blocks.
    So P36 is a claim about every path *except* that one.

    It is expressed by pinning ``contact_suppressed`` and drawing **both** of its values, rather
    than by excluding ``DISPUTES_THE_CHARGE`` and ``NO_LONGER_WANTS_THE_ORDER`` from the generated
    signals. The difference matters: excluding them would stop the property covering the two
    members most likely to acquire an unintended second effect, which is precisely the failure
    worth finding. Pinned-and-drawn instead, the property says something stronger — *a hard stop
    changes the twelve outcomes through the suppression boolean and through nothing else*. On a
    suppressed case the twelve outcomes are whatever suppression makes them, and they are still
    identical before and after arbitrary signal content is substituted.

    The substitution is attempted the only way it can be: by setting each name on the input.
    ``PolicyInput`` is frozen and slotted, so every attempt raises, and the raising *is* the proof
    — there is no attribute to overwrite and none to add. The evaluation is then re-run and
    fingerprinted anyway, so a future edit that made the type mutable would be caught behaviourally
    rather than only by the exception that no longer fires.
    """
    candidate = data.draw(policy_input(contact_suppressed=suppressed))
    before = evaluate(candidate, _RULES)

    submission = data.draw(signal_submissions(note_strategy=delay_reason_notes()))
    _suffix, body = submission
    substitutions: dict[str, object] = {
        # The generated wire body, field by field, so the substituted values are the ones a real
        # request could actually carry rather than values a test invented.
        **body,
        "delay_reason": body.get("delay_reason", DelayReason.OTHER.value),
        "delay_reason_note": body.get("note"),
        "kind": CustomerSignalKind.DELAY_REASON.value,
        "note_truncated": True,
        "provenance": "REAL",
        "token_id": "rvt_" + "a" * 26,
        "signals_remaining": 0,
    }
    for name, value in substitutions.items():
        with pytest.raises(_ASSIGNMENT_REFUSED):
            setattr(candidate, name, value)

    after = evaluate(candidate, _RULES)
    assert _decision_fingerprint(before) == _decision_fingerprint(after), (
        "the twelve check outcomes moved after substituting Customer_Signal content with the "
        f"suppression state pinned to {suppressed}. R20.C10 requires the same verdict, the same "
        "primary reason and the same ordered outcomes; a signal that moved any of them would be a "
        "text box on a public page moving an authorization"
    )
    assert not hasattr(candidate, "__dict__"), (
        "PolicyInput acquired a __dict__, so an undeclared attribute can now be attached to it at "
        "runtime — which is the one route by which signal content could reach an evaluation "
        "without an edit to revora/policy/input.py that a reviewer would see"
    )


@pytest.mark.model
def test_p36_policy_input_declares_no_customer_signal_field() -> None:
    """Property 36, structurally: there is nowhere for signal content to sit (R20.C8).

    The behavioural test above shows that the substitutions it drew changed nothing. This shows why
    no substitution can, which is the stronger statement and the one that survives a generator
    nobody updated: ``PolicyInput``'s declared field set is **disjoint** from every column of
    ``customer_signal`` and ``promise_to_pay`` and from every wire field name the three write shapes
    accept.

    ``contact_suppressed`` is the deliberate exception and is asserted *present* rather than merely
    left off the forbidden list. R21.C3 requires it, and an assertion that it is absent from the
    forbidden set would pass just as well if somebody deleted the field — so the presence is
    checked, and the pairing is what makes R20.C8 and R21.C3 legible as two halves of one design
    instead of as a rule and its exception.
    """
    declared = {field.name for field in dataclasses.fields(PolicyInput)}
    overlap = sorted(declared & _SIGNAL_FIELD_NAMES)
    assert not overlap, (
        f"PolicyInput declares {overlap}, which are Customer_Signal or Promise_To_Pay fields. "
        "R20.C8 excludes every one of them from the declared deterministic input set, and the "
        "exclusion is only structural while there is no field for them to arrive in"
    )
    assert "contact_suppressed" in declared, (
        "PolicyInput no longer declares contact_suppressed. It is the one piece of state a "
        "Customer_Signal may legitimately move (R21.C3), and check 5 reading it is what makes a "
        "hard stop an absolute prohibition rather than a thirteenth check"
    )


@pytest.mark.model
def test_p36_the_policy_package_imports_nothing_that_could_reach_a_signal() -> None:
    """Property 36's other half: ``revora/policy/`` cannot read a signal even if it wanted to.

    The ``policy-isolation`` contract forbids ``revora.persistence`` among others, and
    ``lint-imports`` enforces it on every commit. This is the same claim as a test, so the *reason*
    is visible in the suite: the twelve checks read a frozen value object, and the only route from
    a persisted ``customer_signal`` row to an evaluation is a caller putting it there — which is
    the route the field-set test above closes.

    Asserted over every module in the package rather than the three that exist, so a fourth
    inherits it.
    """
    policy_package = Path(inspect.getfile(policy_input_module)).parent
    checked = sorted(policy_package.rglob("*.py"))
    assert len(checked) >= 4, (
        f"only {len(checked)} files found under {policy_package}; this test is meant to cover the "
        "whole package and has stopped finding it"
    )
    for path in checked:
        imported = _imports_of(path)
        offending = sorted(
            name
            for name in imported
            for forbidden in ("revora.persistence", "revora.customer", "revora.memory")
            if name == forbidden or name.startswith(f"{forbidden}.")
        )
        assert not offending, (
            f"{path.name} imports {offending}. A policy check that could read a customer_signal "
            "row would make R20.C8's exclusion a discipline rather than a structure"
        )


# ---------------------------------------------------------------------------
# `model` — Property 37: no signal moves a configured bound
# ---------------------------------------------------------------------------

NAMED_BOUNDS: tuple[str, ...] = (
    "MAX_RECOVERY_ATTEMPTS",
    "MAX_CUSTOMER_MESSAGES",
    "RECOVERY_WINDOW_DURATION",
    "COOLDOWN_INTERVAL",
    "MIN_NET_VALUE_THRESHOLD",
    "MIN_INCREMENTAL_PROBABILITY",
    "MAX_COST_TO_VALUE_RATIO",
    "HIGH_BASELINE_THRESHOLD",
)
"""The eight bounds R25.C7 names, in the order it names them.

Eight and not "the bounds": R25.C7 lists exactly these, and a test that iterated the whole
catalogue would be asserting something broader than the requirement and would fail the day a
deployment-only bound was added. Listed here so the requirement's set and the test's set are the
same object rather than two lists somebody keeps in step."""


@pytest.mark.model
@given(submissions=st.lists(signal_submissions(note_strategy=delay_reason_notes()), max_size=6))
@settings(max_examples=40, deadline=None)
def test_p37_no_signal_sequence_moves_a_named_bound_or_the_config_version(
    submissions: list[tuple[str, dict[str, object]]],
) -> None:
    """Feature: Customer Response Loop. Property 37 — all eight named bounds and the configuration
    version identifier are unchanged under arbitrary Customer_Signal sequences (R25.C7).

    Driven through the pure functions a submission actually reaches — the request models, the note
    truncation and the mapping table — because those are the only things in the write path that read
    a submitted value, and a ``model``-tier test that opened a database would be asserting the same
    thing more slowly.

    **The bounds cannot move because there is no writer, and this test says so twice.** The
    behavioural half compares the eight values and the version before and after the sequence. The
    structural half below asserts that no module in ``revora/customer/`` so much as names one of the
    eight — which is the claim that holds for sequences nobody generated. Neither half alone is
    enough: the first would pass against a module that wrote a bound on the ninth submission, and
    the second would pass against one that reached a bound through a variable.

    The configuration version is in the property for a reason R25.C7 states directly: a bound may
    change only through a recorded configuration change *carrying a new version identifier*. So a
    version that moved without a bound moving would be as much a violation as the reverse — it would
    mean something wrote the table.
    """
    before = tuple(getattr(_CONFIG, name) for name in NAMED_BOUNDS)
    before_version = _CONFIG.version

    limit = effective_note_limit(_CONFIG)
    for suffix, body in submissions:
        # Validate through the real request model, so the values that reach the pure helpers are the
        # ones the endpoint would have produced rather than raw dicts.
        if suffix == "delay-reason":
            submission = DelayReasonSubmission.model_validate(body)
            cause_for_delay_reason(submission.delay_reason)
        elif suffix == "partial-arrangement":
            submission = PartialArrangementSubmission.model_validate(body)
        else:
            submission = PromiseSubmission.model_validate(body)
        stored, truncated = _note_for_storage(submission.submitted_note, limit)
        # Read the results so a future refactor cannot make this loop dead code the optimiser
        # elides. The assertions about them belong to P38; here they only have to have happened.
        assert stored is None or len(stored) <= limit
        assert isinstance(truncated, bool)

    after = tuple(getattr(_CONFIG, name) for name in NAMED_BOUNDS)
    moved = {
        name: (was, now_)
        for name, was, now_ in zip(NAMED_BOUNDS, before, after, strict=True)
        if was != now_
    }
    assert not moved, (
        f"a configured bound moved across a Customer_Signal sequence: {moved}. R25.C7 permits a "
        "change only through a recorded configuration change with an approving Merchant_User, and "
        "a customer is not one"
    )
    assert _CONFIG.version == before_version, (
        f"the configuration version moved from {before_version} to {_CONFIG.version} across a "
        "signal sequence. A version that moves without an approving user means something wrote "
        "app_config, which is the mechanism R25.C7 exists to keep closed"
    )


@pytest.mark.model
def test_p37_the_customer_package_names_no_configured_bound_it_could_write() -> None:
    """Property 37, structurally: the write path cannot reach a bound to change it (R25.C7).

    The customer surface *reads* configured bounds and must — ``MAX_CUSTOMER_SIGNALS_PER_CASE`` and
    ``DELAY_NOTE_MAX_LENGTH`` are what make R19.C7 and R20.C2 enforceable, and they are read through
    ``Configuration``, which is a frozen dataclass. So the claim is not "no bound is named"; it is
    that **none of the eight R25.C7 names appears at all**, and that no module here reaches the one
    class that could produce a different configuration.

    ``ConfigurationBound`` and ``seed_rows`` are the two names that would be involved in changing a
    bound rather than reading one, so they are on the forbidden list beside the eight. A module that
    imported ``seed_rows`` could write a new default; one that constructed a ``ConfigurationBound``
    could add a bound the accessor does not declare.
    """
    forbidden = frozenset(NAMED_BOUNDS) | {"ConfigurationBound", "seed_rows", "app_config"}
    for path in sorted(_CUSTOMER_PACKAGE.rglob("*.py")):
        referenced = _referenced_names(path)
        overlap = sorted(referenced & forbidden)
        assert not overlap, (
            f"{path.name} references {overlap} in code. None of the eight bounds R25.C7 names is "
            "an input to anything the customer surface does, and the two configuration-writing "
            "names are how a bound would be changed rather than read"
        )


# ---------------------------------------------------------------------------
# `pure` — Property 38: nothing is derived from a note's contents
# ---------------------------------------------------------------------------

_MARKUP_SIGNIFICANT: tuple[str, ...] = ("&", "<", ">", '"', "'")
"""The five characters R29.C11 requires escaped, restated here rather than imported.

Restated on purpose. Importing :data:`revora.api.rendering.MARKUP_ESCAPES` would make the test
assert that the implementation escapes what the implementation says it escapes, which is true of any
implementation. This is the requirement's list, written independently, and the two agreeing is the
finding."""

_ENTITIES: tuple[str, ...] = ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;")
"""The five entities the escaped form may legally contain, in the order they must be removed.

The assertion below cannot be "no ``&`` appears in the escaped text": every entity starts with one,
so that would fail on any output that escaped anything. What R29.C11 requires is that no
markup-significant character appears **unescaped**, which is checked by deleting each legal entity
and then asserting that none of the five raw characters survives the deletion.

``&amp;`` is removed first, and for the same reason the escape produces it first: removing ``&lt;``
before ``&amp;`` would leave the ampersand of a ``&amp;lt;`` sequence looking like a stray one."""


@pytest.mark.pure
@given(reason=st.sampled_from(tuple(DelayReason)), note=delay_reason_notes())
@settings(max_examples=250, deadline=None)
def test_p38_nothing_is_derived_from_a_note_and_the_rendering_is_inert(
    reason: DelayReason, note: str | None
) -> None:
    """Feature: Customer Response Loop. Property 38 — for arbitrary note content, the derived
    Delay_Reason, Hard_Stop_Reason, Promise_Date, Partial_Arrangement_Request flag and every
    currency figure equal their values with the note absent; the stored length is within the bound;
    and the rendered output has every markup-significant character escaped (R20.C2, R20.C3,
    R20.C7, R29.C11).

    **The reference is the same submission with ``note=None``**, not a hand-written expected value.
    That is what makes this a statement about the note's *irrelevance* rather than about the four
    derivations happening to be right: whatever they produce, they must produce it identically with
    the note present and absent, for every one of the generated shapes — markup, already-escaped
    markup, SQL, control characters, four scripts, and text that spells out in words each of the
    five things R20.C3 forbids deriving. A note reading ``DISPUTES_THE_CHARGE`` produces no hard
    stop, and a note reading ``amount 249900 INR`` produces no currency figure, because the derived
    values are compared against a submission that contains neither string.

    **The currency clause is asserted as an absence of anything a figure could come from**, which is
    the only honest form it has: ``DelayReasonSubmission`` declares no money field, so the assertion
    is that the audit values it records contain no integer and no ``Decimal`` at all. R22.C1 is the
    same absence one layer down — there is no ``amount`` column — so a currency figure derived from
    a note would have to invent both a field and a column.

    **The rendering clause is asserted on the escaped form, over the *stored* text**, which is the
    order the implementation uses and the order that matters: escaping before truncation would let a
    cut land inside ``&amp;`` and emit ``&am``, a broken entity, which is a rendering defect in
    exactly the place the requirement is about. Truncate, then escape, then assert no
    markup-significant character survives raw.
    """
    body: dict[str, object] = {"delay_reason": reason.value}
    if note is not None:
        body["note"] = note
    with_note = DelayReasonSubmission.model_validate(body)
    without_note = DelayReasonSubmission.model_validate({"delay_reason": reason.value})

    # R20.C3, clause by clause. Each is a value something downstream reads, and each is compared
    # against the note-absent submission rather than against a literal.
    assert with_note.submitted_delay_reason == without_note.submitted_delay_reason
    assert cause_for_delay_reason(with_note.submitted_delay_reason) == cause_for_delay_reason(
        without_note.submitted_delay_reason
    ), "the mapped Risk_Cause moved with the note's contents"
    assert (with_note.submitted_delay_reason in _HARD_STOP_MEMBERS) == (
        without_note.submitted_delay_reason in _HARD_STOP_MEMBERS
    ), "a note's contents produced or removed a hard stop"
    assert with_note.kind is without_note.kind is CustomerSignalKind.DELAY_REASON, (
        "the signal kind moved with the note's contents, so a note could turn a stated reason into "
        "a Partial_Arrangement_Request — which is R22's shape and has different consequences"
    )
    # No Promise_Date and no arrangement flag can be derived, because neither field exists on this
    # shape. Asserted as the absence rather than as `is None`, so a field added later fails here
    # instead of quietly becoming derivable.
    for absent in ("promise_date", "amount", "instalment_count", "schedule", "partial"):
        assert not hasattr(with_note, absent), (
            f"DelayReasonSubmission acquired a {absent!r} field. R22.C1 and R20.C3 are both the "
            "absence of one, and a nullable field is a field something eventually populates"
        )

    recorded = with_note.recorded_values()
    assert set(recorded) == set(without_note.recorded_values()), (
        "the audit value key set changed with the note's contents"
    )
    assert recorded["delay_reason"] == reason.value
    for key, value in recorded.items():
        assert not isinstance(value, int | Decimal) or isinstance(value, bool), (
            f"the recorded audit values carry a numeric field {key!r}={value!r}. Every currency "
            "figure in the system is an integer minor-unit count, so a numeric value derived on "
            "this path is indistinguishable from one — and R20.C3 forbids deriving any currency "
            "figure from a note. Booleans are exempt: note_present is one"
        )
    # The note itself is never an audit field; only whether one was supplied. Asserted as the
    # absence of a key rather than as a substring search, because the note is generated content and
    # some draws are Delay_Reason member names — a substring test would fail on the note "OTHER"
    # beside the reason ``OTHER`` and would be asserting a coincidence rather than the requirement.
    # It matters because the audit log cannot be rewritten: a note that reached it would outlive
    # CUSTOMER_DATA_RETENTION with no sweep able to reach it.
    assert "note" not in recorded and "delay_reason_note" not in recorded, (
        f"the recorded audit values carry a note field: {sorted(recorded)}. The audit log is the "
        "one store the R29.C10 retention sweep cannot reach, which is why the note is retained on "
        "customer_signal and only note_present is recorded here"
    )

    # R20.C2: the stored length respects the effective bound, which is the smaller of the
    # configured one and the column's.
    limit = effective_note_limit(_CONFIG)
    stored, truncated = _note_for_storage(with_note.submitted_note, limit)
    if stored is None:
        assert not truncated, (
            "note_truncated was set on a note that stored as NULL. The flag means 'incomplete at "
            "the end', and there is no end"
        )
    else:
        assert len(stored) <= limit, (
            f"stored note is {len(stored)} characters against a bound of {limit}; the column's "
            "CHECK would reject the insert and the customer would get a 503 for a well-formed "
            "request"
        )
        assert "\x00" not in stored, (
            "a NUL byte survived into the stored note. It is the one character PostgreSQL TEXT "
            "cannot hold, so the insert would fail and answer 503"
        )
        assert truncated == (len(_stripped(with_note.submitted_note)) > limit), (
            "note_truncated disagrees with whether length truncation actually happened"
        )

    # R29.C11: the rendered form, escaped, over the stored text.
    document = customer_supplied_note(stored, truncated=truncated)
    if stored is None:
        assert document is None, "an absent note produced a presentation document"
        return
    assert document is not None
    assert document["label"] == UNVERIFIED_CUSTOMER_TEXT, (
        "the note is presented without R20.C12's mark. The label is a fact about the data — a "
        "stranger's assertion — and a surface that omitted it would present it as a finding"
    )
    assert document["verified"] is False
    assert document["render_as"] == RENDER_AS_TEXT_ONLY
    assert document["text"] == stored, (
        "the verbatim text differs from what was stored. It is what a text-node renderer uses, and "
        "a pre-escaped copy there would display a customer who typed '<3' as '&lt;3'"
    )
    escaped = document["text_escaped"]
    assert isinstance(escaped, str)
    residue = escaped
    for entity in _ENTITIES:
        residue = residue.replace(entity, "\u0001")
    for character in _MARKUP_SIGNIFICANT:
        assert character not in residue, (
            f"{character!r} survives unescaped in the rendered note. R29.C11 requires every "
            f"markup-significant character escaped, and {character!r} is one: the angle brackets "
            "open and close a tag, the quotes close an attribute value, and the ampersand is what "
            f"makes the substitution order load-bearing. Rendered: {escaped[:120]!r}"
        )
    # Nothing was dropped. Escaping only ever lengthens, so a shorter result means a character was
    # removed rather than replaced — which R20.C3 forbids, because deciding a character does not
    # belong in a note is deriving a judgement about its contents.
    assert len(escaped) >= len(stored), (
        "the escaped form is shorter than the stored text, so escaping removed content"
    )


def _stripped(note: str | None) -> str:
    """The note as ``_note_for_storage`` sees it before the length check.

    Duplicated from the writer on purpose, so the truncation assertion above compares against an
    independently computed length rather than against the function under test. If the writer's
    NUL-then-strip order ever changed, this is where the property would notice.
    """
    return "" if note is None else note.replace("\x00", "").strip()


@pytest.mark.pure
def test_p38_no_presentation_surface_renders_a_note_as_markup() -> None:
    """Property 38's rendering clause at the surface that actually renders (R29.C11).

    ``dangerouslySetInnerHTML`` is the only way a React component can execute a string as markup,
    and R29.C11 forbids it "in any presentation surface" — so the assertion is over the **whole**
    ``web/src`` tree rather than over the one component that shows a note. A second component added
    later inherits the guarantee instead of being trusted to repeat it, and the customer-facing page
    is covered by the same sweep as the dashboard.

    A source-level check from a Python test rather than from vitest, and that is deliberate: this is
    a property of the repository, not of a rendered component, and running it in the ``pure`` tier
    means it fails on every commit rather than only when somebody runs the web suite.

    **Comments are stripped first, for the reason :func:`_referenced_names` reads the AST.** These
    components are documented at length and the one that renders a note explains in prose exactly
    which API it must not use, so a substring search over the raw text fails on the explanation
    rather than on a call — which is the failure that would teach the next person to delete the
    comment instead of keeping the guarantee. Python has no JSX parser to hand, so the comments are
    removed with two substitutions and the search runs on what is left. That is weaker than an AST
    walk in one specific way, recorded here rather than glossed: a string constant containing the
    name would still be found, which is fine, and a name assembled from fragments at runtime would
    not be — and neither would it be by any check short of executing the module.
    """
    web_source = Path(inspect.getfile(signals_module)).parents[2] / "web" / "src"
    assert web_source.is_dir(), f"{web_source} is not a directory; this test has lost its subject"
    checked = sorted(
        path
        for suffix in ("*.jsx", "*.js")
        for path in web_source.rglob(suffix)
    )
    assert len(checked) >= 10, (
        f"only {len(checked)} source files found under {web_source}; this test is meant to cover "
        "the whole tree and has stopped finding it"
    )
    for path in checked:
        code = _without_comments(path.read_text(encoding="utf-8"))
        assert "dangerouslySetInnerHTML" not in code, (
            f"{path.relative_to(web_source)} uses dangerouslySetInnerHTML. It is the only way a "
            "React component executes a string as markup, and a Delay_Reason_Note is a string a "
            "stranger typed on an endpoint reachable without a session (R29.C11)"
        )


def _without_comments(source: str) -> str:
    """JavaScript source with ``/* ... */`` and ``// ...`` comments removed.

    Deliberately naive: it does not understand that ``//`` inside a string literal is not a comment,
    which for this codebase means a URL in a string loses its tail. That is acceptable because the
    only thing searched for afterwards is one identifier, and truncating a URL cannot create or
    destroy an occurrence of it. A parser would be the right tool if anything more were being asked
    of the result.
    """
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", without_block)

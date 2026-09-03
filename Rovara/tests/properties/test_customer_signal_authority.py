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
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.customer import projection as projection_module
from revora.customer import signals as signals_module
from revora.customer import tokens as tokens_module
from revora.customer.tokens import TokenService
from revora.domain.actions import CandidateAction
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from tests.fakes.customer import installed_signing_secrets
from tests.pg_support import insert_merchant
from tests.strategies.customer import signal_submissions

_CONFIG = default_configuration()

_CUSTOMER_PACKAGE = Path(inspect.getfile(signals_module)).parent

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

    assert all(status in {201, 429} for status in live), (
        f"a declared write shape was answered {statuses}; only 201 and the two 429 caps are legal "
        "outcomes for a schema-valid submission on a live token against a non-terminal case. The "
        f"first hard stop is at index {first_hard_stop}, so everything up to and including it was "
        "submitted on a token that had not been revoked"
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

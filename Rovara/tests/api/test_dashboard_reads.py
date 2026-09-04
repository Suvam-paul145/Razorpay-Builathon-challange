"""What the dashboard is handed. Formatted money, explicit absences, and legible refusals.

These tests drive a **real** webhook through the **real** pipeline and then read the endpoints,
rather than inserting a hand-built decision trail. The difference matters: a hand-built trail is a
test of the serializer against a shape the author imagined, and every field the pipeline actually
writes differently would pass. Driving the pipeline means the case-detail view is asserted against
the rows the system produces.

Four claims are under test, and each one prevents a specific misstatement.

**R14.C12 — money is formatted server-side.** Every currency field carries ``minor``, ``currency``
and ``formatted`` together, and ``formatted`` is asserted to agree with ``format_minor`` on the same
integer. A client that renders the string cannot disagree with the server; a client that divides
``minor`` by a hundred in one component and not another will, and shipping the string is what makes
that take deliberate effort.

**R14.C15 and C16 — an absent value is never zero.** A case that has no recommendation yet returns a
marker naming its state, not ``0`` and not ``null``. The two are different claims from each other as
well: "not yet" on a ``DETECTED`` case versus "policy stopped it" on a ``BLOCKED`` one.

**R14.C14 and R11.C5 — a refusal is as legible as an action.** Where the optimizer chose
``DO_NOTHING``, the response carries the reason, the baseline, the incremental probability, the net
value and all three compared thresholds.

**R12.C13 — observed recovery is never presented as incremental.** The metrics response returns the
``NOT_ESTABLISHED`` sentinel with refusal codes, and no path returns a number in its place.
"""

from __future__ import annotations

import hmac
import json
import uuid
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from revora.api.rendering import DATA_UNAVAILABLE, NOT_YET_RECORDED, PRESENT
from revora.audit.events import (
    CONSENT_RECORDED,
    CUSTOMER_DATA_REDACTED,
    HUMAN_OWNER_ASSIGNED,
    HUMAN_OWNER_RELEASED,
)
from revora.domain.enums import POLICY_CHECK_ORDER
from revora.domain.money import Minor, format_minor
from revora.jobs.worker import run_once
from tests.api.conftest import WEBHOOK_SECRET, Tenant, insert_case

pytestmark = pytest.mark.pg

_MAX_WORKER_PASSES = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failed_payment_body(payment_id: str, event_id: str, *, amount: int = 2_000_000) -> bytes:
    """A verified-shape ``payment.failed`` envelope whose reason maps deterministically.

    ``insufficient_funds`` on purpose: it maps to ``INSUFFICIENT_FUNDS``, whose eligibility row
    permits ``PAYMENT_LINK``, so the optimizer has a real action to compare against the null ones
    rather than falling through for lack of a candidate.
    """
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
                    "contact": "+919876543210",
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


def _drive_pipeline(
    engine: Engine, client: TestClient, tenant: Tenant, *, amount: int = 2_000_000
) -> uuid.UUID:
    """Ingest one signed failure and drain the worker until the case has a decision.

    Consent is granted after detection because the customer key is only known once the case exists.
    Without it the policy engine correctly blocks on ``CONSENT_MISSING``, which is a valid outcome
    and not the one these tests need — the point here is a *populated* decision trail.

    ``amount`` is a parameter because it decides which branch the optimizer takes, and both branches
    need covering. Twenty thousand rupees clears ``MIN_NET_VALUE_THRESHOLD`` comfortably; a small
    payment does not, and that is how the refusal path is reached without rigging configuration.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    body = _failed_payment_body(payment_id, event_id, amount=amount)
    signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, sha256).hexdigest()

    response = client.post(
        f"/webhooks/razorpay/{tenant.slug}",
        content=body,
        headers={
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
            "content-type": "application/json",
        },
    )
    assert response.status_code == 200, response.text

    case_id = None
    for _ in range(_MAX_WORKER_PASSES):
        with engine.begin() as connection:
            found = connection.execute(
                text(
                    "SELECT id, customer_key FROM recovery_case "
                    "WHERE merchant_id = :m AND provider_payment_id = :p"
                ),
                {"m": str(tenant.merchant_id), "p": payment_id},
            ).one_or_none()
        if found is not None:
            case_id = found[0]
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO customer_consent (
                            merchant_id, customer_key, opted_out, source, effective_at, created_at
                        ) VALUES (:m, :ck, false, 'test', now() - interval '1 minute', now())
                        """
                    ),
                    {"m": str(tenant.merchant_id), "ck": found[1]},
                )
            break
        if run_once("api-tier-worker") == 0:
            break
    assert case_id is not None, "detection never opened a case for the ingested failure"

    for _ in range(_MAX_WORKER_PASSES):
        if run_once("api-tier-worker") == 0:
            break
    return case_id


def _money_fields(document: object, path: str = "") -> list[tuple[str, dict]]:
    """Every money object anywhere in a response, with the path that found it.

    Walks the whole document rather than checking named fields, so a money figure added later is
    covered without anybody remembering to extend a list. Identified by carrying ``minor`` and
    ``formatted`` together, which is exactly the contract being asserted.
    """
    found: list[tuple[str, dict]] = []
    if isinstance(document, dict):
        if "minor" in document and "formatted" in document:
            found.append((path, document))
        for key, value in document.items():
            found.extend(_money_fields(value, f"{path}.{key}"))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_money_fields(value, f"{path}[{index}]"))
    return found


def _audit_events(engine: Engine, merchant_id: uuid.UUID, event_type: str) -> list[dict]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT decision FROM audit_record WHERE merchant_id = :m AND event_type = :e "
                "ORDER BY created_at"
            ),
            {"m": str(merchant_id), "e": event_type},
        ).all()
    return [row[0] or {} for row in rows]


# ---------------------------------------------------------------------------
# R14.C12 — server-formatted money
# ---------------------------------------------------------------------------


def test_every_money_figure_arrives_formatted_beside_its_integer(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C12, walked over the whole response rather than a named field list.

    The formatted string is asserted to agree with ``format_minor`` on the same integer. If they
    ever disagree, one of two surfaces is wrong and a merchant reading the string has no way to
    know which.
    """
    case_id = _drive_pipeline(installed_engine, client, tenant)
    detail = client.get(f"/cases/{case_id}", headers=tenant.auth).json()

    figures = _money_fields(detail)
    assert figures, "the case detail returned no money figures at all"
    for path, figure in figures:
        assert figure["status"] == PRESENT, path
        assert figure["currency"] == "INR", path
        assert figure["formatted"] == format_minor(
            Minor(int(figure["minor"])), symbol="₹", minor_digits=2
        ), path
        assert "₹" in figure["formatted"], path

    # And on the metrics surface too, which is where a merchant reads the totals.
    summary = client.get("/metrics/summary", headers=tenant.auth).json()
    for path, figure in _money_fields(summary):
        assert figure["formatted"] == format_minor(
            Minor(int(figure["minor"])), symbol="₹", minor_digits=2
        ), path


def test_a_large_amount_is_grouped_so_a_lakh_is_readable(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Twenty thousand rupees is ``₹20,000.00``, not ``₹2000000``.

    The one place a formatting bug is guaranteed to be noticed and the one place it is guaranteed to
    be misread: an ungrouped minor-unit integer looks like a number a hundred times larger.
    """
    case_id = insert_case(installed_engine, tenant.merchant_id, amount=2_000_000)
    detail = client.get(f"/cases/{case_id}", headers=tenant.auth).json()
    assert detail["case"]["payment_amount"]["formatted"] == "₹20,000.00"
    assert detail["case"]["payment_amount"]["minor"] == 2_000_000


# ---------------------------------------------------------------------------
# R14.C15 — absent values
# ---------------------------------------------------------------------------


def test_an_absent_value_names_the_case_state_and_is_never_zero(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C15. A ``DETECTED`` case has no diagnosis, no recommendation and no outcome yet.

    Each absence carries the state, because the state is the explanation. Zero is asserted against
    explicitly: substituting it for a recovered amount is a false financial statement, not a
    display shortcut.
    """
    case_id = insert_case(installed_engine, tenant.merchant_id, state="DETECTED")
    detail = client.get(f"/cases/{case_id}", headers=tenant.auth).json()

    for field in ("diagnosis", "baseline", "recommendation", "outcome"):
        marker = detail[field]
        assert marker["status"] == NOT_YET_RECORDED, field
        assert marker["case_state"] == "DETECTED", field
        assert "DETECTED" in marker["detail"], field

    summary = detail["case"]
    assert summary["recovered_amount"]["status"] == NOT_YET_RECORDED
    assert "minor" not in summary["recovered_amount"], (
        "an unrecovered case must not carry a recovered amount of any value, including zero"
    )
    assert summary["outcome_classification"]["status"] == NOT_YET_RECORDED
    assert summary["executed_action"]["status"] == NOT_YET_RECORDED


def test_a_blocked_case_reads_differently_from_a_pending_one(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Same empty cell, opposite meanings, and the state is what distinguishes them.

    A reader who cannot tell "not yet" from "policy stopped it" will read the first as a bug and the
    second as working — exactly backwards.
    """
    pending = insert_case(installed_engine, tenant.merchant_id, state="DETECTED")
    blocked = insert_case(installed_engine, tenant.merchant_id, state="BLOCKED")

    pending_marker = client.get(f"/cases/{pending}", headers=tenant.auth).json()["recommendation"]
    blocked_marker = client.get(f"/cases/{blocked}", headers=tenant.auth).json()["recommendation"]

    assert pending_marker["case_state"] == "DETECTED"
    assert blocked_marker["case_state"] == "BLOCKED"
    assert pending_marker["detail"] != blocked_marker["detail"]


# ---------------------------------------------------------------------------
# R14.C4, C5, C14 — the comparison and the refusal
# ---------------------------------------------------------------------------


def test_the_case_detail_returns_every_candidate_with_all_of_its_figures(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C4 and R7.C8. Excluded candidates included, with their reason.

    The detail *is* the comparison, not the winner. An action excluded for being unavailable is
    different evidence from one excluded for not being worth it, and both are different from an
    action that never appears.
    """
    case_id = _drive_pipeline(installed_engine, client, tenant)
    recommendation = client.get(f"/cases/{case_id}", headers=tenant.auth).json()["recommendation"]

    assert recommendation.get("status") != NOT_YET_RECORDED, recommendation
    candidates = recommendation["candidates"]
    assert len(candidates) >= 2, "a candidate set always holds at least the two null actions"

    for candidate in candidates:
        for field in (
            "incremental_probability",
            "expected_incremental_revenue",
            "financial_cost",
            "communication_cost",
            "risk_cost",
            "customer_cost",
            "total_action_cost",
            "net_recovery_value",
        ):
            assert field in candidate, candidate["action"]
        # R31.C7. Four separate figures *and* the total, never the total in place of them —
        # so the presence check above is the assertion, and this is the arithmetic that
        # proves the server summed them rather than the client having to.
        assert candidate["total_action_cost"]["minor"] == (
            candidate["financial_cost"]["minor"]
            + candidate["communication_cost"]["minor"]
            + candidate["risk_cost"]["minor"]
            + candidate["customer_cost"]["minor"]
        ), candidate["action"]
        assert "action_cost" not in candidate, (
            "the blended action_cost is gone from the wire (R31.C1); a client still reading "
            "it would silently drop the communication term"
        )
        # R31.C10's marking travels with the two figures it qualifies. A live estimate is
        # never marked — only a row migration 0008 rewrote is.
        assert candidate["cost_split_not_measured"] is False, candidate["action"]
        assert candidate["financial_cost_method"] is not None, candidate["action"]
        assert candidate["communication_cost_method"] is not None, candidate["action"]
        if candidate["excluded"]:
            assert candidate["exclusion_reason"], (
                f"{candidate['action']} is excluded with no stated reason"
            )
            assert candidate["rank"] is None, "an excluded action has no rank"

    assert any(candidate["excluded"] for candidate in candidates), (
        "the MVP-unavailable actions should appear excluded rather than being omitted (R6.C9)"
    )


def test_a_refusal_is_rendered_with_every_number_that_decided_it(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C14 and R11.C5. "We chose not to act" has to be as legible as an action.

    Reached with a small payment rather than by rigging configuration. ₹150 at any plausible uplift
    produces an expected incremental revenue well under ``MIN_NET_VALUE_THRESHOLD`` of 5000 minor
    units, so every real action is excluded and the null actions are all that remain — which is a
    case the system genuinely produces, unlike one manufactured by lowering a bound.

    The three thresholds are asserted present even though only one of them decided, because a
    merchant asking "why not?" is asking about the whole comparison. Showing only the failing bound
    invites "so lower it", and the answer to that is the other two.
    """
    case_id = _drive_pipeline(installed_engine, client, tenant, amount=15_000)
    recommendation = client.get(f"/cases/{case_id}", headers=tenant.auth).json()["recommendation"]

    assert recommendation.get("status") != NOT_YET_RECORDED, recommendation
    assert recommendation["selected_action"] in ("DO_NOTHING", "WAIT"), (
        f"a ₹150 payment should not be worth acting on: {recommendation['selected_action']}"
    )
    assert recommendation["selection_reason"] == "NO_POSITIVE_VALUE", recommendation

    refusal = recommendation["refusal"]
    assert refusal["reason"] == "NO_POSITIVE_VALUE"
    assert "cost more than it was expected to recover" in refusal["explanation"]
    assert refusal["baseline_probability"]
    assert refusal["incremental_probability"] is not None
    assert refusal["net_recovery_value"] is not None
    thresholds = refusal["compared_thresholds"]
    for field in (
        "min_net_value_threshold",
        "min_incremental_probability",
        "max_cost_to_value_ratio",
        "high_baseline_threshold",
    ):
        assert field in thresholds, field


def test_all_twelve_policy_checks_are_returned_in_order(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C5. Twelve, not "the ones that ran".

    A record showing eleven rows is indistinguishable from an evaluation that stopped early and
    approved — and a reader used to twelve will not notice which one went missing.
    """
    case_id = _drive_pipeline(installed_engine, client, tenant)
    decisions = client.get(f"/cases/{case_id}", headers=tenant.auth).json()["policy_decisions"]

    assert isinstance(decisions, list) and decisions, decisions
    for decision in decisions:
        checks = decision["checks"]
        assert len(checks) == len(POLICY_CHECK_ORDER) == 12
        assert [check["check_id"] for check in checks] == [
            check.value for check in POLICY_CHECK_ORDER
        ]
        assert decision["expected_check_count"] == 12
        assert all(check["outcome"] for check in checks)


def test_the_audit_trail_is_returned_in_sequence_order(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R11.C5. Sequence order, because two records can share a millisecond."""
    case_id = _drive_pipeline(installed_engine, client, tenant)
    body = client.get(f"/cases/{case_id}/audit", headers=tenant.auth).json()

    sequences = [record["seq"] for record in body["records"]]
    assert sequences, "a case that walked the pipeline has an audit trail"
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1)), (
        f"the per-case sequence must be gap-free and start at 1, got {sequences}"
    )


# ---------------------------------------------------------------------------
# R12.C13 / R14.C7 through C10 — metrics
# ---------------------------------------------------------------------------


def test_incremental_revenue_is_the_sentinel_and_never_a_number_without_an_experiment(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R12.C13. With no experiment there is no causal claim, and the field says so by name.

    Asserted against a numeric zero explicitly. ``0`` renders as a measurement of nothing and
    ``null`` renders as an empty cell; the claim being made is neither.
    """
    _drive_pipeline(installed_engine, client, tenant)
    report = client.get("/metrics/summary", headers=tenant.auth).json()["report"]

    incremental = report["incremental_recovered_revenue"]
    assert incremental["status"] == "NOT_ESTABLISHED"
    assert incremental["value"] == "NOT_ESTABLISHED"
    assert "NO_COMPLETED_EXPERIMENT" in incremental["refusal_codes"]
    assert "amount" not in incremental
    assert report["causality_established"] is False


def test_every_rate_is_a_string_so_undefined_cannot_be_coerced_to_zero(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """A period with no cases has no recovery rate. ``UNDEFINED``, not ``0``.

    Emitted as a string so a consumer doing arithmetic crashes rather than silently coercing the
    sentinel to zero — which is the exact confusion the sentinel exists to prevent.
    """
    report = client.get("/metrics/summary", headers=tenant.auth).json()["report"]
    for field in (
        "recovery_rate",
        "intervention_rate",
        "action_success_rate",
        "escalation_rate",
        "average_hours_to_recovery",
    ):
        rate = report[field]
        assert rate["status"] == PRESENT, field
        assert isinstance(rate["value"], str), field
    assert report["recovery_rate"]["value"] == "UNDEFINED", (
        "a merchant with no cases in the period has no recovery rate"
    )


def test_the_gross_of_refunds_label_travels_with_every_recovery_figure(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Refunds are captured on every read and not netted out, so saying so is not optional."""
    report = client.get("/metrics/summary", headers=tenant.auth).json()["report"]
    assert "RECOVERY_GROSS_OF_REFUNDS" in report["labels"]


def test_the_unresolved_grouping_returns_all_five_groups_including_the_empty_ones(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C10. A ``GROUP BY`` would omit the empty ones, and an omitted row renders as nothing.

    "Nothing" reads as "we did not look", and a reader used to five rows seeing four will not notice
    which one is missing.
    """
    insert_case(installed_engine, tenant.merchant_id, state="EXPIRED", amount=100_000)
    insert_case(installed_engine, tenant.merchant_id, state="EXPIRED", amount=50_000)

    document = client.get("/metrics/unresolved", headers=tenant.auth).json()
    groups = {group["state"]: group for group in document["groups"]}
    assert set(groups) == {"STOPPED", "BLOCKED", "EXPIRED", "ESCALATED", "FAILED"}

    assert groups["EXPIRED"]["case_count"] == 2
    # A formatted money field, like every other amount on the wire. This endpoint used to emit a
    # bare ``amount_minor`` with one ``currency`` field beside it, which left the browser dividing
    # by a hundred and picking a symbol — the only client-side currency arithmetic in the dashboard,
    # and the first place a rounding disagreement with the summary page would have appeared.
    assert groups["EXPIRED"]["amount"]["minor"] == 150_000
    assert groups["EXPIRED"]["amount"]["formatted"] == "₹1,500.00"
    for state in ("STOPPED", "BLOCKED", "ESCALATED", "FAILED"):
        assert groups[state]["case_count"] == 0, state
        # An empty group is a real zero — "we looked and there were none" — so it renders as a
        # formatted zero rather than as an absent-value marker. This is the one place a zero amount
        # is the honest answer, and it is honest because the row is present at all.
        assert groups[state]["amount"]["minor"] == 0, state
        assert groups[state]["amount"]["status"] == "PRESENT", state

    assert document["total_amount"]["minor"] == sum(
        group["amount"]["minor"] for group in document["groups"]
    )


def test_a_metrics_timeout_degrades_one_figure_and_keeps_its_caveat(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C16. Forced by setting the bound to one millisecond.

    Two things have to hold at once, and the second is the one that is easy to lose: the incremental
    field becomes a data-unavailable marker naming itself, **and** the report still carries the
    causality caveat. A figure that could not be computed is certainly not a causal claim that was
    established, so degrading fails toward the safe statement.
    """
    _drive_pipeline(installed_engine, client, tenant)
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_config (
                    merchant_id, key, value, value_kind, config_version, is_active,
                    effective_at, is_assumption, created_at
                ) VALUES (
                    :m, 'DASHBOARD_METRICS_TIMEOUT', '0.001', 'DURATION_SECONDS',
                    'forced-timeout', true, now(), true, now()
                )
                """
            ),
            {"m": str(tenant.merchant_id)},
        )

    body = client.get("/metrics/summary", headers=tenant.auth).json()
    if body["incremental_available"]:
        # A one-millisecond bound is not always enough to cancel a query on a fast machine, so the
        # *plumbing* check is opportunistic. The *contract* is checked deterministically by
        # `test_the_degraded_metrics_shape_names_the_figure_and_keeps_the_caveat`, which builds the
        # degraded document directly — the two together cover both without either being flaky.
        pytest.skip("the experiment read completed inside one millisecond on this machine")

    incremental = body["report"]["incremental_recovered_revenue"]
    assert incremental["status"] == DATA_UNAVAILABLE
    assert incremental["figure"] == "incremental_recovered_revenue"
    assert body["report"]["case_count"] >= 1, "the other figures must still return"
    assert body["report"]["causality_established"] is False


# ---------------------------------------------------------------------------
# R14.C11 — ownership
# ---------------------------------------------------------------------------


def test_taking_ownership_suspends_automation_and_is_audited(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R14.C11, R8.C11. Policy check 7 fails while an owner is set, so this is a control surface."""
    case_id = insert_case(installed_engine, tenant.merchant_id, state="POLICY_CHECK")

    taken = client.post(f"/cases/{case_id}/owner", headers=tenant.auth)
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert body["outcome"] == "ASSIGNED"
    assert body["owner_user_id"] == str(tenant.user_id)
    assert body["automated_action_suspended"] is True
    assert "HUMAN_OWNERSHIP" in body["detail"]

    with installed_engine.begin() as connection:
        owner, assigned_at = connection.execute(
            text(
                "SELECT human_owner_user_id, human_assigned_at FROM recovery_case WHERE id = :c"
            ),
            {"c": str(case_id)},
        ).one()
    assert owner == tenant.user_id
    assert assigned_at is not None

    records = _audit_events(installed_engine, tenant.merchant_id, HUMAN_OWNER_ASSIGNED)
    assert records and records[-1]["merchant_user_id"] == str(tenant.user_id)


def test_taking_ownership_twice_is_idempotent_for_the_same_user(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """A double click is not an error and must not write a second record."""
    case_id = insert_case(installed_engine, tenant.merchant_id, state="POLICY_CHECK")
    assert client.post(f"/cases/{case_id}/owner", headers=tenant.auth).status_code == 200
    assert client.post(f"/cases/{case_id}/owner", headers=tenant.auth).status_code == 200
    assert len(_audit_events(installed_engine, tenant.merchant_id, HUMAN_OWNER_ASSIGNED)) == 1


def test_releasing_ownership_lets_automation_resume(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    case_id = insert_case(installed_engine, tenant.merchant_id, state="POLICY_CHECK")
    client.post(f"/cases/{case_id}/owner", headers=tenant.auth)

    released = client.delete(f"/cases/{case_id}/owner", headers=tenant.auth)
    assert released.status_code == 200
    assert released.json()["outcome"] == "RELEASED"
    assert released.json()["automated_action_suspended"] is False
    assert _audit_events(installed_engine, tenant.merchant_id, HUMAN_OWNER_RELEASED)

    # And releasing an unowned case is a 409 rather than a silent success, because a caller that
    # believes it released something it did not is worse off than one that got told.
    assert client.delete(f"/cases/{case_id}/owner", headers=tenant.auth).status_code == 409


def test_ownership_is_refused_on_a_terminal_case(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """There is no automated action left to suspend, and an ownership list that fills with finished
    work is an ownership list nobody trusts."""
    case_id = insert_case(installed_engine, tenant.merchant_id, state="EXPIRED")
    response = client.post(f"/cases/{case_id}/owner", headers=tenant.auth)
    assert response.status_code == 409
    assert response.json()["detail"]["outcome"] == "CASE_TERMINAL"


# ---------------------------------------------------------------------------
# R17.C10, C11 — consent and retention
# ---------------------------------------------------------------------------


def test_recording_an_opt_out_appends_and_supersedes(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R17.C10. Rows are appended, never updated, so "when did they ask us to stop?" is answerable.

    The second call names the first as superseded rather than overwriting it, which is what keeps
    the history readable.
    """
    contact = "+919000012345"
    first = client.post(
        "/consent",
        json={"contact": contact, "opted_out": True, "source": "ticket-4821"},
        headers=tenant.auth,
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["opted_out"] is True
    assert body["supersedes_consent_id"] is None
    assert contact not in first.text, "the contact must never be echoed back"

    second = client.post(
        "/consent",
        json={"contact": contact, "opted_out": False, "source": "ticket-4899"},
        headers=tenant.auth,
    )
    assert second.status_code == 201
    assert second.json()["supersedes_consent_id"] == body["consent_id"]
    assert second.json()["customer_key"] == body["customer_key"]

    with installed_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT opted_out FROM customer_consent WHERE merchant_id = :m "
                "AND customer_key = :ck ORDER BY effective_at"
            ),
            {"m": str(tenant.merchant_id), "ck": body["customer_key"]},
        ).all()
    assert [row[0] for row in rows] == [True, False], "the opt-out must survive as history"

    records = _audit_events(installed_engine, tenant.merchant_id, CONSENT_RECORDED)
    assert len(records) == 2
    assert contact not in json.dumps(records), "no audit record may hold the cleartext contact"


def test_consent_refuses_both_identifiers_and_neither(
    client: TestClient, tenant: Tenant
) -> None:
    """Ambiguity about *whose* consent was recorded is refused rather than resolved."""
    both = client.post(
        "/consent",
        json={
            "contact": "+919000012345",
            "customer_key": "a" * 32,
            "opted_out": True,
            "source": "t",
        },
        headers=tenant.auth,
    )
    neither = client.post(
        "/consent", json={"opted_out": True, "source": "t"}, headers=tenant.auth
    )
    assert both.status_code == 422
    assert neither.status_code == 422


def test_the_retention_sweep_redacts_contact_data_and_records_the_bound_it_applied(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R17.C11. The applied configuration version is on the record, because the bound is
    configurable and "we deleted it on time" is a claim about which bound was in force.

    Also asserts what *survives*: the amount and the customer key. A retention sweep that destroyed
    the amount would make every historical figure irreproducible, and one that destroyed the key
    would silently revoke every recorded opt-out.
    """
    from revora.cases.retention import sweep_customer_data_retention
    from revora.persistence.repositories.config import ConfigurationRepository
    from revora.persistence.repositories.session import tenant_transaction

    case_id = insert_case(installed_engine, tenant.merchant_id, state="EXPIRED")
    with installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET detected_at = now() - interval '400 days' "
                "WHERE id = :c"
            ),
            {"c": str(case_id)},
        )
        customer_key = connection.execute(
            text("SELECT customer_key FROM recovery_case WHERE id = :c"), {"c": str(case_id)}
        ).scalar_one()

    with tenant_transaction(tenant.merchant_id) as session:
        config = ConfigurationRepository(session).load(tenant.merchant_id)
    report = sweep_customer_data_retention(tenant.merchant_id, config=config)
    assert report.cases_redacted == 1
    assert report.retention_seconds == 15_552_000

    with installed_engine.begin() as connection:
        contact, amount, key = connection.execute(
            text(
                "SELECT customer_contact_masked, payment_amount, customer_key "
                "FROM recovery_case WHERE id = :c"
            ),
            {"c": str(case_id)},
        ).one()
    assert contact is None, "the masked contact must be gone"
    assert int(amount) == 250_000, "the amount must survive, or history is irreproducible"
    assert key == customer_key, "the customer key must survive, or every opt-out is revoked"

    records = _audit_events(installed_engine, tenant.merchant_id, CUSTOMER_DATA_REDACTED)
    assert records
    assert records[-1]["retention_config_version"] == config.version
    assert "customer_key" in records[-1]["retained_fields"]

    # Idempotent: the row no longer matches, so a second sweep does nothing and writes no record.
    again = sweep_customer_data_retention(tenant.merchant_id, config=config)
    assert again.cases_redacted == 0
    assert len(_audit_events(installed_engine, tenant.merchant_id, CUSTOMER_DATA_REDACTED)) == 1


def test_the_retention_sweep_deletes_an_aged_note_and_the_check_holds_afterwards(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R29.C10. A redacted note is **gone**, not merely marked — and the schema is what says so.

    The requirement permits "delete or irreversibly mask", and this is the test that pins which one
    happened. ``customer_signal`` carries ``CHECK (note_redacted_at IS NULL OR delay_reason_note IS
    NULL)`` from migration ``0008``, so the two halves are asserted separately and the second is the
    load-bearing one:

    * the note column is ``NULL`` and ``note_redacted_at`` and ``retention_config_version`` are set;
    * an ``UPDATE`` putting text back beside the redaction instant is **refused by the database**.

    Without the second, "irreversible" would be a property of the sweep rather than of the store,
    and a later code path that wrote a placeholder alongside the timestamp would satisfy every
    assertion about the sweep while leaving the text in the table.

    **The note's own clock, not the case's.** Three signals are seeded on a case that is *not*
    terminal, and their ``submitted_at`` values straddle the cutoff: one well past
    ``CUSTOMER_DATA_RETENTION``, one just inside it, one with no note at all. Only the first is
    redacted. That combination is what distinguishes R29.C10 from R17.C11: the contact sweep filters
    on terminal state and measures the case's ``detected_at``, this one filters on neither and
    measures the signal's ``submitted_at``. A sweep that reused the case pass's predicate would
    redact nothing here, and one that reused its clock would redact the recent note too — which is
    deleting data inside its retention period, a worse failure than keeping it too long because it
    is unrecoverable.

    **The non-identifying fields survive**, which is the requirement's second clause: the kind, the
    stated Delay_Reason and the submission instant are what Recovery_Memory segments on (R25.C3) and
    what the Metrics_Engine counts cohorts by (R25.C11), and a sweep that took them would make every
    historical Delay_Reason cohort irreproducible.
    """
    from revora.cases.retention import sweep_customer_data_retention
    from revora.persistence.repositories.config import ConfigurationRepository
    from revora.persistence.repositories.session import tenant_transaction

    case_id = insert_case(installed_engine, tenant.merchant_id, state="POLICY_CHECK")
    aged_id, recent_id, no_note_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    aged_note = "salary is late <script>alert(1)</script>"
    recent_note = "bank said try tomorrow"
    with installed_engine.begin() as connection:
        for signal_id, offset, reason, note in (
            (aged_id, "400 days", "SALARY_OR_CASHFLOW_TIMING", aged_note),
            (recent_id, "2 days", "BANK_OR_CARD_PROBLEM", recent_note),
            (no_note_id, "400 days", "OTHER", None),
        ):
            connection.execute(
                text(
                    f"""
                    INSERT INTO customer_signal (
                        id, merchant_id, case_id, token_id, kind, delay_reason,
                        delay_reason_note, note_truncated, provenance, submitted_at, created_at
                    ) VALUES (
                        :id, :m, :c, :tok, 'DELAY_REASON', :reason, :note, false, 'REAL',
                        now() - interval '{offset}', now() - interval '{offset}'
                    )
                    """
                ),
                {
                    "id": str(signal_id),
                    "m": str(tenant.merchant_id),
                    "c": str(case_id),
                    "tok": f"rvt_{signal_id.hex[:26]}",
                    "reason": reason,
                    "note": note,
                },
            )

    # R20.C12 and R29.C11, before the sweep and over the real endpoint: all three signals present,
    # the note labelled, and the escaped copy carrying no unescaped markup. Asserted here rather
    # than in a test of its own because this is the one fixture that holds all three signal shapes,
    # and the *same* document is read again after the sweep — which is what makes the redaction
    # visible as a change to what a merchant sees rather than only as a change to a column.
    before_sweep = client.get(f"/cases/{case_id}", headers=tenant.auth).json()
    presented = {row["signal_id"]: row for row in before_sweep["customer_signals"]}
    assert len(presented) == 3, (
        f"the case detail view presents {len(presented)} of 3 signals. R20.C12 says every "
        "persisted Customer_Signal, and the one most likely to be dropped is the silent one"
    )
    aged_view = presented[str(aged_id)]["note"]
    assert aged_view["label"] == "customer-supplied unverified text", (
        "the note is presented without R20.C12's mark, so on this screen a stranger's assertion "
        "reads exactly like a finding Revora reached"
    )
    assert aged_view["verified"] is False
    assert aged_view["text"] == aged_note, "the verbatim text a text-node renderer uses is missing"
    assert "<script>" not in aged_view["text_escaped"], (
        "the escaped copy still carries a raw tag, so a surface interpolating it into markup would "
        "execute part of a note a stranger typed on an unauthenticated endpoint (R29.C11)"
    )
    assert presented[str(recent_id)]["hard_stop_label"] is None
    assert presented[str(no_note_id)]["note"] is None

    with tenant_transaction(tenant.merchant_id) as session:
        config = ConfigurationRepository(session).load(tenant.merchant_id)
    report = sweep_customer_data_retention(tenant.merchant_id, config=config)

    assert report.notes_redacted == 1, (
        f"{report.notes_redacted} notes redacted; exactly one is past CUSTOMER_DATA_RETENTION. "
        "Two would mean the sweep measured the case's age instead of the note's and deleted data "
        "inside its retention period; zero would mean it inherited the contact pass's "
        "terminal-state filter, which no note needs"
    )

    with installed_engine.begin() as connection:
        rows = {
            str(row[0]): row
            for row in connection.execute(
                text(
                    "SELECT id, delay_reason_note, note_redacted_at, retention_config_version, "
                    "kind, delay_reason, submitted_at FROM customer_signal WHERE case_id = :c"
                ),
                {"c": str(case_id)},
            ).all()
        }

    aged = rows[str(aged_id)]
    assert aged[1] is None, "the aged note survived the sweep"
    assert aged[2] is not None, "note_redacted_at was not set, so the redaction is unrecorded"
    assert aged[3] == config.version, (
        "the applied retention configuration version was not recorded. R29.C10's last clause is "
        "that it is, because the bound is per-merchant configurable and 'we deleted it on time' is "
        "a claim about which bound was in force"
    )
    assert aged[4] == "DELAY_REASON", "the signal kind was destroyed with the note"
    assert aged[5] == "SALARY_OR_CASHFLOW_TIMING", (
        "the stated Delay_Reason was destroyed with the note. It is one of six enumerated members, "
        "it identifies nobody, and Recovery_Memory segments and the Metrics_Engine cohort counts "
        "are computed from it (R25.C3, R25.C11)"
    )
    assert aged[6] is not None, "the submission instant was destroyed with the note"

    recent = rows[str(recent_id)]
    assert recent[1] == recent_note, (
        "a note inside its retention period was deleted. That is a worse failure than keeping one "
        "too long, because it cannot be undone"
    )
    assert recent[2] is None and recent[3] is None

    untouched = rows[str(no_note_id)]
    assert untouched[2] is None, (
        "a signal that never carried a note was marked redacted. It is not in the partial index "
        "the scan reads, so reaching it means the scan is not using that index"
    )

    # The load-bearing half: the CHECK, not the sweep, is what makes the erasure irreversible.
    with pytest.raises(IntegrityError), installed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE customer_signal SET delay_reason_note = '[redacted]' WHERE id = :s"
            ),
            {"s": str(aged_id)},
        )

    # The same read again. A redaction is a third state on the wire — not an absence — because
    # "wrote nothing" and "wrote something we may no longer hold" are different histories, and only
    # one of them lets a reader conclude the customer stayed silent.
    after_sweep = client.get(f"/cases/{case_id}", headers=tenant.auth).json()
    redacted_view = {row["signal_id"]: row for row in after_sweep["customer_signals"]}[
        str(aged_id)
    ]
    assert redacted_view["note"]["status"] == "REDACTED"
    assert "text" not in redacted_view["note"], (
        "the redacted note document still carries a text key. The column is NULL, so any text here "
        "would have to have been invented"
    )
    assert redacted_view["retention_config_version"] == config.version
    assert redacted_view["delay_reason"] == "SALARY_OR_CASHFLOW_TIMING", (
        "the presented Delay_Reason went with the note. It is what a merchant reads to know why "
        "the payment was late, and it is retained so the redaction does not erase that too"
    )

    records = _audit_events(installed_engine, tenant.merchant_id, CUSTOMER_DATA_REDACTED)
    assert records[-1]["notes_redacted"] == 1
    assert records[-1]["retention_config_version"] == config.version
    assert "customer_signal.delay_reason" in records[-1]["retained_fields"]

    # Idempotent for the same reason the contact pass is: the redacted row no longer satisfies
    # ``delay_reason_note IS NOT NULL``, so it leaves the partial index and the scan's set.
    again = sweep_customer_data_retention(tenant.merchant_id, config=config)
    assert again.notes_redacted == 0
    assert len(_audit_events(installed_engine, tenant.merchant_id, CUSTOMER_DATA_REDACTED)) == 1, (
        "a second sweep with nothing to do wrote a second CUSTOMER_DATA_REDACTED record, so the "
        "audit trail's count of redactions is a count of sweeps instead"
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_is_unauthenticated_and_reveals_nothing(client: TestClient) -> None:
    """Liveness only. A health endpoint that names the schema revision is a reconnaissance
    endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_health_reports_silence_rather_than_asserting_a_threshold(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """A disabled webhook is silent total detection loss, so the interval has to be visible.

    No threshold is asserted by the endpoint, deliberately: a merchant with four failures a week and
    one with four hundred a day have normal silences that differ by orders of magnitude.
    """
    empty = client.get("/health/webhook", headers=tenant.auth).json()
    assert empty["last_event_at"] is None
    assert empty["seconds_since_last_event"] is None
    assert empty["events_last_24h"] == 0
    assert "ever been received" in empty["detail"], (
        "never having received an event is a louder condition than a long silence and the wording "
        "has to say so"
    )

    _drive_pipeline(installed_engine, client, tenant)

    live = client.get("/health/webhook", headers=tenant.auth).json()
    assert live["last_event_at"] is not None
    assert live["seconds_since_last_event"] is not None
    assert live["events_last_24h"] == 1
    assert live["verified_events_last_24h"] == 1

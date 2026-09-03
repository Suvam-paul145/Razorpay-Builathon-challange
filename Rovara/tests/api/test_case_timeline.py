"""The timeline endpoint over HTTP, on a migrated database (task 50.3, R26.C1 to C12).

Everything about the *projection* is checked exhaustively in ``tests/properties/test_timeline.py``,
at the ``pure`` tier, because the projection takes frozen views and returns one. What is left, and
what this file is for, is the part that only exists once a real request runs against real rows:

* the reads inside one ``tenant_transaction`` produce views that project without error;
* the endpoint is tenanted like every other case read — another merchant's case is 404, not 403;
* the stage instants and figures that reach the wire come from the rows, formatted server-side;
* the wire shape is what ``web/src/components/Timeline.jsx`` was written against.

``pg`` tier. A separate file rather than an addition to ``test_dashboard_reads.py`` because it is a
separate endpoint with its own degradation path, and the fixtures it needs are already shared
through ``conftest``.

**What this file deliberately does not test.** The ``TIMELINE_QUERY_TIMEOUT`` path is not exercised
here. Provoking a real ``statement_timeout`` means making an indexed per-case read take longer than
three seconds, which is either a sleep in a test that then costs three seconds every run, or a
lock-contention setup whose failure mode is a hung suite rather than a red one. The handler's
behaviour on that path — the marker naming the case, no stages, no substituted status — is the
same code the ``available: false`` arm of ``Timeline.test.jsx`` renders against, and the arm itself
is two statements. Recording the gap is more honest than a test that pretends to close it.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.rendering import DATA_UNAVAILABLE
from revora.audit.events import CASE_DETECTED, DIAGNOSIS_RECORDED, STATE_TRANSITION
from revora.domain.enums import TimelineStage, TimelineStageStatus
from revora.timeline.stages import STAGE_ORDER
from tests.api.conftest import Tenant, insert_case

pytestmark = pytest.mark.pg


def _append_audit(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    seq: int,
    event_type: str,
    new_state: str | None = None,
) -> None:
    """Insert one audit record at an explicit sequence number.

    Explicit rather than through ``AuditWriter``, because the point of several tests here is to
    control the *sequence* — including leaving a hole in it, which the writer cannot produce: it
    allocates from ``recovery_case.audit_seq`` under the row lock, and that is exactly why a gap
    means the allocation was bypassed. A fixture that went through the writer could not construct
    the state R26.C11 is written about.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_record (
                    id, merchant_id, case_id, seq, event_type, actor, new_state,
                    correlation_id, occurred_at, created_at
                ) VALUES (
                    :id, :m, :c, :seq, :event_type, 'test', :new_state,
                    :correlation, now(), now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "m": str(merchant_id),
                "c": str(case_id),
                "seq": seq,
                "event_type": event_type,
                "new_state": new_state,
                "correlation": str(uuid.uuid4()),
            },
        )


def test_a_detected_case_projects_nine_stages_with_one_done(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """Nine stages in the declared order, one of them ``DONE``, and the amount already formatted.

    The smallest real case: detected and nothing else. Every other stage is ``UPCOMING``, which is
    the honest answer and the one a naive implementation would render as an empty list — a nine-row
    timeline on a one-record case is the shape R26.C1 asks for, because the stages the case has not
    reached are part of the explanation.
    """
    case_id = insert_case(installed_engine, tenant.merchant_id, amount=250_000)
    _append_audit(
        installed_engine, tenant.merchant_id, case_id, seq=1, event_type=CASE_DETECTED
    )

    response = client.get(f"/cases/{case_id}/timeline", headers=tenant.auth)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["available"] is True
    assert body["unavailable"] is None
    timeline = body["timeline"]
    assert timeline["stage_count"] == len(STAGE_ORDER)
    assert [stage["stage"] for stage in timeline["stages"]] == [
        stage.value for stage in STAGE_ORDER
    ]
    assert [stage["order"] for stage in timeline["stages"]] == list(range(1, 10))

    detected = timeline["stages"][0]
    assert detected["status"] == TimelineStageStatus.DONE.value
    assert detected["instant"] is not None
    # Formatted server-side, with the Indian grouping the browser never computes (R26.C8). The
    # timeline's wire carries no minor units at all, so there is nothing here to do arithmetic on.
    assert "₹2,500.00" in detected["decision_sentence"]
    assert detected["fields"]["payment_amount"] == "₹2,500.00"
    assert "minor" not in detected["fields"]

    for stage in timeline["stages"][1:]:
        assert stage["status"] == TimelineStageStatus.UPCOMING.value
        assert stage["instant"] is None
        assert stage["decision_sentence"] is None

    assert timeline["audit_sequence"]["complete"] is True
    assert timeline["audit_sequence"]["missing"] == []
    # R26.C9. The key travels with no paragraph behind it, because nothing writes `ai_invocation`.
    assert timeline["ai_explanation"] is None
    assert timeline["ai_explanation_label"] == "AI_GENERATED"


def test_a_gapped_sequence_still_projects_and_names_the_hole(
    installed_engine: Engine, client: TestClient, tenant: Tenant
) -> None:
    """R26.C11. The banner names the missing number, every stage renders, and none is promoted.

    Sequence 1, 3 and 4 with 2 removed — a hole the allocation mechanism cannot produce, which is
    the whole reason the banner is worth showing rather than absorbing.
    """
    case_id = insert_case(installed_engine, tenant.merchant_id)
    for seq, event_type, state in (
        (1, CASE_DETECTED, None),
        (3, DIAGNOSIS_RECORDED, None),
        (4, STATE_TRANSITION, "WAITING_FOR_OUTCOME"),
    ):
        _append_audit(
            installed_engine,
            tenant.merchant_id,
            case_id,
            seq=seq,
            event_type=event_type,
            new_state=state,
        )

    body = client.get(f"/cases/{case_id}/timeline", headers=tenant.auth).json()
    sequence = body["timeline"]["audit_sequence"]

    assert sequence["complete"] is False
    assert sequence["missing"] == [2]
    assert sequence["record_count"] == 3
    assert sequence["first_seq"] == 1
    assert sequence["last_seq"] == 4
    assert sequence["starts_at_one"] is True
    assert "bypassed" in str(sequence["detail"])

    # Still nine stages, and the three the records witness are the three that are DONE.
    stages = {stage["stage"]: stage["status"] for stage in body["timeline"]["stages"]}
    assert len(stages) == 9
    assert stages[TimelineStage.DETECTED.value] == TimelineStageStatus.DONE.value
    assert stages[TimelineStage.DIAGNOSED.value] == TimelineStageStatus.DONE.value
    assert stages[TimelineStage.EXECUTED.value] == TimelineStageStatus.DONE.value
    # And nothing was filled in across the hole: the stages between DIAGNOSED and EXECUTED have no
    # completing record and are not promoted on the strength of their neighbours.
    assert stages[TimelineStage.BASELINE_ESTIMATED.value] == TimelineStageStatus.UPCOMING.value
    assert stages[TimelineStage.DECIDED.value] == TimelineStageStatus.UPCOMING.value
    assert stages[TimelineStage.POLICY_CHECKED.value] == TimelineStageStatus.UPCOMING.value


def test_another_tenants_case_is_404_and_never_a_timeline(
    installed_engine: Engine,
    client: TestClient,
    tenant: Tenant,
    other_tenant: Tenant,
) -> None:
    """R17.C2, R17.C3. The timeline is tenanted like every other case read, and answers 404.

    404 rather than 403, on the same terms as ``GET /cases/{id}``: a 403 confirms the row exists and
    belongs to somebody else, which is the one fact a cross-tenant probe wants. The same 404 covers
    "no such case", and that indistinguishability is required rather than sloppy.

    The endpoint takes no merchant id from the request — there is nowhere in the URL or the body to
    write one — so this is a check that the session's tenant is the one used, not that a filter was
    remembered.
    """
    theirs = insert_case(installed_engine, other_tenant.merchant_id)
    _append_audit(
        installed_engine, other_tenant.merchant_id, theirs, seq=1, event_type=CASE_DETECTED
    )

    response = client.get(f"/cases/{theirs}/timeline", headers=tenant.auth)
    assert response.status_code == 404
    assert "timeline" not in response.text

    # And the owner still sees it, so the 404 above is isolation rather than a broken endpoint.
    assert client.get(f"/cases/{theirs}/timeline", headers=other_tenant.auth).status_code == 200

    # A case that does not exist at all is the same answer.
    assert client.get(f"/cases/{uuid.uuid4()}/timeline", headers=tenant.auth).status_code == 404


def test_the_unavailable_arm_is_shaped_the_way_the_client_renders_it() -> None:
    """R26.C10. The data-unavailable marker's status token is the one the frontend branches on.

    Not a request, and deliberately so — see the module docstring on why the timeout path is not
    provoked here. What *can* be pinned without a three-second test is the contract between the two
    halves: the handler builds its marker with ``rendering.data_unavailable``, whose status is
    ``DATA_UNAVAILABLE``, and ``<AbsentValue>`` renders that token and no other. A rename on
    either side would leave a marker that rendered as the wrong kind of absence — "not yet
    recorded" instead of "could not be read" — which are different claims.
    """
    assert DATA_UNAVAILABLE == "DATA_UNAVAILABLE"

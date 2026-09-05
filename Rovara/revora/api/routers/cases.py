"""Case list, case detail, the audit trail, and human ownership.

Four things are worth reading before the handlers.

**Nothing here reads a merchant id from the request.** Every function takes the tenant from
``AuthenticatedSession`` and passes it to a repository that requires it. There is no path where a
path parameter, a query parameter or a body field names a tenant, so R17.C2 holds by there being
nowhere to write one rather than by a check that could be skipped.

**A case that is not visible answers 404 and records ``AUTHORIZATION_DENIED``.** Not 403: a 403
confirms the row exists and belongs to somebody else, which is the one fact a cross-tenant probe
wants. The same 404 covers "no such case", and that is required rather than sloppy — from the
caller's side the two must be indistinguishable.

**The list is bounded by ``DASHBOARD_PAGE_SIZE``** and ordered by descending detection timestamp
(R14.C2). A caller may ask for fewer; asking for more is silently clamped rather than rejected,
because a client that requests 500 wants as many as it can have and a 422 would give it none.

**Ownership is a control surface, not a label.** Assigning suspends every automated action through
policy check 7, so the write goes through ``revora.cases.ownership`` — locked, audited, and in the
layer that owns the case — rather than being an ``UPDATE`` here.

**The timeline is read in one transaction and projected outside it.** :func:`get_case_timeline`
opens a single ``tenant_transaction``, does every read inside it, reduces the rows to frozen views
and then calls the projection with no session in scope. That is deliberate on both halves: one
transaction means a concurrent write cannot change the input half way through a projection, and no
session means the projection has nothing to write with — see :mod:`revora.timeline.stages`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from revora.api.auth import AuthenticatedSession, deny_cross_tenant
from revora.api.deps import TenantSession
from revora.api.rendering import data_unavailable
from revora.api.views import (
    TimelineInputs,
    audit_document,
    case_detail,
    case_summary,
    case_summary_reads,
    timeline_inputs,
)
from revora.cases.ownership import (
    OwnershipOutcome,
    OwnershipResult,
    assign_owner,
    release_owner,
)
from revora.domain.enums import CaseState
from revora.persistence.models import RecoveryCase
from revora.persistence.repositories.audit import AuditRecordRepository
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger
from revora.timeline.stages import project

__all__ = ["TIMELINE_QUERY_TIMEOUT", "router"]

_logger = get_logger(__name__)

router = APIRouter(tags=["cases"])

TIMELINE_QUERY_TIMEOUT: Final[timedelta] = timedelta(seconds=3)
"""How long the timeline's reads may take before R26.C10's data-unavailable path applies.

Three seconds, an **[ASSUMPTION]** taken from the design and measured by nothing. Nine indexed
per-case reads should be far inside it; the bound exists so that a case whose audit trail has grown
past what a single request can read degrades into a named absence rather than into a page that
hangs.

**This is a module constant and every other bound in Revora is a configuration row, so the deviation
needs its reason.** ``revora.platform.config`` states the rule plainly — a change to a bound must be
recorded with an approving user, and a redeploy cannot name a person — and a bound belonging in that
catalogue also needs a seed row, which means an Alembic revision. At the time this endpoint was
written the configuration catalogue and the migration chain were both being edited by concurrent
work on a different feature, and adding a ``ConfigurationBound`` with no accompanying seed migration
does not degrade gracefully: ``default_configuration`` raises ``ConfigurationError`` on a bound with
no row, so every request in the system would fail rather than this one being slow.

So the constant is here, named exactly as the requirement names it, and moving it is a two-line
change: a ``ConfigurationBound("TIMELINE_QUERY_TIMEOUT", ValueKind.DURATION_SECONDS, "3", …)`` in
the catalogue, the matching ``timedelta`` field on ``Configuration``, and a seed row in the
migration that seeds the rest. Until then this value cannot be changed without a deploy, which is a
real limitation and is the whole of what the deviation costs.

Enforced as a Postgres ``statement_timeout`` rather than as a Python deadline, for the reason
``routers/metrics.py`` gives at length: a timer in the application leaves the query running on the
server, so a dashboard that times out repeatedly piles work onto the database it is already
struggling to read. A ``statement_timeout`` cancels the statement.
"""


class CaseListResponse(BaseModel):
    """A page of cases plus the paging facts a client needs to ask for the next one."""

    cases: list[dict[str, object]]
    page_size: int
    offset: int
    returned: int
    has_more: bool
    ordering: str = "detected_at descending"


@router.get("/cases", response_model=CaseListResponse)
def list_cases(
    current: TenantSession,
    state: Annotated[CaseState | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseListResponse:
    """This merchant's cases, newest detection first, paged at ``DASHBOARD_PAGE_SIZE``.

    ``has_more`` is derived by asking for one row beyond the page rather than by counting the
    table. A ``COUNT(*)`` on every list request is a scan whose cost grows with a tenant's success,
    and the only question a client actually has is whether to offer a next-page control.

    **The page's five supporting reads are issued once for the whole page, not once per row.** Each
    summary row draws on five other tables, and reading them per row made this endpoint issue five
    hundred statements for a hundred cases — a fan-out that arrived by composition rather than by
    anybody writing a loop over queries. :func:`~revora.api.views.case_summary_reads` fetches all
    five for the page and each ``case_summary`` is handed its own slice.

    What deliberately did **not** change is where the choosing happens. The batch reads fetch
    candidate rows and pick "the" recommendation, "the" diagnosis and the cycle's decisions in
    Python, by the same rules the per-case reads use — no joins, no ``DISTINCT ON``, no window
    functions. A join would put that choice in a second place, and a list column that disagrees with
    the detail page it links to is worse than either being wrong alone.
    """
    page_size = min(limit or current.config.DASHBOARD_PAGE_SIZE, current.config.DASHBOARD_PAGE_SIZE)

    with tenant_transaction(current.merchant_id) as session:
        repository = RecoveryCaseRepository(session)
        statement = repository.scoped(current.merchant_id)
        if state is not None:
            statement = statement.where(RecoveryCase.state == state.value)
        rows = list(
            session.execute(
                statement.order_by(RecoveryCase.detected_at.desc())
                .limit(page_size + 1)
                .offset(offset)
            ).scalars()
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        reads = case_summary_reads(session, current.merchant_id, page)
        cases = [
            case_summary(
                session,
                current.merchant_id,
                case,
                config=current.config,
                reads=reads[case.id],
            )
            for case in page
        ]

    return CaseListResponse(
        cases=cases,
        page_size=page_size,
        offset=offset,
        returned=len(cases),
        has_more=has_more,
    )


@router.get(
    "/cases/{case_id}",
    responses={404: {"description": "No case with this id is visible to this session."}},
)
def get_case(
    case_id: uuid.UUID,
    current: TenantSession,
) -> dict[str, object]:
    """The whole decision trail for one case (R14.C3 through C6, R14.C14).

    Diagnosis, baseline, every candidate with all six figures, the twelve ordered policy checks,
    every executed action, every authoritative read, and the verified outcome — plus the refusal
    block where the selection was ``DO_NOTHING`` or ``WAIT``.
    """
    with tenant_transaction(current.merchant_id) as session:
        case = RecoveryCaseRepository(session).get(current.merchant_id, case_id)
        if case is None:
            document = None
        else:
            document = case_detail(
                session, current.merchant_id, case, config=current.config
            )
    if document is None:
        raise deny_cross_tenant(current, resource="recovery_case", requested_id=case_id)
    return document


class AuditTrailResponse(BaseModel):
    case_id: str
    records: list[dict[str, object]]
    ordering: str = "per-case sequence ascending"


@router.get(
    "/cases/{case_id}/audit",
    response_model=AuditTrailResponse,
    responses={404: {"description": "No case with this id is visible to this session."}},
)
def get_case_audit(
    case_id: uuid.UUID,
    current: TenantSession,
) -> AuditTrailResponse:
    """The ordered audit trail for one case (R11.C5).

    Sequence order, not timestamp order. Two records can share a millisecond; the per-case sequence
    is allocated inside the transaction that wrote the record, so it is gap-free and says which came
    first even when the clock cannot.
    """
    with tenant_transaction(current.merchant_id) as session:
        case = RecoveryCaseRepository(session).get(current.merchant_id, case_id)
        records = (
            None
            if case is None
            else audit_document(
                AuditRecordRepository(session).list_for_case(current.merchant_id, case_id)
            )
        )
    if records is None:
        raise deny_cross_tenant(current, resource="recovery_case", requested_id=case_id)
    return AuditTrailResponse(case_id=str(case_id), records=records)


class CaseTimelineResponse(BaseModel):
    """The projected timeline, or a named absence — never a partial one dressed as whole.

    ``available`` is the field a client branches on, and it exists so the branch is one boolean
    rather than a shape test. When it is ``false``, ``timeline`` is ``null`` and ``unavailable``
    carries the marker naming this case; when it is ``true``, the reverse. Both keys are always
    present, so the response shape does not change with the outcome — a client whose rendering path
    depended on that would have two paths for one screen and the rare one would be the broken one.
    """

    case_id: str
    available: bool
    timeline: dict[str, object] | None = None
    unavailable: dict[str, object] | None = None


@router.get(
    "/cases/{case_id}/timeline",
    response_model=CaseTimelineResponse,
    responses={404: {"description": "No case with this id is visible to this session."}},
)
def get_case_timeline(
    case_id: uuid.UUID,
    current: TenantSession,
) -> CaseTimelineResponse:
    """One case's nine-stage timeline, projected from its Audit_Records (R26.C1 through C11).

    **One transaction, every read, then the projection outside it.** The reads happen inside a
    single ``tenant_transaction`` under ``TIMELINE_QUERY_TIMEOUT``, are reduced to frozen views
    there, and the projection runs after the block has closed. So a concurrent write cannot change
    the input part-way through — the input is a snapshot by the time anything looks at it — and the
    projection is called with no session in scope, which is what makes "it performs no write" a
    statement about what is expressible rather than about what it happens to do.

    **``now()`` is read here and passed in.** The projection reads no clock; it takes the instant as
    an argument and uses it for exactly one decision, whether the ``DECIDED`` stage is
    ``IN_PROGRESS`` because a further review remains permitted (R30.C14). Reading the clock at this
    boundary is what lets P56 assert that two projections of one unchanged input are equal — with
    the same ``now``, they are, and a projection that called ``now()`` itself could not promise it.

    **On timeout, a marker naming the case and no substituted anything** (R26.C10). Not a zero, not
    an empty stage list dressed as nine ``UPCOMING`` stages, and not a status invented to fill a
    row. Every stage successfully projected is still presented — which, because the reads are one
    transaction, is either all nine or none, and the honest response when it is none says so.

    The rejected alternative is worth recording, because it looks better and is not. Splitting the
    reads into two transactions — the audit spine in one, the figures in another — would let the
    nine stages project with their statuses intact when only the figure reads timed out, so more of
    the page would survive. It is rejected because the second transaction reads a later snapshot:
    the statuses would come from one instant and the figures from another, and a case that executed
    in between would show a stage marked ``UPCOMING`` beside a figure that only exists because it
    happened. A timeline that is internally inconsistent is worse than a timeline that is absent,
    and R26.C7's identical-on-repeat claim would no longer hold either.

    A gap in the audit sequence does **not** take this path. The timeline still renders, with the
    banner :class:`~revora.timeline.stages.SequenceIntegrity` carries, and no stage is asserted
    ``DONE`` on the strength of an absent record (R26.C11) — that guarantee is a property of the
    completion rules rather than of this handler, which is why the gap is reported rather than
    handled.
    """
    timeout_ms = int(TIMELINE_QUERY_TIMEOUT.total_seconds() * 1000)
    moment = now()

    try:
        with tenant_transaction(current.merchant_id) as session:
            session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            case = RecoveryCaseRepository(session).get(current.merchant_id, case_id)
            inputs: TimelineInputs | None = (
                None
                if case is None
                else timeline_inputs(
                    session, current.merchant_id, case, config=current.config
                )
            )
    except (OperationalError, DBAPIError) as exc:
        # Broad on the same terms as `routers/metrics.py`: a cancelled statement surfaces as a
        # driver error whose class depends on the driver and on the phase it was cancelled in, and
        # treating an unrecognised one as a hard failure would turn a slow read into a 500 on a page
        # that is allowed to say "not available" instead.
        _logger.warning(
            "case timeline unavailable within TIMELINE_QUERY_TIMEOUT",
            merchant_id=str(current.merchant_id),
            case_id=str(case_id),
            timeout_ms=timeout_ms,
            error=type(exc).__name__,
        )
        return CaseTimelineResponse(
            case_id=str(case_id),
            available=False,
            unavailable=data_unavailable(
                f"case_timeline:{case_id}",
                "the audit trail for this case could not be read within "
                "TIMELINE_QUERY_TIMEOUT. No stage is shown, because none was projected — "
                "nothing below has been substituted with a status or a zero.",
            ),
        )

    if inputs is None:
        raise deny_cross_tenant(current, resource="recovery_case", requested_id=case_id)

    timeline = project(
        inputs.records,
        inputs.case,
        inputs.signals,
        inputs.intents,
        inputs.figures,
        moment,
    )
    return CaseTimelineResponse(
        case_id=str(case_id), available=True, timeline=timeline.as_document()
    )


class OwnershipResponse(BaseModel):
    """The outcome, who holds the case, and what it means for automation."""

    outcome: str
    owner_user_id: str | None
    assigned_at: str | None
    automated_action_suspended: bool
    detail: str


_OWNERSHIP_DETAIL: dict[OwnershipOutcome, str] = {
    OwnershipOutcome.ASSIGNED: (
        "You own this case. Policy check HUMAN_OWNERSHIP will fail while you do, so Revora will "
        "not schedule or execute any automated action on it."
    ),
    OwnershipOutcome.RELEASED: (
        "Ownership released. Automated action may resume from the next decision cycle."
    ),
    OwnershipOutcome.ALREADY_OWNED: (
        "Another user already owns this case. Reassigning silently would leave two people "
        "believing they were responsible for it."
    ),
    OwnershipOutcome.NOT_OWNED: "This case has no owner, so there was nothing to release.",
    OwnershipOutcome.CASE_TERMINAL: (
        "This case has reached a terminal state. There is no automated action left to suspend."
    ),
    OwnershipOutcome.CASE_NOT_FOUND: "",
}


@router.post(
    "/cases/{case_id}/owner",
    response_model=OwnershipResponse,
    responses={
        404: {"description": "No case with this id is visible to this session."},
        409: {"description": "Already owned by another user, or the case is terminal."},
    },
)
def take_ownership(
    case_id: uuid.UUID,
    current: TenantSession,
) -> OwnershipResponse:
    """Take ownership of a case, suspending automated action (R14.C11, R8.C11).

    The owner is the session's user and cannot be specified in the request. Letting a caller name
    somebody else would be an assignment with no consent from the assignee and no accountability
    for the assigner, in a product whose whole ownership feature exists so that exactly one person
    is responsible.
    """
    result = assign_owner(
        current.merchant_id,
        case_id,
        user_id=current.merchant_user_id,
        config=current.config,
    )
    return _ownership_response(current, case_id, result)


@router.delete(
    "/cases/{case_id}/owner",
    response_model=OwnershipResponse,
    responses={
        404: {"description": "No case with this id is visible to this session."},
        409: {"description": "The case has no owner."},
    },
)
def release_ownership(
    case_id: uuid.UUID,
    current: TenantSession,
) -> OwnershipResponse:
    """Release ownership so automation may resume from the next decision cycle."""
    result = release_owner(
        current.merchant_id,
        case_id,
        user_id=current.merchant_user_id,
        config=current.config,
    )
    return _ownership_response(current, case_id, result)


def _ownership_response(
    current: AuthenticatedSession,
    case_id: uuid.UUID,
    result: OwnershipResult,
) -> OwnershipResponse:
    """Map an ownership outcome onto a status code and a sentence.

    ``CASE_NOT_FOUND`` becomes the same 404 and the same ``AUTHORIZATION_DENIED`` record as a read
    of an invisible case, because from the caller's side an ownership write on another tenant's case
    and on a nonexistent one must be indistinguishable.
    """
    outcome = result.outcome
    owner = result.owner_user_id
    assigned_at = result.assigned_at

    if outcome is OwnershipOutcome.CASE_NOT_FOUND:
        raise deny_cross_tenant(current, resource="recovery_case", requested_id=case_id)

    response = OwnershipResponse(
        outcome=outcome.value,
        owner_user_id=None if owner is None else str(owner),
        assigned_at=None if assigned_at is None else assigned_at.isoformat(),
        automated_action_suspended=outcome is OwnershipOutcome.ASSIGNED,
        detail=_OWNERSHIP_DETAIL[outcome],
    )
    if outcome in (
        OwnershipOutcome.ALREADY_OWNED,
        OwnershipOutcome.NOT_OWNED,
        OwnershipOutcome.CASE_TERMINAL,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=response.model_dump()
        )
    return response

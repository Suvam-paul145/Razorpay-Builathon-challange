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
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from revora.api.auth import AuthenticatedSession, deny_cross_tenant
from revora.api.deps import TenantSession
from revora.api.views import audit_document, case_detail, case_summary
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

__all__ = ["router"]

router = APIRouter(tags=["cases"])


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
        cases = [case_summary(session, current.merchant_id, case) for case in page]

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

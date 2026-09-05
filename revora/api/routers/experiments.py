"""Experiment definitions and their newest analysis.

An experiment with no analysis returns 200 with a not-yet-recorded marker rather than 404. A running
experiment is a normal state, and answering 404 would make "running" indistinguishable from "does
not exist" — which is exactly the confusion a merchant waiting for a result does not need.

Every result carries its interval bounds and every label. There is no endpoint that returns a lift
without them, and that is structural rather than a convention: :func:`revora.api.views.
experiment_document` is the only assembler and it emits all three together. A lift shown alone is a
number that looks like a finding, and whether zero sits inside the interval is the only thing that
licenses a causal claim.

**The list carries ``demonstration_incremental_revenue`` beside the experiments, once** (R28.C13).
It is a merchant-level figure rather than a per-experiment one — there is at most one completed
demonstration experiment worth reporting — and it is served here rather than only from
``/metrics/summary`` because the Experiments page is where a reader goes to ask "what did the
comparison find", and a page that showed a lift without the money figure that lift implies would
send them to a second screen to finish the sentence. The figure arrives with its two labels inside
its own object, so a client cannot render the amount without them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from revora.api.auth import deny_cross_tenant
from revora.api.deps import TenantSession
from revora.api.rendering import money
from revora.api.views import experiment_document
from revora.domain.enums import ExperimentState
from revora.metrics.engine import demonstration_finding
from revora.persistence.repositories.experiments import ExperimentRepository
from revora.persistence.repositories.session import tenant_transaction

__all__ = ["router"]

router = APIRouter(tags=["experiments"])

_LIST_LIMIT = 50
"""How many experiments a list returns. A merchant runs a handful, not a page of them; the bound
exists because every list read in this system has one, not because fifty is expected."""


class ExperimentListResponse(BaseModel):
    experiments: list[dict[str, object]]
    returned: int
    demonstration_incremental_revenue: dict[str, object]
    """Never named ``incremental_recovered_revenue`` here or anywhere (R28.C12).

    Declared on the response model rather than stuffed into the experiment documents, so the
    key is visible in the OpenAPI schema and a client cannot acquire it by accident."""


@router.get("/experiments", response_model=ExperimentListResponse)
def list_experiments(
    current: TenantSession,
    state: Annotated[ExperimentState | None, Query()] = None,
) -> ExperimentListResponse:
    """This merchant's experiments, newest first, with each one's latest analysis."""
    with tenant_transaction(current.merchant_id) as session:
        repository = ExperimentRepository(session)
        rows = (
            repository.list_by_state(current.merchant_id, state, limit=_LIST_LIMIT)
            if state is not None
            else repository.list_page(current.merchant_id, limit=_LIST_LIMIT)
        )
        documents = [
            experiment_document(
                session, current.merchant_id, row, currency=current.default_currency
            )
            for row in rows
        ]
        demonstration = demonstration_finding(session, current.merchant_id)
    figure = demonstration.as_document()
    if demonstration.presented:
        figure = {
            **figure,
            "amount": money(
                int(demonstration.value or 0), currency=current.default_currency
            ),
            "lift_interval": (
                None
                if demonstration.lift_ci_low is None
                else f"[{demonstration.lift_ci_low}, {demonstration.lift_ci_high}]"
            ),
        }
    return ExperimentListResponse(
        experiments=documents,
        returned=len(documents),
        demonstration_incremental_revenue=figure,
    )


@router.get(
    "/experiments/{experiment_id}",
    responses={404: {"description": "No experiment with this id is visible to this session."}},
)
def get_experiment(
    experiment_id: uuid.UUID,
    current: TenantSession,
) -> dict[str, object]:
    """One experiment with per-arm counts, interval bounds and every label (R14.C8)."""
    with tenant_transaction(current.merchant_id) as session:
        experiment = ExperimentRepository(session).get(current.merchant_id, experiment_id)
        document = (
            None
            if experiment is None
            else experiment_document(
                session, current.merchant_id, experiment, currency=current.default_currency
            )
        )
    if document is None:
        raise deny_cross_tenant(current, resource="experiment", requested_id=experiment_id)
    return document

"""Recording an opt-out or a consent, keyed on the customer.

``POST /consent`` appends one statement. It never updates and never deletes: a re-consent is a later
row and the reader takes the newest, so "when did they ask us to stop?" stays answerable. That
question is what a complaint actually turns on, and a mutable row cannot answer it.

**A contact may be supplied as text or as an already-derived key, and only one of the two.** Text is
what a dashboard has when an operator types an address from a support ticket; a key is what a
provider-side unsubscribe hook has. Accepting both and requiring exactly one keeps the derivation in
``platform.crypto`` — the only place holding the HMAC secret — rather than asking a caller to
pre-hash and hoping they used the right function.

**The contact is never stored and never echoed.** It is turned into a keyed hash and dropped. The
response returns the key, not the contact, so a client cannot accidentally render what it sent back
into a page.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from revora.api.deps import TenantSession
from revora.cases.consent import record_consent
from revora.platform.crypto import customer_key as derive_customer_key
from revora.platform.secrets import CredentialUnavailableError

__all__ = ["router"]

router = APIRouter(tags=["consent"])

_MAX_SOURCE_LENGTH = 200


class ConsentRequest(BaseModel):
    """One consent statement. Exactly one of ``contact`` or ``customer_key``.

    There is no ``merchant_id`` field, and no ``case_id`` either. The merchant comes from the
    session; the statement is about a person rather than a payment, so attaching it to a case would
    make the trail imply otherwise.
    """

    contact: str | None = Field(default=None, min_length=1, max_length=320)
    customer_key: str | None = Field(default=None, min_length=16, max_length=200)
    opted_out: bool
    source: str = Field(min_length=1, max_length=_MAX_SOURCE_LENGTH)
    """Where the statement came from — a support ticket reference, a dashboard action, a provider
    unsubscribe. Required, because a consent record whose provenance is unknown cannot be
    defended."""

    consent_expires_at: datetime | None = None
    """When consent lapses, if it does. Ignored for an opt-out: an opt-out does not expire."""

    @model_validator(mode="after")
    def exactly_one_identifier(self) -> ConsentRequest:
        """Refuse both and refuse neither.

        Accepting both would need a precedence rule, and a precedence rule here decides *whose*
        consent was recorded. Getting that wrong silently is the one failure this endpoint must not
        have, so the ambiguity is refused rather than resolved.
        """
        if (self.contact is None) == (self.customer_key is None):
            raise ValueError("supply exactly one of contact or customer_key")
        return self


class ConsentResponse(BaseModel):
    consent_id: str
    customer_key: str
    opted_out: bool
    effective_at: str
    affected_open_case_count: int
    supersedes_consent_id: str | None
    detail: str


@router.post(
    "/consent",
    response_model=ConsentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={503: {"description": "The customer-key secret could not be resolved."}},
)
def create_consent(
    body: ConsentRequest,
    current: TenantSession,
) -> ConsentResponse:
    """Append one consent statement for a customer (R17.C10).

    Authoritative for every policy evaluation beginning after ``effective_at``, across the cases
    that already exist and the ones that do not yet. Decisions already recorded are not revisited —
    a past APPROVED decision must not read, months later, as one made against an opt-out that did
    not exist at the time.
    """
    try:
        key = (
            body.customer_key
            if body.customer_key is not None
            else derive_customer_key(str(body.contact))
        )
    except CredentialUnavailableError as exc:
        # 503, not 500. The request is well-formed and will succeed once the credential is
        # configured, and answering 500 would send an operator looking for a bug in their payload.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the customer-key secret is not available; consent cannot be recorded",
        ) from exc

    record = record_consent(
        current.merchant_id,
        customer_key=key,
        opted_out=body.opted_out,
        source=f"{body.source} via {current.actor}",
        consent_expires_at=body.consent_expires_at,
        config=current.config,
    )

    return ConsentResponse(
        consent_id=str(record.consent_id),
        customer_key=record.customer_key,
        opted_out=record.opted_out,
        effective_at=record.effective_at.isoformat(),
        affected_open_case_count=record.affected_open_case_count,
        supersedes_consent_id=(
            None if record.supersedes is None else str(record.supersedes)
        ),
        detail=(
            "Recorded. This applies to every policy evaluation beginning after "
            f"{record.effective_at.isoformat()}, across existing and future cases of this "
            "customer. Decisions already recorded are not revisited."
        ),
    )

"""Customer consent, keyed on the customer rather than on the case.

An opt-out is a statement about a person, not about a payment. So this table is keyed on
``customer_key`` — the keyed, non-reversible hash of the normalized contact — and a lookup
returns the consent record for the *customer*, which is what makes an opt-out recorded
against a failed payment in March still hold for a different failed payment in June
(R17.C10, and what Property 8 checks).

That is also why the key is a hash rather than the contact: this column is the most widely
spread customer-derived value in the database, and a reversible one would make the table a
contact list.

The read is on the policy hot path — every decision that could produce a customer-visible
action does this lookup — which is what ``ix_customer_consent_merchant_id_customer_key``
exists for.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from revora.persistence.models.tenancy import CustomerConsent
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["CustomerConsentRepository"]


class CustomerConsentRepository(MerchantScopedRepository[CustomerConsent]):
    """The consent record behind policy checks 5 and 6."""

    model = CustomerConsent

    def for_customer(
        self, merchant_id: uuid.UUID, customer_key: str
    ) -> CustomerConsent | None:
        """The effective consent record for one customer, or ``None`` if none exists.

        ``None`` is meaningful rather than a failure: it means consent was never recorded,
        which fails the ``CONSENT_MISSING`` check. The policy engine distinguishes that
        from "the record exists but could not be read", which is ``UNAVAILABLE`` and
        blocks — because absence of permission and absence of knowledge call for the same
        refusal but different diagnoses.

        Newest ``effective_at`` first, so a later opt-out supersedes an earlier opt-in. The
        rows are history; the newest one is the customer's current wish.
        """
        statement = (
            self.scoped(merchant_id)
            .where(CustomerConsent.customer_key == customer_key)
            .order_by(CustomerConsent.effective_at.desc(), CustomerConsent.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def history_for_customer(
        self, merchant_id: uuid.UUID, customer_key: str, *, limit: int
    ) -> Sequence[CustomerConsent]:
        """Every consent record for one customer, newest first.

        For the case-detail view and for answering a customer who asks when they opted
        out. ``limit`` is required, like every list read in this package.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(CustomerConsent.customer_key == customer_key)
            .order_by(CustomerConsent.effective_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

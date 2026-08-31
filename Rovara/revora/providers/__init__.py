"""The provider adapter: typed call objects in, classified results out.

This package is the only place in Revora that talks to Razorpay, and it is a pure
adapter. It imports ``revora.platform`` and ``revora.domain`` and nothing else — no
persistence, no audit, no feature module — which is what keeps the layering contract
in ``.importlinter`` true and, more usefully, what keeps the question "what did the
provider say?" separate from the question "what should we record about it?". The
execution engine owns the second question and every transaction it implies.

Three things a caller needs to know:

* **Nothing here raises on a provider outcome.** Every client method returns one of
  five results. A caller that writes ``try`` around a call is a caller that will one
  day forget, and a crash between the committed intent and the recorded result is the
  window exactly-once exists to survive.
* **Branch on certainty, not on the variant.** ``effect_certainty`` maps the five
  results onto "exists", "does not exist" and "unknown", which is the distinction that
  actually decides whether another attempt is permitted.
* **The key is derived in one place.** ``reference_id_for`` produces the provider
  ``reference_id``, which is also the ``Idempotency_Key``, which is also the
  reconciliation query. Any second construction of it is a latent duplicate payment
  link.
"""

from __future__ import annotations

from revora.providers.classification import (
    CONCURRENCY_CAP_REJECTED,
    CREDENTIAL_UNAVAILABLE_CODE,
    INVALID_REQUEST_REFUSED,
    LOCAL_REFUSAL_CODES,
    MAX_RETAINED_RAW_BODY,
    PAYMENT_LINK_STATUSES,
    PAYMENT_STATUSES,
    CallPhase,
    ClientError,
    EffectCertainty,
    PaymentEntity,
    PaymentLinkEntity,
    PaymentLinkList,
    PaymentList,
    ProviderResult,
    ResultSource,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    classify_response,
    effect_certainty,
    is_definitive_failure,
)
from revora.providers.payment_link import (
    MAX_REFERENCE_ID_LENGTH,
    PROVIDER_EXPIRY_CEILING,
    REFERENCE_ID_PREFIX,
    CustomerContact,
    PaymentLinkRequest,
    PaymentLinkRequestError,
    build_payment_link_request,
    clamp_expire_by,
    reference_id_for,
    validate_description,
)
from revora.providers.razorpay import (
    MAX_CONCURRENT_PROVIDER_CALLS,
    MAX_PAYMENT_WINDOW_TS,
    MAX_PAYMENTS_PAGE_SIZE,
    MIN_PAYMENT_WINDOW_TS,
    OPERATION_CREATE_PAYMENT_LINK,
    OPERATION_FETCH_PAYMENT,
    OPERATION_FIND_PAYMENT_LINKS,
    OPERATION_LIST_PAYMENTS,
    PaymentProviderClient,
    RazorpayClient,
    split_timeout,
)

__all__ = [
    "CONCURRENCY_CAP_REJECTED",
    "CREDENTIAL_UNAVAILABLE_CODE",
    "INVALID_REQUEST_REFUSED",
    "LOCAL_REFUSAL_CODES",
    "MAX_CONCURRENT_PROVIDER_CALLS",
    "MAX_PAYMENTS_PAGE_SIZE",
    "MAX_PAYMENT_WINDOW_TS",
    "MAX_REFERENCE_ID_LENGTH",
    "MAX_RETAINED_RAW_BODY",
    "MIN_PAYMENT_WINDOW_TS",
    "OPERATION_CREATE_PAYMENT_LINK",
    "OPERATION_FETCH_PAYMENT",
    "OPERATION_FIND_PAYMENT_LINKS",
    "OPERATION_LIST_PAYMENTS",
    "PAYMENT_LINK_STATUSES",
    "PAYMENT_STATUSES",
    "PROVIDER_EXPIRY_CEILING",
    "REFERENCE_ID_PREFIX",
    "CallPhase",
    "ClientError",
    "CustomerContact",
    "EffectCertainty",
    "PaymentEntity",
    "PaymentLinkEntity",
    "PaymentLinkList",
    "PaymentLinkRequest",
    "PaymentLinkRequestError",
    "PaymentList",
    "PaymentProviderClient",
    "ProviderResult",
    "RazorpayClient",
    "ResultSource",
    "ServerError",
    "Success",
    "Timeout",
    "Unclassifiable",
    "build_payment_link_request",
    "clamp_expire_by",
    "classify_response",
    "effect_certainty",
    "is_definitive_failure",
    "reference_id_for",
    "split_timeout",
    "validate_description",
]

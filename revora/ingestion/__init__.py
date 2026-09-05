"""Event ingestion: untrusted HTTP into one durable, canonical, deduplicated event.

Public surface:

* :func:`revora.ingestion.service.ingest_webhook` — verify, canonicalize, dedup and
  enqueue for one inbound webhook.
* :func:`revora.ingestion.signature.verify_for_merchant` and
  :func:`revora.ingestion.signature.signature_canary`.
* :func:`revora.ingestion.canonical.canonicalize` — parse with the round-trip check.
"""

from __future__ import annotations

from revora.ingestion.canonical import CanonicalizationError, canonicalize
from revora.ingestion.service import (
    DETECTION_JOB_KIND,
    IngestionOutcome,
    IngestionResult,
    ingest_webhook,
)
from revora.ingestion.signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    signature_canary,
    verify_for_merchant,
)

__all__ = [
    "DETECTION_JOB_KIND",
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "CanonicalizationError",
    "IngestionOutcome",
    "IngestionResult",
    "canonicalize",
    "ingest_webhook",
    "signature_canary",
    "verify_for_merchant",
]

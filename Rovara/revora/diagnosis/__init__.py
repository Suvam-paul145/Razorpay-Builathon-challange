"""The diagnosis engine: why this payment failed, decided from structured data first.

The deterministic path is the primary path and it lives here. The provider publishes
the failure reason in a machine-handleable field, so the mapping table in
:mod:`revora.domain.failure_taxonomy` decides the common cases with no model, no
prompt, and no network call. What is left over is the tail.

Public surface:

* :func:`~revora.diagnosis.service.run_diagnosis` — records exactly one active
  diagnosis for a case's current decision cycle, inside the caller's transaction.
* :func:`~revora.diagnosis.service.resolve_recorded_diagnosis` — R3.C8's substitution
  rule, pure. Shared with the optional reasoning path so the rule has one
  implementation rather than one per caller.
* :class:`~revora.diagnosis.service.DiagnosisOutcome` — what the run did, plus the two
  things the caller must act on: the transition to ``DIAGNOSED`` and, for a
  merchant-side integration fault, the operational alert.

The taxonomy table itself is deliberately *not* re-exported from here. It sits in
``domain`` because it imports only the standard library, and a caller that wants to
classify error fields without touching a database should import it from there and be
visibly free of this package.
"""

from __future__ import annotations

from revora.diagnosis.service import (
    DETERMINISTIC_CONFIDENCE,
    SUBSTITUTION_BELOW_FLOOR,
    SUBSTITUTION_METHOD_UNTRUSTED,
    UNKNOWN_CONFIDENCE,
    UNTRUSTED_METHODS,
    DiagnosisOutcome,
    RecordedDiagnosis,
    resolve_recorded_diagnosis,
    run_diagnosis,
)

__all__ = [
    "DETERMINISTIC_CONFIDENCE",
    "SUBSTITUTION_BELOW_FLOOR",
    "SUBSTITUTION_METHOD_UNTRUSTED",
    "UNKNOWN_CONFIDENCE",
    "UNTRUSTED_METHODS",
    "DiagnosisOutcome",
    "RecordedDiagnosis",
    "resolve_recorded_diagnosis",
    "run_diagnosis",
]

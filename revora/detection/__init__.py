"""The detection engine: deterministic risk classification and case opening.

Public surface:

* :func:`revora.detection.rules.classify` — the pure, AI-free rule set.
* :func:`revora.detection.service.run_detection` — the transactional verdict,
  case-open-or-attach, and audit for one persisted event.
"""

from __future__ import annotations

from revora.detection.rules import DetectionResult, classify
from revora.detection.service import DetectionServiceResult, run_detection

__all__ = [
    "DetectionResult",
    "DetectionServiceResult",
    "classify",
    "run_detection",
]

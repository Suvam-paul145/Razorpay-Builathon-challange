"""The Case_Timeline read model. It owns no table and writes nothing.

The Recovery_Case detail view already presents ten sections and six tables, each faithful to a
requirement and none of them readable under time pressure. R26 adds no data to fix that: the
timeline is a projection of the *existing* per-case Audit_Record sequence — the one R11.C4 already
guarantees is gap-free and strictly increasing — into nine ordered stages with a sentence each.

Because it adds no data, its correctness claim cannot be "the rows are right". It is a **purity**
claim: the same Audit_Records always produce the same timeline (R26.C7, P56). Everything about the
shape of this package follows from making that claim provable rather than merely true today.

**What that costs, concretely.** :func:`revora.timeline.stages.project` takes frozen dataclasses and
returns one. No ``Session``, no repository, no clock, no configuration accessor — *it cannot write
because it has nothing to write with*, which is the same argument the base spec uses for
``policy.evaluate``. The reads happen one layer up, in :mod:`revora.api.routers.cases`, inside a
single ``tenant_transaction`` whose results are passed down as views; so a concurrent write cannot
change the input half way through a projection either.

**Placed in the layering band with ``revora.metrics`` and ``revora.outcome``.** It reads persisted
rows and writes nothing, and it imports nothing from ``revora.persistence`` — the router builds the
views. That import boundary is what makes the P56 test ``pure``-tier rather than ``pg``-tier: the
property generates audit sequences directly and projects them twice, with no database in reach to
be written to even accidentally.

Two files, and the split is between *what is true* and *how it is worded*:

* :mod:`revora.timeline.stages` — the stage enumeration, the frozen input views, the
  audit-keyed completion rules and the projection.
* :mod:`revora.timeline.templates` — the declared sentence templates and the R26.C14 label
  tables. It imports nothing from ``stages``, so the dependency runs one way and a template can
  be read without knowing how a stage was decided.
"""

from __future__ import annotations

__all__: list[str] = []

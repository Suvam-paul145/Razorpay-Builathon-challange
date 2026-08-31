"""Persistence: the schema, the migrations it is generated from, and repositories.

This package may import ``revora.domain`` and ``revora.platform`` and nothing else
from ``revora`` — the import contract in ``.importlinter`` enforces it. That is what
keeps the schema a statement about the domain rather than about whatever feature
module happened to need a column.

Three claims the design makes are database facts because of what lives here rather
than code promises:

* duplicate webhook delivery is safe — ``UNIQUE (merchant_id, provider_event_id)``
* one open case per payment — a partial unique index over the non-terminal states
* one external effect per authorization — ``UNIQUE (merchant_id, idempotency_key)``

and one more that is enforced twice over: the audit log is append-only, by a revoked
grant *and* a trigger.
"""

from __future__ import annotations

__all__: list[str] = []

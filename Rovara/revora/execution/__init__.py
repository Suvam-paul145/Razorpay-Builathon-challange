"""Execution: the only package in Revora that causes an external effect.

Everything here is arranged around one guarantee — **at most one external effect per
idempotency key, across any crash** — and one ordering decision that delivers it: the
execution intent is committed to Postgres *before* the provider call goes out, and the
per-case lock is released before it too.

That ordering is deliberately pessimistic. A worker that dies between the commit and the
call has burned a recovery attempt on a request that may never have been sent. The
alternative ordering — call first, record afterwards — loses the knowledge that a call
happened at all, and the next worker calls again. One wasted attempt is recoverable; a
customer asked twice to pay one invoice is not.

The modules, in the order a request moves through them:

* :mod:`~revora.execution.authorization` — assembles what policy sees, from reloaded rows.
  Shared with the decision pipeline so the pre-check and the act-time check ask the same
  question.
* :mod:`~revora.execution.contact` — the only place cleartext PII exists in this process.
  Decrypts just in time, returns a refusal rather than raising, never logs a value.
* :mod:`~revora.execution.messages` — every word a customer reads. Approved templates only,
  never generated prose.
* :mod:`~revora.execution.intents` — the durable record, and the mapping from a provider
  result to an intent state. Branches on effect *certainty*, never on the result variant.
* :mod:`~revora.execution.engine` — the two transactions and the call between them.
* :mod:`~revora.execution.reconcile` — resolves an unresolved intent by **reading**. The
  only provider operation reachable from it is a read; it can never repeat a create.

Two invariants to hold onto when reading any of it. An unresolved intent
(``ATTEMPTED``/``UNCERTAIN``) never permits another call — only a read may resolve it. And a
resolved intent is never rewritten, because the stability of the record, not the call count,
is what the guarantee actually rests on.
"""

from __future__ import annotations

from revora.execution.engine import (
    ExecutionAttempt,
    ExecutionOutcome,
    execute_approved_action,
)
from revora.execution.intents import (
    RESOLVED_STATES,
    UNRESOLVED_STATES,
    IntentDisposition,
)
from revora.execution.reconcile import (
    ReconcileOutcome,
    ReconcileResult,
    promote_stale_intents,
    reconcile_intents,
    unresolved_intent_count,
)

__all__ = [
    "RESOLVED_STATES",
    "UNRESOLVED_STATES",
    "ExecutionAttempt",
    "ExecutionOutcome",
    "IntentDisposition",
    "ReconcileOutcome",
    "ReconcileResult",
    "execute_approved_action",
    "promote_stale_intents",
    "reconcile_intents",
    "unresolved_intent_count",
]

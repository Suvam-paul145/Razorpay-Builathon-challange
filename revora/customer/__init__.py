"""The public customer surface: the one part of Revora reachable without a session.

Everything in this package is compensation for that absence. A customer has no
Merchant_User session and never will, so a second and deliberately weaker credential
exists — the Customer_Access_Token in :mod:`revora.customer.tokens` — bounded so
narrowly that its compromise costs one case's amount and one recovery opportunity.

The package sits in the ``.importlinter`` layering band alongside ``revora.detection``
and ``revora.ingestion``: it needs ``cases``, ``audit``, ``persistence``, ``platform``
and ``domain``, and nothing above. That is not a tidiness preference. Placing it any
higher would put the optimizer, the policy engine and the execution engine within
reach of code that runs on an unauthenticated request, and R18.C10's list of what a
token may not read — no recommendation, no policy decision, no metric, no
configuration value — would then be a matter of what this package happens to import
rather than of what it *can*.
"""

from __future__ import annotations

__all__: list[str] = []

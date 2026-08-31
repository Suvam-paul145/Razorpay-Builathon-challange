"""The policy engine: the only thing in Revora that can authorize an external effect.

Three modules, and the package is **pure** — it touches no database, no clock and no
network:

* :mod:`revora.policy.input` — ``PolicyInput``, the closed set of facts an evaluation may
  consider. There is no field an AI-produced value could occupy.
* :mod:`revora.policy.rules` — the versioned ``RuleSet`` a decision is judged against, so
  a past decision stays explicable after the rules move on.
* :mod:`revora.policy.engine` — ``evaluate``, a pure function running the twelve checks in
  a fixed order. No I/O, no clock, no randomness, no logging.

**Why there is no ``service`` module here.** The ``policy-isolation`` import contract
forbids this package from importing ``revora.persistence`` — along with ``reasoning``,
``estimation``, ``optimizer``, ``memory``, ``providers``, ``execution`` and ``synthetic``.
That is not an oversight in the contract: purity is what makes "identical inputs, identical
decision" (R8.C14) literally true, what makes any historical decision replayable from its
recorded inputs, and what lets Property 2 be checked by substituting every AI-produced
field and re-evaluating in microseconds with no fixtures.

So reading the persisted rows, building the ``PolicyInput``, calling ``evaluate`` and
writing the decision with its twelve check rows all live one layer up, in
:mod:`revora.jobs.pipeline`, which may import both this package and ``persistence``. The
task plan sketched that persistence as ``revora/policy/service.py``; the import contract is
the stronger authority, and the behaviour is identical either way.

Combined with ``PolicyInput`` having nowhere to put an AI value, the contract makes
Property 2 — a policy verdict is independent of AI output — a structural fact rather than a
rule to remember.
"""

from __future__ import annotations

from revora.policy.engine import (
    CheckResult,
    PolicyEvaluation,
    evaluate,
    idempotency_key_for,
)
from revora.policy.input import PolicyInput
from revora.policy.rules import DEFAULT_RULES_VERSION, RuleSet, default_rule_set

__all__ = [
    "DEFAULT_RULES_VERSION",
    "CheckResult",
    "PolicyEvaluation",
    "PolicyInput",
    "RuleSet",
    "default_rule_set",
    "evaluate",
    "idempotency_key_for",
]

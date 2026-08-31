"""The versioned rule set the policy engine evaluates against.

A decision reviewed six months from now has to be judged against the rules that were live
when it was made, not the ones live when it is read. That is why every ``policy_decision``
row carries a ``rule_set_version`` and why this type is passed into ``evaluate`` rather
than read from module globals: a rule set is a value, versions coexist, and a past
decision remains explicable after the current rules have moved on.

**Why the thresholds live here rather than being read inside the engine.** R15.C6 requires
a recorded approving user for a policy change, which is why the ~50 bounds live in
``app_config`` rather than in environment variables. The engine, though, must stay a pure
function — no session, no clock, no configuration lookup — so the bounds are lifted out of
configuration by the service layer, packed into this frozen value, and handed in. The
consequence worth stating: two evaluations with the same ``PolicyInput`` and the same
``RuleSet`` are identical decisions (R8.C14), and that would not be true of an engine that
consulted a mutable global.

**The fraud condition is a configured set, not a provider flag.** No dedicated fraud-flag
or risk-score field exists on the payment entity — the design verified that. What exists
is a set of failure reasons. So :attr:`RuleSet.risk_reason_codes` carries the configured
set and the fraud check derives from membership in it. This is the design's MODIFY of
R8.C5, and it means a newly discovered risk reason is a configuration change with a
recorded approver rather than a release.

**This module cannot import ``revora.memory``.** The import contract forbids it, and the
reason is specific: if a threshold could be derived from Recovery_Memory, then historical
outcomes would be feeding back into the authorization rules, and a run of bad luck could
quietly loosen the bound that stops a customer being messaged twice. Thresholds are chosen
by a person and recorded with their name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from revora.domain.actions import (
    ACTION_PRECEDENCE,
    CUSTOMER_VISIBLE_ACTIONS,
    ELIGIBILITY,
    EXECUTABLE_ACTIONS,
    NULL_ACTIONS,
    CandidateAction,
)
from revora.domain.enums import RiskCause
from revora.domain.money import Minor

__all__ = ["DEFAULT_RULES_VERSION", "RuleSet", "default_rule_set"]

_NO_MONEY: Final[Minor] = Minor(0)
_NO_PROBABILITY: Final[Decimal] = Decimal("0")
"""Module-level zeros, so the two carried-for-the-record thresholds can be defaulted
without calling a constructor in an argument default — which would be evaluated once at
import and shared, and is the bug ruff's B008 exists to catch."""

DEFAULT_RULES_VERSION: Final[str] = "v1-assumption-baseline"
"""The version label of the code-declared baseline rule set.

Named to say what it is. Every bound it carries is an ``[ASSUMPTION]`` placeholder
inherited from ``platform.config``, so a label like ``v1`` alone would imply a
deliberateness the numbers have not earned."""


@dataclass(frozen=True, slots=True)
class RuleSet:
    """One version of the policy rules: thresholds, the risk set, and the tables.

    Frozen, and the mappings inside it are read-only proxies, so an evaluation cannot
    mutate the rules it is being judged against — which would make the recorded
    ``rule_set_version`` a lie about what actually ran.
    """

    version_label: str

    # -- bounds the twelve checks compare against ----------------------------
    max_recovery_attempts: int
    max_customer_messages: int
    cooldown_interval: timedelta
    policy_decision_validity: timedelta

    # -- the derived fraud condition (R8.C5, as modified) --------------------
    risk_reason_codes: frozenset[str]
    """Provider failure reasons that constitute a risk signal. The fraud condition
    derives from membership in this set rather than from a provider flag field, because
    no such field exists."""

    # -- vocabulary the eligibility check reads ------------------------------
    eligibility: Mapping[RiskCause, frozenset[CandidateAction]] = field(
        default_factory=lambda: ELIGIBILITY
    )
    executable_actions: frozenset[CandidateAction] = field(
        default_factory=lambda: EXECUTABLE_ACTIONS
    )
    customer_visible_actions: frozenset[CandidateAction] = field(
        default_factory=lambda: CUSTOMER_VISIBLE_ACTIONS
    )
    null_actions: frozenset[CandidateAction] = field(default_factory=lambda: NULL_ACTIONS)
    precedence: tuple[CandidateAction, ...] = field(default_factory=lambda: ACTION_PRECEDENCE)

    # -- value thresholds, carried for the record rather than for a check ----
    min_net_value_threshold: Minor = _NO_MONEY
    min_incremental_probability: Decimal = _NO_PROBABILITY
    """Carried so the recorded decision names every bound that was live, including the
    ones the optimizer applied rather than the engine. A reviewer asking why an action was
    not proposed needs those two numbers, and the policy decision is the row they will be
    reading."""

    def permits_action_for_cause(
        self, action: CandidateAction, cause: RiskCause
    ) -> bool:
        """Whether the cause's eligibility row admits this action.

        The two null actions are always permitted, for every cause including ``UNKNOWN``.
        That is what guarantees the engine always has a legal answer: a case whose
        diagnosis was substituted to ``UNKNOWN`` permits nothing customer-visible, but it
        can still be recorded as a decision to do nothing, which is a decision rather
        than a dead end.
        """
        if action in self.null_actions:
            return True
        return action in self.eligibility.get(cause, frozenset())

    def is_customer_visible(self, action: CandidateAction) -> bool:
        """Whether the action consumes the customer-message cap."""
        return action in self.customer_visible_actions

    def is_executable(self, action: CandidateAction) -> bool:
        """Whether the MVP can actually perform the action."""
        return action in self.executable_actions

    def as_document(self) -> dict[str, object]:
        """The JSONB form stored on a ``policy_rule_set`` row.

        Every bound, so the stored rule set is self-describing: reconstructing what a
        past decision was judged against must not require the build that made it.
        """
        return {
            "version_label": self.version_label,
            "max_recovery_attempts": self.max_recovery_attempts,
            "max_customer_messages": self.max_customer_messages,
            "cooldown_interval_seconds": int(self.cooldown_interval.total_seconds()),
            "policy_decision_validity_seconds": int(
                self.policy_decision_validity.total_seconds()
            ),
            "risk_reason_codes": sorted(self.risk_reason_codes),
            "min_net_value_threshold": int(self.min_net_value_threshold),
            "min_incremental_probability": str(self.min_incremental_probability),
            "executable_actions": sorted(a.value for a in self.executable_actions),
            "customer_visible_actions": sorted(
                a.value for a in self.customer_visible_actions
            ),
            "eligibility": {
                cause.value: sorted(a.value for a in actions)
                for cause, actions in sorted(self.eligibility.items())
            },
        }


def default_rule_set(
    *,
    max_recovery_attempts: int,
    max_customer_messages: int,
    cooldown_interval: timedelta,
    policy_decision_validity: timedelta,
    risk_reason_codes: frozenset[str],
    min_net_value_threshold: Minor = _NO_MONEY,
    min_incremental_probability: Decimal = _NO_PROBABILITY,
    version_label: str = DEFAULT_RULES_VERSION,
) -> RuleSet:
    """Build the baseline rule set from configured bounds.

    Every argument is required except the two carried-for-the-record thresholds and the
    label. Required rather than defaulted on purpose: a rule set constructed with a
    silently defaulted attempt cap is a rule set whose most important bound nobody
    chose, and the failure mode is a case that permits more outbound actions than the
    merchant configured.
    """
    return RuleSet(
        version_label=version_label,
        max_recovery_attempts=max_recovery_attempts,
        max_customer_messages=max_customer_messages,
        cooldown_interval=cooldown_interval,
        policy_decision_validity=policy_decision_validity,
        risk_reason_codes=risk_reason_codes,
        eligibility=MappingProxyType(dict(ELIGIBILITY)),
        min_net_value_threshold=min_net_value_threshold,
        min_incremental_probability=min_incremental_probability,
    )

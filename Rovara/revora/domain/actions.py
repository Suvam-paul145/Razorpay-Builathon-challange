"""Candidate actions, what can actually be executed, and cause-to-action eligibility.

The full enumeration is the vocabulary of the value model. It is deliberately
wider than what the MVP can execute, because an action that appears in a
recommendation marked unavailable is more honest than one silently omitted — the
dashboard can then show "a retry would have been considered but is not available
on this account".

Three actions have no verified provider capability for one-off payments, so they
are marked unavailable at estimation time rather than removed from the vocabulary.
``PROMISE_TO_PAY_FOLLOW_UP`` is absent from every eligibility row, so it never
generates an estimate at all: it has no detection trigger in scope and no verified
capability. See the design's amendment list.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum, unique
from types import MappingProxyType

from revora.domain.enums import RiskCause

__all__ = [
    "ACTION_PRECEDENCE",
    "CUSTOMER_VISIBLE_ACTIONS",
    "ELIGIBILITY",
    "EXECUTABLE_ACTIONS",
    "NULL_ACTIONS",
    "PROVIDER_ACTIONS",
    "UNAVAILABLE_IN_MVP",
    "CandidateAction",
    "candidate_set_for",
    "is_customer_visible",
    "is_executable",
    "needs_provider_call",
]


@unique
class CandidateAction(StrEnum):
    """Every action the value model can reason about."""

    DO_NOTHING = "DO_NOTHING"
    WAIT = "WAIT"
    RETRY = "RETRY"
    DELAYED_RETRY = "DELAYED_RETRY"
    PAYMENT_LINK = "PAYMENT_LINK"
    CUSTOMER_MESSAGE = "CUSTOMER_MESSAGE"
    PAYMENT_METHOD_UPDATE = "PAYMENT_METHOD_UPDATE"
    PROMISE_TO_PAY_FOLLOW_UP = "PROMISE_TO_PAY_FOLLOW_UP"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"


NULL_ACTIONS: frozenset[CandidateAction] = frozenset(
    {CandidateAction.DO_NOTHING, CandidateAction.WAIT}
)
"""Always present in every candidate set, and always priced on the same terms as
the real actions. This is what makes the comparison honest rather than a search
for a reason to act."""

EXECUTABLE_ACTIONS: frozenset[CandidateAction] = frozenset(
    {
        CandidateAction.DO_NOTHING,
        CandidateAction.WAIT,
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
        CandidateAction.HUMAN_ESCALATION,
    }
)
"""What the MVP can actually carry out. ``CUSTOMER_MESSAGE`` is a payment link with
provider notification enabled — there is no separate messaging vendor."""

UNAVAILABLE_IN_MVP: frozenset[CandidateAction] = frozenset(
    {
        CandidateAction.RETRY,
        CandidateAction.DELAYED_RETRY,
        CandidateAction.PAYMENT_METHOD_UPDATE,
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
    }
)
"""No verified provider capability for one-off payments. A failed payment is
terminal at the provider; a new attempt is a new payment the customer starts.
Automatic retry exists in the provider's subscriptions product, which is a
different object and out of scope. If the retry-capability spike comes back
positive, this set shrinks and the eligibility rows below gain real actions."""

PROVIDER_ACTIONS: frozenset[CandidateAction] = frozenset(
    {CandidateAction.PAYMENT_LINK, CandidateAction.CUSTOMER_MESSAGE}
)
"""The actions the execution engine performs by calling the provider.

A **narrower** set than :data:`EXECUTABLE_ACTIONS`, and the distinction is load-bearing rather
than pedantic. ``DO_NOTHING`` and ``WAIT`` are executable in the sense that Revora can carry
them out — by not acting — and ``HUMAN_ESCALATION`` is executable by handing the case to a
person. Neither reaches the provider, so neither belongs in a state called
``ACTION_SCHEDULED``, which means "an external effect is pending".

Conflating the two produced a case that could never leave ``ACTION_SCHEDULED``: policy approved
``HUMAN_ESCALATION``, the pipeline scheduled it, the execution engine tried to build a payment
link for it, found no approved wording, refused — and left the case authorized, unexecuted and
waiting for a window to close. The dead end was not in the engine's refusal, which was correct;
it was in having scheduled an external effect that was never going to exist.
"""

CUSTOMER_VISIBLE_ACTIONS: frozenset[CandidateAction] = frozenset(
    {
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
    }
)
"""Actions the customer perceives. These consume the message cap, which is checked
before execution and incremented at execution."""

ACTION_PRECEDENCE: tuple[CandidateAction, ...] = (
    CandidateAction.DO_NOTHING,
    CandidateAction.WAIT,
    CandidateAction.PAYMENT_LINK,
    CandidateAction.CUSTOMER_MESSAGE,
    CandidateAction.DELAYED_RETRY,
    CandidateAction.RETRY,
    CandidateAction.PAYMENT_METHOD_UPDATE,
    CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
    CandidateAction.HUMAN_ESCALATION,
)
"""Tie-break order of last resort, after net value and then total cost have both
tied. Cheaper and less intrusive actions come first, so a tie resolves toward
doing less."""


_ELIGIBILITY: dict[RiskCause, frozenset[CandidateAction]] = {
    # Money is not there right now. Waiting genuinely helps, and a link lets the
    # customer pay when it is.
    RiskCause.INSUFFICIENT_FUNDS: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.DELAYED_RETRY,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Retrying the same dead instrument is pointless. The customer has to supply a
    # different one, which a link lets them do.
    RiskCause.EXPIRED_PAYMENT_METHOD: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.PAYMENT_METHOD_UPDATE,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Infrastructure failed, not the customer. Waiting is often the whole answer.
    RiskCause.BANK_OR_NETWORK_FAILURE: frozenset(
        {
            CandidateAction.RETRY,
            CandidateAction.DELAYED_RETRY,
            CandidateAction.PAYMENT_LINK,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Includes our own integration faults, which need an operational alert rather
    # than a customer contact.
    RiskCause.TECHNICAL_ISSUE: frozenset(
        {
            CandidateAction.RETRY,
            CandidateAction.PAYMENT_LINK,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # The customer walked away. A link is the only thing that brings them back.
    RiskCause.ABANDONMENT: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Wrong OTP, wrong CVV, a limit hit. The customer must do something.
    RiskCause.CUSTOMER_ACTION_REQUIRED: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # A human decides. Policy escalates this before any automated action is
    # scheduled, so the eligibility row only exists for completeness.
    RiskCause.FRAUD_OR_RISK_SIGNAL: frozenset({CandidateAction.HUMAN_ESCALATION}),
    # The narrowest set. A rejected or low-confidence diagnosis is substituted to
    # UNKNOWN, so a bad guess makes Revora more conservative, not less. Nothing
    # customer-visible is eligible here.
    RiskCause.UNKNOWN: frozenset({CandidateAction.HUMAN_ESCALATION}),
}

ELIGIBILITY: Mapping[RiskCause, frozenset[CandidateAction]] = MappingProxyType(_ELIGIBILITY)
"""Which actions a cause permits, beyond the two null actions. Read-only."""


def is_customer_visible(action: CandidateAction) -> bool:
    """True if the customer perceives this action, so it consumes the message cap."""
    return action in CUSTOMER_VISIBLE_ACTIONS


def is_executable(action: CandidateAction) -> bool:
    """True if the MVP can actually carry this action out."""
    return action in EXECUTABLE_ACTIONS


def needs_provider_call(action: CandidateAction) -> bool:
    """True if carrying this action out means calling the provider.

    What the pipeline branches on to decide whether a case should be scheduled for execution at
    all. An action that needs no call needs no ``ACTION_SCHEDULED``, no intent and no idempotency
    key — and giving it those is how a case ends up authorized for an effect that cannot happen.
    """
    return action in PROVIDER_ACTIONS


def candidate_set_for(cause: RiskCause) -> frozenset[CandidateAction]:
    """The candidate set for a cause: the null actions plus whatever it permits.

    A cause with no eligibility row permits nothing beyond the null actions. The
    set always has at least two members and never more than the nine in the
    enumeration.
    """
    return NULL_ACTIONS | ELIGIBILITY.get(cause, frozenset())

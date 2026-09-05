"""Candidate actions, what can actually be executed, and cause-to-action eligibility.

The full enumeration is the vocabulary of the value model. It is deliberately
wider than what the MVP can execute, because an action that appears in a
recommendation marked unavailable is more honest than one silently omitted — the
dashboard can then show "a retry would have been considered but is not available
on this account".

Three actions have no verified provider capability for one-off payments, so they
are marked unavailable at estimation time rather than removed from the vocabulary.

``PROMISE_TO_PAY_FOLLOW_UP`` used to be the fourth, and R24 moved it out. The design's
verification closed the one item it was waiting on: ``POST
/v1/payment_links/:id/notify_by/:medium`` re-notifies the link already recorded for the
case and creates no second link, so the capability is verified and
``PROVIDER_CAPABILITY_UNVERIFIED`` stopped being an honest thing to say about it. It is
now executable, needs a provider call, and was already customer-visible — so it already
consumed ``MAX_CUSTOMER_MESSAGES`` and nothing about the cap changes. See the design's
Fact 1 and Fact 2.
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
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
        CandidateAction.HUMAN_ESCALATION,
    }
)
"""What the MVP can actually carry out. ``CUSTOMER_MESSAGE`` is a payment link with
provider notification enabled — there is no separate messaging vendor.

``PROMISE_TO_PAY_FOLLOW_UP`` joined under R24.C1 and it is carried out by *re-notifying* the
link the case already has, which is a different mechanism from the other two rather than a
third copy of the same one: a resend creates no payment link, so the follow-up is the one
executable customer-visible action that mints no new payable object. Where the case holds no
live link the follow-up falls back to creating one (R24.C11), which is why it is in this set
rather than conditional on a link existing."""

UNAVAILABLE_IN_MVP: frozenset[CandidateAction] = frozenset(
    {
        CandidateAction.RETRY,
        CandidateAction.DELAYED_RETRY,
        CandidateAction.PAYMENT_METHOD_UPDATE,
    }
)
"""No verified provider capability for one-off payments. A failed payment is
terminal at the provider; a new attempt is a new payment the customer starts.
Automatic retry exists in the provider's subscriptions product, which is a
different object and out of scope. If the retry-capability spike comes back
positive, this set shrinks and the eligibility rows below gain real actions.

``RETRY`` and ``DELAYED_RETRY`` stay here, and stay visible in the recorded candidate set with
their exclusion reason (R6.C9, R26.C15). Three of these four became executable and one did
not, so the set is now three members rather than four — the interesting part being that it can
shrink at all: ``PROMISE_TO_PAY_FOLLOW_UP`` left it because a capability was *verified*, not
because anybody decided to be optimistic about it."""

PROVIDER_ACTIONS: frozenset[CandidateAction] = frozenset(
    {
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
    }
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
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
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
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Infrastructure failed, not the customer. Waiting is often the whole answer.
    RiskCause.BANK_OR_NETWORK_FAILURE: frozenset(
        {
            CandidateAction.RETRY,
            CandidateAction.DELAYED_RETRY,
            CandidateAction.PAYMENT_LINK,
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Includes our own integration faults, which need an operational alert rather
    # than a customer contact.
    RiskCause.TECHNICAL_ISSUE: frozenset(
        {
            CandidateAction.RETRY,
            CandidateAction.PAYMENT_LINK,
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # The customer walked away. A link is the only thing that brings them back.
    RiskCause.ABANDONMENT: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # Wrong OTP, wrong CVV, a limit hit. The customer must do something.
    RiskCause.CUSTOMER_ACTION_REQUIRED: frozenset(
        {
            CandidateAction.PAYMENT_LINK,
            CandidateAction.CUSTOMER_MESSAGE,
            CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
            CandidateAction.HUMAN_ESCALATION,
        }
    ),
    # A human decides. Policy escalates this before any automated action is
    # scheduled, so the eligibility row only exists for completeness.
    #
    # R24.C3 keeps PROMISE_TO_PAY_FOLLOW_UP out of this row and the next one, and the
    # reason is not that a promise is unlikely on a flagged case — it is that a promise
    # must not be a way *around* the flag. A customer under a fraud signal who submits
    # "I will pay on Friday" would otherwise have supplied Revora with a fresh reason to
    # message them, on a case whose whole disposition is that a person should look at it.
    RiskCause.FRAUD_OR_RISK_SIGNAL: frozenset({CandidateAction.HUMAN_ESCALATION}),
    # The narrowest set. A rejected or low-confidence diagnosis is substituted to
    # UNKNOWN, so a bad guess makes Revora more conservative, not less. Nothing
    # customer-visible is eligible here — the promise follow-up included, which is the
    # other half of R24.C3: a promise is evidence about the customer's intent and not
    # about the failure's cause, so it cannot be allowed to rescue a diagnosis Revora
    # does not have. R3.C8's substitution to UNKNOWN is what routes a rejected or
    # low-confidence hypothesis here, and this row is what makes that substitution cost
    # something.
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

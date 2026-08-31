"""The approved payment-link descriptions. Every word a customer reads is in this file.

**This copy needs product sign-off.** The wording below is a working default, not an
approved one, and it is isolated here precisely so that approving or replacing it is a
one-file review rather than an audit of the execution engine.

Templates, not free text, and that is a deliberate narrowing. Even in the design's optional
reasoning path the model's role is *selecting* among approved templates rather than drafting
prose — because a generated sentence shown to a paying customer carries a class of risk that
no content filter reliably catches: a promise Revora cannot keep, an amount stated wrongly, a
tone that reads as a threat, an implication about their bank. A fixed set of reviewed strings
removes that class entirely rather than mitigating it.

Four rules the templates hold to, each one a failure they are written to avoid:

* **No claim about why the payment failed.** The diagnosis may be ``UNKNOWN``, and even when
  it is not, telling a customer their card was declined for insufficient funds is a
  disclosure they did not ask for and may be wrong.
* **No urgency or consequence language.** No "immediately", no "final notice", no suggestion
  that something is lost by not paying. Revora exists to recover an incomplete payment, not
  to pressure anyone, and a recovery tool that reads as a debt collector damages the merchant
  it is working for.
* **No amount interpolated into the text.** The provider renders the authoritative amount
  from the ``amount`` field. A second copy of it in prose is a second thing that can disagree
  with the money, and if they ever disagree the customer is right to distrust both.
* **Merchant name, not ours.** The customer has a relationship with the merchant and none
  with Revora. Naming ourselves in a payment request would be confusing at best.

Every template is validated against ``MAX_MESSAGE_LENGTH`` (300) at build time by
``providers.payment_link.validate_description``, which *rejects* rather than truncates — a
truncated sentence shown to a customer is worse than no send at all.
"""

from __future__ import annotations

from typing import Final

from revora.domain.actions import CandidateAction

__all__ = [
    "DEFAULT_TEMPLATE_ID",
    "MessageTemplate",
    "description_for",
    "template_for_action",
]

_MERCHANT_PLACEHOLDER: Final[str] = "{merchant}"
"""The only substitution any template performs. Deliberately the only one: every additional
placeholder is another way for a template to say something untrue at render time."""


class MessageTemplate:
    """One approved description, identified so an audit record can name what was sent.

    The id is recorded rather than the rendered text, for two reasons. The rendered text
    contains the merchant's trading name, which does not need to be duplicated across every
    audit row; and an id makes "which wording did this customer see" answerable by lookup
    even after the copy is revised, whereas a stored string only answers it for rows written
    before the revision.
    """

    __slots__ = ("body", "template_id")

    def __init__(self, template_id: str, body: str) -> None:
        self.template_id = template_id
        self.body = body

    def render(self, *, merchant_name: str) -> str:
        """Substitute the merchant name. The only variable part of any message."""
        return self.body.replace(_MERCHANT_PLACEHOLDER, merchant_name.strip())

    def __repr__(self) -> str:
        return f"MessageTemplate(template_id={self.template_id!r})"


PAYMENT_LINK_NEUTRAL = MessageTemplate(
    "payment_link.neutral.v1",
    f"Complete your payment to {_MERCHANT_PLACEHOLDER}. "
    "Your earlier payment did not go through, so nothing has been charged. "
    "You can pay securely using this link.",
)
"""**[AWAITING PRODUCT SIGN-OFF]** The default, and the only one used today.

Written to be the least presumptuous sentence that still explains why the customer received
it. "Nothing has been charged" is there because the most likely thing a customer worries
about on receiving a second payment request is being charged twice — and stating it plainly
prevents a support contact. It is also true by construction: Revora only opens a case on a
payment the provider reported as failed, and ``accept_partial`` is false so a partial capture
cannot have occurred either."""


_BY_ACTION: Final[dict[CandidateAction, MessageTemplate]] = {
    CandidateAction.PAYMENT_LINK: PAYMENT_LINK_NEUTRAL,
}
"""Action to template. A mapping rather than a conditional so an action with no approved
wording is a missing key — a loud failure at execution time — rather than an empty string
quietly sent to a customer."""

DEFAULT_TEMPLATE_ID: Final[str] = PAYMENT_LINK_NEUTRAL.template_id


def template_for_action(action: CandidateAction) -> MessageTemplate | None:
    """The approved template for an action, or ``None`` if it has no approved wording.

    ``None`` rather than a fallback string. An action nobody has written copy for must not be
    executed with borrowed copy from a different action, because the borrowed sentence would
    describe something the system is not doing.
    """
    return _BY_ACTION.get(action)


def description_for(action: CandidateAction, *, merchant_name: str) -> str | None:
    """The rendered customer-visible description, or ``None`` if the action has no template."""
    template = template_for_action(action)
    if template is None:
        return None
    return template.render(merchant_name=merchant_name)

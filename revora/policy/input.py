"""``PolicyInput`` — every fact the policy engine may consider, and nothing else.

This type is one half of Property 2. The other half is the import contract that forbids
``revora.policy`` from importing ``revora.reasoning``, ``revora.estimation``,
``revora.optimizer`` and ``revora.memory``; this is the half that stops an AI-produced
value arriving as *data* rather than as an import.

**There is nowhere for a model's opinion to sit.** Every field below is a named scalar of
an enumerated or primitive type. There is no ``dict``, no ``Any``, no ``**kwargs``, no
``extra``, no ``metadata`` and no ``JSONB`` passthrough. A caller who wanted the policy
engine to consider a language model's confidence, its suggested action, or its
explanation would have to add a field to this frozen dataclass and a line to
:meth:`PolicyInput.from_persisted`, in a module that cannot import the reasoning layer,
and it would be visible in the diff to anyone reviewing it. That is the point: the
guarantee is enforced by the shape of the type rather than by a rule somebody has to
remember while writing a call site.

**``from_persisted`` is the only constructor, and it reads named columns.** It does not
read ``recommendation.ai_explanation_text`` — the column exists precisely so AI prose has
one place to live where nothing reads it for a decision. It does not read the diagnosis
confidence either, and that omission is deliberate in a way worth stating: an
AI-assisted diagnosis carries a confidence below 1.0, and if policy consulted that number
the model would be influencing authorization through the back door. Policy consults the
*recorded cause* — which the diagnosis layer has already substituted to ``UNKNOWN`` where
the answer was not trustworthy — and the ``risk_flagged`` column. Neither is an AI output.

**Absence is representable and is not the same as false.** Several fields are
``bool | None`` rather than ``bool``. ``None`` means "this fact could not be read", which
the engine turns into ``UNAVAILABLE`` and therefore into ``BLOCKED``. A missing input must
never read as a passed check: R8.C17 exists because the alternative — defaulting an
unreadable opt-out flag to "not opted out" — is how a system contacts somebody who asked
it not to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, RiskCause
from revora.domain.money import Minor

__all__ = ["CaseFacts", "ConsentFacts", "PolicyInput"]


class CaseFacts(Protocol):
    """The columns of ``recovery_case`` that policy is permitted to read.

    A Protocol rather than the ORM class, for two reasons. It keeps this package free of
    ``revora.persistence`` — which the ``policy-isolation`` import contract requires, since
    the engine's purity is what makes R8.C14 and Property 2 checkable. And it makes the
    coupling an explicit, reviewable list: this is the *complete* set of case columns a
    policy decision may consider, and adding one is an edit to this Protocol that a reviewer
    will see.

    Notably absent: anything AI-derived. There is no diagnosis confidence here and no
    explanation text, because policy consults the recorded cause — already substituted to
    ``UNKNOWN`` where the answer was not trustworthy — and never the model's own numbers.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    state: str
    version: int
    payment_amount: int
    customer_key: str
    decision_cycle_count: int
    executed_action_count: int
    customer_message_count: int
    last_outbound_at: datetime | None
    window_end_at: datetime
    human_owner_user_id: uuid.UUID | None
    risk_flagged: bool
    verified_payment_status: str | None


class ConsentFacts(Protocol):
    """The columns of ``customer_consent`` that policy is permitted to read."""

    opted_out: bool
    consent_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """The complete, closed set of facts one policy evaluation may consider.

    Frozen and slotted: frozen so an evaluation cannot mutate its own inputs partway
    through the twelve checks, slotted so an attribute that is not declared here cannot
    be attached to an instance at runtime. The second is not paranoia — it closes the one
    remaining route by which a caller could smuggle a field in without editing this file.
    """

    # -- identity -------------------------------------------------------------
    case_id: uuid.UUID
    merchant_id: uuid.UUID
    decision_cycle: int
    selected_action: CandidateAction
    """The action being authorized. Policy does not choose it — the optimizer proposes
    and policy permits or refuses. Which is why the engine can be a pure function of this
    input: it is answering "may this happen", not "what should happen"."""

    # -- case state -----------------------------------------------------------
    case_state: CaseState
    case_version: int
    payment_amount: Minor
    customer_key: str

    # -- verified payment state (the ALREADY_PAID check) ----------------------
    verified_payment_captured: bool | None
    """Whether an authoritative provider read says the payment is captured.

    ``None`` means no authoritative read exists yet, which is the ordinary state of a
    freshly detected case and is *not* the same as "not captured". The engine treats it
    as available-and-false only because a case is opened from a ``failed`` payment event,
    so the absence of a capture read on a failed payment is genuine evidence rather than
    a gap. See the check's own docstring — this is the one place absence is read as a
    negative, and it is justified there rather than assumed here."""

    verified_payment_status: str | None

    # -- consent (checks 5 and 6) --------------------------------------------
    customer_opted_out: bool | None
    """``None`` means the consent record could not be read. Blocks.

    The customer-wide status of R17.C10: this person asked not to be contacted at all, about
    anything. Distinct from :attr:`contact_suppressed` below, and R21.C9 turns on keeping the
    two distinct — a suppression is applied *without* setting this, so an objection to one debt
    stays legible as one."""

    contact_suppressed: bool
    """Whether a live Contact_Suppression covers this case's Suppression_Scope (R21.C3).

    The second input to check 5, and the reason R21 adds no thirteenth check. A hard stop is an
    absolute prohibition, and check 5 is where absolute prohibitions already sit — fifth of
    twelve, ahead of the window and all three counters. A check of its own would have had to be
    ordered somewhere, and every position left is after a bound, which is the one place a
    prohibition must not be.

    Resolved by the caller, like every other field here. ``revora.policy`` may not import
    ``revora.persistence`` — the ``policy-isolation`` contract forbids it, which is what makes
    the engine's purity structural — so the lookup happens in
    ``revora.execution.authorization`` and arrives as this boolean.
    ``revora.customer.suppression.suppression_in_force`` is the one function that performs it.

    A plain ``bool`` with no ``None``, unlike :attr:`customer_opted_out`, and the asymmetry is
    deliberate rather than an oversight. "The consent record could not be read" is a real state
    with its own answer, because a consent row can exist and be unreadable. A suppression lookup
    has no equivalent: it either found a row in force or there is none, and a read that fails
    raises and takes the transaction with it. There is no third value to represent, and adding
    one would create a branch that no caller can ever produce."""

    consent_expires_at: datetime | None
    consent_recorded: bool
    """Whether any consent row exists for this customer. Absence is a fact, not a gap:
    no row means consent was never recorded, which fails ``CONSENT_MISSING``."""

    # -- risk (check 4) -------------------------------------------------------
    risk_flagged: bool
    diagnosed_cause: RiskCause | None
    """The **recorded** cause from the active diagnosis, after the confidence-floor
    substitution the diagnosis layer applies. Not the model's original answer, and not
    accompanied by its confidence — see the module docstring."""

    # -- human ownership (check 7) -------------------------------------------
    human_owner_user_id: uuid.UUID | None

    # -- window and counters (checks 8 through 11) ---------------------------
    window_end_at: datetime
    executed_action_count: int
    customer_message_count: int
    last_outbound_at: datetime | None

    # -- duplicate action (check 3) ------------------------------------------
    open_intent_exists: bool
    """Whether an unresolved execution intent already exists for this case.

    ``ATTEMPTED`` or ``UNCERTAIN``. Either means a provider call may already have
    happened and its effect is unknown, so authorizing a second one risks the duplicate
    charge or duplicate message the whole system exists to prevent."""

    intent_exists_for_key: bool
    """Whether an intent already exists for the idempotency key this decision would
    mint. The narrower duplicate check: the same action at the same attempt ordinal has
    already been attempted."""

    # -- evaluation context --------------------------------------------------
    evaluated_at: datetime
    """The instant the evaluation is made against. Passed in rather than read from a
    clock inside the engine, which is what keeps ``evaluate`` free of I/O and makes
    "identical inputs give an identical decision" (R8.C14) literally true — a function
    that read a clock would return a different expiry on every call."""

    rules_version: str
    config_version: str | None = None

    @property
    def window_expired(self) -> bool:
        """Whether the recovery window has closed as of ``evaluated_at``."""
        return self.evaluated_at >= self.window_end_at

    @property
    def consent_expired(self) -> bool:
        """Whether recorded consent has lapsed as of ``evaluated_at``.

        A ``None`` expiry means indefinite consent, so it has not lapsed. A past expiry
        has, and blocks rather than being treated as still valid — consent is not
        perpetual and an expired record is closer to no record than to a live one.
        """
        return self.consent_expires_at is not None and self.evaluated_at >= self.consent_expires_at

    @classmethod
    def from_persisted(
        cls,
        *,
        case: CaseFacts,
        consent: ConsentFacts | None,
        verified_captured: bool | None,
        verified_status: str | None,
        diagnosed_cause: RiskCause | None,
        contact_suppressed: bool,
        open_intent_exists: bool,
        intent_exists_for_key: bool,
        selected_action: CandidateAction,
        evaluated_at: datetime,
        rules_version: str,
        config_version: str | None = None,
    ) -> PolicyInput:
        """Build from persisted rows, reading named columns only.

        The sole constructor. Every argument is either a row this reads named attributes
        off, or a value the caller computed from a row — never a payload, never a
        serialized document, never something a model produced.

        ``case`` and ``consent`` are typed as Protocols rather than as the ORM classes so
        that this module needs no import from ``persistence`` and stays testable without
        one. The Protocols above are the complete list of columns policy may read, so a
        renamed column is a type error here rather than a silent ``None``.

        Args:
            case: the ``recovery_case`` row.
            consent: the ``customer_consent`` row for this customer, or ``None`` if none
                exists. ``None`` is meaningful — it fails ``CONSENT_MISSING``.
            verified_captured: whether an authoritative read says captured. ``None``
                where no read exists.
            diagnosed_cause: the recorded cause from the active diagnosis, post
                substitution. ``None`` where no diagnosis exists, which blocks.
            contact_suppressed: whether a live ``contact_suppression`` row covers this case's
                Suppression_Scope. Required rather than defaulted to ``False``, and that is the
                point: a caller that has not performed the lookup is a type error here instead
                of a decision made against a suppression nobody read. Defaulting it would make
                the safe value the one you get by forgetting, which is the shape of every
                control that has ever silently stopped applying.
            selected_action: the action being authorized.
            evaluated_at: the instant to evaluate against.
        """
        opted_out: bool | None = None
        expires_at: datetime | None = None
        recorded = consent is not None
        if consent is not None:
            opted_out = bool(consent.opted_out)
            expires_at = consent.consent_expires_at

        return cls(
            case_id=case.id,
            merchant_id=case.merchant_id,
            decision_cycle=int(case.decision_cycle_count),
            selected_action=selected_action,
            case_state=CaseState(case.state),
            case_version=int(case.version),
            payment_amount=Minor(int(case.payment_amount)),
            customer_key=str(case.customer_key),
            verified_payment_captured=verified_captured,
            verified_payment_status=verified_status,
            customer_opted_out=opted_out,
            contact_suppressed=contact_suppressed,
            consent_expires_at=expires_at,
            consent_recorded=recorded,
            risk_flagged=bool(case.risk_flagged),
            diagnosed_cause=diagnosed_cause,
            human_owner_user_id=case.human_owner_user_id,
            window_end_at=case.window_end_at,
            executed_action_count=int(case.executed_action_count),
            customer_message_count=int(case.customer_message_count),
            last_outbound_at=case.last_outbound_at,
            open_intent_exists=open_intent_exists,
            intent_exists_for_key=intent_exists_for_key,
            evaluated_at=evaluated_at,
            rules_version=rules_version,
            config_version=config_version,
        )

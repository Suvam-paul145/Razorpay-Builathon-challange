"""The declared sentences and the label tables. Nothing here decides anything.

Two claims are being defended in this module, and both are structural rather than stylistic.

**A stage sentence is a declared template over persisted values, not composed prose** (R26.C3).
Every template is a module-level constant with named ``{…}`` placeholders, and every placeholder is
either a persisted column rendered as a string or a lookup in one of the fixed label tables below.
So the set of sentences this system can produce is enumerable by reading this file, and a reviewer
can check the wording of the ``DECIDED`` stage without running anything.

What that forbids, stated as the three things it forbids rather than as an aspiration:

* **No branching on a value's magnitude.** No template reads a number and picks different wording
  for a big one. "Recovered a substantial amount" is a judgement, and the whole reason the figure is
  presented is so the reader makes it.
* **No pluralization logic that reads a count twice.** ``Priced 1 options`` is ungrammatical and it
  is the correct trade. A plural rule is a second read of the count with a branch on it, and a
  branch on a count is the thing that eventually disagrees with the count printed beside it.
* **No free text.** There is nowhere in this module a sentence can be assembled from anything but a
  template and a substitution, and :func:`render` refuses a substitution set that does not match the
  template's placeholders exactly.

**A label carries the persisted enumeration member alongside it** (R26.C14). Every label table maps
to a :class:`Labelled`, which is the pair, and never to a bare string. That pairing is the
requirement and it is also the defence against the specific failure R26.C14 was written about:
``DO_NOTHING`` and ``WAIT`` share one label — *"Waiting and watching"* — because to a merchant they
are one situation, and a reader who saw only the label could no longer tell which of the two was
actually recorded. Showing only the member is the opposite failure: ``DO_NOTHING`` reads as
abandonment to anybody who has not read the requirements document.

**Why these tables are not the ones in ``revora.api.rendering``.** They overlap — ``rendering``
already holds ``SELECTED_ACTION_LABELS`` and ``CASE_STATE_LABELS`` for the case list and the
detail view — and this module cannot import them: ``revora.api`` sits above ``revora.timeline`` in
the layering contract, so the import would be upward and ``lint-imports`` refuses it. The
alternative to duplication would be pushing the tables down into ``revora.domain``, which would put
presentation vocabulary in the layer whose whole claim is that it holds none. So the shared label
string itself is defined once here and asserted character-identical to ``rendering``'s by a test,
which is where a divergence would actually be caught; the two *terminal* vocabularies differ on
purpose and the docstring on :data:`TERMINAL_STATE_LABELS` says why.

**Nothing in this module knows how a stage was decided.** It imports the stage vocabulary from
``revora.domain.enums`` and imports nothing from :mod:`revora.timeline.stages`. The dependency runs
one way so that a template can be read as a sentence rather than as a conclusion — and so that a
template existing for a stage no completion rule can reach is a visible fact rather than a
circular one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from string import Formatter
from types import MappingProxyType
from typing import Final

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    CustomerSignalKind,
    DelayReason,
    DiagnosisEvidenceSource,
    DiagnosisMethod,
    OutcomeClass,
    PolicyCheck,
    PolicyVerdict,
    RiskCause,
    SelectionReason,
    TerminalReason,
    TimelineStage,
)
from revora.domain.transitions import TERMINAL_STATES

__all__ = [
    "ACTION_LABELS",
    "CAUSE_LABELS",
    "DELAY_REASON_LABELS",
    "EVIDENCE_SOURCE_LABELS",
    "NOT_RECORDED",
    "OUTCOME_CLASS_LABELS",
    "POLICY_REASON_LABELS",
    "POLICY_VERDICT_LABELS",
    "SELECTION_REASON_LABELS",
    "SIGNAL_KIND_LABELS",
    "STAGE_TEMPLATES",
    "TERMINAL_REASON_LABELS",
    "TERMINAL_STATE_LABELS",
    "UNCERTAINTY_UNAVAILABLE",
    "WAITING_AND_WATCHING",
    "Labelled",
    "StageTemplate",
    "TemplateError",
    "labelled",
    "render",
]


class TemplateError(RuntimeError):
    """A substitution set did not match its template's placeholders exactly.

    Raised rather than tolerated, and raised on a *missing* key and a *surplus* one alike. A
    missing key would leave a ``{placeholder}`` on the page, which is visible and merely
    embarrassing. A surplus one is the dangerous half: it means the caller computed a value the
    declared sentence has no slot for, which is how a second, undeclared sentence starts existing
    beside the declared one.
    """


# ---------------------------------------------------------------------------
# The label pair (R26.C14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Labelled:
    """A human label and the persisted enumeration member it stands for.

    ``frozen`` and ``slots`` for the same reason the customer projection is: the pair is fixed at
    construction and cannot acquire a third field that a surface might render instead of either of
    these two.

    The member is a plain ``str`` rather than the enum instance. What is being presented is *the
    value the row holds*, and a row can hold a member this build's enumeration does not have —
    a case decided before a rename, say. Carrying the string means such a row renders its own value
    beside :data:`NOT_RECORDED`'s label rather than raising on the way to a page a reader needed.
    """

    label: str
    member: str

    def as_document(self) -> dict[str, str]:
        """The wire form: both halves, always, under fixed keys."""
        return {"label": self.label, "member": self.member}


NOT_RECORDED: Final[str] = "Not recorded"
"""The label for a value the audit trail does not carry.

A sentence-cased phrase rather than a sentinel token, because it is rendered in the position a
label occupies. It is paired with the member ``""`` by :func:`labelled`, so the wire form still
carries two keys and a client never has to branch on the shape.

Deliberately *not* the same thing as ``NOT_YET_RECORDED`` in :mod:`revora.api.rendering`. That
marker is about a *figure* the pipeline has not produced and names the case state that explains it.
This is about a *label* for a value that is present in one column and absent in another — for
instance an ``OUTCOME_VERIFIED`` stage completed by a terminal transition, where there is a
terminal state and no recovered amount. Collapsing the two would make the second look like the
first, and the first is the one a reader is entitled to read as "wait for it"."""

UNCERTAINTY_UNAVAILABLE: Final[str] = "UNCERTAINTY_UNAVAILABLE"
"""What the ``BASELINE_ESTIMATED`` evidence sentence says where no interval was recorded (R26.C4).

The token, not a phrase, and not an omission. R26.C4 names this exact value, and the reason it is a
loud machine-readable token in the middle of an English sentence is that the alternative readings
are both wrong: an interval rendered as ``[0, 1]`` claims a measurement of total ignorance, and no
interval at all lets a bare point probability read as a precise one. An estimate without an
interval is a guess wearing a decimal point, and the absence of the interval *is* the finding."""

WAITING_AND_WATCHING: Final[str] = "Waiting and watching"
"""The one label ``DO_NOTHING`` and ``WAIT`` share (R26.C14).

Character-identical to :data:`revora.api.rendering.WAITING_AND_WATCHING`, and a test asserts the
equality rather than the two being wired together — the layering contract forbids this module from
importing that one, so the string is duplicated and the duplication is guarded. The words matter:
a merchant reading either member's own name would read a case that Revora deliberately left alone
as a case Revora gave up on, and those are opposite statements about the same row."""


def labelled(table: Mapping[str, Labelled], member: str | None) -> Labelled:
    """Look a member up in a label table, or return the not-recorded pair.

    The only lookup helper in this module, and every table is read through it. Two behaviours,
    each closing a hole a bare ``table[member]`` would leave:

    ``None`` — the column is empty — returns ``Labelled(NOT_RECORDED, "")``. This is the ordinary
    case rather than a defensive one: a stage completed by a state transition has no selected
    action, a page-view signal has no delay reason, and a terminal case has no recovered amount.

    A member the table does not hold returns the not-recorded label **with the member preserved**,
    so the presented row still shows the value the database actually holds. That is the honest
    failure direction for a read model: a label nobody chose is worse than no label, and losing
    the stored value as well would leave a reader with nothing to check against the row.
    """
    if member is None:
        return Labelled(label=NOT_RECORDED, member="")
    found = table.get(member)
    if found is None:
        return Labelled(label=NOT_RECORDED, member=member)
    return found


def _table(pairs: Mapping[str, str]) -> Mapping[str, Labelled]:
    """Build a label table from member-to-label pairs.

    Present so each table below reads as the vocabulary decision it is, without a
    ``Labelled(label=..., member=...)`` on every line obscuring which word was chosen for which
    member.
    """
    return MappingProxyType(
        {member: Labelled(label=label, member=member) for member, label in pairs.items()}
    )


def _assert_total(name: str, table: Mapping[str, Labelled], members: Iterable[str]) -> None:
    """Fail at import if a label table does not cover its enumeration.

    Totality asserted rather than defaulted, on the same terms as
    ``revora.customer.projection.PLAIN_LANGUAGE_CAUSE``: a ``.get`` with a generic fallback would
    let a member added tomorrow ship under wording nobody chose, and :func:`labelled` already
    provides the fallback for the *row* case — a value in a column that this build's enumeration
    does not know. That is a different situation from a member this build *does* know and has no
    words for, and the second one is a mistake rather than a state.
    """
    missing = sorted(set(members) - set(table))
    if missing:  # pragma: no cover - import-time invariant
        raise RuntimeError(
            f"{name} has no label for {missing}; a label table must be total over its "
            "enumeration so a new member cannot reach a merchant under wording nobody chose"
        )


# ---------------------------------------------------------------------------
# The label tables (R26.C14, R26.C4)
# ---------------------------------------------------------------------------

ACTION_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        CandidateAction.DO_NOTHING.value: WAITING_AND_WATCHING,
        CandidateAction.WAIT.value: WAITING_AND_WATCHING,
        CandidateAction.RETRY.value: "Retry the charge",
        CandidateAction.DELAYED_RETRY.value: "Retry the charge later",
        CandidateAction.PAYMENT_LINK.value: "Send a payment link",
        CandidateAction.CUSTOMER_MESSAGE.value: "Message the customer",
        CandidateAction.PAYMENT_METHOD_UPDATE.value: "Ask for a different card",
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value: "Follow up on a promise to pay",
        CandidateAction.HUMAN_ESCALATION.value: "Hand to a person",
    }
)
"""Every ``CandidateAction``, labelled, with the two null actions sharing one label.

Deliberately word-for-word the same table as ``revora.api.rendering.SELECTED_ACTION_LABELS``, and
a test asserts they stay equal. This is the one duplication in this module that has no defensible
difference — a merchant moving between the case list and the timeline must not find the action
they selected described in two ways — so the guard is an equality assertion rather than a
docstring asking people to remember."""

_assert_total("ACTION_LABELS", ACTION_LABELS, (action.value for action in CandidateAction))

TERMINAL_STATE_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        CaseState.RECOVERED.value: "Recovered",
        CaseState.STOPPED.value: "Stopped — bound reached",
        CaseState.BLOCKED.value: "Blocked by policy",
        CaseState.EXPIRED.value: "Window closed",
        CaseState.ESCALATED.value: "With a person",
        CaseState.FAILED.value: "Failed",
    }
)
"""The Terminal_States, each under its own distinct label (R26.C14).

``STOPPED``, ``BLOCKED`` and ``EXPIRED`` are the same money and three completely different
problems: a bound we set, a rule we applied, and time running out. R26.C14 requires three distinct
labels and the wording here is the requirement's own — *"Stopped — bound reached"*, *"Blocked by
policy"*, *"Window closed"*.

**These are narrower than ``rendering.CASE_STATE_LABELS`` and the difference is deliberate.** That
table labels all fourteen states for a *status column*, where the reader wants to know what the
case is doing now and ``STOPPED`` reads best as "Stopped trying". This table appears inside one
sentence — ``Ended: {terminal_reason}.`` — at the end of a nine-stage history, where the reader
already knows the case ended and the useful half of the label is *why*. Naming the bound in the
label is what turns an ending into an explanation at the position it is read.

Only the terminal states are here. A non-terminal state has no place in this table because no
sentence in :data:`STAGE_TEMPLATES` presents one: an unfinished case is described by its stage
statuses, not by a state name.

Total over ``revora.domain.transitions.TERMINAL_STATES`` rather than over the six members listed
above, and asserted so. The domain owns which states are terminal; a seventh added there without a
label here would otherwise present a case's ending as ``Not recorded``."""

_assert_total(
    "TERMINAL_STATE_LABELS", TERMINAL_STATE_LABELS, (state.value for state in TERMINAL_STATES)
)

CAUSE_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        RiskCause.INSUFFICIENT_FUNDS.value: "Not enough funds at the time",
        RiskCause.EXPIRED_PAYMENT_METHOD.value: "Card or account details expired",
        RiskCause.BANK_OR_NETWORK_FAILURE.value: "The bank could not be reached",
        RiskCause.TECHNICAL_ISSUE.value: "A technical problem during the attempt",
        RiskCause.ABANDONMENT.value: "Started but not finished",
        RiskCause.CUSTOMER_ACTION_REQUIRED.value: "Needs one more step from the customer",
        RiskCause.FRAUD_OR_RISK_SIGNAL.value: "Flagged by a risk control",
        RiskCause.UNKNOWN.value: "Not clear from what the bank said",
    }
)
"""Every ``RiskCause``, labelled for a reviewer.

**Not the same sentences as ``revora.customer.projection.PLAIN_LANGUAGE_CAUSE``, and the audience
is the whole reason.** Those are written for the person who owes the money: they are full
sentences, they never accuse, and ``FRAUD_OR_RISK_SIGNAL`` deliberately declines to say what it is
— telling a customer their payment was flagged is either an accusation or a hint to a fraudster.
These are written for a reviewer with two minutes who needs the fact, so the same cause reads
*"Flagged by a risk control"*: naming the control is exactly what the operator has to know and
exactly what the customer must not be told.

Short noun phrases rather than sentences, because they are substituted into
``Diagnosed as {cause}.`` and a sentence inside a sentence reads as a quotation."""

_assert_total("CAUSE_LABELS", CAUSE_LABELS, (cause.value for cause in RiskCause))

DIAGNOSIS_METHOD_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        DiagnosisMethod.DETERMINISTIC.value: "from the taxonomy table",
        DiagnosisMethod.AI_ASSISTED.value: "AI-assisted",
        DiagnosisMethod.REJECTED_AI_OUTPUT.value: "after refusing the model's answer",
        DiagnosisMethod.FALLBACK_UNKNOWN.value: "no method resolved it",
    }
)
"""How the cause was arrived at (R26.C4), phrased to fit inside the evidence sentence.

``REJECTED_AI_OUTPUT`` says the model was asked and its answer was refused, rather than reading as
a method in its own right. That is the fact a reviewer needs: the reasoning layer ran, produced
something that failed validation, and the deterministic path carried the decision — which is the
system working, and it looks identical to a failure if the label does not say so."""

_assert_total(
    "DIAGNOSIS_METHOD_LABELS",
    DIAGNOSIS_METHOD_LABELS,
    (method.value for method in DiagnosisMethod),
)

EVIDENCE_SOURCE_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value: "the provider's error fields",
        DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value: "what the customer said",
    }
)
"""What was read to reach the cause (R26.C4, R20.C4).

Two sources, and the timeline is one of the places the distinction has to survive. A provider error
code is an authoritative observation of a failed charge; a stated reason is a person's account of
their own finances typed into a public page. Both may inform an estimate and neither authorizes
anything, but only one of them is evidence a reviewer should weigh at face value — so the sentence
says which, rather than leaving both under the word "evidence"."""

_assert_total(
    "EVIDENCE_SOURCE_LABELS",
    EVIDENCE_SOURCE_LABELS,
    (source.value for source in DiagnosisEvidenceSource),
)

SELECTION_REASON_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        SelectionReason.HIGHEST_NET_VALUE.value: "it was worth the most",
        SelectionReason.NO_POSITIVE_VALUE.value: "nothing was worth doing",
        SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value: (
            "this customer was likely to pay anyway"
        ),
    }
)
"""Why the optimizer chose what it chose.

The two null-action reasons are not interchangeable and the labels keep them apart: *"nothing was
worth doing"* and *"this customer was likely to pay anyway"* are different findings and a merchant
shown the same words for both learns nothing from either. Both are phrased as findings rather than
as failures — "we chose not to act" being read as "we could not act" is the misreading this
product can least afford."""

_assert_total(
    "SELECTION_REASON_LABELS",
    SELECTION_REASON_LABELS,
    (reason.value for reason in SelectionReason),
)

POLICY_VERDICT_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        PolicyVerdict.APPROVED.value: "Approved",
        PolicyVerdict.BLOCKED.value: "Blocked",
        PolicyVerdict.DEFERRED.value: "Deferred",
        PolicyVerdict.ESCALATE.value: "Escalated to a person",
    }
)
"""The four policy verdicts. Only ``APPROVED`` ever permitted an external effect."""

_assert_total(
    "POLICY_VERDICT_LABELS",
    POLICY_VERDICT_LABELS,
    (verdict.value for verdict in PolicyVerdict),
)

POLICY_REASON_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        PolicyCheck.ALREADY_PAID.value: "the payment had already been made",
        PolicyCheck.ALREADY_TERMINAL.value: "the case had already ended",
        PolicyCheck.DUPLICATE_ACTION.value: "the same action was already in flight",
        PolicyCheck.FRAUD_OR_RISK.value: "a risk control applied",
        PolicyCheck.CUSTOMER_OPTED_OUT.value: "the customer asked not to be contacted",
        PolicyCheck.CONSENT_MISSING.value: "no consent on record",
        PolicyCheck.HUMAN_OWNERSHIP.value: "a person owns this case",
        PolicyCheck.WINDOW_EXPIRED.value: "the recovery window had closed",
        PolicyCheck.MAX_ATTEMPTS_REACHED.value: "the attempt bound was reached",
        PolicyCheck.MAX_MESSAGES_REACHED.value: "the message bound was reached",
        PolicyCheck.COOLDOWN_ACTIVE.value: "the cooldown had not elapsed",
        PolicyCheck.ACTION_NOT_ELIGIBLE.value: "the action was not eligible for this cause",
    }
)
"""All twelve checks, labelled, because any one of them can be the primary reason.

Total over the twelve rather than over "the ones that usually decide". The evaluation order is
fixed precisely so the stated reason cannot be an expensive or case-specific check, and a label
table covering only the common ones would put the uncommon reason on the page as a bare
enumeration member — on the row explaining why a customer was not contacted, which is the row
that most needs to be readable."""

_assert_total(
    "POLICY_REASON_LABELS", POLICY_REASON_LABELS, (check.value for check in PolicyCheck)
)

SIGNAL_KIND_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        CustomerSignalKind.PAGE_VIEWED.value: "opened the page",
        CustomerSignalKind.DELAY_REASON.value: "gave a reason",
        CustomerSignalKind.PROMISE_TO_PAY.value: "promised a date",
        CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST.value: "asked about paying in parts",
    }
)
"""Every ``CustomerSignalKind`` with its submission instant is presented (R26.C4).

``PAGE_VIEWED`` gets a label of its own rather than being filtered out. "The customer opened the
link and said nothing" is evidence about the next decision — often the only evidence there is —
and a stage that showed nothing for it would be indistinguishable from a customer who never
arrived."""

_assert_total(
    "SIGNAL_KIND_LABELS", SIGNAL_KIND_LABELS, (kind.value for kind in CustomerSignalKind)
)

DELAY_REASON_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: "waiting on money coming in",
        DelayReason.BANK_OR_CARD_PROBLEM.value: "a problem with their bank or card",
        DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW.value: "the amount is too high right now",
        DelayReason.DISPUTES_THE_CHARGE.value: "disputes the charge",
        DelayReason.NO_LONGER_WANTS_THE_ORDER.value: "no longer wants the order",
        DelayReason.OTHER.value: "a reason outside the list",
    }
)
"""The six stated reasons, reported as the customer's account rather than as a finding.

Phrased in the third person and without endorsement, because that is what the record is: a
stranger's statement about their own finances, persisted as evidence and authorizing nothing. The
two hard-stop members read plainly — *"disputes the charge"*, *"no longer wants the order"* — since
both end contact permanently and a euphemism there would hide the most consequential thing a
customer can say."""

_assert_total(
    "DELAY_REASON_LABELS", DELAY_REASON_LABELS, (reason.value for reason in DelayReason)
)

OUTCOME_CLASS_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        OutcomeClass.NATURAL.value: "paid without us acting",
        OutcomeClass.OBSERVED.value: "paid after we acted — not shown to be because we acted",
        OutcomeClass.ATTRIBUTED.value: "paid, and a controlled comparison supports the credit",
    }
)
"""The three recovery classes, which license three different claims.

``OBSERVED``'s label carries its own caveat inside the label, and that is the point of it being
here rather than in a footnote. This is the class almost every recovery lands in, the sentence it
appears in is the one a merchant screenshots, and *"paid after we acted"* without the second half
is precisely the overstatement the whole metrics design exists to refuse."""

_assert_total(
    "OUTCOME_CLASS_LABELS", OUTCOME_CLASS_LABELS, (outcome.value for outcome in OutcomeClass)
)

TERMINAL_REASON_LABELS: Final[Mapping[str, Labelled]] = _table(
    {
        TerminalReason.RECOVERED_VERIFIED.value: "the payment was verified as captured",
        TerminalReason.RECOVERY_WINDOW_ELAPSED.value: "the recovery window closed",
        TerminalReason.MAX_ATTEMPTS_REACHED.value: "the attempt bound was reached",
        TerminalReason.DECISION_CYCLE_LIMIT_REACHED.value: "the decision-cycle bound was reached",
        TerminalReason.CUSTOMER_OPTED_OUT.value: "the customer asked not to be contacted",
        TerminalReason.ALREADY_PAID.value: "the payment had already been made",
        TerminalReason.FRAUD_OR_RISK_FLAG.value: "a risk control applied",
        TerminalReason.PAYMENT_STATE_UNVERIFIABLE.value: (
            "the payment's state could not be established"
        ),
        TerminalReason.EXECUTION_RESULT_UNVERIFIABLE.value: (
            "whether the action took effect could not be established"
        ),
        TerminalReason.POLICY_BLOCKED.value: "policy blocked every available action",
        TerminalReason.HUMAN_OWNERSHIP.value: "a person took the case over",
        TerminalReason.COMMUNICATION_FAILED.value: "the message could not be delivered",
        TerminalReason.CUSTOMER_DISPUTED_CHARGE.value: "the customer disputed the charge",
        TerminalReason.CUSTOMER_CANCELLED_ORDER.value: (
            "the customer no longer wants the order"
        ),
        TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT.value: (
            "the customer asked to pay differently"
        ),
        TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW.value: (
            "the customer named a date past the recovery window"
        ),
    }
)
"""Why the case ended, in the words a reviewer needs.

The two ``…_UNVERIFIABLE`` members are the ones worth reading. Both mean *we stopped because we
could not establish a fact*, not *we established a bad fact* — and both are the deliberately
unsatisfying ending the design chose over guessing. A label that read "failed" would collapse
them into the thing they were chosen instead of.

The four customer-stated endings are phrased in the customer's voice — *the customer disputed*,
*the customer asked* — and not in Revora's. Every other label describes something the system
concluded; these describe something a person said, and a label reading "chasing was stopped"
would attribute the ending to the system that merely obeyed it. The distinction matters on the
one screen where a merchant decides who to call back."""

_assert_total(
    "TERMINAL_REASON_LABELS",
    TERMINAL_REASON_LABELS,
    (reason.value for reason in TerminalReason),
)


# ---------------------------------------------------------------------------
# The sentence templates (R26.C3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageTemplate:
    """One stage's two sentences: what it decided, and what it decided on.

    Two rather than one, because R26.C3 asks for both and they answer different questions. *"Chose
    to send a payment link, worth ₹412.00"* is the decision; *"Reason: it was worth the most"* is
    the evidence. A single combined sentence would let the second be dropped as redundant, and it
    is the one a reviewer disagrees with.
    """

    decision: str
    evidence: str


STAGE_TEMPLATES: Final[Mapping[TimelineStage, tuple[StageTemplate, ...]]] = MappingProxyType(
    {
        TimelineStage.DETECTED: (
            StageTemplate(
                decision="A failed payment of {amount} was detected.",
                evidence="From provider payment {provider_payment_id}.",
            ),
        ),
        TimelineStage.DIAGNOSED: (
            StageTemplate(
                decision="Diagnosed as {cause} with confidence {confidence}.",
                evidence="From {evidence_source} ({method}).",
            ),
        ),
        TimelineStage.BASELINE_ESTIMATED: (
            StageTemplate(
                decision="Without acting, this was estimated {probability} likely to be paid.",
                evidence="Interval {interval}.",
            ),
            StageTemplate(
                decision="Without acting, this was estimated {probability} likely to be paid.",
                evidence="{uncertainty}.",
            ),
        ),
        TimelineStage.ALTERNATIVES_PRICED: (
            StageTemplate(
                decision="Priced {priced_count} options; {unavailable_count} were unavailable.",
                evidence="Cheapest available option cost {cheapest_total_action_cost}.",
            ),
        ),
        TimelineStage.DECIDED: (
            StageTemplate(
                decision=(
                    "Chose {action}, worth {net_recovery_value}. "
                    "Runner-up {runner_up} at {runner_up_value}."
                ),
                evidence="Reason: {selection_reason}.",
            ),
        ),
        TimelineStage.POLICY_CHECKED: (
            StageTemplate(
                decision="Policy verdict {verdict}.",
                evidence="Primary reason: {primary_reason}.",
            ),
        ),
        TimelineStage.EXECUTED: (
            StageTemplate(
                decision="Sent {action} at {instant}.",
                evidence="Provider result: {intent_state}.",
            ),
        ),
        TimelineStage.CUSTOMER_RESPONDED: (
            StageTemplate(
                decision="Customer {signal_kind}: {delay_reason}.",
                evidence="Submitted {instant}.",
            ),
        ),
        TimelineStage.OUTCOME_VERIFIED: (
            StageTemplate(
                decision="Recovered {amount}, classified {outcome_class}.",
                evidence="Verified by provider read at {instant}.",
            ),
            StageTemplate(
                decision="Ended: {terminal_reason}.",
                evidence="Recorded at {instant}.",
            ),
        ),
    }
)
"""One template per stage — two, where a stage has two genuinely different things to say.

**Why some stages carry two and what decides between them.** ``BASELINE_ESTIMATED`` and
``OUTCOME_VERIFIED`` each have a variant pair, and in both cases the choice turns on **whether a
record exists**, never on what a value is: an interval was recorded or it was not; the case was
verified as recovered or it ended for another reason. Those are two different sentences, not one
sentence with a hole in it, and expressing them as a tuple of declared alternatives keeps the
choice enumerable — a reader can see both possible wordings without running the projection, which
an inline conditional inside a format string would not allow.

The variants are indexed by position rather than named, and ``stages.py`` selects by index with
the reason written at the call site. Indices rather than an enumeration because the alternatives
are local to one stage and a two-member enum per stage would be five more names to keep in step
with these tuples.

**No stage's template reads a count twice or branches on a magnitude.** ``Priced 1 options`` is
what this table produces for a single candidate and that is the accepted cost: a plural rule is a
second read of ``priced_count`` with a branch on it, and the failure mode of branching on a count
is a sentence that disagrees with the number printed inside it.

**Every ``{…}`` is either a persisted column rendered as a string or a label-table lookup.** The
two money placeholders — ``{amount}``, ``{net_recovery_value}``, ``{runner_up_value}`` and
``{cheapest_total_action_cost}`` — are substituted with the *server-formatted string*, never with
minor units. No arithmetic happens in this module, nothing here divides by a power of ten, and the
projection hands the formatted figure in. That is R26.C8 held at the place a sentence is built,
which is the one place a currency figure could be quietly reconstructed."""

_MISSING_STAGE_TEMPLATES = sorted(
    stage.value for stage in TimelineStage if stage not in STAGE_TEMPLATES
)
if _MISSING_STAGE_TEMPLATES:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "STAGE_TEMPLATES has no template for "
        f"{_MISSING_STAGE_TEMPLATES}; R26.C3 requires a declared deterministic template for "
        "every stage, and a stage with none would present a status and no explanation"
    )


def render(template: str, substitutions: Mapping[str, str]) -> str:
    """Substitute into a declared template, refusing any mismatch.

    ``str.format_map`` over an exact key set, checked in both directions before the call. The check
    is the whole value of this function existing rather than the call sites using ``.format``
    directly:

    A **missing** key would raise ``KeyError`` from ``format_map`` anyway; it is checked here so the
    message names the template and the whole missing set rather than the first key encountered.

    A **surplus** key would be silently ignored by ``format_map``, and that is the failure worth
    catching. A caller passing a value the declared sentence has no slot for has computed something
    the template does not present — which is either a placeholder somebody removed from the
    template without removing its producer, or the beginning of a second sentence assembled outside
    this module. Both are the thing R26.C3 forbids, and both are invisible without this check.

    Every substituted value is already a ``str``. The signature says so, so no formatting decision
    — no ``:.2f``, no thousands separator, no rounding — can be taken here: there is no number to
    take it on.
    """
    required = _placeholders(template)
    supplied = frozenset(substitutions)
    if required != supplied:
        raise TemplateError(
            f"template {template!r} declares {sorted(required)} but was given "
            f"{sorted(supplied)}: missing {sorted(required - supplied)}, "
            f"surplus {sorted(supplied - required)}"
        )
    return template.format_map(substitutions)


def _placeholders(template: str) -> frozenset[str]:
    """The ``{name}`` placeholders in a template.

    Parsed with :meth:`str.format`'s own machinery rather than with a regular expression, so what
    is checked is exactly what ``format_map`` will read. A regex would be a second, approximate
    parser of the same syntax and would disagree with the first on ``{{`` — which is not used in
    any template above and is precisely the sort of thing that becomes true later.
    """
    return frozenset(
        name for _, name, _, _ in Formatter().parse(template) if name is not None and name != ""
    )

"""What policy is shown, assembled from persisted rows. One construction, used twice.

The decision pipeline evaluates policy when it schedules an action. The execution engine
evaluates it again, against freshly reloaded state, at the instant it is about to act.
Those two evaluations have to be asking the same question — and the only way to guarantee
that is for them to build the input the same way, from one function.

Two evaluations rather than one, because the gap between them is real. A job sits in the
queue; a worker restarts; a merchant revokes consent; the customer pays through another
channel; the recovery window closes. The scheduling decision was correct when it was made
and that is not the same as being correct now. The design states it plainly: authority is
re-requested against reloaded state, never inherited from a payload.

**Why this lives under** ``revora.execution``. It needs both ``revora.policy`` and the
persistence repositories, and the layering contract admits only ``execution``, ``outcome``,
``metrics`` and ``experiment`` as homes for something that imports both and is reachable
from the engine — ``revora.cases`` is *below* policy, so it cannot import it. Of those,
execution is the honest owner: it is the component that may not act without an answer, and
the pipeline's earlier evaluation is a pre-check that has to agree with it. Naming the
authoritative asker as the owner is better than putting it in a neutral module and leaving
the reader to guess which of the two callers is definitive.

Nothing here writes. Assembly and evaluation only, so a caller can ask the question inside
a transaction it is about to roll back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from revora.domain.actions import CandidateAction
from revora.domain.enums import RiskCause
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.policy.engine import PolicyEvaluation, evaluate, idempotency_key_for
from revora.policy.input import CaseFacts, ConsentFacts, PolicyInput
from revora.policy.rules import RuleSet, rule_set_from_config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.persistence.models import RecoveryCase
    from revora.platform.config import Configuration

__all__ = [
    "AuthorizationInput",
    "assemble_policy_input",
    "evaluate_against_reloaded_state",
    "verified_captured_from",
]


def verified_captured_from(verified_status: str | None) -> bool | None:
    """Whether a recorded verified status means captured. ``None`` when none was read.

    ``None`` and ``False`` are different answers and the distinction is load-bearing: no
    read having happened is not evidence that the payment did not succeed, and a policy
    check that conflated them would treat an unread payment as a confirmed failure and
    contact the customer about money they may already have sent.
    """
    if verified_status is None:
        return None
    return verified_status == "captured"


@dataclass(frozen=True, slots=True)
class AuthorizationInput:
    """The assembled input, plus the two derived values a caller needs afterwards.

    ``prospective_key`` is returned rather than recomputed by the caller because it is the
    idempotency key, which is also the provider ``reference_id``, which is also the
    reconciliation query. A second derivation of it anywhere is a latent duplicate payment
    link, so it is derived once and handed over.
    """

    candidate: PolicyInput
    rules: RuleSet
    prospective_key: str
    decision_cycle: int


def assemble_policy_input(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    action: CandidateAction,
    config: Configuration,
    moment: datetime,
    decision_cycle: int | None = None,
) -> AuthorizationInput:
    """Load every fact policy is entitled to see, and nothing else.

    The reads are deliberately narrow. Policy sees the case row, the consent row, the
    active diagnosis's cause, and two booleans about existing intents — no payload, no
    recommendation body, no model output. That narrowness is the mechanism behind
    Property 2: an input built only from named columns of persisted rows cannot be
    influenced by anything a language model produced, whatever else is in the database.

    Args:
        case: the case row. The engine passes one it is holding ``FOR UPDATE``; the
            pipeline passes one it has merely read. Either is fine here — this function
            does not write, so it imposes no locking requirement of its own.
        decision_cycle: the cycle to read the diagnosis for. Defaults to the case's own
            counter. The pipeline overrides it with the recommendation's cycle, because the
            counter advances on the edge into ``DECISION_PENDING`` and is therefore one
            ahead of the cycle a fresh recommendation belongs to.
    """
    cycle = case.decision_cycle_count if decision_cycle is None else decision_cycle
    diagnosis = DiagnosisRepository(session).active_for_cycle(merchant_id, case.id, cycle)
    consent = CustomerConsentRepository(session).for_customer(merchant_id, case.customer_key)
    decisions = PolicyDecisionRepository(session)
    rules = rule_set_from_config(config)

    # The key for the attempt this evaluation would authorize. The ordinal is the count of
    # actions already executed plus one, so re-executing the same ordinal after a crash
    # derives the same key and the unique constraint recognises the retry.
    prospective_key = idempotency_key_for(
        case_id=case.id, action=action, attempt_ordinal=case.executed_action_count + 1
    )

    # The one place an ORM row is bridged to policy's read-only Protocols. `Mapped[X]`
    # is not structurally `X` to a type checker even though instance access yields `X`,
    # and the Protocols are a decoupling device — the complete, reviewable list of columns
    # policy may read — rather than a runtime boundary. Centralising the assembly is what
    # reduces this to a single cast; two call sites would mean two.
    candidate = PolicyInput.from_persisted(
        case=cast("CaseFacts", case),
        consent=cast("ConsentFacts | None", consent),
        verified_captured=verified_captured_from(case.verified_payment_status),
        verified_status=case.verified_payment_status,
        diagnosed_cause=RiskCause(diagnosis.cause) if diagnosis is not None else None,
        open_intent_exists=decisions.open_intent_exists(merchant_id, case.id),
        intent_exists_for_key=decisions.intent_exists_for_key(merchant_id, prospective_key),
        selected_action=action,
        evaluated_at=moment,
        rules_version=rules.version_label,
        config_version=config.version,
    )
    return AuthorizationInput(
        candidate=candidate,
        rules=rules,
        prospective_key=prospective_key,
        decision_cycle=cycle,
    )


def evaluate_against_reloaded_state(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    action: CandidateAction,
    config: Configuration,
    moment: datetime,
    decision_cycle: int | None = None,
) -> tuple[PolicyEvaluation, AuthorizationInput]:
    """Assemble and evaluate. The engine's authority check.

    Returns the evaluation alongside the input it was computed from, so a caller recording
    a refusal can name the state it refused against rather than re-reading it.
    """
    assembled = assemble_policy_input(
        session,
        merchant_id,
        case,
        action=action,
        config=config,
        moment=moment,
        decision_cycle=decision_cycle,
    )
    return evaluate(assembled.candidate, assembled.rules), assembled

# Requirements Document

## Introduction

Revora is an AI-assisted incremental revenue recovery decision system. Revora detects revenue at risk, diagnoses why the revenue is at risk, estimates what happens with no intervention, simulates candidate recovery interventions, selects the intervention with the highest safe incremental recovery value, validates that selection through a deterministic policy layer, executes a bounded action, verifies the payment outcome against authoritative payment data, and measures revenue actually recovered.

Revora is not a payment reminder service, a chatbot, a voice agent, a retry engine, a dunning platform, a collection bot, or a multi-agent demonstration. The decision question Revora answers is not "which customer is likely to pay" but "which intervention is likely to make a meaningful economic difference." A customer with a high natural recovery probability may warrant no intervention at all.

**Core principle: AI recommendation is not AI authority.** The Reasoning_Layer may analyze and recommend. Deterministic components validate and authorize. External payment or communication actions occur only after Policy_Engine approval.

**Primary business metric:** rupees of revenue recovered.
**Strategic metric:** rupees of *incremental* revenue recovered, evidenced by controlled comparison.

This document covers MVP Phase 1 scope only. Deferred capabilities are listed in "Scope Boundaries" so that exclusions are explicit rather than silent.

### Evidence Discipline Used In This Document

Requirements that rest on unverified premises carry an inline tag:

- **[FACT]** — verifiable from the project brief, from established engineering practice, or from data already in hand.
- **[ASSUMPTION]** — a working premise chosen to make the requirement testable; must be confirmed or replaced during design.
- **[INFERENCE]** — a conclusion derived from stated premises rather than direct evidence.
- **[EVIDENCE INSUFFICIENT]** — the value, capability, or external behavior is unknown at requirements time and must be verified before design closes. No invented API names, endpoint shapes, regulatory claims, or industry recovery-rate figures appear in this document.

Payment provider capabilities are referenced generically as the Payment_Provider throughout. Specific API surfaces, webhook event names, payload shapes, signature schemes, and recovery-action availability are tagged **[EVIDENCE INSUFFICIENT]** and deferred to design-phase verification against live provider documentation.

### Positioning Note

Revora's proposed differentiation is economic decision quality — selecting actions by expected net incremental value and proving that value experimentally. Intelligent retry, dunning sequencing, and communication automation already exist in the market; Revora claims no uniqueness in action automation. The gap under investigation is whether incremental-value optimization produces more recovered revenue than a baseline recovery workflow at equal or lower operational and customer cost. **[INFERENCE]** — competitor internals were not audited.

## Glossary

### Systems and Components

- **Revora**: The complete system described by this document, deployed as a modular monolith.
- **Event_Ingestion**: The component that receives, authenticates, deduplicates, and persists inbound Payment_Events.
- **Detection_Engine**: The deterministic component that classifies persisted Payment_Events as revenue at risk and opens Recovery_Cases.
- **Recovery_Case_Manager**: The component that owns Recovery_Case state and enforces legal state transitions and lifecycle bounds.
- **Diagnosis_Engine**: The component that determines the Risk_Cause of a Recovery_Case.
- **Baseline_Model**: The component that estimates baseline_recovery_probability.
- **Intervention_Simulator**: The component that estimates outcome and cost figures for each Candidate_Action.
- **Value_Optimizer**: The component that computes net_recovery_value per Candidate_Action and produces a Recommendation.
- **Policy_Engine**: The deterministic component holding final authority over whether any Candidate_Action may execute.
- **Execution_Engine**: The component that performs approved external actions against the Payment_Provider or the Communication_Provider.
- **Outcome_Monitor**: The component that observes payment state changes and resolves Recovery_Case outcomes.
- **Experiment_Engine**: The component that assigns Recovery_Cases to Control_Group or Treatment_Group and computes experiment results.
- **Audit_Log**: The append-only store of Audit_Records.
- **Metrics_Engine**: The component that computes recovery metrics from Recovery_Cases, Audit_Records, and verified payment state.
- **Recovery_Memory**: The store of historical Recovery_Case features and outcomes used by Baseline_Model and Intervention_Simulator.
- **Merchant_Dashboard**: The web interface presenting Recovery_Cases, decisions, policy outcomes, and metrics to the Merchant.
- **Reasoning_Layer**: The large-language-model-backed component used for ambiguous interpretation, explanation, and message drafting.
- **Payment_Provider**: The external payment platform that emits payment events and exposes payment state and recovery actions (Razorpay for the MVP). **[EVIDENCE INSUFFICIENT]** on specific capabilities.
- **Communication_Provider**: The external service used to deliver a Customer_Message. **[EVIDENCE INSUFFICIENT]** on selection.

### Domain Terms

- **Merchant**: The business that owns the at-risk revenue and operates Revora.
- **Merchant_User**: An authenticated human operating the Merchant_Dashboard on behalf of one Merchant.
- **Payment_Event**: A structured inbound record describing a payment or checkout state change.
- **Recovery_Case**: The unit of work tracking one at-risk payment from detection to a Terminal_State.
- **Risk_Cause**: The classified reason revenue is at risk, drawn from the enumeration: INSUFFICIENT_FUNDS, EXPIRED_PAYMENT_METHOD, BANK_OR_NETWORK_FAILURE, TECHNICAL_ISSUE, ABANDONMENT, CUSTOMER_ACTION_REQUIRED, FRAUD_OR_RISK_SIGNAL, UNKNOWN.
- **Candidate_Action**: One member of the allowed action set: DO_NOTHING, WAIT, RETRY, DELAYED_RETRY, PAYMENT_LINK, CUSTOMER_MESSAGE, PAYMENT_METHOD_UPDATE, PROMISE_TO_PAY_FOLLOW_UP, HUMAN_ESCALATION.
- **payment_amount**: The at-risk amount A of a Recovery_Case, in minor currency units.
- **baseline_recovery_probability**: The estimated probability that the payment recovers with no Revora intervention.
- **intervention_recovery_probability**: The estimated probability that the payment recovers when a given Candidate_Action executes.
- **incremental_probability**: intervention_recovery_probability minus baseline_recovery_probability.
- **expected_incremental_revenue**: payment_amount multiplied by incremental_probability.
- **action_cost**: The direct monetary cost of executing a Candidate_Action.
- **risk_cost**: The monetized expected cost of risk exposure created by a Candidate_Action.
- **customer_cost**: The monetized expected cost of customer friction and operational handling created by a Candidate_Action.
- **net_recovery_value**: expected_incremental_revenue minus action_cost minus risk_cost minus customer_cost.
- **Recommendation**: The Value_Optimizer output naming one selected Candidate_Action, the rejected alternatives, and the value figures behind the selection.
- **Policy_Decision**: The Policy_Engine verdict for a Recommendation, one of APPROVED, BLOCKED, DEFERRED, ESCALATE.
- **Idempotency_Key**: The deterministic identifier that makes one logical external action execute at most once.
- **Recovery_Window**: The bounded time interval during which Revora may act on a Recovery_Case.
- **Cooldown**: The minimum interval between two consecutive outbound actions on one Recovery_Case.
- **Terminal_State**: One of RECOVERED, STOPPED, BLOCKED, EXPIRED, ESCALATED, FAILED.
- **Natural_Recovery**: A recovery that completed with no Revora intervention on that Recovery_Case.
- **Observed_Recovery**: A recovery that completed after a Revora intervention, with causality not established.
- **Attributed_Recovery**: A recovery increment supported by controlled Control_Group versus Treatment_Group comparison.
- **Control_Group**: Recovery_Cases handled by the Baseline_Workflow.
- **Treatment_Group**: Recovery_Cases handled by Revora decisioning.
- **Baseline_Workflow**: The Merchant's pre-Revora recovery behavior, reproduced deterministically for comparison.
- **Synthetic_Dataset**: Generated data used where Merchant production data is unavailable.

### Configurable Bounds

Every bound below is configuration, not a hard-coded constant. Default values are **[ASSUMPTION]** placeholders chosen to make requirements testable; real-world calibration is **[EVIDENCE INSUFFICIENT]** until Merchant data exists.

| Bound | Default | Purpose |
| --- | --- | --- |
| MAX_RECOVERY_ATTEMPTS | 3 | Upper limit on outbound actions per Recovery_Case |
| MAX_CUSTOMER_MESSAGES | 2 | Upper limit on customer-visible communications per Recovery_Case |
| RECOVERY_WINDOW_DURATION | 168 hours | Maximum Recovery_Case lifetime before EXPIRED |
| COOLDOWN_INTERVAL | 24 hours | Minimum gap between outbound actions |
| MIN_NET_VALUE_THRESHOLD | 5000 minor units | Minimum net_recovery_value required to justify action |
| MIN_INCREMENTAL_PROBABILITY | 0.05 | Minimum incremental_probability required to justify action |
| MAX_COST_TO_VALUE_RATIO | 0.30 | Maximum permitted (action_cost + risk_cost + customer_cost) divided by expected_incremental_revenue |
| HIGH_BASELINE_THRESHOLD | 0.80 | baseline_recovery_probability above which DO_NOTHING is preferred |
| REASONING_TIMEOUT | 10 seconds | Maximum wait for a Reasoning_Layer response |
| OUTCOME_WAIT_TIMEOUT | 72 hours | Maximum wait for an outcome after an executed action |
| INGEST_ACK_TIMEOUT | 3000 ms | Maximum Event_Ingestion acknowledgement latency |

## Requirements

### Requirement 1: Payment Event Ingestion and Failed Payment Detection

**User Story:** As a Merchant, I want Revora to reliably receive payment events from the Payment_Provider and open a recovery case for each genuinely at-risk payment, so that no recoverable revenue is missed and no payment is tracked twice.

MVP detection priority is failed payment recovery. Checkout abandonment, missed promise-to-pay, and payment-window expiry are modelled in the event schema but are deferred as detection triggers (see Scope Boundaries). **[FACT]** from project scope. Provider webhook event names, signature algorithm, and payload fields are **[EVIDENCE INSUFFICIENT]** and must be verified in design.

#### Acceptance Criteria

1. WHEN Event_Ingestion receives an inbound Payment_Event, THE Event_Ingestion SHALL verify the Payment_Provider signature before interpreting the payload contents.
2. IF Payment_Provider signature verification fails, THEN THE Event_Ingestion SHALL reject the Payment_Event, respond with HTTP status 401, and write a SIGNATURE_REJECTED Audit_Record.
3. WHEN Event_Ingestion accepts a signature-verified Payment_Event, THE Event_Ingestion SHALL persist the raw payload together with the provider_event_id before any downstream component processes the Payment_Event.
4. WHEN Event_Ingestion receives a Payment_Event whose provider_event_id matches an already persisted provider_event_id, THE Event_Ingestion SHALL discard the duplicate, respond with HTTP status 200, and write a DUPLICATE_EVENT_DISCARDED Audit_Record.
5. WHEN Event_Ingestion has persisted a Payment_Event, THE Event_Ingestion SHALL respond to the Payment_Provider within INGEST_ACK_TIMEOUT.
6. IF a Payment_Event fails schema validation, THEN THE Event_Ingestion SHALL store the payload in the quarantine store, respond with HTTP status 202, and write a MALFORMED_EVENT Audit_Record.
7. WHEN Event_Ingestion persists a Payment_Event carrying a provider timestamp earlier than the newest already-processed provider timestamp for the same payment identifier, THE Recovery_Case_Manager SHALL apply the Payment_Event only where the Payment_Event produces a legal forward state transition, and SHALL write an OUT_OF_ORDER_EVENT Audit_Record.
8. WHEN Detection_Engine evaluates a persisted failed-payment Payment_Event, THE Detection_Engine SHALL classify revenue at risk using deterministic rules over structured payment fields.
9. WHEN Detection_Engine classifies a Payment_Event as revenue at risk and no active Recovery_Case exists for the payment identifier, THE Detection_Engine SHALL create exactly one Recovery_Case holding payment_amount, currency, customer identifier, provider payment identifier, and detection timestamp.
10. WHERE an active Recovery_Case already exists for the payment identifier, THE Detection_Engine SHALL attach the new Payment_Event to that existing Recovery_Case rather than creating a second Recovery_Case.
11. IF a Payment_Event reports a payment state of paid or captured for a payment identifier with an active Recovery_Case, THEN THE Recovery_Case_Manager SHALL transition that Recovery_Case toward outcome resolution before any further action evaluation occurs.
12. THE Detection_Engine SHALL reach a detection verdict without invoking the Reasoning_Layer.

### Requirement 2: Recovery Case Lifecycle, State Legality, and Boundedness

**User Story:** As a Merchant, I want every recovery case to follow one legal, bounded lifecycle that always terminates, so that no customer is pursued indefinitely and every case has an explainable end state.

Defined lifecycle: NEW → DETECTED → DIAGNOSED → DECISION_PENDING → POLICY_CHECK → ACTION_SCHEDULED → EXECUTING → WAITING_FOR_OUTCOME → RECOVERED, with Terminal_State alternatives STOPPED, BLOCKED, EXPIRED, ESCALATED, FAILED. **[FACT]** from project scope.

#### Acceptance Criteria

1. THE Recovery_Case_Manager SHALL persist for each Recovery_Case a current state drawn from the defined lifecycle enumeration.
2. WHEN a component requests a Recovery_Case state transition, THE Recovery_Case_Manager SHALL apply the transition only where the transition appears in the declared legal transition table, and SHALL record the prior state, the new state, and the transition reason.
3. IF a requested Recovery_Case state transition is absent from the legal transition table, THEN THE Recovery_Case_Manager SHALL reject the request, leave the current state unchanged, and write an ILLEGAL_TRANSITION Audit_Record.
4. WHEN a Recovery_Case enters a Terminal_State, THE Recovery_Case_Manager SHALL reject every subsequent transition request for that Recovery_Case except reconciliation of verified payment state.
5. THE Recovery_Case_Manager SHALL assign every Recovery_Case a Recovery_Window whose duration equals RECOVERY_WINDOW_DURATION measured from the detection timestamp.
6. WHEN the Recovery_Window of a Recovery_Case elapses while that Recovery_Case holds a non-terminal state, THE Recovery_Case_Manager SHALL transition the Recovery_Case to EXPIRED and record the unresolved payment_amount.
7. THE Recovery_Case_Manager SHALL maintain per Recovery_Case an executed-action counter, a customer-message counter, and the timestamp of the most recent outbound action.
8. WHEN the executed-action counter of a Recovery_Case reaches MAX_RECOVERY_ATTEMPTS, THE Recovery_Case_Manager SHALL transition the Recovery_Case to STOPPED or ESCALATED according to the configured escalation rule.
9. WHILE a Recovery_Case holds the state WAITING_FOR_OUTCOME, THE Recovery_Case_Manager SHALL reject requests to schedule an additional action for that Recovery_Case.
10. WHEN OUTCOME_WAIT_TIMEOUT elapses for a Recovery_Case in WAITING_FOR_OUTCOME, THE Outcome_Monitor SHALL query authoritative payment state and drive the Recovery_Case to a further decision cycle or to a Terminal_State.
11. THE Recovery_Case_Manager SHALL bound every Recovery_Case by MAX_RECOVERY_ATTEMPTS, MAX_CUSTOMER_MESSAGES, RECOVERY_WINDOW_DURATION, COOLDOWN_INTERVAL, and a defined escalation condition.
12. THE Recovery_Case_Manager SHALL cause every Recovery_Case to reach a Terminal_State within RECOVERY_WINDOW_DURATION plus OUTCOME_WAIT_TIMEOUT of the detection timestamp.

### Requirement 3: Risk Cause Diagnosis

**User Story:** As a Merchant, I want Revora to determine why a payment is at risk using structured payment data first, so that diagnosis is cheap, repeatable, and only uses AI where genuine ambiguity exists.

#### Acceptance Criteria

1. WHEN a Recovery_Case enters DETECTED, THE Diagnosis_Engine SHALL attempt Risk_Cause classification using deterministic mapping from Payment_Provider failure codes and structured payment fields.
2. WHEN deterministic mapping yields exactly one Risk_Cause, THE Diagnosis_Engine SHALL record that Risk_Cause, a confidence value of 1.0, the mapping rule identifier as evidence, and the method value DETERMINISTIC.
3. WHERE deterministic mapping yields no Risk_Cause, conflicting Risk_Causes, or requires interpretation of unstructured context, THE Diagnosis_Engine SHALL request a Risk_Cause hypothesis from the Reasoning_Layer.
4. THE Diagnosis_Engine SHALL emit a Diagnosis record containing the fields cause, confidence, evidence, and method, where cause is a member of the Risk_Cause enumeration and confidence is a value between 0.0 and 1.0 inclusive.
5. IF the Reasoning_Layer returns a cause outside the Risk_Cause enumeration, THEN THE Diagnosis_Engine SHALL record the Risk_Cause UNKNOWN with the method value REJECTED_AI_OUTPUT and retain the rejected payload as evidence.
6. WHEN the Diagnosis_Engine records a Diagnosis with the Risk_Cause FRAUD_OR_RISK_SIGNAL, THE Recovery_Case_Manager SHALL route the Recovery_Case to policy evaluation with automation suppressed pending Policy_Engine verdict.
7. WHEN the Diagnosis_Engine records a Diagnosis, THE Recovery_Case_Manager SHALL transition the Recovery_Case to DIAGNOSED and write a Diagnosis Audit_Record carrying cause, confidence, evidence, and method.
8. WHERE the recorded confidence falls below the configured diagnosis confidence floor, THE Value_Optimizer SHALL treat the Risk_Cause as UNKNOWN when constructing the Candidate_Action set.

### Requirement 4: Reasoning Layer Output Treated as Untrusted Input

**User Story:** As a Merchant, I want every AI output validated before use and the system to keep working when AI is slow, broken, or unavailable, so that model behavior can never move money or contact a customer on its own.

#### Acceptance Criteria

1. WHEN the Reasoning_Layer returns a response, THE receiving component SHALL validate the response against the declared output schema before using any field of the response.
2. IF a Reasoning_Layer response fails schema validation, THEN THE receiving component SHALL discard the response, write an AI_OUTPUT_REJECTED Audit_Record containing the raw response, and continue using the deterministic fallback path.
3. IF the Reasoning_Layer produces no response within REASONING_TIMEOUT, THEN THE receiving component SHALL abandon the request and continue using the deterministic fallback path.
4. WHILE the Reasoning_Layer is unavailable, THE Revora system SHALL continue to detect Payment_Events, open Recovery_Cases, apply deterministic diagnosis, evaluate policy, and record metrics.
5. THE Policy_Engine SHALL derive every Policy_Decision from deterministic rules and persisted state, using no field produced by the Reasoning_Layer as an authorization input.
6. THE Execution_Engine SHALL accept an execution request only where the request carries a Policy_Decision of APPROVED issued by the Policy_Engine.
7. WHERE the Reasoning_Layer drafts customer-visible message content, THE Execution_Engine SHALL send that content only after the content passes the configured content validation rules covering permitted length, permitted placeholders, absence of payment amounts contradicting the Recovery_Case, and absence of links other than the Policy_Engine-approved link.
8. THE Audit_Log SHALL record for every Reasoning_Layer invocation the prompt identifier, model identifier, latency, validation verdict, and whether the output influenced the final Recommendation.

### Requirement 5: Baseline Recovery Probability Estimation

**User Story:** As a Merchant, I want an explicit estimate of what happens if Revora does nothing, so that intervention value is measured against no-intervention reality rather than against zero.

Baseline calibration against real Merchant outcomes is **[EVIDENCE INSUFFICIENT]** at requirements time. Historical Merchant data reflects the Merchant's past intervention behavior, so naive training on that data measures intervened outcomes rather than natural outcomes **[INFERENCE]**; Requirement 5 criteria 6 through 9 exist to keep that limitation visible and correctable.

#### Acceptance Criteria

1. WHEN a Recovery_Case enters DIAGNOSED, THE Baseline_Model SHALL produce a baseline_recovery_probability between 0.0 and 1.0 inclusive representing recovery with no Revora intervention.
2. THE Baseline_Model SHALL record alongside every baseline_recovery_probability the feature values used, the model version identifier, and the estimation method.
3. WHERE Recovery_Memory holds fewer observations than the configured minimum sample size for the feature segment of a Recovery_Case, THE Baseline_Model SHALL produce a segment-level prior estimate and mark the estimate with the method value PRIOR_FALLBACK.
4. WHERE the baseline data source is a Synthetic_Dataset, THE Baseline_Model SHALL mark every produced baseline_recovery_probability with the data provenance value SYNTHETIC.
5. THE Baseline_Model SHALL publish a calibration report comparing predicted baseline_recovery_probability bands against observed no-intervention recovery rates from Control_Group Recovery_Cases.
6. THE Baseline_Model SHALL derive its training labels from Recovery_Cases where no Revora intervention executed, and SHALL record the count of excluded intervened Recovery_Cases.
7. WHERE the count of no-intervention observations for a feature segment falls below the configured minimum, THE Baseline_Model SHALL report the segment as an intervention-bias risk segment in the calibration report.
8. IF the calibration report shows an absolute deviation between a predicted probability band and the observed Control_Group recovery rate exceeding the configured calibration tolerance, THEN THE Metrics_Engine SHALL flag every value decision derived from that band as CALIBRATION_SUSPECT.
9. THE Baseline_Model SHALL treat every produced probability as an estimate carrying stated uncertainty, and SHALL record a confidence interval or an explicit statement that uncertainty quantification is unavailable for the estimation method used.

### Requirement 6: Candidate Intervention Simulation

**User Story:** As a Merchant, I want Revora to evaluate a full set of candidate actions including doing nothing, so that the comparison is economic rather than a search for a reason to act.

#### Acceptance Criteria

1. WHEN a Recovery_Case holds a recorded baseline_recovery_probability, THE Intervention_Simulator SHALL construct a Candidate_Action set that always includes DO_NOTHING and WAIT.
2. THE Intervention_Simulator SHALL restrict the Candidate_Action set to actions permitted for the recorded Risk_Cause according to the declared cause-to-action eligibility table.
3. FOR every Candidate_Action in the constructed set, THE Intervention_Simulator SHALL estimate intervention_recovery_probability, action_cost, risk_cost, and customer_cost.
4. THE Intervention_Simulator SHALL assign the Candidate_Action DO_NOTHING an intervention_recovery_probability equal to baseline_recovery_probability, an action_cost of zero, and a customer_cost of zero.
5. THE Intervention_Simulator SHALL record for every estimated figure the estimation method, the model version identifier, and the data provenance value REAL or SYNTHETIC.
6. WHERE Recovery_Memory holds no observation of a Candidate_Action for the feature segment of a Recovery_Case, THE Intervention_Simulator SHALL mark that Candidate_Action estimate with the method value UNCALIBRATED.
7. THE Intervention_Simulator SHALL express action_cost, risk_cost, and customer_cost in the same minor currency units as payment_amount.
8. THE Intervention_Simulator SHALL produce estimates for the Candidate_Action set without issuing any request to the Payment_Provider or the Communication_Provider.
9. WHERE a Candidate_Action requires a Payment_Provider capability that design-phase verification has not confirmed, THE Intervention_Simulator SHALL mark that Candidate_Action as UNAVAILABLE and exclude the Candidate_Action from selection. **[EVIDENCE INSUFFICIENT]** on provider capability inventory.

### Requirement 7: Incremental Value Optimization and Action Selection

**User Story:** As a Merchant, I want Revora to select the action with the highest positive net incremental value and to choose no action when nothing is economically justified, so that I stop paying for interventions that do not change outcomes.

Worked reference from the project brief, payment_amount ₹20,000: DO_NOTHING 20% baseline, ₹0 incremental; RETRY 35%, +15%, ₹3,000 expected incremental; PAYMENT_LINK 60%, +40%, ₹8,000 expected incremental; a voice channel at 62%, +42%, ₹8,400 expected incremental carries higher execution cost and may lose on net value. **[FACT]** as an illustrative arithmetic example; the probability figures themselves are **[EVIDENCE INSUFFICIENT]** as real-world estimates.

#### Acceptance Criteria

1. FOR every Candidate_Action, THE Value_Optimizer SHALL compute incremental_probability as intervention_recovery_probability minus baseline_recovery_probability.
2. FOR every Candidate_Action, THE Value_Optimizer SHALL compute expected_incremental_revenue as payment_amount multiplied by incremental_probability.
3. FOR every Candidate_Action, THE Value_Optimizer SHALL compute net_recovery_value as expected_incremental_revenue minus action_cost minus risk_cost minus customer_cost.
4. THE Value_Optimizer SHALL select the Candidate_Action holding the greatest net_recovery_value among Candidate_Actions whose net_recovery_value is greater than or equal to MIN_NET_VALUE_THRESHOLD and whose incremental_probability is greater than or equal to MIN_INCREMENTAL_PROBABILITY.
5. IF no Candidate_Action satisfies both MIN_NET_VALUE_THRESHOLD and MIN_INCREMENTAL_PROBABILITY, THEN THE Value_Optimizer SHALL select DO_NOTHING or WAIT and SHALL record the selection reason NO_POSITIVE_VALUE.
6. WHERE baseline_recovery_probability is greater than or equal to HIGH_BASELINE_THRESHOLD, THE Value_Optimizer SHALL select DO_NOTHING or WAIT unless a Candidate_Action satisfies both MIN_NET_VALUE_THRESHOLD and MIN_INCREMENTAL_PROBABILITY.
7. IF the sum of action_cost, risk_cost, and customer_cost for a Candidate_Action divided by expected_incremental_revenue for that Candidate_Action exceeds MAX_COST_TO_VALUE_RATIO, THEN THE Value_Optimizer SHALL exclude that Candidate_Action from selection and SHALL record the exclusion reason COST_RATIO_EXCEEDED.
8. WHERE the Candidate_Action holding the greatest intervention_recovery_probability differs from the selected Candidate_Action, THE Value_Optimizer SHALL record both Candidate_Actions in the Recommendation together with the net_recovery_value figures that produced the difference.
9. THE Value_Optimizer SHALL record in every Recommendation the selected Candidate_Action, every rejected Candidate_Action, the four value figures per Candidate_Action, the selection reason, and every exclusion reason.
10. THE Value_Optimizer SHALL produce a Recommendation using arithmetic over recorded numeric estimates, taking no Recovery_Case ranking from Reasoning_Layer free text.
11. WHERE the Recommendation includes Reasoning_Layer explanation text, THE Value_Optimizer SHALL store that text as explanation only and SHALL keep the selection derived from the numeric comparison.
12. THE Value_Optimizer SHALL compute value figures in integer minor currency units, applying a declared rounding rule that keeps the sum of reported per-case figures equal to the reported aggregate figure.

### Requirement 8: Policy Engine as Final Authority

**User Story:** As a Merchant, I want a deterministic policy layer to hold the final say on every action, so that an AI recommendation can never contact a paid customer, an opted-out customer, or a flagged customer.

#### Acceptance Criteria

1. WHEN the Value_Optimizer produces a Recommendation, THE Policy_Engine SHALL evaluate the Recommendation and issue a Policy_Decision of APPROVED, BLOCKED, DEFERRED, or ESCALATE before any execution occurs.
2. THE Policy_Engine SHALL evaluate for every Recommendation the checks: verified payment already paid, customer opt-out status, required consent presence, retry limit consumption, communication Cooldown elapsed, Recovery_Window validity, fraud or risk flag presence, duplicate action presence, escalation condition presence, and action eligibility for the Recovery_Case.
3. IF the verified payment state of a Recovery_Case is paid, THEN THE Policy_Engine SHALL issue BLOCKED and THE Recovery_Case_Manager SHALL transition the Recovery_Case toward RECOVERED.
4. IF the customer of a Recovery_Case holds opt-out status, THEN THE Policy_Engine SHALL issue BLOCKED for every customer-visible Candidate_Action and THE Recovery_Case_Manager SHALL transition the Recovery_Case to STOPPED.
5. IF a Recovery_Case carries a fraud or high-risk flag, THEN THE Policy_Engine SHALL issue ESCALATE, suppress automated execution, and THE Recovery_Case_Manager SHALL transition the Recovery_Case to ESCALATED.
6. IF the executed-action counter of a Recovery_Case equals or exceeds MAX_RECOVERY_ATTEMPTS, THEN THE Policy_Engine SHALL issue BLOCKED with the reason MAX_ATTEMPTS_REACHED.
7. IF the customer-message counter of a Recovery_Case equals or exceeds MAX_CUSTOMER_MESSAGES, THEN THE Policy_Engine SHALL issue BLOCKED for every customer-visible Candidate_Action with the reason MAX_MESSAGES_REACHED.
8. IF the interval since the most recent outbound action of a Recovery_Case is shorter than COOLDOWN_INTERVAL, THEN THE Policy_Engine SHALL issue DEFERRED together with the earliest permitted execution timestamp.
9. IF the Recovery_Window of a Recovery_Case has elapsed, THEN THE Policy_Engine SHALL issue BLOCKED with the reason WINDOW_EXPIRED.
10. IF an action carrying the same Idempotency_Key as the Recommendation already holds a recorded execution attempt, THEN THE Policy_Engine SHALL issue BLOCKED with the reason DUPLICATE_ACTION.
11. IF a human owner is assigned to a Recovery_Case, THEN THE Policy_Engine SHALL issue BLOCKED for automated execution with the reason HUMAN_OWNERSHIP.
12. WHEN the Policy_Engine issues any Policy_Decision, THE Policy_Engine SHALL write an Audit_Record containing the decision, every evaluated check, every check result, the rule set version identifier, and the Recovery_Case state observed at evaluation time.
13. WHERE the selected Candidate_Action is DO_NOTHING or WAIT, THE Policy_Engine SHALL record a Policy_Decision without initiating any external request.
14. THE Policy_Engine SHALL produce the same Policy_Decision for identical Recovery_Case state and identical rule set version on repeated evaluation.

### Requirement 9: Bounded, Idempotent Execution of an Approved Recovery Action

**User Story:** As a Merchant, I want approved actions executed exactly once with confirmed results, so that a customer is never charged twice, messaged twice, or told a link exists when the provider call failed.

MVP execution scope is one real Payment_Provider recovery action. Payment link creation is the intended candidate, conditional on design-phase verification of provider capability. **[EVIDENCE INSUFFICIENT]** on the exact provider API and its constraints; the requirement is written against a generic capability so that a verified alternative can substitute without rewriting the specification.

#### Acceptance Criteria

1. WHEN the Execution_Engine receives an execution request, THE Execution_Engine SHALL reload the authoritative Recovery_Case state before performing any external call.
2. WHEN the Execution_Engine holds reloaded Recovery_Case state, THE Execution_Engine SHALL re-request Policy_Engine evaluation and SHALL proceed only where the Policy_Decision is APPROVED.
3. THE Execution_Engine SHALL derive an Idempotency_Key deterministically from the Recovery_Case identifier, the Candidate_Action type, and the attempt ordinal.
4. WHEN the Execution_Engine begins an external call, THE Execution_Engine SHALL first persist an execution-intent record carrying the Idempotency_Key and the state ATTEMPTED.
5. WHERE an execution-intent record already exists for an Idempotency_Key, THE Execution_Engine SHALL return the recorded result and SHALL issue no additional external call.
6. WHEN an external call returns a success confirmation, THE Execution_Engine SHALL persist the provider response identifier and set the execution-intent state to CONFIRMED before reporting success.
7. THE Execution_Engine SHALL report an action as successful only after a Payment_Provider or Communication_Provider confirmation is persisted.
8. IF an external call returns an error response, THEN THE Execution_Engine SHALL set the execution-intent state to FAILED, write a failure Audit_Record containing the provider error, and refrain from retrying within the same execution attempt.
9. IF an external call produces no response within the configured provider timeout, THEN THE Execution_Engine SHALL set the execution-intent state to UNCERTAIN and THE Outcome_Monitor SHALL reconcile the action result against authoritative provider state before any further action on the Recovery_Case.
10. WHEN the Execution_Engine records a CONFIRMED execution, THE Recovery_Case_Manager SHALL increment the executed-action counter, update the most-recent-action timestamp, and transition the Recovery_Case to WAITING_FOR_OUTCOME.
11. WHEN the Execution_Engine records a CONFIRMED customer-visible action, THE Recovery_Case_Manager SHALL increment the customer-message counter.
12. THE Execution_Engine SHALL acquire an exclusive lock keyed by Recovery_Case identifier for the duration of an execution attempt.
13. IF the exclusive lock for a Recovery_Case is unavailable, THEN THE Execution_Engine SHALL abandon the execution attempt and write a CONCURRENT_EXECUTION_PREVENTED Audit_Record.
14. IF the Communication_Provider is unavailable, THEN THE Execution_Engine SHALL set the execution-intent state to FAILED and THE Recovery_Case_Manager SHALL return the Recovery_Case to DECISION_PENDING within the remaining Recovery_Window and attempt bounds.

### Requirement 10: Outcome Observation and Verification

**User Story:** As a Merchant, I want recovery declared only against verified payment state, so that reported recoveries match money that actually arrived.

#### Acceptance Criteria

1. THE Outcome_Monitor SHALL treat Payment_Provider verified payment state as the authoritative source of payment truth for every Recovery_Case.
2. WHEN the Outcome_Monitor receives an event indicating payment success for a Recovery_Case, THE Outcome_Monitor SHALL confirm the payment state through an authoritative provider state read before declaring RECOVERED.
3. WHEN a Recovery_Case payment is confirmed paid, THE Recovery_Case_Manager SHALL transition the Recovery_Case to RECOVERED and record the recovered amount, the recovery timestamp, and the elapsed time from detection.
4. WHEN a Recovery_Case payment is confirmed paid while an action is scheduled or queued for that Recovery_Case, THE Execution_Engine SHALL cancel the scheduled action and write an ACTION_CANCELLED_PAYMENT_RECEIVED Audit_Record.
5. WHERE a scheduled action for a paid Recovery_Case has already reached the external provider, THE Outcome_Monitor SHALL record the action as POST_PAYMENT_ACTION and THE Metrics_Engine SHALL count that action in the unnecessary-action metric.
6. WHILE the payment state of a Recovery_Case is unresolved between conflicting sources, THE Outcome_Monitor SHALL hold the Recovery_Case in WAITING_FOR_OUTCOME and SHALL schedule reconciliation against authoritative provider state.
7. IF authoritative provider state cannot be read within the configured reconciliation attempt bound, THEN THE Recovery_Case_Manager SHALL transition the Recovery_Case to ESCALATED with the reason PAYMENT_STATE_UNVERIFIABLE.
8. WHEN a Recovery_Case reaches RECOVERED with zero executed Revora actions, THE Metrics_Engine SHALL classify the outcome as Natural_Recovery.
9. WHEN a Recovery_Case reaches RECOVERED with at least one executed Revora action, THE Metrics_Engine SHALL classify the outcome as Observed_Recovery.
10. WHEN a Recovery_Case reaches a Terminal_State other than RECOVERED, THE Metrics_Engine SHALL record the payment_amount as unresolved revenue together with the Terminal_State reason.
11. WHEN the Outcome_Monitor receives a duplicate payment-success event for an already RECOVERED Recovery_Case, THE Metrics_Engine SHALL count the recovered amount once.

### Requirement 11: Audit Trail and Decision Explainability

**User Story:** As a Merchant, I want every decision traceable to its evidence, alternatives, and policy verdict, so that I can explain to a customer or an auditor why Revora did what it did.

#### Acceptance Criteria

1. WHEN any component changes Recovery_Case state, records a Diagnosis, produces a Recommendation, issues a Policy_Decision, or executes an action, THE Audit_Log SHALL persist an Audit_Record for that occurrence.
2. THE Audit_Log SHALL store in every Audit_Record the fields case_id, event_id, timestamp, actor, previous_state, new_state, diagnosis, evidence, decision, confidence, policy_result, action, action_result, idempotency_key, and correlation_id.
3. THE Audit_Log SHALL accept insert operations only, rejecting update and delete operations on persisted Audit_Records.
4. THE Audit_Log SHALL assign every Audit_Record a strictly increasing sequence number per Recovery_Case.
5. WHEN a Merchant requests the history of a Recovery_Case, THE Merchant_Dashboard SHALL present the ordered Audit_Records answering what happened, why, on what evidence, which alternatives were considered, which policy rules allowed or blocked the action, which action executed, whether payment recovered, and whether the recovery is classified Natural_Recovery, Observed_Recovery, or Attributed_Recovery.
6. THE Audit_Log SHALL record for every Recommendation the rejected Candidate_Actions and their net_recovery_value figures.
7. THE Audit_Log SHALL propagate one correlation_id across every Audit_Record produced from a single inbound Payment_Event.
8. WHERE an Audit_Record contains customer contact data or payment instrument data, THE Audit_Log SHALL store the masked representation of that data.

### Requirement 12: Recovery Metrics and Outcome Class Separation

**User Story:** As a Merchant, I want metrics that separate money that arrived on its own from money that arrived after intervention from money proven incremental, so that I am not sold activity as outcome.

#### Acceptance Criteria

1. THE Metrics_Engine SHALL compute revenue_at_risk as the sum of payment_amount across Recovery_Cases opened within the reporting period.
2. THE Metrics_Engine SHALL compute observed_recovered_revenue as the sum of confirmed recovered amounts across Recovery_Cases classified Observed_Recovery.
3. THE Metrics_Engine SHALL compute natural_recovered_revenue as the sum of confirmed recovered amounts across Recovery_Cases classified Natural_Recovery.
4. THE Metrics_Engine SHALL compute incremental_recovered_revenue only from Control_Group versus Treatment_Group comparison produced by the Experiment_Engine.
5. THE Metrics_Engine SHALL compute recovery_rate, intervention_rate, action_success_rate, average_time_to_recovery, escalation_rate, blocked_case_count, unnecessary_action_count, and unresolved_revenue for the reporting period.
6. THE Metrics_Engine SHALL compute total_recovery_cost as the sum of realized action_cost across executed actions, and net_recovered_revenue as observed_recovered_revenue minus total_recovery_cost.
7. WHEN the Metrics_Engine reports recovery_rate for a period, THE Metrics_Engine SHALL report net_recovered_revenue for the same period in the same view.
8. IF recovery_rate increases between two consecutive reporting periods while net_recovered_revenue decreases, THEN THE Metrics_Engine SHALL raise a COST_OUTPACING_RECOVERY finding naming the segments contributing the divergence.
9. IF observed_recovered_revenue is positive while the Experiment_Engine reports an incremental lift whose confidence interval contains zero, THEN THE Metrics_Engine SHALL label the reported observed recovery as CAUSALITY_NOT_ESTABLISHED.
10. THE Metrics_Engine SHALL report every metric segmented by Risk_Cause, payment_amount band, selected Candidate_Action, and Policy_Decision outcome.
11. THE Metrics_Engine SHALL label every metric derived from a Synthetic_Dataset with the provenance value SYNTHETIC in every presentation surface.
12. THE Metrics_Engine SHALL derive every reported metric from persisted Recovery_Case records, Audit_Records, and verified payment state.

### Requirement 13: Control Versus Treatment Evaluation and Causality Discipline

**User Story:** As a Merchant, I want a controlled comparison against my baseline workflow, so that I can tell whether Revora created recovered revenue rather than merely being present when payments arrived.

The reference experiment shape from the project brief is 500 Control_Group cases on the Baseline_Workflow against 500 Treatment_Group cases on Revora decisioning. Whether 500 per arm detects the true effect is **[EVIDENCE INSUFFICIENT]**; criterion 4 makes the required sample size a computed output rather than an assumed constant.

#### Acceptance Criteria

1. WHEN a Recovery_Case is created while an experiment is active, THE Experiment_Engine SHALL assign the Recovery_Case to Control_Group or Treatment_Group using a randomization function seeded by the Recovery_Case identifier and the experiment identifier.
2. THE Experiment_Engine SHALL record the group assignment of a Recovery_Case before any Diagnosis or Recommendation for that Recovery_Case is produced.
3. WHILE a Recovery_Case is assigned to Control_Group, THE Recovery_Case_Manager SHALL apply the Baseline_Workflow and SHALL suppress Revora-selected interventions.
4. WHEN a Merchant defines an experiment, THE Experiment_Engine SHALL compute and record the minimum detectable effect, the required sample size per group, the primary metric, and the analysis method before Recovery_Case assignment begins.
5. THE Experiment_Engine SHALL freeze the Baseline_Workflow definition, the policy rule set version, the Baseline_Model version, and the Intervention_Simulator version for the duration of an experiment.
6. WHEN an experiment reaches its recorded required sample size per group, THE Experiment_Engine SHALL report recovery rate, recovered revenue, average time to recovery, intervention rate, cost per recovery, and incremental lift for each group.
7. THE Experiment_Engine SHALL report incremental lift together with an uncertainty interval and the count of Recovery_Cases in each group.
8. THE Experiment_Engine SHALL classify a recovery as Attributed_Recovery only where the recovery belongs to a completed experiment whose reported incremental lift uncertainty interval excludes zero.
9. IF an experiment reports fewer Recovery_Cases per group than the recorded required sample size, THEN THE Experiment_Engine SHALL label the result UNDERPOWERED and SHALL withhold Attributed_Recovery classification.
10. THE Experiment_Engine SHALL report Control_Group and Treatment_Group results for every defined segment, presenting aggregate results together with segment results.
11. THE Experiment_Engine SHALL report results for both the primary metric recorded at experiment definition time and every secondary metric recorded at experiment definition time.
12. WHERE experiment inputs originate from a Synthetic_Dataset, THE Experiment_Engine SHALL label the experiment result SYNTHETIC and SHALL record the generation assumptions, the embedded ground truth, and the generation seed.
13. THE Experiment_Engine SHALL present the question "does Revora decisioning recover more revenue than the Baseline_Workflow within policy limits and without added customer or operational cost" as a reported comparison of net_recovered_revenue, intervention_rate, and customer-message count per group.

### Requirement 14: Merchant Dashboard and Decision Transparency

**User Story:** As a Merchant, I want one interface showing revenue at risk, the reason, the recommendation, the alternatives, the policy verdict, the executed action, and the recovered amount, so that I can act on Revora output without taking the output on trust.

#### Acceptance Criteria

1. THE Merchant_Dashboard SHALL present for a selected reporting period revenue_at_risk, observed_recovered_revenue, natural_recovered_revenue, net_recovered_revenue, unresolved_revenue, recovery_rate, and intervention_rate.
2. THE Merchant_Dashboard SHALL present a Recovery_Case list showing per Recovery_Case the payment_amount, the Risk_Cause, the current state, the selected Candidate_Action, the Policy_Decision, and the outcome classification.
3. WHEN a Merchant_User opens a Recovery_Case detail view, THE Merchant_Dashboard SHALL present the recorded Diagnosis fields cause, confidence, evidence, and method.
4. WHEN a Merchant_User opens a Recovery_Case detail view, THE Merchant_Dashboard SHALL present the selected Candidate_Action, every rejected Candidate_Action, and the values of incremental_probability, expected_incremental_revenue, the summed costs, and net_recovery_value for every Candidate_Action considered.
5. WHEN a Merchant_User opens a Recovery_Case detail view, THE Merchant_Dashboard SHALL present the Policy_Decision, every evaluated policy check, and every check result.
6. WHERE the selected Candidate_Action of a Recovery_Case is DO_NOTHING or WAIT, THE Merchant_Dashboard SHALL present the recorded selection reason together with the value figures that produced the selection.
7. THE Merchant_Dashboard SHALL label every presented recovered amount with the outcome classification Natural_Recovery, Observed_Recovery, or Attributed_Recovery.
8. THE Merchant_Dashboard SHALL present incremental_recovered_revenue together with the experiment identifier, the Recovery_Case count per group, and the reported uncertainty interval.
9. WHERE a presented figure derives from a Synthetic_Dataset, THE Merchant_Dashboard SHALL display the provenance label SYNTHETIC adjacent to that figure.
10. THE Merchant_Dashboard SHALL present unresolved Recovery_Cases grouped by Terminal_State reason together with the summed unresolved payment_amount per group.
11. WHEN a Merchant_User assigns human ownership of a Recovery_Case through the Merchant_Dashboard, THE Recovery_Case_Manager SHALL record the assigned Merchant_User and suppress automated execution for that Recovery_Case.
12. THE Merchant_Dashboard SHALL display recovery figures obtained from the Metrics_Engine, deriving no recovery figure inside the browser client.
13. THE Merchant_Dashboard SHALL conform to WCAG 2.1 Level AA success criteria for keyboard operation, contrast ratio, focus visibility, and programmatic labelling of presented data. **[FACT]** WCAG 2.1 Level AA is the target standard; full conformance validation requires manual testing with assistive technologies and expert accessibility review.

### Requirement 15: Recovery Memory and Bounded Learning

**User Story:** As a Merchant, I want Revora to learn from completed cases through explicit, versioned model promotion, so that estimates improve over time while policy authority stays under human control.

#### Acceptance Criteria

1. WHEN a Recovery_Case reaches a Terminal_State, THE Recovery_Memory SHALL persist the feature values, the Diagnosis, the selected Candidate_Action, the Policy_Decision, the outcome classification, the realized action_cost, and the experiment group assignment.
2. THE Recovery_Memory SHALL record for every stored observation the data provenance value REAL or SYNTHETIC.
3. WHERE the Baseline_Model or the Intervention_Simulator produces an estimate labelled REAL, THE producing component SHALL draw training observations from Recovery_Memory observations carrying the provenance value REAL.
4. THE Recovery_Memory SHALL store the experiment group assignment and the executed-action count of every observation so that Baseline_Model training can exclude intervened observations.
5. THE Revora system SHALL activate a Baseline_Model version or an Intervention_Simulator version only through an explicit version promotion recorded with the promotion timestamp, the prior version identifier, and the approving Merchant_User.
6. THE Revora system SHALL keep the Policy_Engine rule set independent of Recovery_Memory contents, changing policy rules only through a recorded configuration change carrying a new rule set version identifier.
7. WHILE an experiment is active, THE Revora system SHALL retain the model versions frozen for that experiment for every Recovery_Case assigned to that experiment.
8. WHEN a model version promotion is recorded, THE Metrics_Engine SHALL report metrics for Recovery_Cases decided under the promoted version separately from metrics for Recovery_Cases decided under prior versions.
9. THE Recovery_Memory SHALL retain the model version identifiers that produced every stored estimate.

### Requirement 16: Failure Tolerance, Event Canonicalization, and Data Consistency

**User Story:** As a Merchant, I want Revora to fail safely rather than act blindly, so that infrastructure faults produce delayed decisions instead of duplicate charges, duplicate messages, or invented recovery numbers.

#### Acceptance Criteria

1. WHEN the Recovery_Case_Manager applies a Recovery_Case state transition, THE Recovery_Case_Manager SHALL persist the new state and the corresponding Audit_Record within one atomic transaction.
2. IF the persistence store rejects a Recovery_Case state transition, THEN THE Recovery_Case_Manager SHALL retain the prior state and SHALL report the failure to the calling component.
3. IF the persistence store is unavailable when Event_Ingestion receives a Payment_Event, THEN THE Event_Ingestion SHALL respond with HTTP status 503 so that the Payment_Provider redelivery mechanism resends the Payment_Event. **[ASSUMPTION]** the Payment_Provider redelivers on a 5xx response; redelivery behavior and retry schedule are **[EVIDENCE INSUFFICIENT]** pending design-phase verification.
4. WHILE the persistence store is unavailable, THE Revora system SHALL withhold every Payment_Provider request and every Communication_Provider request.
5. WHEN a background worker terminates during an execution attempt, THE Execution_Engine SHALL resolve the recorded execution-intent state for the Idempotency_Key of that attempt before issuing any further external call for the same Recovery_Case.
6. WHEN the Revora system restarts, THE Recovery_Case_Manager SHALL reload every non-terminal Recovery_Case and SHALL re-evaluate Recovery_Window expiry, Cooldown state, and executed-action counters before scheduling further actions.
7. IF two components request a state transition for the same Recovery_Case concurrently, THEN THE Recovery_Case_Manager SHALL apply exactly one transition using a persisted version check and SHALL reject the remaining request with a VERSION_CONFLICT Audit_Record.
8. WHEN Event_Ingestion receives a replay of previously processed Payment_Events, THE Revora system SHALL create no additional Recovery_Case and SHALL issue no additional external action.
9. THE Revora system SHALL evaluate every timing bound against stored UTC timestamps.
10. THE Event_Ingestion SHALL parse an accepted Payment_Event payload into a canonical internal Payment_Event representation and SHALL serialize a canonical internal Payment_Event representation back into a provider-shaped payload.
11. FOR ALL valid Payment_Event payloads, parsing the payload, serializing the result, and parsing the serialized output SHALL produce an equivalent canonical Payment_Event representation (round-trip property).
12. THE Merchant_Dashboard SHALL present quarantined malformed Payment_Events for Merchant_User review together with the recorded validation failure.

### Requirement 17: Security, Access Control, and Customer Data Protection

**User Story:** As a Merchant, I want payment and customer data protected and every actor identified, so that a recovery system holding sensitive data does not become the largest risk in the stack.

Applicable statutory and card-network obligations are **[EVIDENCE INSUFFICIENT]** at requirements time. This document states engineering controls only and makes no regulatory compliance claim.

#### Acceptance Criteria

1. THE Revora system SHALL require successful authentication for every Merchant_Dashboard request and every management endpoint request.
2. THE Revora system SHALL scope every Recovery_Case read operation and write operation to the Merchant identifier associated with the authenticated Merchant_User.
3. IF an authenticated Merchant_User requests a Recovery_Case belonging to a different Merchant, THEN THE Revora system SHALL respond with HTTP status 404 and SHALL write an AUTHORIZATION_DENIED Audit_Record.
4. THE Revora system SHALL read Payment_Provider credentials, webhook signing secrets, Communication_Provider credentials, and Reasoning_Layer credentials from configured secret storage held outside source control.
5. THE Revora system SHALL transmit every request to the Payment_Provider, the Communication_Provider, and the Reasoning_Layer over TLS.
6. THE Revora system SHALL store customer contact identifiers and payment instrument references in masked or tokenized form, treating the Payment_Provider as the authoritative holder of payment instrument data.
7. THE Revora system SHALL write customer contact identifiers, payment instrument references, and authentication secrets to application logs in masked form only.
8. WHERE the Reasoning_Layer receives Recovery_Case context, THE Revora system SHALL send the field set declared in the prompt contract, excluding payment instrument data, authentication secrets, and raw customer contact identifiers.
9. WHEN a Merchant_User performs a state-changing operation through the Merchant_Dashboard, THE Audit_Log SHALL record the Merchant_User identifier in the actor field.
10. WHEN a customer opt-out is recorded, THE Revora system SHALL persist the opt-out status as authoritative for every subsequent Policy_Engine evaluation covering every Recovery_Case of that customer.
11. THE Revora system SHALL apply the configured retention period to stored customer contact data and SHALL record the retention configuration version applied.
12. THE Revora system SHALL rate limit the Event_Ingestion endpoint per source and SHALL write a RATE_LIMIT_APPLIED Audit_Record when the configured rate is exceeded.

## Scope Boundaries

### In MVP Scope

| MVP capability | Covered by |
| --- | --- |
| Failed payment detection | Requirement 1 |
| Payment_Provider webhook integration | Requirement 1, Requirement 16 |
| Recovery_Case state machine | Requirement 2 |
| Diagnosis | Requirement 3, Requirement 4 |
| Baseline recovery probability | Requirement 5 |
| Intervention simulation | Requirement 6 |
| Incremental value calculation | Requirement 7 |
| Policy engine | Requirement 8 |
| One real recovery execution path | Requirement 9 |
| Outcome verification | Requirement 10 |
| Audit trail | Requirement 11 |
| Recovery metrics | Requirement 12 |
| Control versus treatment evaluation | Requirement 13 |
| Merchant dashboard | Requirement 14 |

Requirements 15 through 17 cover the cross-cutting learning, resilience, and security constraints that the MVP capabilities depend on.

### Deferred, Not Silently Included

- Voice agents and multilingual voice interaction.
- Agent swarms, autonomous multi-agent orchestration, and self-directed tool use.
- Kafka, Kubernetes, and a microservice decomposition. Phase 1 is a modular monolith.
- Vector databases and retrieval-augmented generation beyond structured context passing.
- Additional communication channels beyond the single Communication_Provider path.
- Detection triggers other than failed payment: checkout abandonment, missed promise-to-pay, and payment-window expiry are represented in the Payment_Event schema and the Candidate_Action enumeration, and are excluded from Phase 1 detection.
- Automated policy rule learning. Policy changes stay manual and versioned (Requirement 15, criterion 6).
- Automated model retraining without recorded human promotion (Requirement 15, criterion 5).
- Multi-currency valuation. Phase 1 assumes one currency per Merchant. **[ASSUMPTION]**

### Items Requiring Verification Before Design Closes

Each item is **[EVIDENCE INSUFFICIENT]** at requirements time. No API name, endpoint, payload shape, statutory obligation, or industry benchmark has been invented to fill these gaps.

1. Payment_Provider webhook event catalogue, signature scheme, payload fields, delivery guarantees, and redelivery schedule.
2. Payment_Provider recovery action capabilities, including whether payment link creation, payment retry, and payment method update are available under the Merchant account type in use, and their idempotency semantics.
3. Payment_Provider authoritative payment state read capability and its consistency guarantees.
4. Communication_Provider selection, consent model, and delivery confirmation semantics.
5. Real-world baseline recovery rates, intervention recovery rates, action costs, risk costs, and customer costs. Every default in the bounds table is a placeholder.
6. Required sample size per experiment arm for a defensible incremental lift result.
7. Calibration tolerance, diagnosis confidence floor, and minimum segment sample size values.
8. Applicable statutory, contractual, and card-network obligations covering stored customer data and outbound customer contact.

## Appendix A: Candidate Correctness Properties

These properties are stated for design-phase formalization as property-based tests. Each property names the requirements it derives from. Formal property definitions and generators belong to the design document.

**Policy authority**

- P1: For every executed external action, a Policy_Decision of APPROVED exists in the Audit_Log for that Recovery_Case and Idempotency_Key with a timestamp preceding the execution-intent record. No Reasoning_Layer output alone precedes an execution. (Requirements 4, 8, 9)
- P2: For any Recommendation and any Recovery_Case state, replacing the Reasoning_Layer output with arbitrary schema-valid content leaves the Policy_Decision unchanged. (Requirement 4, criterion 5; Requirement 8, criterion 14)

**Idempotency**

- P3: For any sequence of execution requests carrying one Idempotency_Key, the count of external calls issued is at most one, and every request returns the same recorded result. (Requirement 9, criteria 3 through 6)
- P4: For any inbound Payment_Event sequence containing duplicates and reorderings, the count of Recovery_Cases created for one payment identifier is at most one. (Requirement 1, criteria 4, 7, 9, 10; Requirement 16, criterion 8)

**State machine legality and termination**

- P5: For any sequence of transition requests, the persisted Recovery_Case state history contains only transitions present in the legal transition table. (Requirement 2, criteria 2, 3)
- P6: For any Recovery_Case, the state history reaches a Terminal_State within RECOVERY_WINDOW_DURATION plus OUTCOME_WAIT_TIMEOUT of the detection timestamp, and no state appears after a Terminal_State other than a verified payment reconciliation. (Requirement 2, criteria 4, 12)

**No action after PAID or OPT_OUT**

- P7: For any Recovery_Case whose verified payment state became paid at time T, no customer-visible or payment-affecting action holds a confirmed execution timestamp later than T, except actions already in flight, which are recorded as POST_PAYMENT_ACTION. (Requirement 8, criterion 3; Requirement 10, criteria 4, 5)
- P8: For any customer holding opt-out status, the count of confirmed customer-visible actions across every Recovery_Case of that customer after the opt-out timestamp equals zero. (Requirement 8, criterion 4; Requirement 17, criterion 10)

**Bounds**

- P9: For any Recovery_Case, the confirmed executed-action count is at most MAX_RECOVERY_ATTEMPTS and the confirmed customer-message count is at most MAX_CUSTOMER_MESSAGES. (Requirement 2, criteria 7, 8; Requirement 8, criteria 6, 7)
- P10: For any two consecutive confirmed outbound actions on one Recovery_Case, the interval between them is at least COOLDOWN_INTERVAL. (Requirement 8, criterion 8)
- P11: For any confirmed action, the execution timestamp falls inside the Recovery_Window of the Recovery_Case. (Requirement 2, criterion 6; Requirement 8, criterion 9)

**Audit trail**

- P12: For any Recovery_Case, Audit_Record sequence numbers are strictly increasing, contain no gaps, and no persisted Audit_Record is modified or removed by any operation. (Requirement 11, criteria 3, 4)
- P13: For any inbound Payment_Event, every Audit_Record produced from that Payment_Event carries the same correlation_id. (Requirement 11, criterion 7)

**Incremental value arithmetic**

- P14: For any Candidate_Action set, incremental_probability equals intervention_recovery_probability minus baseline_recovery_probability, expected_incremental_revenue equals payment_amount multiplied by incremental_probability, and net_recovery_value equals expected_incremental_revenue minus the three cost terms, computed in integer minor units. (Requirement 7, criteria 1 through 3, 12)
- P15: For any Candidate_Action set, the selected Candidate_Action holds a net_recovery_value greater than or equal to the net_recovery_value of every other Candidate_Action not excluded by policy or by an exclusion rule. (Requirement 7, criterion 4)
- P16: For any Candidate_Action set where every intervention net_recovery_value falls below MIN_NET_VALUE_THRESHOLD, the selection is DO_NOTHING or WAIT. (Requirement 7, criterion 5)
- P17: For any Candidate_Action set where baseline_recovery_probability is greater than or equal to HIGH_BASELINE_THRESHOLD and no Candidate_Action clears both thresholds, the selection is DO_NOTHING or WAIT. This is the "customer was going to pay anyway" property. (Requirement 7, criterion 6)
- P18: The Candidate_Action holding the greatest intervention_recovery_probability is not necessarily the selection; whenever the two differ, both appear in the Recommendation with their net_recovery_value figures. (Requirement 7, criterion 8)
- P19: DO_NOTHING always holds incremental_probability equal to zero, action_cost equal to zero, and therefore net_recovery_value equal to zero minus risk_cost. (Requirement 6, criterion 4)

**Metrics and causality**

- P20: For any reporting period, observed_recovered_revenue plus natural_recovered_revenue equals the sum of confirmed recovered amounts, each recovered amount is counted once, and unresolved_revenue equals the summed payment_amount of non-recovered Terminal_State Recovery_Cases. (Requirement 10, criteria 8, 9, 10, 11; Requirement 12, criteria 1 through 3)
- P21: incremental_recovered_revenue is non-zero only where a completed experiment reports an uncertainty interval excluding zero; otherwise the reported observed recovery carries the label CAUSALITY_NOT_ESTABLISHED. (Requirement 12, criteria 4, 9; Requirement 13, criteria 8, 9)
- P22: For any two consecutive periods where recovery_rate increases and net_recovered_revenue decreases, a COST_OUTPACING_RECOVERY finding exists. (Requirement 12, criterion 8)
- P23: For any metric derived from a Synthetic_Dataset, the provenance label SYNTHETIC appears in every presentation of that metric. (Requirement 12, criterion 11; Requirement 14, criterion 9)

**Experiment integrity**

- P24: For one experiment identifier and one Recovery_Case identifier, group assignment is deterministic and stable across repeated evaluation, and the assignment timestamp precedes the first Diagnosis record. (Requirement 13, criteria 1, 2)
- P25: For any Recovery_Case assigned to Control_Group, the count of Revora-selected interventions executed equals zero. (Requirement 13, criterion 3)

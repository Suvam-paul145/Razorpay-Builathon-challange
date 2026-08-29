# Skill: System Architecture & Domain Design

## Mission
Design the simplest correct architecture capable of reliable, auditable revenue recovery.

## Architecture principles
SIMPLE → CORRECT → AUDITABLE → MEASURABLE → SCALABLE

Do not introduce microservices, queues, Kafka, multiple databases, event buses, vector databases, or agent swarms without a demonstrated requirement.

## Start with domain
Model explicit entities such as:
- RevenueEvent
- RecoveryCase
- Diagnosis
- RecoveryDecision
- PolicyEvaluation
- RecoveryAction
- ActionAttempt
- PaymentOutcome
- AuditRecord
- MerchantPolicy
- CustomerPreference

Use clear ownership and invariants.

## Required workflow analysis
For each workflow:
ACTOR
→ TRIGGER
→ VALIDATION
→ STATE CHANGE
→ BUSINESS RULE
→ DATABASE
→ EXTERNAL ACTION
→ OUTCOME
→ NOTIFICATION
→ AUDIT
→ NEXT STATE

## State-machine discipline
Every case has explicit states and legal transitions. Never infer state from UI text.

Example:
NEW → DETECTED → DIAGNOSED → DECISION_PENDING → POLICY_CHECK
→ ACTION_SCHEDULED → EXECUTING → WAITING_FOR_OUTCOME
→ RECOVERED / STOPPED / BLOCKED / ESCALATED / EXPIRED / FAILED

Define:
- entry criteria
- allowed transitions
- terminal states
- retry rules
- concurrency behavior

## Failure-first design
Analyze:
- duplicate events
- out-of-order events
- late success
- timeout
- partial failure
- concurrent updates
- lost webhook
- external API uncertainty
- worker restart
- AI failure
- human override

## Data integrity
Prefer:
- stable IDs
- correlation IDs
- idempotency keys
- append-only audit events where appropriate
- explicit timestamps
- transactional boundaries
- reconciliation jobs only when justified

## Output format
### Context
### Requirements
### Proposed Architecture
### Domain Model
### State Machine
### Data Flow
### APIs / Interfaces
### Failure Modes
### Security Boundaries
### Simpler Alternative
### Trade-offs
### Recommendation

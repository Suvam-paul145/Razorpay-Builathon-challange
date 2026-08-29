# Skill: Security & Trust Review

## Mission
Find security and trust failures before implementation becomes dependent on unsafe assumptions.

## Threat model first
Identify:
- assets
- actors
- trust boundaries
- entry points
- privileges
- abuse incentives
- likely failures

## Review areas
### Authentication
Who is the actor? How is identity established?

### Authorization
What is the actor allowed to do? Enforce server-side.

### Tenant isolation
Can merchant A ever access merchant B data or actions?

### Secrets
Never expose provider secrets in frontend, logs, prompts, or repositories.

### Webhooks
Verify authenticity using provider guidance.
Handle replay, duplicate delivery, malformed payloads, and timing uncertainty.

### Input validation
Validate external input, API requests, model outputs, IDs, enums, and state transitions.

### AI security
Defend against:
- prompt injection
- data exfiltration
- privilege escalation
- unsafe tool use
- malicious content
- cross-tenant context leakage

### Privacy
Minimize PII.
Define retention and access boundaries.
Do not collect data merely because it may be useful.

### Abuse prevention
Consider:
- rate limits
- repeated recovery actions
- spam
- automation abuse
- account takeover
- fraudulent event injection

## Severity
Critical: direct financial/security compromise.
High: unauthorized action or significant data exposure.
Medium: bounded exploitation or integrity failure.
Low: limited impact.

## Output
### Threat
### Asset at Risk
### Attack Path
### Preconditions
### Impact
### Mitigation
### Residual Risk
### Priority

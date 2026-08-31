# Agent Contract

## Agent ID
[Unique identifier, e.g., "Agent-C-Recovery"]

## Role
[Domain from A–K, e.g., "Domain C — Recovery"]

## Objective
[One sentence describing the agent's goal]

## Required Skills
[List of skill numbers this agent must read before coding]
- Skill 00: Governance
- Skill [XX]: [Name]

## Inputs
[What to read before coding]
- `requirements.md` sections: [list]
- `design.md` sections: [list]
- `tasks.md` tasks: [list]
- Existing code: [file paths]

## Dependencies
[Tasks that must be complete before this agent starts]
- Task [X.Y]: [description] — status: [COMPLETE/IN_PROGRESS]

## Owned Files
[Files this agent creates or modifies — primary ownership]
- `revora/[module]/[file].py`

## Files Allowed to Read (Not Modify)
- `revora/domain/enums.py`
- `revora/platform/clock.py`

## Files Forbidden to Modify
- [Any files owned by other agents]

## Public Contracts Used
[Interfaces this agent consumes]
- `CaseState` enum from `revora/domain/enums.py`
- `PolicyInput` from `revora/policy/input.py`

## Public Contracts Allowed to Change
[If any — empty means no contract changes permitted]

## Expected Outputs
[Deliverables]
- [ ] `revora/[module]/[file].py` — [description]
- [ ] Tests in `tests/[path]`
- [ ] Completion report

## Tests Required
- [ ] Unit tests for [functionality]
- [ ] Property tests for [properties P_]
- [ ] Integration tests for [scenarios]

## Verification Required
[How to prove correctness]
- `pytest tests/[path]` passes
- `mypy --strict revora/[module]` clean
- `lint-imports` passes
- [Specific checks]

## Integration Dependencies
[What must combine with this agent's work]
- Agent-[Y]'s [output] feeds into [this file]

## Stop Conditions
[When to halt and report instead of guessing]
- If [contract X] needs changing
- If [assumption Y] is wrong
- If [requirement Z] is ambiguous

# Agent Completion Report

## Agent ID
[e.g., "Agent-C-Recovery"]

## Status
[COMPLETE / BLOCKED / NEEDS_REVIEW]

## Tasks Completed
[Task IDs from tasks.md]
- [x] Task [X.Y]: [description]

---

## Implemented
[Summary of what changed]

## Files Created
| File | Purpose |
|---|---|
| `revora/[path]` | [description] |

## Files Modified
| File | Change Summary |
|---|---|
| `revora/[path]` | [what changed] |

## Contracts
### Consumed (read-only)
- [Interface/type from another module]

### Created
- [New interface/type this agent defined]

### Changed
- [Contract changes — should be empty if contract was frozen]

---

## Tests

| Test | Status | Notes |
|---|---|---|
| `tests/[path]` | PASS/FAIL | [details] |

### Test Commands Run
```bash
pytest tests/[path] -v
mypy --strict revora/[module]
lint-imports
```

---

## Risks
- [Known risk or edge case]

## Assumptions
- [New assumption made during implementation]

## Decisions
| Decision | Rationale |
|---|---|
| [What was decided] | [Why] |

---

## Integration Notes
[What another agent needs to know to integrate with this work]

- Agent-[Y] should [action] because [reason]
- The [interface] expects [specific behavior]

## Blockers
[What prevents continuation, if anything]

- [Blocker description] → needs [agent/human] to resolve

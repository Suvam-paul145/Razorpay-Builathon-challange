# Orchestration State

This directory tracks **execution status** for multi-agent parallel development of Revora.

## Source of truth

The authoritative product specifications are:

- `requirements.md` — what to build
- `design.md` — how to build it
- `tasks.md` — implementation order and dependencies
- `decision-checkpoints.md` — when to ask

**Do NOT duplicate requirements into orchestration files.** This directory records only:

- Current phase and wave
- Active agent assignments
- Completed / blocked / in-progress tasks
- Frozen contracts and their versions
- Integration status
- Decision log for orchestration-level decisions

## Structure

```
.kiro/orchestration/
├── README.md                          ← this file
├── domain-ownership.md                ← domain-to-module mapping
└── templates/
    ├── phase-plan.md                  ← orchestrator output per wave
    ├── agent-contract.md              ← per-agent assignment
    ├── agent-completion-report.md     ← structured agent output
    └── change-impact-analysis.md      ← shared contract changes
```

## How to use

1. When starting a new implementation wave, fill out `templates/phase-plan.md`.
2. For each agent, fill out `templates/agent-contract.md`.
3. When an agent completes, they fill out `templates/agent-completion-report.md`.
4. When a frozen contract must change, fill out `templates/change-impact-analysis.md`.

## Related skills

- Skill 12: Multi-Agent Orchestration
- Skill 13: Parallel Workstream Design
- Skill 14: Agent Contracts and Ownership
- Skill 15: Integration Quality Gates
- Skill 16: Cross-Agent Review

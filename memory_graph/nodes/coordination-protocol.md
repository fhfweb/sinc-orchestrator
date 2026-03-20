---
id: coordination-protocol
type: module
project_slug: orchestrator-os
tags: [coordination, locks, agents, conflict-prevention]
---

# Coordination Protocol

Prevents conflicts when multiple AI agents (Codex, Claude Code, Antigravity) work on
the same project simultaneously.

## Lock system

Two complementary files:
- `ai-orchestrator/coordination/locks.json` — machine-readable, authoritative
- `docs/agents/LOCKS.md` — human-readable mirror (synced by `Sync-LockState.ps1`)

### Lock structure

```json
{
  "locks": [
    {
      "id": "lock-001",
      "agent": "Codex",
      "zone": "app/models/",
      "status": "active",
      "task_id": "DEV-MIGRATION-001",
      "acquired_at": "2026-03-09T10:00:00Z",
      "expires_at": "2026-03-09T10:30:00Z"
    }
  ]
}
```

## Coordination modes

Set via `Set-CoordinationMode.ps1`:
- `normal` — agents work concurrently on non-overlapping zones
- `freeze` — no new tasks assigned; active tasks complete then pause
- `release` — clears all locks and resumes normal mode

## Zone rules

Defined in `docs/agents/CONFLICT_RULES.md`:
- No two agents may hold a lock on the same file zone simultaneously
- Overlapping zones (e.g. `app/` and `app/models/`) are treated as conflicting
- REPAIR tasks bypass zone locks (emergency override)

## Agent capacity defaults

| Agent | Max concurrent tasks |
|-------|---------------------|
| Codex | 2 |
| Claude Code | 1 |
| Antigravity | 1 |

## Relations
- USED_BY: orchestrator-core
- USED_BY: v2-architecture
- ENFORCED_BY: intake-pipeline

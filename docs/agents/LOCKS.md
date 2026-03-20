# Lock Manager

## Active Locks

| TASK-ID | Agent | Locked At | TTL | Files Affected |
|---------|-------|-----------|-----|----------------|
| -       | -     | -         | -   | -              |

<!-- Sync source: ai-orchestrator/locks/locks.json -->
## Lock Rules

1. Lock task files before edits.
2. Release locks when task leaves in-progress.
3. Resolve stale locks before next assignment.
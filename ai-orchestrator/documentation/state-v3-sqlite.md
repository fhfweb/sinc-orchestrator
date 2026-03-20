# Task State V3 (SQLite Mirror)

## Objective
Provide a transactional, queryable state backend without breaking current JSON flow.

`task-dag.json` remains canonical for now. `task-state-v3.db` is an automatically synced mirror.

## Location
- DAG: `ai-orchestrator/tasks/task-dag.json`
- DB: `ai-orchestrator/state/task-state-v3.db`

## Sync command
```powershell
python scripts/v2/task_state_db.py --project-path . --mode sync --emit-json
```

## Query examples
Open execution tasks:
```powershell
python scripts/v2/task_state_db.py --project-path . --mode query --query open-execution --limit 20 --emit-json
```

Blocked tasks:
```powershell
python scripts/v2/task_state_db.py --project-path . --mode query --query blocked --limit 20 --emit-json
```

## Current rollout
- Observer runs DB sync each cycle and reports `task-state-db-sync` in `health-report.json`.
- Project state now includes `task_state_db` summary.
- This enables phased migration from file-based coordination to SQL-first scheduling/querying.

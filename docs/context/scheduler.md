# Module Context: Scheduler

Canonical implementation:
- worker: `ai-orchestrator/services/scheduler_worker.py`
- runtime logic: `ai-orchestrator/services/streaming/core/runtime_plane.py`
- routes: `ai-orchestrator/services/streaming/routes/system.py`

## What it does

1. Reads canonical task state from Postgres.
2. Resolves dependency eligibility using DB-backed compatibility helpers.
3. Assigns tasks to agents and bridge routes.
4. Persists dispatch metadata, task ownership, and scheduling decisions.
5. Feeds the same state plane consumed by watchdog, dashboard, and readiness.

## Canonical outputs

- task status transitions
- task assignments
- webhook dispatch rows
- agent event rows

## Legacy note

`scripts/v2/Invoke-SchedulerV2.ps1` is retained only for migration audit and fallback analysis. New behavior must not be implemented there.

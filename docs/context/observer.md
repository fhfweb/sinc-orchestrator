# Module Context: Observer

Canonical implementation:
- worker: `ai-orchestrator/services/observer_worker.py`
- runtime logic: `ai-orchestrator/services/streaming/core/runtime_plane.py`
- routes: `ai-orchestrator/services/streaming/routes/system.py`

## What it does

1. Reads canonical task/runtime state from Postgres.
2. Evaluates readiness and operational drift.
3. Persists observer snapshots and incidents back into Postgres.
4. Seeds repair work through the same runtime plane used by scheduler and watchdog.
5. Exposes control endpoints through the FastAPI control plane.

## Canonical outputs

- `readiness_reports`
- `incidents`
- `loop_states`
- DB-backed system snapshots used by `/status`, `/readiness`, and dashboard APIs

## Legacy note

`scripts/v2/Invoke-ObserverV2.ps1` is retained only for migration audit and fallback analysis. New behavior must not be implemented there.

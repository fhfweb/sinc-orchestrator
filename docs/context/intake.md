# Module Context: Intake Pipeline

Canonical direction:
- provider APIs and Python services are the active control plane
- task state is Postgres-first
- project memory and graph/vector hydration remain part of intake and ingest

## Current runtime split

- Active provider runtime: `ai-orchestrator/services/streaming`
- Active client runtime: `ai-orchestrator/sdk/client_loop.py` and `agent_worker.py`
- Migration-only legacy intake: `scripts/v2/Invoke-UniversalOrchestratorV2.ps1`

## What intake must produce

1. Project identity and tenant metadata.
2. Initial task state in the canonical DB-backed plane.
3. Architecture and memory seed artifacts.
4. Optional graph/vector hydration kickoff.
5. Docker/provider/client bootstrap hints.

## Legacy note

The historic PowerShell intake path is still present for compatibility analysis, but new intake behavior should be implemented in Python services and routes.

# AI Project Orchestrator OS - Onboarding Guide

This guide reflects the canonical runtime as of 2026-03-19.

## Canonical Entry Point

The orchestrator is now Python-first.

- Provider stack: `ai-orchestrator/docker/docker-compose.orchestrator.yml`
- Client stack: `ai-orchestrator/docker/docker-compose.client.yml`
- Control plane: `ai-orchestrator/services/streaming`
- Dashboard: `http://127.0.0.1:8765/dashboard`
- Canonical task state: Postgres

PowerShell assets remain only for migration and legacy maintenance. They are not the operational path for new setups.

## Quick Start

### 1. Start provider

```powershell
cd ai-orchestrator/docker
cp .env.docker.generated .env
docker compose -f docker-compose.orchestrator.yml up -d
```

### 2. Create a tenant

```powershell
curl -X POST http://localhost:8765/admin/tenants `
  -H "X-Admin-Key: $env:ADMIN_API_KEY" `
  -H "Content-Type: application/json" `
  -d '{"name":"My Project","plan":"pro"}'
```

### 3. Start client workers in the project

```powershell
cd ai-orchestrator/docker
docker compose -f docker-compose.client.yml up -d
```

### 4. Open dashboard

- `http://127.0.0.1:8765/dashboard`

## Runtime Model

```text
Provider FastAPI control plane
  |- scheduler_worker
  |- observer_worker
  |- readiness_worker
  |- external_agent_bridge_worker
  |- watchdog

Client workers
  |- agent-worker
  |- orchestrator-loop
  |- ingest-worker
```

## Notes

- `task-dag.json` is being demoted to projection-only output.
- `scripts/v2/Start-StreamingServer.py` is deprecated.
- `docker-compose.n5.yml` is deprecated and inert.
- `services/orchestrator_core.py` is no longer part of the official deployment.

## If you need migration-only legacy access

Legacy surfaces must be treated as opt-in compatibility only. Do not build new flows on them.

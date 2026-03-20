# AI Project Orchestrator OS

Python-first control plane for autonomous software delivery.

This repository now treats the FastAPI runtime under `ai-orchestrator/services/streaming` as the only canonical execution surface. PowerShell assets remain in the repository only as legacy maintenance references until the migration backlog is fully closed.

## Canonical Runtime

- Control plane: `ai-orchestrator/services/streaming`
- Official provider stack: `ai-orchestrator/docker/docker-compose.orchestrator.yml`
- Official client stack: `ai-orchestrator/docker/docker-compose.client.yml`
- Official dashboard: `http://127.0.0.1:8765/dashboard`
- Canonical task state: Postgres (`orchestrator_tasks`)
- Eventing: Redis + append-only event store
- Memory plane: Postgres projections + Neo4j + Qdrant

## Current Architecture

```text
FastAPI control plane (8765)
  |- scheduler_worker
  |- observer_worker
  |- readiness_worker
  |- external_agent_bridge_worker
  |- policy_worker
  |- mutation_worker
  |- finops_worker
  |- deploy_verify_worker
  |- pattern_promotion_worker
  |- release_worker
  |- watchdog
  |- dashboard + admin/control routes
  |- SDK endpoints for client workers

Client-side Python workers
  |- agent_worker.py
  |- client_loop.py
  |- ingest_pipeline.py

State and memory
  |- Postgres task state
  |- Redis event bus / streaming
  |- Neo4j knowledge graph
  |- Qdrant vector retrieval
```

## Repository Priorities

- `ai-orchestrator/`: provider runtime, SDK, Docker, migration docs
- `docs/agents/`: human-facing projections and coordination docs
- `scripts/` and `scripts/v2/`: legacy maintenance only, not canonical runtime
- `workspace/projects/`: project workspaces consumed by the orchestrator

## Operator Rules

- Do not treat `scripts/v2` as active runtime unless a migration task explicitly says so.
- Do not add new provider features to PowerShell.
- Do not create parallel compose stacks for the same role.
- Do not reintroduce `task-dag.json` as operational source of truth.
- Prefer Postgres-backed APIs and projections for all new work.

## Official Entry Points

### Provider

```powershell
cd ai-orchestrator/docker
docker compose -f docker-compose.orchestrator.yml up -d
```

### Client

```powershell
cd ai-orchestrator/docker
docker compose -f docker-compose.client.yml up -d
```

### Dashboard

- `http://127.0.0.1:8765/dashboard`

## Migration Status

The platform is in controlled migration to Python-only runtime.

Already removed from the official path:
- `docker-compose.n5.yml` as active stack
- `orchestrator-core` from official compose
- PowerShell loop from official client compose
- static legacy dashboard as active UI

Still pending before full legacy shutdown:
- archive `scripts/v2` after parity tasks are complete
- archive `ai-orchestrator/scripts/v2` after compatibility tombstones are in place
- finalize `task-dag.json` as projection-only artifact

## Reference Documents

- `ai-orchestrator/README.md`
- `ai-orchestrator/documentation/migration/README.md`
- `ai-orchestrator/documentation/migration/migration-task-board.md`
- `docs/agents/SYSTEM_SUMMARY.md`
- `docs/agents/SYSTEM_MAP.md`

# PROJECT_BRAIN

Canonical architectural context for the orchestrator.

This file is the human/agent-facing summary of the active runtime. It supersedes older descriptions based on `task-dag.json`, `Invoke-AutonomousLoopV2.ps1`, `Start-StreamingServer.py`, and `orchestrator_core.py`.

## Canonical runtime

- Control plane: `ai-orchestrator/services/streaming`
- Entry point: `ai-orchestrator/services/streaming_server_v2.py`
- Official provider stack: `ai-orchestrator/docker/docker-compose.orchestrator.yml`
- Official client stack: `ai-orchestrator/docker/docker-compose.client.yml`
- Official dashboard: `http://127.0.0.1:8765/dashboard`
- Canonical task state: Postgres (`orchestrator_tasks`)

## Runtime topology

```text
FastAPI control plane (8765)
  |- dashboard
  |- admin/control routes
  |- task and agent APIs
  |- SSE/events
  |- SDK distribution
  |- embedded watchdog only

Dedicated provider workers
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

Client-side Python workers
  |- sdk/client_loop.py
  |- sdk/agent_worker.py
  |- services/ingest_pipeline.py

State and memory
  |- Postgres task state and governance reports
  |- Redis event bus / SSE coordination
  |- Neo4j knowledge graph
  |- Qdrant vector retrieval
```

## Source of truth

Operational source of truth is Postgres.

- `tasks`
- `webhook_dispatches`
- `agent_events`
- `loop_states`
- `incidents`
- `readiness_reports`
- `policy_reports`
- `mutation_reports`
- `finops_reports`
- `deploy_reports`
- `pattern_promotion_reports`
- `release_reports`

`task-dag.json` is in migration to projection-only status. It is not the canonical operational state.

## Control surfaces

Primary runtime APIs:

- `GET /health`
- `GET /health/deep`
- `GET /dashboard`
- `GET /events`
- `POST /tasks`
- `POST /tasks/claim`
- `POST /tasks/complete`
- `POST /external-bridge/run`
- `GET /external-bridge/status`
- `POST /policy/run`
- `GET /policy`
- `POST /mutation/run`
- `GET /mutation`
- `POST /finops/run`
- `GET /finops`
- `POST /deploy-verify/run`
- `GET /deploy-verify`
- `POST /pattern-promotion/run`
- `GET /pattern-promotion`
- `POST /release/run`
- `GET /release`

## Legacy status

These files remain in the repository only for controlled migration, audit, or rollback analysis:

- `scripts/v2/*`
- `ai-orchestrator/scripts/v2/*`
- `scripts/v2/Start-StreamingServer.py`
- `ai-orchestrator/services/orchestrator_core.py`
- `ai-orchestrator/docker/archive-docker-compose.n5.legacy.yml`

Rules:

- Do not add new runtime behavior to PowerShell.
- Do not treat `scripts/v2` as the active loop.
- Do not restore `docker-compose.n5.yml` to the provider path.
- Do not reintroduce `8767` or `orchestrator-core` into the official stack.

## Operational model

1. Provider stack boots infra plus the FastAPI control plane.
2. Dedicated Python workers run scheduling, observation, readiness, external bridge, and governance loops.
3. Client workers connect over HTTP API and operate on project workspaces.
4. Incidents, repairs, governance reports, and release gates persist in Postgres.
5. Dashboard and metrics read real state from Postgres/Redis/Neo4j/Qdrant.

## Current migration priorities

- Finish Postgres-first projections so `task-dag.json` becomes read-only.
- Close parity for remaining quality/governance details where heuristics are still baseline.
- Archive legacy trees physically after remaining compatibility tombstones are in place.

## What changed from the old architecture

Deprecated assumptions:

- `task-dag.json` as the single source of truth
- PowerShell as the canonical orchestrator shell
- Flask `Start-StreamingServer.py` as the control plane
- `orchestrator_core.py` as the official provider API
- parallel compose variants such as `n5`

Current assumptions:

- Python/FastAPI is the only canonical runtime direction
- Postgres is the operational source of truth
- workers are explicit services in the official compose
- legacy assets are opt-in only and excluded from default deployment

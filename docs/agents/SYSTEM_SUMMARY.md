# SYSTEM_SUMMARY - AI Project Orchestrator OS

Mandatory read at session start.

## Identity

- Name: AI Project Orchestrator OS
- Runtime generation: Python control plane
- Canonical platform: FastAPI + Postgres + Redis + Neo4j + Qdrant
- Canonical dashboard: `/dashboard` on port `8765`
- Status: migration in progress, official path already Python-only for provider and client stacks

## Canonical Stack

| Layer | Runtime |
|---|---|
| Control plane | `ai-orchestrator/services/streaming` |
| API server | FastAPI / Uvicorn |
| Task state | Postgres |
| Event transport | Redis + append-only event stream |
| Scheduler/observer/readiness | Embedded Python workers |
| External agent execution | Python external bridge + agent workers |
| Semantic memory | Qdrant |
| Knowledge graph | Neo4j |
| Dashboard | FastAPI dashboard at `/dashboard` |

## Active Runtime Components

| Component | Location | Role |
|---|---|---|
| Provider compose | `ai-orchestrator/docker/docker-compose.orchestrator.yml` | official provider stack |
| Client compose | `ai-orchestrator/docker/docker-compose.client.yml` | official client stack |
| Control plane | `ai-orchestrator/services/streaming/__init__.py` | app bootstrap |
| Runtime plane | `ai-orchestrator/services/streaming/core/runtime_plane.py` | scheduler/observer/readiness logic |
| Watchdog | `ai-orchestrator/services/streaming/core/watchdog.py` | reclaim, stale recovery, repair seeding |
| External bridge | `ai-orchestrator/services/streaming/core/external_agent_bridge.py` | dispatch/completion for external agents |
| SDK loop | `ai-orchestrator/sdk/client_loop.py` | client autonomous loop |
| SDK agent | `ai-orchestrator/sdk/agent_worker.py` | client worker |

## State Model

- Canonical source of truth: Postgres
- `task-dag.json` is being demoted to projection-only output
- Dashboard and health paths must read DB-first sources
- Human-facing Markdown should be generated from canonical state, not manually drifted copies

## Runtime Contract

1. Observe from Postgres-backed state.
2. Schedule from Postgres-backed queues and dependencies.
3. Dispatch to Python workers or external bridge.
4. Complete back into Postgres.
5. Generate incidents, repairs, readiness, and projections from the same state plane.

## Legacy Policy

These artifacts are not canonical anymore:

- `scripts/v2/*`
- `scripts/v2/Start-StreamingServer.py`
- `docs/agents/dashboard.html`
- `docker-compose.n5.yml`
- `services/orchestrator_core.py`

They may remain in the repository temporarily for compatibility, audit, or migration tasks, but new behavior must not be implemented there.

## Operator Guidance

- Use `docker-compose.orchestrator.yml` as the only provider stack.
- Use `docker-compose.client.yml` as the only client stack.
- Use `/dashboard` as the only dashboard UI.
- Do not add new endpoints to Flask or PowerShell surfaces.
- Do not add new compose variants for the same runtime role.

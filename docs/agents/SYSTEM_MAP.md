# SYSTEM_MAP - AI Project Orchestrator

Canonical map of the active runtime.

## Official Topology

```text
Provider stack
  docker-compose.orchestrator.yml
    |- orchestrator-streaming (8765)
    |- postgres task-db
    |- redis
    |- neo4j
    |- qdrant
    |- ollama
    |- supporting workers

Client stack
  docker-compose.client.yml
    |- agent-worker
    |- orchestrator-loop
    |- ingest-worker
```

## Core Flow

1. API receives tasks, control commands, or ingest requests.
2. `runtime_plane.py` computes scheduling, readiness, and observer actions from Postgres.
3. `watchdog.py` reclaims stale work and seeds repairs/incidents.
4. `external_agent_bridge.py` dispatches external-agent tasks and processes completions.
5. SDK workers execute against the client workspace and report completion over HTTP.
6. Dashboard reads canonical runtime state from the same control plane.

## Active Modules

- `ai-orchestrator/services/streaming/__init__.py`
- `ai-orchestrator/services/streaming/routes/*`
- `ai-orchestrator/services/streaming/core/runtime_plane.py`
- `ai-orchestrator/services/streaming/core/watchdog.py`
- `ai-orchestrator/services/streaming/core/external_agent_bridge.py`
- `ai-orchestrator/services/scheduler_worker.py`
- `ai-orchestrator/services/observer_worker.py`
- `ai-orchestrator/services/readiness_worker.py`
- `ai-orchestrator/services/external_agent_bridge_worker.py`
- `ai-orchestrator/sdk/client_loop.py`
- `ai-orchestrator/sdk/agent_worker.py`
- `scripts/memory_sync.py`

## Data Stores

- Postgres: canonical task state, incidents, readiness, projections
- Redis: queue/event transport and streaming support
- Neo4j: architecture and knowledge graph
- Qdrant: embeddings and semantic retrieval
- Markdown: generated projections, ADRs, migration docs, human coordination

## Legacy Boundaries

Legacy artifacts still present for controlled migration only:

- `scripts/v2/*`
- `ai-orchestrator/scripts/v2/*`
- `services/orchestrator_core.py`
- `scripts/v2/Start-StreamingServer.py`
- `docs/agents/dashboard.html`

These are outside the active topology and must not be treated as source of truth.

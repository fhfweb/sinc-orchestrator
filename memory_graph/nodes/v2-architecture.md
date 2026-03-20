---
id: v2-architecture
type: module
project_slug: orchestrator-os
tags: [architecture, v2, json-machine, powershell]
---

# V2 Architecture

The V2 layer is the production-ready orchestration engine. All state is JSON-canonical:
the markdown files are human-readable mirrors, never the source of truth.

## Design principles

1. **JSON-first** — `state.json` and `task-dag.json` are the authoritative state
2. **Markdown mirrors** — `TASK_BOARD.md`, `backlog.md`, `LOCKS.md` are synced views
3. **Idempotent intake** — re-submitting a project merges state instead of resetting it
4. **Guard rails over trust** — architecture.md content guard, vault DPAPI encryption,
   `-Force` safety checks
5. **Self-healing loop** — Observer → Scheduler → Loop creates REPAIR tasks automatically

## Module layout

```
scripts/v2/
  Invoke-UniversalOrchestratorV2.ps1   Main engine (submit, new, access, clean)
  Invoke-ObserverV2.ps1                 Health sensor + REPAIR task generator
  Invoke-SchedulerV2.ps1                DAG-aware task assignor
  Invoke-AutonomousLoopV2.ps1           Continuous loop controller
  Invoke-DockerAutoBuilderV2.ps1        Docker compose generator
  Set-CoordinationMode.ps1              Freeze/release agent coordination
  Invoke-CoordinationStatus.ps1         Read current coordination state
  Invoke-LocalEnvAgent.ps1              Local Ollama relay agent
  Common.ps1                            Shared utility functions
```

## State schema (state.json)

```json
{
  "project_slug": "my-project",
  "project_name": "My Project",
  "stack": "python",
  "databases": { "relational": {...}, "neo4j": {...}, "qdrant": {...} },
  "infra_mode": "dedicated-infra",
  "startup_paths": {
    "docker_compose_file": "ai-orchestrator/docker/docker-compose.generated.yml",
    "secrets_vault": "ai-orchestrator/database/.secrets/vault.json"
  }
}
```

## Relations
- PART_OF: orchestrator-core
- USES: intake-pipeline
- USES: coordination-protocol
- USES: memory-layer

---
id: orchestrator-core
type: module
project_slug: orchestrator-os
tags: [orchestration, entry-point, powershell, v2]
---

# Orchestrator Core

The root entry point of the AI Project Orchestrator OS. Accepts an `-Action` parameter
and dispatches to the appropriate V2 script.

## Responsibilities

- Parse CLI action and project path
- Validate that required files exist
- Delegate to V2 sub-scripts (intake, schedule, observe, loop, access, clean)
- Surface exit codes and error messages back to the caller

## Key file

`scripts/orchestrator.ps1` (root launcher — V1 compat wrapper)
`scripts/v2/Invoke-UniversalOrchestratorV2.ps1` (V2 main engine)

## Actions

| Action | Script | Description |
|--------|--------|-------------|
| `v2-submit` | Invoke-UniversalOrchestratorV2.ps1 | Intake + scaffold a project |
| `v2-observe` | Invoke-ObserverV2.ps1 | Health check pass |
| `v2-schedule` | Invoke-SchedulerV2.ps1 | Assign pending tasks to agents |
| `v2-loop` | Invoke-AutonomousLoopV2.ps1 | Continuous observe→schedule loop |
| `v2-access` | Invoke-UniversalOrchestratorV2.ps1 | Print connection info |
| `v2-clean` | Invoke-UniversalOrchestratorV2.ps1 | Tear down infra for a project |

## Relations
- CONTAINS: v2-architecture
- OWNS: intake-pipeline
- ORCHESTRATES: coordination-protocol

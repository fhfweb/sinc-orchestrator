---
id: intake-pipeline
type: module
project_slug: orchestrator-os
tags: [intake, classification, world-model, dag]
---

# Intake Pipeline

The intake pipeline is the entry gate for every project. It transforms a raw project
directory into a fully scaffolded, AI-ready workspace.

## Pipeline stages

```
1. Stack detection    (Invoke-ProjectIntake.ps1 — V1 classifier)
2. World model        (extract_world_model.py — 5 semantic dimensions)
3. AI scaffold        (Write-V2AnalysisArtifacts — architecture.md + tech-debt.md stubs)
4. Task seeding       (Set-V2TaskSeeds — 5 bootstrap tasks in task-dag.json, merge-safe)
5. Docker generation  (Invoke-DockerAutoBuilderV2.ps1 — compose + env files)
6. Secrets vault      (Write-V2SecretsVault — DPAPI-encrypted credentials)
7. Memory sync        (memory_sync.py — push all_nodes to Neo4j + Qdrant)
```

## Classification output

```json
{
  "stack": "python",
  "framework": "fastapi",
  "database": "postgres",
  "legacy_policy": "maintain",
  "open_questions": ["Does the project use async tasks?"]
}
```

## Bootstrap task IDs

| ID | Description | Default deps |
|----|-------------|-------------|
| `V2-INTAKE-001` | Project intake and classification | none |
| `V2-PLAN-001` | Architecture planning | V2-INTAKE-001 |
| `V2-ANALYSIS-001` | Tech-debt and risk analysis | V2-INTAKE-001 |
| `V2-DOCKER-001` | Docker scaffold validation | V2-PLAN-001 |
| `V2-LEGACY-GATE-001` | Legacy gate review | V2-ANALYSIS-001 |

## Guard rails

- **Merge on re-submit**: existing `done/skipped/in-progress` task status is preserved
- **Architecture content guard**: `architecture.md` not overwritten if it has >30 lines
- **Non-bootstrap carry-forward**: `DEV-*` and `REPAIR-*` tasks are never deleted on re-submit

## Relations
- PART_OF: v2-architecture
- WRITES_TO: memory-layer
- SEEDS: coordination-protocol

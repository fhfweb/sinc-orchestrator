---
id: cross-episode-4e0416408d28e8d1
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: sistema-gestao-psicologos-autonomos
source_kind: pattern
source_files: [ai-orchestrator/patterns/repair-20260311153221-fba72f.md]
source_modules: [ai-orchestrator/patterns]
content_hash: 4e0416408d28e8d13f34668be0e440f2309eddf7ad0856ebb3590be7b53d8b6a
---

# Cross-Project Episode: Pattern: REPAIR-20260311153221-fba72f

## Summary
# Pattern: REPAIR-20260311153221-fba72f

**Source task:** REPAIR-20260311153221-fba72f
**Resolved by:** Codex
**Recorded at:** 2026-03-11T16:04:09

## Problem
agent-dispatch-failures :: failed_agents=AG-10

## Solution
Corrigido erro de resolução de caminho de script no Invoke-AgentDispatcherV2.ps1 que impedia a execução de agentes no Windows (Security, Performance, etc). Verificado com novo dispatch bem-sucedido.

## Artifacts
- scripts/v2/Invoke-AgentDispatcherV2.ps1

## Source
- project: sistema-gestao-psicologos-autonomos
- path: ai-orchestrator/patterns/repair-20260311153221-fba72f.md
- imported_at: 2026-03-13T14:28:23
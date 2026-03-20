---
id: cross-episode-3ce57711f6983e9c
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/repair-20260311153221-fba72f.md]
source_modules: [memory_graph/patterns]
content_hash: 3ce57711f6983e9cddacc55c31a90bb2387c24b1dfd10239fe512b4939bc5059
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
---
_Promoted from project: sistema-gestao-psicologos-autonomos on 2026-03-12T12:01:45_
_Confidence score: 0.85_

## Source
- project: project0
- path: memory_graph/patterns/repair-20260311153221-fba72f.md
- imported_at: 2026-03-13T14:28:23
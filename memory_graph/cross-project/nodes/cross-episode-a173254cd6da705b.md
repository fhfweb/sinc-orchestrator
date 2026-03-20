---
id: cross-episode-a173254cd6da705b
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: orchestrator-root
source_kind: pattern
source_files: [orchestrator-root/memory_graph/patterns/repair-20260311153221-fba72f.md]
source_modules: [orchestrator-root/memory_graph]
content_hash: a173254cd6da705b99b963442491dd5e631ad8b0232d0f81fcf995e2fc869cab
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
- project: orchestrator-root
- path: orchestrator-root/memory_graph/patterns/repair-20260311153221-fba72f.md
- imported_at: 2026-03-14T17:05:19
---
id: cross-episode-00ed0c0e5c86fc65
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: orchestrator-root
source_kind: pattern
source_files: [orchestrator-root/memory_graph/patterns/README.md]
source_modules: [orchestrator-root/memory_graph]
content_hash: 00ed0c0e5c86fc65acbbcefa7137e044c1b76de8e77b4ddf268c20cd7877dc40
---

# Cross-Project Episode: Pattern Library

## Summary
# Pattern Library

Soluções reutilizáveis aprendidas de REPAIR tasks resolvidas em projetos anteriores.
Cada arquivo representa um problema + solução confirmada que funciona.

## Como usar

Quando o Scheduler atribui uma task, o context bundle inclui automaticamente links
para os padrões mais recentes desta pasta.

Quando um agente completa uma REPAIR task via:
```powershell
.\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode complete `
    -ProjectPath <path> -TaskId REPAIR-xxx -AgentName "Claude Code" `
    -Notes "descrição da solução" -Artifacts "arquivo1,arquivo2"
```

O orquestrador salva automaticamente um padrão em:
- `ai-orchestrator/patterns/repair-xxx.md` (padrão local do projeto)
- Pode ser promovido aqui manualmente como padrão cross-project

## Formato de um padrão

```markdown
# Pattern: <título do problema>

**Source task:** REPAIR-xxx
**Resolved by:** Claude Code
**Recorded at:** 2026-03-11T...

## Problem
Descrição do que foi detectado.

## Solution
O que foi feito para resolver.

## Artifacts
arquivo1.py, arquivo2.sql
```

## Padrões conhecidos

| Arquivo | Problema | Stack |
|---------|----------|-------|
| neo4j-community-edition.md | Neo4j CE não suporta CREATE DATABASE | any |
| fastapi-lifespan.md | @app.on_event deprecated no FastAPI 0.103+ | python |
| docker-app-command-review.md | REVIEW_REQUIRED no comando app do compose | any |
| postgres-env-local.md | .env.example ausente para dev local | any |

## Source
- project: orchestrator-root
- path: orchestrator-root/memory_graph/patterns/README.md
- imported_at: 2026-03-14T17:05:19
---
id: cross-episode-9311fea432ed3875
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/README.md]
source_modules: [memory_graph/patterns]
content_hash: 9311fea432ed3875d93c2c98734339852a1074a5f735671f58df2f3ae1867d1b
---

# Cross-Project Episode: Pattern Library

## Summary
# Pattern Library

SoluÃ§Ãµes reutilizÃ¡veis aprendidas de REPAIR tasks resolvidas em projetos anteriores.
Cada arquivo representa um problema + soluÃ§Ã£o confirmada que funciona.

## Como usar

Quando o Scheduler atribui uma task, o context bundle inclui automaticamente links
para os padrÃµes mais recentes desta pasta.

Quando um agente completa uma REPAIR task via:
```powershell
.\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode complete `
    -ProjectPath <path> -TaskId REPAIR-xxx -AgentName "Claude Code" `
    -Notes "descriÃ§Ã£o da soluÃ§Ã£o" -Artifacts "arquivo1,arquivo2"
```

O orquestrador salva automaticamente um padrÃ£o em:
- `ai-orchestrator/patterns/repair-xxx.md` (padrÃ£o local do projeto)
- Pode ser promovido aqui manualmente como padrÃ£o cross-project

## Formato de um padrÃ£o

```markdown
# Pattern: <tÃ­tulo do problema>

**Source task:** REPAIR-xxx
**Resolved by:** Claude Code
**Recorded at:** 2026-03-11T...

## Problem
DescriÃ§Ã£o do que foi detectado.

## Solution
O que foi feito para resolver.

## Artifacts
arquivo1.py, arquivo2.sql
```

## PadrÃµes conhecidos

| Arquivo | Problema | Stack |
|---------|----------|-------|
| neo4j-community-edition.md | Neo4j CE nÃ£o suporta CREATE DATABASE | any |
| fastapi-lifespan.md | @app.on_event deprecated no FastAPI 0.103+ | python |
| docker-app-command-review.md | REVIEW_REQUIRED no comando app do compose | any |
| postgres-env-local.md | .env.example ausente para dev local | any |

## Source
- project: project0
- path: memory_graph/patterns/README.md
- imported_at: 2026-03-13T14:28:23
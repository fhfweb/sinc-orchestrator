# Migration Program: PowerShell -> Python Control Plane

## Objective

Descontinuar o runtime PowerShell sem perder capacidades operacionais do orquestrador e sem regredir as funcionalidades novas da frente Python.

Este diretório é a fonte de verdade da migração. O `task-dag.json` atual está saturado e não é um lugar seguro para governar esta transição até a camada de estado canônica ser consolidada no Postgres.

## Current Position

Hoje o produto está dividido em duas frentes:

- `ai-orchestrator/scripts/v2`: runtime legado com loop autônomo, observer, scheduler, readiness, finops, mutation e deploy verification.
- `ai-orchestrator/services`: control plane novo em Python/FastAPI com APIs modernas, SSE, watchdog, cognitive services, twin, analytics e dashboard.

A direção correta é manter a frente Python e migrar para ela as capacidades operacionais do legado.

## Non-Negotiable Rules

1. Python é a plataforma-alvo.
2. Nenhuma capacidade do legado pode ser desligada antes de existir equivalente Python validado.
3. Nenhuma funcionalidade nova da frente Python pode ser removida para "facilitar" a migração.
4. `Postgres` deve virar a fonte canônica do estado operacional.
5. `task-dag.json` deve virar espelho/projeção, nunca mais fonte primária.
6. `ai-orchestrator/scripts/v2` entra em freeze funcional: apenas bugfixes e compatibilidade até o sunset.

## Migration Phases

### Phase 0: Governance and Safety

- declarar árvore autoritativa
- congelar expansão do legado
- mapear paridade completa
- proteger recursos Python-only

### Phase 1: State Plane Consolidation

- Postgres canônico
- DAG como projeção
- readiness unificada
- lessons/incidents/whiteboard em DB

### Phase 2: Execution Plane Parity

- observer worker Python
- scheduler worker Python
- incident/repair flow Python
- external agent bridge Python

### Phase 3: Governance and Quality Parity

- mutation pipeline
- finops
- deploy verification
- pattern promotion
- output schema validation
- release pipeline

### Phase 4: Legacy Shutdown

- soak test
- failover test
- switch default runtime
- arquivar PowerShell

## How To Use This Folder

- `runtime-capability-matrix.md`: paridade funcional real, incluindo risco de perda.
- `python-control-plane-target.md`: arquitetura-alvo da nova frente.
- `migration-guardrails.md`: restrições para não degradar o produto.
- `migration-task-board.md`: backlog completo, dependências e critérios de aceite.
- `active-migration-tasks.md`: execução corrente.
- `powershell-deprecation-plan.md`: plano explícito de desligamento do legado.

## Immediate Execution Priority

As primeiras entregas obrigatórias são:

1. consolidar estado canônico em Postgres
2. endurecer bootstrap e config da frente Python
3. migrar observer/scheduler/readiness/incident loop
4. só então migrar finops/mutation/deploy/policies

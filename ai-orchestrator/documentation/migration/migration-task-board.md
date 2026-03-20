# Migration Task Board

## Status Legend

- `planned`
- `in-progress`
- `blocked`
- `completed`
- `deferred`

## Phase 0 - Governance and Safety

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P0-001 | P0 | completed | Formalizar Python como control plane canÃƒÂ´nico | - | evita terceira arquitetura | documentaÃƒÂ§ÃƒÂ£o publicada; freeze do legado definido |
| MIG-P0-002 | P0 | in-progress | Escolher ÃƒÂ¡rvore autoritativa e congelar `ai-orchestrator/scripts/v2` | MIG-P0-001 | reduz drift de runtime | ÃƒÂ¡rvore ÃƒÂºnica definida e documentada |
| MIG-P0-003 | P0 | in-progress | Consolidar resoluÃƒÂ§ÃƒÂ£o de `repo_root` e `orchestrator_root` | MIG-P0-002 | corrige path drift e registry failures | sem lookup quebrado para docs/agents e artifacts |
| MIG-P0-004 | P0 | completed | Remover bootstrap parcial silencioso da FastAPI | MIG-P0-002 | evita servidor subir quebrado | startup falha em ausÃƒÂªncia de mÃƒÂ³dulos crÃƒÂ­ticos |
| MIG-P0-005 | P0 | completed | Eliminar mÃƒÂ©tricas mockadas do dashboard | MIG-P0-004 | preserva observabilidade real | dashboard lÃƒÂª apenas fontes reais |
| MIG-P0-006 | P0 | completed | Criar suÃƒÂ­te de nÃƒÂ£o-regressÃƒÂ£o para features Python-only | MIG-P0-001 | protege cognitive/twin/simulate/ask | testes cobrindo runtime/rotas Python-only publicados |

## Phase 1 - State Plane Consolidation

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P1-001 | P0 | completed | Fazer Postgres a fonte canÃƒÂ´nica do task state | MIG-P0-003 | elimina split-brain DAG/DB | status/readiness/scheduler operam em Postgres e rotas centrais nao assumem mais `tasks.id` rigidamente |
| MIG-P1-002 | P0 | completed | Rebaixar `task-dag.json` para espelho read-only | MIG-P1-001 | evita dual-write destrutivo | DAG gerado por projeÃƒÂ§ÃƒÂ£o, nÃƒÂ£o por operaÃƒÂ§ÃƒÂ£o |
| MIG-P1-003 | P0 | completed | Migrar whiteboard/health/incident summary para DB-first | MIG-P1-001 | remove dependÃƒÂªncia de arquivos | APIs e workers usam DB |
| MIG-P1-004 | P1 | planned | Reescrever projeÃƒÂ§ÃƒÂ£o `docs/agents/*` a partir do state canÃƒÂ´nico | MIG-P1-002 | melhora coordenaÃƒÂ§ÃƒÂ£o humana | projeÃƒÂ§ÃƒÂµes sincronizadas pelo DB |
| MIG-P1-005 | P1 | planned | Unificar lessons learned e pattern memory em DB + projection | MIG-P1-001 | preserva memÃƒÂ³ria operacional | lessons consultÃƒÂ¡veis e exportÃƒÂ¡veis |

## Phase 2 - Execution Plane Parity

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P2-001 | P0 | completed | Implementar `observer_worker.py` com incident detection | MIG-P1-001 | substitui observer legado | incidentes gerados sem PowerShell |
| MIG-P2-002 | P0 | completed | Implementar `scheduler_worker.py` como scheduler oficial | MIG-P1-001 | substitui assignment loop legado | assign/claim/requeue em Python |
| MIG-P2-003 | P0 | completed | Integrar watchdog com incident/repair planner | MIG-P2-001, MIG-P2-002 | preservar self-healing | reclaims e REPAIRs no mesmo fluxo |
| MIG-P2-004 | P0 | completed | Implementar `readiness_worker.py` oficial | MIG-P1-001, MIG-P2-001 | remove readiness legado | `/health/deep` e readiness batem com runtime |
| MIG-P2-005 | P1 | completed | Migrar external agent bridge para Python | MIG-P2-002 | preservar hybrid execution | bridge externo DB-first, dispatch/completion compatÃ­veis, telemetry e E2E real validados |
| MIG-P2-008 | P0 | completed | Harden cognitive runtime init and reward feedback loop | MIG-P2-002 | preserve LangGraph, MCTS, memory layers and audit learning | single `_ensure_init`, single cognitive graph factory, agent completion publishes audit stream, reputation worker and entropy worker online, MCTS uses real fallback metrics |
| MIG-P2-006 | P1 | planned | Migrar intake universal para serviÃƒÂ§o Python | MIG-P1-001 | remove entrypoint legado | intake publicado por API/worker |
| MIG-P2-007 | P1 | planned | Migrar output schema validation para Python | MIG-P2-006 | preserva contrato dos agentes | validaÃƒÂ§ÃƒÂ£o acionada no fluxo padrÃƒÂ£o |
| MIG-P2-009 | P1 | completed | Introduce real parallel goal execution plane | MIG-P2-008 | avoid fake swarm integration | goals are decomposed into minimally dependent subtasks, persisted in DB, monitored by `goal_monitor`, and dependency-free slices are released immediately by the canonical scheduler |
| MIG-P2-010 | P2 | completed | Unify GoT and LangGraph graph-reasoning paths | MIG-P2-008 | avoid split cognitive routing and duplicated graph context logic | one canonical graph reasoning adapter selected, `cognitive_graph` and `prepare_execution_context()` both consume it, and successful LangGraph outcomes persist back into GoT |
| MIG-P2-011 | P0 | completed | Harden `agent_worker` execution semantics and post-task learning | MIG-P2-008, MIG-P4-001 | avoid silent host fallback and shallow post-task automation | Docker sandbox remains the default execution path, host fallback is explicit opt-in only, preflight includes `prepare_execution_context()`, and `self_reflect`/`memory_write` run automatically after execution |
| MIG-P2-012 | P0 | completed | Surface cognitive degradation explicitly in health/readiness | MIG-P2-008, MIG-P2-004 | avoid fail-open cognitive blind spots | `/health/deep` exposes a cognitive capability snapshot, deep health degrades on critical cognitive gaps, and readiness snapshots publish cognitive quality separately from operational counts |

## Phase 3 - Governance and Quality Parity

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P3-001 | P1 | completed | Implementar `mutation_worker.py` | MIG-P2-002 | nÃƒÂ£o perder mutation feedback | execuÃƒÂ§ÃƒÂ£o em cadÃƒÂªncia e reports reais |
| MIG-P3-002 | P1 | completed | Implementar policy engine Python para mutation / script validation | MIG-P3-001 | manter gates de qualidade | policy verdicts em DB/API |
| MIG-P3-003 | P1 | completed | Implementar `finops_worker.py` | MIG-P1-001 | nÃƒÂ£o perder controle de recursos | pause/resume/telemetry em Python |
| MIG-P3-004 | P1 | completed | Implementar `deploy_verify_worker.py` | MIG-P2-002 | nÃƒÂ£o perder deploy checks | deploy verification integrado ao runtime |
| MIG-P3-005 | P1 | completed | Implementar `pattern_promotion_worker.py` | MIG-P1-005 | preservar aprendizado e reuse | padrÃƒÂµes e lessons promovidos automaticamente |
| MIG-P3-006 | P1 | completed | Implementar `release_worker.py` / release pipeline | MIG-P3-004 | preservar governanÃƒÂ§a de entrega | gate de release funcional em Python |

## Phase 4 - Legacy Shutdown

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P4-001 | P1 | completed | Rodar E2E completo sem loop PowerShell | MIG-P2-004, MIG-P3-004 | valida paridade real | observe->schedule->execute->repair->close em Python |
| MIG-P4-002 | P1 | in-progress | Rodar soak test de 14 dias | MIG-P4-001 | estabilidade | soak harness publicado, execuÃ§Ã£o curta validada e janela longa em acompanhamento via profile `orchestrator-soak` sem fallback crÃƒÂ­tico ao legado |
| MIG-P4-003 | P2 | completed | Alterar deployment padrÃƒÂ£o para Python-only | MIG-P4-002 | efetivar migraÃƒÂ§ÃƒÂ£o | compose/bootstrap padrÃƒÂ£o sem loop PS |
| MIG-P4-004 | P2 | completed | Arquivar `scripts/v2` e `ai-orchestrator/scripts/v2` como legacy | MIG-P4-003 | reduzir complexidade | runtime oficial unico |

## Phase 5 - Platform Utilization Hardening

| Task ID | Priority | Status | Title | Depends On | Preserve / Risk Focus | Acceptance Criteria |
|---|---|---|---|---|---|---|
| MIG-P5-001 | P0 | completed | Padronizar outbound async HTTP no client resiliente compartilhado | MIG-P2-011 | remove clientes ad-hoc e trace drift | `health`, `plans`, `ask` e probes do dashboard usam `services.http_client` |
| MIG-P5-002 | P0 | completed | Promover dashboard template para macros/componentes Jinja2 | MIG-P4-003 | reduz template drift e JS incidental | dashboard usa macros reutilizaveis server-side |
| MIG-P5-003 | P0 | completed | Tornar `spawn_agent` orientado a eventos Redis Streams | MIG-P5-001 | elimina polling raso no join/fan-in | rotas de `tasks`/`agents` publicam lifecycle em Redis Streams, `spawn_agent` faz join stream-first por cursor e usa polling apenas como reconciliacao defensiva |
| MIG-P5-004 | P1 | in-progress | Converter diagnostics/logs para pipeline canonico baseado em stream | MIG-P5-003 | remove file-based observability | projeção de logs publica em `sinc:stream:diagnostic_logs`, queries usam stream/projection primeiro e fallback file-based permanece apenas como degradacao controlada |
| MIG-P5-005 | P1 | completed | Isolar sessoes Playwright por task/agente | MIG-P2-011 | evita vazamento de estado e cookies | `local_agent_runner` abre browser context efemero por execucao e fecha contexto/pagina de forma deterministica |
| MIG-P5-006 | P1 | in-progress | Reduzir `urllib` a bordas sync-only e remover duplicidade HTTP | MIG-P5-001 | aumenta resiliencia e tracing | `semantic_backend`, `local_agent_runner`, `alert_notifier`, `webhook_worker`, `orchestrator_client` e `memory_auditor` migraram para `httpx` resiliente compartilhado; `urllib` restante ficou concentrado em utilitarios legados isolados |
| MIG-P5-007 | P1 | planned | Aplicar graph intelligence na politica de execucao | MIG-P2-010 | evita grafo operar so como ranking | centrality/bottleneck alteram cautela e review policy |

## Execution Order

Ordem correta:

1. P0
2. P1
3. P2
4. P3
5. P4

NÃƒÂ£o inverter P2 e P1. Sem state plane canÃƒÂ´nico, o scheduler/observer Python vai nascer com os mesmos vÃƒÂ­cios do legado.



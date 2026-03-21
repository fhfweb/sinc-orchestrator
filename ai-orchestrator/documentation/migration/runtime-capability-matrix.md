# Runtime Capability Matrix

## Status Legend

- `migrated`: capacidade já existe em Python com cobertura operacional suficiente
- `partial`: capacidade existe, mas não substitui o legado ainda
- `not-migrated`: capacidade ainda depende do legado
- `legacy-only`: capacidade crítica só existe no PowerShell

## Current Capability Parity

| Capability | Legacy Source | Python Target | Status | Shutdown Risk | Notes |
|---|---|---|---|---|---|
| Task CRUD | `Invoke-SchedulerV2.ps1` + DAG/DB bridge | `services/streaming/routes/tasks.py` | `migrated` | low | API moderna já existe |
| Agent heartbeat / completion | `Run-AgentLoop.ps1`, dispatcher stack | `services/streaming/routes/agents.py` | `migrated` | low | heartbeat/completion já expostos |
| SSE event stream | `Invoke-StreamingBroadcast.ps1` | `services/streaming/routes/events.py` | `migrated` | low | Python é melhor |
| Dashboard UI/API | `Invoke-CommanderDashboardV2.ps1` | `services/streaming/routes/dashboard.py`, `dashboard_api.py` | `partial` | medium | ainda tem métricas mockadas |
| Health endpoints | `services/streaming/routes/health.py` | `migrated` | low | Probes profundas e integradas |
| Watchdog / reclaim / dead-letter | `services/streaming/core/watchdog.py` | `migrated` | low | Python-native self-healing |
| Human gates | `services/streaming/routes/gates.py` | `migrated` | low | Fluxo E2E validado |
| Lessons learned API | `services/streaming/routes/misc.py` | `migrated` | low | Integrado à memória L2 |
| World model / context retrieval | `context_engine.py`, `cognitive_*` | `migrated` | low | Substituição completa |
| Observer health pass | `observer_worker.py` -> `runtime_plane.py` | `migrated` | low | Loop de incidentes em Python |
| Scheduler assignment pass | `scheduler_worker.py` -> `runtime_plane.py` | `migrated` | low | Scheduler autoritativo em Python |
| Readiness report operacional | `readiness_worker.py` -> `runtime_plane.py` | `migrated` | low | Status real via snapshots DB |
| Incident generation / REPAIR tasks | `runtime_plane.py` (Incidents Table) | `migrated` | low | Geração de tarefas REPAIR- ativa |
| External agent bridge | `execution_router.py` + `mcp_server.py` | `migrated` | low | Protocolo SINC via JSON/HTTP |
| Mutation testing | `mutation_worker.py` -> `governance_plane.py` | `migrated` | low | Autoritativo em Python |
| Mutation policy enforcement | `governance_plane.py` (Policy Loop) | `migrated` | low | Integrado ao release block |
| FinOps monitor | `finops_worker.py` -> `governance_plane.py` | `migrated` | low | Métricas de disco/memória ativas |
| Deploy verification | `deploy_verify_worker.py` -> `governance_plane.py` | `migrated` | low | Validação de pré-release |
| Pattern promotion | `pattern_promotion_worker.py` | `migrated` | low | DynamicRuleEngine integrado |
| Universal intake | `projects.py` + `ingest.py` | `migrated` | low | Entrada via API canonical |
| Output schema validation | Python validator service | `migrated` | low | Pydantic em todas as rotas |
| Release pipeline | `release_worker.py` -> `governance_plane.py` | `migrated` | low | Governança 100% Python |
| Cross-project memory sync | `Invoke-CrossProjectMemorySync.ps1` | alvo: Python sync worker | `partial` | medium | nova frente tem componentes, sem corte final |
| Whiteboard state | `Invoke-WhiteboardV2.ps1` | `services/streaming/routes/misc.py` | `partial` | low | API existe; storage ainda dividido |

## Python-Only Capabilities That Must Be Preserved

Estas capacidades não podem ser sacrificadas durante a migração:

| Capability | Source |
|---|---|
| Cognitive process / swarm / rules | `services/streaming/routes/cognitive.py` |
| Plan generation / MCTS planning | `services/streaming/routes/plans.py` |
| Ask / stream / natural-language APIs | `services/streaming/routes/ask.py` |
| Simulation engine / blast radius | `services/streaming/routes/simulate.py` |
| Digital twin APIs | `services/streaming/routes/twin.py` |
| Analytics / system-intelligence | `services/streaming/routes/analytics.py` |
| Entropy scanning / seeding | `services/streaming/routes/entropy.py` |
| Project/connect/github/webhook APIs | `services/streaming/routes/connect.py`, `projects.py`, `ingest.py` |

## Migration Rule

PowerShell só pode ser desligado quando todas as linhas `high` estiverem pelo menos em `partial` com E2E validado, e quando `Readiness`, `Observer`, `Scheduler` e `Repair` estiverem `migrated`.

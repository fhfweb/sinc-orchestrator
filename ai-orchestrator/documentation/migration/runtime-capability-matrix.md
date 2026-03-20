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
| Health endpoints | `Invoke-ObserverV2.ps1`, `Invoke-ReadinessReportV2.ps1` | `services/streaming/routes/health.py` | `partial` | medium | health existe; readiness operacional ainda não |
| Watchdog / reclaim / dead-letter | scheduler self-heal + repair cleanup | `services/streaming/core/watchdog.py` | `migrated` | low | Python já está melhor aqui |
| Human gates | `Approve-HITLGate.ps1`, `Invoke-HITLGate.ps1` | `services/streaming/routes/gates.py` | `migrated` | low | falta validar fluxo E2E com workers |
| Lessons learned API | scheduler + lessons files | `services/streaming/routes/misc.py` | `partial` | medium | API existe; legado ainda alimenta muito da memória |
| World model / context retrieval | observer + memory sync | `context_engine.py`, `context_retriever.py`, `cognitive_*` | `partial` | medium | nova frente tem base, mas ainda há dependência do legado |
| Observer health pass | `Invoke-ObserverV2.ps1` | alvo: `observer_worker.py` | `legacy-only` | high | sem migração, perde incident detection madura |
| Scheduler assignment pass | `Invoke-SchedulerV2.ps1` | `runtime/agent_scheduler/scheduler.py` + worker dedicado | `partial` | high | existe scheduler Python, mas não é o loop autoritativo |
| Readiness report operacional | `Invoke-ReadinessReportV2.ps1` | alvo: `readiness_worker.py` | `legacy-only` | high | hoje o status real ainda vem do legado |
| Incident generation / REPAIR tasks | observer + scheduler | alvo: `incident_worker.py` + watchdog integration | `partial` | high | watchdog repara estado, não substitui incident planner |
| External agent bridge | `Invoke-ExternalAgentBridgeV2.ps1` | alvo: `execution_router.py` + dedicated bridge worker | `partial` | high | ainda sem substituição comprovada |
| Mutation testing | `Invoke-MutationTestingV2.ps1` | alvo: `mutation_worker.py` | `not-migrated` | high | perda direta se desligar legado |
| Mutation policy enforcement | `Invoke-MutationPolicyEnforcerV2.ps1` | alvo: policy service Python | `not-migrated` | high | depende do mutation pipeline |
| FinOps monitor | `Invoke-FinOpsMonitorV2.ps1` | alvo: `finops_worker.py` | `not-migrated` | high | sem paridade hoje |
| Deploy verification | `Invoke-DeployVerificationV2.ps1` | alvo: `deploy_verify_worker.py` | `not-migrated` | high | perda direta |
| Pattern promotion | `Invoke-PromotePatterns.ps1` | alvo: `pattern_promotion_worker.py` | `not-migrated` | medium | afeta aprendizado e reuse |
| Universal intake | `Invoke-UniversalIntakeV2.ps1` | alvo: intake service Python | `not-migrated` | high | fluxo de entrada ainda é legado |
| Output schema validation | `Invoke-OutputSchemaValidator.ps1` | alvo: Python validator service | `not-migrated` | high | afeta segurança contratual dos agentes |
| Release pipeline | `Invoke-ReleasePipelineV2.ps1` | alvo: release service Python | `not-migrated` | medium | governança de entrega ficaria cega |
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

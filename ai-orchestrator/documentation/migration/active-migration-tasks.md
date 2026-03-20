# Active Migration Tasks

## In Progress

### Phase 5 Hardening Follow-up

- Status: `completed`
- Owner: `Codex`
- Summary: remover bypass de RLS do writer canônico de reputação e supervisionar tarefas internas do hot path
- Artifacts:
  - `ai-orchestrator/services/background_tasks.py`
  - `ai-orchestrator/services/streaming/core/lifecycle.py`
  - `ai-orchestrator/services/reputation_engine.py`
  - `ai-orchestrator/services/memory_evolution.py`
  - `ai-orchestrator/services/streaming/tests/test_reputation_engine_hardening.py`
  - `ai-orchestrator/services/streaming/tests/test_memory_evolution_hardening.py`
- Progress:
  - `ReputationEngine` agora cancela `periodic_gds_update` e `drift_check` via registry explícito, em vez de `asyncio.create_task` órfão
  - `_update_postgres()` e `_check_reputation_drift()` deixaram de usar `bypass_rls=True`
  - eventos de reputação sem `tenant_id` deixaram de cair silenciosamente em `local`
  - o gatilho de distillation em `memory_evolution` agora usa registry compartilhado e evita job duplicado por tenant

### Phase 5 Hardening Follow-up 2

- Status: `completed`
- Owner: `Codex`
- Summary: renovar lease distribuída do GDS, ligar evidência real de CI ao `CodeValidatorAgent` e reduzir `urllib` nas bordas restantes
- Artifacts:
  - `ai-orchestrator/services/graph_intelligence.py`
  - `ai-orchestrator/services/code_validator_agent.py`
  - `ai-orchestrator/services/alert_notifier.py`
  - `ai-orchestrator/services/webhook_worker.py`
  - `ai-orchestrator/services/orchestrator_client.py`
  - `ai-orchestrator/services/memory_auditor.py`
  - `ai-orchestrator/services/local_agent_runner.py`
  - `ai-orchestrator/services/streaming/tests/test_graph_intelligence_hardening.py`
  - `ai-orchestrator/services/streaming/tests/test_code_validator_agent.py`
- Progress:
  - `run_reputation_gds()` agora mantém heartbeat de lease enquanto a projeção GDS roda
  - `CodeValidatorAgent` passou a produzir `ci_validation` a partir de ambiente CI e relatórios JUnit/JSON no workspace
  - `alert_notifier`, `webhook_worker`, `orchestrator_client` e `memory_auditor` migraram de `urllib` para o client HTTP resiliente compartilhado
  - o cabeçalho corrompido de `local_agent_runner.py` foi reestabilizado e o gate adaptativo do runner voltou a ficar verde

### Phase 5 Hardening Follow-up 3

- Status: `completed`
- Owner: `Codex`
- Summary: validar a tranche em runtime real, colocar fencing token observável no GDS e continuar a limpeza final de `urllib`
- Artifacts:
  - `ai-orchestrator/services/graph_intelligence.py`
  - `ai-orchestrator/services/agent_worker.py`
  - `ai-orchestrator/services/peer_review_agent.py`
  - `ai-orchestrator/services/github_connector.py`
  - `ai-orchestrator/services/maintenance.py`
  - `ai-orchestrator/services/streaming/tests/test_graph_intelligence_hardening.py`
  - `ai-orchestrator/documentation/migration/migration-task-board.md`
- Progress:
  - `run_reputation_gds()` agora emite `fence_token` quando a lease distribuída é adquirida e mantém renew/heartbeat compatível com fallback sem `eval`
  - `agent_worker`, `peer_review_agent`, `github_connector` e `maintenance` deixaram de usar `urllib` para outbound HTTP
  - smoke vivo do control plane passou em `127.0.0.1:8765` para create -> claim -> heartbeat -> complete -> readiness
  - smoke do batch cognitivo passou no contrato assíncrono (`batch_job_id`, `status=completed`)
  - o probe real do `CodeValidatorAgent` promoveu `ci_test_suite` a partir de um artefato JUnit local
  - o probe real de GDS retornou `gds_not_installed`, então o fencing token ficou validado na suíte e não no runtime desta stack

### MIG-P0-001

- Status: `completed`
- Owner: `Codex`
- Summary: formalizaÃ§Ã£o do programa de migraÃ§Ã£o, matriz de paridade e backlog de execuÃ§Ã£o
- Artifacts:
  - `ai-orchestrator/documentation/migration/README.md`
  - `ai-orchestrator/documentation/migration/runtime-capability-matrix.md`
  - `ai-orchestrator/documentation/migration/python-control-plane-target.md`
  - `ai-orchestrator/documentation/migration/migration-guardrails.md`
  - `ai-orchestrator/documentation/migration/migration-task-board.md`
  - `ai-orchestrator/documentation/migration/powershell-deprecation-plan.md`

## Ready To Start

### MIG-P0-002

- Status: `in-progress`
- Recommended owner: `Codex`
- Summary: declarar Ã¡rvore autoritativa e congelar `ai-orchestrator/scripts/v2`
- Primary files:
  - `ai-orchestrator/services/**/*`
  - `scripts/**/*`
  - `ai-orchestrator/scripts/v2/**/*`
- Progress:
  - freeze documentado em `ai-orchestrator/scripts/v2/LEGACY_RUNTIME_FREEZE.md`
  - uso de `ai-orchestrator/scripts/v2` assumido explicitamente como runtime legado ativo

### MIG-P0-003

- Status: `in-progress`
- Recommended owner: `Codex`
- Summary: consolidar path resolution e corrigir registry/artifact lookups
- Primary files:
  - `scripts/**/*`
  - `ai-orchestrator/services/**/*`
  - `docs/agents/**/*`
- Progress:
  - `scripts/Run-AgentLoop.ps1` agora resolve a Ã¡rvore V2 canÃ´nica dinamicamente
  - validadores/dispatchers passaram a usar `Get-V2RepoRoot`
  - `core_llm/agent_worker.py` deixou de calcular `repo_root` incorretamente

### MIG-P0-004

- Status: `completed`
- Owner: `Codex`
- Summary: remover bootstrap parcial silencioso do app FastAPI
- Primary files:
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/services/streaming_server_v2.py`
- Progress:
  - rotas criticas agora registram em modo fail-fast
  - startup valida `postgres` e `event_bus` antes de aceitar trafego
  - background tasks ficam supervisionadas e sao canceladas no shutdown

### MIG-P0-005

- Status: `completed`
- Owner: `Codex`
- Summary: eliminar mÃ©tricas mockadas do dashboard e plugar fontes reais
- Primary files:
  - `ai-orchestrator/services/streaming/routes/dashboard_api.py`
  - `ai-orchestrator/services/metrics_exporter.py`
  - `ai-orchestrator/services/event_store.py`
- Progress:
  - `dashboard_api.py` deixou de usar `random`
  - metricas de memoria, fleet, throughput e reputacao passaram a sair de Redis/Postgres/Neo4j/Qdrant
  - dashboard HTML deixou de renderizar fleet aleatoria e consome dados reais da API
  - painel canonico agora inclui `Task Debugger` consumindo `pipeline` real e `/api/v5/dashboard/task-debugger/{task_id}`

### MIG-P1-003

- Status: `in-progress`
- Owner: `Codex`
- Summary: migrar whiteboard/health/system snapshot para DB-first com projecoes explicitas
- Primary files:
  - `ai-orchestrator/services/streaming/core/state_plane.py`
  - `ai-orchestrator/services/streaming/routes/misc.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/services/metrics_exporter.py`
- Progress:
  - `state_plane.py` criado como adaptador DB-first para whiteboard e snapshot sistemico
  - `misc.py` deixou de usar `whiteboard.json` como fonte primaria
  - `system.py` deixou de depender de `task-dag.json` e `workload.json` para `/status`
  - `metrics_exporter.py` agora calcula `health_score` em modo DB-first
  - `whiteboard` e `/status` foram validados em `source=db`
  - `state_plane.py` agora tolera esquemas parciais (`heartbeats` sem `tenant_id`, ausencia de `loop_states`/`policy_reports`)

### MIG-P1-001

- Status: `in-progress`
- Owner: `Codex`
- Summary: fazer Postgres a fonte canÃ´nica do task state
- Primary files:
  - `ai-orchestrator/services/streaming/core/config.py`
  - `ai-orchestrator/services/streaming/core/db.py`
  - `ai-orchestrator/services/orchestrator_core.py`
  - `ai-orchestrator/services/metrics_exporter.py`
  - `ai-orchestrator/services/webhook_worker.py`
  - `ai-orchestrator/services/alert_notifier.py`
  - `ai-orchestrator/services/agent_worker.py`
- Progress:
  - descoberta de env/DB unificada em `streaming/core/config.py`
  - suporte a `ORCHESTRATOR_TASK_DB_*` com fallback para `ORCH_DB_*`
  - host/port agora resolvem alias Docker -> fallback local automaticamente fora do container
  - servicos Python criticos deixaram de carregar defaults de DB divergentes
  - `runtime_plane.py` passou a persistir `loop_states`, `policy_reports`, `incidents` e `readiness_reports` em Postgres
  - compute_readiness_snapshot(), scheduler_tick_once() e observer_tick_once() foram validados em smoke com source=db`r
  - rotas e servicos centrais (gents, dashboard_api, events, gates, dmin, lert_notifier) deixaram de assumir 	asks.id como chave operacional unica

### MIG-P1-002

- Status: `in-progress`
- Owner: `Codex`
- Summary: rebaixar `task-dag.json` para espelho read-only gerado do Postgres
- Primary files:
  - `ai-orchestrator/services/streaming/core/state_plane.py`
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
  - `ai-orchestrator/docker/.env.docker.generated`
- Progress:
  - `state_plane.py` agora gera `task-dag.json` diretamente do Postgres com `read_only=true` e `source=db-projection`
  - a gravaÃ§Ã£o da projeÃ§Ã£o passou a ser atÃ´mica para evitar arquivo truncado em fallback
  - o control plane canÃ´nico agora sobe `task_dag_projection_worker` embutido, separado dos workers operacionais
  - o provider compose passou a declarar explicitamente os knobs dessa projeÃ§Ã£o para manter compatibilidade controlada
  - `task-dag.json` foi validado em smoke local como `schema_version=projection-v1`, `source=db-projection` e `read_only=true`

### MIG-P0-006

- Status: `completed`
- Owner: `Codex`
- Summary: criar suÃƒÂ­te de nÃƒÂ£o-regressÃƒÂ£o para features Python-only
- Primary files:
  - `ai-orchestrator/services/streaming/tests/test_runtime_plane.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/tests/test_watchdog.py`

### MIG-P5-005

- Status: `completed`
- Owner: `Codex`
- Summary: isolar sessoes Playwright por task/agente no runner local
- Primary files:
  - `ai-orchestrator/services/local_agent_runner.py`
  - `ai-orchestrator/services/streaming/tests/test_local_agent_runner_tools.py`
- Progress:
  - `PlaywrightManager` passou a abrir `browser.new_context()` efemero por tool execution
  - fechamento de `page` e `context` ficou deterministico mesmo em excecao
  - toolings de screenshot, clique, type, scroll e acao semantica deixaram de compartilhar contexto implicito

### MIG-P5-003

- Status: `completed`
- Owner: `Codex`
- Summary: mover lifecycle de `spawn_agent` para Redis Streams com join stream-first
- Primary files:
  - `ai-orchestrator/services/streaming/core/task_lifecycle.py`
  - `ai-orchestrator/services/streaming/routes/tasks.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/local_agent_runner.py`
  - `ai-orchestrator/services/streaming/tests/test_local_agent_runner_tools.py`
  - `ai-orchestrator/services/streaming/tests/test_golden_flow.py`
- Progress:
  - `tasks` e `agents/completion` agora publicam eventos de lifecycle em `sinc:stream:task_lifecycle:{tenant}`
  - `spawn_agent` faz wait/join via `XREAD` cursor-first e cai para polling apenas como reconciliacao defensiva
  - o payload final do join agora expõe `lifecycle.mode`, `stream_name` e `reason`
  - regressao validada no runner e o fluxo dourado `tasks -> agents -> completion` permaneceu verde

### MIG-P5-004

- Status: `in-progress`
- Owner: `Codex`
- Summary: converter diagnostics/logs para stream/projection canonico
- Primary files:
  - `ai-orchestrator/services/streaming/routes/dashboard_api.py`
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/services/streaming/tests/test_dashboard_diagnostics_routes.py`
- Progress:
  - loop de projeção embutido passa a publicar entradas em `sinc:stream:diagnostic_logs`
  - consulta de diagnostics consome stream/projection primeiro e agrega `patterns`, `anomalies` e `recommendations` no backend
  - cobertura adicionada para path file-based e para path stream-first
  - fallback por arquivo ainda permanece como degradacao controlada, então a tarefa ainda nao esta 100% encerrada

### MIG-P5-006

- Status: `in-progress`
- Owner: `Codex`
- Summary: reduzir duplicidade HTTP/Qdrant/Ollama e empurrar `urllib` para bordas sync-only
- Primary files:
  - `ai-orchestrator/services/semantic_backend.py`
  - `ai-orchestrator/services/context_retriever.py`
  - `ai-orchestrator/services/memory_compaction.py`
  - `ai-orchestrator/scripts/verify_otel_export.py`
- Progress:
  - cliente semantico compartilhado publicado para embedding/Qdrant (`semantic_backend.py`)
  - `context_retriever.py` e `memory_compaction.py` deixaram de manter implementacoes HTTP copiadas para embeddings, search, scroll e upsert
  - `verify_otel_export.py` agora valida export real por artifact do collector e cai para `/otel/probe` quando o SDK local nao esta operacional
  - `local_agent_runner.py` migrou o hot path restante de `urlopen` para `httpx` resiliente compartilhado, incluindo Ollama browser-semantic fallback e chamadas do orquestrador
  - os testes do runner foram realinhados para o contrato novo via `_orchestrator_json_request`, `_embed_text`, `_search_qdrant` e `_upsert_qdrant`
  - `urllib` restante ficou restrito a bordas sync antigas fora do runner quente
- Progress:
  - cobertura inicial publicada para compatibilidade de schema (`task_id` vs `id`)
  - cobertura inicial publicada para dispatch do scheduler em Postgres com schemas parciais
  - cobertura publicada para reclaim cycle do watchdog contra o runtime novo
  - `python -m pytest ...test_runtime_plane.py ...test_watchdog.py` validado com 6 testes verdes

### MIG-P2-001

- Status: `in-progress`
- Owner: `Codex`
- Summary: implementar `observer_worker.py` com incident detection
- Primary files:
  - `ai-orchestrator/services/observer_worker.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/services/streaming/routes/misc.py`
- Progress:
  - `observer_worker.py` publicado como entrypoint Python dedicado
  - `observer_tick_once()` calcula readiness DB-first, persiste snapshot e registra incidentes com cooldown
  - `/observer/run`, `/readiness` e `/incidents` expostos pela API nova
  - smoke local validou `observer_status=ok` sem fallback ao legado

### MIG-P2-002

- Status: `in-progress`
- Owner: `Codex`
- Summary: implementar `scheduler_worker.py` como scheduler oficial
- Primary files:
  - `ai-orchestrator/services/scheduler_worker.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
- Progress:
  - `scheduler_worker.py` publicado como entrypoint Python dedicado
  - `scheduler_tick_once()` promove `blocked-deps`, atribui agente e cria `webhook_dispatches` em Postgres
  - o scheduler novo opera com compatibilidade de schema (`task_id`/`id`, tabelas sem `tenant_id`)
  - smoke local validou `scheduler_status=ok`

### MIG-P2-003

- Status: `in-progress`
- Owner: `Codex`
- Summary: integrar watchdog com incident/repair planner
- Primary files:
  - `ai-orchestrator/services/streaming/core/watchdog.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/tests/test_watchdog.py`
- Progress:
  - watchdog migrado para compatibilidade de schema (`task_id`/`id`, heartbeat timestamp dinÃƒÂ¢mico)
  - reclaim e dead-letter agora registram incidentes DB-first
  - dead-letter e stale recovery recorrente agora seedam `REPAIR-*` deduplicado no Postgres
  - regressÃƒÂ£o do watchdog validada em teste
  - `EventBus` ganhou compatibilidade explÃ­cita com `publish`, `ack`, `read_group`, `auto_claim` e iterador SSE para fechar o contrato do runtime Python
  - erro de runtime `watchdog_stream_reclaim_error` foi eliminado na stack oficial apÃ³s recriaÃ§Ã£o do compose

### MIG-P2-004

- Status: `in-progress`
- Owner: `Codex`
- Summary: implementar `readiness_worker.py` oficial
- Primary files:
  - `ai-orchestrator/services/readiness_worker.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/services/streaming/__init__.py`
- Progress:
  - `readiness_tick_once()`, `get_latest_readiness_snapshot()` e `run_readiness_loop()` publicados
  - `readiness_worker.py` publicado como entrypoint dedicado
  - FastAPI agora sobe `readiness_worker` embutido por env
  - `/readiness`, `/readiness/live` e `/readiness/run` expostos pela API nova
  - smoke local validou `readiness_tick=ok` e `readiness_latest_source=db`
  - E2E Python-only validado no Docker oficial: `create -> scheduler -> claim -> heartbeat -> complete -> readiness`

### MIG-P2-005

- Status: `completed`
- Owner: `Codex`
- Summary: migrar external agent bridge para Python
- Primary files:
  - `ai-orchestrator/services/streaming/core/external_agent_bridge.py`
  - `ai-orchestrator/services/external_agent_bridge_worker.py`
  - `ai-orchestrator/services/streaming/routes/tasks.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/services/streaming/core/watchdog.py`
- Progress:
  - `TaskCreate` e `TaskUpdate` passaram a persistir metadata de roteamento (`execution_mode`, `preferred_agent`, `runtime_engine`, `files_affected`, `preflight_path`)
  - `/tasks/{task_id}/status` e `/tasks/{task_id}/replay` foram publicados para fechar o contrato usado por workers Python/HTTP
  - `external_agent_bridge.py` publicado com dispatch artifact compatÃ­vel com o contrato legado e processamento DB-first de completions
  - FastAPI agora sobe `external_agent_bridge_worker` embutido por env e expÃµe `/external-bridge/run` e `/external-bridge/status`
  - watchdog deixou de reciclar `delivered` de tasks roteadas pelo bridge externo
  - suÃ­te de regressÃ£o publicada para dispatch/completion/conn reuse
  - E2E real validado em Postgres local: `pending -> delivered -> completion artifact -> done`

### MIG-P2-008

- Status: `completed`
- Owner: `Codex`
- Summary: hardening do motor cognitivo e fechamento do feedback loop de reputacao/MCTS
- Primary files:
  - `ai-orchestrator/services/cognitive_orchestrator.py`
  - `ai-orchestrator/services/cognitive_graph.py`
  - `ai-orchestrator/services/mcts_planner.py`
  - `ai-orchestrator/services/reputation_engine.py`
  - `ai-orchestrator/services/reputation_worker.py`
  - `ai-orchestrator/services/entropy_worker.py`
  - `ai-orchestrator/services/agent_worker.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/streaming/routes/core_compat.py`
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
- Progress:
  - `CognitiveOrchestrator` ficou com apenas um `_ensure_init()` valido e voltou a marcar `_initialized`
  - `cognitive_graph.py` ficou com uma unica factory/compile path, sem bloco morto duplicado
  - `agent_worker.py` passou a injetar `prepare_execution_context()` no preflight real dos agentes
  - completions agora publicam evento de auditoria consistente (`complete`) e alimentam leaderboard/hash de reputacao
  - `ReputationEngine` foi redesenhada para consumir `sinc:stream:audit` e atualizar Redis + `agent_reputation` em vez de tentar escrever na materialized view
  - `MCTSPlanner` passou a usar fallback `agent:all` e reputacao real quando a view historica ainda nao tem amostras
  - `orchestrator-reputation` e `orchestrator-entropy` entraram na stack oficial
  - `orchestrator-ingest-worker` saiu do crash loop de import e teve o wiring Redis alinhado ao compose oficial
  - auditoria tecnica confirmou que `agent_swarm.py` e um scheduler heuristico, nao um executor paralelo por `asyncio.gather`; o plano correto de paralelismo ficou separado em `MIG-P2-009`

## Platform Utilization Hardening

### MIG-P5-001

- Status: `completed`
- Owner: `Codex`
- Summary: padronizar outbound async HTTP no client resiliente compartilhado
- Primary files:
  - `ai-orchestrator/services/http_client.py`
  - `ai-orchestrator/services/streaming/routes/health.py`
  - `ai-orchestrator/services/streaming/routes/plans.py`
  - `ai-orchestrator/services/streaming/routes/ask.py`
  - `ai-orchestrator/services/streaming/routes/dashboard_api.py`
  - `ai-orchestrator/services/cognitive_graph.py`
- Progress:
  - `http_client.py` deixou de ser um stub e passou a injetar `trace_id`, `User-Agent`, identidade do servico e logging de latencia
  - probes do `health`, chamadas do `plan`, hot path do `ask` para Ollama e probes Qdrant do dashboard deixaram de abrir `httpx.AsyncClient()` ad-hoc
  - `cognitive_graph.py` deixou de cair para client inline sem identidade de servico

### MIG-P5-002

- Status: `completed`
- Owner: `Codex`
- Summary: promover dashboard template para macros/componentes Jinja2
- Primary files:
  - `ai-orchestrator/services/streaming/templates/dashboard.html`
  - `ai-orchestrator/services/streaming/templates/_dashboard_macros.html`
- Progress:
  - o dashboard agora usa macros server-side para `nav_item`, `stat_card` e `glass_card`
  - a camada Jinja2 deixou de ser apenas um arquivo HTML estatico servido pelo FastAPI

### MIG-P2-009

- Status: `completed`
- Owner: `Codex`
- Summary: introduzir um plano de execucao paralela real para goals/subtasks sem acoplar ao scheduler heuristico `agent_swarm.py`
- Primary files:
  - `ai-orchestrator/services/goals_orchestrator.py`
  - `ai-orchestrator/services/streaming/core/goal_monitor.py`
  - `ai-orchestrator/services/streaming/routes/plans.py`
  - `ai-orchestrator/services/streaming/routes/intelligence.py`
  - `ai-orchestrator/services/streaming/tests/test_goals_orchestrator.py`
- Progress:
  - auditoria do codigo mostrou que `agent_swarm.py` so faz affinity scheduling e rebalance
  - nao existe `asyncio.gather` nem runtime de subagentes naquele modulo
  - a integracao correta de paralelismo nasceu do execution plane/goal plane, nao de um falso "toggle" em `agent_swarm`
  - `goals_orchestrator.py` passou a decompor objetivos em subtasks minimamente dependentes e persisti-las em `goals`/`plans`/`tasks`/`dependencies`
  - o endpoint `/intelligence/goals` passou a usar o contrato canonico (`goal`, `context`, `acceptance_criteria`, `constraints`) e retorna o plano completo
  - `goal_monitor.py` foi endurecido para o schema real e segue acompanhando/adaptando o goal sem assumir estados legados
  - o scheduler canonico e acionado imediatamente apos o seed do goal para liberar slices independentes em paralelo
  - regressao publicada em `test_goals_orchestrator.py` para preservar ramificacoes paralelas e o contrato da rota

### MIG-P2-010

- Status: `completed`
- Owner: `Codex`
- Summary: unificar GoT e LangGraph em um caminho canônico de raciocinio sobre grafo
- Primary files:
  - `ai-orchestrator/services/graph_reasoning_adapter.py`
  - `ai-orchestrator/services/cognitive_graph.py`
  - `ai-orchestrator/services/cognitive_orchestrator.py`
  - `ai-orchestrator/services/streaming/tests/test_cognitive_hardening.py`
- Progress:
  - `graph_reasoning_adapter.py` publicado como contrato unico para GraphRAG estrutural + GoT
  - `graph_reasoning_node()` deixou de misturar chamadas ad hoc e agora consome apenas o adapter canônico
  - `prepare_execution_context()` passou a usar o mesmo adapter, eliminando drift entre o preflight do agente e o motor LangGraph
  - solucoes aprovadas do LangGraph agora persistem de volta no GoT em `learn_and_store_node()`, fechando o ciclo de memoria estrutural
  - `DEFAULT_MAX_STEPS` foi restaurado em `cognitive_graph.py`, corrigindo o import quebrado em `execution_router.py`
  - validacao focada verde: `11 passed`

### MIG-P2-011

- Status: `completed`
- Owner: `Codex`
- Summary: endurecer o `agent_worker` e tornar o pos-task learning parte do hot path real
- Primary files:
  - `ai-orchestrator/services/agent_worker.py`
  - `ai-orchestrator/services/local_agent_runner.py`
  - `ai-orchestrator/services/streaming/tests/test_agent_worker_hardening.py`
  - `ai-orchestrator/services/streaming/tests/test_local_agent_runner_tools.py`
- Progress:
  - fallback de execucao no host deixou de ser implicito e ficou sob opt-in explicito
  - `prepare_execution_context()` passou a entrar no preflight real do worker
  - `self_reflect` e `memory_write` agora rodam automaticamente no pos-task
  - o runner ganhou `AUTONOMY DOSSIER`, politica adaptativa por tipo de task e gate por risco
  - `spawn_agent` deixou de ser apenas submit remoto e agora suporta review paralelo e consenso no fan-in
  - validacao focada verde: `17 passed`

### MIG-P2-012

- Status: `completed`
- Owner: `Codex`
- Summary: expor degradacao cognitiva de forma explicita em health/readiness e validar o comportamento no runtime real
- Primary files:
  - `ai-orchestrator/services/cognitive_orchestrator.py`
  - `ai-orchestrator/services/streaming/routes/health.py`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/tests/test_health_cognitive_quality.py`
  - `ai-orchestrator/scripts/e2e_python_control_plane.py`
  - `ai-orchestrator/scripts/e2e_cognitive_batch.py`
- Progress:
  - `get_cognitive_capability_snapshot()` voltou a existir como compat helper e ganhou variante assincra `get_cognitive_capability_snapshot_async()`
  - `health/deep` e `compute_readiness_snapshot()` passaram a usar o caminho assincro real para inicializar/inspecionar o orquestrador cognitivo dentro do loop
  - `readiness` deixou de degradar apenas porque o snapshot estava sendo lido pelo helper sincrono dentro de contexto assincrono
  - `e2e_cognitive_batch.py` foi atualizado para o contrato assincro novo (`202 Accepted` + polling por `batch_job_id`)
  - runtime oficial validado novamente apos rebuild da imagem canônica:
    - `/health` -> `200`
    - `e2e_python_control_plane.py` -> verde com `readiness.status=ready`, `health=ok`, `cognitive_status=full`
    - `e2e_cognitive_batch.py` -> verde com `status=completed`, `cache_hits=2`
  - OTEL teve prova explicita de export via `POST /otel/probe` seguido de novo batch de traces no `sinc-otel-collector`

## Locks

No momento, os locks humanos desta frente de migraÃ§Ã£o sÃ£o:

| Resource | Locked By | Purpose |
|---|---|---|
| `ai-orchestrator/documentation/migration/*` | Codex | governanÃ§a e backlog de migraÃ§Ã£o |
| `ai-orchestrator/ROADMAP.md` | Codex | alinhamento do roadmap com o programa de transiÃ§Ã£o |

### MIG-P4-003

- Status: `completed`
- Owner: `Codex`
- Summary: alterar deployment padrao para Python-only
- Primary files:
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
  - `ai-orchestrator/docker/docker-compose.client.yml`
  - `ai-orchestrator/sdk/docker/docker-compose.client.yml`
  - `ai-orchestrator/sdk/client_loop.py`
  - `ai-orchestrator/services/orchestrator_core.py`
  - `ai-orchestrator/README.md`
- Progress:
  - `orchestrator-core` removido da stack oficial
  - `peer-review-agent` repontado para `orchestrator-streaming:8765`
  - template cliente trocado para `sdk/client_loop.py`
  - template SDK cliente corrigido para o mesmo loop Python oficial
  - `services/orchestrator_core.py` marcado explicitamente como modulo legado
  - a stack oficial deixou de depender do loop PowerShell para execucao padrao

### MIG-P4-001

- Status: `completed`
- Owner: `Codex`
- Summary: validar E2E Python-only na stack oficial sem loop PowerShell
- Primary files:
  - `ai-orchestrator/scripts/e2e_python_control_plane.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/streaming/routes/core_compat.py`
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
- Progress:
  - `orchestrator-streaming` recriado na stack oficial apos bake das imagens canônicas
  - E2E real validado em `http://127.0.0.1:8765`: `health -> task create -> scheduler run -> claim -> heartbeat -> complete -> readiness run`
  - compatibilidade de `heartbeats` ficou adaptativa ao schema vivo do Postgres
  - contrato legado de `/tasks/complete` passou a normalizar `done/completed` para `success` sem quebrar a modelagem canônica
  - regressao de schema coberta por `test_agents_schema_compat.py`
  - incidentes sintéticos (`runtime-readiness`, `watchdog-stale-recovery`) agora são reconciliados automaticamente e o tenant `local` voltou a `status=ready`, `health=ok`

### MIG-P4-002

- Status: `in-progress`
- Owner: `Codex`
- Summary: rodar soak test controlado e preparar janela longa de observação sem fallback legado
- Primary files:
  - `ai-orchestrator/scripts/soak_python_control_plane.py`
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
  - `ai-orchestrator/services/streaming/core/runtime_plane.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/sdk/docker/docker-compose.client.yml`
  - `ai-orchestrator/docker/docker-compose.client.yml`
  - `ai-orchestrator/services/streaming/tests/test_runtime_plane.py`
- Progress:
  - `POST /incidents/reconcile` publicado para limpeza controlada de incidentes sintéticos sem perder histórico
  - `runtime-readiness` deixou de se autoalimentar na própria contagem de incidentes
  - `watchdog-stale-recovery` passa a fechar sozinho quando a task não está mais `in-progress`
  - soak harness publicado e validado com 5 ciclos seguidos sem falha na stack Docker oficial
  - `orchestrator-soak` publicado como profile do compose oficial para observação longa fora da sessão interativa
  - distribuição de SDK corrigida para servir assets aninhados do diretório canônico
  - templates cliente passaram a usar `docker compose build` com `.orchestrator-sdk/Dockerfile.*`, eliminando `pip install` e `apt-get` no startup
  - resumo do smoke de soak:
    - `failures=0`
    - `observer p95 ~= 69.44ms`
    - `scheduler p95 ~= 23.27ms`
    - `readiness p95 ~= 65.87ms`

### MIG-P4-004

- Status: `completed`
- Owner: `Codex`
- Summary: arquivar legado restante e eliminar reativacao acidental do runtime PowerShell
- Primary files:
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
  - `ai-orchestrator/docker/docker-compose.n5.yml`
  - `scripts/v2/**/*`
  - `ai-orchestrator/scripts/v2/**/*`
  - `ai-orchestrator/services/orchestrator_core.py`
  - `ai-orchestrator/services/metrics_exporter.py`
  - `docs/agents/dashboard.html`
- Progress:
  - `docker-compose.n5.yml` foi neutralizado e arquivado fora da linha oficial
  - `orchestrator-core` e `heartbeat` foram removidos da stack oficial com `--remove-orphans`
  - dashboard can??nico parou de disparar requests sem API key v??lida; agora exige chave real antes de iniciar polling/SSE
  - `scripts/v2` foi arquivado fisicamente em `legacy/powershell-v2/scripts-v2` e substituido por tombstones deprecados
  - `ai-orchestrator/scripts/v2` foi reduzido a um `README` de tombstone para impedir reativacao acidental
  - `ai-orchestrator/services/orchestrator_core.py` foi arquivado como modulo legado sob `legacy/python-provider/orchestrator_core.py`
  - `Start-StreamingServer.py` agora exige opt-in explicito via `ALLOW_LEGACY_START_STREAMING_SERVER=1`
  - `New-DockerFactory.ps1` deixou de gerar o dashboard Flask legado
  - onboarding e docs de contexto foram reescritos para o runtime Python canonico
  - `docker-compose.orchestrator.yml` executa workers Python dedicados para scheduler, observer, readiness, external bridge e governanca
  - `metrics_exporter.py` foi migrado de Flask dev server para FastAPI + uvicorn na trilha oficial
  - `sinc-orchestrator-metrics` foi recriado com sucesso; `/metrics` responde `200` e `/health` responde `503` apenas por estado operacional `critical`, nao por falha do exporter
  - stack oficial Docker foi validada com todos os servicos canonicos em `Up`

### MIG-P3-001 .. MIG-P3-006

- Status: `in-progress`
- Owner: `Codex`
- Summary: publicar a camada oficial de governanÃ§a/qualidade no runtime Python
- Primary files:
  - `ai-orchestrator/services/streaming/core/governance_plane.py`
  - `ai-orchestrator/services/policy_worker.py`
  - `ai-orchestrator/services/mutation_worker.py`
  - `ai-orchestrator/services/finops_worker.py`
  - `ai-orchestrator/services/deploy_verify_worker.py`
  - `ai-orchestrator/services/pattern_promotion_worker.py`
  - `ai-orchestrator/services/release_worker.py`
  - `ai-orchestrator/services/streaming/routes/system.py`
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/services/dynamic_rules.py`
- Progress:
  - workers oficiais publicados para `policy`, `mutation`, `finops`, `deploy verify`, `pattern promotion` e `release`
  - bootstrap FastAPI agora consegue subir esses workers por env, mas o compose oficial roda workers dedicados e desabilita a execuÃ§Ã£o embutida para evitar duplicidade
  - novas rotas de controle/status publicadas em `/policy`, `/mutation`, `/finops`, `/deploy-verify`, `/pattern-promotion`, `/release`
  - `dynamic_rules` foi alinhado ao schema real e `pattern_promotion` passou a rodar sem depender do pool global
  - `docker/.env.docker.generated` agora expÃµe intervals, flags embedded e knobs de mutation/release da trilha canÃ´nica
  - smoke real contra Postgres local validou:
    - `policy -> ok`
    - `mutation -> not-configured`
    - `finops -> ok`
    - `deploy -> ok`
    - `pattern -> ok`
    - `release -> blocked`
  - `release=blocked` Ã© veredito operacional atual, nÃ£o falha de runtime

### Audit v3 hardening

- Status: `completed`
- Owner: `Codex`
- Summary: corrigir gaps residuais reais da auditoria v3 sem reimplementar funcionalidades que jÃ¡ estavam vivas
- Primary files:
  - `ai-orchestrator/services/streaming/core/redis_.py`
  - `ai-orchestrator/services/reputation_engine.py`
  - `ai-orchestrator/services/reputation_worker.py`
  - `ai-orchestrator/services/global_confidence.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/streaming/__init__.py`
  - `ai-orchestrator/services/otel_setup.py`
  - `ai-orchestrator/docker/docker-compose.orchestrator.yml`
  - `ai-orchestrator/docker/otel-collector-config.yaml`
  - `ai-orchestrator/services/streaming/routes/dashboard.py`
- Progress:
  - reputaÃ§Ã£o Redis passou a ser tenant-scoped na escrita nova, com leitura backward-compatible dos hashes legados
  - `reputation_worker` deixou de hardcodar `tenant_id='local'` e agora respeita `ORCH_TENANT_ID` / `ORCHESTRATOR_TENANT_ID` / `TENANT_ID`
  - `global_confidence` passou a ler score tenant-aware, reduzindo risco de mistura entre projetos
  - `reputation_engine` agora atualiza `semantic_score` e `reputation_fit_score` a partir do sinal Redis vivo
  - `process_batch` foi validado como rota jÃ¡ existente em `/cognitive/batch`; o gap da auditoria estava desatualizado
  - `otel-collector` foi reintroduzido na stack oficial com config local e bootstrap de tracing no control plane + workers principais
  - dashboard canÃ´nico continuou no estilo N5 de referÃªncia e recebeu teste para evitar retorno de mojibake no HTML servido

### Phase 5 hardening follow-up

- Status: `in-progress`
- Owner: `Codex`
- Summary: consolidar writers de reputacao, colocar lease distribuida no GDS e endurecer promocao de memoria verificada
- Primary files:
  - `ai-orchestrator/services/reputation_engine.py`
  - `ai-orchestrator/services/graph_intelligence.py`
  - `ai-orchestrator/services/code_validator_agent.py`
  - `ai-orchestrator/services/memory_evolution.py`
  - `ai-orchestrator/services/context_retriever.py`
  - `ai-orchestrator/services/streaming/routes/agents.py`
  - `ai-orchestrator/services/streaming/__init__.py`
- Progress:
  - a rota de completion deixou de escrever Redis/Postgres de reputacao diretamente; agora publica apenas `audit` e a `ReputationEngine` virou o writer canonico
  - `memory_evolution` parou de atualizar `agent_reputation`, removendo a terceira trilha concorrente de score
  - `leaderboard_flush` embutido no app foi desabilitado por default (`ORCHESTRATOR_EMBEDDED_LEADERBOARD_FLUSH_ENABLED=0`) para nao competir com a trilha canonica
  - `graph_intelligence.run_reputation_gds()` ganhou lease distribuida em Redis, cooldown cluster-wide e refresh de projection persistente em vez de rebuild concorrente por processo
  - `graph_intelligence` agora implementa `sync_task_dependency()`, fechando um drift do sync de grafo
  - a promocao de memoria `verified` passou a depender de `CodeValidatorAgent` / CI / review confiavel; heuristica de sintaxe nao promove mais memoria
  - `ContextRetriever.store_solution()` agora aceita `verified` e `metadata`, permitindo persistir o gate de verificacao corretamente


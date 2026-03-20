# Orchestrator Library Utilization Audit

Date: 2026-03-19
Scope: `ai-orchestrator/services`, `ai-orchestrator/docker`, dashboard runtime

## Executive Summary

The orchestrator is no longer weak on core runtime plumbing. The main gap is that several strong dependencies are still used as thin wrappers instead of as force multipliers.

The pattern repeats across the codebase:

1. the library is installed and partially integrated
2. one or two advanced features are used
3. the hot path still falls back to ad-hoc calls, shallow abstractions, or duplicated logic

This audit separates:

- already in use, but underutilized
- present but inconsistently adopted
- still missing from the hot path

## Inventory

### Redis

Current usage:

- durable event emission via Streams and real-time Pub/Sub in [event_bus.py](../services/event_bus.py)
- dashboard config and metrics in Redis keys in [dashboard_api.py](../services/streaming/routes/dashboard_api.py)
- watchdog Pub/Sub listener in [watchdog_service.py](../services/watchdog_service.py)

Underutilization:

- sub-agent lifecycle still relies on task polling instead of event-driven join/fan-in
- diagnostics/logs are still file-based instead of stream-backed
- no canonical DLQ policy for failed consumers
- dashboard feed pagination is still query-window based, not stream-cursor based

Priority:

- P0: event-driven `spawn_agent` join/fan-in over Streams
- P1: convert diagnostics/log pipeline to Redis Stream + persistent projection
- P1: add DLQ/retry budget for critical consumers

### FastAPI

Current usage:

- REST control plane across `services/streaming/routes/*`
- WebSocket telemetry in [dashboard_api.py](../services/streaming/routes/dashboard_api.py)
- `BackgroundTasks` in `ask`, `agents`, `misc`, `core_compat`

Underutilization:

- several background operations still run as manual tasks or ad-hoc loops instead of explicit job semantics
- WebSocket tenant/auth handshake is still simplified
- many diagnostics remain request/response oriented rather than live operational channels

Priority:

- P1: formal job semantics for long-running background actions
- P1: authenticated WebSocket handshake and richer live diagnostics

### Neo4j and Graph Intelligence

Current usage:

- graph retrieval and centrality-aware context in [context_retriever.py](../services/context_retriever.py)
- graph intelligence and GDS fallback logic in [graph_intelligence.py](../services/graph_intelligence.py)
- MCTS uses graph metrics in [mcts_planner.py](../services/mcts_planner.py)

Underutilization:

- graph metrics still influence scoring more than execution policy
- no live bottleneck scoring for goal plans or incident response paths
- no consensus/routing policy driven by graph authority in the control plane

Priority:

- P1: inject graph risk/authority into execution policy, not only scoring
- P2: use centrality/bottleneck signals for adaptive planning and critical review routing

### HTTPX

Current usage:

- pooled client in [cognitive_orchestrator.py](../services/cognitive_orchestrator.py)
- agent worker uses a shared client in [agent_worker.py](../services/agent_worker.py)
- several routes still instantiate ad-hoc clients

Underutilization:

- outbound HTTP still mixes `httpx` and `urllib`
- trace propagation and latency logging were inconsistent across routes
- no single standard for service identity on outbound calls

Priority:

- P0: standardize outbound async HTTP on the shared resilient client
- P1: shrink `urllib` usage to legacy or sync-only edges

### Jinja2

Current usage:

- dashboard served via [dashboard.py](../services/streaming/routes/dashboard.py)
- static template in `services/streaming/templates/dashboard.html`

Underutilization:

- template had no macros/components
- server-side composition was thin despite repeated UI structure

Priority:

- P0: convert dashboard template to macro-based components
- P1: move repeated diagnostics/feed cards into reusable server-side components

### Tree-sitter

Current usage:

- structured AST analysis in [ast_analyzer.py](../services/ast_analyzer.py)

Underutilization:

- still uneven by language
- generic fallback remains regex-heavy in the runner

Priority:

- P1: expand parser coverage before adding more heuristic analysis features

### Playwright

Current usage:

- singleton browser manager in [local_agent_runner.py](../services/local_agent_runner.py)

Underutilization:

- session isolation is not task-scoped
- current design is good for convenience, weak for safety and repeatability

Priority:

- P1: isolate browser context per task/agent
- P2: integrate visual validation into frontend and QA policy

## Current Hot Spots

These files still carry the biggest utilization debt:

- [local_agent_runner.py](../services/local_agent_runner.py)
- [agent_worker.py](../services/agent_worker.py)
- [dashboard_api.py](../services/streaming/routes/dashboard_api.py)
- [ask.py](../services/streaming/routes/ask.py)
- [health.py](../services/streaming/routes/health.py)

## Tranche Applied In This Round

Completed:

- standardized async outbound HTTP around the shared resilient client
- replaced ad-hoc `httpx.AsyncClient()` usage in `health`, `plans`, `ask`, and dashboard qdrant probes
- upgraded the dashboard template to use Jinja2 macros/components

Files changed in this tranche:

- [http_client.py](../services/http_client.py)
- [health.py](../services/streaming/routes/health.py)
- [plans.py](../services/streaming/routes/plans.py)
- [ask.py](../services/streaming/routes/ask.py)
- [dashboard_api.py](../services/streaming/routes/dashboard_api.py)
- [dashboard.html](../services/streaming/templates/dashboard.html)
- [_dashboard_macros.html](../services/streaming/templates/_dashboard_macros.html)

## Recommended Next Backlog

### P0

- move sub-agent join/fan-in from polling to Redis Stream events
- remove host/subprocess fallbacks from the remaining hot paths
- make diagnostics/logs stream-backed instead of file-backed

### P1

- isolate Playwright sessions per task
- deepen active memory compaction and causal reactivation
- make graph intelligence change runtime policy, not only ranking
- reduce `urllib` usage to non-critical sync-only boundaries

### P2

- critical-action consensus
- pair execution / parallel review as a first-class primitive
- visual validation pipeline for frontend and QA

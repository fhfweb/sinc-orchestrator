# Roadmap — SINC AI Orchestrator

> Last updated: 2026-03-19
> Items are labelled: **[P0]** ship-blocker · **[P1]** next cycle · **[P2]** improvement

---

## Runtime Migration Program (M)

| ID | Item | Status | Notes |
|---|---|---|---|
| M-1 | Formalize Python as the canonical control plane | Done | See `documentation/migration/README.md` |
| M-2 | Freeze PowerShell legacy runtime to bugfix-only mode | [P0] | `ai-orchestrator/scripts/v2` remains active only until parity is complete |
| M-3 | Consolidate Postgres as canonical task state | [P0] | `status`, `whiteboard`, `readiness`, `scheduler` and `observer` smoke already run DB-first; core routes/services now tolerate `task_id`/`id`, but dual-write cutoff is still pending |
| M-4 | Migrate observer/scheduler/readiness/repair loop to Python workers | [P0] | `observer_worker.py`, `scheduler_worker.py`, `readiness_worker.py` and the external bridge Python are live; watchdog seeds incidents/REPAIRs, core API routes follow schema-compatible task lookups, and the official Docker stack now passes a Python-only control-plane E2E (`health -> create -> scheduler -> claim -> heartbeat -> complete -> readiness`) |
| M-4b | Harden cognitive runtime and feedback loop | Done | `CognitiveOrchestrator` init duplication removed, cognitive graph factory deduplicated, agent preflight now injects `prepare_execution_context()`, audit completions feed `ReputationEngine`, `orchestrator-reputation` and `orchestrator-entropy` are live, MCTS now falls back to live agent-level metrics when the historical view is cold, and GoT/LangGraph now share a canonical graph-reasoning adapter plus write-back path |
| M-4c | Introduce real parallel goal execution plane | Done | Goal decomposition now seeds `goals`/`plans`/`tasks`/`dependencies`, `/intelligence/goals` uses the canonical contract, `goal_monitor` follows the schema-real statuses, and the canonical scheduler is kicked immediately so dependency-free slices dispatch in parallel without abusing `agent_swarm.py` |
| M-5 | Migrate finops/mutation/deploy/policy/release stack to Python | [P1] | Preserve governance and self-healing |
| M-6 | Sunset PowerShell runtime after 14-day soak | [P2] | Official provider/client stacks are already Python-only; synthetic incidents now auto-reconcile, the soak harness is published in `scripts/soak_python_control_plane.py`, `orchestrator-soak` is available as an official compose profile, and the remaining work is the long observation window plus residual image hardening |

---

## Security (S)

| ID | Item | Status | Notes |
|---|---|---|---|
| S-1 | Redis password + fail-safe rate limiting | Done | `REDIS_PASSWORD`, `RATE_LIMIT_FAIL_OPEN` env vars |
| S-2 | `safe_project_path()` for `entropy.py` and `simulate.py` | Done | All `path`/`project_path` fields validated before file system calls |
| S-3 | Default `ADMIN_API_KEY` detection | Done | Warns on startup if key is empty or `sk-admin-change-me` |
| S-4 | mTLS or signed JWTs for agent-to-orchestrator channel | [P2] | Currently authenticated only by API key |

---

## Reliability (R)

| ID | Item | Status | Notes |
|---|---|---|---|
| R-1 | Circuit breaker on Neo4j + Qdrant routes | Done | `circuit.py`, wired in `twin.py` and `ask.py` |
| R-2 | Watchdog reclaim cycle | Done | Stale `in-progress` → `pending`; excess retries → `dead-letter` |
| R-3 | DB pool health in `/health/deep` | Done | Reports pool size and wait count |
| R-4 | Graceful Neo4j reconnect on driver expiry | Done | `_twin_call()` resets singleton on `ServiceUnavailable`/`SessionExpired` |
| R-5 | Qdrant connection pool (currently creates per-request) | [P2] | |

---

## Performance (P)

| ID | Item | Status | Notes |
|---|---|---|---|
| P-1 | WITH RECURSIVE cycle detection (replaced full-graph load) | Done | DB migration 005 adds covering indexes |
| P-2 | `last_used_at` fire-and-forget update | Done | `asyncio.create_task()` keeps auth off the critical path |
| P-3 | Ask cache hit rate metric | Done | `orchestrator_ask_cache_hits_total{tenant}` in `/metrics`; counter in Redis |
| P-4 | Qdrant batch retrieve (currently 1 query per ask) | [P2] | |

---

## Observability (O)

| ID | Item | Status | Notes |
|---|---|---|---|
| O-1 | Prometheus: SSE connections + circuit breaker state | Done | `/metrics` endpoint |
| O-2 | Correlation ID on all authenticated requests | Done | `X-Correlation-ID` header respected; UUID generated if absent |
| O-3 | Structured log key glossary | Done | See `RUNBOOK.md` |
| O-4 | Distributed tracing (OpenTelemetry) | [P2] | `otel_setup.py` exists but not wired into request middleware |

---

## Growth (G)

| ID | Item | Status | Notes |
|---|---|---|---|
| G-1 | Golden flow integration test | Done | `test_golden_flow.py` — 5 tests covering full task lifecycle |
| G-2 | Load test: 500 concurrent SSE + 100 req/s tasks API | [P2] | Baseline before adding new features |
| G-3 | Multi-region deployment guide | [P2] | |

---

## Technical Debt (T)

| ID | Item | Status | Notes |
|---|---|---|---|
| T-1 | Remove backward-compat re-exports from `config.py` | Done | All callers migrated to `billing.py` / `security_config.py`; re-exports removed |
| T-2 | Consolidate `streaming_server.py` vs `streaming_server_v2.py` | Done | Flask server marked DEPRECATED; FastAPI (`streaming_server_v2.py`) is canonical |
| T-3 | `cognitive.py`: `_get_scheduler` manipulates `sys.path` | Done | Replaced with `from ...agent_swarm import get_scheduler` |
| T-4 | Replace `@app.on_event("startup")` with `lifespan` context manager | Done | `services/streaming/__init__.py` now uses a single lifespan manager |
| T-5 | Clarify `agent_swarm.py` scope | Done | Audited in March 2026: it is a heuristic scheduler/rebalancer, not a dormant parallel execution runtime |

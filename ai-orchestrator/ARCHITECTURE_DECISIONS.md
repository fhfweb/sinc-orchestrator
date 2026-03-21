# Architecture Decisions — SINC AI Orchestrator

> Last updated: 2026-03-16 (V2.0 audit — ADR-014 through ADR-016 added)
> Audience: engineers on-call, contributors, and future reviewers.

---

## ADR-001 — SSE connection tracking via Redis SET

**Status:** Accepted
**Phase:** 1.1

**Context:** Multiple FastAPI instances run behind a load balancer. An in-memory counter per process cannot enforce a global per-tenant SSE connection limit.

**Decision:** Use Redis `SADD / SCARD / SREM` with a per-tenant key (`sse_conns:{tenant_id}`). A 3600 s safety TTL prevents stale entries if `SREM` is missed (e.g., hard crash).

**Consequences:**
- Adds one Redis round-trip per SSE connect/disconnect. Acceptable — these are low-frequency.
- Requires Redis to be reachable at connection time. `connection_count()` returns 0 (fail-open) when Redis is unavailable to avoid blocking all SSE traffic on Redis downtime.

---

## ADR-002 — Cycle detection with WITH RECURSIVE CTE (replaced full-graph load)

**Status:** Accepted
**Phase:** 1.2

**Context:** The original `_has_cycle` loaded the entire tenant's dependency graph into Python dicts on every task-create request — O(N) DB reads and O(N) memory per call, with an unbounded Redis cache that could serve stale data after deletions.

**Decision:** Replace with a single `WITH RECURSIVE` PostgreSQL CTE that traverses only the subgraph reachable from the new task's proposed dependencies. The DB is always authoritative; the cache was removed entirely.

**Consequences:**
- Cycle detection is now O(reachable subgraph), not O(all deps for tenant).
- Requires `idx_dep_task_id` and `idx_dep_task_dep` indexes (migration 005).
- Cache invalidation bug class eliminated.

---

## ADR-003 — `last_used_at` update via `asyncio.create_task()` (fire-and-forget)

**Status:** Accepted
**Phase:** 1.3

**Context:** Every authenticated request needs to update `api_keys.last_used_at`. Doing it synchronously adds a DB write to every request's critical path.

**Decision:** Use `asyncio.create_task(_update_last_used_at(key))` inside `get_tenant_id`. The task runs concurrently with the request but is not awaited.

**Trade-off considered:** FastAPI's `BackgroundTasks` was the first choice, but `get_tenant_id` is a dependency function, not a route handler — it cannot receive `BackgroundTasks` by injection without changing every route signature. `create_task()` achieves the same semantics with no signature changes.

**Consequences:**
- A crash immediately after auth (before the task runs) will miss the update. Acceptable — `last_used_at` is advisory, not security-critical.

---

## ADR-004 — Redis password + fail-safe rate limiting

**Status:** Accepted
**Phase:** 1.4

**Context:** Redis was deployed without authentication. Additionally, when Redis was unavailable, the rate limiter silently allowed all requests.

**Decision:**
1. Added `REDIS_PASSWORD` env var (applied to both sync and async clients).
2. Added `RATE_LIMIT_FAIL_OPEN` env var (default `false`). When Redis is unavailable and fail-open is `false`, the limiter denies the request (fail-safe). Operators may set `true` to fall back to in-memory per-process counting.
3. If `APP_ENV=production` and `REDIS_PASSWORD` is empty, a `WARNING` is emitted on every startup.

---

## ADR-005 — `safe_project_path()` for path traversal protection

**Status:** Accepted
**Phase:** 1.5

**Context:** `ask.py` and `entropy.py` accept user-supplied `project_path` values that are passed to file system operations. A path like `../../etc/passwd` could escape the workspace.

**Decision:** Introduced `safe_project_path(user_path, base_dir)` in `security_config.py`. It uses `Path.resolve()` then `relative_to()` to confirm the resolved path is inside the base directory. Any deviation raises `ValueError`, which callers convert to HTTP 400.

**Note:** `entropy.py`'s `/entropy/scan-local` endpoint accepts a `path` field but only passes it to `os.path.isdir()` + `scanner.scan_project()`. The scanner itself should validate paths; a follow-up task should add `safe_project_path()` there as well.

---

## ADR-006 — Config split into `config.py` / `billing.py` / `security_config.py`

**Status:** Accepted
**Phase:** 3.3

**Context:** `config.py` contained heterogeneous concerns: database params, Redis params, plan/billing features, SSE limits, and security constants. Any module that needed one constant had to import the entire file.

**Decision:** Extract billing constants to `billing.py` and security/path constants to `security_config.py`. Keep backward-compatible re-exports in `config.py` (`# noqa: F401`) so existing callers are not broken by the split.

**Migration path:** Callers should be updated incrementally to import from the canonical module. The re-exports will be removed in a future cleanup cycle.

---

## ADR-007 — Circuit breaker for Neo4j and Qdrant

**Status:** Accepted
**Phase:** 2.3

**Context:** `twin.py` and `ask.py` make synchronous calls to Neo4j and Qdrant. An outage in either external service caused cascading timeouts across all routes.

**Decision:** Applied `@circuit_breaker(name="neo4j")` to all 7 `twin.py` routes and `get_breaker("qdrant")` to `_retrieve_context` in `ask.py`. The breaker opens after 5 consecutive failures and resets after 60 s.

**Consequences:**
- Routes return 503 immediately when the breaker is open instead of queuing connections.
- Breaker state is visible in `/health/deep` and `/metrics`.

---

## ADR-008 — Peripheral modules: simulate, connect, entropy, cognitive

**Status:** Accepted
**Phase:** 3.1 audit

**Audit finding:** The original audit classified `simulate.py` and `connect.py` as stubs. This is incorrect — all four peripheral modules are real implementations with lazy module loading and a 503 fallback when the optional dependency is absent.

| Module | External dependency | Status |
|---|---|---|
| `simulate.py` | `time_machine` (in `services/`) | Active — lazy-loaded, 503 on failure |
| `connect.py` | `github_connector` (in `services/`) | Active — HMAC-validated webhooks, DB persistence |
| `entropy.py` | `entropy_scanner` (in `services/`) | Active — 8 endpoints, score storage in DB |
| `cognitive.py` | `cognitive_orchestrator`, `agent_swarm` | Active — 10 endpoints, swarm assignment |

**Decision:** Do not remove any of these modules. Do not replace with 501 stubs. The lazy-loading pattern (`_get_scanner()`, `_get_orchestrator()`, etc.) already handles the case where the dependency is absent gracefully with a 503. This is the correct production pattern.

**Follow-up:** `simulate.py` and `entropy.py` expose `project_path` / `path` fields in request bodies that bypass `safe_project_path()`. These should be hardened in the next security pass (see [ROADMAP.md](ROADMAP.md) item S-2).

---

## ADR-009 — Admin mutation rate limiting via Redis sliding window

**Status:** Accepted
**Phase:** 2.4

**Context:** Admin mutation endpoints (`POST/DELETE /tenants`, `POST /api-keys`, `POST /reset`) were unthrottled. A compromised admin key could flood the system.

**Decision:** Added `_admin_mutation_rate_limit` dependency on all mutation routes: 10 requests/minute per source IP using a Redis sorted-set sliding window. Returns HTTP 429 with `Retry-After: 60`.

---

## ADR-010 — 1 MB completion payload limit

**Status:** Accepted
**Phase:** 2.5

**Context:** Agent completion bodies (`POST /agent/{id}/complete`) had no size guard. A rogue agent could send arbitrarily large `summary` fields.

**Decision:** Check `len(body.summary.encode()) > 1_000_000` before any DB write; return HTTP 413.

**Note:** A global `MAX_REQUEST_BYTES` nginx/gunicorn limit is the defense-in-depth layer. This check is the application-level guard for the specific field that carries the most risk.

---

## ADR-011 — EventBus singleton with asyncio double-checked locking

**Status:** Accepted
**Phase:** V1.0 / 1.1

**Context:** `EventBus.connect()` opens a Redis pub/sub connection. Under high concurrency (50+ simultaneous task dispatches at startup) without a lock, each coroutine would independently call `connect()`, exhausting the Redis connection pool and leaving multiple competing subscriptions on the same channel.

**Decision:** `EventBus` is a module-level singleton accessed exclusively through `await EventBus.get_instance()`. `connect()` is guarded by an `asyncio.Lock()` with a double-check (`if not self._connected`) to avoid redundant I/O after the lock is acquired.

**Consequences:**
- All 50 concurrent callers share one connection. Lock contention is a single `await` per caller until the first `connect()` resolves.
- `_connected` must only be set to `True` inside the lock, after `connect()` returns without exception.
- Direct instantiation (`EventBus()`) must be avoided in all call sites; use `get_instance()` everywhere.

---

## ADR-012 — L2/L3 memory layers are optional with 500 ms timeout degradation

**Status:** Accepted
**Phase:** V1.0 / 2.1

**Context:** The cognitive pipeline resolves tasks through a 5-layer memory hierarchy (L0 rules → L1 Postgres → L2 Neo4j GoT → L3 Qdrant semantic → L4 LLM). Neo4j and Qdrant are optional infrastructure; if they are down or slow, the entire pipeline must not stall.

**Decision:**
- L0 (Redis) and L1 (Postgres) are critical; a failure propagates as an error.
- L2 (Neo4j / Graph-of-Thought) and L3 (Qdrant / semantic memory) are wrapped with `asyncio.wait_for(timeout=MEMORY_L2_TIMEOUT_S / MEMORY_L3_TIMEOUT_S)` (default 0.5 s each). A timeout logs a warning and allows the pipeline to continue to the next layer.
- Config vars `MEMORY_L2_TIMEOUT_S` and `MEMORY_L3_TIMEOUT_S` allow operators to tune the trade-off between freshness and latency.

**Consequences:**
- Maximum added latency from optional layers: `L2_TIMEOUT + L3_TIMEOUT = 1 s` before falling through to L4 LLM.
- `/health/deep` exposes per-layer status so operators can see which layers are degraded.

---

## ADR-013 — Digital Twin sync as BackgroundTask (outside DB transaction)

**Status:** Accepted
**Phase:** V1.0 / 2.3

**Context:** The original `agent_completion` handler called `twin.link_task_to_files()` inside the main `async with async_db()` transaction block. Neo4j calls are slow (100–500 ms), and any Neo4j exception would roll back the task status update — losing the completion record even though the agent finished successfully.

**Decision:** Moved `_sync_digital_twin` to a FastAPI `BackgroundTask`. The DB transaction commits first with the correct task status; the twin sync runs asynchronously after the HTTP response is returned. If the twin sync fails, it logs a warning but does not affect the task record.

**Consequences:**
- There is a brief window where the task is `done` in Postgres but not yet reflected in the twin graph. This is acceptable — the twin is a read-model for analysis, not the source of truth for task status.
- Twin sync failures are silent to the API caller; operators must monitor `twin_sync_error` log events.

---

## ADR-014 — Docker-first sandbox execution with workspace path guard (C1)

**Status:** Accepted
**Phase:** V2.0 / C1

**Context:** `_safe_execute` in `agent_worker.py` previously ran verification scripts via `subprocess.run(["bash", "-c", script])` with no isolation. A compromised task payload could execute arbitrary commands on the worker host (RCE). The intermediate "temp file" fix avoided argument-injection through the `-c` flag but still ran scripts on the host with worker-user privileges.

**Decision:**
- Introduce `_validated_wdir()` that resolves `wdir` and calls `Path.relative_to(WORKSPACE)`, refusing any path that escapes the workspace root before any I/O is attempted.
- Add `_docker_execute()`: runs the script in an ephemeral container with `network_mode="none"`, `read_only=True`, `security_opt=["no-new-privileges"]`, and resource caps (`SANDBOX_MEM_LIMIT`, `SANDBOX_CPU_QUOTA`). The workspace is bind-mounted read-write; everything else is read-only.
- `_safe_execute()` prefers Docker (`_HAS_DOCKER=True`) and falls back to `_host_execute()` (the improved temp-file approach) when the Docker SDK is unavailable, logging a clear warning.
- Set `SANDBOX_IMAGE` env var to control the container image (default: `python:3.12-slim`).

**Consequences:**
- Verification scripts can no longer reach the network or write outside the workspace, even if they contain malicious payloads.
- Requires `pip install docker` and a reachable Docker daemon on the worker host. Workers without Docker still function but are less isolated.
- CPU/memory caps prevent runaway scripts from starving the host.

---

## ADR-015 — Single FastAPI entrypoint (C2)

**Status:** Accepted
**Phase:** V2.0 / C2

**Context:** `fastapi_server.py` contained a duplicate `POST /tasks` implementation that omitted quota enforcement, dependency-cycle detection, gate logic, and task-flag fields (`requires_review`, `verification_required`, `red_team_enabled`). Depending on which file was used to start the server, the system could silently bypass all business-rule guards — a split-brain condition.

**Decision:** Consolidate to a single entrypoint. `fastapi_server.py` is now a thin bootstrap that calls `create_app()` from `streaming/`, where all validated route logic lives. There is no duplicate `POST /tasks` definition.

**Consequences:**
- All `POST /tasks` requests go through the fully-validated path in `streaming/routes/tasks.py`.
- `fastapi_server.py` is kept as the Docker/uvicorn entrypoint for the active server but contains no route logic.

---

## ADR-016 — Structured error propagation in agent_worker (R2)

**Status:** Accepted
**Phase:** V2.0 / R2

**Context:** `_http_complete()` and `update_task_in_db()` silently swallowed exceptions (`except Exception: _log(...)`). A failure in either function left the task permanently stuck in `in-progress` — the orchestrator never received the completion signal and the agent never retried, so the task only recovered when the watchdog reclaimed it as stale after `TASK_STALE_MINUTES`.

**Decision:**
- `_http_complete()` re-raises after logging. Callers (`update_task_in_db`, `_http_dispatch_loop`) already wrap this in an outer `except` block that reports the task as `failed`.
- `update_task_in_db()` (DB/file mode) re-raises after logging, for the same reason.
- The error-path `write_completion()` fallback (best-effort JSON cache) uses `_log()` instead of silent `pass`, so the failure is visible in logs without masking the original exception.

**Consequences:**
- Completion failures now surface immediately as task `failed` states rather than hidden `in-progress` zombie tasks.
- Operators see `completion_post_failed` / `db_update_failed` log keys that are actionable.

---

## ADR-017 — Unified Python Control Plane (Zero PowerShell)

**Status:** Accepted
**Phase:** V2.0 / P5

**Context:** The system originally relied on a hybrid architecture of PowerShell scripts (`Invoke-Observer`, `Invoke-Scheduler`, etc.) and Python services. This created significant deployment friction, environment inconsistency, and made multi-tenancy support extremely difficult to maintain across two distinct runtimes.

**Decision:**
- Eliminate all remaining PowerShell scripts (`.ps1`) from the project codebase.
- Standardize on the **Python Runtime Plane** (`runtime_plane.py`) for critical loops (Observer, Readiness, Scheduler).
- Standardize on the **Python Governance Plane** (`governance_plane.py`) for automated policies (Mutation, FinOps, Deploy Verification).
- Use asynchronous background workers (e.g., `observer_worker.py`) as the operational entry points.

**Consequences:**
- **Simplified Deployment:** Only Python 3.12+ and its dependencies are required.
- **Improved Multi-Tenancy:** The Python Control Plane natively supports `X-Tenant-Id` routing and tenant-isolated loops.
- **Enhanced Observability:** All system events and logs now follow the canonical OpenTelemetry/Python logging pipeline established in MIG-P5-004.
- **Legacy CLI Impact:** Users must now use the Python SDK (`SincClient`) or the `orchestrator_db_bridge.py` for administrative tasks, as PowerShell wrappers were removed.

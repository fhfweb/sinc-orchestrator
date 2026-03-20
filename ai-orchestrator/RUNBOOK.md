# Runbook — SINC AI Orchestrator

> Last updated: 2026-03-16
> For architecture decisions, see [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md).
> For planned work, see [ROADMAP.md](ROADMAP.md).

---

## Table of Contents

1. [Starting the stack](#1-starting-the-stack)
2. [Starting an agent worker](#2-starting-an-agent-worker)
3. [Health checks](#3-health-checks)
4. [Common failure modes](#4-common-failure-modes)
5. [Rate limiting](#5-rate-limiting)
6. [SSE connections](#6-sse-connections)
7. [Watchdog / dead-letter tasks](#7-watchdog--dead-letter-tasks)
8. [Log key glossary](#8-log-key-glossary)
9. [Database migrations](#9-database-migrations)
10. [Environment variables reference](#10-environment-variables-reference)

---

## 1. Starting the stack

```bash
cd ai-orchestrator/docker
cp .env.example .env          # fill in ORCH_DB_PASSWORD, ADMIN_API_KEY, NEO4J_AUTH
docker compose up -d
```

The streaming server starts on port 8001 (configurable via `PORT`).

---

## 2. Starting an agent worker

```bash
cd ai-orchestrator/services
python agent_worker.py --tenant <tenant_id> --agent <agent_name>
```

The worker polls the task queue via the orchestrator API. It expects `ORCHESTRATOR_URL` and `ORCHESTRATOR_API_KEY` in the environment (or `.env`).

---

## 3. Health checks

| Endpoint | Purpose |
|---|---|
| `GET /health` | Shallow — returns 200 if the process is alive |
| `GET /health/deep` | Deep — checks DB pool, Redis, Neo4j, Qdrant, circuit breaker states, SSE counts |
| `GET /metrics` | Prometheus text format |

**Deep health response shape:**

```json
{
  "status": "ok",
  "layers": {
    "l0_redis":              "ok",
    "l1_postgres":           "ok",
    "l2_neo4j":              "ok",
    "l3_qdrant":             "not_configured",
    "l4_llm":                "configured",
    "event_bus":             "ok",
    "llm_semaphore":         "0/5 in flight",
    "cognitive_orchestrator": "initialized"
  },
  "circuit_breakers": {"neo4j": "closed", "qdrant": "closed"},
  "sse_connections":  {"total": 3, "by_tenant": {"t-abc": 2, "t-xyz": 1}}
}
```

Each layer probe has a 500 ms timeout. Critical layers (`l1_postgres`, `event_bus`) set `"status": "degraded"` and return HTTP 503 if unavailable. Optional layers (`l0_redis`, `l2_neo4j`, `l3_qdrant`, `l4_llm`) are reported but do not change the HTTP status.

---

## 4. Common failure modes

### Circuit breaker open (Neo4j or Qdrant)

**Symptom:** All `GET /twin/*` routes return 503. `/health/deep` shows `"neo4j": "open"`.

**Cause:** 5 consecutive failures to the external service.

**Resolution:**
1. Check Neo4j / Qdrant connectivity from the container: `docker exec -it orchestrator curl http://neo4j:7474`.
2. Once the service is healthy, the breaker resets automatically after 60 s.
3. To force an immediate reset (without restarting the process): `POST /admin/circuit/reset` with `X-Admin-Key`.

### Redis unavailable + `RATE_LIMIT_FAIL_OPEN=false`

**Symptom:** All API calls return 429 with body `"redis_unavailable_fail_safe"`.

**Cause:** Redis is down and fail-safe mode is active (default).

**Resolution:** Restore Redis. If you must temporarily allow traffic, set `RATE_LIMIT_FAIL_OPEN=true` and restart (or hot-reload). Revert immediately after Redis recovers.

### Stale `in-progress` tasks (agent crashed)

**Symptom:** Tasks are stuck in `in-progress` and the assigned agent is no longer heartbeating.

**Cause:** Agent crashed without marking the task `done` or `failed`.

**Resolution:** The watchdog reclaims these automatically every 60 s. A task is reclaimed when its heartbeat is older than `TASK_STALE_MINUTES` (default: 5). If a task hits `MAX_TASK_RETRIES` (default: 3), it moves to `dead-letter` — see [section 7](#7-watchdog--dead-letter-tasks).

### `ImportError` on startup (peripheral module)

**Symptom:** Log shows `router_register_failed name=entropy error=...`.

**Cause:** An optional dependency (`entropy_scanner`, `cognitive_orchestrator`, etc.) is missing.

**Resolution:** This is expected if the dependency is not installed. The route is simply skipped; the rest of the API is unaffected. To enable the module, install its package and restart.

---

## 5. Rate limiting

- Implemented in `streaming/core/redis_.py` (`check_rate_limit`).
- Per-tenant sliding window (1 minute).
- When Redis is unavailable: controlled by `RATE_LIMIT_FAIL_OPEN`.
- Admin mutation endpoints: additionally rate-limited to 10 req/min per source IP.

**Log on rate limit hit:**

```
{"level":"WARNING","msg":"rate_limit_exceeded tenant=<id> path=<path>"}
```

**Log on fail-safe trigger:**

```
{"level":"WARNING","msg":"rate_limit_fail_safe tenant=<id> redis_unavailable"}
```

---

## 6. SSE connections

- Per-tenant limit enforced globally via Redis SET (`sse_conns:{tenant_id}`).
- Plan limits: `free=2`, `pro=20`, `enterprise=100`.
- A 3600 s TTL on each Redis key handles leaked connections (crash without `SREM`).

**Check current connections:**

```bash
redis-cli SCARD sse_conns:<tenant_id>
# or via the API:
curl /health/deep | jq .sse_connections
```

**Manually clear a stuck connection:**

```bash
redis-cli SREM sse_conns:<tenant_id> <conn_id>
```

---

## 7. Watchdog / dead-letter tasks

The watchdog runs every 60 s and performs three actions:

1. **Reclaim stale `in-progress`** — tasks where `heartbeat_at < NOW() - TASK_STALE_MINUTES` are reset to `pending` and `lock_retry_count` is incremented.
2. **Reclaim stale `delivered`** — tasks stuck in `delivered` for more than `TASK_STALE_MINUTES` are returned to `pending`.
3. **Move to `dead-letter`** — tasks where `lock_retry_count >= MAX_TASK_RETRIES` are moved to `dead-letter`.

**Inspect dead-letter tasks:**

```sql
SELECT id, title, assigned_agent, lock_retry_count, updated_at
FROM tasks
WHERE status = 'dead-letter' AND tenant_id = '<id>'
ORDER BY updated_at DESC;
```

**Manually requeue a dead-letter task:**

```sql
UPDATE tasks
SET status = 'pending', lock_retry_count = 0, assigned_agent = NULL
WHERE id = '<task_id>';
```

---

## 8. Log key glossary

All logs are JSON with at minimum `ts`, `level`, `logger`, `msg`.

| Key | Meaning |
|---|---|
| `tenant=<id>` | Tenant scope |
| `correlation_id=<uuid>` | Request trace ID (also in `X-Correlation-ID` response header) |
| `path=<route>` | HTTP path |
| `rate_limit_exceeded` | Request denied by rate limiter |
| `rate_limit_fail_safe` | Redis down, request denied in fail-safe mode |
| `security_warning` | Production security misconfiguration |
| `router_register_failed name=<mod>` | Optional router could not be loaded at startup |
| `rule_learner_cycle_done` | Cognitive rule learning completed |
| `admin_tenant_created tenant_id=<id> plan=<plan> src_ip=<ip>` | Admin action audit log |
| `watchdog_reclaimed count=<n>` | Tasks returned to pending by watchdog |
| `circuit_open name=<svc>` | Circuit breaker opened |
| `circuit_closed name=<svc>` | Circuit breaker reset |

---

## 9. Database migrations

Migrations live in two places:

| Path | Used by |
|---|---|
| `database/migrations/` | Manual application (CI/CD, Flyway, Liquibase) |
| `docker/init-db/` | Docker Compose `init-db` volume — runs on first container start |

To apply a migration manually:

```bash
psql "$DATABASE_URL" -f database/migrations/005_dep_graph_indexes.sql
```

Migrations are idempotent (`IF NOT EXISTS` / `OR REPLACE`).

---

## 10. Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORCH_DB_PASSWORD` | Yes | — | PostgreSQL password |
| `ADMIN_API_KEY` | Yes | — | `X-Admin-Key` for admin endpoints |
| `NEO4J_AUTH` | Yes | — | `user/password` for Neo4j |
| `REDIS_PASSWORD` | Prod | `""` | Redis password; empty = no auth |
| `APP_ENV` | No | `development` | `production` enables security warnings |
| `RATE_LIMIT_FAIL_OPEN` | No | `false` | Allow traffic when Redis is down |
| `ANTHROPIC_API_KEY` | No | `""` | Claude API key (pro/enterprise) |
| `CORS_ORIGINS` | No | `*` | Comma-separated allowed origins |
| `GIT_WEBHOOK_SECRET` | No | `""` | HMAC secret for GitHub webhooks |
| `SIGNUP_ENABLED` | No | `true` | Allow `POST /signup` |
| `PORT` | No | `8001` | HTTP listen port |
| `LOG_LEVEL` | No | `INFO` | `DEBUG` or `INFO` |
| `TASK_STALE_MINUTES` | No | `5` | Minutes before in-progress task is reclaimed |
| `MAX_TASK_RETRIES` | No | `3` | Retries before task goes to dead-letter |

# SINC Orchestrator - Service Provider

Multi-tenant orchestration platform with a canonical Python control plane.

## Canonical Runtime

- Control plane: `services/streaming`
- Official provider stack: `docker/docker-compose.orchestrator.yml`
- Official client stack: `docker/docker-compose.client.yml`
- Official dashboard: `http://127.0.0.1:8765/dashboard`
- Canonical task state: Postgres

## Structure

```text
ai-orchestrator/
|- services/                 API server + workers + SDK services
|- services/streaming/       canonical FastAPI control plane
|- sdk/                      client loop, agent worker, downloadable assets
|- database/migrations/      orchestrator schema
|- docker/                   provider/client stacks
|- documentation/migration/  migration backlog and parity plan
|- scripts/v2/               legacy maintenance only
```

## Start Provider

```bash
cd ai-orchestrator/docker
cp .env.docker.generated .env
docker compose -f docker-compose.orchestrator.yml up -d
```

API and dashboard will be available at `http://localhost:8765`.

## Long Soak Validation

Use the dedicated soak profile when you want the 14-day observation window without tying it to an interactive shell:

```bash
docker compose -f docker/docker-compose.orchestrator.yml --profile soak up -d orchestrator-soak
docker logs -f sinc-orchestrator-soak
```

## OpenTelemetry

The provider stack now includes an `otel-collector` service by default and the
canonical control plane plus the dedicated workers export spans to it through
`OTEL_EXPORTER_OTLP_ENDPOINT`.

Key ports:

- `4317` OTLP gRPC
- `4318` OTLP HTTP
- `55679` zPages

The collector now persists traces to `/var/lib/otel/traces.jsonl` via a real
`file` exporter in addition to the debug stream. If you need to forward spans
to Tempo, Jaeger, or another backend, extend `docker/otel-collector-config.yaml`.

To validate export explicitly instead of only checking container health:

```bash
python ai-orchestrator/scripts/verify_otel_export.py
```

The control plane also exposes:

- `POST /otel/probe`

which emits a named `system.otel_probe` span for exporter-based verification in the collector.

## Cognitive Batch Smoke

You can exercise the live batch path without touching the background soak:

```bash
python ai-orchestrator/scripts/e2e_cognitive_batch.py --base-url http://127.0.0.1:8765 --api-key dev
```

## Agent Engineering Toolchain

The canonical local agent runner now exposes an engineering-first tool stack for
all 21 agents:

- `semantic_search`
- `analyze_code`
- `explain_code`
- `run_tests`
- `diff_files`
- `api_call`
- `plan_tasks`
- `memory_search`
- `memory_write`
- `self_reflect`

The system prompts enforce the mandatory loop:

1. `CONTEXT` via `search_files` or `semantic_search`
2. `UNDERSTAND` via `read_file`, `analyze_code`, `explain_code`
3. `PLAN` via `plan_tasks`
4. `EXECUTE` via `patch_file` / `write_file`
5. `VALIDATE` via `run_tests` first, then targeted `bash_exec`
6. `REFLECT` via `diff_files`, `self_reflect`, and `memory_write`

## Minimal Environment

```env
ORCH_DB_PASSWORD=strong-password
ADMIN_API_KEY=sk-admin-...
ANTHROPIC_API_KEY=sk-ant-...     # optional
GIT_WEBHOOK_SECRET=...           # optional
CORS_ORIGINS=https://your-frontend.example
```

## Create Tenant

```bash
curl -X POST http://localhost:8765/admin/tenants \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Project", "plan": "pro"}'
```

## Connect Client

Copy `docker/docker-compose.client.yml` to the project root and create `.env`:

```env
ORCHESTRATOR_URL=http://<provider-host>:8765
ORCHESTRATOR_API_KEY=<tenant-api-key>
PROJECT_ID=my-project
TENANT_ID=my-tenant
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
docker compose -f docker-compose.client.yml up -d
```

## Runtime Status

Already removed from the official path:
- `docker-compose.n5.yml` as active stack
- `services/orchestrator_core.py` from official compose
- PowerShell client loop from official client compose
- static legacy dashboard as active UI

Legacy artifacts still present only for controlled migration:
- `scripts/v2/Start-StreamingServer.py`
- `scripts/v2/*`
- `ai-orchestrator/scripts/v2/*`
- `docs/agents/dashboard.html`

## Main Endpoints

| Method | Route | Description |
|---|---|---|
| GET | `/health` | quick health check |
| GET | `/health/deep` | deep infrastructure check |
| GET | `/metrics` | Prometheus metrics |
| POST | `/otel/probe` | emit an explicit OTEL span for collector verification |
| GET | `/docs` | Swagger UI |
| GET | `/dashboard` | canonical dashboard |
| GET | `/api/v5/dashboard/task-debugger/{task_id}` | deep task inspector payload used by the dashboard debugger |
| POST | `/tasks` | create task |
| GET | `/tasks` | list tasks |
| POST | `/tasks/claim` | claim task |
| POST | `/tasks/complete` | complete task |
| POST | `/cognitive/process` | run one task through the full cognitive pipeline |
| POST | `/cognitive/batch` | process a semantic batch through `process_batch()` |
| POST | `/external-bridge/run` | tick external bridge |
| GET | `/external-bridge/status` | external bridge status |
| POST | `/policy/run` | execute policy tick |
| GET | `/policy` | latest policy report |
| POST | `/mutation/run` | execute mutation tick |
| GET | `/mutation` | latest mutation report |
| POST | `/finops/run` | execute finops tick |
| GET | `/finops` | latest finops report |
| POST | `/deploy-verify/run` | execute deploy verification tick |
| GET | `/deploy-verify` | latest deploy verification report |
| POST | `/pattern-promotion/run` | execute rule promotion tick |
| GET | `/pattern-promotion` | latest pattern promotion report |
| POST | `/release/run` | execute release gate tick |
| GET | `/release` | latest release gate report |
| GET | `/events` | SSE stream |
| POST | `/admin/tenants` | create tenant |

Detailed API documentation: `GET /docs`

The canonical dashboard now includes a task debugger panel driven by the live pipeline and `/api/v5/dashboard/task-debugger/{task_id}`.

The cognitive batch path is already live in the provider runtime; it does not
require a separate worker or feature flag.

## Worker Topology

The official provider compose now runs dedicated Python workers for:

- `scheduler`
- `observer`
- `readiness`
- `external-bridge`
- `reputation`
- `entropy`
- `policy`
- `mutation`
- `finops`
- `deploy-verify`
- `pattern-promotion`
- `release`

The reputation worker is tenant-aware via `ORCH_TENANT_ID` / `ORCHESTRATOR_TENANT_ID`
fallbacks, while still honoring the `tenant_id` carried by audit events.

The FastAPI app keeps embedded long-running workers disabled in the official stack to avoid duplicate execution. `watchdog` remains embedded in the control plane.
`task-dag.json` projection also remains embedded there, but only as a DB-generated compatibility artifact.

## Governance Runtime Flags

Set these in `docker/.env` when operating the provider stack:

```env
ORCHESTRATOR_SCHEDULER_INTERVAL_SECONDS=30
ORCHESTRATOR_OBSERVER_INTERVAL_SECONDS=45
ORCHESTRATOR_EXTERNAL_BRIDGE_INTERVAL_SECONDS=20
ORCHESTRATOR_POLICY_INTERVAL_SECONDS=120
ORCHESTRATOR_MUTATION_INTERVAL_SECONDS=300
ORCHESTRATOR_MUTATION_TIMEOUT_SECONDS=1800
ORCHESTRATOR_MUTATION_COMMAND=
ORCHESTRATOR_MUTATION_REQUIRED=0
ORCHESTRATOR_FINOPS_INTERVAL_SECONDS=180
ORCHESTRATOR_FINOPS_DISK_FREE_MIN_PERCENT=10
ORCHESTRATOR_FINOPS_MEM_FREE_MIN_MB=512
ORCHESTRATOR_DEPLOY_VERIFY_INTERVAL_SECONDS=180
ORCHESTRATOR_PATTERN_PROMOTION_INTERVAL_SECONDS=300
ORCHESTRATOR_RELEASE_INTERVAL_SECONDS=300
ORCHESTRATOR_RELEASE_ALLOW_NO_MUTATION=0
ORCHESTRATOR_EMBEDDED_TASK_DAG_PROJECTION_ENABLED=1
ORCHESTRATOR_TASK_DAG_PROJECTION_INTERVAL_SECONDS=20
```

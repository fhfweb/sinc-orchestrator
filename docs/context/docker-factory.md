# Module Context: Docker Factory

Legacy context note:

- This document describes the legacy project-level Docker factory flow.
- It is not the canonical deployment path for the orchestrator platform itself.
- The official provider runtime is `ai-orchestrator/docker/docker-compose.orchestrator.yml`.
- The official client runtime is `ai-orchestrator/docker/docker-compose.client.yml`.

**Script:** `scripts/v2/Invoke-DockerAutoBuilderV2.ps1`
**Invoked by:** Intake pipeline (Mode = submit/new) when `-GenerateDocker` or `IncludeNeo4j/Qdrant` is set.
**Output:** `ai-orchestrator/docker/docker-compose.generated.yml` + `.env.docker.generated`

---

## What it does

1. **Detects** the project stack and database from `state.json`.
2. **Generates** a `docker-compose.generated.yml` with services for:
   - The application container (inferred Dockerfile or base image)
   - PostgreSQL / MySQL / MongoDB (if detected)
   - Neo4j (if `IncludeNeo4j`)
   - Qdrant (if `IncludeQdrant`)
   - Redis / RabbitMQ (if `IncludeRedis` / `IncludeRabbitMq`)
   - Worker container (if `IncludeWorker`)
3. **Generates** `.env.docker.generated` with all service credentials.
4. **Writes** startup paths into `state.json` so the loop can find the compose file.
5. **Validates** that required Docker ports are available before starting services.

## App command detection

The factory tries to infer the app entrypoint:
- Python + FastAPI → `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Python + Flask → `flask run --host 0.0.0.0`
- Node → `node dist/index.js` or `npm start`
- Unknown → `# REVIEW_REQUIRED` (agent must fix manually)

**If you see `REVIEW_REQUIRED` in the compose file**, manually set the `command:` field
and update `APP_COMMAND_CONFIDENCE` in `.env.docker.generated`.

## Neo4j configuration

- Community Edition (CE) only supports a single database named `neo4j`
- Do **not** set `NEO4J_DATABASE` to a custom name — CE will reject it
- Enterprise Edition supports `CREATE DATABASE <name>` — check your license

## Key files written

| File | Description |
|------|-------------|
| `ai-orchestrator/docker/docker-compose.generated.yml` | Full service stack |
| `ai-orchestrator/docker/.env.docker.generated` | Env vars for all services |
| `ai-orchestrator/database/.secrets/vault.json` | DPAPI-encrypted credentials |

## Port defaults

| Service | Default port |
|---------|-------------|
| App | 8000 |
| PostgreSQL | 5432 |
| Neo4j Bolt | 7687 |
| Neo4j HTTP | 7474 |
| Qdrant | 6333 |
| Redis | 6379 |
| RabbitMQ | 5672 |

## Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `REVIEW_REQUIRED` in app command | Stack not recognized | Set `command:` manually |
| Port already in use | Another service on the same port | Change port in compose or stop other service |
| Neo4j fails to start | Database name mismatch | Set `NEO4J_DATABASE=neo4j` |
| `.env.docker.generated` not found | Factory not run | Re-run intake with `-GenerateDocker` |
| Container build fails | No Dockerfile in project root | Add Dockerfile or use pre-built image |

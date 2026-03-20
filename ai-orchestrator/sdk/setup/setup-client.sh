#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Client Setup
# Usage:
#   curl -sf http://<orchestrator-host>:8765/sdk/setup-client.sh | bash
# Or with explicit URL:
#   ORCHESTRATOR_URL=http://myhost:8765 bash setup-client.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ORCH_URL="${ORCHESTRATOR_URL:-http://localhost:8765}"
TARGET_DIR="${1:-.}"

echo "[setup] Orchestrator SDK bootstrap — target: $TARGET_DIR"
echo "[setup] Orchestrator URL: $ORCH_URL"

# Verify orchestrator is reachable
if ! curl -sf "$ORCH_URL/health" > /dev/null; then
    echo "[setup] ERROR: Orchestrator unreachable at $ORCH_URL"
    exit 1
fi
echo "[setup] Orchestrator is healthy."

# Download docker-compose client template
echo "[setup] Downloading docker-compose.client.yml..."
curl -sf "$ORCH_URL/sdk/docker-compose.client.yml" -o "$TARGET_DIR/docker-compose.client.yml"

echo "[setup] Downloading baked client Docker assets..."
mkdir -p "$TARGET_DIR/.orchestrator-sdk"
for asset in \
  docker/Dockerfile.agent \
  docker/Dockerfile.loop \
  docker/Dockerfile.ingest \
  docker/requirements.agent.txt \
  docker/requirements.loop.txt \
  docker/requirements.ingest.txt
do
  curl -sf "$ORCH_URL/sdk/$asset" -o "$TARGET_DIR/.orchestrator-sdk/$(basename "$asset")"
done

# Download .env.example (only if .env doesn't exist)
if [ ! -f "$TARGET_DIR/.env" ]; then
    echo "[setup] Downloading .env.example → .env ..."
    curl -sf "$ORCH_URL/sdk/.env.example" -o "$TARGET_DIR/.env"
    echo "[setup] IMPORTANT: Edit .env and set ORCHESTRATOR_API_KEY, PROJECT_ID, TENANT_ID"
else
    echo "[setup] .env already exists, skipping."
fi

echo ""
echo "[setup] Done. Next steps:"
echo "  1. Edit .env and set your ORCHESTRATOR_API_KEY / PROJECT_ID / TENANT_ID"
echo "  2. docker compose -f docker-compose.client.yml build"
echo "  3. docker compose -f docker-compose.client.yml up -d agent-worker"
echo "  4. docker compose -f docker-compose.client.yml run --rm ingest-worker"

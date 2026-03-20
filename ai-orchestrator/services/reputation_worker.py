from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import logging
import os

from services.otel_setup import bootstrap_worker_otel
from services.reputation_engine import ReputationEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reputation_worker")


def _resolve_worker_tenant_id() -> str:
    return (
        env_get("ORCH_TENANT_ID")
        or env_get("ORCHESTRATOR_TENANT_ID")
        or env_get("TENANT_ID")
        or "local"
    )


async def _main() -> None:
    bootstrap_worker_otel("orchestrator-reputation")
    tenant_id = _resolve_worker_tenant_id()
    log.info("starting_reputation_worker tenant=%s", tenant_id)
    await ReputationEngine(tenant_id=tenant_id).start()


if __name__ == "__main__":
    asyncio.run(_main())

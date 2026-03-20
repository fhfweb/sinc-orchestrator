from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import logging
import os

from services.otel_setup import bootstrap_worker_otel
from services.streaming.core.external_agent_bridge import run_external_bridge_loop

log = logging.getLogger("orchestrator.external-agent-bridge-worker")


async def main_async() -> None:
    bootstrap_worker_otel("orchestrator-external-bridge")
    tenant_id = env_get("TENANT_ID", default="local")
    project_id = env_get("PROJECT_ID", default="")
    await run_external_bridge_loop(tenant_id=tenant_id, project_id=project_id)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("external_agent_bridge_worker_stopped")


if __name__ == "__main__":
    main()

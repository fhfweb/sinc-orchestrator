from __future__ import annotations

import asyncio
import logging

from services.otel_setup import bootstrap_worker_otel
from services.streaming.core.runtime_plane import run_readiness_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("readiness_worker")


async def _main() -> None:
    bootstrap_worker_otel("orchestrator-readiness")
    log.info("starting_readiness_worker")
    await run_readiness_loop()


if __name__ == "__main__":
    asyncio.run(_main())

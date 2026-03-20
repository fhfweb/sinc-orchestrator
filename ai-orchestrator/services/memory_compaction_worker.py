from __future__ import annotations

import asyncio
import logging

from services.memory_compaction import run_memory_compaction_loop
from services.otel_setup import bootstrap_worker_otel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("memory_compaction_worker")


async def _main() -> None:
    bootstrap_worker_otel("orchestrator-memory-compaction")
    log.info("starting_memory_compaction_worker")
    await run_memory_compaction_loop()


if __name__ == "__main__":
    asyncio.run(_main())

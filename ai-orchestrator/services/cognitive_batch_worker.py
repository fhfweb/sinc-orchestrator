from __future__ import annotations

import asyncio
import logging

from services.otel_setup import bootstrap_worker_otel
from services.streaming.routes.cognitive import run_cognitive_batch_queue_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cognitive_batch_worker")


async def _main() -> None:
    bootstrap_worker_otel("orchestrator-cognitive-batch")
    log.info("starting_cognitive_batch_worker")
    await run_cognitive_batch_queue_loop()


if __name__ == "__main__":
    asyncio.run(_main())

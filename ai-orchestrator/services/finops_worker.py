from __future__ import annotations

import asyncio
import logging
import sys

from services.otel_setup import bootstrap_worker_otel
from services.streaming.core.governance_plane import run_finops_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def _main() -> None:
    bootstrap_worker_otel("orchestrator-finops")
    await run_finops_loop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(130)

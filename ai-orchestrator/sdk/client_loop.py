"""
Minimal Python client loop for the canonical control plane.

The old PowerShell autonomous loop is being retired. This worker keeps the
consumer-side loop contract alive by periodically nudging the canonical
streaming runtime to execute observer/scheduler/readiness/bridge ticks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import logging
from datetime import datetime, timezone
from sdk.sinc_client import SincClient

# Configuration from Environment
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000")
ORCHESTRATOR_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")
TENANT_ID = os.environ.get("TENANT_ID", "local")
INTERVAL_SECONDS = int(os.environ.get("ORCHESTRATOR_LOOP_INTERVAL_SECONDS", "120"))
MAX_CYCLES = int(os.environ.get("ORCHESTRATOR_LOOP_MAX_CYCLES", "0"))

# Logger Setup
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
log = logging.getLogger("worker-loop")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

async def main_async() -> int:
    client = SincClient(
        base_url=ORCHESTRATOR_URL,
        api_key=ORCHESTRATOR_API_KEY,
        tenant_id=TENANT_ID
    )
    
    cycle = 0
    log.info(f"Starting SINC Worker Loop | url={client.base_url} | tenant={client.tenant_id}")

    endpoints = ["observer", "scheduler", "readiness", "external-bridge"]

    while True:
        cycle += 1
        log.info(f"--- Cycle {cycle} Starting ---")
        
        for component in endpoints:
            try:
                log.info(f"Nudging component: {component}")
                res = await client.run_heartbeat(component)
                log.info(f"Component {component} response: {res.get('status', 'ok')}")
            except Exception as e:
                log.error(f"Failed to nudge {component}: {e}")

        if MAX_CYCLES > 0 and cycle >= MAX_CYCLES:
            log.info("Max cycles reached. Exiting.")
            return 0

        # Wait for the next interval
        await asyncio.sleep(max(INTERVAL_SECONDS, 5))

def main():
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Loop interrupted by user.")
        return 0
    except Exception as e:
        log.exception(f"Fatal error in worker loop: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())

"""
watchdog_service.py
===================
Standalone reliability engine for SINC AI Orchestrator.
Decouples task reclamation from the API and implements event-driven triggers.

Run with: python watchdog_service.py
"""
import asyncio
import logging
import json
import os
import signal
import redis.asyncio as redis
from typing import Optional

# Ensure services/ is in path if running directly
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))

from services.streaming.core.watchdog import run_watchdog, perform_reclaim_cycle
from services.streaming.core.config import REDIS_HOST, REDIS_PORT, REDIS_DB
from services.event_bus import EventBus

# ─────────────────────────────────────────────
# CONFIG & LOGGING
# ─────────────────────────────────────────────

log = logging.getLogger("watchdog_service")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

# ─────────────────────────────────────────────
# REDIS KEYSPACE NOTIFICATION LISTENER
# ─────────────────────────────────────────────

async def listen_for_task_expiry():
    """
    Subscribes to Redis keyspace notifications for expired heartbeat keys.
    Triggers an immediate reclaim cycle for the specific task.
    """
    redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    r = redis.from_url(redis_url, decode_responses=True)
    
    # Enable Keyspace Notifications for Expired events (Ex)
    try:
        await r.config_set("notify-keyspace-events", "Ex")
        log.info("Redis keyspace notifications enabled (Ex)")
    except Exception as e:
        log.warning(f"Could not SET notify-keyspace-events: {e}. Ensure Redis has this enabled.")

    pubsub = r.pubsub()
    # Subscribe to expired events on DB
    channel = f"__keyevent@{REDIS_DB}__:expired"
    await pubsub.subscribe(channel)
    log.info(f"Subscribed to keyspace notifications on {channel}")

    async for message in pubsub.listen():
        if message["type"] == "message":
            key_name = message["data"]
            if key_name.startswith("hb:task:"):
                task_id = key_name.replace("hb:task:", "")
                log.info(f"Event-driven trigger: task {task_id} heartbeat expired.")
                # Trigger immediate (but rate-limited) reclaim for this task
                asyncio.create_task(perform_reclaim_cycle(task_id))

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def periodic_agent_health_check():
    """Module 3.2: Detects agent degradation periodically."""
    from services.memory_evolution import monitor_agent_health
    # We'd ideally iterate over all active tenants
    # For now, we simulate with a default or discoverable tenant list
    # log.info("Running periodic agent health check...")
    # await monitor_agent_health("default")
    pass

async def main():
    log.info("SINC Standalone Watchdog Service Initializing...")
    
    # 1. Start the periodic background loops
    periodic_task = asyncio.create_task(run_watchdog())
    health_task = asyncio.create_task(asyncio.sleep(0)) # Placeholder for real Ticker
    
    # 2. Start the event-driven listener
    expiry_task = asyncio.create_task(listen_for_task_expiry())
    
    # Handle Shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def stop():
        log.info("Shutting down Watchdog Service...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)

    await stop_event.wait()
    
    periodic_task.cancel()
    expiry_task.cancel()
    await asyncio.gather(periodic_task, expiry_task, return_exceptions=True)
    log.info("Watchdog Service stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

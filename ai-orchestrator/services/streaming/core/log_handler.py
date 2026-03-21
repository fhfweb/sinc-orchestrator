import logging
import json
import asyncio
from datetime import datetime, timezone
from services.event_bus import get_event_bus

class EventBusHandler(logging.Handler):
    """
    A logging handler that publishes log records to the SINC Event Bus (Redis Streams).
    This is the canonical way to handle diagnostic logs in the Python control plane (MIG-P5-004).
    """
    def __init__(self, component_name: str):
        super().__init__()
        self.component_name = component_name
        self._loop = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def emit(self, record):
        try:
            # We don't want to log the event bus's own logs to the event bus (recursion)
            if record.name.startswith("services.event_bus") or record.name.startswith("orchestrator.event_bus"):
                return

            msg = self.format(record)
            payload = {
                "component": self.component_name,
                "line": msg,
                "level": record.levelname,
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "logger": record.name,
                "trace_id": getattr(record, "trace_id", None),
            }

            # If we are in an async context, we can schedule the publish
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._publish(payload), self._loop)
            else:
                # Sync fallback is harder because EventBus is async.
                # In this system, most critical services are async.
                pass
        except Exception:
            self.handleError(record)

    async def _publish(self, payload: dict):
        try:
            bus = await get_event_bus()
            stream_key = f"sinc:stream:diagnostic_logs:{self.component_name}"
            await bus.publish(stream_key, payload, use_stream=True)
        except Exception:
            # Silent fail to avoid log loops
            pass

def setup_canonical_logging(component_name: str, level=logging.INFO):
    """Integrate EventBusHandler into the root logger."""
    root_logger = logging.getLogger()
    handler = EventBusHandler(component_name)
    handler.setLevel(level)
    
    # Use standard format if not already set
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(name)s: %(message)s'
    )
    handler.setFormatter(formatter)
    
    root_logger.addHandler(handler)
    return handler

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.event_bus import get_event_bus

log = logging.getLogger("orchestrator.task_lifecycle")


def task_lifecycle_channel(tenant_id: str) -> str:
    normalized = str(tenant_id or "").strip() or "local"
    return f"task_lifecycle:{normalized}"


def task_lifecycle_stream_name(tenant_id: str) -> str:
    return f"sinc:stream:{task_lifecycle_channel(tenant_id)}"


async def publish_task_lifecycle_event(
    *,
    task_id: str,
    tenant_id: str,
    event_type: str,
    status: str = "",
    agent_name: str = "orchestrator",
    payload: dict[str, Any] | None = None,
) -> None:
    message = {
        "type": event_type,
        "task_id": task_id,
        "tenant_id": tenant_id,
        "status": status,
        "agent_name": agent_name,
        "payload": payload or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        bus = await get_event_bus()
        await bus.publish(task_lifecycle_channel(tenant_id), message, use_stream=True)
    except Exception as exc:
        log.debug(
            "task_lifecycle_publish_error task_id=%s tenant_id=%s event_type=%s error=%s",
            task_id,
            tenant_id,
            event_type,
            exc,
        )

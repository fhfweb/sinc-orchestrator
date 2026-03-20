from __future__ import annotations

import json
from typing import Any

from .state_plane import table_columns, table_exists

_TABLE_COLUMNS_CACHE: dict[str, set[str]] = {}

async def get_table_columns_cached(cur, table_name: str, tenant_id: str = "default") -> set[str]:
    cache_key = f"{tenant_id}:{table_name}"
    cached = _TABLE_COLUMNS_CACHE.get(cache_key)

    if cached is not None:
        return cached
    if not await table_exists(cur, table_name):
        _TABLE_COLUMNS_CACHE[table_name] = set()
        return set()
    cols = await table_columns(cur, table_name)
    _TABLE_COLUMNS_CACHE[cache_key] = cols
    return cols



async def table_has_column(cur, table_name: str, column_name: str) -> bool:
    return column_name in await get_table_columns_cached(cur, table_name)


async def get_task_pk_column(cur) -> str:
    cols = await get_table_columns_cached(cur, "tasks")
    if "task_id" in cols:
        return "task_id"
    return "id"


async def get_dependency_ref_column(cur) -> str:
    cols = await get_table_columns_cached(cur, "dependencies")
    if "dependency_id" in cols:
        return "dependency_id"
    return "depends_on"


async def insert_agent_event(
    cur,
    *,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
    agent_name: str = "orchestrator",
    tenant_id: str | None = None,
) -> None:
    cols = await get_table_columns_cached(cur, "agent_events", tenant_id=tenant_id) # Added tenant_id
    if not cols:
        return

    insert_cols: list[str] = []
    values: list[Any] = []

    if "task_id" in cols:
        insert_cols.append("task_id")
        values.append(task_id)
    if "agent_name" in cols:
        insert_cols.append("agent_name")
        values.append(agent_name)
    elif "actor" in cols:
        insert_cols.append("actor")
        values.append(agent_name)
    if "event_type" in cols:
        insert_cols.append("event_type")
        values.append(event_type)
    if "tenant_id" in cols and tenant_id is not None:
        insert_cols.append("tenant_id")
        values.append(tenant_id)
    if "payload" in cols:
        insert_cols.append("payload")
        values.append(json.dumps(payload))

    if not insert_cols:
        return

    placeholders = ", ".join(["%s"] * len(insert_cols))
    await cur.execute(
        f"INSERT INTO agent_events ({', '.join(insert_cols)}) VALUES ({placeholders})",
        tuple(values),
    )

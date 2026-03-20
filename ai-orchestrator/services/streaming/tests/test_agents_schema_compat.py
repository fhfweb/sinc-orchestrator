from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_write_heartbeat_uses_task_id_conflict_when_schema_requires_it():
    from services.streaming.routes import agents

    cur = AsyncMock()
    cur.execute = AsyncMock()

    with patch.object(
        agents,
        "get_table_columns_cached",
        new=AsyncMock(return_value={"task_id", "agent_name", "beat_at", "progress_pct", "current_step", "metadata"}),
    ), patch.object(
        agents,
        "_get_heartbeat_time_column",
        new=AsyncMock(return_value="beat_at"),
    ), patch.object(
        agents,
        "_resolve_heartbeat_conflict_columns",
        new=AsyncMock(return_value=("task_id",)),
    ):
        await agents._write_heartbeat(
            cur,
            hb=agents.Heartbeat(
                task_id="TASK-123",
                progress_pct=55,
                current_step="running",
                metadata={"origin": "test"},
            ),
            agent_name="agent-alpha",
            tenant_id="local",
        )

    executed_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
    assert "ON CONFLICT (task_id)" in executed_sql
    assert "agent_name" in executed_sql


def test_normalize_completion_status_maps_legacy_done_to_success():
    from services.streaming.routes.core_compat import _normalize_completion_status

    assert _normalize_completion_status("done") == "success"
    assert _normalize_completion_status("completed") == "success"
    assert _normalize_completion_status("failure") == "failed"


@pytest.mark.asyncio
async def test_apply_agent_completion_does_not_require_task_type_column():
    from services.streaming.routes import agents

    conn = AsyncMock()
    conn.commit = AsyncMock()
    cur = AsyncMock()
    cur.execute = AsyncMock()
    cur.fetchone = AsyncMock(
        return_value={
            "requires_review": False,
            "verification_required": False,
            "title": "Fix auth",
            "project_id": "proj",
            "red_team_enabled": False,
            "created_at": None,
        }
    )

    async def _cols(_, table_name: str):
        if table_name == "tasks":
            return {"id", "status", "updated_at", "completed_at", "title", "project_id", "created_at"}
        if table_name == "agent_reputation":
            return {"agent_name", "tenant_id", "tasks_total", "tasks_success", "tasks_failure", "runtime_success_rate", "updated_at"}
        return set()

    with patch.object(agents, "get_task_pk_column", new=AsyncMock(return_value="id")), \
         patch.object(agents, "get_table_columns_cached", new=AsyncMock(side_effect=_cols)), \
         patch.object(agents, "_table_has_tenant", new=AsyncMock(return_value=True)), \
         patch.object(agents, "table_exists", new=AsyncMock(return_value=False)), \
         patch.object(agents, "insert_agent_event", new=AsyncMock()), \
         patch.object(agents, "_resolve_dependencies", new=AsyncMock(return_value=[])):
        result = await agents._apply_agent_completion(
            conn,
            cur,
            agent_name="agent-x",
            tenant_id="tenant-a",
            body=agents.Completion(task_id="TASK-1", status="success", summary="done"),
        )

    executed_sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
    assert "INSERT INTO agent_reputation" not in executed_sql
    assert "UPDATE agent_reputation" not in executed_sql
    assert result["audit_event"]["task_type"] == "generic"
    assert result["task_status"] == "done"

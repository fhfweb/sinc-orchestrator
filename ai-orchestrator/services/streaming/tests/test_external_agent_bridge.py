from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _wrap_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.commit = AsyncMock()
    return conn


def _make_local_temp_root() -> Path:
    root = Path.cwd() / ".bridge-test-artifacts" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.asyncio
async def test_resolve_dependencies_accepts_live_async_connection():
    from services.streaming.routes import tasks as tasks_routes

    cur = AsyncMock()
    cur.fetchall = AsyncMock(
        side_effect=[
            [{"task_id": "TASK-B", "assigned_agent": None, "title": "Task B", "tenant_id": "local"}],
            [{"task_id": "TASK-B"}],
        ]
    )
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)

    mock_bus = AsyncMock()
    mock_bus.publish = AsyncMock()

    with patch.object(
        tasks_routes,
        "get_task_pk_column",
        new=AsyncMock(return_value="task_id"),
    ), patch.object(
        tasks_routes,
        "get_dependency_ref_column",
        new=AsyncMock(return_value="dependency_id"),
    ), patch.object(
        tasks_routes,
        "insert_agent_event",
        new=AsyncMock(),
    ), patch(
        "services.event_bus.EventBus.get_instance",
        new=AsyncMock(return_value=mock_bus),
    ):
        result = await tasks_routes._resolve_dependencies("TASK-A", "local", conn=conn)

    assert result == ["TASK-B"]
    conn.cursor.assert_called_once()


@pytest.mark.asyncio
async def test_materialize_dispatches_writes_artifact_and_marks_delivered():
    from services.streaming.core import external_agent_bridge as bridge

    cur = AsyncMock()
    cur.fetchall = AsyncMock(
        return_value=[
            {
                "dispatch_id": 42,
                "dispatch_status": "pending",
                "dispatch_agent": "agent-ext",
                "dispatched_at": None,
                "task_id": "TASK-EXT-001",
                "title": "Handle external task",
                "description": "Dispatch through file bridge",
                "priority": 1,
                "assigned_agent": None,
                "project_id": "orchestrator",
                "metadata": {
                    "execution_mode": "external-agent",
                    "preferred_agent": "agent-ext",
                    "runtime_engine": "claude-code",
                    "files_affected": ["README.md"],
                },
            }
        ]
    )
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    root = _make_local_temp_root()
    dispatches = root / "dispatches"
    completions = root / "completions"
    try:
        with patch.object(bridge, "async_db", return_value=conn), patch.object(
            bridge,
            "get_table_columns_cached",
            new=AsyncMock(
                side_effect=[
                    {"task_id", "metadata", "tenant_id", "project_id"},
                    {"id", "status", "agent_name", "dispatch_payload", "dispatched_at"},
                    {"id", "status", "agent_name", "dispatch_payload", "dispatched_at"},
                ]
            ),
        ), patch.object(
            bridge,
            "get_task_pk_column",
            new=AsyncMock(return_value="task_id"),
        ), patch.object(
            bridge,
            "insert_agent_event",
            new=AsyncMock(),
        ), patch.object(bridge, "DISPATCHES", dispatches), patch.object(
            bridge, "IN_PROGRESS_DISPATCHES", dispatches / "in-progress"
        ), patch.object(
            bridge, "COMPLETIONS", completions
        ), patch.object(
            bridge, "PROCESSED_COMPLETIONS", completions / "processed"
        ):
            bridge._ensure_bridge_dirs()
            dispatched = await bridge._materialize_dispatches(tenant_id="local")

        assert dispatched == 1
        dispatch_file = dispatches / "TASK-EXT-001.json"
        assert dispatch_file.exists()
        payload = json.loads(dispatch_file.read_text(encoding="utf-8"))
        assert payload["task_id"] == "TASK-EXT-001"
        assert payload["assigned_agent"] == "agent-ext"
        executed_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
        assert "UPDATE webhook_dispatches" in executed_sql
        assert "SET status = 'delivered'" in executed_sql
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_process_completion_artifact_archives_and_cleans_dispatch():
    from services.streaming.core import external_agent_bridge as bridge

    cur = AsyncMock()
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    root = _make_local_temp_root()
    dispatches = root / "dispatches"
    completions = root / "completions"
    processed = completions / "processed"
    try:
        dispatches.mkdir(parents=True, exist_ok=True)
        completions.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)

        (dispatches / "TASK-EXT-002.json").write_text(
            json.dumps({"task_id": "TASK-EXT-002", "assigned_agent": "agent-ext"}, indent=2),
            encoding="utf-8",
        )
        completion_file = completions / "TASK-EXT-002.json"
        completion_file.write_text(
            json.dumps(
                {
                    "task_id": "TASK-EXT-002",
                    "agent_name": "agent-ext",
                    "status": "success",
                    "summary": "Completed externally",
                    "files_modified": [],
                    "tests_passed": True,
                    "backend_used": "claude-code",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with patch.object(bridge, "async_db", return_value=conn), patch.object(
            bridge,
            "_apply_agent_completion",
            new=AsyncMock(return_value={"task_status": "done", "unblocked": [], "project_id": "orchestrator", "files_modified": []}),
        ), patch.object(
            bridge,
            "_sync_digital_twin",
            new=AsyncMock(),
        ), patch.object(bridge, "DISPATCHES", dispatches), patch.object(
            bridge, "IN_PROGRESS_DISPATCHES", dispatches / "in-progress"
        ), patch.object(
            bridge, "COMPLETIONS", completions
        ), patch.object(
            bridge, "PROCESSED_COMPLETIONS", processed
        ):
            ok = await bridge._process_completion_artifact(completion_file, tenant_id="local")

        assert ok is True
        assert not (dispatches / "TASK-EXT-002.json").exists()
        assert not completion_file.exists()
        assert (processed / "TASK-EXT-002.json").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)

"""
tests/test_runtime_plane.py
===========================
Regression coverage for the Python runtime plane.

Focus:
- schema compatibility for tasks keyed by ``task_id`` instead of ``id``
- scheduler dispatch flow against partially-evolved Postgres schemas
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _wrap_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.commit = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_compute_readiness_snapshot_supports_task_id_schema():
    from services.streaming.core import runtime_plane

    cur = AsyncMock()
    cur.fetchone = AsyncMock(
        side_effect=[
            {
                "total": 7,
                "pending": 2,
                "in_progress": 1,
                "failed": 0,
                "blocked": 3,
                "open_repairs": 1,
            },
            {"active_agents": 2},
            {"open_incidents": 0},
        ]
    )
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    with patch.object(runtime_plane, "async_db", return_value=conn), patch.object(
        runtime_plane,
        "get_table_columns_cached",
        new=AsyncMock(
            side_effect=[
                {"task_id", "tenant_id", "status"},
                {"agent_name", "beat_at"},
            ]
        ),
    ), patch.object(runtime_plane, "get_task_pk_column", new=AsyncMock(return_value="task_id")), patch.object(
        runtime_plane, "ensure_runtime_plane_schema", new=AsyncMock()
    ):
        snapshot = await runtime_plane.compute_readiness_snapshot("local")

    assert snapshot["source"] == "db"
    assert snapshot["counts"]["open_repairs"] == 1
    executed_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
    assert "task_id ILIKE 'REPAIR-%%'" in executed_sql


@pytest.mark.asyncio
async def test_scheduler_tick_once_dispatches_with_task_id_schema():
    from services.streaming.core import runtime_plane

    cur = AsyncMock()
    cur.fetchall = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "task_id": "TASK-001",
                    "title": "Migrate runtime plane",
                    "description": "Dispatch from Python scheduler",
                    "priority": 1,
                    "project_id": "orchestrator",
                    "assigned_agent": None,
                }
            ],
            [{"agent_name": "agent-alpha"}],
        ]
    )
    cur.fetchone = AsyncMock(return_value={"locked": True})
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    with patch.object(runtime_plane, "async_db", return_value=conn), patch.object(
        runtime_plane,
        "get_table_columns_cached",
        new=AsyncMock(
            side_effect=[
                {"task_id", "tenant_id", "status", "project_id", "assigned_agent"},
                {"task_id", "agent_name", "status", "dispatch_payload", "dispatched_at"},
                {"agent_name", "reputation_fit_score", "tasks_total"},
            ]
        ),
    ), patch.object(runtime_plane, "get_task_pk_column", new=AsyncMock(return_value="task_id")), patch.object(
        runtime_plane, "get_dependency_ref_column", new=AsyncMock(return_value="dependency_id")
    ), patch.object(runtime_plane, "ensure_runtime_plane_schema", new=AsyncMock()), patch.object(
        runtime_plane, "insert_agent_event", new=AsyncMock()
    ), patch.object(runtime_plane, "_unlock_tick", new=AsyncMock()):
        result = await runtime_plane.scheduler_tick_once("local", "orchestrator")

    assert result["status"] == "ok"
    assert result["dispatched"] == 1
    executed_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
    assert "t.task_id AS task_id" in executed_sql
    assert "wd.task_id = t.task_id" in executed_sql
    update_calls = [
        call
        for call in cur.execute.call_args_list
        if "UPDATE tasks SET assigned_agent" in str(call.args[0])
    ]
    assert update_calls
    assert update_calls[0].args[1] == ("agent-alpha", "TASK-001")


@pytest.mark.asyncio
async def test_compute_readiness_snapshot_excludes_runtime_readiness_incidents():
    from services.streaming.core import runtime_plane

    cur = AsyncMock()
    cur.fetchone = AsyncMock(
        side_effect=[
            {
                "total": 4,
                "pending": 1,
                "in_progress": 0,
                "failed": 0,
                "blocked": 0,
                "open_repairs": 0,
            },
            {"active_agents": 1},
            {"open_incidents": 2},
        ]
    )
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    with patch.object(runtime_plane, "async_db", return_value=conn), patch.object(
        runtime_plane,
        "get_table_columns_cached",
        new=AsyncMock(
            side_effect=[
                {"id", "tenant_id", "status"},
                {"agent_name", "beat_at"},
            ]
        ),
    ), patch.object(runtime_plane, "ensure_runtime_plane_schema", new=AsyncMock()):
        snapshot = await runtime_plane.compute_readiness_snapshot("local")

    assert snapshot["counts"]["open_incidents"] == 2
    executed_sql = " ".join(str(call.args[0]) for call in cur.execute.call_args_list if call.args)
    assert "category <> 'runtime-readiness'" in executed_sql


@pytest.mark.asyncio
async def test_compute_readiness_snapshot_exposes_cognitive_quality():
    from services.streaming.core import runtime_plane

    cur = AsyncMock()
    cur.fetchone = AsyncMock(
        side_effect=[
            {
                "total": 2,
                "pending": 0,
                "in_progress": 0,
                "failed": 0,
                "blocked": 0,
                "open_repairs": 0,
            },
            {"active_agents": 1},
            {"open_incidents": 0},
        ]
    )
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = _wrap_conn(cur)

    with patch.object(runtime_plane, "async_db", return_value=conn), patch.object(
        runtime_plane,
        "get_table_columns_cached",
        new=AsyncMock(
            side_effect=[
                {"id", "tenant_id", "status"},
                {"agent_name", "beat_at"},
            ]
        ),
    ), patch.object(runtime_plane, "ensure_runtime_plane_schema", new=AsyncMock()), patch.object(
        runtime_plane,
        "_get_cognitive_quality_snapshot",
        return_value={
            "quality_status": "limited",
            "score": 0.4,
            "critical_missing": ["planner"],
            "optional_missing": [],
            "summary": "critical gaps: planner",
        },
    ):
        snapshot = await runtime_plane.compute_readiness_snapshot("local")

    assert snapshot["status"] == "degraded"
    assert snapshot["health"] == "degraded"
    assert snapshot["quality"] == "degraded"
    assert snapshot["cognitive_status"] == "limited"
    assert snapshot["cognitive"]["critical_missing"] == ["planner"]


@pytest.mark.asyncio
async def test_reconcile_incidents_resolves_superseded_runtime_readiness():
    from services.streaming.core import runtime_plane

    resolve_open = AsyncMock(side_effect=[0, 1])
    resolve_watchdog = AsyncMock(return_value=2)
    readiness = {
        "tenant_id": "local",
        "status": "not_ready",
        "counts": {
            "failed": 0,
            "blocked": 0,
            "open_repairs": 0,
            "open_incidents": 3,
            "active_agents": 1,
        },
    }

    with patch.object(runtime_plane, "_resolve_open_incidents", new=resolve_open), patch.object(
        runtime_plane,
        "_resolve_watchdog_stale_recovery_incidents",
        new=resolve_watchdog,
    ):
        result = await runtime_plane.reconcile_incidents(
            tenant_id="local",
            project_id="sinc",
            readiness=readiness,
            stale_tasks=[],
        )

    assert result["resolved"] == 3
    assert resolve_watchdog.await_count == 1
    assert resolve_open.await_count == 2
    final_call = resolve_open.await_args_list[-1]
    assert final_call.kwargs["category"] == "runtime-readiness"
    assert final_call.kwargs["exclude_fingerprint"] == "readiness:local:failed=0:blocked=0:repairs=0:incidents=3:active_agents=1"

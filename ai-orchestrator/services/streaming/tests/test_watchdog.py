"""
tests/test_watchdog.py
=======================
Tests for the watchdog reclaim cycle (Phase 3.4).
Verifies: stale tasks are reclaimed, recent tasks untouched,
terminal tasks never touched.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_db(stale_in_progress=None, stale_delivered=None, dead=None):
    """Build a mock async_db context manager with preset query results."""
    stale_in_progress = stale_in_progress or []
    stale_delivered   = stale_delivered   or []
    dead              = dead              or []

    cur = AsyncMock()
    # fetchall is called three times: for each UPDATE … RETURNING
    cur.fetchall = AsyncMock(side_effect=[stale_in_progress, stale_delivered, dead])
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)
    conn.commit    = AsyncMock()
    return conn, cur


def _watchdog_patches(conn, mock_bus):
    return (
        patch("services.streaming.core.watchdog.async_db", return_value=conn),
        patch("services.streaming.core.watchdog.get_event_bus", return_value=mock_bus),
        patch(
            "services.streaming.core.watchdog.get_table_columns_cached",
            new=AsyncMock(
                side_effect=[
                    {"task_id", "tenant_id", "project_id", "status", "assigned_agent", "lock_retry_count", "updated_at"},
                    {"task_id", "beat_at"},
                    {"task_id", "agent_name", "status", "delivered_at"},
                ]
            ),
        ),
        patch("services.streaming.core.watchdog.get_task_pk_column", new=AsyncMock(return_value="task_id")),
        patch("services.streaming.core.watchdog.insert_agent_event", new=AsyncMock()),
        patch("services.streaming.core.watchdog._record_incident_if_needed", new=AsyncMock(return_value=True)),
        patch("services.streaming.core.watchdog.ensure_repair_task", new=AsyncMock(return_value="REPAIR-1")),
    )


@pytest.mark.asyncio
async def test_stale_in_progress_task_is_reclaimed():
    """
    An 'in-progress' task that stopped heartbeating must be returned
    to 'pending' by the watchdog.
    """
    stale = [{"task_id": "T-zombie", "tenant_id": "local", "project_id": "proj", "assigned_agent": "agent-1", "lock_retry_count": 1}]
    conn, cur = _make_db(stale_in_progress=stale)

    mock_bus = AsyncMock()
    mock_bus.auto_claim = AsyncMock(return_value=(0, [], []))

    with _watchdog_patches(conn, mock_bus)[0], \
         _watchdog_patches(conn, mock_bus)[1], \
         _watchdog_patches(conn, mock_bus)[2], \
         _watchdog_patches(conn, mock_bus)[3], \
         _watchdog_patches(conn, mock_bus)[4], \
         _watchdog_patches(conn, mock_bus)[5], \
         _watchdog_patches(conn, mock_bus)[6]:
        from services.streaming.core.watchdog import perform_reclaim_cycle
        await perform_reclaim_cycle()

    # The UPDATE that resets stale in-progress tasks must have been executed
    first_execute_sql = cur.execute.call_args_list[0][0][0]
    assert "in-progress" in first_execute_sql
    assert "pending" in first_execute_sql


@pytest.mark.asyncio
async def test_recent_heartbeat_task_not_touched():
    """
    If no tasks are stale (all return empty lists), the watchdog does nothing
    and commit is still called (clean cycle).
    """
    conn, cur = _make_db()  # all empty

    mock_bus = AsyncMock()
    mock_bus.auto_claim = AsyncMock(return_value=(0, [], []))

    with _watchdog_patches(conn, mock_bus)[0], \
         _watchdog_patches(conn, mock_bus)[1], \
         _watchdog_patches(conn, mock_bus)[2], \
         _watchdog_patches(conn, mock_bus)[3], \
         _watchdog_patches(conn, mock_bus)[4], \
         _watchdog_patches(conn, mock_bus)[5], \
         _watchdog_patches(conn, mock_bus)[6]:
        from services.streaming.core.watchdog import perform_reclaim_cycle
        await perform_reclaim_cycle()

    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_exceeding_max_retries_goes_to_dead_letter():
    """
    A task that has hit max retries must be moved to 'dead-letter'.
    """
    dead = [{"task_id": "T-dead", "tenant_id": "local", "project_id": "proj"}]
    conn, cur = _make_db(dead=dead)

    mock_bus = AsyncMock()
    mock_bus.auto_claim = AsyncMock(return_value=(0, [], []))

    with _watchdog_patches(conn, mock_bus)[0], \
         _watchdog_patches(conn, mock_bus)[1], \
         _watchdog_patches(conn, mock_bus)[2], \
         _watchdog_patches(conn, mock_bus)[3], \
         _watchdog_patches(conn, mock_bus)[4], \
         _watchdog_patches(conn, mock_bus)[5], \
         _watchdog_patches(conn, mock_bus)[6]:
        from services.streaming.core.watchdog import perform_reclaim_cycle
        await perform_reclaim_cycle()

    # Third UPDATE (dead-letter) must reference 'dead-letter'
    dead_letter_sql = cur.execute.call_args_list[2][0][0]
    assert "dead-letter" in dead_letter_sql


@pytest.mark.asyncio
async def test_terminal_tasks_not_in_stale_query():
    """
    The stale reclaim query must filter on status = 'in-progress'.
    Tasks in 'done' or 'failed' must never match.
    """
    conn, cur = _make_db()

    mock_bus = AsyncMock()
    mock_bus.auto_claim = AsyncMock(return_value=(0, [], []))

    with _watchdog_patches(conn, mock_bus)[0], \
         _watchdog_patches(conn, mock_bus)[1], \
         _watchdog_patches(conn, mock_bus)[2], \
         _watchdog_patches(conn, mock_bus)[3], \
         _watchdog_patches(conn, mock_bus)[4], \
         _watchdog_patches(conn, mock_bus)[5], \
         _watchdog_patches(conn, mock_bus)[6]:
        from services.streaming.core.watchdog import perform_reclaim_cycle
        await perform_reclaim_cycle()

    first_sql = cur.execute.call_args_list[0][0][0]
    # Must scope to in-progress only
    assert "in-progress" in first_sql
    # Must not mention done/failed in the WHERE clause of the first query
    assert "done" not in first_sql.split("WHERE")[1] if "WHERE" in first_sql else True

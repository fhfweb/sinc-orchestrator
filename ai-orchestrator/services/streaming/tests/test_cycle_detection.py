"""
tests/test_cycle_detection.py
==============================
Tests for WITH RECURSIVE cycle detection (Phase 1.2).
Uses mocked DB cursor — no live PostgreSQL required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_cursor(has_cycle: bool):
    """Return a mock async cursor whose fetchone returns has_cycle."""
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value={"has_cycle": has_cycle})
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)
    return conn


@pytest.mark.asyncio
async def test_no_deps_returns_false():
    """If new_deps is empty, _has_cycle must short-circuit to False without DB call."""
    with patch("streaming.routes.tasks.async_db") as mock_db:
        from services.streaming.routes.tasks import _has_cycle
        result = await _has_cycle("T-new", [], "tenant-1")

    assert result is False
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_db_reports_cycle_returns_true():
    """When the CTE query returns has_cycle=True, _has_cycle must return True."""
    cursor = _make_cursor(has_cycle=True)
    conn   = _make_conn(cursor)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _has_cycle
        result = await _has_cycle("T-A", ["T-B", "T-C"], "tenant-1")

    assert result is True


@pytest.mark.asyncio
async def test_db_reports_no_cycle_returns_false():
    """When the CTE query returns has_cycle=False, _has_cycle must return False."""
    cursor = _make_cursor(has_cycle=False)
    conn   = _make_conn(cursor)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _has_cycle
        result = await _has_cycle("T-new", ["T-existing"], "tenant-1")

    assert result is False


@pytest.mark.asyncio
async def test_query_includes_correct_params():
    """
    The CTE query must be called with (dep_list, tenant_id, new_task_id, new_task_id).
    Verifies the parameter order and content.
    """
    cursor = _make_cursor(has_cycle=False)
    conn   = _make_conn(cursor)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _has_cycle
        await _has_cycle("TASK-NEW", ["DEP-1", "DEP-2"], "tenant-42")

    # The execute call should have been made once
    cursor.execute.assert_awaited_once()
    _, params = cursor.execute.await_args
    actual_params = params[0]  # positional args tuple
    assert actual_params == (["DEP-1", "DEP-2"], "tenant-42", "TASK-NEW", "TASK-NEW")


@pytest.mark.asyncio
async def test_five_node_linear_graph_no_cycle():
    """
    Linear graph A→B→C→D→E.
    Adding F depending on A should NOT create a cycle.
    """
    # The CTE would not find F in the reachable set (F doesn't exist yet)
    cursor = _make_cursor(has_cycle=False)
    conn   = _make_conn(cursor)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _has_cycle
        result = await _has_cycle("F", ["A"], "tenant-1")

    assert result is False


@pytest.mark.asyncio
async def test_closing_cycle_detected():
    """
    Graph: A depends on B, B depends on C.
    Trying to make C depend on A should be detected as a cycle.
    """
    # If A is reachable from C (via C→B→A) the DB returns has_cycle=True
    cursor = _make_cursor(has_cycle=True)
    conn   = _make_conn(cursor)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _has_cycle
        result = await _has_cycle("C", ["A"], "tenant-1")

    assert result is True

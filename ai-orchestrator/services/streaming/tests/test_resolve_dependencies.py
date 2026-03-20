"""
tests/test_resolve_dependencies.py
=====================================
Tests for _resolve_dependencies (Phase 3.3 — batched N+1 fix).

The function now issues:
  1. One CTE SELECT  → candidates list
  2. One batch UPDATE ANY(%s) RETURNING  → confirmed IDs
  3. One executemany INSERT into agent_events
  4. EventBus publish per pre-assigned agent

All DB round-trips are covered by the mock cursor below.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_cursor(candidates=None, confirmed_ids=None):
    """
    Build a mock async cursor for the batched _resolve_dependencies flow.

    candidates    – rows returned by the CTE SELECT (list of dicts)
    confirmed_ids – rows returned by the batch UPDATE RETURNING (list of dicts with "id")
    """
    candidates   = candidates   or []
    confirmed_ids = confirmed_ids or []

    cur = AsyncMock()
    # fetchall is called twice: once for candidates, once for RETURNING rows.
    cur.fetchall = AsyncMock(side_effect=[candidates, confirmed_ids])
    cur.executemany = AsyncMock()
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.cursor   = MagicMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)
    conn.commit   = AsyncMock()
    return conn


def _make_bus():
    bus = AsyncMock()
    bus.publish = AsyncMock()
    bus.get_instance = AsyncMock(return_value=bus)
    return bus


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_waiting_tasks_returns_empty():
    """No blocked tasks waiting on the completed task → empty list."""
    cur  = _make_cursor(candidates=[])
    conn = _make_conn(cur)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _resolve_dependencies
        result = await _resolve_dependencies("T-done", "tenant-1")

    assert result == []
    # The batch UPDATE must NOT be issued when there are no candidates.
    # fetchall is called only once (for candidates), not twice.
    assert cur.fetchall.call_count == 1


@pytest.mark.asyncio
async def test_all_deps_done_unblocks_task():
    """
    Task T-waiting has all dependencies satisfied.
    The batch UPDATE confirms the transition → T-waiting in result.
    """
    candidates = [{"task_id": "T-waiting", "assigned_agent": None,
                   "title": "wait", "tenant_id": "tenant-1"}]
    confirmed  = [{"id": "T-waiting"}]
    cur  = _make_cursor(candidates=candidates, confirmed_ids=confirmed)
    conn = _make_conn(cur)
    bus  = _make_bus()

    with patch("streaming.routes.tasks.async_db", return_value=conn), \
         patch("streaming.routes.tasks.EventBus.get_instance",
               AsyncMock(return_value=bus)):
        from services.streaming.routes.tasks import _resolve_dependencies
        result = await _resolve_dependencies("T-done", "tenant-1")

    assert result == ["T-waiting"]
    # executemany must have been called once for agent_events
    cur.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_multiple_tasks_unblocked_batch():
    """
    Three tasks all become unblocked by the same completed task.
    Verifies batching: only 1 UPDATE query (not 3) and 1 executemany.
    """
    candidates = [
        {"task_id": "T-A", "assigned_agent": None, "title": "a", "tenant_id": "t1"},
        {"task_id": "T-B", "assigned_agent": None, "title": "b", "tenant_id": "t1"},
        {"task_id": "T-C", "assigned_agent": "agent-x", "title": "c", "tenant_id": "t1"},
    ]
    confirmed  = [{"id": "T-A"}, {"id": "T-B"}, {"id": "T-C"}]
    cur  = _make_cursor(candidates=candidates, confirmed_ids=confirmed)
    conn = _make_conn(cur)
    bus  = _make_bus()

    with patch("streaming.routes.tasks.async_db", return_value=conn), \
         patch("streaming.routes.tasks.EventBus.get_instance",
               AsyncMock(return_value=bus)):
        from services.streaming.routes.tasks import _resolve_dependencies
        result = await _resolve_dependencies("T-dep", "tenant-1")

    assert set(result) == {"T-A", "T-B", "T-C"}
    # Only one executemany call, not N individual inserts.
    cur.executemany.assert_called_once()
    # EventBus publish called once for T-C (the pre-assigned agent task).
    assert bus.publish.call_count == 1


@pytest.mark.asyncio
async def test_race_condition_none_confirmed():
    """
    UPDATE RETURNING yields nothing (another worker already claimed the task).
    Result must be empty — no zombie unblocking.
    """
    candidates = [{"task_id": "T-raced", "assigned_agent": None,
                   "title": "race", "tenant_id": "t1"}]
    confirmed  = []  # another worker won
    cur  = _make_cursor(candidates=candidates, confirmed_ids=confirmed)
    conn = _make_conn(cur)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        from services.streaming.routes.tasks import _resolve_dependencies
        result = await _resolve_dependencies("T-dep", "tenant-1")

    assert result == []
    # executemany must NOT be called if no rows were confirmed.
    cur.executemany.assert_not_called()


@pytest.mark.asyncio
async def test_agent_dispatch_only_for_assigned():
    """
    EventBus.publish should only fire for tasks with a pre-assigned agent.
    """
    candidates = [
        {"task_id": "T-noagent", "assigned_agent": None,  "title": "x", "tenant_id": "t"},
        {"task_id": "T-agent",   "assigned_agent": "bob", "title": "y", "tenant_id": "t"},
    ]
    confirmed  = [{"id": "T-noagent"}, {"id": "T-agent"}]
    cur  = _make_cursor(candidates=candidates, confirmed_ids=confirmed)
    conn = _make_conn(cur)
    bus  = _make_bus()

    with patch("streaming.routes.tasks.async_db", return_value=conn), \
         patch("streaming.routes.tasks.EventBus.get_instance",
               AsyncMock(return_value=bus)):
        from services.streaming.routes.tasks import _resolve_dependencies
        result = await _resolve_dependencies("T-trigger", "tenant-1")

    assert set(result) == {"T-noagent", "T-agent"}
    # Only T-agent gets a dispatch event.
    assert bus.publish.call_count == 1
    call_kwargs = bus.publish.call_args[0][1]  # second positional arg is the payload
    assert call_kwargs["task_id"] == "T-agent"
    assert call_kwargs["agent"] == "bob"

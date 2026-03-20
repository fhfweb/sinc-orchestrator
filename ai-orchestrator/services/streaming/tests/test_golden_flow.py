"""
tests/test_golden_flow.py
==========================
Golden flow integration test (Phase 4.1).

Covers the full task lifecycle end-to-end with mocked DB and Redis:
  1. Create task A (no dependencies)
  2. Create task B (depends on A) → status becomes 'blocked-deps'
  3. Agent picks up task A via GET /agents/{name}/tasks
  4. Agent completes task A → status transitions to 'done'
  5. _resolve_dependencies unblocks task B → status becomes 'pending'

No live PostgreSQL or Redis instance required.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── DB mock factories ─────────────────────────────────────────────────────────

def _make_create_task_cursor(task_id: str, tenant_id: str, plan: str = "free"):
    """Cursor for POST /tasks — quota check + insert."""
    cur = AsyncMock()
    # quota SELECT COUNT(*) → below limit
    quota_row   = {"count": 0}
    # no-cycle check → fetchone has_cycle=False
    cycle_row   = {"has_cycle": False}
    # INSERT RETURNING
    insert_row  = {"id": task_id, "status": "pending"}

    cur.fetchone  = AsyncMock(side_effect=[quota_row, cycle_row, insert_row])
    cur.fetchall  = AsyncMock(return_value=[])
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)
    return cur


def _make_pickup_cursor(task_id: str, agent: str, tenant_id: str):
    """Cursor for GET /agents/{name}/tasks — dispatch query + update."""
    cur = AsyncMock()
    dispatch_row = {
        "id": "D-1",
        "task_id": task_id,
        "dispatch_payload": {"id": task_id, "title": "Task A"},
    }
    cur.fetchall  = AsyncMock(return_value=[dispatch_row])
    cur.fetchone  = AsyncMock(return_value=None)
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)
    return cur


def _make_completion_cursor(task_id: str, blocked_task_id: str, tenant_id: str):
    """
    Cursor for POST /agents/{name}/completion.
    Covers: dispatch update, task meta fetch, task status update,
    dependency resolution and audit publication.
    """
    cur = AsyncMock()
    task_meta   = {"requires_review": False, "verification_required": False,
                   "title": "Task A", "project_id": "P-1"}
    # _resolve_dependencies: candidates for blocked_task_id
    dep_candidates = [{
        "task_id": blocked_task_id, "assigned_agent": None,
        "title": "Task B", "description": "", "priority": 2, "project_id": "P-1",
    }]
    dep_counts  = {"total": 1, "done_count": 1}
    unblock_row = {"id": blocked_task_id}

    cur.fetchone = AsyncMock(side_effect=[
        None,           # UPDATE webhook_dispatches (no return needed, just executes)
        task_meta,      # SELECT requires_review, verification_required …
        None,           # UPDATE tasks SET status = 'done'
        dep_counts,     # SELECT COUNT(*) FILTER (WHERE status='done') AS done_count
        unblock_row,    # UPDATE tasks SET status = 'pending' … RETURNING id
    ])
    cur.fetchall = AsyncMock(side_effect=[
        dep_candidates,  # SELECT candidates for _resolve_dependencies
        [],              # fallback fetchall (safety)
    ])
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)
    return cur


def _wrap_conn(cur):
    conn = MagicMock()
    conn.cursor  = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__  = AsyncMock(return_value=False)
    conn.commit  = AsyncMock()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

TENANT_ID  = "tenant-golden"
TASK_A_ID  = "T-A"
TASK_B_ID  = "T-B"
AGENT_NAME = "agent-alpha"
TENANT_OBJ = {"id": TENANT_ID, "plan": "free"}


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_task_a_no_deps():
    """
    Step 1: POST /tasks with no dependency_ids.
    Expects HTTP 201 and the returned task_id == TASK_A_ID.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.tasks import router as tasks_router

    app = FastAPI()
    app.include_router(tasks_router)

    cur  = _make_create_task_cursor(TASK_A_ID, TENANT_ID)
    conn = _wrap_conn(cur)

    with patch("streaming.routes.tasks.async_db",       return_value=conn), \
         patch("streaming.routes.tasks.get_tenant_id",  return_value=TENANT_ID), \
         patch("streaming.routes.tasks.get_tenant",     return_value=TENANT_OBJ), \
         patch("streaming.routes.tasks.check_quota",    new=AsyncMock()), \
         patch("streaming.routes.tasks.broadcast",      new=AsyncMock()), \
         patch("streaming.routes.tasks._has_cycle",     new=AsyncMock(return_value=False)):

        # Override INSERT RETURNING with a simpler mock
        cur.fetchone = AsyncMock(side_effect=[
            {"count": 0},                               # quota check
            {"id": TASK_A_ID, "status": "pending"},     # INSERT RETURNING
        ])

        client   = TestClient(app, raise_server_exceptions=True)
        response = client.post("/tasks", json={
            "id":    TASK_A_ID,
            "title": "Task A",
        })

    assert response.status_code == 201
    data = response.json()
    assert data.get("id") == TASK_A_ID or data.get("task_id") == TASK_A_ID or TASK_A_ID in str(data)


@pytest.mark.asyncio
async def test_create_task_b_with_dep_on_a():
    """
    Step 2: POST /tasks with dependency_ids=[TASK_A_ID].
    No cycle → task is created with status 'blocked-deps'.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.tasks import router as tasks_router

    app = FastAPI()
    app.include_router(tasks_router)

    cur  = _make_create_task_cursor(TASK_B_ID, TENANT_ID)
    conn = _wrap_conn(cur)

    cur.fetchone = AsyncMock(side_effect=[
        {"count": 1},                                     # quota: 1 existing task
        {"id": TASK_B_ID, "status": "blocked-deps"},      # INSERT RETURNING
    ])

    with patch("streaming.routes.tasks.async_db",       return_value=conn), \
         patch("streaming.routes.tasks.get_tenant_id",  return_value=TENANT_ID), \
         patch("streaming.routes.tasks.get_tenant",     return_value=TENANT_OBJ), \
         patch("streaming.routes.tasks.check_quota",    new=AsyncMock()), \
         patch("streaming.routes.tasks.broadcast",      new=AsyncMock()), \
         patch("streaming.routes.tasks._has_cycle",     new=AsyncMock(return_value=False)):

        client   = TestClient(app, raise_server_exceptions=True)
        response = client.post("/tasks", json={
            "id":             TASK_B_ID,
            "title":          "Task B",
            "dependency_ids": [TASK_A_ID],
        })

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_agent_picks_up_task_a():
    """
    Step 3: GET /agents/{name}/tasks — agent claims Task A from the dispatch queue.
    Expects the response to include TASK_A_ID.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.agents import router as agents_router

    app = FastAPI()
    app.include_router(agents_router)

    cur  = _make_pickup_cursor(TASK_A_ID, AGENT_NAME, TENANT_ID)
    conn = _wrap_conn(cur)

    with patch("streaming.routes.agents.async_db",      return_value=conn), \
         patch("streaming.routes.agents.get_tenant_id", return_value=TENANT_ID):

        client   = TestClient(app, raise_server_exceptions=True)
        response = client.get(f"/agents/{AGENT_NAME}/tasks")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    assert any(t.get("id") == TASK_A_ID for t in data["tasks"])


@pytest.mark.asyncio
async def test_agent_completes_task_a_unblocks_task_b():
    """
    Step 4 + 5: POST /agents/{name}/completion for Task A.
    - Task A status → 'done'.
    - _resolve_dependencies fires → Task B moves from 'blocked-deps' to 'pending'.
    The test verifies that the UPDATE statement that unblocks Task B was executed.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.agents import router as agents_router

    app = FastAPI()
    app.include_router(agents_router)

    cur  = _make_completion_cursor(TASK_A_ID, TASK_B_ID, TENANT_ID)
    conn = _wrap_conn(cur)

    # The completion handler calls conn.cursor() directly (not via async with) for
    # _resolve_dependencies, so we must also return the same cursor from cursor()
    conn.cursor = MagicMock(return_value=cur)

    mock_bus = AsyncMock()
    mock_bus.connect = AsyncMock()
    mock_bus.publish = AsyncMock()

    with patch("streaming.routes.agents.async_db",         return_value=conn), \
         patch("streaming.routes.agents.get_tenant_id",    return_value=TENANT_ID), \
         patch("streaming.routes.agents.broadcast",        new=AsyncMock()), \
         patch("streaming.routes.tasks.async_db",          return_value=conn), \
         patch("streaming.routes.tasks.EventBus",          return_value=mock_bus, create=True):

        client   = TestClient(app, raise_server_exceptions=True)
        response = client.post(f"/agents/{AGENT_NAME}/completion", json={
            "task_id":        TASK_A_ID,
            "status":         "success",
            "summary":        "Task A completed successfully.",
            "files_modified": [],
            "tests_passed":   True,
        })

    assert response.status_code == 200

    # Verify that a query mentioning 'blocked-deps' was issued (dependency resolution)
    all_sql = " ".join(
        str(c[0][0]) for c in cur.execute.call_args_list if c[0]
    )
    assert "blocked-deps" in all_sql, (
        "Expected _resolve_dependencies to query for 'blocked-deps' tasks"
    )


@pytest.mark.asyncio
async def test_full_flow_no_zombie_on_concurrent_completion():
    """
    Regression: if two agents try to complete the same task simultaneously,
    only the first UPDATE RETURNING succeeds.  _resolve_dependencies must not
    add the task to the unblocked list when RETURNING yields None.
    """
    from services.streaming.routes.tasks import _resolve_dependencies

    cur = AsyncMock()
    # candidates contain T-B
    cur.fetchall = AsyncMock(return_value=[{
        "task_id": TASK_B_ID, "assigned_agent": None,
        "title": "B", "description": "", "priority": 2, "project_id": "",
    }])
    # dep counts: fully satisfied
    # UPDATE RETURNING: None → another worker won the race
    cur.fetchone  = AsyncMock(side_effect=[{"total": 1, "done_count": 1}, None])
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__  = AsyncMock(return_value=False)

    conn = _wrap_conn(cur)

    with patch("streaming.routes.tasks.async_db", return_value=conn):
        result = await _resolve_dependencies(TASK_A_ID, TENANT_ID)

    assert result == [], f"Expected no unblocked tasks (race lost), got {result}"

"""
tests/test_audit_v2.py
======================
Tests for the V2.0 audit fixes:

  C1  — _validated_wdir rejects path traversal; Docker is preferred over host exec
  C1  — _safe_execute returns ("failed", ..., 1) for out-of-workspace wdir
  R1  — _main_loop is the real implementation (no stub override)
  R2  — _http_complete re-raises on API failure (task does not get stuck)
  R2  — update_task_in_db re-raises on DB failure (file mode)
"""
import asyncio
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _worker_module():
    """import services.agent_worker with ORCHESTRATOR_URL unset (file mode defaults)."""
    # Ensure the module is fresh for each test that needs it
    import importlib
    import services.agent_worker
    importlib.reload(agent_worker)
    return agent_worker


# ---------------------------------------------------------------------------
# C1 — path traversal guard
# ---------------------------------------------------------------------------

class TestValidatedWdir:
    def setup_method(self):
        # Always reload so WORKSPACE reflects env changes
        import importlib, agent_worker
        importlib.reload(agent_worker)
        self.mod = agent_worker

    def test_path_inside_workspace_accepted(self, tmp_path):
        sub = tmp_path / "project"
        sub.mkdir()
        with patch.object(self.mod, "WORKSPACE", tmp_path):
            result = self.mod._validated_wdir(str(sub))
        assert result == str(sub.resolve())

    def test_path_traversal_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        with patch.object(self.mod, "WORKSPACE", workspace):
            with pytest.raises(ValueError, match="outside WORKSPACE"):
                self.mod._validated_wdir(str(outside))

    def test_dotdot_traversal_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        traversal = str(workspace) + "/../secret"
        with patch.object(self.mod, "WORKSPACE", workspace):
            with pytest.raises(ValueError, match="outside WORKSPACE"):
                self.mod._validated_wdir(traversal)


@pytest.mark.asyncio
async def test_safe_execute_rejects_bad_wdir(tmp_path):
    """_safe_execute must return failure status when wdir is outside WORKSPACE."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with patch.object(agent_worker, "WORKSPACE", workspace):
        status, output, rc = await agent_worker._safe_execute("echo hi", str(outside))

    assert status == "failed"
    assert "outside WORKSPACE" in output
    assert rc == 1


@pytest.mark.asyncio
async def test_safe_execute_prefers_docker_when_available(tmp_path):
    """When _HAS_DOCKER=True, _safe_execute must call _docker_execute, not _host_execute."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    docker_called = []
    host_called   = []

    async def _fake_docker(script, wdir, timeout):
        docker_called.append(True)
        return "passed", "docker output", 0

    async def _fake_host(script, wdir, timeout):
        host_called.append(True)
        return "passed", "host output", 0

    with patch.object(agent_worker, "WORKSPACE", workspace), \
         patch.object(agent_worker, "_HAS_DOCKER", True), \
         patch.object(agent_worker, "_docker_execute", _fake_docker), \
         patch.object(agent_worker, "_host_execute", _fake_host):
        status, output, _ = await agent_worker._safe_execute("echo hi", str(workspace))

    assert docker_called, "_docker_execute was not called despite _HAS_DOCKER=True"
    assert not host_called, "_host_execute must not be called when Docker is available"
    assert status == "passed"


@pytest.mark.asyncio
async def test_safe_execute_falls_back_to_host_without_docker(tmp_path):
    """When _HAS_DOCKER=False, _safe_execute must call _host_execute."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    host_called = []

    async def _fake_host(script, wdir, timeout):
        host_called.append(True)
        return "passed", "host output", 0

    with patch.object(agent_worker, "WORKSPACE", workspace), \
         patch.object(agent_worker, "_HAS_DOCKER", False), \
         patch.object(agent_worker, "_host_execute", _fake_host):
        status, _, _ = await agent_worker._safe_execute("echo hi", str(workspace))

    assert host_called, "_host_execute was not called when Docker unavailable"
    assert status == "passed"


# ---------------------------------------------------------------------------
# R1 — _main_loop stub override eliminated
# ---------------------------------------------------------------------------

def test_main_loop_is_not_stub():
    """
    There must be exactly one _main_loop definition and it must contain the
    real poll logic (while True loop), not a stub placeholder comment.
    """
    import services.agent_worker
    import inspect

    src = inspect.getsource(agent_worker._main_loop)
    assert "while True" in src, (
        "_main_loop does not contain a 'while True' loop — "
        "the stub definition is still overriding the real implementation"
    )
    assert "loop logic from main" not in src, (
        "_main_loop still contains the stub placeholder comment"
    )


def test_only_one_main_loop_definition():
    """agent_worker.py must define _main_loop exactly once."""
    src_path = os.path.join(os.path.dirname(__file__), "..", "..", "agent_worker.py")
    # Try relative path from test file location
    candidate = os.path.normpath(src_path)
    if not os.path.exists(candidate):
        # Fall back to module location
        import services.agent_worker
        import inspect
        candidate = inspect.getfile(agent_worker)

    with open(candidate, encoding="utf-8") as f:
        content = f.read()

    count = content.count("async def _main_loop(")
    assert count == 1, (
        f"_main_loop is defined {count} times in agent_worker.py; "
        "only the stub-overrides-real problem produces count > 1"
    )


# ---------------------------------------------------------------------------
# R2 — _http_complete re-raises on failure
# ---------------------------------------------------------------------------

def test_http_complete_reraises_on_api_failure():
    """_http_complete must propagate the exception instead of swallowing it."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    def _boom(*a, **kw):
        raise RuntimeError("API unreachable")

    with patch.object(agent_worker, "_api", _boom):
        with pytest.raises(RuntimeError, match="API unreachable"):
            agent_worker._http_complete("T1", "done", "ok", [], "anthropic")


def test_update_task_in_db_reraises_on_db_failure():
    """update_task_in_db (file mode) must propagate DB exceptions."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    def _bad_db():
        raise RuntimeError("connection refused")

    # Force file mode (no HTTP)
    with patch.object(agent_worker, "HTTP_MODE", False), \
         patch.object(agent_worker, "_db", _bad_db):
        with pytest.raises(RuntimeError, match="connection refused"):
            agent_worker.update_task_in_db("T1", "failed", "agent-x")


def test_update_task_in_db_http_mode_propagates_complete_failure():
    """In HTTP mode, update_task_in_db delegates to _http_complete which re-raises."""
    import importlib, agent_worker
    importlib.reload(agent_worker)

    def _boom(*a, **kw):
        raise RuntimeError("503 Service Unavailable")

    with patch.object(agent_worker, "HTTP_MODE", True), \
         patch.object(agent_worker, "_http_complete", _boom):
        with pytest.raises(RuntimeError, match="503"):
            agent_worker.update_task_in_db("T1", "done", "agent-x")

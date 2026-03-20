import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest


@pytest.mark.asyncio
async def test_safe_execute_requires_explicit_host_fallback(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    from services import agent_worker

    importlib.reload(agent_worker)
    workspace = Path("g:/Fernando/project0") / f"_tmp_agent_worker_{uuid.uuid4().hex}"
    workspace.mkdir()

    try:
        with patch.object(agent_worker, "WORKSPACE", workspace), patch.object(
            agent_worker, "_HAS_DOCKER", False
        ), patch.object(agent_worker, "_ALLOW_HOST_SANDBOX_FALLBACK", False):
            status, output, rc = await agent_worker._safe_execute("echo hi", str(workspace))

        assert status == "failed"
        assert "host fallback is disabled" in output
        assert rc == -1
    finally:
        workspace.rmdir()


def test_post_task_actions_runs_reflection_and_memory_write(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    from services import agent_worker

    importlib.reload(agent_worker)
    calls = []

    def _fake_execute_tool(name, payload, workspace):
        calls.append((name, payload, workspace))
        if name == "self_reflect":
            return '{"verdict":"good","concerns":[],"next_steps":["persist"],"incident_family":"validation","memory_candidate":{"should_persist":true,"summary":"persist it","tags":["reflection"]}}'
        if name == "memory_write":
            return "OK: stored"
        raise AssertionError(name)

    with patch("services.local_agent_runner._execute_tool", new=_fake_execute_tool), patch(
        "services.local_agent_runner.WORKSPACE", new=Path("g:/Fernando/project0")
    ):
        agent_worker._perform_post_task_actions(
            None,
            {
                "id": "TASK-123",
                "title": "Harden worker",
                "task_type": "fix_bug",
                "assigned_agent": "ai engineer",
            },
            SimpleNamespace(
                status="done",
                summary="Applied fix and validated tests",
                error="",
                backend_used="anthropic",
                files_modified=["services/agent_worker.py"],
            ),
        )

    assert calls[0][0] == "self_reflect"
    memory_calls = [call for call in calls if call[0] == "memory_write"]
    assert len(memory_calls) >= 2
    assert memory_calls[0][1]["key"] == "task-outcome:local:TASK-123"
    assert memory_calls[0][1]["incident_family"] == "validation"
    assert memory_calls[0][1]["metadata"]["task_id"] == "TASK-123"


def test_classify_execution_error_adds_structured_category(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    from services import agent_worker

    importlib.reload(agent_worker)

    failure = agent_worker._classify_execution_error(
        RuntimeError("docker sandbox failed and host fallback is disabled: timeout"),
        step="sandbox",
    )

    assert failure["category"] == "sandbox"
    assert failure["step"] == "sandbox"
    assert failure["retryable"] is True


@pytest.mark.asyncio
async def test_build_preflight_context_includes_active_memory(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    from services import agent_worker

    importlib.reload(agent_worker)

    def _fake_execute_tool(name, payload, workspace):
        if name == "memory_search":
            return '{"results":[{"content":"Previous fix for services/foo.py","metadata":{"files":["services/foo.py"],"task_type":"fix_bug"}}]}'
        raise AssertionError(name)

    with patch.object(
        agent_worker,
        "_fetch_task_context",
        return_value={"enriched_prompt": "cognitive brief", "nodes": [{"name": "services/foo.py"}]},
    ), patch.object(
        agent_worker,
        "_fetch_lessons",
        return_value="LESSONS LEARNED:\n  1. reuse prior fix",
    ), patch.object(
        agent_worker,
        "_fetch_task_debugger",
        return_value={"context": {"files_affected": ["services/foo.py"]}, "reasoning": {"incidents": [{"summary": "validation failure"}]}},
    ), patch(
        "services.local_agent_runner._execute_tool",
        new=_fake_execute_tool,
    ), patch(
        "services.local_agent_runner.WORKSPACE",
        new=Path("g:/Fernando/project0"),
    ), patch.object(
        agent_worker,
        "_api",
        side_effect=lambda method, path, body=None: (
            {
                "items": [
                    {
                        "hint_kind": "file_path",
                        "summary": "Prefer the validated fix for services/foo.py",
                        "file_path": "services/foo.py",
                        "incident_family": "validation",
                        "match_score": 17,
                    }
                ]
            }
            if "/cognitive/memory/reactivation" in path
            else {
                "profile": "guarded",
                "files": [
                    {
                        "file_path": "services/foo.py",
                        "entropy_score": 0.82,
                        "label": "critical",
                        "complexity": 14,
                        "coupling": 9,
                    }
                ],
                "recommendations": ["Prefer minimal diffs."],
            }
        ),
    ):
        merged = await agent_worker._build_preflight_context(
            "TASK-1",
            {"title": "Fix foo", "task_type": "fix_bug", "assigned_agent": "ai engineer"},
        )

    assert "ACTIVE MEMORY:" in merged
    assert "ACTIVE REACTIVATION HINTS:" in merged
    assert "EXECUTION RISK PROFILE: GUARDED" in merged
    assert "Previous fix for services/foo.py" in merged

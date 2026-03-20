import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import local_agent_runner as runner

_FIXTURE_ROOT = Path("ai-orchestrator/services/streaming/tests/fixtures").resolve()


@pytest.fixture(autouse=True)
def _scope_env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "sinc")
    monkeypatch.setenv("TENANT_ID", "tenant-test")


def _tool_names_anthropic():
    return {tool["name"] for tool in runner._build_tools()}


def _tool_names_ollama():
    return {tool["function"]["name"] for tool in runner._build_ollama_tools()}


def test_runner_registers_level2_tools():
    expected = {
        "analyze_code",
        "explain_code",
        "plan_tasks",
        "memory_search",
        "memory_write",
        "self_reflect",
        "spawn_agent",
        "root_cause_analysis",
        "analyze_logs",
        "system_health_check",
    }
    assert expected.issubset(_tool_names_anthropic())
    assert expected.issubset(_tool_names_ollama())


def test_analyze_code_python_ast():
    raw = runner._execute_tool(
        "analyze_code",
        {"path": "runner_sample.py", "mode": "full"},
        _FIXTURE_ROOT,
    )
    data = json.loads(raw)
    assert data["language"] == "python"
    assert data["functions"][0]["name"] == "run"
    assert data["classes"][0]["name"] == "Demo"
    assert any("debug call" in issue for issue in data["possible_bugs"])


def test_explain_code_python_function():
    explanation = runner._execute_tool(
        "explain_code",
        {"path": "runner_explain.py", "function_name": "calculate_total"},
        _FIXTURE_ROOT,
    )
    assert "calculate_total" in explanation
    assert "aggregate total" in explanation


def test_plan_tasks_local_fallback(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    raw = runner._execute_tool(
        "plan_tasks",
        {"goal": "Add audit logging to task completion", "context": "touch agent completion path"},
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["source"] in {"local_fallback", "mcts:mcts"}
    assert len(data["tasks"]) >= 3
    assert data["tasks"][1]["depends_on"]


def test_run_coro_sync_handles_existing_event_loop():
    async def _inner():
        return runner._run_coro_sync(runner.asyncio.sleep(0, result="ok"))

    assert runner.asyncio.run(_inner()) == "ok"


def test_orchestrator_json_request_propagates_context_headers(monkeypatch):
    class _FakeResponse:
        text = "{}"

        def json(self):
            return {"ok": True}

    captured = {}

    def fake_request(url, *, method="GET", headers=None, body=None, timeout=20, service_name=""):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _FakeResponse()

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setattr(runner, "_sync_http_request", fake_request)
    monkeypatch.setattr(
        "services.cognitive_orchestrator.get_context",
        lambda: SimpleNamespace(tenant_id="tenant-ctx", trace_id="trace-ctx", project_id="proj-ctx"),
    )

    result = runner._orchestrator_json_request("/health")

    assert result["ok"] is True
    assert captured["headers"]["X-Tenant-Id"] == "tenant-ctx"
    assert captured["headers"]["X-Trace-Id"] == "trace-ctx"
    assert captured["headers"]["X-Correlation-ID"] == "trace-ctx"
    assert captured["headers"]["X-Project-Id"] == "proj-ctx"


def test_embed_text_uses_cache(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(runner, "_semantic_embed_text", lambda text, model, timeout=30: (calls.__setitem__("count", calls["count"] + 1) or [0.1, 0.2], None))
    runner.EMBEDDING_CACHE.clear()

    first = runner._embed_text("same text")
    second = runner._embed_text("same text")

    assert first == second
    assert calls["count"] == 1


def test_playwright_manager_session_isolates_context(monkeypatch):
    events = []

    class _FakePage:
        def goto(self, url, **_kwargs):
            events.append(("goto", url))

        def close(self):
            events.append(("page_close", None))

    class _FakeContext:
        def __init__(self):
            self.page = _FakePage()

        def new_page(self):
            events.append(("new_page", None))
            return self.page

        def close(self):
            events.append(("context_close", None))

    class _FakeBrowser:
        def new_context(self):
            events.append(("new_context", None))
            return _FakeContext()

    monkeypatch.setattr(runner.browser_manager, "get_browser", lambda: _FakeBrowser())

    with runner.browser_manager.session(url="http://example.test") as page:
        assert isinstance(page, _FakePage)

    assert ("new_context", None) in events
    assert ("goto", "http://example.test") in events
    assert ("context_close", None) in events


def test_memory_write_and_search(monkeypatch):
    upserts = []

    monkeypatch.setattr(runner, "_embed_text", lambda _query: ([0.1, 0.2, 0.3], ""))
    monkeypatch.setattr(
        runner,
        "_search_qdrant",
        lambda _collection, _vector, top_k=5: (
            [
                {
                    "score": 0.92,
                    "payload": {
                        "content": "Use task_success_prediction before switching agents.",
                        "tags": ["reputation", "scheduler"],
                        "source": "agent_memory",
                    },
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_upsert_qdrant",
        lambda collection, vector, payload: upserts.append(
            {"collection": collection, "vector": vector, "payload": payload}
        )
        or None,
    )

    write_result = runner._execute_tool(
        "memory_write",
        {"content": "Reputation data must be tenant-aware.", "tags": ["reputation"]},
        runner.WORKSPACE,
    )
    search_result = runner._execute_tool(
        "memory_search",
        {"query": "tenant aware reputation", "top_k": 3},
        runner.WORKSPACE,
    )

    assert write_result.startswith("OK: memory stored")
    parsed = json.loads(search_result)
    assert parsed["results"][0]["content"].startswith("Use task_success_prediction")
    assert "metadata" in parsed["results"][0]
    assert upserts
    assert upserts[0]["payload"]["content"] == "Reputation data must be tenant-aware."


def test_self_reflect_flags_missing_validation():
    raw = runner._execute_tool(
        "self_reflect",
        {
            "goal": "Add multi-tenant guard",
            "action_taken": "Patched the repository class",
            "result": "Code changed and committed locally",
            "status": "partial",
        },
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["verdict"] == "partial"
    assert any("validation" in concern.lower() for concern in data["concerns"])
    assert data["validation_status"] == "missing_validation"
    assert data["memory_candidate"]["should_persist"] is True


def test_autonomy_brief_builds_structured_dossier(monkeypatch):
    runner_obj = runner.HybridAgentRunner.__new__(runner.HybridAgentRunner)
    runner_obj.available_backends = ["anthropic"]

    def fake_execute(name, payload, workspace):
        if name == "plan_tasks":
            return json.dumps(
                {
                    "tasks": [
                        {"title": "Analyze scope", "depends_on": []},
                        {"title": "Implement fix", "depends_on": ["Analyze scope"]},
                    ]
                }
            )
        if name == "memory_search":
            return json.dumps(
                {
                    "results": [
                        {
                            "content": "Previous validation fix on services/foo.py required rerunning tests.",
                            "metadata": {"incident_family": "validation", "files": ["services/foo.py"]},
                        }
                    ]
                }
            )
        if name == "semantic_search":
            return json.dumps(
                {
                    "results": [
                        {"score": 0.91, "content": "Task completion path writes validation-sensitive state."}
                    ]
                }
            )
        if name == "analyze_code":
            return json.dumps(
                {
                    "functions": [{"name": "complete"}],
                    "complexity": {"max_function_complexity": 12},
                    "possible_bugs": ["validation can be skipped on this path"],
                }
            )
        raise AssertionError(name)

    monkeypatch.setattr(runner, "_execute_tool", fake_execute)
    brief = runner_obj._build_autonomy_brief(
        "TASK-9",
        {
            "description": "Fix completion flow in services/foo.py",
            "task_type": "fix_bug",
            "files_affected": ["services/foo.py"],
            "assigned_agent": "ai engineer",
        },
        "preflight mentions services/foo.py",
    )

    assert "EXECUTION PLAN:" in brief
    assert "RELEVANT MEMORY:" in brief
    assert "SEMANTIC HITS:" in brief
    assert "STRUCTURAL RISKS:" in brief


def test_derive_autonomy_policy_escalates_incident_and_blast_radius():
    policy = runner._derive_autonomy_policy(
        {"task_type": "incident_response", "description": "Recover queue worker timeout"},
        "Execution risk profile: elevated\nblast_radius=5\nsecurity validation required",
        "ollama",
    )
    assert policy["task_family"] == "incident"
    assert policy["incident_family"] == "security"
    assert policy["risk_profile"] == "extreme"
    assert policy["blast_radius"] == 5
    assert policy["requires_parallel_review"] is True


def test_finalize_task_complete_downgrades_done_without_validation():
    result = runner._finalize_task_complete(
        {"task_type": "fix_bug"},
        {"status": "done", "summary": "Patched the flow", "files_modified": ["services/foo.py"]},
        ["services/foo.py"],
        ["plan_tasks", "patch_file"],
    )

    assert result.status == "partial"
    assert "downgraded_to_partial:no_validation_evidence" in result.summary


def test_finalize_task_complete_applies_risk_based_gate():
    dispatch = {
        "task_type": "fix_bug",
        "_autonomy_policy": {
            "requires_validation": True,
            "requires_diff": True,
            "requires_parallel_review": True,
            "risk_profile": "guarded",
            "incident_family": "security",
            "backend": "ollama",
            "blast_radius": 4,
        },
    }
    result = runner._finalize_task_complete(
        dispatch,
        {"status": "done", "summary": "Patched critical auth flow", "files_modified": ["services/auth.py"]},
        ["services/auth.py"],
        ["plan_tasks", "patch_file"],
    )

    assert result.status == "partial"
    assert "missing_diff_review" in result.summary
    assert "missing_parallel_review" in result.summary
    assert "missing_structured_reflection" in result.summary
    assert "downgraded_to_partial:critical_incident_requires_review" in result.summary
    assert "downgraded_to_partial:missing_diff_for_blast_radius" in result.summary


def test_spawn_agent_waits_for_terminal_state(monkeypatch):
    class _FakeRedis:
        def __init__(self):
            self._reads = 0

        def xrevrange(self, *_args, **_kwargs):
            return []

        def xread(self, *_args, **_kwargs):
            self._reads += 1
            if self._reads == 1:
                return [
                    (
                        "sinc:stream:task_lifecycle:tenant-test",
                        [("1-0", {"data": json.dumps({"task_id": "SUB-1", "status": "done", "agent_name": "qa agent"})})],
                    )
                ]
            return []

    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path == "/tasks" and method == "POST":
            return {"task_id": "SUB-1", "status": "pending"}
        if path == "/tasks/SUB-1":
            return {
                "id": "SUB-1",
                "status": "done",
                "assigned_agent": "qa agent",
                "summary": "subtask complete",
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)
    monkeypatch.setattr("services.streaming.core.redis_.get_redis", lambda: _FakeRedis())

    raw = runner._execute_tool(
        "spawn_agent",
        {"title": "Review patch", "goal": "Run QA review", "agent_type": "qa agent", "wait": True, "poll_interval_s": 0},
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["task_id"] == "SUB-1"
    assert data["status"] == "done"
    assert data["assigned_agent"] == "qa agent"
    assert data["lifecycle"]["mode"] == "redis-stream"


def test_spawn_agent_supports_parallel_review_consensus(monkeypatch):
    created = []

    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path == "/tasks" and method == "POST":
            reviewer = body.get("agent")
            created.append(reviewer)
            task_id = "REV-1" if reviewer == "code review agent" else "REV-2"
            return {"task_id": task_id, "status": "pending"}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)
    monkeypatch.setattr(
        runner,
        "_wait_for_tasks_via_stream",
        lambda **_kwargs: (
            {
                "REV-1": {
                    "task_id": "REV-1",
                    "status": "done",
                    "summary": "approved after diff review",
                    "assigned_agent": "code review agent",
                    "result": {"id": "REV-1", "status": "done"},
                },
                "REV-2": {
                    "task_id": "REV-2",
                    "status": "done",
                    "summary": "approved after validation",
                    "assigned_agent": "qa agent",
                    "result": {"id": "REV-2", "status": "done"},
                },
            },
            {"available": True, "stream_name": "sinc:stream:task_lifecycle:tenant-test", "snapshots": {}},
        ),
    )

    raw = runner._execute_tool(
        "spawn_agent",
        {
            "title": "Critical review",
            "goal": "Review the auth patch before completion",
            "mode": "review_parallel",
            "reviewers": ["code review agent", "qa agent"],
            "consensus": True,
            "wait": True,
            "poll_interval_s": 0,
        },
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["fan_out"] == 2
    assert data["mode"] == "review_parallel"
    assert data["consensus"]["approved"] is True
    assert created == ["code review agent", "qa agent"]
    assert data["lifecycle"]["mode"] == "redis-stream"


def test_spawn_agent_supports_fan_out_and_timeout_cancellation(monkeypatch):
    create_counter = {"count": 0}
    cancelled = []

    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path == "/tasks" and method == "POST":
            create_counter["count"] += 1
            if create_counter["count"] == 1:
                return {"task_id": "SUB-1", "status": "pending"}
            return {"task_id": "SUB-2", "status": "pending"}
        if path == "/tasks/SUB-2":
            return {"id": "SUB-2", "status": "in-progress", "assigned_agent": "backend agent", "summary": ""}
        if path == "/tasks/SUB-2/status" and method == "PATCH":
            cancelled.append("SUB-2")
            return {"ok": True, "task_id": "SUB-2", "status": "cancelled"}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)
    monkeypatch.setattr(
        runner,
        "_wait_for_tasks_via_stream",
        lambda **_kwargs: (
            {
                "SUB-1": {
                    "task_id": "SUB-1",
                    "status": "done",
                    "summary": "first complete",
                    "assigned_agent": "qa agent",
                    "result": {"id": "SUB-1", "status": "done"},
                }
            },
            {
                "available": True,
                "stream_name": "sinc:stream:task_lifecycle:tenant-test",
                "snapshots": {"SUB-2": {"status": "in-progress", "assigned_agent": "backend agent"}},
            },
        ),
    )

    raw = runner._execute_tool(
        "spawn_agent",
        {
            "subtasks": [
                {"title": "QA review", "goal": "Review change", "agent_type": "qa agent"},
                {"title": "Backend follow-up", "goal": "Apply backend fix", "agent_type": "backend agent"},
            ],
            "wait": True,
            "poll_interval_s": 0,
            "timeout_s": 0.01,
            "cancel_on_timeout": True,
        },
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["fan_out"] == 2
    assert data["fan_in"]["timed_out"] == 1
    assert "SUB-2" in data["timed_out"]
    assert cancelled == ["SUB-2"]
    assert data["lifecycle"]["mode"] == "redis-stream"


def test_analyze_logs_aggregates_component_patterns(monkeypatch):
    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=worker"):
            return {"lines": [
                "[2026-03-19T10:00:00Z] ERROR queue stalled TASK-9",
                "[2026-03-19T10:01:00Z] [WARN] retry TASK-9",
            ]}
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=orch"):
            return {"lines": [
                "[2026-03-19T10:02:00Z] ERROR queue stalled TASK-9",
                "[2026-03-19T10:03:00Z] ERROR queue stalled TASK-9",
            ]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)

    raw = runner._execute_tool(
        "analyze_logs",
        {"components": ["worker", "orch"], "pattern": "TASK-9"},
        runner.WORKSPACE,
    )
    data = json.loads(raw)
    assert data["totals"]["ERROR"] == 3
    assert data["totals"]["WARN"] == 1
    assert len(data["components"]) == 2
    assert data["patterns"]
    assert data["anomalies"]


def test_root_cause_analysis_correlates_runtime_and_logs(monkeypatch):
    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path == "/tasks/TASK-77":
            return {"id": "TASK-77", "status": "failed", "summary": "Tests failed on completion"}
        if path == "/api/v5/dashboard/task-debugger/TASK-77":
            return {
                "metadata": {"status": "failed", "assigned_agent": "ai engineer"},
                "context": {"files_affected": ["services/foo.py"]},
                "reasoning": {"completion_summary": "test failure in validation", "incidents": [{"summary": "validation loop", "severity": "warning"}]},
                "timeline": [{"event": "task.completed", "detail": "tests failed"}],
            }
        if path == "/tasks/TASK-77/events":
            return {"events": [{"event_type": "task.started"}, {"event_type": "task.failed"}]}
        if path == "/tasks/TASK-77/context":
            return {"nodes": [{"name": "services/foo.py"}]}
        if path == "/tasks/TASK-77/impact":
            return {"risk_level": "high", "blast_radius": 3}
        if path == "/readiness/live":
            return {"health": "degraded", "open_incidents": 2, "cognitive_status": "degraded"}
        if path.startswith("/incidents?"):
            return {"incidents": [{"summary": "global validation incident"}]}
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=worker"):
            return {"lines": ["[2026-03-19T10:00:00Z] ERROR TASK-77 tests failed", "[2026-03-19T10:01:00Z] [WARN] TASK-77 retry"]}
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=orch"):
            return {"lines": ["[2026-03-19T10:02:00Z] ERROR validator failed TASK-77"]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)

    raw = runner._execute_tool("root_cause_analysis", {"task_id": "TASK-77"}, runner.WORKSPACE)
    data = json.loads(raw)
    assert data["primary_cause"] == "validation_failure"
    assert data["evidence"]["open_incidents"] == 1
    assert data["evidence"]["context_nodes"] == 1
    assert data["recommendations"]


def test_system_health_check_reports_component_issues(monkeypatch):
    def fake_orchestrator(path, *, method="GET", body=None, timeout=20):
        if path == "/readiness/live":
            return {"health": "degraded", "open_incidents": 3}
        if path == "/api/v5/dashboard/summary":
            return {
                "metrics": {
                    "success_rate": 0.9,
                    "autonomy_score": 0.8,
                    "active_agents": 4,
                    "latency_p95": "120ms",
                    "tps": 0.4,
                },
                "routing": {"fast": 3},
            }
        if path == "/api/v5/dashboard/diagnostics/health":
            return {"components": {"redis": {"status": "up"}, "qdrant": {"status": "down"}}}
        if path == "/health/deep":
            return {"status": "degraded"}
        if path.startswith("/incidents?"):
            return {"incidents": [{"summary": "Qdrant unavailable"}]}
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=worker"):
            return {"lines": ["[2026-03-19T10:00:00Z] ERROR qdrant timeout"]}
        if path.startswith("/api/v5/dashboard/diagnostics/logs?component=orch"):
            return {"lines": ["[2026-03-19T10:01:00Z] ERROR qdrant timeout"]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orch.test")
    monkeypatch.setattr(runner, "_orchestrator_json_request", fake_orchestrator)

    raw = runner._execute_tool("system_health_check", {}, runner.WORKSPACE)
    data = json.loads(raw)
    assert data["status"] == "degraded"
    assert any("qdrant=down" in issue for issue in data["issues"])
    assert data["open_incidents"]

from __future__ import annotations

import inspect
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def test_cognitive_orchestrator_has_single_ensure_init():
    from services import cognitive_orchestrator

    source = inspect.getsource(cognitive_orchestrator)
    assert source.count("def _ensure_init(") == 1


def test_cognitive_graph_has_single_factory():
    from pathlib import Path

    source = Path("ai-orchestrator/services/cognitive_graph.py").read_text(encoding="utf-8")
    assert source.count("def get_cognitive_graph(") == 1
    assert source.count("def build_cognitive_graph(") == 1


def test_dynamic_reward_scorer_uses_agent_level_fallback():
    from services.mcts_planner import DynamicRewardScorer

    scorer = DynamicRewardScorer("local", {"agent-x:all": 0.91})
    assert scorer.get_reward("apply_fix", "agent-x") == 0.91


@pytest.mark.asyncio
async def test_build_preflight_context_includes_cognitive_brief():
    with patch.dict("os.environ", {"ORCHESTRATOR_URL": "http://orch.test"}):
        from services import agent_worker

        importlib.reload(agent_worker)

        with patch.object(agent_worker, "_fetch_task_context", return_value="graph ctx"), patch.object(
            agent_worker, "_fetch_lessons", return_value="lesson ctx"
        ), patch(
            "services.cognitive_orchestrator.prepare_execution_context",
            AsyncMock(return_value={"enriched_system_prompt": "cognitive brief"}),
        ):
            merged = await agent_worker._build_preflight_context(
                "TASK-1",
                {"assigned_agent": "ai engineer", "tenant_id": "local"},
                context_limit=12,
            )

    assert "cognitive brief" in merged
    assert "lesson ctx" in merged
    assert "graph ctx" in merged


@pytest.mark.asyncio
async def test_reputation_engine_processes_completion_event():
    from services.reputation_engine import ReputationEngine

    engine = ReputationEngine("local")
    with patch.object(engine, "_update_redis", AsyncMock()) as update_redis, patch.object(
        engine, "_update_postgres", AsyncMock()
    ) as update_postgres:
            await engine._process_audit_event(
                {
                    "task_type": "backend",
                    "agent_name": "ai engineer",
                    "status": "done",
                    "duration_ms": 1200,
                    "tenant_id": "local",
                    "task_title": "Fix auth flow",
                "summary": "ship the backend patch",
            }
        )

    update_redis.assert_awaited_once_with("local", "backend", "ai engineer", True, 1200)
    update_postgres.assert_awaited_once()
    assert update_postgres.await_args.args[:5] == ("local", "backend", "ai engineer", True, 1200)


@pytest.mark.asyncio
async def test_graph_reasoning_adapter_prefers_got_solution():
    from services.graph_reasoning_adapter import resolve_graph_reasoning

    fake_orch = SimpleNamespace(
        _got=SimpleNamespace(
            find_or_create_reasoning=lambda description, task_type, embedder: SimpleNamespace(
                solution="reuse this fix",
                steps=["inspect", "patch"],
                source="neo4j_existing",
                confidence=0.93,
            )
        ),
        _memory=SimpleNamespace(l2=SimpleNamespace(embedder_func=lambda text: [0.1, 0.2])),
    )

    with patch(
        "services.context_retriever.graph_aware_retrieve",
        AsyncMock(return_value={"context": "structural ctx", "sources": ["a.py"]}),
    ):
        result = await resolve_graph_reasoning(
            "fix login",
            "fix_bug",
            "sinc",
            "local",
            orch=fake_orch,
        )

    assert result.solution == "reuse this fix"
    assert result.cache_level == "neo4j_existing"
    assert result.llm_needed is False
    assert result.structural_context == "structural ctx"


@pytest.mark.asyncio
async def test_prepare_execution_context_includes_graph_reasoning_summary():
    from services import cognitive_orchestrator

    with patch(
        "services.context_retriever.assess_change_impact",
        AsyncMock(return_value={"risk_level": "low"}),
    ), patch(
        "services.context_retriever.find_similar_past_solutions",
        AsyncMock(return_value=[]),
    ), patch(
        "services.context_retriever.build_proactive_context",
        AsyncMock(return_value={"relevant_code": [], "watch_out_for": []}),
    ), patch(
        "services.context_retriever.cluster_recent_failures",
        AsyncMock(return_value=[]),
    ), patch(
        "services.graph_reasoning_adapter.resolve_graph_reasoning",
        AsyncMock(
            return_value=SimpleNamespace(
                structural_context="graph context here",
                solution="known graph-backed fix",
                cache_level="neo4j_existing",
                confidence=0.91,
                graph_result={"sources": ["x.py"]},
            )
        ),
    ):
        context = await cognitive_orchestrator.prepare_execution_context(
            {"description": "fix login", "project_id": "sinc", "task_type": "fix_bug"},
            "ai engineer",
            "local",
        )

    prompt = context["enriched_system_prompt"]
    assert "[GRAPH] graph context here" in prompt
    assert "[REUSED REASONING]" in prompt
    assert context["intelligence"]["graph_reasoning"]["has_solution"] is True


def test_learn_and_store_persists_got_reasoning_on_success():
    from pathlib import Path

    source = Path("ai-orchestrator/services/cognitive_graph.py").read_text(encoding="utf-8")
    assert "got.persist_reasoning" in source
    assert "DEFAULT_MAX_STEPS" in source


@pytest.mark.asyncio
async def test_learn_and_store_does_not_auto_verify_from_syntax_heuristic():
    import importlib
    from services import cognitive_graph

    importlib.reload(cognitive_graph)
    captured = {}

    async def fake_generate_and_store_lesson(state, solution, succeeded, error=None, verified=False):
        captured["verified"] = verified
        return "lesson"

    with patch("services.memory_evolution.generate_and_store_lesson", fake_generate_and_store_lesson), patch(
        "services.cognitive_orchestrator.get_orchestrator",
        return_value=SimpleNamespace(_got=None, _memory=None),
    ):
        await cognitive_graph.learn_and_store_node(
            {
                "task": {"id": "TASK-1"},
                "solution": "```python\nprint('ok')\n```",
                "error": None,
                "description": "emit code",
                "task_type": "backend",
                "project_id": "proj",
                "tenant_id": "tenant-a",
                "validation_passed": True,
                "verified_by_vnode": True,
                "verification_source": "syntax_check",
            }
        )

    assert captured["verified"] is False


@pytest.mark.asyncio
async def test_learn_and_store_requires_explicit_validator_for_verified_memory():
    import importlib
    from services import cognitive_graph

    importlib.reload(cognitive_graph)
    captured = {}

    async def fake_generate_and_store_lesson(state, solution, succeeded, error=None, verified=False):
        captured["verified"] = verified
        return "lesson"

    with patch("services.memory_evolution.generate_and_store_lesson", fake_generate_and_store_lesson), patch(
        "services.cognitive_orchestrator.get_orchestrator",
        return_value=SimpleNamespace(_got=None, _memory=None),
    ):
        await cognitive_graph.learn_and_store_node(
            {
                "task": {"id": "TASK-2"},
                "solution": "validated fix",
                "error": None,
                "description": "emit code",
                "task_type": "backend",
                "project_id": "proj",
                "tenant_id": "tenant-a",
                "validation_passed": True,
                "verified_by_agent": True,
                "verification_source": "code_validator_agent",
                "validator_report": {
                    "passed": True,
                    "checks": [{"name": "pytest", "passed": True}],
                },
            }
        )

    assert captured["verified"] is True

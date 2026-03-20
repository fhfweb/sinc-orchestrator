import pytest

from services.goals_orchestrator import _fallback_goal_plan, _normalize_goal_plan
from services.streaming.routes.intelligence import GoalMissionRequest, goals_orchestration


def test_fallback_goal_plan_preserves_parallel_branch():
    plan = _fallback_goal_plan("Ship goal runtime", ["scheduler unlocks parallel slices"])

    assert len(plan.subtasks) >= 4
    assert plan.subtasks[0].depends_on == []
    assert plan.subtasks[1].depends_on == ["Analyze goal scope and affected components"]
    assert plan.subtasks[2].depends_on == ["Analyze goal scope and affected components"]
    assert plan.subtasks[1].title != plan.subtasks[2].title


def test_normalize_goal_plan_keeps_minimal_dependencies():
    plan = _normalize_goal_plan(
        {
            "plan_summary": "Parallel goal execution",
            "estimated_total_minutes": 60,
            "subtasks": [
                {
                    "title": "Slice A",
                    "description": "Implement the backend slice",
                    "category": "backend",
                    "depends_on": [],
                    "priority": 1,
                },
                {
                    "title": "Slice B",
                    "description": "Implement the frontend slice",
                    "category": "frontend",
                    "depends_on": [],
                    "priority": 1,
                },
                {
                    "title": "Validate",
                    "description": "Run integrated validation",
                    "category": "testing",
                    "depends_on": ["Slice A", "Slice B"],
                    "priority": 1,
                },
            ],
            "critical_path": ["Slice A", "Validate"],
        },
        "Ship goal runtime",
        ["parallel tasks dispatch immediately"],
    )

    assert [task.title for task in plan.subtasks] == ["Slice A", "Slice B", "Validate"]
    assert plan.subtasks[0].depends_on == []
    assert plan.subtasks[1].depends_on == []
    assert plan.subtasks[2].depends_on == ["Slice A", "Slice B"]
    assert plan.critical_path == ["Slice A", "Validate"]


@pytest.mark.asyncio
async def test_goals_route_uses_canonical_goal_contract(monkeypatch):
    captured = {}

    async def fake_plan_and_execute_goal(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "goal_id": "goal-123",
            "plan_id": "plan-123",
            "task_count": 3,
            "ready_parallel": 2,
            "blocked": 1,
        }

    monkeypatch.setattr(
        "services.goals_orchestrator.plan_and_execute_goal",
        fake_plan_and_execute_goal,
    )

    req = GoalMissionRequest(
        goal="Ship real parallel goal execution",
        project_id="orchestrator",
        context="Preserve Python-only runtime",
        acceptance_criteria=["independent slices dispatch in parallel"],
        constraints=["do not use agent_swarm as fake executor"],
    )

    result = await goals_orchestration(req, tenant_id="local")

    assert result["ok"] is True
    assert captured == {
        "description": "Ship real parallel goal execution",
        "tenant_id": "local",
        "project_id": "orchestrator",
        "acceptance_criteria": ["independent slices dispatch in parallel"],
        "constraints": ["do not use agent_swarm as fake executor"],
        "context": "Preserve Python-only runtime",
    }

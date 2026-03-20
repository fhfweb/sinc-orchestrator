from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from services.streaming.core.db import async_db
from services.streaming.core.runtime_plane import scheduler_tick_once
from services.streaming.core.schema_compat import (
    get_dependency_ref_column,
    get_table_columns_cached,
    get_task_pk_column,
    insert_agent_event,
)
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator.goals")


@dataclass
class GoalTaskSpec:
    title: str
    description: str
    category: str = "generic"
    suggested_agent: str = ""
    estimated_minutes: int = 15
    depends_on: list[str] = field(default_factory=list)
    files_likely_touched: list[str] = field(default_factory=list)
    done_criteria: str = ""
    priority: int = 2


@dataclass
class GoalPlan:
    plan_summary: str
    estimated_total_minutes: int
    subtasks: list[GoalTaskSpec]
    critical_path: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    source: str = "llm"


GOAL_DECOMPOSITION_PROMPT = """
You are the mission planner of an autonomous AI engineering system.
Return ONLY valid JSON.

Decompose the goal into atomic engineering subtasks with minimal dependencies.

Rules:
1. Prefer parallelizable subtasks whenever possible.
2. Add dependencies only when a task truly blocks another.
3. Each subtask must be executable by one specialist agent.
4. Each subtask must have a verifiable done criteria.
5. Keep the plan concise and practical.

JSON schema:
{
  "plan_summary": "one sentence",
  "estimated_total_minutes": 120,
  "subtasks": [
    {
      "title": "clear action title",
      "description": "what to do and how to verify",
      "category": "backend|frontend|testing|docs|refactor|security|devops|generic",
      "suggested_agent": "best-fit agent name",
      "estimated_minutes": 20,
      "depends_on": ["other title if needed"],
      "files_likely_touched": ["path/one", "path/two"],
      "done_criteria": "observable completion criteria",
      "priority": 1
    }
  ],
  "critical_path": ["title a", "title b"],
  "risks": ["risk 1", "risk 2"]
}
"""

_CATEGORY_TO_AGENT = {
    "backend": "ai engineer",
    "frontend": "ai engineer frontend",
    "testing": "qa agent",
    "docs": "documentation agent",
    "refactor": "code review agent",
    "security": "ai security engineer",
    "devops": "ai devops engineer",
    "database": "database agent",
}


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result
    text = str(value).strip()
    return [text] if text else []


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("goal planner returned non-JSON payload")

    parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        raise ValueError("goal planner returned non-object JSON")
    return parsed


def _dedupe_titles(raw_subtasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []
    for item in raw_subtasks:
        title = str(item.get("title") or item.get("description") or "Untitled task").strip()
        count = seen.get(title, 0)
        seen[title] = count + 1
        if count > 0:
            title = f"{title} ({count + 1})"
        normalized.append({**item, "title": title})
    return normalized


def _normalize_priority(value: Any) -> int:
    try:
        numeric = int(value)
    except Exception:
        numeric = 2
    return max(1, min(3, numeric))


def _fallback_goal_plan(goal: str, acceptance_criteria: list[str]) -> GoalPlan:
    criteria_text = "; ".join(acceptance_criteria) if acceptance_criteria else "core acceptance criteria satisfied"
    subtasks = [
        GoalTaskSpec(
            title="Analyze goal scope and affected components",
            description=f"Break down the goal '{goal}' into implementation areas and identify impacted modules.",
            category="backend",
            suggested_agent="architect",
            estimated_minutes=20,
            depends_on=[],
            done_criteria="Impacted modules and execution slices are identified.",
            priority=1,
        ),
        GoalTaskSpec(
            title="Implement the primary product changes",
            description=f"Implement the core changes required for '{goal}' in the main execution path.",
            category="backend",
            suggested_agent="ai engineer",
            estimated_minutes=35,
            depends_on=["Analyze goal scope and affected components"],
            done_criteria="Primary feature path works in code and compiles/runs.",
            priority=1,
        ),
        GoalTaskSpec(
            title="Prepare validation and coverage for the goal",
            description=f"Add or update tests, checks, or verification assets for '{goal}' and ensure {criteria_text}.",
            category="testing",
            suggested_agent="qa agent",
            estimated_minutes=25,
            depends_on=["Analyze goal scope and affected components"],
            done_criteria="Verification assets are ready and cover the goal behavior.",
            priority=1,
        ),
        GoalTaskSpec(
            title="Validate integrated outcome and close the goal",
            description=f"Run the integrated validation for '{goal}', check acceptance criteria, and capture residual risks.",
            category="testing",
            suggested_agent="code review agent",
            estimated_minutes=20,
            depends_on=[
                "Implement the primary product changes",
                "Prepare validation and coverage for the goal",
            ],
            done_criteria="Integrated outcome validated and residual risks documented.",
            priority=1,
        ),
    ]
    return GoalPlan(
        plan_summary=f"Fallback parallel plan for: {goal}",
        estimated_total_minutes=sum(task.estimated_minutes for task in subtasks),
        subtasks=subtasks,
        critical_path=[
            "Analyze goal scope and affected components",
            "Implement the primary product changes",
            "Validate integrated outcome and close the goal",
        ],
        risks=["Fallback decomposition used; refine manually if the slices are too coarse."],
        source="fallback",
    )


def _normalize_goal_plan(raw_plan: dict[str, Any], goal: str, acceptance_criteria: list[str]) -> GoalPlan:
    raw_subtasks = raw_plan.get("subtasks")
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        return _fallback_goal_plan(goal, acceptance_criteria)

    deduped = _dedupe_titles([item for item in raw_subtasks if isinstance(item, dict)])
    valid_titles = {str(item["title"]).strip() for item in deduped if str(item.get("title") or "").strip()}
    subtasks: list[GoalTaskSpec] = []

    for item in deduped[:12]:
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or title).strip()
        if not title or not description:
            continue
        category = str(item.get("category") or "generic").strip().lower() or "generic"
        suggested_agent = str(item.get("suggested_agent") or _CATEGORY_TO_AGENT.get(category, "")).strip()
        depends_on = [dep for dep in _normalize_string_list(item.get("depends_on")) if dep in valid_titles and dep != title]
        subtasks.append(
            GoalTaskSpec(
                title=title,
                description=description,
                category=category,
                suggested_agent=suggested_agent,
                estimated_minutes=max(5, int(item.get("estimated_minutes") or 15)),
                depends_on=depends_on,
                files_likely_touched=_normalize_string_list(item.get("files_likely_touched")),
                done_criteria=str(item.get("done_criteria") or "Task output is verifiable and integrated.").strip(),
                priority=_normalize_priority(item.get("priority")),
            )
        )

    if not subtasks:
        return _fallback_goal_plan(goal, acceptance_criteria)

    return GoalPlan(
        plan_summary=str(raw_plan.get("plan_summary") or f"Execution plan for: {goal}").strip(),
        estimated_total_minutes=max(
            int(raw_plan.get("estimated_total_minutes") or 0),
            sum(task.estimated_minutes for task in subtasks),
        ),
        subtasks=subtasks,
        critical_path=[title for title in _normalize_string_list(raw_plan.get("critical_path")) if title in {task.title for task in subtasks}],
        risks=_normalize_string_list(raw_plan.get("risks")),
        source="llm",
    )


async def _decompose_goal(
    *,
    goal: str,
    project_id: str,
    tenant_id: str,
    acceptance_criteria: list[str],
    constraints: list[str],
    context: str,
) -> GoalPlan:
    from services.cognitive_orchestrator import _llm_solve
    from services.context_retriever import find_similar_past_solutions

    historical = await find_similar_past_solutions(goal, project_id, tenant_id)
    few_shot = ""
    if historical:
        parts = []
        for sol in historical[:2]:
            parts.append(
                f"Past goal: {sol.get('description', '')}\n"
                f"Past solution outline: {str(sol.get('solution', ''))[:500]}"
            )
        few_shot = "\n\n=== HISTORICAL PATTERNS ===\n" + "\n---\n".join(parts)

    prompt_parts = [
        f"Goal: {goal}",
        f"Project: {project_id}",
    ]
    if acceptance_criteria:
        prompt_parts.append("Acceptance criteria:\n- " + "\n- ".join(acceptance_criteria))
    if constraints:
        prompt_parts.append("Constraints:\n- " + "\n- ".join(constraints))
    if context:
        prompt_parts.append(f"Context:\n{context}")
    if few_shot:
        prompt_parts.append(few_shot)

    result_text, _ = await _llm_solve(
        description="\n\n".join(prompt_parts),
        task_type="goal_planning",
        steps=[],
        hint=GOAL_DECOMPOSITION_PROMPT,
        tenant_id=tenant_id,
    )

    try:
        raw_plan = _extract_json_object(result_text)
        return _normalize_goal_plan(raw_plan, goal, acceptance_criteria)
    except Exception as exc:
        log.warning("goal_decomposition_parse_failed error=%s", exc)
        return _fallback_goal_plan(goal, acceptance_criteria)


async def plan_and_execute_goal(
    *,
    description: str,
    tenant_id: str,
    project_id: str = "",
    acceptance_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    context: str = "",
) -> dict[str, Any]:
    """
    Canonical goal execution entrypoint.

    Creates a goal, decomposes it into minimally dependent tasks, persists
    the plan in Postgres, and triggers the scheduler immediately so independent
    subtasks can run in parallel through the normal execution plane.
    """
    acceptance_criteria = acceptance_criteria or []
    constraints = constraints or []

    goal_id = str(uuid4())
    plan_id = f"PLAN-GOAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"

    plan = await _decompose_goal(
        goal=description,
        project_id=project_id,
        tenant_id=tenant_id,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        context=context,
    )

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            goal_cols = await get_table_columns_cached(cur, "goals")
            task_pk = await get_task_pk_column(cur)
            dep_col = await get_dependency_ref_column(cur)

            goal_insert_cols = ["id", "tenant_id", "project_id", "description", "status"]
            goal_insert_vals: list[Any] = [goal_id, tenant_id, project_id, description, "executing"]
            goal_placeholders = ["%s"] * len(goal_insert_cols)

            goal_metadata = {
                "plan_id": plan_id,
                "plan_summary": plan.plan_summary,
                "critical_path": plan.critical_path,
                "risks": plan.risks,
                "execution_mode": "goal-parallel",
                "plan_source": plan.source,
            }
            if "acceptance_criteria" in goal_cols:
                goal_insert_cols.append("acceptance_criteria")
                goal_insert_vals.append(json.dumps(acceptance_criteria))
                goal_placeholders.append("%s::jsonb")
            if "constraints" in goal_cols:
                goal_insert_cols.append("constraints")
                goal_insert_vals.append(json.dumps(constraints))
                goal_placeholders.append("%s::jsonb")
            if "metadata" in goal_cols:
                goal_insert_cols.append("metadata")
                goal_insert_vals.append(json.dumps(goal_metadata))
                goal_placeholders.append("%s::jsonb")

            await cur.execute(
                f"""
                INSERT INTO goals ({', '.join(goal_insert_cols)}, created_at, updated_at)
                VALUES ({', '.join(goal_placeholders)}, NOW(), NOW())
                """,
                tuple(goal_insert_vals),
            )

            await cur.execute(
                """
                INSERT INTO plans (id, tenant_id, project_id, goal, status, task_count, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'active', %s, NOW(), NOW())
                """,
                (plan_id, tenant_id, project_id, description, len(plan.subtasks)),
            )

            title_to_task_id: dict[str, str] = {}
            ready_parallel = 0
            blocked = 0

            for index, spec in enumerate(plan.subtasks, start=1):
                task_id = f"TASK-{goal_id[:8]}-{index:02d}-{os.urandom(2).hex()}"
                title_to_task_id[spec.title] = task_id

            for index, spec in enumerate(plan.subtasks, start=1):
                task_id = title_to_task_id[spec.title]
                has_dependencies = bool(spec.depends_on)
                initial_status = "blocked-deps" if has_dependencies else "pending"
                preferred_agent = spec.suggested_agent or _CATEGORY_TO_AGENT.get(spec.category, "")
                metadata = {
                    "goal_id": goal_id,
                    "goal_parallel": not has_dependencies,
                    "preferred_agent": preferred_agent,
                    "category": spec.category,
                    "files_affected": spec.files_likely_touched,
                    "done_criteria": spec.done_criteria,
                    "estimated_minutes": spec.estimated_minutes,
                    "goal_execution_mode": "parallel-by-dependencies",
                }

                insert_cols = [
                    task_pk,
                    "title",
                    "description",
                    "status",
                    "priority",
                    "project_id",
                    "tenant_id",
                    "plan_id",
                ]
                insert_vals: list[Any] = [
                    task_id,
                    spec.title,
                    spec.description,
                    initial_status,
                    spec.priority,
                    project_id,
                    tenant_id,
                    plan_id,
                ]
                placeholders = ["%s"] * len(insert_cols)

                if "goal_id" in task_cols:
                    insert_cols.append("goal_id")
                    insert_vals.append(goal_id)
                    placeholders.append("%s")
                if "metadata" in task_cols:
                    insert_cols.append("metadata")
                    insert_vals.append(json.dumps(metadata))
                    placeholders.append("%s::jsonb")
                if "assigned_agent" in task_cols and not ("metadata" in task_cols):
                    insert_cols.append("assigned_agent")
                    insert_vals.append(preferred_agent or None)
                    placeholders.append("%s")

                await cur.execute(
                    f"""
                    INSERT INTO tasks ({', '.join(insert_cols)}, created_at, updated_at)
                    VALUES ({', '.join(placeholders)}, NOW(), NOW())
                    """,
                    tuple(insert_vals),
                )

                for dep_title in spec.depends_on:
                    dep_task_id = title_to_task_id.get(dep_title)
                    if dep_task_id:
                        await cur.execute(
                            f"INSERT INTO dependencies (task_id, {dep_col}) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (task_id, dep_task_id),
                        )

                await insert_agent_event(
                    cur,
                    task_id=task_id,
                    event_type="goal_task_seeded",
                    payload={
                        "goal_id": goal_id,
                        "plan_id": plan_id,
                        "parallel_ready": not has_dependencies,
                        "preferred_agent": preferred_agent,
                    },
                    agent_name="goal-orchestrator",
                    tenant_id=tenant_id,
                )

                if has_dependencies:
                    blocked += 1
                else:
                    ready_parallel += 1

        await conn.commit()

    # ── Graph Intelligence Sync (Neo4j) ──────────────────────────────────────
    try:
        from services.graph_intelligence import get_graph_intelligence
        gi = get_graph_intelligence()
        # 1. Sync Goal-Task (Parent-Child) and Task-Task (Depends On)
        for spec in plan.subtasks:
            task_id = title_to_task_id[spec.title]
            
            # Sync Goal link
            gi.sync_task_dependency(task_id, goal_id)
            
            # Sync Task-Task dependencies
            for dep_title in spec.depends_on:
                dep_task_id = title_to_task_id.get(dep_title)
                if dep_task_id:
                    gi.sync_task_dependency(task_id, dep_task_id)
        log.info("synced_goal_plan_to_graph_intelligence goal_id=%s", goal_id)
    except Exception as e:
        log.warning("graph_intelligence_sync_failed goal_id=%s error=%s", goal_id, e)

    await broadcast(
        "goal_started",
        {
            "goal_id": goal_id,
            "plan_id": plan_id,
            "goal": description,
            "task_count": len(plan.subtasks),
            "ready_parallel": ready_parallel,
            "blocked": blocked,
            "plan_source": plan.source,
        },
        tenant_id=tenant_id,
    )

    try:
        from services.streaming.core.goal_monitor import monitor_and_adapt_goal

        asyncio.create_task(monitor_and_adapt_goal(goal_id, tenant_id))
    except Exception as exc:
        log.warning("goal_monitor_start_failed goal_id=%s error=%s", goal_id, exc)

    scheduler_result = await scheduler_tick_once(tenant_id=tenant_id, project_id=project_id)

    return {
        "ok": True,
        "goal_id": goal_id,
        "plan_id": plan_id,
        "status": "executing",
        "task_count": len(plan.subtasks),
        "ready_parallel": ready_parallel,
        "blocked": blocked,
        "plan_summary": plan.plan_summary,
        "critical_path": plan.critical_path,
        "risks": plan.risks,
        "plan_source": plan.source,
        "scheduler": scheduler_result,
    }

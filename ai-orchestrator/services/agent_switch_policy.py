from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orchestrator.agent_switch")


def pick_candidate_agent(context: dict[str, Any]) -> str:
    candidate = context.get("recommended_agent") or context.get("best_agent")
    if candidate:
        return str(candidate).strip()

    recommendations = context.get("agent_recommendations") or []
    if isinstance(recommendations, list):
        for item in recommendations:
            if isinstance(item, dict) and item.get("agent_name"):
                return str(item["agent_name"]).strip()
    return ""


async def should_switch_agent_via_leaderboard(
    *,
    tenant_id: str,
    task_type: str,
    current_agent: str,
    candidate_agent: str,
    delta_threshold: float = 0.15,
) -> tuple[bool, str]:
    try:
        from services.streaming.core.redis_ import get_async_redis

        redis_client = get_async_redis()
        if not redis_client:
            return False, "leaderboard_unavailable"

        ranked = await redis_client.zrevrange(
            f"sinc:leaderboard:{tenant_id}:{task_type}",
            0,
            2,
            withscores=True,
        )
        if not ranked:
            return False, "leaderboard_empty"

        normalized = [(str(name), float(score)) for name, score in ranked]
        top_agents = {name for name, _ in normalized}
        if candidate_agent not in top_agents:
            return False, "candidate_outside_top3"

        candidate_score = next((score for name, score in normalized if name == candidate_agent), None)
        current_score = next((score for name, score in normalized if name == current_agent), None)
        if candidate_score is None:
            return False, "candidate_missing_score"
        if current_score is None:
            return True, "current_agent_unranked"
        if (candidate_score - current_score) <= delta_threshold:
            return False, "insufficient_score_delta"
        return True, f"leaderboard_delta_gt_{int(delta_threshold * 100)}pct"
    except Exception as exc:
        log.debug("switch_agent_leaderboard_check_failed error=%s", exc)
        return False, "leaderboard_error"

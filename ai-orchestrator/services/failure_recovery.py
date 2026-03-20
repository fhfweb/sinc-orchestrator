import logging
from typing import Dict, Any, List, Optional
from enum import Enum

log = logging.getLogger("orch.failure.recovery")

class FailureType(Enum):
    INFRASTRUCTURE = "infra"
    LOGIC_ERROR = "logic"
    COMPLEXITY   = "complexity"
    UNKNOWN      = "unknown"

async def handle_task_failure(
    task_id: str,
    description: str,
    task_type: str,
    error_msg: str,
    attempt: int,
    current_agent: str,
    tenant_id: str
) -> Dict[str, Any]:
    """
    Module 2.2: Recovery Decision Engine.
    """
    error_lower = error_msg.lower()
    
    if any(k in error_lower for k in ["429", "timeout", "rate limit", "connection", "503"]):
        ftype = FailureType.INFRASTRUCTURE
    elif any(k in error_lower for k in ["recursion", "too long", "token limit", "context overflow", "complex"]):
        ftype = FailureType.COMPLEXITY
    else:
        ftype = FailureType.LOGIC_ERROR

    if ftype == FailureType.INFRASTRUCTURE:
        if attempt < 3:
            return {"action": "retry", "delay_seconds": 2 ** attempt * 10}
        return {"action": "dead_letter"}

    if ftype == FailureType.LOGIC_ERROR:
        # Module 4.1: Autonomous Re-planning via Neo4j
        try:
            from neo4j import GraphDatabase
            from services.context_retriever import NEO4J_URI, NEO4J_USER, NEO4J_PASS
            auth = (NEO4J_USER, NEO4J_PASS)
            
            def _query_alt():
                with GraphDatabase.driver(NEO4J_URI, auth=auth) as driver:
                    with driver.session() as s:
                        res = s.run("""
                            MATCH (failed_t:Task {id: $tid})
                            OPTIONAL MATCH (failed_t)-[:SIMILAR_TO]-(similar:Task)
                            WHERE similar.status = 'done' AND similar.tenant_id = $tid_env
                            
                            MATCH (best_a:Agent)-[:SUCCEEDED_ON]->(similar)
                            WHERE best_a.name <> $failed_agent AND best_a.tenant_id = $tid_env
                            
                            MATCH (similar)-[:RESOLVED_BY]->(sol:Solution)
                            
                            RETURN best_a.name as agent, sol.description as approach
                            ORDER BY similar.created_at DESC LIMIT 1
                        """, tid=task_id, tid_env=tenant_id, failed_agent=current_agent)
                        return res.single()
            
            import asyncio
            record = await asyncio.to_thread(_query_alt)
            if record:
                return {
                    "action": "retry_with_different_agent",
                    "new_agent": record["agent"],
                    "injected_hint": f"Autonomous Re-planning: Found sibling task successfully resolved by {record['agent']}. Try this approach: {record['approach'][:200]}..."
                }
        except Exception as e:
            log.warning("alternative_path_search_failed error=%s", e)
            
        return {"action": "retry", "delay_seconds": 60, "reasoning": "Logic error. No structural alternative found."}

    if ftype == FailureType.COMPLEXITY:
        return {"action": "decompose_and_retry", "next_node": "goal_decomposer"}

    return {"action": "escalate"}

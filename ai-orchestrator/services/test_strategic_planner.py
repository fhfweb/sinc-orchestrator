import asyncio
import sys
import os
from unittest.mock import AsyncMock, patch

# Add services to path
sys.path.append(os.getcwd())

from services.mcts_planner import MCTSPlanner, get_planner

async def test_planner():
    planner = get_planner()
    
    # Mock _fetch_metrics to avoid DB connection issues
    mock_metrics = {
        "expert_agent:all": 0.95,
        "junior_agent:all": 0.40,
        "_best_agent_": "expert_agent"
    }
    
    # Replace the internal _fetch_metrics with a mock
    planner.mcts._fetch_metrics = AsyncMock(return_value=mock_metrics)
    
    context = {"tenant_id": "system", "project_id": "test"}
    
    tasks = [
        "refactor",
        "review",
        "analyze_impact",
        "fix_bug"
    ]
    
    print("Testing Strategic MCTS Planner (Level 5) with Mocked Metrics...")
    
    for ttype in tasks:
        print(f"\nPlanning for task_type: {ttype}")
        res = await planner.plan(ttype, context)
        
        print(f"  Planner    : {res.get('planner')}")
        print(f"  Steps      : {' -> '.join(res.get('steps', [])) if res.get('steps') else 'None'}")
        if 'confidence' in res:
            print(f"  Confidence : {res.get('confidence')}")
        if 'best_agent' in res:
            print(f"  Best Agent : {res.get('best_agent')}")
        print(f"  LLM Needed : {res.get('llm_needed')}")

if __name__ == "__main__":
    asyncio.run(test_planner())


import asyncio
import os
import json
from cognitive_graph import get_cognitive_graph

# Mock Task
MOCK_TASK = {
    "task": {
        "id": "test_intelligence_task_001",
        "title": "Fix database connection leak",
        "description": "The system is leaking database connections in the auth module.",
        "task_type": "bugfix"
    },
    "description": "Fix database connection leak in auth module",
    "task_type": "bugfix",
    "project_id": "sinc",
    "tenant_id": "local",
    "start_time": 0,
    "tokens_saved": 0,
    "tokens_used": 0,
    "latency_ms": 0,
    "llm_needed": True,
    "hint": "",
    "planner_name": "test_planner",
    "cache_level": "",
    "steps": []
}

async def test_cognitive_graph():
    print("Starting Cognitive Graph Verification...")
    graph = get_cognitive_graph()
    
    # Run the graph
    # We use a thread_id to support persistence
    config = {"configurable": {"thread_id": "test_run_1"}}
    
    try:
        # Initial input state (matching CognitiveState TypedDict)
        input_state = {
            "task": MOCK_TASK["task"],
            "description": MOCK_TASK["description"],
            "task_type": MOCK_TASK["task_type"],
            "project_id": MOCK_TASK["project_id"],
            "tenant_id": MOCK_TASK["tenant_id"],
            "start_time": 0.0,
            "tokens_saved": 0,
            "tokens_used": 0,
            "latency_ms": 0.0,
            "llm_needed": True,
            "hint": "",
            "planner_name": "test_planner",
            "cache_level": "",
            "steps": [],
            "solution": None,
            "error": None,
            "proactive_context": None,
            "confidence": 0.0
        }
        
        print("Executing graph...")
        # Since we don't have a real LLM connected in this env test, it might fail at llm_solver
        # but we want to see if the preceding nodes work.
        async for event in graph.astream(input_state, config):
            for node_name, node_state in event.items():
                print(f"Node: {node_name}")
                if "solution" in node_state and node_state["solution"]:
                    print(f"  -> Solution found: {node_state['solution'][:50]}...")
                if "proactive_context" in node_state and node_state["proactive_context"]:
                    print(f"  -> Proactive context generated: {node_state['proactive_context'].get('proactive_summary')}")
                if "confidence" in node_state:
                    print(f"  -> Confidence: {node_state['confidence']}")

    except Exception as e:
        print(f"Graph execution failed (expected if LLM missing): {e}")

    print("Verification complete.")


import asyncio
import sys
import os

# Add services to path
sys.path.append(os.getcwd())

from services.context_engine import get_context_engine

async def test_engine():
    engine = get_context_engine()
    engine.token_budget = 500 # Small budget for testing
    
    chunks = [
        {"file": "auth.py", "line": 10, "text": "def login():\n    pass # Relevant chunk 1", "hybrid_score": 0.9},
        {"file": "db.py", "line": 50, "text": "def connect():\n    # Long code chunk that should be included\n" + ("x" * 1000), "hybrid_score": 0.85},
        {"file": "utils.py", "line": 100, "text": "def helper():\n    pass # Less relevant", "hybrid_score": 0.4},
    ]
    
    past_solutions = [
        {"task_id": "TASK-1", "solution": "Fixed the login bug by adding a timeout. " + ("s" * 2000), "hybrid_score": 0.75}
    ]
    
    print(f"Testing ContextEngine with budget {engine.token_budget} tokens...")
    
    # We mock _call_ollama to avoid needing a running model for this structural test
    # if it were a real test, but here I want to see if it summarizers
    
    result = await engine.compress_context(chunks, past_solutions, [], [])
    
    print("\n=== COMPRESSED CONTEXT ===")
    print(result)
    print("==========================")
    
    token_est = engine.estimate_tokens(result)
    print(f"\nEstimated tokens: {token_est}")
    assert token_est <= engine.token_budget + 50 # allowance for headers

if __name__ == "__main__":
    asyncio.run(test_engine())

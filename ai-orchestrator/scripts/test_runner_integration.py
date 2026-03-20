import os
import sys
from pathlib import Path

# Add services to path
sys.path.append(str(Path("g:/Fernando/project0/ai-orchestrator").resolve()))

from services.local_agent_runner import HybridAgentRunner

def test_integration():
    runner = HybridAgentRunner(available_backends=["ollama"])
    
    # Test task for AI Architect
    task_architect = {
        "id": "test-arch-001",
        "agent": "ai architect",
        "description": "Create an ADR for a new caching layer using Redis."
    }
    
    print("--- Running test for AI Architect ---")
    # We won't actually call the LLM if we want to just test the prompt resolution
    # But let's try a real run if OLLAMA is up, or just print the brief.
    
    # To test WITHOUT LLM call, we can inspect the generated prompt in a mock
    # OR just run it and see if it fails gracefully with 'Anthropic API key missing' if it tries to use Anthropic
    
    try:
        # We can simulate the prompt generation by calling the internal methods
        agent_name = task_architect.get("agent")
        sys_prompt = get_system_prompt(agent_name)
        print(f"Agent: {agent_name}")
        print(f"System Prompt Length: {len(sys_prompt)}")
        print(f"System Prompt Prefix: {sys_prompt[:100]}...")
        
        result = runner.run("Help me design this.", task=task_architect)
        print(f"Status: {result.status}")
        print(f"Backend Used: {result.backend_used}")
        print(f"Iteration Count: {result.iteration_count}")
    except Exception as e:
        print(f"Integration test failed: {e}")

# Add helper to runner class for testing? No, just use get_system_prompt directly
from services.agents_config import get_system_prompt

if __name__ == "__main__":
    test_integration()

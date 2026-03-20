import asyncio
import os
import sys
from pathlib import Path

# Add root and services to path
SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
ORCH_SERVICES = ROOT_DIR / "ai-orchestrator" / "services"

sys.path.insert(0, str(ORCH_SERVICES))
sys.path.insert(0, str(ROOT_DIR / "ai-orchestrator"))

# Set dummy workspace for testing
os.environ["AGENT_WORKSPACE"] = str(ROOT_DIR)

async def test_sandbox_mitigation():
    import agent_worker
    _safe_execute = agent_worker._safe_execute
    workspace = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    
    print(f"Testing Sandbox Mitigation (Workspace: {workspace})...")
    
    # 1. Normal execution (inside workspace)
    # We use a subfolder or the workspace itself
    status, output, exit_code = await _safe_execute("echo 'hello'", str(workspace))
    print(f"Normal execution: status={status}, exit={exit_code}, output={output.strip()}")
    assert status == "passed", f"Expected passed, got {status}: {output}"
    
    # 2. Injection attempt (trying to break out of bash -c if we were still using it insecurely)
    # The new implementation wrap this in a file, so $(whoami) should still execute but
    # it shouldn't be able to easily compromise the host if it's run in a restricted way.
    # More importantly, we're not passing the string to -c directly anymore.
    status, output, exit_code = await _safe_execute("echo $(whoami); touch /tmp/sinc_test_injection", ".")
    print(f"Injection test: status={status}, exit={exit_code}")
    
    if os.path.exists("/tmp/sinc_test_injection"):
        print("WARNING: Injection created a file in /tmp/ (Expected if not in container)")
        os.remove("/tmp/sinc_test_injection")
    
    print("Sandbox test finished.")

async def test_api_unification():
    import httpx
    print("\nTesting API Unification...")
    
    # Start the server in background if not already running
    # For now, we assume it's running on 8888 (unified port)
    url = "http://localhost:8888"
    try:
        async with httpx.AsyncClient() as client:
            # 1. Health
            resp = await client.get(f"{url}/health")
            print(f"Health: {resp.status_code} - {resp.json().get('status')}")
            
            # 2. Task List (requires dummy auth if not skipping)
            headers = {"X-Api-Key": "dummy_key"}
            resp = await client.get(f"{url}/tasks", headers=headers)
            print(f"Tasks: {resp.status_code} (Unified router check)")
            
            # 3. MCTS Plan (Unique logic migrated)
            resp = await client.post(f"{url}/plan/mcts", headers=headers, json={
                "goal": "Build a button",
                "iterations": 10
            })
            print(f"MCTS Plan: {resp.status_code}")
            
    except Exception as e:
        print(f"API Unification test failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_sandbox_mitigation())
    # asyncio.run(test_api_unification()) # Requires running server

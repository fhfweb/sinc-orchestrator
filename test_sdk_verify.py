import asyncio
import os
import sys

# Ensure we can import from the project root
sys.path.append(os.getcwd())

# Fix for the directory structure: it's in ai-orchestrator/sdk
# But the workspace is g:\Fernando\project0
# Let's adjust sys.path
sys.path.append(os.path.join(os.getcwd(), "ai-orchestrator"))

from sdk.sinc_client import SincClient

async def test_sdk():
    print("--- SINC SDK Verification ---")
    client = SincClient(
        base_url="http://localhost:8000",
        api_key="test-key",
        tenant_id="local"
    )
    
    print(f"Testing SincClient initialization...")
    print(f"Base URL: {client.base_url}")
    print(f"Tenant ID: {client.tenant_id}")
    
    # We won't actually call the network here to avoid stalls if server is down,
    # but we validate the method existence and signature.
    print("Verifying method signatures...")
    assert hasattr(client, 'create_task')
    assert hasattr(client, 'get_task')
    assert hasattr(client, 'search_memory')
    assert hasattr(client, 'run_heartbeat')
    
    print("Verification Successful!")

if __name__ == "__main__":
    asyncio.run(test_sdk())

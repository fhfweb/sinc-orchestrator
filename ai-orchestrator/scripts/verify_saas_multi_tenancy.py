import requests
import json
import uuid

BASE_URL = "http://localhost:8765"
VALID_API_KEY = "sk-sinc-123456"  # SINC Default Tenant
DEV_API_KEY = "dev"               # Local Development Tenant
INVALID_API_KEY = "sk-invalid-999"

def test_unauthorized():
    print("Testing Unauthorized access...")
    try:
        r = requests.get(f"{BASE_URL}/tasks", headers={"X-API-Key": INVALID_API_KEY})
        if r.status_code == 401:
            print("  [PASS] 401 Unauthorized received for invalid key.")
        else:
            print(f"  [FAIL] Expected 401, got {r.status_code}")
    except Exception as e:
        print(f"  [ERROR] Connection failed: {e}")

def test_tenant_isolation():
    print("Testing Tenant Isolation...")
    
    # 1. Fetch SINC tasks
    print("  Fetching tasks for SINC...")
    r_sinc = requests.get(f"{BASE_URL}/tasks", headers={"X-API-Key": VALID_API_KEY})
    if r_sinc.status_code == 200:
        tasks_sinc = r_sinc.json().get("tasks", [])
        print(f"  [OK] Found {len(tasks_sinc)} tasks for SINC.")
    else:
        print(f"  [FAIL] SINC fetch failed: {r_sinc.status_code}")
        return

    # 2. Fetch Local tasks
    print("  Fetching tasks for Local Dev...")
    r_local = requests.get(f"{BASE_URL}/tasks", headers={"X-API-Key": DEV_API_KEY})
    if r_local.status_code == 200:
        tasks_local = r_local.json().get("tasks", [])
        print(f"  [OK] Found {len(tasks_local)} tasks for Local.")
    else:
        print(f"  [FAIL] Local fetch failed: {r_local.status_code}")
        return

    # 3. Cross-check isolation
    # SINC tenant should see 'sinc-tenant' tasks, Local should see 'local' tasks
    # (Assuming the seed data worked and we have separated tasks)
    
    # Let's create a task for SINC and verify Local doesn't see it
    print("  Creating test task for SINC...")
    test_task = {
        "id": f"test-task-{uuid.uuid4().hex[:8]}",
        "project_id": "sinc",
        "name": "Multi-Tenancy Verification Task",
        "description": "Verification task created by automated test."
    }
    r_create = requests.post(f"{BASE_URL}/tasks", headers={"X-API-Key": VALID_API_KEY}, json=test_task)
    if r_create.status_code == 201:
        print("  [OK] Task created for SINC.")
    else:
        print(f"  [FAIL] Failed to create task: {r_create.status_code} - {r_create.text}")
        return

    # Check if SINC sees it
    r_sinc_check = requests.get(f"{BASE_URL}/tasks", headers={"X-API-Key": VALID_API_KEY})
    tasks_sinc_new = [t for t in r_sinc_check.json().get("tasks", []) if t['id'] == test_task['id']]
    if len(tasks_sinc_new) > 0:
        print("  [PASS] SINC tenant sees its own task.")
    else:
        print("  [FAIL] SINC tenant could NOT see its own task.")

    # Check if Local sees it (Should NOT)
    r_local_check = requests.get(f"{BASE_URL}/tasks", headers={"X-API-Key": DEV_API_KEY})
    tasks_local_new = [t for t in r_local_check.json().get("tasks", []) if t['id'] == test_task['id']]
    if len(tasks_local_new) == 0:
        print("  [PASS] Local tenant cannot see SINC task (ISOLATION OK).")
    else:
        print("  [FAIL] CROSS-TENANT VISIBILITY! Isolation failed.")

if __name__ == "__main__":
    print("=== ORCHESTRATOR SAAS VERIFICATION ===\n")
    test_unauthorized()
    print("-" * 40)
    test_tenant_isolation()
    print("\nVerification Complete.")

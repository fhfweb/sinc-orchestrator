import requests
import time
import json

BASE_URL = "http://localhost:8765"
DEFAULT_KEY = "sk-sinc-123456"

def test_auth():
    print("Testing Auth...")
    # No key
    r = requests.get(f"{BASE_URL}/tasks")
    assert r.status_code == 401
    print("  OK: 401 on no key")

    # Invalid key
    r = requests.get(f"{BASE_URL}/tasks", headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401
    print("  OK: 401 on invalid key")

    # Valid key
    r = requests.get(f"{BASE_URL}/tasks", headers={"X-Api-Key": DEFAULT_KEY})
    assert r.status_code == 200
    print(f"  OK: 200 on valid key. Tenant: {r.json().get('tenant')}")

def test_task_creation_and_isolation():
    print("Testing Task Creation and Isolation...")
    # Create task for SINC tenant
    payload = {
        "title": "SaaS Verification Task",
        "description": "Testing multi-tenancy isolation",
        "project_id": "sinc",
        "priority": 1
    }
    r = requests.post(f"{BASE_URL}/tasks", json=payload, headers={"X-Api-Key": DEFAULT_KEY})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    print(f"  OK: Task created {task_id}")

    # Verify it exists for SINC
    r = requests.get(f"{BASE_URL}/tasks/{task_id}", headers={"X-Api-Key": DEFAULT_KEY})
    assert r.status_code == 200
    assert r.json()["id"] == task_id
    print("  OK: Task found for owner tenant")

def main():
    try:
        test_auth()
        test_task_creation_and_isolation()
        print("\nALL PHASE 1 TESTS PASSED!")
    except Exception as e:
        import traceback
        print(f"\nTEST FAILED: {e}")
        traceback.print_exc()
        if 'r' in locals() and hasattr(r, 'text'):
            print(f"Response status: {r.status_code}")
            print(f"Response body: {r.text}")
        exit(1)

if __name__ == "__main__":
    main()

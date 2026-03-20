from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected: set[int] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    response = client.request(method, path, **kwargs)
    if expected and response.status_code not in expected:
        raise RuntimeError(
            f"{method} {path} failed with {response.status_code}: {response.text}"
        )
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke E2E for the Python control plane.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--api-key", default="dev")
    parser.add_argument("--project-id", default="sinc")
    parser.add_argument("--agent-name", default="e2e-smoke-agent")
    args = parser.parse_args()

    task_id = f"e2e-smoke-{int(time.time())}"
    headers = {"X-Api-Key": args.api_key}

    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30.0) as client:
        health = _request(client, "GET", "/health", expected={200})
        print(json.dumps({"health": health.json()}, ensure_ascii=True))

        created = _request(
            client,
            "POST",
            "/tasks",
            expected={201},
            json={
                "id": task_id,
                "title": f"Python-only control plane smoke task {task_id}",
                "description": f"End-to-end validation for create -> claim -> heartbeat -> complete ({task_id})",
                "priority": 2,
                "project_id": args.project_id,
                "metadata": {"origin": "e2e_python_control_plane", "tag": "e2e-smoke", "claim_token": task_id},
            },
        )
        print(json.dumps({"task_created": created.json()}, ensure_ascii=True))

        scheduler = _request(client, "POST", "/scheduler/run", expected={200})
        print(json.dumps({"scheduler": scheduler.json()}, ensure_ascii=True))

        current_task = _request(client, "GET", f"/tasks/{task_id}", expected={200}).json()
        effective_agent = current_task.get("assigned_agent") or args.agent_name

        claim = _request(
            client,
            "POST",
            "/tasks/claim",
            expected={200},
            json={"agent_name": effective_agent, "project_id": args.project_id, "tags": [task_id]},
        )
        claim_payload = claim.json()
        claimed = claim_payload.get("task") or {}
        if not claimed or claimed.get("id") != task_id:
            raise RuntimeError(f"unexpected claimed task: {claim_payload}")
        print(json.dumps({"claimed": claimed}, ensure_ascii=True))

        heartbeat = _request(
            client,
            "POST",
            f"/tasks/{task_id}/heartbeat",
            expected={200},
            json={
                "agent": effective_agent,
                "progress": 55,
                "step": "e2e-smoke-running",
                "metadata": {"origin": "e2e_python_control_plane"},
            },
        )
        print(json.dumps({"heartbeat": heartbeat.json()}, ensure_ascii=True))

        completed = _request(
            client,
            "POST",
            "/tasks/complete",
            expected={200},
            json={
                "task_id": task_id,
                "status": "done",
                "summary": "Python-only E2E smoke completed successfully",
                "files_modified": [],
                "backend_used": "e2e-smoke",
                "tests_passed": True,
                "policy_violations": [],
                "next_suggested_tasks": [],
                "agent_name": effective_agent,
            },
        )
        print(json.dumps({"completed": completed.json()}, ensure_ascii=True))

        task = _request(client, "GET", f"/tasks/{task_id}", expected={200}).json()
        if task.get("status") != "done":
            raise RuntimeError(f"task did not reach done: {task}")
        print(json.dumps({"task_final": task}, ensure_ascii=True))

        readiness = _request(client, "POST", "/readiness/run", expected={200})
        print(json.dumps({"readiness": readiness.json()}, ensure_ascii=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())

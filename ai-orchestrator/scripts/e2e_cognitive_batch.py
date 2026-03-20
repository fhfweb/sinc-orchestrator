from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the cognitive batch endpoint against the live control plane.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--api-key", default="dev")
    args = parser.parse_args()

    payload = {
        "tasks": [
            {
                "id": "batch-smoke-1",
                "task_type": "docs",
                "title": "Summarize runtime status",
                "description": "Produce a concise runtime summary for the canonical orchestrator.",
                "project_id": "orchestrator",
            },
            {
                "id": "batch-smoke-2",
                "task_type": "testing",
                "title": "Check health endpoint",
                "description": "Validate that the health endpoint is healthy and report anomalies.",
                "project_id": "orchestrator",
            },
        ]
    }

    with httpx.Client(base_url=args.base_url, timeout=60.0, headers={"X-Api-Key": args.api_key}) as client:
        response = client.post("/cognitive/batch", json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("total") != len(payload["tasks"]):
            print(json.dumps({"ok": False, "reason": "unexpected_total", "response": data}, indent=2))
            return 1
        batch_job_id = str(data.get("batch_job_id") or "").strip()
        if not batch_job_id:
            print(json.dumps({"ok": False, "reason": "missing_batch_job_id", "response": data}, indent=2))
            return 1

        deadline = time.time() + 60
        status_payload = None
        while time.time() < deadline:
            status_resp = client.get(f"/cognitive/batch/{batch_job_id}")
            status_resp.raise_for_status()
            status_payload = status_resp.json()
            job = status_payload.get("job") or {}
            state = str(job.get("status") or "").lower()
            if state in {"completed", "failed"}:
                break
            time.sleep(1)

    if not status_payload:
        print(json.dumps({"ok": False, "reason": "missing_status_payload"}, indent=2))
        return 1

    job = status_payload.get("job") or {}
    items = status_payload.get("items") or []
    if str(job.get("status") or "").lower() != "completed":
        print(json.dumps({"ok": False, "reason": "batch_not_completed", "response": status_payload}, indent=2))
        return 1
    if len(items) != len(payload["tasks"]):
        print(json.dumps({"ok": False, "reason": "unexpected_items_length", "response": status_payload}, indent=2))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "batch_job_id": batch_job_id,
                "status": job.get("status"),
                "total": job.get("task_count"),
                "llm_used": job.get("llm_used_count"),
                "cache_hits": job.get("cache_hit_count"),
                "task_ids": [item.get("task_id") for item in items],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

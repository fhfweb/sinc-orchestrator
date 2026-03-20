from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected: set[int],
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    response = client.request(method, path, json=json_body)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    if response.status_code not in expected:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
    payload = response.json() if response.content else {}
    return payload, elapsed_ms


def _run_flow(
    client: httpx.Client,
    *,
    cycle: int,
    project_id: str,
    agent_name: str,
) -> dict[str, Any]:
    samples: dict[str, list[float]] = {
        "health": [],
        "observer": [],
        "scheduler": [],
        "bridge": [],
        "readiness": [],
        "create": [],
        "claim": [],
        "heartbeat": [],
        "complete": [],
    }
    summary: dict[str, Any] = {"cycle": cycle, "ts": _now()}

    payload, elapsed = _request(client, "GET", "/health", expected={200})
    samples["health"].append(elapsed)
    summary["health"] = payload

    task_id = f"soak-task-{int(time.time())}-{cycle}"
    payload, elapsed = _request(
        client,
        "POST",
        "/tasks",
        expected={201},
        json_body={
            "id": task_id,
            "title": f"Soak validation task {task_id}",
            "description": f"Cycle {cycle} control-plane soak validation",
            "priority": 2,
            "project_id": project_id,
            "metadata": {
                "origin": "soak_python_control_plane",
                "claim_token": task_id,
                "execution_mode": "llm-native",
            },
        },
    )
    samples["create"].append(elapsed)
    summary["task_created"] = payload

    payload, elapsed = _request(client, "POST", "/scheduler/run", expected={200})
    samples["scheduler"].append(elapsed)
    summary["scheduler"] = payload

    payload, elapsed = _request(
        client,
        "GET",
        f"/tasks/{task_id}",
        expected={200},
    )
    samples.setdefault("task_fetch", []).append(elapsed)
    effective_agent = payload.get("assigned_agent") or agent_name
    summary["task_current"] = payload

    payload, elapsed = _request(
        client,
        "POST",
        "/tasks/claim",
        expected={200},
        json_body={"agent_name": effective_agent, "project_id": project_id, "tags": [task_id]},
    )
    samples["claim"].append(elapsed)
    claimed = payload.get("task") or {}
    if claimed.get("id") != task_id:
        raise RuntimeError(f"unexpected claimed task: {payload}")
    summary["claimed"] = claimed

    payload, elapsed = _request(
        client,
        "POST",
        f"/tasks/{task_id}/heartbeat",
        expected={200},
        json_body={
            "agent": effective_agent,
            "progress": 50,
            "step": "soak-running",
            "metadata": {"origin": "soak_python_control_plane", "cycle": cycle},
        },
    )
    samples["heartbeat"].append(elapsed)
    summary["heartbeat"] = payload

    payload, elapsed = _request(
        client,
        "POST",
        "/tasks/complete",
        expected={200},
        json_body={
            "task_id": task_id,
            "status": "done",
            "summary": "Soak validation completed successfully",
            "files_modified": [],
            "backend_used": "soak-python",
            "tests_passed": True,
            "policy_violations": [],
            "next_suggested_tasks": [],
            "agent_name": effective_agent,
        },
    )
    samples["complete"].append(elapsed)
    summary["completed"] = payload

    payload, elapsed = _request(client, "POST", "/observer/run", expected={200})
    samples["observer"].append(elapsed)
    summary["observer"] = payload

    payload, elapsed = _request(client, "POST", "/external-bridge/run", expected={200})
    samples["bridge"].append(elapsed)
    summary["external_bridge"] = payload

    payload, elapsed = _request(client, "POST", "/incidents/reconcile", expected={200})
    samples["readiness"].append(elapsed)
    summary["incident_reconcile"] = payload

    payload, elapsed = _request(client, "POST", "/readiness/run", expected={200})
    samples["readiness"].append(elapsed)
    summary["readiness"] = payload
    summary["samples_ms"] = {key: values for key, values in samples.items() if values}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Soak harness for the canonical Python control plane.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--api-key", default="dev")
    parser.add_argument("--project-id", default="sinc")
    parser.add_argument("--agent-name", default="soak-agent")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--interval-s", type=float, default=10.0)
    args = parser.parse_args()

    headers = {"X-Api-Key": args.api_key}
    failures: list[dict[str, Any]] = []
    all_samples: dict[str, list[float]] = {}

    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30.0) as client:
        for cycle in range(1, args.cycles + 1):
            started = time.perf_counter()
            try:
                result = _run_flow(
                    client,
                    cycle=cycle,
                    project_id=args.project_id,
                    agent_name=args.agent_name,
                )
                for key, values in result.get("samples_ms", {}).items():
                    all_samples.setdefault(key, []).extend(values)
                print(json.dumps({"cycle": cycle, "result": result}, ensure_ascii=True))
            except Exception as exc:
                failure = {"cycle": cycle, "error": str(exc), "ts": _now()}
                failures.append(failure)
                print(json.dumps({"cycle": cycle, "failure": failure}, ensure_ascii=True), file=sys.stderr)
            remaining = args.interval_s - (time.perf_counter() - started)
            if cycle < args.cycles and remaining > 0:
                time.sleep(remaining)

    summary = {
        "cycles": args.cycles,
        "failures": len(failures),
        "failure_details": failures,
        "latency_ms": {
            key: {
                "count": len(values),
                "avg": round(statistics.fmean(values), 2) if values else 0.0,
                "p95": round(_p95(values), 2) if values else 0.0,
                "max": round(max(values), 2) if values else 0.0,
            }
            for key, values in sorted(all_samples.items())
        },
        "ts": _now(),
    }
    print(json.dumps({"summary": summary}, ensure_ascii=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

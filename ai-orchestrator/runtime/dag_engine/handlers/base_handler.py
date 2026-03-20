"""
SINC Orchestrator Native Task Handlers — Base + Common Types
Each handler maps to a task type prefix and executes natively
without requiring the external agent bridge.

Supported prefixes:
    REPAIR-*       → RepairHandler
    QA-*           → QAHandler
    OPS-*          → OpsHandler
    PLAN-*         → PlanHandler
    V2-*           → V2InfraHandler
"""

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

BASE          = Path(__file__).parent.parent.parent.parent
COMPLETIONS   = BASE / "tasks" / "completions"
KNOWLEDGE_BASE= BASE / "knowledge_base"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_completion(task_id: str, agent_name: str, status: str,
                      summary: str, files_modified: list[str] | None = None,
                      policy_violations: list[str] | None = None,
                      next_suggested_tasks: list[dict] | None = None) -> Path:
    """Write a v3-compliant completion artifact."""
    payload = {
        "schema_version":        "v3-completion",
        "task_id":               task_id,
        "agent_name":            agent_name,
        "status":                status,
        "files_modified":        files_modified or [],
        "tests_passed":          status == "success",
        "policy_violations":     policy_violations or [],
        "next_suggested_tasks":  next_suggested_tasks or [],
        "summary":               summary,
        "tool_calls":            [],
        "local_library_candidates": [],
        "library_decision": {
            "selected_option":    "not-applicable",
            "justification":      "Native handler execution.",
            "selected_libraries": [],
            "rejected_libraries": [],
        },
        "validated_at": _now_iso(),
    }
    COMPLETIONS.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d%H%M%S")
    path = COMPLETIONS / f"{task_id}-{ts}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


class BaseHandler(ABC):
    """Abstract base for all native task handlers."""

    def __init__(self, task: dict):
        self.task    = task
        self.task_id = task["id"]

    @abstractmethod
    def can_handle(self) -> bool:
        """Return True if this handler can execute the task."""

    @abstractmethod
    def execute(self) -> dict:
        """Execute the task. Returns completion payload dict."""

    def _complete(self, status: str, summary: str, **kwargs) -> dict:
        path = _write_completion(
            self.task_id,
            self.__class__.__name__,
            status,
            summary,
            **kwargs
        )
        return {"status": status, "summary": summary, "completion_file": str(path)}

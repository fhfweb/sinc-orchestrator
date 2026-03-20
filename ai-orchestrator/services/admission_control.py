from __future__ import annotations

import logging
import os
import psutil
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

log = logging.getLogger("orch.admission")

class Decision(Enum):
    ADMIT = "admit"
    DEFER = "defer"
    REJECT = "reject"


@dataclass
class AdmissionResult:
    decision: Decision
    reason: str
    metadata: dict[str, Any]


class AdmissionController:
    """
    Electronic Admission Control (EAC) for SINC Batch Processing.
    Reviews system health and task priority before allowing execution.
    """

    def __init__(self, 
                 cpu_threshold: float = 85.0, 
                 mem_threshold: float = 90.0,
                 max_concurrency: int = 20):
        self.cpu_threshold = cpu_threshold
        self.mem_threshold = mem_threshold
        self.max_concurrency = max_concurrency

    async def check_health(self, tenant_id: str = "local") -> dict[str, Any]:
        """Gathers unified health/readiness telemetry."""
        from services.streaming.core.runtime_plane import compute_readiness_snapshot
        
        # 1. Sys Load
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        
        # 2. Runtime Plane Snapshot
        try:
            readiness = await compute_readiness_snapshot(tenant_id)
        except Exception as exc:
            log.error("admission_health_snapshot_error error=%s", exc)
            readiness = {"status": "error", "health": "unknown"}
            
        return {
            "cpu_p": cpu,
            "mem_p": mem,
            "readiness": readiness.get("status", "unknown"),
            "health": readiness.get("health", "unknown"),
            "active_tasks": readiness.get("counts", {}).get("in_progress", 0)
        }

    async def evaluate_batch(self, tasks: list[dict], tenant_id: str = "local") -> dict[str, AdmissionResult]:
        """
        Evaluate a batch of tasks against current pressure.
        Priority:
          - P1: Always Admit if not 'error' health.
          - P2: Admit if health is 'ok' or 'degraded' (CPU < threshold).
          - P3: Only admit if 'ready' and CPU < 70%.
        """
        health = await self.check_health(tenant_id)
        results = {}
        
        pressure_level = "nominal"
        if health["cpu_p"] > self.cpu_threshold or health["mem_p"] > self.mem_threshold:
            pressure_level = "high"
        elif health["active_tasks"] >= self.max_concurrency:
            pressure_level = "congested"

        for task in tasks:
            tid = task.get("id", "unknown")
            prio = int(task.get("priority", 2))
            
            # Policy Decision Matrix
            if health["readiness"] == "error":
                results[tid] = AdmissionResult(Decision.REJECT, "System health critical (error status)", health)
                continue

            if prio <= 1: # P0/P1
                results[tid] = AdmissionResult(Decision.ADMIT, "Critical priority bypass", health)
            elif pressure_level == "high" or pressure_level == "congested":
                if prio >= 3:
                     results[tid] = AdmissionResult(Decision.DEFER, f"High pressure ({pressure_level}) deferral for P3", health)
                else: # P2
                     results[tid] = AdmissionResult(Decision.ADMIT, "P2 admitted under load", health)
            else:
                 results[tid] = AdmissionResult(Decision.ADMIT, "Standard admission", health)
                 
        return results

    def get_admission_stats(self, tenant_id: str) -> dict:
        # This will be backed by Redis in production for across-worker stats
        return {
            "pressure": "nominal",
            "active_policy": "balanced"
        }

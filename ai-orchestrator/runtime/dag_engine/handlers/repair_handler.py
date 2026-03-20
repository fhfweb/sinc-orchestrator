"""
Native handler for REPAIR-* tasks.
Detects the incident type from task metadata and applies the appropriate fix.
"""

import json
import re
from pathlib import Path
from .base_handler import BaseHandler, _now_iso

BASE     = Path(__file__).parent.parent.parent.parent
SINC_APP = BASE.parent / "app"


class RepairHandler(BaseHandler):

    def can_handle(self) -> bool:
        return self.task_id.startswith("REPAIR-")

    def execute(self) -> dict:
        metadata = self.task.get("metadata") or {}
        incident_type = metadata.get("incident_type", "")
        evidence      = metadata.get("evidence", "")

        # Route to specific repair based on incident type
        if "NoRawQueries" in incident_type or "raw" in evidence.lower():
            return self._repair_raw_queries(metadata)
        elif "debug" in incident_type.lower():
            return self._repair_debug_mode()
        else:
            return self._complete(
                "partial",
                f"REPAIR handler: incident_type='{incident_type}' requires manual review.",
                next_suggested_tasks=[{
                    "title":       f"Manual review of {self.task_id}",
                    "description": f"Automated repair could not handle incident: {incident_type}",
                    "priority":    "P1",
                }]
            )

    def _repair_raw_queries(self, metadata: dict) -> dict:
        """
        Detect PDO raw SQL in files listed in metadata and generate
        an Eloquent-migration report for the AI Engineer to apply.
        """
        files_affected = metadata.get("files_affected", [])
        violations_found = []

        raw_sql_pattern = re.compile(
            r'\$\w*pdo\w*->(?:query|exec|prepare)\s*\(',
            re.IGNORECASE
        )

        for rel_path in files_affected:
            candidate = SINC_APP / rel_path.lstrip("/")
            if not candidate.exists():
                # Try relative to SINC root
                candidate = BASE.parent / rel_path.lstrip("/")
            if candidate.exists():
                try:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                    matches = raw_sql_pattern.findall(content)
                    if matches:
                        violations_found.append({
                            "file":    rel_path,
                            "matches": matches[:5],
                        })
                except Exception:
                    pass

        summary = (
            f"Raw SQL scan complete. Found {len(violations_found)} file(s) with PDO patterns. "
            "Recommend migration to Eloquent query builder. "
            "See next_suggested_tasks for AI Engineer action items."
        )

        next_tasks = []
        for v in violations_found:
            next_tasks.append({
                "title":       f"Migrate raw SQL in {Path(v['file']).name}",
                "description": f"Replace PDO raw queries with Eloquent/QueryBuilder in {v['file']}",
                "priority":    "P0",
                "files":       [v["file"]],
            })

        return self._complete(
            "success" if violations_found else "partial",
            summary,
            policy_violations=[f"NoRawQueries in {v['file']}" for v in violations_found],
            next_suggested_tasks=next_tasks,
        )

    def _repair_debug_mode(self) -> dict:
        env_file = BASE.parent / ".env"
        if env_file.exists():
            content = env_file.read_text(encoding="utf-8")
            if "APP_DEBUG=true" in content:
                return self._complete(
                    "failed",
                    "APP_DEBUG=true detected in .env — must be set to false for production. "
                    "Apply fix manually or via deployment pipeline.",
                    policy_violations=["DebugModeEnabled: APP_DEBUG=true in .env"],
                    next_suggested_tasks=[{
                        "title":       "Disable debug mode in .env",
                        "description": "Set APP_DEBUG=false and LOG_LEVEL=warning in .env",
                        "priority":    "P0",
                    }]
                )
        return self._complete("success", "Debug mode already disabled.")

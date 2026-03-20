#!/usr/bin/env python3
"""
Business Context Engine
=======================
Builds a structured world model from an intake .env file.

Usage:
  python scripts/business_context_engine.py --intake <path> --project-id <slug> --output-dir <dir>
  python scripts/business_context_engine.py --intake <path> --project-id <slug> --output-dir <dir> --update
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


REQUIRED_FIELDS = [
    "PROJECT_NAME",
    "OWNER",
    "CORE_PROBLEM",
    "WHO_HAS_THIS_PROBLEM",
    "UNIQUE_VALUE",
    "REVENUE_MODEL",
    "NORTH_STAR_METRIC",
    "PERSONA_1_NAME",
    "PERSONA_1_ROLE",
    "CRITICAL_FLOW_1",
    "CRITICAL_FLOW_2",
]

ENUM_RULES = {
    "REVENUE_MODEL": {"subscription", "usage_based", "one_time", "freemium", "marketplace"},
    "PERSONA_1_TECHNICAL_LEVEL": {"low", "medium", "high", ""},
    "ARCHITECTURAL_PATTERN": {"monolith", "modular_monolith", "microservices", "serverless", "event_driven", "hybrid", ""},
    "DATA_SENSITIVITY": {"public", "internal", "confidential", "restricted", ""},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data


def parse_multiline_list(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\\n", "\n")
    items: list[str] = []
    for raw in normalized.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        if line:
            items.append(line)
    if not items and "," in value:
        return [p.strip() for p in value.split(",") if p.strip()]
    return items


def to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "sim"}


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not values.get(field, "").strip():
            errors.append(f"missing required field: {field}")
    for field, allowed in ENUM_RULES.items():
        raw = values.get(field, "").strip().lower()
        if raw and raw not in allowed:
            errors.append(f"invalid enum: {field}={raw!r}")
    if values.get("CRITICAL_FLOW_1", "").strip() and not values.get("FLOW_1_ACCEPTANCE", "").strip():
        errors.append("FLOW_1_ACCEPTANCE is required when CRITICAL_FLOW_1 is set")
    if values.get("CRITICAL_FLOW_2", "").strip() and not values.get("FLOW_2_ACCEPTANCE", "").strip():
        errors.append("FLOW_2_ACCEPTANCE is required when CRITICAL_FLOW_2 is set")
    return errors


def build_personas(v: dict[str, str]) -> list[dict[str, Any]]:
    personas: list[dict[str, Any]] = []
    for i in range(1, 4):
        name = v.get(f"PERSONA_{i}_NAME", "").strip()
        if not name:
            continue
        personas.append(
            {
                "id": f"persona_{i}",
                "name": name,
                "role": v.get(f"PERSONA_{i}_ROLE", "").strip(),
                "is_payer": to_bool(v.get(f"PERSONA_{i}_IS_PAYER", "false")),
                "is_primary_user": i == 1,
                "technical_level": v.get(f"PERSONA_{i}_TECHNICAL_LEVEL", "medium").strip().lower(),
                "main_goal": v.get(f"PERSONA_{i}_MAIN_GOAL", "").strip(),
                "main_frustration": v.get(f"PERSONA_{i}_MAIN_FRUSTRATION", "").strip(),
                "critical_journeys": [],
            }
        )
    return personas


def build_critical_flows(v: dict[str, str]) -> list[dict[str, Any]]:
    blockers = {x.strip() for x in v.get("LAUNCH_BLOCKER_FLOWS", "").split(",") if x.strip()}
    flows: list[dict[str, Any]] = []
    for i in range(1, 8):
        desc = v.get(f"CRITICAL_FLOW_{i}", "").strip()
        if not desc:
            continue
        acceptance = v.get(f"FLOW_{i}_ACCEPTANCE", "").strip()
        flows.append(
            {
                "id": f"flow_{i}",
                "name": f"Critical Flow {i}",
                "persona_id": "persona_1",
                "priority": "must_have" if i <= 2 else "should_have",
                "description": desc,
                "steps": [],
                "definition_of_done": acceptance,
                "acceptance_criteria": [acceptance] if acceptance else [],
                "is_launch_blocker": str(i) in blockers,
            }
        )
    return flows


def build_integrations(v: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(1, 6):
        name = v.get(f"INTEGRATION_{i}_NAME", "").strip()
        if not name:
            continue
        rows.append(
            {
                "id": f"integration_{i}",
                "name": name,
                "type": v.get(f"INTEGRATION_{i}_TYPE", "other").strip(),
                "direction": v.get(f"INTEGRATION_{i}_DIRECTION", "bidirectional").strip(),
                "is_mandatory": to_bool(v.get(f"INTEGRATION_{i}_MANDATORY", "true")),
                "auth_method": v.get(f"INTEGRATION_{i}_AUTH_METHOD", "api_key").strip(),
                "contract_url": "",
                "notes": v.get(f"INTEGRATION_{i}_NOTES", "").strip(),
            }
        )
    return rows


def build_world_model(values: dict[str, str], project_id: str, preserve: dict[str, Any] | None) -> dict[str, Any]:
    ts = now_iso()
    personas = build_personas(values)
    flows = build_critical_flows(values)
    integrations = build_integrations(values)
    mvp_included = parse_multiline_list(values.get("MVP_INCLUDED", ""))
    mvp_excluded = parse_multiline_list(values.get("MVP_EXCLUDED", ""))
    post_launch = parse_multiline_list(values.get("POST_LAUNCH_BACKLOG", ""))
    launch_criteria = parse_multiline_list(values.get("LAUNCH_SUCCESS_CRITERIA", ""))
    month_3 = parse_multiline_list(values.get("MONTH_3_SUCCESS_CRITERIA", ""))
    month_12 = parse_multiline_list(values.get("MONTH_12_SUCCESS_CRITERIA", ""))
    unknown_scope = [x for x in [values.get("PROJECT_NAME", ""), values.get("CORE_PROBLEM", "")] if not x.strip()]

    world_model: dict[str, Any] = {
        "$schema": "orchestrator/world_model/v1",
        "version": "1.0.0",
        "meta": {
            "project_id": project_id,
            "project_name": values.get("PROJECT_NAME", project_id).strip() or project_id,
            "created_at": ts,
            "updated_at": ts,
            "cycle": 1,
            "status": "draft",
            "owner": values.get("OWNER", "").strip(),
            "orchestrator_version": os.getenv("ORCHESTRATOR_VERSION", "unknown"),
            "intake_hash": "",
        },
        "area_1_product_strategy": {
            "problem_statement": {
                "core_problem": values.get("CORE_PROBLEM", "").strip(),
                "who_has_this_problem": values.get("WHO_HAS_THIS_PROBLEM", "").strip(),
                "current_alternative": values.get("CURRENT_ALTERNATIVE", "").strip(),
                "why_current_alternative_fails": values.get("WHY_ALTERNATIVE_FAILS", "").strip(),
                "evidence_of_problem": "",
            },
            "personas": personas,
            "value_proposition": {
                "unique_value": values.get("UNIQUE_VALUE", "").strip(),
                "why_not_competitor": "",
                "key_differentiators": [],
            },
            "business_model": {
                "revenue_model": values.get("REVENUE_MODEL", "subscription").strip().lower(),
                "pricing_tiers": [],
                "target_cac": "",
                "target_ltv": "",
                "target_ltv_cac_ratio": "",
            },
            "success_criteria": {
                "launch_criteria": launch_criteria,
                "month_3_criteria": month_3,
                "month_12_criteria": month_12,
                "north_star_metric": values.get("NORTH_STAR_METRIC", "").strip(),
            },
            "main_risks": [],
        },
        "area_2_requirements": {
            "critical_flows": flows,
            "mvp_scope": {
                "included": mvp_included,
                "explicitly_excluded": mvp_excluded,
                "post_launch_backlog": post_launch,
            },
            "functional_requirements": [],
            "non_functional_requirements": {
                "performance": {
                    "max_response_time_ms": to_int(values.get("MAX_RESPONSE_TIME_MS", "500"), 500),
                    "max_response_time_critical_endpoints_ms": to_int(values.get("MAX_RESPONSE_TIME_CRITICAL_MS", "200"), 200),
                    "acceptable_error_rate_percent": to_float(values.get("TARGET_ERROR_RATE_PERCENT", "1"), 1),
                },
                "availability": {
                    "target_uptime_percent": to_float(values.get("TARGET_UPTIME_PERCENT", "99.9"), 99.9),
                    "rto_minutes": to_int(values.get("RTO_MINUTES", "60"), 60),
                    "rpo_minutes": to_int(values.get("RPO_MINUTES", "15"), 15),
                },
                "scalability": {
                    "users_at_launch": to_int(values.get("USERS_AT_LAUNCH", "0"), 0),
                    "users_at_month_6": to_int(values.get("USERS_AT_MONTH_6", "0"), 0),
                    "users_at_month_12": to_int(values.get("USERS_AT_MONTH_12", "0"), 0),
                    "concurrent_users_peak": to_int(values.get("CONCURRENT_USERS_PEAK", "0"), 0),
                },
                "security": {
                    "data_sensitivity": values.get("DATA_SENSITIVITY", "internal").strip().lower(),
                    "pii_present": to_bool(values.get("PII_PRESENT", "true")),
                    "mfa_required": to_bool(values.get("MFA_REQUIRED", "false")),
                },
            },
            "compliance": {
                "lgpd": to_bool(values.get("COMPLIANCE_LGPD", "false")),
                "hipaa": to_bool(values.get("COMPLIANCE_HIPAA", "false")),
                "pci_dss": to_bool(values.get("COMPLIANCE_PCI_DSS", "false")),
                "cfm": to_bool(values.get("COMPLIANCE_CFM", "false")),
                "ans": to_bool(values.get("COMPLIANCE_ANS", "false")),
                "data_retention_days": to_int(values.get("DATA_RETENTION_DAYS", "0"), 0),
                "right_to_erasure": to_bool(values.get("RIGHT_TO_ERASURE", "false")),
                "data_export_required": to_bool(values.get("DATA_EXPORT_REQUIRED", "false")),
            },
            "integrations": integrations,
            "out_of_scope": mvp_excluded,
        },
        "area_3_architecture": {
            "stack": {
                "language_backend": values.get("STACK_LANGUAGE_BACKEND", "").strip(),
                "framework_backend": values.get("STACK_FRAMEWORK_BACKEND", "").strip(),
                "language_frontend": values.get("STACK_LANGUAGE_FRONTEND", "").strip(),
                "framework_frontend": values.get("STACK_FRAMEWORK_FRONTEND", "").strip(),
                "database_primary": values.get("STACK_DATABASE_PRIMARY", "").strip(),
                "database_secondary": values.get("STACK_DATABASE_SECONDARY", "").strip(),
                "cache": values.get("STACK_CACHE", "").strip(),
                "queue": values.get("STACK_QUEUE", "").strip(),
                "cloud_provider": values.get("STACK_CLOUD_PROVIDER", "").strip(),
            },
            "architectural_pattern": {
                "pattern": values.get("ARCHITECTURAL_PATTERN", "").strip().lower(),
                "rationale": "",
                "multi_tenancy": {
                    "enabled": to_bool(values.get("MULTI_TENANCY_REQUIRED", "false")),
                    "strategy": values.get("MULTI_TENANCY_STRATEGY", "none").strip().lower(),
                },
            },
            "data_model": {"core_entities": [], "migration_strategy": "", "seed_strategy": ""},
            "auth_strategy": {"authentication": "", "authorization": "", "roles": []},
            "communication": {"sync_protocol": "", "async_protocol": "", "api_versioning_strategy": ""},
            "infrastructure": {"environments": ["development", "staging", "production"]},
            "adrs": [],
            "component_map": {"components": [], "dependencies": []},
        },
        "area_4_ux": {
            "journeys": [],
            "ui_states": [],
            "accessibility": {
                "wcag_level": values.get("WCAG_LEVEL", "AA").strip(),
            },
            "design_system": {
                "exists": to_bool(values.get("DESIGN_SYSTEM_EXISTS", "false")),
                "location": values.get("DESIGN_SYSTEM_LOCATION", "").strip(),
            },
            "mobile_first": to_bool(values.get("MOBILE_FIRST", "false")),
        },
        "area_5_development": {
            "backend": {"standards": [], "service_contracts": []},
            "frontend": {"standards": [], "state_management": ""},
            "infra": {"ci_cd": "", "secrets_policy": "vault-only"},
            "branching_strategy": {"model": "trunk_or_short_lived_branches"},
        },
        "area_6_quality": {
            "testing_strategy": {"unit": True, "integration": True, "e2e": True},
            "critical_regressions": [flow["id"] for flow in flows if flow.get("is_launch_blocker")],
            "performance": {"targets": []},
            "security": {"owasp_required": True},
        },
        "area_7_operations": {
            "deployment": {"strategy": values.get("MAIN_DEPLOYMENT_STRATEGY", "rolling").strip().lower()},
            "rollback": {"strategy": values.get("ROLLBACK_STRATEGY", "automatic").strip().lower()},
            "monitoring": {"signals": ["latency", "error_rate", "conversion_rate"]},
            "incident_response": {"on_call_owner": values.get("ON_CALL_OWNER", "").strip()},
        },
        "area_8_launch": {
            "go_to_market": {"plan": ""},
            "onboarding": {"first_value_moment": ""},
            "feedback_loop": {"channels": []},
            "roadmap": {"next_milestones": post_launch},
        },
        "orchestrator_context": {
            "architecture_decisions": [],
            "agent_memory": [],
            "known_risks": unknown_scope,
            "production_learnings": [],
        },
    }

    if preserve and isinstance(preserve.get("orchestrator_context"), dict):
        world_model["orchestrator_context"] = preserve["orchestrator_context"]
        world_model["meta"]["cycle"] = int(preserve.get("meta", {}).get("cycle", 1) or 1) + 1
        world_model["meta"]["created_at"] = preserve.get("meta", {}).get("created_at", ts)

    intake_hash = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    world_model["meta"]["intake_hash"] = intake_hash
    return world_model


def persist_qdrant_summary(world_model: dict[str, Any], project_id: str, host: str, port: int) -> dict[str, Any]:
    if requests is None:
        return {"enabled": False, "reason": "requests-not-installed"}
    collection = f"{project_id}-memory"
    base = f"http://{host}:{port}"
    summary = world_model.get("area_1_product_strategy", {}).get("problem_statement", {}).get("core_problem", "")
    details = json.dumps(
        {
            "project_name": world_model.get("meta", {}).get("project_name", project_id),
            "north_star": world_model.get("area_1_product_strategy", {}).get("success_criteria", {}).get("north_star_metric", ""),
        },
        ensure_ascii=False,
    )
    vector_seed = hashlib.sha256((project_id + ":" + summary).encode("utf-8")).digest()
    vector = [((vector_seed[i % len(vector_seed)] / 255.0) - 0.5) for i in range(128)]
    payload = {
        "project_slug": project_id,
        "node_type": "business_context_world_model",
        "summary": summary[:500],
        "details": details[:2000],
        "source": "business_context_engine",
    }

    try:
        requests.put(
            f"{base}/collections/{collection}",
            json={"vectors": {"size": 128, "distance": "Cosine"}},
            timeout=8,
        )
        point_id = int(hashlib.sha256((project_id + "::world_model").encode("utf-8")).hexdigest()[:16], 16)
        upsert = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
        resp = requests.put(f"{base}/collections/{collection}/points", json=upsert, timeout=10)
        return {"enabled": True, "ok": resp.ok, "status_code": resp.status_code, "collection": collection}
    except Exception as exc:  # pragma: no cover
        return {"enabled": True, "ok": False, "error": str(exc), "collection": collection}


@dataclass
class Args:
    intake: Path
    project_id: str
    output_dir: Path
    schema: Path | None
    update: bool
    dry_run: bool
    sync_qdrant: bool
    qdrant_host: str
    qdrant_port: int


def parse_args() -> Args:
    ap = argparse.ArgumentParser(description="Build world model from intake env")
    ap.add_argument("--intake", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--schema", default="")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sync-qdrant", action="store_true")
    ap.add_argument("--qdrant-host", default=os.getenv("QDRANT_HOST", "localhost"))
    ap.add_argument("--qdrant-port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    ns = ap.parse_args()
    return Args(
        intake=Path(ns.intake),
        project_id=str(ns.project_id).strip().lower(),
        output_dir=Path(ns.output_dir),
        schema=Path(ns.schema) if ns.schema else None,
        update=bool(ns.update),
        dry_run=bool(ns.dry_run),
        sync_qdrant=bool(ns.sync_qdrant),
        qdrant_host=str(ns.qdrant_host),
        qdrant_port=int(ns.qdrant_port),
    )


def main() -> int:
    args = parse_args()
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", args.project_id):
        print(json.dumps({"success": False, "error": "invalid-project-id"}, ensure_ascii=False))
        return 2
    if not args.intake.exists():
        print(json.dumps({"success": False, "error": f"intake-not-found:{args.intake}"}, ensure_ascii=False))
        return 2

    values = parse_env(args.intake)
    errors = validate(values)
    if errors:
        print(json.dumps({"success": False, "error": "invalid-intake", "details": errors}, ensure_ascii=False))
        return 3

    args.output_dir.mkdir(parents=True, exist_ok=True)
    world_path = args.output_dir / "world-model.json"
    world_versioned = args.output_dir / f"world_model_{args.project_id}.json"
    preserve = None
    if args.update and world_path.exists():
        try:
            preserve = json.loads(world_path.read_text(encoding="utf-8"))
        except Exception:
            preserve = None

    model = build_world_model(values, args.project_id, preserve)

    if not args.dry_run:
        world_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        world_versioned.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")

    qdrant_result: dict[str, Any] = {"enabled": False}
    if args.sync_qdrant and not args.dry_run:
        qdrant_result = persist_qdrant_summary(
            world_model=model,
            project_id=args.project_id,
            host=args.qdrant_host,
            port=args.qdrant_port,
        )

    print(
        json.dumps(
            {
                "success": True,
                "project_id": args.project_id,
                "world_model_path": str(world_path),
                "world_model_versioned_path": str(world_versioned),
                "dry_run": args.dry_run,
                "qdrant": qdrant_result,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


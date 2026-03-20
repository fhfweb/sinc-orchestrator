"""
graph_query.py - controlled Graph RAG query runner for the AI Orchestrator.

Security model:
- Template allowlist is the default execution mode.
- Raw Cypher is blocked unless ORCHESTRATOR_GRAPH_ALLOW_RAW=1.
- Raw Cypher denies write/admin keywords.
- Row limits are enforced to control cost.
- Query audit events are appended to ai-orchestrator/state/graph-query-audit.jsonl.

Usage:
    python graph_query.py --project-path <path> --template module_impact --params '{"module":"auth"}'
    python graph_query.py --project-path <path> --cypher "MATCH (n) RETURN n LIMIT 10" --allow-raw
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


GRAPH_TEMPLATES: dict[str, dict[str, Any]] = {
    "module_impact": {
        "cypher": (
            "MATCH (src) "
            "WHERE toLower(coalesce(src.name, src.module, '')) = toLower($module) "
            "OPTIONAL MATCH (src)-[r*1..2]->(dst) "
            "RETURN coalesce(src.name, src.module, 'unknown') AS source, "
            "coalesce(dst.name, dst.module, dst.path, toString(id(dst)), 'unknown') AS impacted, "
            "size(r) AS hops "
            "LIMIT $limit"
        ),
        "required_params": ["module"],
        "default_limit": 50,
    },
    "file_dependents": {
        "cypher": (
            "MATCH (f) "
            "WHERE toLower(coalesce(f.path, f.file, '')) = toLower($path) "
            "OPTIONAL MATCH (n)-[r*1..3]->(f) "
            "RETURN coalesce(n.name, n.module, n.path, 'unknown') AS dependent, "
            "coalesce(f.path, f.file, 'unknown') AS target_file, "
            "size(r) AS hops "
            "LIMIT $limit"
        ),
        "required_params": ["path"],
        "default_limit": 80,
    },
    "task_risks_open": {
        "cypher": (
            "MATCH (t) "
            "WHERE toLower(coalesce(t.type, '')) CONTAINS 'risk' "
            "AND toLower(coalesce(t.status, '')) IN ['open','pending','in-progress','blocked'] "
            "RETURN coalesce(t.id, 'unknown') AS id, coalesce(t.status, 'unknown') AS status, "
            "coalesce(t.reason, t.title, 'unknown') AS reason "
            "LIMIT $limit"
        ),
        "required_params": [],
        "default_limit": 50,
    },
}


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _sanitize_limit(value: Any, max_rows: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = fallback
    if parsed < 1:
        parsed = fallback
    if parsed > max_rows:
        parsed = max_rows
    return parsed


def _is_safe_raw_cypher(cypher: str) -> tuple[bool, str]:
    lowered = cypher.lower()
    if len(cypher) > 4000:
        return False, "raw-cypher-too-large"
    blocked = [
        r"\bcreate\b",
        r"\bmerge\b",
        r"\bdelete\b",
        r"\bdetach\b",
        r"\bset\b",
        r"\bdrop\b",
        r"\bcall\s+dbms\b",
        r"\bload\s+csv\b",
        r"\bapoc\.periodic\b",
    ]
    for pat in blocked:
        if re.search(pat, lowered, re.IGNORECASE):
            return False, f"raw-cypher-denied:{pat}"
    return True, ""


def _append_limit_if_missing(cypher: str, limit_value: int) -> str:
    if re.search(r"\blimit\s+\$?\w+", cypher, re.IGNORECASE):
        return cypher
    return f"{cypher.rstrip()} LIMIT {limit_value}"


def _resolve_query(template: str, cypher: str, params: dict[str, Any], max_rows: int) -> tuple[str, dict[str, Any], str]:
    if template:
        tpl = GRAPH_TEMPLATES.get(template)
        if not tpl:
            raise ValueError(f"template-not-allowed:{template}")
        required = tpl.get("required_params", [])
        for key in required:
            value = params.get(key)
            if value is None or str(value).strip() == "":
                raise ValueError(f"template-missing-param:{key}")
        default_limit = int(tpl.get("default_limit", 50))
        limit = _sanitize_limit(params.get("limit"), max_rows=max_rows, fallback=default_limit)
        query_params = dict(params)
        query_params["limit"] = limit
        return str(tpl["cypher"]), query_params, "template"

    allow_raw = _as_bool(os.getenv("ORCHESTRATOR_GRAPH_ALLOW_RAW"), default=False)
    if not allow_raw:
        raise ValueError("raw-cypher-disabled")
    if not cypher or not cypher.strip():
        raise ValueError("cypher-required")
    safe, reason = _is_safe_raw_cypher(cypher)
    if not safe:
        raise ValueError(reason)
    limit = _sanitize_limit(params.get("limit"), max_rows=max_rows, fallback=max_rows)
    query_params = dict(params)
    query_params.pop("limit", None)
    return _append_limit_if_missing(cypher, limit), query_params, "raw"


def _neo4j_query(cypher: str, params: dict[str, Any], max_rows: int, timeout_seconds: int) -> dict[str, Any]:
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        return {"error": "neo4j-driver-not-installed", "results": []}

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    pw = os.getenv("NEO4J_PASSWORD", "")
    if not pw:
        return {"error": "NEO4J_PASSWORD-not-set", "results": []}

    started = time.time()
    try:
        driver = GraphDatabase.driver(uri, auth=(user, pw))
        with driver.session() as session:
            raw = session.run(cypher, **params)
            records: list[dict[str, Any]] = []
            for idx, rec in enumerate(raw):
                if idx >= max_rows:
                    break
                records.append(dict(rec))
                if (time.time() - started) > timeout_seconds:
                    break
        driver.close()
        return {
            "source": "neo4j",
            "results": records,
            "count": len(records),
            "truncated": len(records) >= max_rows,
        }
    except Exception as exc:
        return {"error": f"neo4j-error:{exc}", "results": []}


def _markdown_fallback(project_path: Path, query_hint: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    search_dirs = [
        repo_root / "memory_graph" / "nodes",
        repo_root / "memory_graph" / "cross-project" / "nodes",
        project_path / "ai-orchestrator" / "memory",
    ]
    tokens = re.findall(r"'([^']+)'|\"([^\"]+)\"|\b(\w{4,})\b", query_hint)
    keywords = [t[0] or t[1] or t[2] for t in tokens if any(t)]
    keywords = [k.lower() for k in keywords if k.lower() not in {
        "match", "where", "return", "limit", "node", "with", "order",
        "contains", "null", "true", "false", "and", "or", "not",
    }]

    results: list[dict[str, Any]] = []
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md in search_dir.glob("*.md"):
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lowered = content.lower()
            score = sum(1 for kw in keywords if kw in lowered)
            if score > 0:
                rel_path = str(md)
                try:
                    rel_path = str(md.relative_to(repo_root))
                except Exception:
                    pass
                results.append(
                    {
                        "node": md.stem,
                        "path": rel_path.replace("\\", "/"),
                        "score": score,
                        "snippet": content[:300].replace("\n", " "),
                    }
                )
    results.sort(key=lambda r: r["score"], reverse=True)
    return {
        "source": "markdown-fallback",
        "query_keywords": keywords,
        "results": results[:20],
        "count": len(results),
    }


def _append_audit(project_path: Path, event: dict[str, Any]) -> None:
    audit_path = project_path / "ai-orchestrator" / "state" / "graph-query-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled Graph RAG query runner for Orchestrator.")
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--template", default="")
    parser.add_argument("--cypher", default="")
    parser.add_argument("--params", default="{}")
    parser.add_argument("--max-rows", type=int, default=int(os.getenv("ORCHESTRATOR_GRAPH_MAX_ROWS", "200")))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("ORCHESTRATOR_GRAPH_TIMEOUT_SECONDS", "8")))
    args = parser.parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.exists():
        print(json.dumps({"error": f"project-not-found:{project_path}", "results": []}, ensure_ascii=False))
        return 1

    params: dict[str, Any] = {}
    try:
        parsed = json.loads(args.params)
        if isinstance(parsed, dict):
            params = parsed
    except json.JSONDecodeError:
        params = {}

    started = time.time()
    mode = "template" if args.template else "raw"
    try:
        cypher, final_params, resolved_mode = _resolve_query(
            template=args.template.strip(),
            cypher=args.cypher,
            params=params,
            max_rows=max(1, int(args.max_rows)),
        )
        mode = resolved_mode
    except Exception as exc:
        error = str(exc)
        result = {"error": error, "results": [], "mode": mode}
        _append_audit(
            project_path,
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mode": mode,
                "template": args.template.strip(),
                "success": False,
                "error": error,
                "duration_ms": int((time.time() - started) * 1000),
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = _neo4j_query(cypher, final_params, max_rows=max(1, int(args.max_rows)), timeout_seconds=max(1, int(args.timeout_seconds)))
    if result.get("error"):
        fallback_hint = json.dumps({"template": args.template, "cypher": cypher, "params": final_params}, ensure_ascii=False)
        fallback = _markdown_fallback(project_path, fallback_hint)
        result = {
            **fallback,
            "mode": mode,
            "template": args.template.strip(),
            "neo4j_error": result.get("error", ""),
        }
    else:
        result["mode"] = mode
        result["template"] = args.template.strip()

    _append_audit(
        project_path,
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "template": args.template.strip(),
            "success": not bool(result.get("error")),
            "count": int(result.get("count", 0) or 0),
            "duration_ms": int((time.time() - started) * 1000),
        },
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

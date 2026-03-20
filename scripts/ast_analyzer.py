"""
ast_analyzer.py — AST Knowledge Graph extractor.

Parses source files using language-native AST tools:
  - Python:     stdlib `ast` module
  - TypeScript: regex-based (no TS compiler needed)
  - JavaScript: regex-based
  - PHP:        regex-based
  - Go:         regex-based
  - Java:       regex-based

Outputs a structured JSON graph of:
  - nodes: {id, type, name, file, line, docstring, params}
  - edges: {from, to, kind}  (calls, imports, inherits, implements)

Optionally syncs nodes/edges to Neo4j (requires neo4j-driver).

Usage:
    python scripts/ast_analyzer.py --project-path <path> [--output graph.json] [--neo4j]
    python scripts/ast_analyzer.py --project-path <path> --stack python
"""

from __future__ import annotations

import argparse
import ast as pyast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


# ── Neo4j (optional) ─────────────────────────────────────────────────────────
try:
    from neo4j import GraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False


EXCLUDE_DIRS = {
    "node_modules", "vendor", ".git", "__pycache__", "dist",
    "build", ".venv", "venv", ".mypy_cache", ".pytest_cache",
}

MAX_FILES_PER_STACK = 300
PHP_CALL_KEYWORDS = {
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "catch",
    "isset",
    "empty",
    "array",
    "list",
    "echo",
    "print",
    "include",
    "require",
    "include_once",
    "require_once",
    "function",
    "return",
    "new",
}


# ── Python AST parsing ────────────────────────────────────────────────────────

def _python_docstring(node: pyast.AST) -> str:
    try:
        return pyast.get_docstring(node) or ""  # type: ignore[arg-type]
    except Exception:
        return ""


def _python_params(node: pyast.FunctionDef | pyast.AsyncFunctionDef) -> list[str]:
    return [arg.arg for arg in node.args.args]


def parse_python(file_path: Path, project_root: Path) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    rel = file_path.relative_to(project_root).as_posix()

    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = pyast.parse(source, filename=str(file_path))
    except SyntaxError:
        return nodes, edges

    module_id = "module:" + rel

    # Walk top-level and class-level definitions
    for node in pyast.walk(tree):
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            fn_id = f"func:{rel}:{node.name}:{node.lineno}"
            nodes.append({
                "id": fn_id,
                "type": "function",
                "name": node.name,
                "file": rel,
                "line": node.lineno,
                "docstring": _python_docstring(node),
                "params": _python_params(node),
            })
            # Detect calls inside function body
            for child in pyast.walk(node):
                if isinstance(child, pyast.Call):
                    called = ""
                    if isinstance(child.func, pyast.Name):
                        called = child.func.id
                    elif isinstance(child.func, pyast.Attribute):
                        called = child.func.attr
                    if called:
                        edges.append({"from": fn_id, "to": f"call:{called}", "kind": "calls"})

        elif isinstance(node, pyast.ClassDef):
            cls_id = f"class:{rel}:{node.name}:{node.lineno}"
            nodes.append({
                "id": cls_id,
                "type": "class",
                "name": node.name,
                "file": rel,
                "line": node.lineno,
                "docstring": _python_docstring(node),
                "params": [],
            })
            for base in node.bases:
                base_name = ""
                if isinstance(base, pyast.Name):
                    base_name = base.id
                elif isinstance(base, pyast.Attribute):
                    base_name = base.attr
                if base_name:
                    edges.append({"from": cls_id, "to": f"class:{base_name}", "kind": "inherits"})

        elif isinstance(node, (pyast.Import, pyast.ImportFrom)):
            if isinstance(node, pyast.ImportFrom) and node.module:
                edges.append({"from": module_id, "to": f"module:{node.module}", "kind": "imports"})
            elif isinstance(node, pyast.Import):
                for alias in node.names:
                    edges.append({"from": module_id, "to": f"module:{alias.name}", "kind": "imports"})

    return nodes, edges


# ── Regex-based parsers for other languages ───────────────────────────────────

def parse_regex(file_path: Path, project_root: Path, stack: str) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    rel = file_path.relative_to(project_root).as_posix()

    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return nodes, edges

    lines = source.splitlines()

    if stack in ("node", "typescript", "javascript"):
        # Functions/classes
        fn_pattern = re.compile(
            r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)|"
            r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>"
        )
        cls_pattern = re.compile(r"(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?")
        import_pattern = re.compile(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]")

        for lineno, line in enumerate(lines, 1):
            for m in fn_pattern.finditer(line):
                name = m.group(1) or m.group(3)
                params_raw = m.group(2) or m.group(4) or ""
                if name:
                    nodes.append({
                        "id": f"func:{rel}:{name}:{lineno}",
                        "type": "function",
                        "name": name,
                        "file": rel,
                        "line": lineno,
                        "docstring": "",
                        "params": [p.strip().split(":")[0] for p in params_raw.split(",") if p.strip()],
                    })
            for m in cls_pattern.finditer(line):
                cls_name = m.group(1)
                base = m.group(2)
                cls_id = f"class:{rel}:{cls_name}:{lineno}"
                nodes.append({"id": cls_id, "type": "class", "name": cls_name, "file": rel, "line": lineno, "docstring": "", "params": []})
                if base:
                    edges.append({"from": cls_id, "to": f"class:{base}", "kind": "inherits"})
            for m in import_pattern.finditer(line):
                edges.append({"from": f"module:{rel}", "to": f"module:{m.group(1)}", "kind": "imports"})

    elif stack == "go":
        fn_pattern = re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)")
        struct_pattern = re.compile(r"^type\s+(\w+)\s+struct\b")
        import_pattern = re.compile(r'"([^"]+)"')
        in_import = False
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("import ("):
                in_import = True
            elif in_import:
                if stripped == ")":
                    in_import = False
                else:
                    for m in import_pattern.finditer(stripped):
                        edges.append({"from": f"module:{rel}", "to": f"module:{m.group(1)}", "kind": "imports"})
            m = fn_pattern.match(stripped)
            if m:
                nodes.append({"id": f"func:{rel}:{m.group(1)}:{lineno}", "type": "function", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})
            m = struct_pattern.match(stripped)
            if m:
                nodes.append({"id": f"class:{rel}:{m.group(1)}:{lineno}", "type": "class", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})

    elif stack in ("php",):
        fn_pattern = re.compile(r"(?:public|protected|private|static|\s)*function\s+(\w+)\s*\(([^)]*)\)")
        cls_pattern = re.compile(r"class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?")
        method_call_pattern = re.compile(r"(?:->|::)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        direct_call_pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        current_fn_id = ""
        current_fn_depth = 0
        brace_depth = 0
        seen_call_edges: set[tuple[str, str]] = set()
        for lineno, line in enumerate(lines, 1):
            function_declared = False
            m = fn_pattern.search(line)
            if m:
                current_fn_id = f"func:{rel}:{m.group(1)}:{lineno}"
                current_fn_depth = brace_depth
                function_declared = True
                nodes.append({"id": current_fn_id, "type": "function", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})
            m = cls_pattern.search(line)
            if m:
                cls_id = f"class:{rel}:{m.group(1)}:{lineno}"
                nodes.append({"id": cls_id, "type": "class", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})
                if m.group(2):
                    edges.append({"from": cls_id, "to": f"class:{m.group(2)}", "kind": "inherits"})
                if m.group(3):
                    for iface in m.group(3).split(","):
                        edges.append({"from": cls_id, "to": f"class:{iface.strip()}", "kind": "implements"})

            if current_fn_id:
                for m_call in method_call_pattern.finditer(line):
                    called = m_call.group(1)
                    if not called:
                        continue
                    key = (current_fn_id, f"call:{called}")
                    if key not in seen_call_edges:
                        seen_call_edges.add(key)
                        edges.append({"from": current_fn_id, "to": f"call:{called}", "kind": "calls"})

                if not function_declared:
                    for m_call in direct_call_pattern.finditer(line):
                        called = m_call.group(1)
                        if not called:
                            continue
                        lowered = called.lower()
                        if lowered in PHP_CALL_KEYWORDS:
                            continue
                        key = (current_fn_id, f"call:{called}")
                        if key not in seen_call_edges:
                            seen_call_edges.add(key)
                            edges.append({"from": current_fn_id, "to": f"call:{called}", "kind": "calls"})

            brace_depth += line.count("{")
            brace_depth -= line.count("}")
            if brace_depth < 0:
                brace_depth = 0
            if current_fn_id and brace_depth <= current_fn_depth and "}" in line:
                current_fn_id = ""
                current_fn_depth = brace_depth

    elif stack == "java":
        fn_pattern = re.compile(r"(?:public|protected|private|static|final|\s)+\w+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws[^{]+)?\{")
        cls_pattern = re.compile(r"(?:public|abstract|final|\s)*class\s+(\w+)(?:\s+extends\s+(\w+))?")
        for lineno, line in enumerate(lines, 1):
            m = fn_pattern.search(line)
            if m and m.group(1) not in ("if", "while", "for", "switch"):
                nodes.append({"id": f"func:{rel}:{m.group(1)}:{lineno}", "type": "function", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})
            m = cls_pattern.search(line)
            if m:
                cls_id = f"class:{rel}:{m.group(1)}:{lineno}"
                nodes.append({"id": cls_id, "type": "class", "name": m.group(1), "file": rel, "line": lineno, "docstring": "", "params": []})
                if m.group(2):
                    edges.append({"from": cls_id, "to": f"class:{m.group(2)}", "kind": "inherits"})

    return nodes, edges


# ── File collection ───────────────────────────────────────────────────────────

STACK_EXTENSIONS: dict[str, list[str]] = {
    "python":     [".py"],
    "node":       [".ts", ".js", ".mjs", ".tsx"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".mjs"],
    "php":        [".php"],
    "go":         [".go"],
    "java":       [".java"],
    "dotnet":     [".cs"],
    "ruby":       [".rb"],
    "rust":       [".rs"],
}

ALL_EXTENSIONS = {ext for exts in STACK_EXTENSIONS.values() for ext in exts}


def collect_files(project_root: Path, stack: str) -> list[Path]:
    exts = set(STACK_EXTENSIONS.get(stack, list(ALL_EXTENSIONS)))
    result: list[Path] = []
    for path in project_root.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in exts:
            result.append(path)
        if len(result) >= MAX_FILES_PER_STACK:
            break
    return result


# ── Neo4j sync ────────────────────────────────────────────────────────────────

def normalize_project_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")
    if not cleaned:
        return "project"
    return cleaned[:63]


def sanitize_rel_kind(kind: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (kind or "").strip()).upper()
    return cleaned if cleaned else "RELATED_TO"


def label_from_node_type(node_type: str) -> str:
    mapping = {
        "class": "Class",
        "function": "Function",
        "module": "Module",
        "call": "Call",
    }
    return mapping.get((node_type or "").strip().lower(), "Code")


def chunked(rows: list[dict], size: int) -> list[list[dict]]:
    if size <= 0:
        size = 500
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def sync_to_neo4j(
    nodes: list[dict],
    edges: list[dict],
    uri: str,
    user: str,
    password: str,
    db: str,
    project_slug: str,
) -> dict:
    if not NEO4J_AVAILABLE:
        return {"status": "skipped", "reason": "neo4j-driver not installed"}
    try:
        slug = normalize_project_slug(project_slug)
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session(database=db) as session:
            session.run("CREATE CONSTRAINT memory_node_project_id IF NOT EXISTS FOR (n:MemoryNode) REQUIRE (n.project_slug, n.id) IS UNIQUE")
            session.run("CREATE INDEX project_slug_idx IF NOT EXISTS FOR (p:Project) ON (p.slug)")
            session.run("MERGE (:Project {slug: $project_slug})", project_slug=slug)

            node_groups: dict[str, list[dict]] = {}
            existing_ids: set[str] = set()
            for node in nodes:
                node_id = str(node.get("id", "")).strip()
                if not node_id:
                    continue
                existing_ids.add(node_id)
                node_type = str(node.get("type", "code")).strip().lower() or "code"
                label = label_from_node_type(node_type)
                rel_path = str(node.get("file", "")).replace("\\", "/").strip("/")
                module_name = rel_path.split("/", 1)[0] if rel_path else ""
                props = {
                    "node_type": node_type,
                    "name": str(node.get("name", "")).strip(),
                    "file": rel_path,
                    "line": int(node.get("line", 0) or 0),
                    "docstring": str(node.get("docstring", "")).strip(),
                    "params": node.get("params", []),
                    "summary": f"{node_type.capitalize()}: {str(node.get('name', '')).strip()}",
                    "details": f"Source: {rel_path}:{int(node.get('line', 0) or 0)}" if rel_path else "",
                    "project_slug": slug,
                    "project_id": slug,
                    "relative_path": rel_path,
                    "source_path": rel_path,
                    "source_files": [rel_path] if rel_path else [],
                    "source_modules": [module_name] if module_name else [],
                    "module_name": module_name,
                    "placeholder": False,
                    "tags": ["ast", node_type],
                }
                node_groups.setdefault(label, []).append({"id": node_id, "props": props})

            # Materialize symbolic call endpoints to reduce placeholder churn.
            for edge in edges:
                target_id = str(edge.get("to", "")).strip()
                if not target_id or not target_id.startswith("call:") or target_id in existing_ids:
                    continue
                call_name = target_id.split(":", 1)[1] if ":" in target_id else target_id
                call_props = {
                    "node_type": "call",
                    "name": call_name,
                    "file": "",
                    "line": 0,
                    "docstring": "",
                    "params": [],
                    "summary": f"Call target: {call_name}",
                    "details": "Symbolic call node inferred from AST relation.",
                    "project_slug": slug,
                    "project_id": slug,
                    "relative_path": "",
                    "source_path": "",
                    "source_files": [],
                    "source_modules": [],
                    "module_name": "",
                    "placeholder": False,
                    "tags": ["ast", "call"],
                }
                node_groups.setdefault("Call", []).append({"id": target_id, "props": call_props})
                existing_ids.add(target_id)

            for label, rows in node_groups.items():
                for batch in chunked(rows, 400):
                    session.run(
                        f"""
                        MERGE (p:Project {{slug: $project_slug}})
                        WITH p, $rows AS rows
                        UNWIND rows AS row
                        MERGE (n:MemoryNode {{project_slug: $project_slug, id: row.id}})
                        SET n += row.props, n.node_label = '{label}', n.updated_at = datetime()
                        MERGE (p)-[:HAS_NODE]->(n)
                        """,
                        project_slug=slug,
                        rows=batch,
                    )

            rel_groups: dict[str, list[dict]] = {}
            for edge in edges:
                source_id = str(edge.get("from", "")).strip()
                target_id = str(edge.get("to", "")).strip()
                if not source_id or not target_id:
                    continue
                rel_type = sanitize_rel_kind(str(edge.get("kind", "RELATED_TO")))
                rel_groups.setdefault(rel_type, []).append(
                    {
                        "source": source_id,
                        "target": target_id,
                        "origin": "ast_analyzer",
                        "reason": f"AST relation detected: {str(edge.get('kind', 'related_to')).strip().lower()}",
                    }
                )

            for rel_type, rows in rel_groups.items():
                for batch in chunked(rows, 600):
                    session.run(
                        f"""
                        MERGE (p:Project {{slug: $project_slug}})
                        WITH p, $rows AS rows
                        UNWIND rows AS row
                        MERGE (s:MemoryNode {{project_slug: $project_slug, id: row.source}})
                        ON CREATE SET s.node_type='unknown', s.placeholder=true, s.summary='', s.updated_at=datetime()
                        MERGE (t:MemoryNode {{project_slug: $project_slug, id: row.target}})
                        ON CREATE SET t.node_type='unknown', t.placeholder=true, t.summary='', t.updated_at=datetime()
                        MERGE (p)-[:HAS_NODE]->(s)
                        MERGE (p)-[:HAS_NODE]->(t)
                        MERGE (s)-[r:{rel_type}]->(t)
                        SET r.reason = row.reason, r.origin = row.origin, r.updated_at = datetime()
                        """,
                        project_slug=slug,
                        rows=batch,
                    )
        driver.close()
        return {"status": "ok", "project_slug": slug, "nodes": len(nodes), "edges": len(edges)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ── Save nodes to memory_graph ────────────────────────────────────────────────

def save_memory_nodes(nodes: list[dict], project_root: Path, stack: str) -> int:
    """Write summarized AST nodes to memory_graph/nodes/ as markdown for Qdrant/text indexing."""
    nodes_dir = project_root / "memory_graph" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)

    # Aggregate by file
    by_file: dict[str, list[dict]] = {}
    for node in nodes:
        f = node.get("file", "unknown")
        by_file.setdefault(f, []).append(node)

    written = 0
    for file_rel, file_nodes in list(by_file.items())[:50]:   # cap at 50 files
        safe = re.sub(r"[^a-z0-9]+", "-", file_rel.lower()).strip("-")
        node_path = nodes_dir / f"ast-{safe}.md"
        if node_path.exists():
            continue   # don't overwrite curated files
        classes = [n["name"] for n in file_nodes if n["type"] == "class"]
        funcs = [n["name"] for n in file_nodes if n["type"] == "function"]
        lines = [
            f"---",
            f"id: ast-{safe}",
            f"type: code-module",
            f"stack: {stack}",
            f"source_file: {file_rel}",
            f"tags: [ast, code-graph, {stack}]",
            f"---",
            "",
            f"# AST: {file_rel}",
            "",
            f"**Stack:** {stack}  |  **Classes:** {len(classes)}  |  **Functions:** {len(funcs)}",
            "",
        ]
        if classes:
            lines.append("## Classes")
            for c in classes[:20]:
                lines.append(f"- `{c}`")
            lines.append("")
        if funcs:
            lines.append("## Functions")
            for fn in funcs[:40]:
                lines.append(f"- `{fn}`")
            lines.append("")
        node_path.write_text("\n".join(lines), encoding="utf-8")
        written += 1
    return written


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AST Knowledge Graph extractor.")
    parser.add_argument("--project-path", required=True, help="Project root path")
    parser.add_argument("--project-slug", default="", help="Project slug for Neo4j namespace")
    parser.add_argument("--stack", default="auto", help="Language stack (python/node/go/php/java/auto)")
    parser.add_argument("--output", default="", help="Write graph JSON to file instead of stdout")
    parser.add_argument("--neo4j", action="store_true", help="Sync to Neo4j")
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="neo4j")
    parser.add_argument("--neo4j-db", default="neo4j")
    parser.add_argument("--save-memory-nodes", action="store_true", help="Write summary nodes to memory_graph/nodes/")
    args = parser.parse_args()

    project_root = Path(args.project_path).resolve()
    if not project_root.exists():
        print(json.dumps({"error": f"Project path not found: {project_root}"}))
        sys.exit(1)

    project_slug = str(args.project_slug or "").strip()
    if not project_slug:
        state_path = project_root / "ai-orchestrator" / "state" / "project-state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                project_slug = str(state.get("project_slug", "")).strip()
            except Exception:
                project_slug = ""
    if not project_slug:
        project_slug = normalize_project_slug(project_root.name)

    # Auto-detect stack from project state
    stack = args.stack
    if stack == "auto":
        state_path = project_root / "ai-orchestrator" / "state" / "project-state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                stack = state.get("stack", "auto")
                if not stack or stack == "unknown":
                    tf = state.get("technical_fingerprint", {})
                    stack = tf.get("primary_language", "auto")
            except Exception:
                pass
        if stack == "auto":
            stack = "python"   # default

    files = collect_files(project_root, stack)
    all_nodes: list[dict] = []
    all_edges: list[dict] = []

    for file_path in files:
        if stack == "python":
            n, e = parse_python(file_path, project_root)
        else:
            n, e = parse_regex(file_path, project_root, stack)
        all_nodes.extend(n)
        all_edges.extend(e)

    # Deduplicate nodes by id
    seen_ids: set[str] = set()
    deduped_nodes: list[dict] = []
    for node in all_nodes:
        if node["id"] not in seen_ids:
            seen_ids.add(node["id"])
            deduped_nodes.append(node)

    # Deduplicate edges by (from, kind, to).
    deduped_edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in all_edges:
        key = (
            str(edge.get("from", "")).strip(),
            sanitize_rel_kind(str(edge.get("kind", "RELATED_TO"))),
            str(edge.get("to", "")).strip(),
        )
        if not key[0] or not key[2] or key in seen_edges:
            continue
        seen_edges.add(key)
        deduped_edges.append({"from": key[0], "to": key[2], "kind": key[1].lower()})

    result: dict[str, Any] = {
        "project": str(project_root),
        "project_slug": project_slug,
        "stack": stack,
        "files_analyzed": len(files),
        "nodes": len(deduped_nodes),
        "edges": len(deduped_edges),
        "graph": {
            "nodes": deduped_nodes[:2000],   # cap for JSON output
            "edges": deduped_edges[:5000],
        },
    }

    if args.neo4j:
        result["neo4j"] = sync_to_neo4j(
            deduped_nodes, deduped_edges,
            args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_db,
            project_slug,
        )

    if args.save_memory_nodes:
        written = save_memory_nodes(deduped_nodes, project_root, stack)
        result["memory_nodes_written"] = written

    # Output
    output_json = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        # Print summary only
        summary = {k: v for k, v in result.items() if k != "graph"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(output_json)


if __name__ == "__main__":
    main()

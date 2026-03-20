#!/usr/bin/env python3
"""
Generate a normalized dependency graph artifact for impact analysis.

Default output:
  analysis/dependency_graph.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".php",
    ".java",
    ".cs",
    ".rb",
    ".rs",
}

IMPORT_PATTERNS = (
    re.compile(r"^\s*import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    re.compile(r"^\s*from\s+([A-Za-z0-9_./-]+)\s+import\s+", re.MULTILINE),
    re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"^\s*use\s+([A-Za-z0-9_\\]+)", re.MULTILINE),
    re.compile(r"^\s*include(?:_once)?\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
)

IGNORE_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    "workspace",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dependency graph artifact.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root containing source and ai-orchestrator/state/project-state.json",
    )
    parser.add_argument(
        "--output",
        default="analysis/dependency_graph.json",
        help="Output JSON file path (relative to project root when not absolute).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=4000,
        help="Maximum number of code files to scan.",
    )
    return parser.parse_args()


def iter_code_files(project_root: Path, max_files: int) -> Iterable[Path]:
    count = 0
    for path in project_root.rglob("*"):
        if count >= max_files:
            break
        try:
            is_file = path.is_file()
        except OSError:
            # Some Docker socket/device artifacts on Windows mounts can throw
            # access errors during stat; skip them instead of breaking the scan.
            continue
        if not is_file:
            continue
        # Evaluate ignore directories relative to project root to avoid
        # accidentally excluding every file when project root path itself
        # contains names like "workspace".
        try:
            rel_parts = path.relative_to(project_root).parts
        except ValueError:
            rel_parts = path.parts
        if any(part in IGNORE_DIRS for part in rel_parts):
            continue
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        count += 1
        yield path


def detect_module_name(project_root: Path, file_path: Path) -> str:
    relative = file_path.relative_to(project_root)
    if len(relative.parts) == 1:
        return "root"
    first = relative.parts[0]
    if first in {"src", "app", "lib", "services", "modules"} and len(relative.parts) > 1:
        return relative.parts[1]
    return first


def normalize_import(raw: str) -> str:
    value = raw.strip()
    value = value.replace("\\", "/")
    value = value.lstrip("./")
    return value


def import_to_module(import_value: str) -> str:
    if not import_value:
        return ""
    if import_value.startswith("@"):
        import_value = import_value[1:]
    return import_value.split("/", 1)[0].split(".", 1)[0]


def extract_edges(project_root: Path, max_files: int) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    modules: Set[str] = set()
    edges: Set[Tuple[str, str]] = set()

    for file_path in iter_code_files(project_root, max_files):
        source_module = detect_module_name(project_root, file_path)
        modules.add(source_module)
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for pattern in IMPORT_PATTERNS:
            for match in pattern.findall(text):
                value = normalize_import(match)
                target_module = import_to_module(value)
                if not target_module:
                    continue
                if target_module in {"http", "https"}:
                    continue
                if target_module == source_module:
                    continue
                modules.add(target_module)
                edges.add((source_module, target_module))

    return modules, edges


def load_state_edges(project_root: Path) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    modules: Set[str] = set()
    edges: Set[Tuple[str, str]] = set()
    state_path = project_root / "ai-orchestrator" / "state" / "project-state.json"
    if not state_path.exists():
        return modules, edges

    try:
        state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return modules, edges

    analysis = state.get("analysis", {})
    dep = analysis.get("dependency_graph", {})
    for module in dep.get("modules", []):
        if isinstance(module, str) and module.strip():
            modules.add(module.strip())

    for edge in dep.get("edges", []):
        if isinstance(edge, dict):
            source = str(edge.get("from", "")).strip()
            target = str(edge.get("to", "")).strip()
        elif isinstance(edge, (list, tuple)) and len(edge) == 2:
            source = str(edge[0]).strip()
            target = str(edge[1]).strip()
        else:
            continue
        if source and target and source != target:
            edges.add((source, target))

    return modules, edges


def to_sorted_pairs(edges: Set[Tuple[str, str]]) -> List[List[str]]:
    return [[a, b] for a, b in sorted(edges, key=lambda item: (item[0], item[1]))]


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        raise SystemExit(f"project root not found: {project_root}")

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scanned_modules, scanned_edges = extract_edges(project_root, args.max_files)
    state_modules, state_edges = load_state_edges(project_root)

    modules = sorted(scanned_modules.union(state_modules))
    edges = scanned_edges.union(state_edges)

    payload: Dict[str, object] = {
        "generated_at": now_iso(),
        "project_root": str(project_root),
        "services": modules,
        "dependencies": to_sorted_pairs(edges),
        "edge_count": len(edges),
        "sources": {
            "scanner_modules": len(scanned_modules),
            "scanner_edges": len(scanned_edges),
            "state_modules": len(state_modules),
            "state_edges": len(state_edges),
        },
    }

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Dependency graph written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

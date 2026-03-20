#!/usr/bin/env python3
"""Semantic dependency scanner for Universal Intake V2.

Local-first, stdlib-only implementation:
- Python: real AST parsing (imports, functions, classes, call sites)
- Other languages: adapter-based semantic regex parsing
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    ".next",
    "out",
    "target",
    "bin",
    "obj",
    ".venv",
    "venv",
    "__pycache__",
    "workspace",
    "ai-orchestrator",
}

LANG_BY_EXT = {
    ".py": "python",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".ts": "node",
    ".tsx": "node",
    ".js": "node",
    ".jsx": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".php": "php",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".rs": "rust",
    ".rb": "ruby",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".h": "cpp",
    ".swift": "swift",
}


def to_unix(path: Path) -> str:
    return path.as_posix()


def top_level_module(relative_path: str) -> str:
    parts = relative_path.split("/")
    if len(parts) > 1:
        return parts[0]
    return "root"


def collect_code_files(project_root: Path, max_files: int) -> List[Tuple[Path, str, str]]:
    files: List[Tuple[Path, str, str]] = []
    for root, dirs, filenames in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".cache")]
        root_path = Path(root)
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            language = LANG_BY_EXT.get(ext)
            if not language:
                continue
            abs_path = root_path / filename
            rel_path = to_unix(abs_path.relative_to(project_root))
            files.append((abs_path, rel_path, language))
            if len(files) >= max_files:
                return files
    return files


def parse_python(content: str) -> Dict[str, List[str]]:
    tree = ast.parse(content)
    imports: Set[str] = set()
    functions: Set[str] = set()
    classes: Set[str] = set()
    calls: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name:
                    imports.add(item.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and module:
                imports.add("." * node.level + module)
            elif node.level and not module:
                imports.add("." * node.level)
            elif module:
                imports.add(module)
        elif isinstance(node, ast.FunctionDef):
            functions.add(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                calls.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                calls.add(fn.attr)

    return {
        "imports": sorted(imports),
        "functions": sorted(functions),
        "classes": sorted(classes),
        "calls": sorted(calls),
    }


def regex_collect(patterns: Iterable[str], content: str, group: int = 1) -> List[str]:
    out: Set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            value = match.group(group).strip()
            if value:
                out.add(value)
    return sorted(out)


def parse_node(content: str) -> Dict[str, List[str]]:
    imports = regex_collect(
        [
            r'^\s*import\s+.+?\s+from\s+[\'"]([^\'"]+)[\'"]',
            r'^\s*import\s+[\'"]([^\'"]+)[\'"]',
            r'require\([\'"]([^\'"]+)[\'"]\)',
            r'from\s+[\'"]([^\'"]+)[\'"]',
        ],
        content,
    )
    functions = regex_collect([r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)", r"^\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("], content)
    classes = regex_collect([r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_php(content: str) -> Dict[str, List[str]]:
    imports = regex_collect(
        [
            r"^\s*use\s+([A-Za-z0-9_\\]+)\s*;",
            r"^\s*(?:require|require_once|include|include_once)\s*\(?\s*[\"']([^\"']+)[\"']\s*\)?\s*;",
        ],
        content,
    )
    functions = regex_collect([r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    classes = regex_collect([r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_go(content: str) -> Dict[str, List[str]]:
    imports: Set[str] = set()
    # import "x/y"
    imports.update(regex_collect([r'^\s*import\s+"([^"]+)"'], content))
    # import ( "x/y" ... )
    block = re.search(r"(?s)import\s*\((.*?)\)", content)
    if block:
        imports.update(regex_collect([r'"([^"]+)"'], block.group(1)))
    functions = regex_collect([r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    classes: List[str] = []
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": sorted(imports), "functions": functions, "classes": classes, "calls": calls}


def parse_java_family(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r"^\s*import\s+([A-Za-z0-9_.*]+)\s*;"], content)
    functions = regex_collect([r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[A-Za-z0-9_<>\[\]]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    classes = regex_collect([r"^\s*(?:public|private|protected)?\s*(?:class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_csharp(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r"^\s*using\s+([A-Za-z0-9_.]+)\s*;"], content)
    functions = regex_collect([r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?[A-Za-z0-9_<>\[\]]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    classes = regex_collect([r"^\s*(?:public|private|protected|internal)?\s*(?:class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_rust(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r"^\s*use\s+([^;]+);"], content)
    functions = regex_collect([r"^\s*fn\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    classes: List[str] = []
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*!\s*\(|([A-Za-z_][A-Za-z0-9_]*)\s*\("], content, group=1)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_ruby(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r"^\s*require(?:_relative)?\s+[\"']([^\"']+)[\"']"], content)
    functions = regex_collect([r"^\s*def\s+([A-Za-z_][A-Za-z0-9_!?=]*)"], content)
    classes = regex_collect([r"^\s*class\s+([A-Za-z_][A-Za-z0-9_:]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_!?=]*)\s*(?:\(|\s)"], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_cpp_like(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r'^\s*#include\s+[<"]([^>"]+)[>"]'], content)
    functions = regex_collect([r"^\s*[A-Za-z_][A-Za-z0-9_:<>\*&\s]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    classes = regex_collect([r"^\s*(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_swift(content: str) -> Dict[str, List[str]]:
    imports = regex_collect([r"^\s*import\s+([A-Za-z0-9_.]+)"], content)
    functions = regex_collect([r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    classes = regex_collect([r"^\s*(?:class|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"([A-Za-z_][A-Za-z0-9_]*)\s*\("], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_powershell(content: str) -> Dict[str, List[str]]:
    imports = regex_collect(
        [
            r"^\s*Import-Module\s+['\"]?([A-Za-z0-9_./\\-]+)",
            r"^\s*\.\s+['\"]?([A-Za-z0-9_./\\-]+)",
        ],
        content,
    )
    functions = regex_collect([r"^\s*function\s+([A-Za-z_][A-Za-z0-9_-]*)"], content)
    classes = regex_collect([r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)"], content)
    calls = regex_collect([r"\b([A-Za-z_][A-Za-z0-9_-]*)\s*(?:-|\()"], content)
    return {"imports": imports, "functions": functions, "classes": classes, "calls": calls}


def parse_by_language(language: str, content: str) -> Dict[str, List[str]]:
    if language == "python":
        return parse_python(content)
    if language == "node":
        return parse_node(content)
    if language == "php":
        return parse_php(content)
    if language == "go":
        return parse_go(content)
    if language in {"java", "kotlin"}:
        return parse_java_family(content)
    if language == "csharp":
        return parse_csharp(content)
    if language == "rust":
        return parse_rust(content)
    if language == "ruby":
        return parse_ruby(content)
    if language == "cpp":
        return parse_cpp_like(content)
    if language == "swift":
        return parse_swift(content)
    if language == "powershell":
        return parse_powershell(content)
    return {"imports": [], "functions": [], "classes": [], "calls": []}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def resolve_import_to_module(import_path: str, current_rel: str, module_set: Set[str]) -> Optional[str]:
    candidate = import_path.strip()
    if not candidate:
        return None

    if candidate.startswith("."):
        current_dir = Path(current_rel).parent
        try:
            target = (current_dir / candidate).resolve().as_posix()
        except Exception:
            target = (current_dir / candidate).as_posix()
        # Keep relative semantics; avoid absolute path mismatch
        rel = os.path.normpath((current_dir / candidate).as_posix()).replace("\\", "/")
        if rel.startswith("../"):
            return None
        module = top_level_module(rel)
        if module in module_set:
            return module
        return None

    normalized = candidate.replace("\\", "/")
    token = ""
    if normalized.startswith("@"):
        parts = normalized.split("/")
        token = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
    else:
        token = re.split(r"[/.]", normalized)[0]

    if token in module_set:
        return token

    first_segment = normalized.split("/")[0]
    if first_segment in module_set:
        return first_segment

    if normalized in module_set:
        return normalized

    dotted_root = normalized.split(".")[0]
    if dotted_root in module_set:
        return dotted_root

    return None


def detect_cycle(modules: List[str], edges: List[Tuple[str, str]]) -> bool:
    adjacency: Dict[str, List[str]] = {module: [] for module in modules}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    visited: Set[str] = set()
    stack: Set[str] = set()

    def visit(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for neighbor in adjacency.get(node, []):
            if visit(neighbor):
                return True
        stack.remove(node)
        return False

    return any(visit(module) for module in modules)


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic/AST dependency scanner for Intake V2.")
    parser.add_argument("--project-root", required=True, help="Repository root path")
    parser.add_argument("--max-files", type=int, default=6000, help="Maximum code files to scan")
    parser.add_argument("--max-errors", type=int, default=120, help="Maximum parse errors in output")
    parser.add_argument("--max-entities", type=int, default=200, help="Maximum function/class entities in output")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"project root not found: {project_root}")

    code_files = collect_code_files(project_root, args.max_files)
    modules = sorted({top_level_module(rel) for _, rel, _ in code_files})
    module_set = set(modules)

    module_edges_set: Set[Tuple[str, str]] = set()
    file_edges: List[Dict[str, str]] = []
    parse_errors: List[Dict[str, str]] = []
    adapters_used: Set[str] = set()
    ast_supported = {"python"}

    function_entities: List[Dict[str, str]] = []
    class_entities: List[Dict[str, str]] = []
    call_edges_count = 0

    for abs_path, rel_path, language in code_files:
        try:
            content = read_text(abs_path)
            parsed = parse_by_language(language, content)
            adapters_used.add(language)
        except Exception as exc:
            parse_errors.append({"file": rel_path, "language": language, "error": str(exc)})
            continue

        imports = parsed.get("imports", [])
        functions = parsed.get("functions", [])
        classes = parsed.get("classes", [])
        calls = parsed.get("calls", [])
        call_edges_count += len(calls)

        for fn in functions:
            if len(function_entities) < args.max_entities:
                function_entities.append({"file": rel_path, "language": language, "name": fn})
        for cls in classes:
            if len(class_entities) < args.max_entities:
                class_entities.append({"file": rel_path, "language": language, "name": cls})

        source_module = top_level_module(rel_path)
        unique_imports = sorted(set(imports))
        for candidate in unique_imports:
            target_module = resolve_import_to_module(candidate, rel_path, module_set)
            if not target_module or target_module == source_module:
                continue
            edge = (source_module, target_module)
            if edge not in module_edges_set:
                module_edges_set.add(edge)
            if len(file_edges) < 1000:
                file_edges.append(
                    {
                        "source_file": rel_path,
                        "source_module": source_module,
                        "target_module": target_module,
                        "import": candidate,
                    }
                )

    module_edges = sorted(module_edges_set)
    cycle_detected = detect_cycle(modules, module_edges)

    inbound_counts: Dict[str, int] = defaultdict(int)
    outbound_counts: Dict[str, int] = defaultdict(int)
    for source, target in module_edges:
        outbound_counts[source] += 1
        inbound_counts[target] += 1

    independent = sorted(
        [
            module
            for module in modules
            if outbound_counts.get(module, 0) == 0 and inbound_counts.get(module, 0) == 0
        ]
    )

    result = {
        "engine": "semantic-v1",
        "enabled": True,
        "summary": {
            "files_scanned": len(code_files),
            "modules_detected": len(modules),
            "edge_count": len(module_edges),
            "parse_errors": min(len(parse_errors), args.max_errors),
            "ast_supported_languages": sorted(ast_supported),
            "adapters_used": sorted(adapters_used),
            "function_count": len(function_entities),
            "class_count": len(class_entities),
            "call_sites_detected": call_edges_count,
        },
        "modules": modules,
        "edges": [{"source": source, "target": target} for source, target in module_edges[:1200]],
        "edge_count": len(module_edges),
        "cycle_detected": cycle_detected,
        "independent_modules": independent,
        "entities": {"functions": function_entities, "classes": class_entities},
        "file_imports": file_edges,
        "errors": parse_errors[: args.max_errors],
    }

    print(json.dumps(result))


if __name__ == "__main__":
    main()

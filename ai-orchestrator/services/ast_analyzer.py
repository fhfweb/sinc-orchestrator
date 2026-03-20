from services.streaming.core.config import env_get
"""
AST Analyzer
============
Parses a project's source files and builds a knowledge graph in Neo4j.

Parser strategy
---------------
  1. Tree-sitter (primary) — exact line numbers, real cyclomatic complexity,
     nested functions, JSX/TSX support. Requires: tree-sitter + language packages.
  2. Regex fallback — zero deps, always available.

Supported languages:
  PHP (regex only), Python, JavaScript/TypeScript, Go (tree-sitter + regex)

Cyclomatic complexity (Tree-sitter only)
  Counts branch nodes: if, elif, for, while, with, except, case, ternary, and/or.
  Stored on Function nodes as `complexity` property. Feeds the entropy model.

Output: Neo4j nodes (File, Class, Function, Method) + edges

Usage:
    from services.ast_analyzer import ASTAnalyzer
    analyzer = ASTAnalyzer(neo4j_uri="bolt://localhost:7687", neo4j_auth=("neo4j", "..."))
    result = analyzer.analyze_project("/path/to/project", project_id="sinc", tenant_id="local")
    print(result)  # {"files": 142, "nodes": 890, "edges": 2340, "complexity_total": 1203}
"""

import os
import re
import time
from pathlib import Path
from typing import Generator


# ──────────────────────────────────────────────
# NEO4J CONNECTION
# ──────────────────────────────────────────────

NEO4J_URI  = env_get("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = env_get("NEO4J_USER", default="neo4j")
NEO4J_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]


# ──────────────────────────────────────────────
# FILE DISCOVERY
# ──────────────────────────────────────────────

_LANG_EXTENSIONS = {
    ".php": "php",
    ".py":  "python",
    ".js":  "javascript",
    ".ts":  "typescript",
    ".go":  "go",
}

_SKIP_DIRS = {
    "vendor", "node_modules", ".git", "storage", "bootstrap/cache",
    "__pycache__", ".venv", "venv", "dist", "build", ".next",
}


def _walk_source_files(root: str) -> Generator[tuple[str, str], None, None]:
    """Yield (abs_path, language) for each source file."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune directories in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not d.startswith(".")]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            lang = _LANG_EXTENSIONS.get(ext)
            if lang:
                yield os.path.join(dirpath, fname), lang


# ──────────────────────────────────────────────
# LANGUAGE PARSERS (regex-based, zero deps)
# ──────────────────────────────────────────────

def _parse_php(content: str, _rel_path: str) -> dict:
    """Extract symbols from a PHP file."""
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": []}

    # Namespace
    ns_match = re.search(r"^\s*namespace\s+([\w\\]+)\s*;", content, re.MULTILINE)
    namespace = ns_match.group(1) if ns_match else ""

    # Use statements (imports)
    for m in re.finditer(r"^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;", content, re.MULTILINE):
        symbols["imports"].append(m.group(1))

    # Classes / interfaces / traits
    for m in re.finditer(
        r"(?:abstract\s+)?(?:class|interface|trait)\s+(\w+)"
        r"(?:\s+extends\s+([\w\\]+))?"
        r"(?:\s+implements\s+([\w\\,\s]+))?",
        content, re.MULTILINE
    ):
        cls_name  = m.group(1)
        extends   = (m.group(2) or "").strip()
        implements = [i.strip() for i in (m.group(3) or "").split(",") if i.strip()]
        symbols["classes"].append({
            "name":       cls_name,
            "fqn":        f"{namespace}\\{cls_name}" if namespace else cls_name,
            "extends":    extends,
            "implements": implements,
            "line":       content[:m.start()].count("\n") + 1,
        })

    # Methods (public/protected/private function)
    for m in re.finditer(
        r"(?:public|protected|private|static)(?:\s+(?:public|protected|private|static))*"
        r"\s+function\s+(\w+)\s*\(",
        content, re.MULTILINE
    ):
        symbols["functions"].append({
            "name": m.group(1),
            "line": content[:m.start()].count("\n") + 1,
            "type": "method",
        })

    # Top-level functions
    for m in re.finditer(r"^function\s+(\w+)\s*\(", content, re.MULTILINE):
        symbols["functions"].append({
            "name": m.group(1),
            "line": content[:m.start()].count("\n") + 1,
            "type": "function",
        })

    # Method calls: $this->method( or ClassName::method(
    for m in re.finditer(r"(?:\$this->|self::)(\w+)\s*\(", content):
        symbols["calls"].append(m.group(1))

    return symbols


def _parse_python(content: str, _rel_path: str) -> dict:
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": []}

    for m in re.finditer(r"^class\s+(\w+)(?:\(([^)]*)\))?", content, re.MULTILINE):
        bases = [b.strip() for b in (m.group(2) or "").split(",") if b.strip()]
        symbols["classes"].append({
            "name":    m.group(1),
            "extends": bases[0] if bases else "",
            "line":    content[:m.start()].count("\n") + 1,
        })

    for m in re.finditer(r"^def\s+(\w+)\s*\(", content, re.MULTILINE):
        symbols["functions"].append({
            "name": m.group(1),
            "line": content[:m.start()].count("\n") + 1,
            "type": "function",
        })

    for m in re.finditer(r"^(?:from\s+([\w.]+)\s+)?import\s+([\w,\s*]+)", content, re.MULTILINE):
        module = m.group(1) or m.group(2).strip()
        symbols["imports"].append(module)

    return symbols


def _parse_js_ts(content: str, _rel_path: str) -> dict:
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": []}

    for m in re.finditer(r"^(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?",
                         content, re.MULTILINE):
        symbols["classes"].append({
            "name":    m.group(1),
            "extends": m.group(2) or "",
            "line":    content[:m.start()].count("\n") + 1,
        })

    for m in re.finditer(
        r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*\("
        r"|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?",
        content, re.MULTILINE
    ):
        name = m.group(1) or m.group(2)
        if name:
            symbols["functions"].append({
                "name": name,
                "line": content[:m.start()].count("\n") + 1,
                "type": "function",
            })

    for m in re.finditer(r"^import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", content, re.MULTILINE):
        symbols["imports"].append(m.group(1))

    return symbols


def _parse_go(content: str, _rel_path: str) -> dict:
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": []}

    for m in re.finditer(r"^func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(",
                         content, re.MULTILINE):
        receiver = m.group(1) or ""
        name     = m.group(2)
        symbols["functions"].append({
            "name":    name,
            "line":    content[:m.start()].count("\n") + 1,
            "type":    "method" if receiver else "function",
            "receiver": receiver,
        })

    for m in re.finditer(r"^type\s+(\w+)\s+struct", content, re.MULTILINE):
        symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})

    return symbols


# ──────────────────────────────────────────────
# TREE-SITTER LAYER  (primary; falls back to regex if not installed)
# ──────────────────────────────────────────────

_HAS_TS = False
_TS_PARSERS: dict = {}   # lang → tree_sitter.Parser

# Branch node types that increment cyclomatic complexity (+1 each)
_CC_NODES = frozenset({
    # Python
    "if_statement", "elif_clause", "for_statement", "while_statement",
    "with_statement", "except_clause", "assert_statement",
    "conditional_expression",          # ternary x if cond else y
    "boolean_operator",                # and / or
    # JS / TS
    "if_statement", "else_clause", "for_statement", "for_in_statement",
    "while_statement", "do_statement", "switch_case", "catch_clause",
    "ternary_expression", "logical_expression",
    # Go
    "if_statement", "for_statement", "case_clause", "type_switch_statement",
    "select_statement",
})


def _count_complexity(node) -> int:
    """Recursively count branch nodes for cyclomatic complexity."""
    count = 1 if node.type in _CC_NODES else 0
    for child in node.children:
        count += _count_complexity(child)
    return count


def _ts_init():
    """Lazy Tree-sitter initialisation. Called once on first parse."""
    global _HAS_TS, _TS_PARSERS
    if _HAS_TS or _TS_PARSERS:
        return
    try:
        from tree_sitter import Parser as _TSParser, Language as _TSLanguage

        def _try_lang(pkg_name: str, lang_fn_name: str):
            try:
                pkg = __import__(pkg_name)
                lang_fn = getattr(pkg, lang_fn_name, None) or getattr(pkg, "language", None)
                return _TSLanguage(lang_fn())
            except Exception:
                return None

        langs = {
            "python":     _try_lang("tree_sitter_python",     "language"),
            "javascript": _try_lang("tree_sitter_javascript", "language"),
            "typescript": _try_lang("tree_sitter_typescript", "language_typescript"),
            "go":         _try_lang("tree_sitter_go",         "language"),
        }
        for lang, lang_obj in langs.items():
            if lang_obj:
                p = _TSParser(lang_obj)
                _TS_PARSERS[lang] = p

        _HAS_TS = bool(_TS_PARSERS)
        if _HAS_TS:
            print(f"[ast-analyzer] tree-sitter active for: {', '.join(_TS_PARSERS)}")
    except ImportError:
        pass  # tree-sitter not installed — regex fallback will be used


def _ts_query_nodes(node, *types) -> list:
    """Collect all descendant nodes matching any of the given types."""
    results = []
    if node.type in types:
        results.append(node)
    for child in node.children:
        results.extend(_ts_query_nodes(child, *types))
    return results


def _parse_python_ts(content: str, rel_path: str) -> dict:
    """Tree-sitter Python parser with cyclomatic complexity."""
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": [],
                     "complexity_total": 0}
    parser = _TS_PARSERS.get("python")
    if not parser:
        return _parse_python(content, rel_path)
    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        root = tree.root_node

        # Classes
        for node in _ts_query_nodes(root, "class_definition"):
            name_node = node.child_by_field_name("name")
            bases_node = node.child_by_field_name("superclasses")
            if not name_node:
                continue
            extends = ""
            if bases_node:
                for child in bases_node.children:
                    if child.type == "identifier":
                        extends = child.text.decode("utf-8", errors="replace")
                        break
            symbols["classes"].append({
                "name":    name_node.text.decode("utf-8", errors="replace"),
                "extends": extends,
                "line":    node.start_point[0] + 1,
            })

        # Functions / methods
        for node in _ts_query_nodes(root, "function_definition"):
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            if not name_node:
                continue
            cc = _count_complexity(body_node) if body_node else 1
            symbols["functions"].append({
                "name":       name_node.text.decode("utf-8", errors="replace"),
                "line":       node.start_point[0] + 1,
                "type":       "function",
                "complexity": cc,
            })
            symbols["complexity_total"] += cc

        # Imports
        for node in _ts_query_nodes(root, "import_statement", "import_from_statement"):
            for child in node.children:
                if child.type in ("dotted_name", "relative_import"):
                    symbols["imports"].append(
                        child.text.decode("utf-8", errors="replace"))
                    break

        # Calls
        for node in _ts_query_nodes(root, "call"):
            fn_node = node.child_by_field_name("function")
            if fn_node:
                symbols["calls"].append(fn_node.text.decode("utf-8", errors="replace"))

    except Exception as exc:
        print(f"[ast-analyzer] tree-sitter python error for {rel_path}: {exc}")
        return _parse_python(content, rel_path)
    return symbols


def _parse_js_ts_ts(content: str, rel_path: str, lang: str = "javascript") -> dict:
    """Tree-sitter JavaScript/TypeScript parser with cyclomatic complexity."""
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": [],
                     "complexity_total": 0}
    parser = _TS_PARSERS.get(lang) or _TS_PARSERS.get("javascript")
    if not parser:
        return _parse_js_ts(content, rel_path)
    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        root = tree.root_node

        # Classes
        for node in _ts_query_nodes(root, "class_declaration", "class_expression"):
            name_node = node.child_by_field_name("name")
            heritage  = node.child_by_field_name("heritage")
            extends   = ""
            if heritage:
                for child in heritage.children:
                    if child.type == "identifier":
                        extends = child.text.decode("utf-8", errors="replace")
                        break
            if name_node:
                symbols["classes"].append({
                    "name":    name_node.text.decode("utf-8", errors="replace"),
                    "extends": extends,
                    "line":    node.start_point[0] + 1,
                })

        # Functions
        fn_types = ("function_declaration", "function_expression",
                    "arrow_function", "method_definition")
        for node in _ts_query_nodes(root, *fn_types):
            name_node = node.child_by_field_name("name")
            body_node = node.child_by_field_name("body")
            name = (name_node.text.decode("utf-8", errors="replace")
                    if name_node else "<anonymous>")
            cc = _count_complexity(body_node) if body_node else 1
            symbols["functions"].append({
                "name":       name,
                "line":       node.start_point[0] + 1,
                "type":       "method" if node.type == "method_definition" else "function",
                "complexity": cc,
            })
            symbols["complexity_total"] += cc

        # Imports
        for node in _ts_query_nodes(root, "import_statement"):
            for child in node.children:
                if child.type == "string":
                    raw = child.text.decode("utf-8", errors="replace").strip("'\"")
                    symbols["imports"].append(raw)
                    break

        # Calls
        for node in _ts_query_nodes(root, "call_expression"):
            fn_node = node.child_by_field_name("function")
            if fn_node:
                symbols["calls"].append(
                    fn_node.text.decode("utf-8", errors="replace"))

    except Exception as exc:
        print(f"[ast-analyzer] tree-sitter js/ts error for {rel_path}: {exc}")
        return _parse_js_ts(content, rel_path)
    return symbols


def _parse_go_ts(content: str, rel_path: str) -> dict:
    """Tree-sitter Go parser with cyclomatic complexity."""
    symbols: dict = {"classes": [], "functions": [], "imports": [], "calls": [],
                     "complexity_total": 0}
    parser = _TS_PARSERS.get("go")
    if not parser:
        return _parse_go(content, rel_path)
    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        root = tree.root_node

        # Structs (as classes)
        for node in _ts_query_nodes(root, "type_spec"):
            name_node = node.child_by_field_name("name")
            type_node = node.child_by_field_name("type")
            if name_node and type_node and type_node.type == "struct_type":
                symbols["classes"].append({
                    "name": name_node.text.decode("utf-8", errors="replace"),
                    "line": node.start_point[0] + 1,
                })

        # Functions + methods
        for node in _ts_query_nodes(root, "function_declaration", "method_declaration"):
            name_node = node.child_by_field_name("name")
            recv_node = node.child_by_field_name("receiver")
            body_node = node.child_by_field_name("body")
            if not name_node:
                continue
            receiver = ""
            if recv_node:
                for child in _ts_query_nodes(recv_node, "type_identifier"):
                    receiver = child.text.decode("utf-8", errors="replace"); break
            cc = _count_complexity(body_node) if body_node else 1
            symbols["functions"].append({
                "name":       name_node.text.decode("utf-8", errors="replace"),
                "line":       node.start_point[0] + 1,
                "type":       "method" if receiver else "function",
                "receiver":   receiver,
                "complexity": cc,
            })
            symbols["complexity_total"] += cc

        # Imports
        for node in _ts_query_nodes(root, "import_spec"):
            path_node = node.child_by_field_name("path")
            if path_node:
                raw = path_node.text.decode("utf-8", errors="replace").strip('"')
                symbols["imports"].append(raw)

        # Calls
        for node in _ts_query_nodes(root, "call_expression"):
            fn_node = node.child_by_field_name("function")
            if fn_node:
                symbols["calls"].append(
                    fn_node.text.decode("utf-8", errors="replace"))

    except Exception as exc:
        print(f"[ast-analyzer] tree-sitter go error for {rel_path}: {exc}")
        return _parse_go(content, rel_path)
    return symbols


def _get_parser(lang: str):
    """Return the best available parser for a language."""
    _ts_init()
    if _HAS_TS:
        if lang == "python":
            return _parse_python_ts
        if lang in ("javascript", "typescript"):
            return lambda content, rel: _parse_js_ts_ts(content, rel, lang)
        if lang == "go":
            return _parse_go_ts
    # regex fallback
    return _PARSERS.get(lang)


_PARSERS = {
    "php":        _parse_php,
    "python":     _parse_python,
    "javascript": _parse_js_ts,
    "typescript": _parse_js_ts,
    "go":         _parse_go,
}


# ──────────────────────────────────────────────
# NEO4J UPSERT
# ──────────────────────────────────────────────

def _upsert_file_graph(tx, rel_path: str, lang: str, symbols: dict,
                       project_id: str, tenant_id: str):
    """Write nodes and edges for one file inside a Neo4j write transaction."""

    # File node
    tx.run("""
        MERGE (f:File {path: $path, project_id: $pid, tenant_id: $tid})
        SET f.language = $lang, f.updated_at = $ts
    """, path=rel_path, pid=project_id, tid=tenant_id, lang=lang,
         ts=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Class nodes + EXTENDS / IMPLEMENTS
    for cls in symbols.get("classes", []):
        tx.run("""
            MERGE (c:Class {fqn: $fqn, project_id: $pid, tenant_id: $tid})
            SET c.name = $name, c.file = $path, c.line = $line
            WITH c
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:DEFINES]->(c)
        """, fqn=cls.get("fqn", cls["name"]), name=cls["name"],
             path=rel_path, line=cls.get("line", 0),
             pid=project_id, tid=tenant_id)

        if cls.get("extends"):
            tx.run("""
                MATCH (c:Class {name: $child, project_id: $pid, tenant_id: $tid})
                MERGE (p:Class {name: $parent, project_id: $pid, tenant_id: $tid})
                MERGE (c)-[:EXTENDS]->(p)
            """, child=cls["name"], parent=cls["extends"],
                 pid=project_id, tid=tenant_id)

        for iface in cls.get("implements", []):
            if iface:
                tx.run("""
                    MATCH (c:Class {name: $child, project_id: $pid, tenant_id: $tid})
                    MERGE (i:Class {name: $iface, project_id: $pid, tenant_id: $tid})
                    MERGE (c)-[:IMPLEMENTS]->(i)
                """, child=cls["name"], iface=iface,
                     pid=project_id, tid=tenant_id)

    # Function / Method nodes  (complexity stored when available from tree-sitter)
    for fn in symbols.get("functions", []):
        tx.run("""
            MERGE (m:Function {name: $name, file: $path, project_id: $pid, tenant_id: $tid})
            SET m.line = $line, m.type = $type, m.complexity = $cc
            WITH m
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:DEFINES]->(m)
        """, name=fn["name"], path=rel_path, line=fn.get("line", 0),
             type=fn.get("type", "function"), cc=fn.get("complexity", 1),
             pid=project_id, tid=tenant_id)

    # Import edges
    for imp in symbols.get("imports", []):
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (t:Module {name: $mod, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:IMPORTS]->(t)
        """, path=rel_path, mod=imp, pid=project_id, tid=tenant_id)


# ──────────────────────────────────────────────
# MAIN ANALYZER CLASS
# ──────────────────────────────────────────────

class ASTAnalyzer:
    def __init__(self,
                 neo4j_uri: str = NEO4J_URI,
                 neo4j_auth: tuple = (NEO4J_USER, NEO4J_PASS)):
        self.neo4j_uri  = neo4j_uri
        self.neo4j_auth = neo4j_auth
        self._driver    = None

    def _get_driver(self):
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
                self._driver = GraphDatabase.driver(self.neo4j_uri, auth=self.neo4j_auth)
            except ImportError:
                pass
        return self._driver

    def analyze_project(
        self,
        project_path: str,
        project_id: str = "",
        tenant_id: str = "",
        on_progress=None,   # optional callback(file_path, symbols)
    ) -> dict:
        """
        Walk project_path, parse every source file, upsert to Neo4j.
        Returns summary dict: {files, nodes_created, edges_created, errors}
        """
        stats = {"files": 0, "nodes_created": 0, "edges_created": 0, "errors": 0}
        driver = self._get_driver()

        for abs_path, lang in _walk_source_files(project_path):
            rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
            try:
                content  = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                parser   = _get_parser(lang)
                if not parser:
                    continue
                symbols  = parser(content, rel_path)
                stats["files"] += 1
                stats.setdefault("complexity_total", 0)
                stats["complexity_total"] += symbols.get("complexity_total", 0)

                n_nodes = (len(symbols.get("classes", [])) +
                           len(symbols.get("functions", [])) + 1)  # +1 for file node
                n_edges = (len(symbols.get("imports", [])) +
                           len(symbols.get("calls", [])))

                if driver:
                    with driver.session() as session:
                        session.execute_write(
                            _upsert_file_graph,
                            rel_path, lang, symbols, project_id, tenant_id
                        )

                stats["nodes_created"] += n_nodes
                stats["edges_created"] += n_edges

                if on_progress:
                    on_progress(rel_path, symbols)

            except Exception as exc:
                stats["errors"] += 1
                print(f"[ast-analyzer] error parsing {rel_path}: {exc}")

        return stats

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json

    if len(sys.argv) < 2:
        print("Usage: python ast_analyzer.py <project_path> [project_id] [tenant_id]")
        sys.exit(1)

    project_path = sys.argv[1]
    project_id   = sys.argv[2] if len(sys.argv) > 2 else "default"
    tenant_id    = sys.argv[3] if len(sys.argv) > 3 else "local"

    print(f"[ast-analyzer] Analyzing {project_path}  project={project_id} tenant={tenant_id}")

    with ASTAnalyzer() as analyzer:
        result = analyzer.analyze_project(
            project_path, project_id, tenant_id,
            on_progress=lambda p, s: print(
                f"  {p}: {len(s.get('classes',[]))} classes, "
                f"{len(s.get('functions',[]))} fns"
            ),
        )

    print(json.dumps(result, indent=2))

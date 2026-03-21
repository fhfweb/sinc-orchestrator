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
from typing import Generator, Dict, List, Any
from services.semantic_resolver import SymbolTable, SymbolDefinition, SuffixIndex, ResolutionContext, TypeUniverse
from services.parsing.ts_utils import ts_init


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
    ".yaml": "infra",
    ".yml":  "infra",
    "dockerfile": "infra",
    ".java": "enterprise",
    ".cs":   "enterprise",
    ".cpp":  "enterprise",
    ".hpp":  "enterprise",
    ".dart": "enterprise",
    ".kt":   "enterprise",
    ".swift": "enterprise",
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
            lang = _LANG_EXTENSIONS.get(ext) or _LANG_EXTENSIONS.get(fname.lower())
            if lang:
                yield os.path.join(dirpath, fname), lang


# ──────────────────────────────────────────────
# LANGUAGE DRIVERS (Plugin Architecture)
# ──────────────────────────────────────────────

from services.parsing.python_driver import PythonParser
from services.parsing.js_driver import JSParser
from services.parsing.go_driver import GoParser
from services.parsing.php_driver import PHPParser
from services.parsing.infra_driver import InfraParser
from services.parsing.enterprise_driver import EnterpriseParser

_PARSER_REGISTRY = {
    "python": PythonParser(),
    "javascript": JSParser(),
    "typescript": JSParser(),
    "go": GoParser(),
    "php": PHPParser(),
    "infra": InfraParser(),
    "enterprise": EnterpriseParser(),
}

def _get_parser(lang: str):
    """Returns the registered parser for the given language."""
    return _PARSER_REGISTRY.get(lang)


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
            SET c.name = $name, c.file = $path, c.line = $line, c.line_end = $lend, c.docstring = $doc
            WITH c
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:DEFINES]->(c)
        """, fqn=cls.get("fqn", cls["name"]), name=cls["name"],
             path=rel_path, line=cls.get("line", 0), lend=cls.get("line_end", 0),
             doc=cls.get("docstring", ""),
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
            SET m.line = $line, m.line_end = $lend, m.type = $type, 
                m.complexity = $cc, m.docstring = $doc, m.tags = $tags,
                m.url_endpoint = $endpoint
            WITH m
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:DEFINES]->(m)
        """, name=fn["name"], path=rel_path, line=fn.get("line", 0), lend=fn.get("line_end", 0),
             type=fn.get("type", "function"), cc=fn.get("complexity", 1),
             doc=fn.get("docstring", ""), tags=fn.get("tags", []),
             endpoint=fn.get("url_endpoint"),
             pid=project_id, tid=tenant_id)

        # Link to owner class if it's a method
        if fn.get("owner"):
            tx.run("""
                MATCH (m:Function {name: $name, file: $path, project_id: $pid, tenant_id: $tid})
                MATCH (c:Class {name: $owner, file: $path, project_id: $pid, tenant_id: $tid})
                MERGE (m)-[:OWNED_BY]->(c)
            """, name=fn["name"], owner=fn["owner"], path=rel_path,
                 pid=project_id, tid=tenant_id)

    # Resolved Call edges (File -> File)
    for call in symbols.get("resolved_calls", []):
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MATCH (t:File {path: $target, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:CALLS {name: $name}]->(t)
        """, path=rel_path, target=call["target_file"], name=call["name"],
             pid=project_id, tid=tenant_id)

    # Raw Call nodes for Global XRef & Data Lineage
    for call in symbols.get("calls", []):
        c_name = call["name"] if isinstance(call, dict) else call
        p_func = call.get("parent_function") if isinstance(call, dict) else None
        
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (c:Call {name: $name, file_path: $path, tenant_id: $tid})
            SET c.args_content = $args
            MERGE (f)-[:CONTAINS_CALL]->(c)
            WITH c
            // Link to parent function for Taint Analysis
            OPTIONAL MATCH (parent:Function {name: $pname, file: $path, tenant_id: $tid})
            FOREACH (p IN CASE WHEN parent IS NOT NULL THEN [1] ELSE [] END |
                MERGE (parent)-[:CALLS_INTERNAL]->(c)
            )
        """, name=c_name, path=rel_path, pname=p_func, args=call.get("args_content", ""), pid=project_id, tid=tenant_id)

    # Raw Reference nodes for Global XRef (Classes, Types)
    for ref_name in symbols.get("references", []):
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (r:Reference {name: $name, file_path: $path, tenant_id: $tid})
            MERGE (f)-[:HAS_REFERENCE]->(r)
        """, name=ref_name, path=rel_path, pid=project_id, tid=tenant_id)

    # Data Flow edges (Phase 13: Taint Analysis)
    for ass in symbols.get("assignments", []):
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (v:Variable {name: $var, file: $path, tenant_id: $tid})
            MERGE (f)-[:HAS_VARIABLE]->(v)
            WITH v, $val as val
            // Simplificação: se o valor for uma chamada, ligamos a variável a essa intenção
            SET v.last_assigned_source = val
        """, var=ass.get("var"), val=ass.get("value") or ass.get("type"), 
             path=rel_path, pid=project_id, tid=tenant_id)

    # Global Services (Phase 16 - Global Infrastructure)
    for svc_name in symbols.get("services", []):
        tx.run("""
            MERGE (s:Service {name: $name, tenant_id: $tid})
            WITH s
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MERGE (s)-[:DEFINED_IN]->(f)
        """, name=svc_name, path=rel_path, pid=project_id, tid=tenant_id)

    # Import edges (File -> File or File -> Module)
    # We prioritize File -> File for resolved imports
    for imp in symbols.get("imports", []):
        # Try to find if this import was resolved to a file during Pass 1
        # (This is handled by the ResolutionContext in analyze_project)
        # For simplicity in the tx, we'll check if a File node exists for the 'imp' name
        # if it looks like a path or if it was resolved.
        pass # The actual resolution logic is in analyze_project's Pass 2.
        # Let's add a NEW field 'resolved_imports' to symbols in analyze_project
    
    for r_imp in symbols.get("resolved_imports", []):
        tx.run("""
            MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
            MATCH (t:File {path: $target, project_id: $pid, tenant_id: $tid})
            MERGE (f)-[:IMPORTS]->(t)
        """, path=rel_path, target=r_imp, pid=project_id, tid=tenant_id)


# ──────────────────────────────────────────────
# MAIN ANALYZER CLASS
# ──────────────────────────────────────────────

class ASTAnalyzer:
    def __init__(self,
                 neo4j_uri: str = NEO4J_URI,
                 neo4j_auth: tuple = (NEO4J_USER, NEO4J_PASS)):
        self.neo4j_uri   = neo4j_uri
        self.neo4j_auth  = neo4j_auth
        self._driver     = None
        self.symbols      = SymbolTable()
        self.suffix_index = None
        self.type_universe = TypeUniverse()
        ts_init()

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
        on_progress=None,
    ) -> dict:
        """
        Walk project_path, parse every source file, and perform a two-pass analysis.
        Pass 1: Extract all symbols to a global SymbolTable.
        Pass 2: Resolve cross-file dependencies (imports, calls) and upsert to Neo4j.
        """
        stats = {"files": 0, "nodes_created": 0, "edges_created": 0, "errors": 0, "complexity_total": 0}
        driver = self._get_driver()
        
        file_list = list(_walk_source_files(project_path))
        abs_paths = [p for p, _ in file_list]
        rel_paths = [os.path.relpath(p, project_path).replace("\\", "/") for p in abs_paths]
        
        # Initialize SuffixIndex with all relative paths
        self.suffix_index = SuffixIndex(rel_paths)
        res_ctx = ResolutionContext(self.symbols, self.suffix_index)

        # --- PASS 1: Symbol Extraction ---
        extracted_data: Dict[str, dict] = {} # rel_path -> symbols_dict
        
        for i, (abs_path, lang) in enumerate(file_list):
            rel_path = rel_paths[i]
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                parser_obj = _get_parser(lang)
                if not parser_obj:
                    continue
                
                symbols = parser_obj.parse(content, rel_path)
                extracted_data[rel_path] = {"symbols": symbols, "lang": lang}
                
                # Register symbols in global table
                for cls in symbols.get("classes", []):
                    self.symbols.add(SymbolDefinition(
                        name=cls["name"], 
                        file_path=rel_path, 
                        kind="Class", 
                        line=cls.get("line", 0),
                        bases=cls.get("bases", [])
                    ))
                for fn in symbols.get("functions", []):
                    self.symbols.add(SymbolDefinition(
                        name=fn["name"], 
                        file_path=rel_path, 
                        kind=fn.get("type", "Function"), 
                        line=fn.get("line", 0),
                        owner_id=fn.get("owner")
                    ))
                # Register raw imports into resolution context
                for imp in symbols.get("imports", []):
                    res_ctx.add_import(rel_path, imp)
                
                # Register assignments for Type Inference
                for ass in symbols.get("assignments", []):
                    self.type_universe.record_assignment(rel_path, ass["var"], ass["type"])
                    
            except Exception as exc:
                stats["errors"] += 1
                print(f"[ast-analyzer] Pass 1 error for {rel_path}: {exc}")

        # --- PASS 2: Semantic Resolution & Upsert ---
        for rel_path, data in extracted_data.items():
            try:
                symbols = data["symbols"]
                lang    = data["lang"]
                
                stats["files"] += 1
                stats["complexity_total"] += symbols.get("complexity_total", 0)

                # Resolve calls (Cross-file matching)
                resolved_calls = []
                for call_obj in symbols.get("calls", []):
                    call_name = call_obj["name"] if isinstance(call_obj, dict) else call_obj
                    
                    # Deep Resolution: If it's a member call 'obj.method', try type inference
                    target_file = None
                    if "." in call_name:
                        parts = call_name.split(".")
                        obj_name = parts[0]
                        meth_name = parts[-1]
                        inferred_type = self.type_universe.infer_type(rel_path, obj_name)
                        if inferred_type:
                            targets = res_ctx.resolve_symbol(meth_name, rel_path)
                            targets = [t for t in targets if t.owner_id == inferred_type or t.name == meth_name]
                    
                    targets = res_ctx.resolve_symbol(call_name, rel_path)
                    if targets:
                        resolved_calls.append({"name": call_name, "target_file": targets[0].file_path})
                
                symbols["resolved_calls"] = resolved_calls

                # Resolve imports (using the map built in Pass 1)
                symbols["resolved_imports"] = list(res_ctx.import_map.get(rel_path, set()))

                if driver:
                    with driver.session() as session:
                        session.execute_write(
                            _upsert_file_graph,
                            rel_path, lang, symbols, project_id, tenant_id
                        )

                n_nodes = (len(symbols.get("classes", [])) +
                           len(symbols.get("functions", [])) + 1)
                n_edges = (len(symbols.get("imports", [])) +
                           len(resolved_calls))
                
                stats["nodes_created"] += n_nodes
                stats["edges_created"] += n_edges

                if on_progress:
                    on_progress(rel_path, symbols)

            except Exception as exc:
                stats["errors"] += 1
                print(f"[ast-analyzer] Pass 2 error for {rel_path}: {exc}")

        # --- PHASE 3: Flow Mapping (Post-Analysis) ---
        if driver:
            try:
                from services.flow_mapper import FlowMapper
                mapper = FlowMapper(driver)
                mapper.map_processes(project_id, tenant_id)
            except Exception as exc:
                print(f"[ast-analyzer] Flow mapping error: {exc}")

        # Prepare return stats and symbols
        return {
            "files": len(file_list),
            "nodes_created": stats["nodes_created"],
            "edges_created": stats["edges_created"],
            "complexity_total": stats["complexity_total"],
            "symbols": self.symbols  # Full symbol table for Phase 7 (Deep Memory)
        }

    def reanalyze_file(self, project_path: str, abs_path: str, project_id: str, tenant_id: str):
        """
        Re-analyze a single file and update its graph nodes and relationships.
        This is an incremental update triggered by file changes.
        """
        rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
        ext = Path(abs_path).suffix.lower()
        lang = _LANG_EXTENSIONS.get(ext)
        if not lang:
            return

        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            parser = _get_parser(lang)
            if not parser:
                return
            
            symbols = parser.parse(content, rel_path)
            
            # Note: For incremental resolution without full pass 1, 
            # we rely on the EXISTING SymbolTable and SuffixIndex if they were loaded.
            # If not, we do a simple local resolution or skip call resolution for now.
            # Full consistency requires a new two-pass run occasionally.
            
            driver = self._get_driver()
            if driver:
                with driver.session() as session:
                    # Clean old nodes for this file first? 
                    # MATCH (f:File {path: $path}) DETACH DELETE f
                    # In this version, MERGE handles it but leaves orphaned edges.
                    # Best: 
                    session.run("MATCH (f:File {path: $p, project_id: $pid})-[r]->() DELETE r", 
                                p=rel_path, pid=project_id)
                    
                    session.execute_write(
                        _upsert_file_graph, 
                        rel_path, lang, symbols, project_id, tenant_id
                    )
            print(f"[ast-analyzer] Incremental update for {rel_path} complete.")
        except Exception as exc:
            print(f"[ast-analyzer] Incremental error for {rel_path}: {exc}")

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

from services.streaming.core.config import env_get
"""
Project Digital Twin
====================
A live structural model of the entire software project stored as a unified
graph in Neo4j.  Combines four sub-graphs into one queryable model:

  AST Graph   — File / Class / Function / Method nodes (via ast_analyzer.py)
  Task Graph  — OrchestratorTask / OrchestratorPlan nodes (streaming_server)
  Test Graph  — Test nodes + TESTS edges (detected by file name conventions)
  Infra Graph — Service / Container / Network nodes (parsed from docker-compose)

Key relationships added on top of the AST graph:
  (:OrchestratorTask)-[:MODIFIES]->(:File)
  (:Test)-[:TESTS]->(:Function)          # where test file covers a source file
  (:Service)-[:DEPENDS_ON]->(:Service)   # from docker-compose depends_on
  (:Service)-[:MOUNTS]->(:Volume)
  (:File)-[:COVERED_BY]->(:Test)         # reverse of TESTS

Gap-analysis queries (all read-only Cypher):
  /twin/gaps      — methods with zero TESTS coverage + dead functions (never called)
  /twin/coupling  — files/classes with highest in-degree (most depended-upon)
  /twin/status    — node + edge counts by label
  /twin/query     — arbitrary read-only Cypher pass-through

Usage:
    from services.digital_twin import DigitalTwin
    twin = DigitalTwin()
    twin.sync_project("/workspace/sinc", project_id="sinc", tenant_id="local")
    twin.link_task_to_files("TASK-001", ["app/Http/Controllers/AuthController.php"], "local")
    gaps = twin.gap_analysis("sinc", "local")
"""

import os
import re
import time
from pathlib import Path
from typing import Optional

from services.ast_analyzer import ASTAnalyzer, _walk_source_files, _PARSERS

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

NEO4J_URI  = env_get("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = env_get("NEO4J_USER", default="neo4j")
NEO4J_PASS = env_get("NEO4J_PASS", default=env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/")[-1])

# Test file patterns (by language)
_TEST_PATTERNS = [
    re.compile(r"(test|spec|tests)[\._/]", re.IGNORECASE),
    re.compile(r"[\._/](test|spec|tests)\.", re.IGNORECASE),
    re.compile(r"Test\.php$"),
    re.compile(r"test_.*\.py$"),
    re.compile(r".*\.test\.(js|ts)$"),
    re.compile(r".*\.spec\.(js|ts)$"),
]

_COMPOSE_FILENAMES = [
    "docker-compose.yml", "docker-compose.yaml",
    "docker-compose.orchestrator.yml", "docker-compose.generated.yml",
    "docker-compose.client.yml",
]


def _is_test_file(rel_path: str) -> bool:
    for pat in _TEST_PATTERNS:
        if pat.search(rel_path):
            return True
    return False


# ──────────────────────────────────────────────
# NEO4J DRIVER HELPER
# ──────────────────────────────────────────────

def _get_driver(uri: str, user: str, passwd: str):
    try:
        from neo4j import GraphDatabase
        return GraphDatabase.driver(uri, auth=(user, passwd))
    except ImportError:
        return None


# ──────────────────────────────────────────────
# INFRASTRUCTURE GRAPH  (docker-compose parser)
# ──────────────────────────────────────────────

def _parse_compose(compose_path: str) -> dict:
    """
    Parse a docker-compose YAML file and return a simplified infra dict:
      {
        "services": [{"name": ..., "image": ..., "depends_on": [...], "ports": [...], "volumes": [...]}],
        "networks": [...],
        "volumes": [...],
      }
    Uses pure regex / string matching — no PyYAML dependency required.
    Falls back gracefully on any parse error.
    """
    try:
        import yaml  # type: ignore
        content = Path(compose_path).read_text(encoding="utf-8", errors="replace")
        data = yaml.safe_load(content) or {}
    except ImportError:
        # Minimal regex fallback: just extract service names
        content = Path(compose_path).read_text(encoding="utf-8", errors="replace")
        data = _compose_regex_fallback(content)
    except Exception:
        return {"services": [], "networks": [], "volumes": []}

    services = []
    raw_services = data.get("services") or {}
    for svc_name, svc_cfg in (raw_services.items() if isinstance(raw_services, dict) else []):
        if not isinstance(svc_cfg, dict):
            continue
        deps = svc_cfg.get("depends_on", [])
        if isinstance(deps, dict):
            deps = list(deps.keys())
        elif not isinstance(deps, list):
            deps = []

        ports = svc_cfg.get("ports", [])
        if isinstance(ports, list):
            ports = [str(p) for p in ports]

        volumes_raw = svc_cfg.get("volumes", [])
        vol_names = []
        for v in (volumes_raw if isinstance(volumes_raw, list) else []):
            if isinstance(v, str) and ":" in v:
                left = v.split(":")[0]
                if not left.startswith(".") and not left.startswith("/"):
                    vol_names.append(left)
            elif isinstance(v, dict) and v.get("source"):
                vol_names.append(v["source"])

        services.append({
            "name":       svc_name,
            "image":      svc_cfg.get("image", ""),
            "depends_on": deps,
            "ports":      ports,
            "volumes":    vol_names,
        })

    networks = list((data.get("networks") or {}).keys()) if isinstance(data.get("networks"), dict) else []
    volumes  = list((data.get("volumes")  or {}).keys()) if isinstance(data.get("volumes"),  dict) else []

    return {"services": services, "networks": networks, "volumes": volumes}


def _compose_regex_fallback(content: str) -> dict:
    """Minimal regex parse when PyYAML is unavailable."""
    services = {}
    current_svc = None
    for line in content.splitlines():
        # Top-level service key (2-space indent + non-whitespace)
        m = re.match(r"^  (\w[\w\-]+):\s*$", line)
        if m and not line.startswith("    "):
            current_svc = m.group(1)
            services[current_svc] = {"image": "", "depends_on": [], "ports": [], "volumes": []}
            continue
        if current_svc:
            im = re.match(r"^\s+image:\s+(\S+)", line)
            if im:
                services[current_svc]["image"] = im.group(1)
    return {"services": services}


def _upsert_infra_graph(tx, infra: dict, project_id: str, tenant_id: str,
                        compose_file: str):
    """Write docker-compose-derived nodes into Neo4j."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Volume nodes
    for vol in infra.get("volumes", []):
        tx.run("""
            MERGE (v:Volume {name: $name, project_id: $pid, tenant_id: $tid})
            SET v.updated_at = $ts, v.compose_file = $cf
        """, name=vol, pid=project_id, tid=tenant_id, ts=ts, cf=compose_file)

    # Service nodes + edges
    for svc in infra.get("services", []):
        tx.run("""
            MERGE (s:Service {name: $name, project_id: $pid, tenant_id: $tid})
            SET s.image = $image, s.ports = $ports, s.updated_at = $ts,
                s.compose_file = $cf
        """, name=svc["name"], image=svc.get("image", ""),
             ports=svc.get("ports", []), pid=project_id, tid=tenant_id,
             ts=ts, cf=compose_file)

        for dep in svc.get("depends_on", []):
            tx.run("""
                MERGE (a:Service {name: $a, project_id: $pid, tenant_id: $tid})
                MERGE (b:Service {name: $b, project_id: $pid, tenant_id: $tid})
                MERGE (a)-[:DEPENDS_ON]->(b)
            """, a=svc["name"], b=dep, pid=project_id, tid=tenant_id)

        for vol in svc.get("volumes", []):
            tx.run("""
                MATCH (s:Service {name: $svc, project_id: $pid, tenant_id: $tid})
                MERGE (v:Volume {name: $vol, project_id: $pid, tenant_id: $tid})
                MERGE (s)-[:MOUNTS]->(v)
            """, svc=svc["name"], vol=vol, pid=project_id, tid=tenant_id)


# ──────────────────────────────────────────────
# TEST GRAPH  (test file → source file coverage)
# ──────────────────────────────────────────────

def _upsert_test_node(tx, rel_path: str, lang: str, symbols: dict,
                      project_id: str, tenant_id: str):
    """
    Mark a file as a Test node and link it to Function nodes whose names
    appear as identifiers in the test file's function list.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    tx.run("""
        MERGE (t:Test {path: $path, project_id: $pid, tenant_id: $tid})
        SET t.language = $lang, t.updated_at = $ts
        WITH t
        MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
        MERGE (f)-[:IS_TEST]->(t)
    """, path=rel_path, lang=lang, pid=project_id, tid=tenant_id, ts=ts)

    # Heuristic: a test function "test_login" or "testLogin" covers a function
    # whose name appears as a substring (stripped of test_ prefix).
    for fn in symbols.get("functions", []):
        raw = fn["name"]
        # Strip common prefixes: test_, Test, it_, should_, spec_
        stripped = re.sub(r"^(test_?|it_?|should_?|spec_?)", "", raw, flags=re.IGNORECASE)
        stripped = re.sub(r"^(test)", "", stripped, flags=re.IGNORECASE)
        # camelCase split: testLoginUser → Login, User
        parts = re.sub(r"([A-Z])", r" \1", stripped).split()
        candidates = [stripped] + [p for p in parts if len(p) > 3]

        for cand in candidates:
            if not cand:
                continue
            tx.run("""
                MATCH (fn:Function)
                WHERE toLower(fn.name) CONTAINS toLower($cand)
                  AND fn.project_id = $pid AND fn.tenant_id = $tid
                MERGE (t:Test {path: $path, project_id: $pid, tenant_id: $tid})
                MERGE (t)-[:TESTS]->(fn)
                WITH fn
                MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
                MERGE (fn)<-[:COVERS]-(f)
            """, cand=cand, path=rel_path, pid=project_id, tid=tenant_id)


# ──────────────────────────────────────────────
# MAIN DIGITAL TWIN CLASS
# ──────────────────────────────────────────────

class DigitalTwin:
    """
    Live structural model of the project.  All persistence is in Neo4j.
    Each method opens/closes its own driver session — safe to call concurrently.
    """

    def __init__(self,
                 neo4j_uri:  str = NEO4J_URI,
                 neo4j_user: str = NEO4J_USER,
                 neo4j_pass: str = NEO4J_PASS):
        self.neo4j_uri  = neo4j_uri
        self.neo4j_user = neo4j_user
        self.neo4j_pass = neo4j_pass

    def _driver(self):
        return _get_driver(self.neo4j_uri, self.neo4j_user, self.neo4j_pass)

    # ──────────────────────────────────────
    # SYNC: AST (code) graph
    # ──────────────────────────────────────

    def sync_project(
        self,
        project_path: str,
        project_id: str = "",
        tenant_id: str = "",
        on_progress=None,
    ) -> dict:
        """
        Full project sync: AST graph + test graph + infra graph.
        Returns summary statistics.
        """
        stats = {
            "files":         0,
            "test_files":    0,
            "nodes_created": 0,
            "edges_created": 0,
            "infra_services": 0,
            "errors":        0,
            "started_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        # 1. AST + test graph
        driver = self._driver()
        for abs_path, lang in _walk_source_files(project_path):
            rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                parser  = _PARSERS.get(lang)
                if not parser:
                    continue
                symbols = parser(content, rel_path)
                stats["files"] += 1

                n_nodes = (len(symbols.get("classes", [])) +
                           len(symbols.get("functions", [])) + 1)
                n_edges = len(symbols.get("imports", []))

                if driver:
                    from services.ast_analyzer import _upsert_file_graph
                    with driver.session() as session:
                        session.execute_write(
                            _upsert_file_graph,
                            rel_path, lang, symbols, project_id, tenant_id
                        )
                        if _is_test_file(rel_path):
                            stats["test_files"] += 1
                            session.execute_write(
                                _upsert_test_node,
                                rel_path, lang, symbols, project_id, tenant_id
                            )

                stats["nodes_created"] += n_nodes
                stats["edges_created"] += n_edges
                if on_progress:
                    on_progress(rel_path, symbols)

            except Exception as exc:
                stats["errors"] += 1
                print(f"[digital-twin] error parsing {rel_path}: {exc}")

        # 2. Infrastructure graph (docker-compose files)
        search_roots = [project_path, os.path.join(project_path, "docker")]
        for root in search_roots:
            if not os.path.isdir(root):
                continue
            for fname in _COMPOSE_FILENAMES:
                compose_path = os.path.join(root, fname)
                if not os.path.isfile(compose_path):
                    continue
                try:
                    infra = _parse_compose(compose_path)
                    stats["infra_services"] += len(infra.get("services", []))
                    if driver:
                        with driver.session() as session:
                            session.execute_write(
                                _upsert_infra_graph,
                                infra, project_id, tenant_id,
                                os.path.relpath(compose_path, project_path).replace("\\", "/")
                            )
                except Exception as exc:
                    stats["errors"] += 1
                    print(f"[digital-twin] error parsing {compose_path}: {exc}")

        if driver:
            driver.close()

        stats["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return stats

    def sync_file(
        self,
        abs_path: str,
        project_path: str,
        project_id: str,
        tenant_id: str,
    ) -> dict:
        """Incremental sync for a single changed file."""
        from pathlib import Path as _Path
        rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
        ext  = _Path(abs_path).suffix.lower()
        from services.ast_analyzer import _LANG_EXTENSIONS, _upsert_file_graph
        lang = _LANG_EXTENSIONS.get(ext)
        if not lang:
            return {"skipped": True, "reason": "unsupported extension"}
        try:
            content = _Path(abs_path).read_text(encoding="utf-8", errors="replace")
            parser  = _PARSERS.get(lang)
            if not parser:
                return {"skipped": True, "reason": "no parser"}
            symbols = parser(content, rel_path)
        except Exception as e:
            return {"error": str(e)}

        driver = self._driver()
        if driver:
            with driver.session() as session:
                session.execute_write(
                    _upsert_file_graph, rel_path, lang, symbols, project_id, tenant_id
                )
                if _is_test_file(rel_path):
                    session.execute_write(
                        _upsert_test_node, rel_path, lang, symbols, project_id, tenant_id
                    )
            driver.close()

        return {
            "file": rel_path, "language": lang,
            "classes":   len(symbols.get("classes", [])),
            "functions": len(symbols.get("functions", [])),
            "is_test":   _is_test_file(rel_path),
        }

    # ──────────────────────────────────────
    # TASK → FILE LINKS
    # ──────────────────────────────────────

    def link_task_to_files(
        self,
        task_id: str,
        file_paths: list[str],
        tenant_id: str,
        project_id: str = "",
    ) -> int:
        """
        After task completion, record which files the task modified.
        Creates (:OrchestratorTask)-[:MODIFIES]->(:File) edges.
        Returns number of links created.
        """
        if not file_paths:
            return 0
        driver = self._driver()
        if not driver:
            return 0
        count = 0
        try:
            with driver.session() as session:
                for fpath in file_paths:
                    norm = fpath.replace("\\", "/")
                    session.run("""
                        MERGE (t:OrchestratorTask {id: $tid, tenant_id: $tenant})
                        MERGE (f:File {path: $path, project_id: $pid, tenant_id: $tenant})
                        MERGE (t)-[:MODIFIES]->(f)
                    """, tid=task_id, tenant=tenant_id, path=norm, pid=project_id)
                    count += 1
        except Exception as exc:
            print(f"[digital-twin] link_task_to_files error: {exc}")
        finally:
            driver.close()
        return count

    # ──────────────────────────────────────
    # GAP ANALYSIS
    # ──────────────────────────────────────

    def gap_analysis(self, project_id: str, tenant_id: str) -> dict:
        """
        Returns:
          untested_functions  — functions with no (:Test)-[:TESTS]-> edge
          dead_functions      — functions never called by any other node
          uncovered_files     — files that have no test file covering them
        """
        driver = self._driver()
        if not driver:
            return {"error": "Neo4j unavailable"}

        result: dict = {
            "untested_functions": [],
            "dead_functions":     [],
            "uncovered_files":    [],
        }
        try:
            with driver.session() as session:
                # Functions with no test coverage
                rows = session.run("""
                    MATCH (fn:Function)
                    WHERE fn.project_id = $pid AND fn.tenant_id = $tid
                      AND NOT ()-[:TESTS]->(fn)
                      AND fn.name <> '__construct'
                      AND fn.name <> '__destruct'
                    RETURN fn.name AS name, fn.file AS file, fn.line AS line
                    ORDER BY fn.file, fn.line
                    LIMIT 200
                """, pid=project_id, tid=tenant_id)
                for rec in rows:
                    result["untested_functions"].append({
                        "name": rec["name"], "file": rec["file"], "line": rec["line"]
                    })

                # Dead functions (never called, not a constructor/destructor/entry)
                rows = session.run("""
                    MATCH (fn:Function)
                    WHERE fn.project_id = $pid AND fn.tenant_id = $tid
                      AND NOT ()-[:CALLS]->(fn)
                      AND NOT fn.name IN ['main', 'index', 'boot', 'register',
                                          '__construct', 'handle', 'run', 'setUp',
                                          'tearDown', 'invoke']
                      AND NOT fn.type = 'test'
                    RETURN fn.name AS name, fn.file AS file, fn.line AS line
                    ORDER BY fn.file, fn.line
                    LIMIT 200
                """, pid=project_id, tid=tenant_id)
                for rec in rows:
                    result["dead_functions"].append({
                        "name": rec["name"], "file": rec["file"], "line": rec["line"]
                    })

                # Files with no test coverage at all
                rows = session.run("""
                    MATCH (f:File)
                    WHERE f.project_id = $pid AND f.tenant_id = $tid
                      AND NOT (f)-[:IS_TEST]->()
                      AND NOT ()-[:COVERS]->(f)
                      AND NOT ()-[:TESTS]->()-[:DEFINED_IN|DEFINES]-(f)
                    RETURN f.path AS path, f.language AS lang
                    ORDER BY f.path
                    LIMIT 200
                """, pid=project_id, tid=tenant_id)
                for rec in rows:
                    result["uncovered_files"].append({
                        "path": rec["path"], "language": rec["lang"]
                    })

        except Exception as exc:
            result["error"] = str(exc)
        finally:
            driver.close()

        return result

    def coupling_analysis(self, project_id: str, tenant_id: str,
                          min_dependents: int = 3) -> dict:
        """
        Returns files/classes with the most in-degree edges (most depended-upon).
        High coupling → high change-impact risk.
        """
        driver = self._driver()
        if not driver:
            return {"error": "Neo4j unavailable"}

        result: dict = {"hotspots": [], "circular_deps": []}
        try:
            with driver.session() as session:
                # Hotspot files: many things import or depend on them
                rows = session.run("""
                    MATCH (target)<-[r:IMPORTS|CALLS|EXTENDS|IMPLEMENTS|DEPENDS_ON]-(source)
                    WHERE target.project_id = $pid AND target.tenant_id = $tid
                    WITH target, labels(target)[0] AS lbl,
                         COUNT(DISTINCT source) AS dependents
                    WHERE dependents >= $min_dep
                    RETURN lbl AS type,
                           COALESCE(target.name, target.path) AS name,
                           COALESCE(target.file, target.path) AS file,
                           dependents
                    ORDER BY dependents DESC
                    LIMIT 50
                """, pid=project_id, tid=tenant_id, min_dep=min_dependents)
                for rec in rows:
                    result["hotspots"].append({
                        "type":       rec["type"],
                        "name":       rec["name"],
                        "file":       rec["file"],
                        "dependents": rec["dependents"],
                    })

                # Simple circular dependency detection (depth ≤ 4)
                rows = session.run("""
                    MATCH path = (a:File)-[:IMPORTS*2..4]->(a)
                    WHERE a.project_id = $pid AND a.tenant_id = $tid
                    RETURN DISTINCT a.path AS file
                    LIMIT 30
                """, pid=project_id, tid=tenant_id)
                for rec in rows:
                    result["circular_deps"].append(rec["file"])

        except Exception as exc:
            result["error"] = str(exc)
        finally:
            driver.close()

        return result

    # ──────────────────────────────────────
    # LIVE STATUS  (node / edge counts)
    # ──────────────────────────────────────

    def status(self, project_id: str = "", tenant_id: str = "") -> dict:
        """Return counts of all node labels and relationship types in the twin."""
        driver = self._driver()
        if not driver:
            return {"error": "Neo4j unavailable"}

        info: dict = {"nodes": {}, "relationships": {}, "project_id": project_id}
        try:
            with driver.session() as session:
                # Node label counts (scoped to project/tenant when provided)
                if project_id:
                    rows = session.run("""
                        CALL apoc.meta.stats()
                        YIELD labels
                        RETURN labels
                    """)
                    # fallback if APOC not available
                    r = rows.single()
                    if r:
                        info["nodes"] = dict(r["labels"])

                if not info["nodes"]:
                    for label in ["File", "Class", "Function", "Module", "Test",
                                  "Service", "Volume", "OrchestratorTask", "OrchestratorPlan"]:
                        row = session.run(
                            f"MATCH (n:{label}) WHERE n.project_id = $pid RETURN count(n) AS c",
                            pid=project_id
                        ).single()
                        if row:
                            info["nodes"][label] = row["c"]

                for rel_type in ["DEFINES", "IMPORTS", "CALLS", "EXTENDS", "IMPLEMENTS",
                                  "TESTS", "MODIFIES", "DEPENDS_ON", "MOUNTS", "COVERS",
                                  "IS_TEST"]:
                    row = session.run(
                        f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS c"
                    ).single()
                    if row:
                        info["relationships"][rel_type] = row["c"]

        except Exception as exc:
            info["error"] = str(exc)
        finally:
            driver.close()

        return info

    # ──────────────────────────────────────
    # READ-ONLY CYPHER PASS-THROUGH
    # ──────────────────────────────────────

    def query(self, cypher: str, params: Optional[dict] = None,
              max_rows: int = 500) -> list[dict]:
        """
        Execute a read-only Cypher query and return results as list of dicts.
        Only SELECT-like (MATCH/RETURN) statements are allowed.
        """
        cypher_upper = cypher.strip().upper()
        if not cypher_upper.startswith("MATCH") and not cypher_upper.startswith("CALL"):
            raise ValueError("Only MATCH/CALL read queries are allowed via /twin/query")

        driver = self._driver()
        if not driver:
            return []
        rows = []
        try:
            with driver.session() as session:
                result = session.run(cypher, **(params or {}))
                for i, rec in enumerate(result):
                    if i >= max_rows:
                        break
                    rows.append(dict(rec))
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            driver.close()
        return rows


# ──────────────────────────────────────────────
# MODULE-LEVEL SINGLETON (for streaming_server.py)
# ──────────────────────────────────────────────

_twin = DigitalTwin()


def get_twin() -> DigitalTwin:
    return _twin


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    project_path = sys.argv[2] if len(sys.argv) > 2 else "."
    project_id   = env_get("PROJECT_ID", default="default")
    tenant_id    = env_get("TENANT_ID", default="local")

    twin = DigitalTwin()

    if cmd == "sync":
        print(f"[digital-twin] syncing {project_path}  project={project_id} tenant={tenant_id}")
        stats = twin.sync_project(
            project_path, project_id, tenant_id,
            on_progress=lambda p, s: print(
                f"  {'[TEST]' if _is_test_file(p) else '      '} {p}: "
                f"{len(s.get('classes',[]))} classes, {len(s.get('functions',[]))} fns"
            ),
        )
        print(json.dumps(stats, indent=2))

    elif cmd == "gaps":
        print(json.dumps(twin.gap_analysis(project_id, tenant_id), indent=2))

    elif cmd == "coupling":
        min_dep = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        print(json.dumps(twin.coupling_analysis(project_id, tenant_id, min_dep), indent=2))

    elif cmd == "status":
        print(json.dumps(twin.status(project_id, tenant_id), indent=2))

    elif cmd == "query":
        cypher = " ".join(sys.argv[2:])
        print(json.dumps(twin.query(cypher), indent=2))

    else:
        print("Usage: python digital_twin.py <cmd> [args]")
        print("  sync <project_path>         — full AST + infra sync")
        print("  gaps                        — untested functions + dead code")
        print("  coupling [min_dependents]   — high-coupling hotspots")
        print("  status                      — node/edge counts")
        print('  query "MATCH (n) RETURN n"  — read-only Cypher')
        print()
        print("Env: PROJECT_ID, TENANT_ID, NEO4J_URI, NEO4J_AUTH")

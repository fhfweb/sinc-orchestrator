from services.streaming.core.config import env_get
"""
Engineering Time Machine
========================
Pre-change impact simulation for the Orchestrator.

Before a task executes, the Time Machine:
  1. Snapshots the affected subgraph from Neo4j (or builds one from the filesystem)
  2. Applies the proposed change in memory — does NOT touch the real graph
  3. Traverses the in-memory graph to find all affected files, tests, and services
  4. Scores the risk of the change (0.0 = safe → 1.0 = critical)
  5. Returns a recommendation: execute_now / proceed_with_tests / split_task / requires_review
  6. Suggests pre-tasks (compatibility layers, test updates) when risk is high

Change Spec format (what the caller describes):
  {
    "files_modified":   ["app/Http/Controllers/AuthController.php"],
    "symbols_removed":  ["AuthController::login"],
    "symbols_renamed":  [{"from": "AuthController::login", "to": "AuthController::authenticate"}],
    "new_dependencies": ["App\\Services\\JwtService"],
    "removed_dependencies": [],
    "task_id":    "TASK-123",    # optional
    "task_title": "Refactor AuthController"
  }

Works standalone (filesystem graph) — Neo4j enhances results but is not required.

Usage:
    from services.time_machine import TimeMachine
    tm = TimeMachine(db_conn_fn=get_db)

    # Before executing a task:
    result = tm.simulate_change(
        change_spec={"files_modified": ["app/Services/AuthService.php"],
                     "symbols_removed": ["AuthService::legacyLogin"],
                     "task_title": "Refactor AuthService"},
        project_path="/workspace/sinc",
        project_id="sinc",
        tenant_id="local",
    )
    print(result["recommendation"])   # "split_task"
    print(result["risk_score"])       # 0.72
    print(result["pre_tasks"])        # ["Update AuthController tests", ...]
"""

import os
import re
import json
import time
import hashlib
from pathlib import Path
from typing import Callable, Optional
from collections import defaultdict, deque

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

NEO4J_URI  = env_get("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = env_get("NEO4J_USER", default="neo4j")
NEO4J_PASS = env_get("NEO4J_PASS", default=env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/")[-1])

# Risk score weights
_W_BLAST_FILES  = float(env_get("TM_W_BLAST_FILES", default="0.30"))
_W_BLAST_TESTS  = float(env_get("TM_W_BLAST_TESTS", default="0.25"))
_W_API_BREAK    = float(env_get("TM_W_API_BREAK", default="0.25"))
_W_ENTROPY_RISE = float(env_get("TM_W_ENTROPY_RISE", default="0.20"))

# Blast radius normalisation ceilings
_CEIL_BLAST_FILES = int(env_get("TM_CEIL_BLAST_FILES", default="20"))
_CEIL_BLAST_TESTS = int(env_get("TM_CEIL_BLAST_TESTS", default="15"))
_CEIL_API_BREAKS  = int(env_get("TM_CEIL_API_BREAKS", default="5"))

# Risk thresholds
RISK_LOW      = float(env_get("TM_RISK_LOW", default="0.30"))
RISK_MEDIUM   = float(env_get("TM_RISK_MEDIUM", default="0.50"))
RISK_HIGH     = float(env_get("TM_RISK_HIGH", default="0.70"))

_SKIP_DIRS    = {"vendor", "node_modules", ".git", "storage", "bootstrap/cache",
                 "__pycache__", ".venv", "venv", "dist", "build", ".next"}
_SOURCE_EXTS  = {".php", ".py", ".js", ".ts", ".go"}
_TEST_RE      = re.compile(
    r"(test|spec|tests)[\._/]|[\._/](test|spec|tests)\."
    r"|Test\.php$|test_.*\.py$|.*\.(test|spec)\.(js|ts)$",
    re.IGNORECASE,
)

# Import patterns (same as entropy_scanner, kept local to avoid circular import)
_IMPORT_PATTERNS: dict[str, re.Pattern] = {
    ".php": re.compile(r"^\s*(?:use|require_once|require|include)\s+([^\s;]+)", re.MULTILINE),
    ".py":  re.compile(r"^from\s+([\w./]+)\s+import|^import\s+([\w./,\s]+)", re.MULTILINE),
    ".js":  re.compile(r"(?:import\s+.*?from\s+|require\s*\()\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
    ".ts":  re.compile(r"(?:import\s+.*?from\s+|require\s*\()\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
    ".go":  re.compile(r'^\s*"([^"]+)"', re.MULTILINE),
}

# Public symbol patterns (functions/classes likely called by other modules)
_EXPORT_PATTERNS: dict[str, re.Pattern] = {
    ".php": re.compile(
        r"(?:public)\s+(?:static\s+)?function\s+(\w+)|"
        r"^(?:class|interface|trait)\s+(\w+)",
        re.MULTILINE,
    ),
    ".py":  re.compile(r"^(?:class\s+(\w+)|def\s+(\w+))", re.MULTILINE),
    ".js":  re.compile(r"^export\s+(?:default\s+)?(?:class|function|const)\s+(\w+)", re.MULTILINE),
    ".ts":  re.compile(r"^export\s+(?:default\s+)?(?:class|function|const)\s+(\w+)", re.MULTILINE),
    ".go":  re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?([A-Z]\w*)\s*\(", re.MULTILINE),
}


# ──────────────────────────────────────────────
# IN-MEMORY PROJECT GRAPH
# ──────────────────────────────────────────────

class ProjectGraph:
    """
    Lightweight in-memory dependency graph built from source files.
    Nodes = file rel_paths.  Edges = import relationships.
    Also tracks exported symbols and test file mappings.
    """

    def __init__(self):
        # rel_path → set of rel_paths it imports
        self.imports:  dict[str, set[str]] = defaultdict(set)
        # rel_path → set of rel_paths that import it (reverse)
        self.imported_by: dict[str, set[str]] = defaultdict(set)
        # rel_path → set of public symbol names
        self.exports:  dict[str, set[str]] = defaultdict(set)
        # rel_path → bool (is test file)
        self.is_test:  dict[str, bool] = {}
        # test_path → set of source paths it covers
        self.covers:   dict[str, set[str]] = defaultdict(set)
        # source_path → set of test paths covering it
        self.covered_by: dict[str, set[str]] = defaultdict(set)
        # All known file paths
        self.files: set[str] = set()

    @classmethod
    def build(cls, project_path: str) -> "ProjectGraph":
        """Build a full graph by walking the project directory."""
        g = cls()
        raw_imports: dict[str, list[str]] = {}

        # Pass 1: collect files, exports, raw import strings
        for dirpath, dirnames, filenames in os.walk(project_path):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in _SOURCE_EXTS:
                    continue
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, project_path).replace("\\", "/")
                g.files.add(rel)
                g.is_test[rel] = bool(_TEST_RE.search(rel))

                try:
                    content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                # Exports
                pat = _EXPORT_PATTERNS.get(ext)
                if pat:
                    for m in pat.finditer(content):
                        sym = next((x for x in m.groups() if x), None)
                        if sym:
                            g.exports[rel].add(sym)

                # Raw imports
                imp_pat = _IMPORT_PATTERNS.get(ext)
                if imp_pat:
                    imports = []
                    for m in imp_pat.finditer(content):
                        val = next((x for x in m.groups() if x), None)
                        if val:
                            imports.append(val.strip().strip("'\""))
                    raw_imports[rel] = imports

        # Pass 2: resolve import strings → rel_paths
        for src, imports in raw_imports.items():
            for imp in imports:
                matched = cls._resolve(imp, src, g.files)
                if matched:
                    g.imports[src].add(matched)
                    g.imported_by[matched].add(src)

        # Pass 3: test coverage heuristic
        for rel in g.files:
            if not g.is_test[rel]:
                continue
            base = Path(rel).stem.lower()
            base_clean = re.sub(r"(test|spec|tests)$|^(test_?|spec_?)", "", base, flags=re.IGNORECASE)
            for other in g.files:
                if g.is_test[other]:
                    continue
                other_base = Path(other).stem.lower()
                if (base_clean and (base_clean in other_base or other_base in base_clean)):
                    g.covers[rel].add(other)
                    g.covered_by[other].add(rel)

        return g

    @staticmethod
    def _resolve(imp: str, src: str, known: set[str]) -> Optional[str]:
        norm = imp.replace("\\", "/").replace(".", "/")
        for path in known:
            base = re.sub(r"\.\w+$", "", path)
            if base.endswith(norm) or norm.endswith(base.split("/")[-1]):
                return path
        return None

    def transitive_dependents(self, seed_paths: list[str],
                              max_depth: int = 4) -> set[str]:
        """
        BFS from seed_paths following imported_by edges.
        Returns all files that transitively depend on the seeds.
        """
        visited: set[str] = set()
        queue: deque = deque()
        for p in seed_paths:
            if p in self.files:
                queue.append((p, 0))
        while queue:
            node, depth = queue.popleft()
            if node in visited or depth > max_depth:
                continue
            visited.add(node)
            for dep in self.imported_by.get(node, set()):
                if dep not in visited:
                    queue.append((dep, depth + 1))
        # Exclude the seeds themselves
        return visited - set(seed_paths)

    def tests_for(self, file_paths: set[str]) -> set[str]:
        """Return all test files that cover any of the given source files."""
        tests: set[str] = set()
        for p in file_paths:
            tests |= self.covered_by.get(p, set())
        return tests


# ──────────────────────────────────────────────
# NEO4J ENRICHMENT (optional)
# ──────────────────────────────────────────────

def _neo4j_blast(file_paths: list[str], project_id: str, tenant_id: str,
                 max_depth: int = 3) -> dict:
    """
    Query Neo4j for transitive dependents + test coverage.
    Returns {"files": [...], "tests": [...]} or {} on failure.
    """
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS)
        )
        with driver.session() as s:
            # Transitive file dependents
            dep_result = s.run(f"""
                MATCH (src:File)<-[:IMPORTS|CALLS|DEPENDS_ON*1..{max_depth}]-(dep:File)
                WHERE src.path IN $paths
                  AND src.project_id = $pid AND src.tenant_id = $tid
                RETURN DISTINCT dep.path AS path
                LIMIT 100
            """, paths=file_paths, pid=project_id, tid=tenant_id)
            dep_files = [r["path"] for r in dep_result if r["path"]]

            # Tests covering the seeds + their dependents
            all_paths = file_paths + dep_files
            test_result = s.run("""
                MATCH (t:Test)-[:TESTS]->(fn:Function)
                WHERE fn.file IN $paths
                RETURN DISTINCT t.path AS path
                LIMIT 50
            """, paths=all_paths)
            test_files = [r["path"] for r in test_result if r["path"]]

        driver.close()
        return {"files": dep_files, "tests": test_files}
    except Exception:
        return {}


# ──────────────────────────────────────────────
# RISK SCORING
# ──────────────────────────────────────────────

def _risk_label(score: float) -> str:
    if score < RISK_LOW:    return "low"
    if score < RISK_MEDIUM: return "medium"
    if score < RISK_HIGH:   return "high"
    return "critical"


def _recommend(risk: float, n_api_breaks: int, n_blast_files: int) -> str:
    if risk < RISK_LOW:
        return "execute_now"
    if risk < RISK_MEDIUM:
        return "proceed_with_tests"
    if risk < RISK_HIGH:
        return "split_task"
    return "requires_review"


def _score_risk(n_blast_files: int, n_blast_tests: int,
                n_api_breaks: int, entropy_delta: float) -> float:
    s1 = min(n_blast_files / _CEIL_BLAST_FILES, 1.0)
    s2 = min(n_blast_tests / _CEIL_BLAST_TESTS, 1.0)
    s3 = min(n_api_breaks  / _CEIL_API_BREAKS,  1.0)
    s4 = max(min(entropy_delta, 1.0), 0.0)
    return round(
        _W_BLAST_FILES * s1 +
        _W_BLAST_TESTS * s2 +
        _W_API_BREAK   * s3 +
        _W_ENTROPY_RISE * s4,
        4,
    )


# ──────────────────────────────────────────────
# PRE-TASK SUGGESTION
# ──────────────────────────────────────────────

def _suggest_pre_tasks(
    change_spec: dict,
    blast_files: list[str],
    broken_interfaces: list[dict],
    affected_tests: list[str],
) -> list[dict]:
    """Generate concrete pre-task suggestions based on simulation findings."""
    suggestions: list[dict] = []
    files_modified = change_spec.get("files_modified", [])
    symbols_removed = change_spec.get("symbols_removed", [])
    symbols_renamed = change_spec.get("symbols_renamed", [])
    title = change_spec.get("task_title", "the change")

    # 1. Compatibility layer for removed public symbols
    for sym in symbols_removed:
        callers = [f for f in blast_files if any(
            sym.split("::")[-1] in f or sym.split("\\")[-1].lower() in f.lower()
            for _ in [1]
        )]
        if callers:
            suggestions.append({
                "type":  "compatibility",
                "title": f"Add compatibility shim for removed symbol: {sym}",
                "description": (
                    f"Symbol `{sym}` will be removed by \"{title}\". "
                    f"The following files reference it and need to be updated first: "
                    f"{', '.join(callers[:5])}. "
                    f"Add a compatibility wrapper or update call-sites before removal."
                ),
                "priority": 1,
            })

    # 2. Update renamed symbol callers
    for rename in symbols_renamed:
        suggestions.append({
            "type":  "rename_callers",
            "title": f"Update callers of renamed symbol: {rename.get('from')} → {rename.get('to')}",
            "description": (
                f"Symbol `{rename.get('from')}` is being renamed to `{rename.get('to')}` "
                f"in \"{title}\". Update all call-sites across the codebase "
                f"before or alongside this change."
            ),
            "priority": 1,
        })

    # 3. Test updates for heavily-impacted test files
    if len(affected_tests) > 0:
        suggestions.append({
            "type":  "update_tests",
            "title": f"Update {len(affected_tests)} test file(s) before \"{title}\"",
            "description": (
                f"The following test files cover code that will change: "
                f"{', '.join(affected_tests[:8])}. "
                f"Review and update tests to match the new interfaces."
            ),
            "priority": 2,
        })

    # 4. High blast radius → suggest splitting
    if len(blast_files) >= 8:
        suggestions.append({
            "type":  "split_change",
            "title": f"Split \"{title}\" into smaller increments",
            "description": (
                f"This change impacts {len(blast_files)} files transitively. "
                f"Consider breaking it into: (1) internal refactor with no public API changes, "
                f"(2) interface update, (3) consumer updates — each independently verifiable."
            ),
            "priority": 2,
        })

    # 5. No test coverage on modified files
    uncovered = [f for f in files_modified if f not in
                 {t for t in affected_tests for _ in [1]}]
    if uncovered:
        suggestions.append({
            "type":  "add_tests",
            "title": f"Add tests for {len(uncovered)} uncovered modified file(s)",
            "description": (
                f"The following files have no test coverage and are part of "
                f"\"{title}\": {', '.join(uncovered[:5])}. "
                f"Add tests before the change to catch regressions."
            ),
            "priority": 2,
        })

    return suggestions


# ──────────────────────────────────────────────
# SIMULATION RESULT BUILDER
# ──────────────────────────────────────────────

def _build_result(
    change_spec:       dict,
    blast_files:       list[str],
    blast_tests:       list[str],
    broken_interfaces: list[dict],
    entropy_delta:     float,
    project_id:        str,
    tenant_id:         str,
) -> dict:
    risk = _score_risk(
        len(blast_files), len(blast_tests),
        len(broken_interfaces), entropy_delta
    )
    rec  = _recommend(risk, len(broken_interfaces), len(blast_files))
    pre  = _suggest_pre_tasks(change_spec, blast_files, broken_interfaces, blast_tests)

    return {
        "task_id":     change_spec.get("task_id", ""),
        "task_title":  change_spec.get("task_title", ""),
        "project_id":  project_id,
        "tenant_id":   tenant_id,
        "simulated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "blast_radius": {
            "files":          len(blast_files),
            "tests":          len(blast_tests),
            "affected_files": blast_files[:50],
            "affected_tests": blast_tests[:30],
        },
        "broken_interfaces": broken_interfaces,
        "entropy_delta":      round(entropy_delta, 4),
        "risk_score":         risk,
        "risk_label":         _risk_label(risk),
        "recommendation":     rec,
        "pre_tasks":          pre,
        "summary": (
            f"Risk {_risk_label(risk)} ({risk:.2f}): "
            f"{len(blast_files)} files affected, "
            f"{len(blast_tests)} tests impacted, "
            f"{len(broken_interfaces)} potential interface breaks. "
            f"Recommendation: {rec}."
        ),
    }


# ──────────────────────────────────────────────
# MAIN TIME MACHINE CLASS
# ──────────────────────────────────────────────

class TimeMachine:
    """
    Engineering Time Machine — pre-change impact simulation.
    All computation is in-memory.  Results are optionally persisted to PostgreSQL.
    """

    def __init__(self, db_conn_fn: Optional[Callable] = None):
        self._db = db_conn_fn
        self._graph_cache: dict[str, tuple[float, ProjectGraph]] = {}  # path → (mtime, graph)

    # ──────────────────────────────────────────
    # CORE API
    # ──────────────────────────────────────────

    def simulate_change(
        self,
        change_spec:  dict,
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
        use_neo4j:    bool = True,
    ) -> dict:
        """
        Simulate applying change_spec to the project and compute impact.

        change_spec keys:
          files_modified    — list of rel_paths being changed
          symbols_removed   — public symbols being deleted
          symbols_renamed   — list of {from, to} symbol renames
          new_dependencies  — new imports being added
          removed_dependencies — imports being dropped

        Returns full simulation result including risk_score and pre_tasks.
        """
        files_modified = change_spec.get("files_modified", [])
        if not files_modified:
            return {"error": "files_modified is required in change_spec"}

        # Load in-memory project graph
        graph = self._get_graph(project_path)

        # 1. Blast radius — transitive dependents
        blast_set = graph.transitive_dependents(files_modified, max_depth=4)
        blast_files = sorted(blast_set)

        # Enrich with Neo4j if available
        if use_neo4j and NEO4J_URI:
            neo_data = _neo4j_blast(files_modified, project_id, tenant_id)
            if neo_data.get("files"):
                # Union with filesystem analysis
                blast_set |= set(neo_data["files"])
                blast_files = sorted(blast_set)
            if neo_data.get("tests"):
                neo_tests = neo_data["tests"]
            else:
                neo_tests = []
        else:
            neo_tests = []

        # 2. Affected tests
        test_set = graph.tests_for(blast_set | set(files_modified))
        test_set |= set(neo_tests)
        blast_tests = sorted(test_set)

        # 3. Broken interface detection
        symbols_removed = change_spec.get("symbols_removed", [])
        broken = self._find_broken_interfaces(graph, files_modified, symbols_removed)

        # 4. Entropy delta estimate
        # Heuristic: high blast + removed symbols → entropy likely increases
        symbols_renamed = change_spec.get("symbols_renamed", [])
        entropy_delta = self._estimate_entropy_delta(
            project_id, tenant_id, files_modified,
            len(blast_files), len(symbols_removed), len(symbols_renamed)
        )

        result = _build_result(
            change_spec, blast_files, blast_tests,
            broken, entropy_delta, project_id, tenant_id
        )

        self._persist(result)
        return result

    def simulate_task(
        self,
        task_id:      str,
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
    ) -> dict:
        """
        Simulate a task by looking it up in the DB (files_modified from task_file_links
        if the task was previously run, or extract from task title/description).
        Falls back to title-based file inference if no history.
        """
        change_spec: dict = {"task_id": task_id}

        if self._db:
            try:
                with self._db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT column_name
                              FROM information_schema.columns
                             WHERE table_schema = 'public'
                               AND table_name = 'tasks'
                            """
                        )
                        task_cols = {row["column_name"] for row in cur.fetchall()}
                        task_pk = "task_id" if "task_id" in task_cols else "id"
                        # Get task metadata
                        cur.execute(
                            "SELECT title, description, project_id FROM tasks WHERE {task_pk} = %s".format(task_pk=task_pk),
                            (task_id,)
                        )
                        row = cur.fetchone()
                        if row:
                            change_spec["task_title"]   = row.get("title", task_id)
                            change_spec["task_desc"]    = row.get("description", "")
                            pid = row.get("project_id", project_id)
                            if pid:
                                project_id = pid

                        # Get previously linked files (if task ran before and is being replayed)
                        cur.execute(
                            "SELECT file_path FROM task_file_links WHERE task_id = %s",
                            (task_id,)
                        )
                        linked = [r["file_path"] for r in cur.fetchall()]
                        if linked:
                            change_spec["files_modified"] = linked
            except Exception:
                pass

        # If no files linked, infer from task title/description
        if not change_spec.get("files_modified"):
            change_spec["files_modified"] = self._infer_files_from_text(
                change_spec.get("task_title", "") + " " + change_spec.get("task_desc", ""),
                project_path,
            )

        if not change_spec.get("files_modified"):
            return {
                "task_id": task_id,
                "warning": "No modified files identified — cannot simulate. "
                           "Run the task once to record file links, or add files_modified manually.",
                "risk_score":     0.0,
                "risk_label":     "unknown",
                "recommendation": "proceed_with_tests",
            }

        return self.simulate_change(change_spec, project_path, project_id, tenant_id)

    def simulate_plan(
        self,
        tasks: list[dict],
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
    ) -> dict:
        """
        Simulate an ordered sequence of tasks (e.g. from Global Planner).
        Applies changes cumulatively — task B sees the world after task A.
        tasks: list of change_spec dicts (same format as simulate_change).
        Returns per-task results + plan-level risk + ordering recommendations.
        """
        cumulative_modified: set[str] = set()
        per_task: list[dict] = []
        total_risk: float = 0.0

        for i, spec in enumerate(tasks):
            # Accumulate modified files from previous tasks
            all_modified = list(cumulative_modified | set(spec.get("files_modified", [])))
            enriched = {**spec, "files_modified": all_modified}

            result = self.simulate_change(enriched, project_path, project_id, tenant_id)
            result["task_index"] = i
            per_task.append(result)

            cumulative_modified |= set(spec.get("files_modified", []))
            total_risk = max(total_risk, result["risk_score"])

        # Check for conflicts: two tasks modify the same file
        file_to_tasks: dict[str, list[int]] = defaultdict(list)
        for i, spec in enumerate(tasks):
            for f in spec.get("files_modified", []):
                file_to_tasks[f].append(i)
        conflicts = [
            {"file": f, "tasks": idxs}
            for f, idxs in file_to_tasks.items()
            if len(idxs) > 1
        ]

        # Topological ordering check (no task should modify a file
        # that a later task depends on without explicit sequencing)
        ordering_warnings: list[str] = []
        for i, spec_a in enumerate(tasks):
            files_a = set(spec_a.get("files_modified", []))
            for j in range(i + 1, len(tasks)):
                spec_b = tasks[j]
                deps_b = set(spec_b.get("files_modified", []))
                if files_a & deps_b:
                    ordering_warnings.append(
                        f"Task {i} and task {j} both touch "
                        f"{list(files_a & deps_b)[:3]} — verify ordering"
                    )

        return {
            "project_id":         project_id,
            "tenant_id":          tenant_id,
            "task_count":         len(tasks),
            "max_risk_score":     round(total_risk, 4),
            "max_risk_label":     _risk_label(total_risk),
            "recommendation":     _recommend(total_risk, 0, 0),
            "per_task":           per_task,
            "file_conflicts":     conflicts,
            "ordering_warnings":  ordering_warnings,
            "simulated_at":       time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def blast_radius(
        self,
        file_paths:   list[str],
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
        max_depth:    int = 4,
    ) -> dict:
        """
        Compute the transitive blast radius for a set of files.
        Lighter than simulate_change — no change_spec required.
        """
        graph = self._get_graph(project_path)
        blast = graph.transitive_dependents(file_paths, max_depth)
        tests = graph.tests_for(blast | set(file_paths))

        # Neo4j enrichment
        if NEO4J_URI:
            neo_data = _neo4j_blast(file_paths, project_id, tenant_id, max_depth)
            if neo_data.get("files"):
                blast |= set(neo_data["files"])
            if neo_data.get("tests"):
                tests |= set(neo_data["tests"])

        blast_sorted = sorted(blast)
        tests_sorted  = sorted(tests)
        risk = _score_risk(len(blast_sorted), len(tests_sorted), 0, 0.0)

        return {
            "seed_files":      file_paths,
            "affected_files":  blast_sorted,
            "affected_tests":  tests_sorted,
            "file_count":      len(blast_sorted),
            "test_count":      len(tests_sorted),
            "risk_score":      risk,
            "risk_label":      _risk_label(risk),
        }

    # ──────────────────────────────────────────
    # HISTORY
    # ──────────────────────────────────────────

    def simulation_history(
        self, project_id: str, tenant_id: str, limit: int = 50
    ) -> list[dict]:
        """Return recent simulation runs for a project."""
        if not self._db:
            return []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT task_id, task_title, risk_score, risk_label,
                               recommendation, blast_files, blast_tests,
                               broken_interfaces, simulated_at
                        FROM simulation_runs
                        WHERE project_id = %s AND tenant_id = %s
                        ORDER BY simulated_at DESC
                        LIMIT %s
                    """, (project_id, tenant_id, limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ──────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────

    def _get_graph(self, project_path: str) -> ProjectGraph:
        """Load (or return cached) project graph. Cache invalidates after 5 min."""
        cached = self._graph_cache.get(project_path)
        if cached and (time.time() - cached[0]) < 300:
            return cached[1]
        g = ProjectGraph.build(project_path)
        self._graph_cache[project_path] = (time.time(), g)
        return g

    @staticmethod
    def _find_broken_interfaces(
        graph: ProjectGraph,
        files_modified: list[str],
        symbols_removed: list[str],
    ) -> list[dict]:
        """
        Find callers of removed symbols that exist in files outside files_modified.
        Returns list of {symbol, caller_file} dicts.
        """
        broken: list[dict] = []
        if not symbols_removed:
            return broken

        # For each file that imports a modified file,
        # check if it references the removed symbol name (heuristic)
        dependents = graph.transitive_dependents(files_modified, max_depth=2)
        for sym in symbols_removed:
            sym_short = sym.split("::")[-1].split("\\")[-1].split(".")[-1]
            for dep in dependents:
                # We don't re-parse here — use export data as proxy
                # A file is "at risk" if the symbol name appears in its known exports
                # (meaning it re-exports it) or if it's a direct dependent
                if dep in graph.imported_by.get(files_modified[0], set()):
                    broken.append({
                        "symbol":      sym,
                        "caller_file": dep,
                        "severity":    "high",
                    })

        return broken[:20]  # cap at 20

    def _estimate_entropy_delta(
        self,
        project_id: str,
        tenant_id:  str,
        files:      list[str],
        n_blast:    int,
        n_removed:  int,
        n_renamed:  int,
    ) -> float:
        """
        Estimate how much entropy will change after this modification.
        Positive = entropy increases (worse). Negative = entropy decreases (better).
        Uses latest entropy DB data if available, otherwise heuristic.
        """
        if self._db and (n_removed > 0 or n_renamed > 0 or n_blast > 5):
            try:
                with self._db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT AVG(entropy_score) AS avg
                            FROM (
                                SELECT DISTINCT ON (file_path) entropy_score
                                FROM entropy_snapshots
                                WHERE project_id = %s AND tenant_id = %s
                                  AND file_path = ANY(%s)
                                ORDER BY file_path, scan_at DESC
                            ) sub
                        """, (project_id, tenant_id, files))
                        row = cur.fetchone()
                        current_avg = float(row["avg"] or 0.5) if row else 0.5
            except Exception:
                current_avg = 0.5
        else:
            current_avg = 0.5

        # Heuristic delta: more removals/renames + wide blast → entropy tends to rise
        delta = 0.0
        if n_removed > 0:
            delta += min(n_removed * 0.05, 0.3)
        if n_renamed > 0:
            delta += min(n_renamed * 0.03, 0.15)
        if n_blast > 10:
            delta += 0.15
        elif n_blast > 5:
            delta += 0.05

        return round(delta, 4)

    @staticmethod
    def _infer_files_from_text(text: str, project_path: str) -> list[str]:
        """
        Try to infer likely modified files from task title/description
        by matching keywords against existing file names.
        Returns up to 5 candidate paths.
        """
        if not project_path or not os.path.isdir(project_path):
            return []

        # Extract capitalized words (likely class names) and path-like tokens
        keywords = re.findall(r"[A-Z][a-z]+[A-Za-z]*|[\w/]+\.(?:php|py|js|ts|go)", text)
        keywords = [k.lower() for k in keywords if len(k) > 3]
        if not keywords:
            return []

        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(project_path):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in _SOURCE_EXTS:
                    continue
                fname_lower = fname.lower()
                if any(kw in fname_lower for kw in keywords):
                    rel = os.path.relpath(
                        os.path.join(dirpath, fname), project_path
                    ).replace("\\", "/")
                    matches.append(rel)
                    if len(matches) >= 5:
                        return matches
        return matches

    def _persist(self, result: dict) -> None:
        """Persist simulation result to simulation_runs table."""
        if not self._db:
            return
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    sim_id = hashlib.md5(
                        f"{result.get('task_id','')}{result.get('simulated_at','')}".encode()
                    ).hexdigest()[:12]
                    cur.execute("""
                        INSERT INTO simulation_runs
                            (id, task_id, task_title, project_id, tenant_id,
                             risk_score, risk_label, recommendation,
                             blast_files, blast_tests, broken_interfaces,
                             entropy_delta, pre_tasks, full_result)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        sim_id,
                        result.get("task_id", ""),
                        result.get("task_title", ""),
                        result.get("project_id", ""),
                        result.get("tenant_id", ""),
                        result.get("risk_score", 0),
                        result.get("risk_label", ""),
                        result.get("recommendation", ""),
                        len(result.get("blast_radius", {}).get("affected_files", [])),
                        len(result.get("blast_radius", {}).get("affected_tests", [])),
                        len(result.get("broken_interfaces", [])),
                        result.get("entropy_delta", 0),
                        json.dumps(result.get("pre_tasks", [])),
                        json.dumps(result, default=str),
                    ))
                    conn.commit()
        except Exception:
            pass


# ──────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ──────────────────────────────────────────────

_tm: Optional[TimeMachine] = None


def get_time_machine(db_conn_fn: Optional[Callable] = None) -> TimeMachine:
    global _tm
    if _tm is None:
        _tm = TimeMachine(db_conn_fn=db_conn_fn)
    return _tm


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    project_path = sys.argv[2] if len(sys.argv) > 2 else "."
    project_id   = env_get("PROJECT_ID", default="default")
    tenant_id    = env_get("TENANT_ID", default="local")

    tm = TimeMachine()

    if cmd == "blast":
        files = sys.argv[3:] if len(sys.argv) > 3 else []
        if not files:
            print("Usage: time_machine.py blast <project_path> <file1> [file2 ...]")
            sys.exit(1)
        result = tm.blast_radius(files, project_path, project_id, tenant_id)
        print(json.dumps(result, indent=2))

    elif cmd == "simulate":
        # Quick simulation from CLI: read change_spec from stdin or args
        if len(sys.argv) < 4:
            print("Usage: time_machine.py simulate <project_path> <file1> [file2 ...]")
            print("       Reads change_spec from stdin (JSON) or uses files as files_modified")
            sys.exit(1)
        import select
        spec: dict = {}
        # Check if stdin has data (piped JSON)
        try:
            if not sys.stdin.isatty():
                spec = json.load(sys.stdin)
        except Exception:
            pass
        if not spec:
            spec = {"files_modified": sys.argv[3:], "task_title": "CLI simulation"}
        result = tm.simulate_change(spec, project_path, project_id, tenant_id)
        print(f"\n{'='*60}")
        print(f"Risk:           {result['risk_label'].upper()} ({result['risk_score']:.2f})")
        print(f"Recommendation: {result['recommendation']}")
        print(f"Blast radius:   {result['blast_radius']['files']} files, "
              f"{result['blast_radius']['tests']} tests")
        if result.get("broken_interfaces"):
            print(f"Broken APIs:    {len(result['broken_interfaces'])}")
        if result.get("pre_tasks"):
            print(f"\nSuggested pre-tasks ({len(result['pre_tasks'])}):")
            for pt in result["pre_tasks"]:
                print(f"  [{pt['type']}] {pt['title']}")
        print('='*60)

    else:
        print("Usage: python time_machine.py <cmd> <project_path> [args]")
        print("  blast    <project_path> <file1> [file2 ...]  — blast radius only")
        print("  simulate <project_path> <file1> [file2 ...]  — full simulation")
        print()
        print("Env: PROJECT_ID, TENANT_ID, NEO4J_URI, NEO4J_AUTH")

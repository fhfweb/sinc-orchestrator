"""
Entropy Scanner — Structural Entropy Model v3
==============================================
Measures structural health of a software project and produces an Entropy Score
(0.0 = perfect health, 1.0 = critical degradation) for each file/module.

Works standalone — no Neo4j required.  Reads source files directly.
Stores results in PostgreSQL (entropy_snapshots table).
Can seed repair tasks automatically when entropy exceeds a threshold.

Structural Entropy Model v3
────────────────────────────
  E(f) = Σ wᵢ · nᵢ(f)     Σwᵢ = 1.0

  Component             Symbol  Weight  Basis
  ────────────────────  ──────  ──────  ────────────────────────────────
  Cyclomatic compl.     n_cc    0.27    Bug predictor (Nagappan 2006)
  Function size         n_fs    0.14    Single-responsibility violation
  File size             n_sz    0.07    God-object signal
  Martin instability    n_I     0.12    Ce/(Ca+Ce); unstable → likely debt
  Blast-radius weight   n_bw    0.10    Ca/max_Ca; change propagation risk
  Test coverage gap     n_cv    0.13    Risk exposure (binary heuristic)
  Circular deps         n_ci    0.05    Architectural cycle
  Duplication           n_du    0.05    DRY violation
  Dependency entropy    n_Hd    0.07    Shannon H over import packages

  Normalization — Z-score (project-relative):
    z(f) = (x(f) − μ_project) / σ_project
    n(f) = clamp((z + 2) / 4, 0, 1)
    Average file → 0.50,  +2σ outlier → 1.00

  Non-linear dominance penalty:
    E_final = base + 0.08 · max(nᵢ)   if  max(nᵢ) ≥ 0.85

  Thresholds:
    [0.00, 0.35)  healthy           — within acceptable bounds
    [0.35, 0.60)  watch             — monitor; schedule review
    [0.60, 0.80)  refactor          — active technical debt
    [0.80, 0.85)  critical          — structural risk; block PR
    [0.85, 1.00]  structural_hazard — dominance penalty applied

Usage:
    from services.entropy_scanner import EntropyScanner
    scanner = EntropyScanner(db_conn_fn=get_db)
    report  = scanner.scan_and_store("/workspace/myproject", "myproject", "local")
    tasks   = scanner.seed_tasks("myproject", "local", threshold=0.70)
"""
from __future__ import annotations
from services.streaming.core.config import env_get

import math
import os
import re
import json
import time
import hashlib
from pathlib import Path
from typing import Callable, Optional

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

# Entropy weights — 9 components summing to 1.0
_W_COMPLEXITY  = 0.27   # cyclomatic complexity
_W_FN_SIZE     = 0.14   # largest function (lines)
_W_FILE_SIZE   = 0.07   # total file lines
_W_INSTABILITY = 0.12   # Martin's instability I = Ce/(Ca+Ce)
_W_BLAST       = 0.10   # blast-radius weight Ca/max_Ca
_W_COVERAGE    = 0.13   # test coverage gap
_W_CIRCULAR    = 0.05   # circular dependency
_W_DUPLICATION = 0.05   # duplicate-line fraction
_W_DEP_ENTROPY = 0.07   # Shannon H over import-package distribution
# sum: 0.27+0.14+0.07+0.12+0.10+0.13+0.05+0.05+0.07 = 1.00

# Non-linear dominance penalty
_DOMINANCE_THRESHOLD = 0.85
_DOMINANCE_LAMBDA    = 0.08

_METRIC_NAMES = (
    "complexity", "fn_size", "file_size",
    "instability", "blast", "coverage", "circular", "duplication",
    "dep_entropy",
)

# Auto-seeding thresholds
THRESHOLD_WATCH    = float(env_get("ENTROPY_THRESHOLD_WATCH", default="0.35"))
THRESHOLD_REFACTOR = float(env_get("ENTROPY_THRESHOLD_REFACTOR", default="0.60"))
THRESHOLD_CRITICAL = float(env_get("ENTROPY_THRESHOLD_CRITICAL", default="0.80"))
THRESHOLD_SEED     = float(env_get("ENTROPY_THRESHOLD_SEED", default="0.70"))

_SKIP_DIRS = {
    "vendor", "node_modules", ".git", "storage", "bootstrap/cache",
    "__pycache__", ".venv", "venv", "dist", "build", ".next",
    ".nuxt", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", "target", "out", ".idea", ".vscode",
}
_SOURCE_EXTS = {
    ".php", ".py", ".js", ".ts", ".go",
    ".jsx", ".tsx", ".rb", ".java", ".kt", ".cs", ".cpp", ".c",
    ".h", ".rs", ".swift", ".vue", ".svelte",
}
_TEST_RE = re.compile(
    r"(test|spec|tests)[\._/]|[\._/](test|spec|tests)\."
    r"|Test\.php$|test_.*\.py$|.*\.(test|spec)\.(js|ts)$",
    re.IGNORECASE,
)

# Decision-point keywords per language (for cyclomatic complexity)
_DECISION_RE = re.compile(
    r"\b(if|elif|else\s+if|for|foreach|while|do\b|case|catch|except|"
    r"unless|switch)\b|(\?\s*[^:]+:)|(\|\||&&|\band\b|\bor\b)",
    re.IGNORECASE,
)

# Function / method boundary patterns per language
_FN_START: dict[str, re.Pattern] = {
    ".php": re.compile(
        r"(?:public|protected|private|static)(?:\s+(?:public|protected|private|static))*"
        r"\s+function\s+\w+\s*\(|^function\s+\w+\s*\(",
        re.MULTILINE
    ),
    ".py":  re.compile(r"^(?:    )*def\s+\w+\s*\(", re.MULTILINE),
    ".js":  re.compile(
        r"(?:^|\s)(?:async\s+)?function\s+\w+\s*\("
        r"|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(",
        re.MULTILINE
    ),
    ".ts":  re.compile(
        r"(?:^|\s)(?:async\s+)?function\s+\w+\s*\("
        r"|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(",
        re.MULTILINE
    ),
    ".go":  re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?\w+\s*\(", re.MULTILINE),
}

# Import/dependency extraction per language
_IMPORT_RE: dict[str, re.Pattern] = {
    ".php": re.compile(r"^\s*(?:use|require_once|require|include_once|include)\s+([^\s;]+)", re.MULTILINE),
    ".py":  re.compile(r"^from\s+([\w./]+)\s+import|^import\s+([\w./,\s]+)", re.MULTILINE),
    ".js":  re.compile(r"(?:import\s+.*?from\s+|require\s*\(\s*)['\"]([^'\"]+)['\"]", re.MULTILINE),
    ".ts":  re.compile(r"(?:import\s+.*?from\s+|require\s*\(\s*)['\"]([^'\"]+)['\"]", re.MULTILINE),
    ".go":  re.compile(r'^\s*"([^"]+)"', re.MULTILINE),
}


# ──────────────────────────────────────────────
# LOW-LEVEL METRIC EXTRACTORS
# ──────────────────────────────────────────────

def _count_cyclomatic(content: str) -> int:
    """
    Approximate cyclomatic complexity for a whole file.
    Starts at 1 + counts decision points.
    """
    return 1 + len(_DECISION_RE.findall(content))


def _function_line_lengths(content: str, ext: str) -> list[int]:
    """
    Return a list of approximate line-lengths for each function/method.
    Uses next function start as end boundary.
    """
    pattern = _FN_START.get(ext)
    if not pattern:
        return []
    lines  = content.splitlines()
    starts = [content[:m.start()].count("\n") for m in pattern.finditer(content)]
    if not starts:
        return []
    lengths = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(lines)
        lengths.append(end - start)
    return lengths


def _extract_imports(content: str, ext: str) -> list[str]:
    """Return raw import/dependency strings from a file."""
    pattern = _IMPORT_RE.get(ext)
    if not pattern:
        return []
    imports = []
    for m in pattern.finditer(content):
        val = next((g for g in m.groups() if g), None)
        if val:
            imports.append(val.strip().strip("'\""))
    return imports


def _line_fingerprints(content: str, min_length: int = 6) -> set[str]:
    """
    Return a set of stripped non-trivial line fingerprints for duplication detection.
    A fingerprint is a normalised, stripped line of >= min_length chars.
    """
    fps = set()
    for line in content.splitlines():
        stripped = re.sub(r"\s+", " ", line.strip())
        # skip trivial lines: blank, single-char, comments, braces
        if (len(stripped) >= min_length
                and not stripped.startswith(("//", "#", "*", "/*", "<!--"))
                and stripped not in ("{", "}", "}", "end", "pass", ";")):
            fps.add(stripped)
    return fps


# ──────────────────────────────────────────────
# Z-SCORE NORMALIZATION HELPERS
# ──────────────────────────────────────────────

def _mean_std(values: list[float]) -> tuple[float, float]:
    """Return (mean, population_std) with floor to avoid division by zero."""
    if not values:
        return 0.0, 1.0
    n    = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 1.0
    var  = sum((v - mean) ** 2 for v in values) / n
    return mean, max(var ** 0.5, 1e-9)


def _z_clamp(x: float, mean: float, std: float) -> float:
    """
    Project-relative Z-score normalization, clamped to [0, 1].
    Average file → 0.50,  +2σ outlier → 1.00,  -2σ → 0.00
    """
    if std < 1e-9:
        return 0.0
    return max(0.0, min(1.0, ((x - mean) / std + 2.0) / 4.0))


# ──────────────────────────────────────────────
# SHANNON DEPENDENCY ENTROPY
# ──────────────────────────────────────────────

def _dependency_entropy(imports: list[str]) -> float:
    """
    Shannon entropy H̃ over the distribution of import packages, normalized [0,1].
    H̃ → 0: all imports from one package (cohesive)
    H̃ → 1: imports maximally spread (chaotic dependency web)
    """
    if not imports:
        return 0.0
    packages: list[str] = []
    for imp in imports:
        clean = imp.replace("\\", "/")
        if clean.startswith("."):
            packages.append(clean.split("/")[0] or ".")
        else:
            packages.append(clean.split(".")[0].split("/")[0] or imp)
    if not packages:
        return 0.0
    total  = len(packages)
    counts: dict[str, int] = {}
    for pkg in packages:
        counts[pkg] = counts.get(pkg, 0) + 1
    k = len(counts)
    if k <= 1:
        return 0.0
    h_raw = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return round(h_raw / math.log2(k), 4)


# ──────────────────────────────────────────────
# AFFERENT COUPLING (Ca)
# ──────────────────────────────────────────────

def _build_afferent(import_graph: dict[str, list[str]]) -> dict[str, int]:
    """
    Compute Ca (afferent coupling) for each file.
    Ca(f) = number of other project files that import f.
    """
    stem_to_rel: dict[str, str] = {}
    for rel in import_graph:
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        stem_to_rel[stem] = rel
        parts = rel.replace("\\", "/").replace("/", ".").lower()
        stem_to_rel[parts] = rel

    ca: dict[str, int] = {rel: 0 for rel in import_graph}
    for _src, imported_list in import_graph.items():
        for imp in imported_list:
            imp_clean = imp.split("/")[-1].split(".")[-1].lower()
            if imp_clean in stem_to_rel:
                target = stem_to_rel[imp_clean]
                if target != _src:
                    ca[target] = ca.get(target, 0) + 1
    return ca


# ──────────────────────────────────────────────
# ENTROPY SCORING
# ──────────────────────────────────────────────

def _label(score: float) -> str:
    if score < THRESHOLD_WATCH:
        return "healthy"
    if score < THRESHOLD_REFACTOR:
        return "watch"
    if score < THRESHOLD_CRITICAL:
        return "refactor"
    if score < _DOMINANCE_THRESHOLD:
        return "critical"
    return "structural_hazard"


def _emoji(score: float) -> str:
    return {
        "healthy":           "🟢",
        "watch":             "🟡",
        "refactor":          "🟠",
        "critical":          "🔴",
        "structural_hazard": "☠",
    }[_label(score)]


def _compute_entropy(
    n_cc:         float,       # pre-normalized cyclomatic complexity  [0, 1]
    n_fs:         float,       # pre-normalized max function size      [0, 1]
    n_sz:         float,       # pre-normalized file size              [0, 1]
    instability:  float,       # Ce / (Ca + Ce)                        [0, 1]
    blast_weight: float,       # Ca / max_Ca                           [0, 1]
    has_tests:    bool,
    circular:     bool,
    duplication:  float,
    dep_entropy:  float = 0.0,
    churn_norm:   float = 0.0,
) -> tuple[float, str]:
    """
    Returns (score, dominant_metric_name).
    All continuous inputs are pre-normalized to [0,1] by the caller (Z-score).
    """
    n_I  = instability
    n_bw = blast_weight
    n_cv = 0.0 if has_tests else 1.0
    n_ci = 1.0 if circular  else 0.0
    n_du = min(duplication, 1.0)
    n_Hd = min(dep_entropy, 1.0)

    normalized = (n_cc, n_fs, n_sz, n_I, n_bw, n_cv, n_ci, n_du, n_Hd)

    base = (
        _W_COMPLEXITY  * n_cc +
        _W_FN_SIZE     * n_fs +
        _W_FILE_SIZE   * n_sz +
        _W_INSTABILITY * n_I  +
        _W_BLAST       * n_bw +
        _W_COVERAGE    * n_cv +
        _W_CIRCULAR    * n_ci +
        _W_DUPLICATION * n_du +
        _W_DEP_ENTROPY * n_Hd
    )

    churn_boost = 0.04 * churn_norm if churn_norm > 0 else 0.0

    dom_idx  = max(range(len(normalized)), key=lambda i: normalized[i])
    dom_val  = normalized[dom_idx]
    dom_name = _METRIC_NAMES[dom_idx] if dom_val >= _DOMINANCE_THRESHOLD else ""
    penalty  = _DOMINANCE_LAMBDA * dom_val if dom_val >= _DOMINANCE_THRESHOLD else 0.0

    score = round(min(base + churn_boost + penalty, 1.0), 4)
    return score, dom_name


def _martin_zone(instability: float, afferent: int, efferent: int) -> str:
    """Classify file into Martin's architectural zones."""
    if instability < 0.3:
        return "zone_of_pain" if efferent > 5 else "main_sequence"
    if instability > 0.7:
        return "zone_of_uselessness" if afferent > 5 else "neutral"
    return "main_sequence"


# ──────────────────────────────────────────────
# MAIN SCANNER CLASS
# ──────────────────────────────────────────────

class EntropyScanner:
    """
    Scans a project directory, computes entropy metrics per file,
    persists results to PostgreSQL, and optionally seeds repair tasks.
    """

    def __init__(self, db_conn_fn: Optional[Callable] = None):
        """
        db_conn_fn — zero-arg callable that returns a psycopg2-compatible
                     connection with cursor(row_factory=dict).
                     If None, metrics are computed but not persisted.
        """
        self._db = db_conn_fn

    # ──────────────────────────────────────────
    # SCAN
    # ──────────────────────────────────────────

    def scan_project(
        self,
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
        on_progress:  Optional[Callable] = None,
        churn_map:    Optional[dict[str, int]] = None,
    ) -> list[dict]:
        """
        Walk the project, compute entropy for every source file.
        Uses two-pass Z-score normalization (project-relative anomaly scoring).
        Returns list of metric dicts (one per file), sorted by entropy DESC.
        Does NOT persist to DB — call scan_and_store() for that.
        """
        # ── Pass 1: read files + extract raw metrics ────────────────────────
        # raw entry: (abs_path, rel_path, ext, compl, max_fn, lines, imports, fps)
        raw: list[tuple] = []
        import_graph: dict[str, list[str]] = {}

        for abs_path, ext, rel_path in self._walk(project_path):
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                fn_lens = _function_line_lengths(content, ext)
                imports = _extract_imports(content, ext)
                fps     = _line_fingerprints(content)
                compl   = _count_cyclomatic(content)
                max_fn  = max(fn_lens) if fn_lens else 0
                lines   = content.count("\n") + 1
                raw.append((abs_path, rel_path, ext, compl, max_fn, lines, imports, fps))
                import_graph[rel_path] = imports
            except Exception as exc:
                print(f"[entropy] error reading {abs_path}: {exc}")

        if not raw:
            return []

        # ── Build structural maps ────────────────────────────────────────────
        covered_files  = self._map_test_coverage_list([r[1] for r in raw])
        cyclic_files   = self._detect_cycles(import_graph)
        fp_list        = [(r[1], r[7]) for r in raw]
        dup_map        = self._compute_duplication(fp_list)
        afferent_map   = _build_afferent(import_graph)

        max_ca    = max(afferent_map.values(), default=1) or 1
        max_churn = max(churn_map.values(), default=1) if churn_map else 1

        # ── Compute per-project Z-score statistics ───────────────────────────
        src_files = [r for r in raw if not _TEST_RE.search(r[1])]
        cc_mean, cc_std = _mean_std([float(r[3]) for r in src_files])
        fs_mean, fs_std = _mean_std([float(r[4]) for r in src_files])
        sz_mean, sz_std = _mean_std([float(r[5]) for r in src_files])

        # ── Pass 2: compute per-file entropy ─────────────────────────────────
        results: list[dict] = []

        for idx, (_abs, rel, ext, compl, max_fn, lines, imports, _fps) in enumerate(raw):
            if on_progress:
                on_progress(rel, {"file_path": rel})

            is_test = bool(_TEST_RE.search(rel))
            if is_test:
                results.append({
                    "file_path":    rel, "project_id": project_id,
                    "tenant_id":    tenant_id, "language": ext.lstrip("."),
                    "is_test":      True, "entropy_score": 0.0,
                    "label": "healthy", "status": "🟢",
                    "complexity": compl, "max_fn_lines": max_fn,
                    "file_lines": lines, "efferent": len(imports),
                    "afferent": 0, "instability": 1.0, "blast_weight": 0.0,
                    "has_tests": True, "circular_deps": False,
                    "duplication": 0.0, "dep_entropy": 0.0,
                    "hotspot_score": 0.0, "dominant_metric": "",
                    "martin_zone": "neutral", "churn_count": 0,
                })
                continue

            ce           = len(imports)
            ca           = afferent_map.get(rel, 0)
            has_tests    = rel in covered_files
            circular     = rel in cyclic_files
            dup          = dup_map.get(rel, 0.0)
            instability  = ce / (ca + ce) if (ca + ce) > 0 else 1.0
            blast_weight = ca / max_ca
            dep_ent      = _dependency_entropy(imports)
            churn_raw    = churn_map.get(rel, 0) if churn_map else 0
            churn_norm   = min(churn_raw / max_churn, 1.0) if churn_map else 0.0

            n_cc = _z_clamp(float(compl),  cc_mean, cc_std)
            n_fs = _z_clamp(float(max_fn), fs_mean, fs_std)
            n_sz = _z_clamp(float(lines),  sz_mean, sz_std)

            score, dominant = _compute_entropy(
                n_cc, n_fs, n_sz,
                instability, blast_weight,
                has_tests, circular, dup,
                dep_entropy=dep_ent,
                churn_norm=churn_norm,
            )

            results.append({
                "file_path":      rel,
                "project_id":     project_id,
                "tenant_id":      tenant_id,
                "language":       ext.lstrip("."),
                "is_test":        False,
                "entropy_score":  score,
                "label":          _label(score),
                "status":         _emoji(score),
                "dominant_metric": dominant,
                "complexity":     compl,
                "fn_count":       0,
                "max_fn_lines":   max_fn,
                "file_lines":     lines,
                "efferent":       ce,
                "afferent":       ca,
                "instability":    round(instability, 4),
                "blast_weight":   round(blast_weight, 4),
                "has_tests":      has_tests,
                "circular_deps":  circular,
                "duplication":    dup,
                "dep_entropy":    dep_ent,
                "hotspot_score":  round(score * churn_norm, 4) if churn_norm > 0 else 0.0,
                "churn_count":    churn_raw,
                "martin_zone":    _martin_zone(instability, ca, ce),
                # legacy field kept for DB schema compatibility
                "coupling":       ca,
                "test_coverage":  1.0 if has_tests else 0.0,
            })

        results.sort(key=lambda x: x["entropy_score"], reverse=True)
        return results

    def scan_and_store(
        self,
        project_path: str,
        project_id:   str = "",
        tenant_id:    str = "",
    ) -> dict:
        """
        Run scan_project() and persist results to entropy_snapshots.
        Returns summary statistics.
        """
        results = self.scan_project(project_path, project_id, tenant_id)

        summary = {
            "files_scanned":      len(results),
            "structural_hazard":  sum(1 for r in results if r["label"] == "structural_hazard"),
            "critical":           sum(1 for r in results if r["label"] == "critical"),
            "refactor":           sum(1 for r in results if r["label"] == "refactor"),
            "watch":              sum(1 for r in results if r["label"] == "watch"),
            "healthy":            sum(1 for r in results if r["label"] == "healthy"),
            "avg_entropy":        round(
                sum(r["entropy_score"] for r in results) / len(results), 4
            ) if results else 0.0,
            "top_offenders":  [
                {"file": r["file_path"], "score": r["entropy_score"], "label": r["label"]}
                for r in results[:10]
            ],
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        if self._db:
            try:
                with self._db() as conn:
                    with conn.cursor() as cur:
                        for r in results:
                            metrics_json = {
                                "complexity":       r["complexity"],
                                "fn_count":         r.get("fn_count", 0),
                                "efferent":         r.get("efferent", 0),
                                "afferent":         r.get("afferent", 0),
                                "dominant_metric":  r.get("dominant_metric", ""),
                                "co_change_score":  r.get("co_change_score", 0.0),
                            }
                            cur.execute("""
                                INSERT INTO entropy_snapshots
                                    (project_id, tenant_id, file_path,
                                     entropy_score, complexity, max_fn_lines,
                                     file_lines, coupling, test_coverage,
                                     circular_deps, duplication, language,
                                     label, metrics_json,
                                     instability, blast_weight, dep_entropy,
                                     hotspot_score, martin_zone, churn_count)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                        %s,%s,%s,%s,%s,%s)
                            """, (
                                project_id, tenant_id, r["file_path"],
                                r["entropy_score"], r["complexity"], r["max_fn_lines"],
                                r["file_lines"], r.get("coupling", r.get("afferent", 0)),
                                r.get("test_coverage", 0.0),
                                r["circular_deps"], r["duplication"], r["language"],
                                r["label"], json.dumps(metrics_json),
                                r.get("instability", 1.0),
                                r.get("blast_weight", 0.0),
                                r.get("dep_entropy", 0.0),
                                r.get("hotspot_score", 0.0),
                                r.get("martin_zone", "neutral"),
                                r.get("churn_count", 0),
                            ))
                        conn.commit()
            except Exception as exc:
                summary["db_error"] = str(exc)

        return summary

    # ──────────────────────────────────────────
    # REPORTING
    # ──────────────────────────────────────────

    def latest_report(self, project_id: str, tenant_id: str,
                      limit: int = 200) -> list[dict]:
        """
        Return the most recent entropy snapshot per file for this project.
        Results sorted by entropy_score DESC.
        """
        if not self._db:
            return []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT DISTINCT ON (file_path)
                               file_path, entropy_score, label,
                               complexity, max_fn_lines, file_lines,
                               coupling, test_coverage, circular_deps,
                               duplication, language, scan_at,
                               COALESCE(instability,   1.0)      AS instability,
                               COALESCE(blast_weight,  0.0)      AS blast_weight,
                               COALESCE(dep_entropy,   0.0)      AS dep_entropy,
                               COALESCE(hotspot_score, 0.0)      AS hotspot_score,
                               COALESCE(martin_zone, 'neutral')  AS martin_zone,
                               COALESCE(churn_count,   0)        AS churn_count
                        FROM entropy_snapshots
                        WHERE project_id = %s AND tenant_id = %s
                        ORDER BY file_path, scan_at DESC
                        LIMIT %s
                    """, (project_id, tenant_id, limit))
                    rows = cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception:
            return []

    def trend(self, project_id: str, tenant_id: str,
              file_path: str, limit: int = 30) -> list[dict]:
        """
        Return entropy score history for a single file (oldest first).
        """
        if not self._db:
            return []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT entropy_score, label, complexity, max_fn_lines,
                               file_lines, coupling, test_coverage, scan_at
                        FROM entropy_snapshots
                        WHERE project_id = %s AND tenant_id = %s
                          AND file_path = %s
                        ORDER BY scan_at ASC
                        LIMIT %s
                    """, (project_id, tenant_id, file_path, limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def project_trend(self, project_id: str, tenant_id: str,
                      limit: int = 30) -> list[dict]:
        """
        Return average entropy per scan run for the whole project (time series).
        Each entry = one scan point {scan_at, avg_entropy, critical, refactor, watch, healthy}.
        """
        if not self._db:
            return []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            DATE_TRUNC('hour', scan_at)                            AS scan_at,
                            ROUND(AVG(entropy_score)::NUMERIC, 4)                  AS avg_entropy,
                            COUNT(*) FILTER (WHERE label = 'structural_hazard')    AS structural_hazard,
                            COUNT(*) FILTER (WHERE label = 'critical')             AS critical,
                            COUNT(*) FILTER (WHERE label = 'refactor')             AS refactor,
                            COUNT(*) FILTER (WHERE label = 'watch')                AS watch,
                            COUNT(*) FILTER (WHERE label = 'healthy')              AS healthy
                        FROM entropy_snapshots
                        WHERE project_id = %s AND tenant_id = %s
                        GROUP BY DATE_TRUNC('hour', scan_at)
                        ORDER BY scan_at ASC
                        LIMIT %s
                    """, (project_id, tenant_id, limit))
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ──────────────────────────────────────────
    # TEMPORAL ANALYSIS
    # ──────────────────────────────────────────

    def entropy_velocity(
        self,
        project_id: str,
        tenant_id:  str,
        window:     int = 5,   # number of most-recent scan points to analyse
    ) -> dict:
        """
        Compute entropy velocity and acceleration for a project.

        Temporal model
        ──────────────
          V(t) = E(t) - E(t-1)             entropy velocity   (rate of change)
          A(t) = V(t) - V(t-1)             entropy acceleration
          predicted_E(t+1) = E(t) + V(t)  next-scan forecast

        Interpretation
          V > 0   → degrading  (architecture is getting worse)
          V < 0   → improving  (refactoring is working)
          A > 0   → accelerating degradation (urgent)
          A < 0   → degradation slowing down (positive signal)

        Returns a dict with keys:
          points         — list of {scan_at, avg_entropy, velocity, acceleration}
          current_velocity
          current_acceleration
          trend          — "degrading" | "improving" | "stable"
          urgency        — "critical" | "high" | "medium" | "low"
          predicted_next — forecasted average entropy on next scan
        """
        series = self.project_trend(project_id, tenant_id, limit=window + 2)
        if len(series) < 2:
            return {
                "points": series,
                "current_velocity": 0.0,
                "current_acceleration": 0.0,
                "trend": "stable",
                "urgency": "low",
                "predicted_next": series[-1]["avg_entropy"] if series else None,
            }

        # Compute per-point velocity
        enriched: list[dict] = []
        for i, pt in enumerate(series):
            v = round(pt["avg_entropy"] - series[i - 1]["avg_entropy"], 4) if i > 0 else 0.0
            enriched.append({**pt, "velocity": v, "acceleration": 0.0})

        # Compute per-point acceleration
        for i in range(1, len(enriched)):
            enriched[i]["acceleration"] = round(
                enriched[i]["velocity"] - enriched[i - 1]["velocity"], 4
            )

        cur_v = enriched[-1]["velocity"]
        cur_a = enriched[-1]["acceleration"]
        cur_e = enriched[-1]["avg_entropy"]

        # Trend classification
        if cur_v > 0.02:
            trend = "degrading"
        elif cur_v < -0.02:
            trend = "improving"
        else:
            trend = "stable"

        # Urgency: combines current score level + velocity
        if cur_e >= 0.70 and cur_v > 0.01:
            urgency = "critical"
        elif cur_e >= 0.60 or cur_v > 0.03:
            urgency = "high"
        elif cur_v > 0.01:
            urgency = "medium"
        else:
            urgency = "low"

        predicted_next = round(min(cur_e + cur_v, 1.0), 4)

        return {
            "points":               enriched[-window:],
            "current_velocity":     cur_v,
            "current_acceleration": cur_a,
            "trend":                trend,
            "urgency":              urgency,
            "predicted_next":       predicted_next,
        }

    def file_velocity(
        self,
        project_id: str,
        tenant_id:  str,
        file_path:  str,
        window:     int = 10,
    ) -> dict:
        """
        Per-file entropy velocity — same model as project-level,
        but computed from the single-file trend series.

        Returns {points, current_velocity, current_acceleration, trend, predicted_next}.
        """
        series = self.trend(project_id, tenant_id, file_path, limit=window + 2)
        if len(series) < 2:
            return {
                "points": series,
                "current_velocity": 0.0,
                "current_acceleration": 0.0,
                "trend": "stable",
                "predicted_next": series[-1]["entropy_score"] if series else None,
            }

        enriched: list[dict] = []
        for i, pt in enumerate(series):
            v = round(pt["entropy_score"] - series[i - 1]["entropy_score"], 4) if i > 0 else 0.0
            enriched.append({**pt, "velocity": v, "acceleration": 0.0})

        for i in range(1, len(enriched)):
            enriched[i]["acceleration"] = round(
                enriched[i]["velocity"] - enriched[i - 1]["velocity"], 4
            )

        cur_v = enriched[-1]["velocity"]
        cur_a = enriched[-1]["acceleration"]
        cur_e = enriched[-1]["entropy_score"]
        trend = "degrading" if cur_v > 0.02 else ("improving" if cur_v < -0.02 else "stable")

        return {
            "points":               enriched[-window:],
            "current_velocity":     cur_v,
            "current_acceleration": cur_a,
            "trend":                trend,
            "predicted_next":       round(min(cur_e + cur_v, 1.0), 4),
        }

    # ──────────────────────────────────────────
    # TASK SEEDER
    # ──────────────────────────────────────────

    def seed_tasks(
        self,
        project_id:   str,
        tenant_id:    str,
        threshold:    float = THRESHOLD_SEED,
        auto_assign:  bool  = False,
    ) -> list[dict]:
        """
        For every file above `threshold` that has no open repair task,
        create one or more repair tasks in PostgreSQL.
        Returns list of created tasks {task_id, file, score, type}.
        Returns empty list if self._db is None.
        """
        if not self._db:
            return []

        candidates = self.latest_report(project_id, tenant_id)
        high_entropy = [r for r in candidates if r.get("entropy_score", 0) >= threshold]

        created: list[dict] = []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    for r in high_entropy:
                        file_path = r["file_path"]
                        score     = r["entropy_score"]
                        label     = r["label"]

                        # Skip if an open entropy repair task already exists
                        cur.execute("""
                            SELECT 1 FROM tasks
                            WHERE tenant_id = %s AND project_id = %s
                              AND status NOT IN ('done','cancelled','dead-letter')
                              AND title ILIKE %s
                            LIMIT 1
                        """, (tenant_id, project_id, f"%{file_path}%"))
                        if cur.fetchone():
                            continue

                        # Choose repair type based on dominant metric
                        task_type, title, description = _pick_repair(r, file_path, score)

                        task_id = (f"ENTROPY-{int(time.time())}"
                                   f"-{hashlib.md5(file_path.encode()).hexdigest()[:6]}")
                        priority = 1 if label == "critical" else 2

                        cur.execute("""
                            INSERT INTO tasks
                                (id, title, description, status, priority,
                                 project_id, tenant_id, created_at, updated_at)
                            VALUES (%s,%s,%s,'pending',%s,%s,%s, NOW(), NOW())
                            ON CONFLICT (id) DO NOTHING
                        """, (task_id, title, description,
                              priority, project_id, tenant_id))

                        cur.execute("""
                            INSERT INTO agent_events
                                (task_id, agent_name, event_type, payload)
                            VALUES (%s, 'entropy-scanner', 'seeded', %s)
                        """, (task_id, json.dumps({
                            "file": file_path, "entropy": score, "type": task_type
                        })))

                        created.append({
                            "task_id":   task_id,
                            "file":      file_path,
                            "score":     score,
                            "label":     label,
                            "task_type": task_type,
                            "title":     title,
                        })

                    conn.commit()
        except Exception as exc:
            return [{"error": str(exc)}]

        return created

    # ──────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────

    @staticmethod
    def _walk(project_path: str):
        """Yield (abs_path, ext, rel_path) for each source file."""
        for dirpath, dirnames, filenames in os.walk(project_path):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext in _SOURCE_EXTS:
                    abs_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                    yield abs_path, ext, rel_path

    @staticmethod
    def _resolve_import(imp: str, src_file: str, known_files: set[str]) -> Optional[str]:
        """
        Try to match an import string to a known rel_path in the project.
        Returns matched rel_path or None.
        """
        # Convert PHP \ namespace to path
        norm = imp.replace("\\", "/").replace(".", "/")
        for known in known_files:
            # strip extension for comparison
            base = re.sub(r"\.\w+$", "", known)
            if base.endswith(norm) or norm.endswith(base.split("/")[-1]):
                return known
        return None

    @staticmethod
    def _detect_cycles(import_graph: dict[str, list[str]]) -> set[str]:
        """
        DFS-based cycle detection.
        Returns set of file paths that participate in at least one cycle.
        """
        visited:   set[str] = set()
        rec_stack: set[str] = set()
        cyclic:    set[str] = set()

        def dfs(node: str, path: list[str]):
            visited.add(node)
            rec_stack.add(node)
            for nbr in import_graph.get(node, []):
                # Only follow edges to files we actually tracked
                if nbr not in import_graph:
                    continue
                if nbr not in visited:
                    dfs(nbr, path + [nbr])
                elif nbr in rec_stack:
                    # Found a cycle; mark all nodes in the back-path
                    start = path.index(nbr) if nbr in path else 0
                    for n in path[start:]:
                        cyclic.add(n)
                    cyclic.add(nbr)
            rec_stack.discard(node)

        for node in list(import_graph.keys()):
            if node not in visited:
                dfs(node, [node])

        return cyclic

    @staticmethod
    def _map_test_coverage(all_files: dict[str, dict]) -> dict[str, float]:
        """Legacy dict-based test coverage (kept for compatibility)."""
        test_names: set[str] = set()
        for path, meta in all_files.items():
            if meta.get("is_test"):
                base = Path(path).stem.lower()
                base = re.sub(r"(test|spec|tests)$|^(test_?|spec_?)", "", base)
                test_names.add(base)
        coverage: dict[str, float] = {}
        for path, meta in all_files.items():
            if meta.get("is_test"):
                coverage[path] = 1.0
                continue
            base = Path(path).stem.lower()
            coverage[path] = 0.6 if any(base in tn or tn in base for tn in test_names if tn) else 0.0
        return coverage

    @staticmethod
    def _map_test_coverage_list(all_rel: list[str]) -> set[str]:
        """List-based test coverage — returns set of covered rel_paths."""
        test_bases: set[str] = set()
        for rel in all_rel:
            bn   = os.path.basename(rel).lower()
            stem = os.path.splitext(bn)[0]
            if (stem.startswith("test_") or stem.endswith("_test")
                    or re.search(r"\.test$|\.spec$", stem)
                    or "/test" in rel.lower() or "/tests/" in rel.lower()
                    or "/spec/" in rel.lower()):
                clean = re.sub(r"^test_|_test$|\.test$|\.spec$", "", stem, flags=re.I)
                test_bases.add(clean)
        covered: set[str] = set()
        for rel in all_rel:
            stem = os.path.splitext(os.path.basename(rel).lower())[0]
            if stem in test_bases:
                covered.add(rel)
        return covered

    @staticmethod
    def _compute_duplication(fp_list: list[tuple[str, set]]) -> dict[str, float]:
        """
        For each file, compute the fraction of its lines that appear verbatim
        in at least one other file.  O(N²) over line fingerprints — capped at 300 files.
        """
        if len(fp_list) > 300:
            fp_list = fp_list[:300]

        dup_map: dict[str, float] = {}
        for i, (path_a, fps_a) in enumerate(fp_list):
            if not fps_a:
                dup_map[path_a] = 0.0
                continue
            shared = set()
            for j, (path_b, fps_b) in enumerate(fp_list):
                if i == j:
                    continue
                shared |= fps_a & fps_b
            dup_map[path_a] = round(len(shared) / len(fps_a), 4)
        return dup_map


# ──────────────────────────────────────────────
# TASK SEEDER HELPERS
# ──────────────────────────────────────────────

def _pick_repair(metrics: dict, file_path: str, score: float) -> tuple[str, str, str]:
    """
    Choose the most appropriate repair task type based on the dominant metric.
    Returns (task_type, title, description).
    """
    m         = metrics
    fname     = Path(file_path).stem
    score_pct = int(score * 100)

    if m.get("circular_deps"):
        return (
            "circular_dep",
            f"[Entropy] Break circular dependency in {fname}",
            f"File `{file_path}` is part of a circular import chain "
            f"(entropy={score:.2f}). Refactor to remove the cycle and decouple "
            f"the modules. Apply dependency inversion or introduce an interface.",
        )

    if m.get("complexity", 0) >= _CEIL_COMPLEXITY * 0.8:
        return (
            "complexity",
            f"[Entropy] Reduce cyclomatic complexity in {fname}",
            f"File `{file_path}` has cyclomatic complexity "
            f"{m.get('complexity')} (entropy={score:.2f}, risk={score_pct}%). "
            f"Extract methods, simplify conditionals, or split into smaller units.",
        )

    if m.get("max_fn_lines", 0) >= _CEIL_FN_LINES * 0.8:
        return (
            "fn_size",
            f"[Entropy] Split large functions in {fname}",
            f"File `{file_path}` contains a function of "
            f"{m.get('max_fn_lines')} lines (entropy={score:.2f}). "
            f"Break it into focused, single-responsibility functions.",
        )

    if m.get("test_coverage", 1.0) < 0.1:
        return (
            "no_tests",
            f"[Entropy] Add unit tests for {fname}",
            f"File `{file_path}` has no test coverage detected "
            f"(entropy={score:.2f}). Write unit tests covering its public "
            f"interface and key edge cases.",
        )

    if m.get("coupling", 0) >= _CEIL_COUPLING * 0.8:
        return (
            "coupling",
            f"[Entropy] Reduce coupling in {fname}",
            f"File `{file_path}` is imported by "
            f"{m.get('coupling')} modules (entropy={score:.2f}). "
            f"Consider splitting responsibilities or introducing a facade/interface.",
        )

    if m.get("duplication", 0) > 0.3:
        return (
            "duplication",
            f"[Entropy] Remove code duplication in {fname}",
            f"File `{file_path}` has ~{int(m.get('duplication',0)*100)}% of its lines "
            f"duplicated across the codebase (entropy={score:.2f}). "
            f"Extract shared logic into reusable functions or services.",
        )

    # Fallback: general refactor
    return (
        "refactor",
        f"[Entropy] Refactor {fname} (entropy={score:.2f})",
        f"File `{file_path}` has an entropy score of {score:.2f} ({score_pct}% risk). "
        f"Metrics — complexity: {m.get('complexity')}, "
        f"max fn lines: {m.get('max_fn_lines')}, "
        f"file lines: {m.get('file_lines')}, "
        f"coupling: {m.get('coupling')}, "
        f"test coverage: {m.get('test_coverage', 0):.0%}. "
        f"Refactor to reduce complexity and improve maintainability.",
    )


# ──────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ──────────────────────────────────────────────

_scanner: Optional[EntropyScanner] = None


def get_scanner(db_conn_fn: Optional[Callable] = None) -> EntropyScanner:
    global _scanner
    if _scanner is None:
        _scanner = EntropyScanner(db_conn_fn=db_conn_fn)
    return _scanner


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd          = sys.argv[1] if len(sys.argv) > 1 else "help"
    project_path = sys.argv[2] if len(sys.argv) > 2 else "."
    project_id   = env_get("PROJECT_ID", default="default")
    tenant_id    = env_get("TENANT_ID", default="local")
    threshold    = float(env_get("ENTROPY_THRESHOLD_SEED", default="0.70"))

    scanner = EntropyScanner()  # no DB in CLI mode

    if cmd == "scan":
        print(f"[entropy] scanning {project_path}  project={project_id}")
        results = scanner.scan_project(
            project_path, project_id, tenant_id,
            on_progress=lambda p, m: None,
        )
        print(f"\n{'ENTROPY':>8}  {'LABEL':>10}  FILE")
        print("-" * 70)
        for r in results[:50]:
            print(f"{r['entropy_score']:>8.3f}  {r['status']} {r['label']:>8}  {r['file_path']}")
        print(f"\n{len(results)} files scanned.")

    elif cmd == "report":
        print(json.dumps(
            scanner.scan_project(project_path, project_id, tenant_id),
            indent=2, default=str,
        ))

    else:
        print("Usage: python entropy_scanner.py <cmd> <project_path>")
        print("  scan   <project_path>  — print entropy table")
        print("  report <project_path>  — full JSON report")
        print()
        print("Env: PROJECT_ID, TENANT_ID, ENTROPY_THRESHOLD_SEED")
        print("     ENTROPY_W_COMPLEXITY, ENTROPY_W_FN_SIZE, ENTROPY_W_COVERAGE ...")

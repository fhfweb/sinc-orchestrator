# SINC Structural Entropy Model — Specification v1

## 1. Motivation

Static analysis tools (ESLint, SonarQube, Pylint) measure the **current state** of
individual files. They answer: *"Is this file well-written?"*

The SINC entropy model answers a different question:

> *"Is this system structurally healthy — and is it getting better or worse?"*

This distinction places SINC in a different category: **architectural observability**,
not static analysis.

---

## 2. Formal Definition

### 2.1 File-Level Entropy

For a source file `f`, the structural entropy score is:

```
E(f) = Σ wᵢ · nᵢ(f)       where Σwᵢ = 1.0,  E(f) ∈ [0, 1]
```

| Component | Symbol | Weight | Formula | Theoretical basis |
|---|---|---|---|---|
| Cyclomatic complexity | n_cc | 0.27 | Z-score (project-relative) | McCabe 1976; bug density predictor (Nagappan et al., 2006) |
| Function size | n_fs | 0.14 | Z-score (project-relative) | Single Responsibility; cognitive load |
| File size | n_sz | 0.07 | Z-score (project-relative) | God-object signal |
| Martin instability | n_I | 0.12 | Ce / (Ca + Ce) | Robert C. Martin, "Clean Architecture" |
| Blast-radius weight | n_bw | 0.10 | Ca / max_Ca(project) | Change propagation risk |
| Test coverage gap | n_cv | 0.13 | 1 − has_tests(f) | Risk exposure without regression net |
| Circular dependency | n_ci | 0.05 | is_in_cycle(f) ? 1 : 0 | DFS cycle detection |
| Duplication ratio | n_du | 0.05 | line_overlap(f, project) | DRY violation; divergence risk |
| Dependency entropy | n_Hd | 0.07 | H̃(import packages) | Shannon H — the only true information-theoretic entropy |

### 2.1.1 Z-Score Normalization

For the three raw count metrics (complexity, function size, file size), normalization is
computed per-project rather than against fixed ceilings:

```
z(f) = (x(f) − μ_project) / σ_project

n(f) = clamp( (z(f) + 2) / 4,  0,  1 )
```

| z value | n value | Interpretation |
|---|---|---|
| −2 or below | 0.00 | Well below project average |
| 0 | 0.50 | Exactly average |
| +2 or above | 1.00 | Extreme outlier |

This makes the model measure **structural anomaly score** — how much each file
deviates from its own project's norms.  A function with cyclomatic complexity 60
in a game engine (project average 55) scores near 0.50; the same function in a
web app (project average 8) scores near 1.00.

### 2.2 Coupling Metrics (Martin, 1994)

```
Ce(f)  = efferent coupling  = count of project files that f imports
Ca(f)  = afferent coupling  = count of project files that import f

I(f)   = Ce(f) / (Ca(f) + Ce(f))        ∈ [0, 1]

I → 0  stable   (many depend on this file; high blast radius)
I → 1  unstable (this file depends on many; likely accumulating debt)
```

### 2.3 Architectural Zones (Martin's Main Sequence)

```
D(f) = |A(f) + I(f) - 1|               ∈ [0, 1]

A(f) = abstractness = abstract_elements / total_elements
     ≈ interface/protocol ratio  (approximated from file structure)

D → 0  On the Main Sequence       — balanced, healthy
D → 1  Zone of Pain               — stable + concrete  (hard to change)
       Zone of Uselessness        — unstable + abstract (nobody uses it)
```

### 2.4 Blast-Radius Weight

```
bw(f) = Ca(f) / max{ Ca(g) : g ∈ project }       ∈ [0, 1]
```

Files with `bw → 1` are architectural load-bearing elements.
A change to them propagates through the largest fraction of the codebase.

### 2.5 Entropy Thresholds

| Range | Label | Meaning |
|---|---|---|
| [0.00, 0.35) | **healthy** | Within acceptable structural bounds |
| [0.35, 0.60) | **watch** | Monitor; schedule architectural review |
| [0.60, 0.80) | **refactor** | Active technical debt; queue repair task |
| [0.80, 0.85) | **critical** | Structural risk; block PR, create urgent task |
| [0.85, 1.00] | **structural_hazard** | One metric is catastrophically bad; dominance penalty applied |

### 2.6 Non-Linear Dominance Penalty

The base formula is linear (`E = Σ wᵢ · nᵢ`), which models the average-case
risk surface well. However, **one catastrophically bad dimension is
disproportionately dangerous** compared to many mildly-bad dimensions.

```
E_final = base + λ · max(nᵢ)    iff  max(nᵢ) ≥ θ

θ = 0.85   (dominance threshold)
λ = 0.08   (penalty coefficient)
```

The `dominant_metric` field records which metric triggered the penalty,
enabling targeted repair recommendations.

*Example*: a file with cyclomatic complexity = 90 (n_cc = 1.0) would have
`base ≈ 0.28 + other terms` and gain a penalty of `0.08 × 1.0 = +0.08`,
pushing a borderline `refactor` score into `structural_hazard`.

---

## 3. Temporal Model

### 3.1 Entropy Velocity

```
V(t) = E(t) - E(t-1)
```

| Value | Interpretation |
|---|---|
| V > 0.02 | Degrading — architecture getting worse |
| -0.02 ≤ V ≤ 0.02 | Stable |
| V < -0.02 | Improving — refactoring is working |

### 3.2 Entropy Acceleration

```
A(t) = V(t) - V(t-1)
```

| Value | Interpretation |
|---|---|
| A > 0 | Degradation accelerating — requires immediate intervention |
| A ≈ 0 | Linear drift |
| A < 0 | Degradation slowing — refactor effort is effective |

### 3.3 Next-Scan Forecast

```
Ê(t+1) = E(t) + V(t)
```

A simple linear extrapolation. Useful for sprint-level planning: *"At current velocity,
this module will cross the critical threshold in approximately N scans."*

### 3.4 Urgency Classification

```
urgency = critical  if E(t) ≥ 0.70 AND V(t) > 0.01
urgency = high      if E(t) ≥ 0.60 OR  V(t) > 0.03
urgency = medium    if V(t) > 0.01
urgency = low       otherwise
```

---

## 4. Project-Level Aggregation

```
E_project(t) = (1/N) Σ E(fᵢ, t)          arithmetic mean
p50, p90                                    percentile distribution

hotspots = { f : Ca(f) > threshold }       high blast-radius files
```

The p90 entropy is a more conservative signal than the mean — it captures the
worst 10% of the codebase regardless of project size.

---

## 5. Change Coupling (Churn Analysis)

When a git repository is available, SINC enriches the entropy model with
commit-history data.

### 5.1 File Churn

```
churn(f) = count of git commits that modified file f
```

Normalized: `n_churn(f) = min(churn(f) / 50, 1.0)`, applied as a small
boost (`+0.04 × n_churn`) to the final entropy score.

High churn combined with high structural entropy signals a **hot zone** —
a file being actively changed that is simultaneously structurally degraded.
This is the highest-priority class of refactor candidate.

### 5.2 Co-Change Pairs

```
co_change(f, g) = count of commits where BOTH f AND g were modified

co_change_score(f) = max{ co_change(f, g) / churn(f) : g ≠ f }
```

A `co_change_score → 1.0` means f is *almost always* changed alongside
some partner g, revealing **implicit coupling** not visible in static
import analysis.

This pattern typically indicates:
- Two files that should be one module (merge candidate)
- A hidden shared responsibility (extract interface candidate)
- Shotgun surgery smell: changing X always requires changing Y

*Implementation*: `git log --name-only --diff-filter=AM`, capped at 500
most-recent commits. O(C × F²) pairwise bounded by commit cap.

## 6. Duplication Detection

Line-level fingerprinting with O(N²) pairwise comparison, capped at 300 files:

```
FP(f) = { strip(line) : line ∈ f, len(line) ≥ 6, not a comment }

dup(f) = max{ |FP(f) ∩ FP(g)| / |FP(f)| : g ∈ project, g ≠ f }
```

This captures *copy-paste debt* between files without requiring a full AST parse.

---

## 6. Circular Dependency Detection

Depth-first search over the project import graph:

```
G = (V, E)   where V = source files, E = import relationships

cycle(f) = True  iff  f is a member of any strongly connected component |SCC| > 1
```

DFS with a recursion stack; time complexity O(V + E).

---

## 7. Relationship to Digital Twin

In the full Orchestrator deployment, the entropy scanner integrates with the
Neo4j-based Digital Twin graph:

```
(:File)-[:IMPORTS]->(:File)           import graph
(:Test)-[:TESTS]->(:Function)         test coverage edges
(:Task)-[:MODIFIES]->(:File)          change history
(:Service)-[:DEPENDS_ON]->(:Service)  infra coupling
```

This enables:
- **Precise Ca/Ce** from graph traversal (vs heuristic import matching in CLI mode)
- **Test coverage at function level** (vs file-level heuristic)
- **Churn-weighted entropy**: files modified by many tasks get elevated scores
- **Cross-service blast radius**: change propagation through infrastructure layers

---

## 8. Comparison with Existing Tools

| Dimension | ESLint / Pylint | SonarQube | SINC |
|---|---|---|---|
| Unit of analysis | File / line | File / function | File + graph position |
| Coupling awareness | No | Partial | Yes (Ca, Ce, I) |
| Blast-radius scoring | No | No | Yes |
| Circular dep detection | No | Partial | Yes (DFS) |
| Temporal model | No | No | Yes (V, A, forecast) |
| Architecture zones | No | No | Yes (Martin's model) |
| Requires compilation | No | Yes (some) | No |
| Works offline | Yes | No | Yes |
| Seeding repair tasks | No | No | Yes |

---

## 9. Known Limitations and Future Work

1. **Abstractness (A)** is approximated. A full A metric requires detecting
   abstract classes, interfaces, and protocols — language-specific AST parsing.

2. **Churn data** (commit frequency per file) would strengthen velocity signals.
   Currently velocity is computed from scan-to-scan snapshots, not git history.

3. **Test coverage ratio** is heuristic (file name matching). Integration with
   coverage.py, Istanbul, or similar tools would give per-function coverage.

4. **Duplication is capped at 300 files** to avoid O(N²) blow-up.
   LSH (Locality-Sensitive Hashing) would scale this to large monorepos.

5. **Weight calibration** is empirical. A supervised learning approach trained
   on historical bug density data (e.g., defect prediction datasets) could
   produce statistically validated weights.

"""
semantic_batcher.py
===================
Semantic Batch Executor — Plano Avançado v2.

Groups tasks by semantic similarity, enabling:
  - Shared context inference across similar tasks
  - Prioritized batch execution (P1 before P2)
  - Reduced redundant LLM calls for near-duplicate tasks

Clustering strategy:
  1. HDBSCAN (preferred) — density-based, handles noise, no k required
  2. Agglomerative clustering (fallback) — distance threshold

Public API
----------
  batcher = SemanticBatcher()
  groups  = batcher.group_tasks(tasks)           # List[List[str]] — task_id groups
  plan    = batcher.batch_plan(tasks)            # BatchPlan with priority ordering
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

log = logging.getLogger("orch.batcher")

# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class BatchGroup:
    cluster_id:  int
    task_ids:    list[str]
    priority:    int   = 2    # min priority in group (lower = more urgent)
    centroid:    Optional[list[float]] = field(default=None, repr=False)
    label:       str   = ""   # dominant task_type in group


@dataclass
class BatchPlan:
    groups:         list[BatchGroup]
    total_tasks:    int
    total_clusters: int
    noise_tasks:    list[str]   # HDBSCAN label -1 (no cluster)


# ── Embedding helper ──────────────────────────────────────────────────────────

def _get_embeddings(texts: list[str]) -> Optional[np.ndarray]:
    """Embed a list of texts; returns numpy array or None if unavailable."""
    try:
        from services.context_retriever import _embed_query
        vecs = [_embed_query(t) for t in texts]
        if any(v is None for v in vecs):
            return None
        arr = np.array(vecs, dtype=np.float32)
        # L2-normalise so cosine ≈ dot product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return arr / norms
    except Exception as exc:
        log.debug("embedding_failed error=%s", exc)
        return None


# ── Clustering ────────────────────────────────────────────────────────────────

def _cluster_hdbscan(X: np.ndarray, min_cluster_size: int) -> np.ndarray:
    """HDBSCAN — handles outliers (label = -1)."""
    import hdbscan
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        metric="euclidean",      # on L2-normalised vectors ≈ cosine
        cluster_selection_epsilon=0.15,
    )
    return clusterer.fit_predict(X)


def _cluster_agglomerative(X: np.ndarray, threshold: float) -> np.ndarray:
    """Agglomerative — deterministic fallback."""
    from sklearn.cluster import AgglomerativeClustering
    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=threshold,
    )
    return model.fit_predict(X)


# ── SemanticBatcher ───────────────────────────────────────────────────────────

class SemanticBatcher:
    """
    Groups tasks by semantic similarity of title + description.

    Parameters
    ----------
    similarity_threshold : float
        For agglomerative fallback: cosine similarity cutoff (default 0.85).
    min_cluster_size : int
        HDBSCAN minimum points per cluster (default 2).
    """

    def __init__(self,
                 similarity_threshold: float = 0.85,
                 min_cluster_size: int = 2):
        self.similarity_threshold = similarity_threshold
        self.min_cluster_size     = min_cluster_size

    # ── Core grouping ─────────────────────────────────────────────────────────

    def group_tasks(self, tasks: list[dict[str, Any]]) -> list[list[str]]:
        """
        Group tasks by semantic similarity.
        Returns a list of task_id groups (each group is a list of IDs).
        Ungrouped tasks appear as singleton groups.
        """
        if not tasks:
            return []
        if len(tasks) == 1:
            return [[tasks[0]["id"]]]

        texts = [
            f"{t.get('title', '')} {t.get('description', '')}".strip()
            for t in tasks
        ]
        X = _get_embeddings(texts)

        if X is None:
            # No embeddings — each task is its own group
            return [[t["id"]] for t in tasks]

        labels = self._cluster(X)

        groups: dict[int, list[str]] = {}
        for idx, label in enumerate(labels):
            groups.setdefault(int(label), []).append(tasks[idx]["id"])

        # label -1 are HDBSCAN noise — each becomes singleton
        noise = groups.pop(-1, [])
        result = list(groups.values()) + [[tid] for tid in noise]
        return result

    # ── Full batch plan ───────────────────────────────────────────────────────

    def batch_plan(self, tasks: list[dict[str, Any]]) -> BatchPlan:
        """
        Build a prioritised BatchPlan from a list of tasks.
        Groups are sorted by min-priority (P1 before P2 before P3).
        """
        if not tasks:
            return BatchPlan([], 0, 0, [])

        texts  = [
            f"{t.get('title', '')} {t.get('description', '')}".strip()
            for t in tasks
        ]
        id_map = {t["id"]: t for t in tasks}
        X      = _get_embeddings(texts)

        if X is None:
            # Degenerate: one group per task, sorted by priority
            groups = [
                BatchGroup(
                    cluster_id = i,
                    task_ids   = [t["id"]],
                    priority   = int(t.get("priority", 2)),
                    label      = t.get("task_type", "generic"),
                )
                for i, t in enumerate(tasks)
            ]
            groups.sort(key=lambda g: g.priority)
            return BatchPlan(
                groups         = groups,
                total_tasks    = len(tasks),
                total_clusters = len(groups),
                noise_tasks    = [],
            )

        labels = self._cluster(X)

        raw_groups: dict[int, list[int]] = {}
        for idx, label in enumerate(labels):
            raw_groups.setdefault(int(label), []).append(idx)

        noise_ids = []
        batch_groups: list[BatchGroup] = []

        for label, indices in raw_groups.items():
            tids = [tasks[i]["id"] for i in indices]

            if label == -1:
                noise_ids = tids
                continue

            # Priority = minimum priority number across group (1 = critical)
            group_prios = [int(id_map[tid].get("priority", 2)) for tid in tids]
            min_prio    = min(group_prios)

            # Dominant task type
            from collections import Counter
            type_counts = Counter(
                id_map[tid].get("task_type", "generic") for tid in tids
            )
            dominant = type_counts.most_common(1)[0][0]

            # Centroid
            vecs     = X[indices]
            centroid = vecs.mean(axis=0).tolist()

            batch_groups.append(BatchGroup(
                cluster_id = label,
                task_ids   = tids,
                priority   = min_prio,
                centroid   = centroid,
                label      = dominant,
            ))

        # Add noise as singletons (priority from task)
        for tid in noise_ids:
            t = id_map[tid]
            batch_groups.append(BatchGroup(
                cluster_id = -1,
                task_ids   = [tid],
                priority   = int(t.get("priority", 2)),
                label      = t.get("task_type", "generic"),
            ))

        # Sort: P1 first, then P2, then P3; within same priority sort by size (larger first)
        batch_groups.sort(key=lambda g: (g.priority, -len(g.task_ids)))

        return BatchPlan(
            groups         = batch_groups,
            total_tasks    = len(tasks),
            total_clusters = len([g for g in batch_groups if g.cluster_id != -1]),
            noise_tasks    = noise_ids,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cluster(self, X: np.ndarray) -> np.ndarray:
        """Try HDBSCAN, fall back to Agglomerative."""
        try:
            return _cluster_hdbscan(X, self.min_cluster_size)
        except ImportError:
            pass
        except Exception as exc:
            log.debug("hdbscan_failed fallback_agglomerative error=%s", exc)

        try:
            return _cluster_agglomerative(X, 1.0 - self.similarity_threshold)
        except Exception as exc:
            log.warning("agglomerative_failed singleton_fallback error=%s", exc)
            # Last resort: all in one cluster
            return np.zeros(len(X), dtype=int)


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mock_tasks = [
        {"id": "T1", "title": "Fix login 500",
         "description": "POST /login crashes with 500", "task_type": "fix_bug", "priority": 1},
        {"id": "T2", "title": "Fix register 500",
         "description": "POST /register crashes with 500", "task_type": "fix_bug", "priority": 1},
        {"id": "T3", "title": "Create users schema",
         "description": "Add users table migration", "task_type": "generate_schema", "priority": 2},
        {"id": "T4", "title": "Add password reset",
         "description": "Implement forgot password flow", "task_type": "add_feature", "priority": 2},
        {"id": "T5", "title": "Write auth unit tests",
         "description": "Test all auth endpoints", "task_type": "create_test", "priority": 3},
    ]

    # Mock embedder to run without Qdrant
    import sys
    import types

    mock_cr = types.ModuleType("context_retriever")

    _base_vecs = {
        "T1": [1.0, 0.0, 0.0, 0.0],
        "T2": [0.9, 0.1, 0.0, 0.0],   # similar to T1
        "T3": [0.0, 1.0, 0.0, 0.0],
        "T4": [0.0, 0.0, 1.0, 0.0],
        "T5": [0.0, 0.0, 0.0, 1.0],
    }
    _call_count = [0]

    def _fake_embed(text: str):
        _call_count[0] += 1
        for tid, vec in _base_vecs.items():
            if f"T{_call_count[0]}" == tid or tid in text:
                return vec
        return [0.5, 0.5, 0.0, 0.0]

    mock_cr._embed_query = _fake_embed
    sys.modules["context_retriever"] = mock_cr

    batcher = SemanticBatcher(min_cluster_size=2)
    plan    = batcher.batch_plan(mock_tasks)

    print(f"Total tasks:    {plan.total_tasks}")
    print(f"Total clusters: {plan.total_clusters}")
    print(f"Noise tasks:    {plan.noise_tasks}")
    print()
    for g in plan.groups:
        print(f"  cluster={g.cluster_id:2d}  prio={g.priority}  "
              f"type={g.label:20s}  tasks={g.task_ids}")

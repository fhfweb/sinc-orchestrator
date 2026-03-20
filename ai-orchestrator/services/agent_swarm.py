"""
agent_swarm.py
==============
Agent Swarm Scheduler — Plano Avançado v2.

Intelligent multi-agent task assignment with:
  - Skill-affinity scoring per agent (task_type → agent match)
  - Load-aware balancing (penalises over-loaded agents)
  - Anti-starvation priority boosting (long-waiting tasks get bumped)
  - Rebalancing — redistribute pending tasks across all agents
  - DB integration — reads live workload from PostgreSQL

Public API
----------
  scheduler = get_scheduler()
  agent     = scheduler.assign(task, available_agents)
  pairs     = scheduler.rebalance(all_agents, pending_tasks)   # [(task_id, agent_name)]
  report    = scheduler.workload_report(agents)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("orch.swarm")

# ── Agent skill profiles ──────────────────────────────────────────────────────
# Affinity score 0.0–1.0 for each (agent_name, task_type) pair.
# Agents not listed for a task_type get a default score of 0.3.

_AGENT_AFFINITY: dict[str, dict[str, float]] = {
    "ai engineer": {
        "fix_bug":         0.90,
        "create_route":    0.90,
        "create_endpoint": 0.90,
        "add_feature":     0.85,
        "refactor":        0.80,
        "review":          0.70,
        "analyze_impact":  0.75,
        "ingest":          0.60,
    },
    "ai engineer frontend": {
        "create_route":    0.85,
        "create_endpoint": 0.80,
        "add_feature":     0.90,
        "create_test":     0.70,
        "review":          0.65,
        "refactor":        0.70,
    },
    "database agent": {
        "generate_schema":  0.95,
        "create_migration": 0.95,
        "analyze_impact":   0.80,
        "refactor":         0.60,
        "ingest":           0.70,
    },
    "ai devops engineer": {
        "ingest":          0.85,
        "analyze_impact":  0.80,
        "fix_bug":         0.65,
        "add_feature":     0.55,
    },
    "code review agent": {
        "review":          0.98,
        "fix_bug":         0.70,
        "refactor":        0.85,
        "analyze_impact":  0.80,
        "create_test":     0.75,
    },
    "ai security engineer": {
        "review":          0.80,
        "fix_bug":         0.75,
        "analyze_impact":  0.85,
        "refactor":        0.65,
    },
    "qa agent": {
        "create_test":     0.98,
        "fix_bug":         0.75,
        "review":          0.70,
        "analyze_impact":  0.65,
    },
    "documentation agent": {
        "review":          0.70,
        "add_feature":     0.65,
        "refactor":        0.60,
        "create_route":    0.55,
        "create_endpoint": 0.55,
    },
}

# Maximum concurrent tasks per agent before load penalty kicks in
_MAX_LOAD = 3

# Anti-starvation: tasks waiting this many seconds get a priority boost
_STARVATION_THRESHOLD_S = 300


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    name:          str
    active_tasks:  int   = 0
    queued_tasks:  int   = 0
    total_done:    int   = 0
    error_rate:    float = 0.0   # 0.0–1.0
    last_seen_s:   float = field(default_factory=time.time)

    @property
    def is_alive(self) -> bool:
        return (time.time() - self.last_seen_s) < 120   # 2-min heartbeat window

    @property
    def load(self) -> int:
        return self.active_tasks + self.queued_tasks


@dataclass
class Assignment:
    task_id:    str
    agent_name: str
    score:      float
    reason:     str


# ── Scoring ───────────────────────────────────────────────────────────────────

def _affinity(agent_name: str, task_type: str) -> float:
    profile = _AGENT_AFFINITY.get(agent_name, {})
    return profile.get(task_type, 0.3)


def _load_penalty(agent: AgentState) -> float:
    """Returns 0.0 (no penalty) to 1.0 (fully saturated)."""
    return min(agent.load / max(_MAX_LOAD, 1), 1.0)


def _starvation_boost(waiting_since_s: Optional[float]) -> float:
    """Extra score for tasks that have been waiting too long."""
    if waiting_since_s is None:
        return 0.0
    waited = time.time() - waiting_since_s
    if waited < _STARVATION_THRESHOLD_S:
        return 0.0
    # Linear ramp: 0 → 0.3 over 10 minutes
    return min(0.3, (waited - _STARVATION_THRESHOLD_S) / 600 * 0.3)


def score_assignment(agent: AgentState, task: dict) -> float:
    """
    Composite score (higher = better fit).

    Weights:
      skill_affinity   : 0.40
      load_available   : 0.30
      priority_urgency : 0.20
      error_penalty    : 0.10
    """
    if not agent.is_alive:
        return -1.0

    task_type = task.get("task_type", "generic")
    priority  = int(task.get("priority", 2))      # 1=critical, 2=important, 3=nice

    skill   = _affinity(agent.name, task_type)                     # 0–1
    avail   = 1.0 - _load_penalty(agent)                           # 0–1
    urgency = (4 - priority) / 3.0                                 # P1→1.0, P3→0.33
    error_p = 1.0 - agent.error_rate                               # 0–1

    boost = _starvation_boost(task.get("created_at_ts"))

    base = (
        0.40 * skill   +
        0.30 * avail   +
        0.20 * urgency +
        0.10 * error_p
    )
    return min(1.0, base + boost)


# ── SwarmScheduler ────────────────────────────────────────────────────────────

class SwarmScheduler:
    """
    Intelligent multi-agent scheduler.

    Operates in two modes:
      assign()     — best agent for a single task (online, ~O(A))
      rebalance()  — global reassignment of all pending tasks (batch, ~O(T×A))
    """

    # ── Single assignment ──────────────────────────────────────────────────────

    def assign(self, task: dict,
               agents: list[AgentState]) -> Optional[Assignment]:
        """
        Pick the best available agent for `task`.
        Returns None if no alive agent is available or all agents are saturated.
        """
        alive = [a for a in agents if a.is_alive]
        if not alive:
            return None

        scored = [(score_assignment(a, task), a) for a in alive]
        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best_agent = scored[0]
        if best_score <= 0:
            return None

        task_type = task.get("task_type", "generic")
        reason = (
            f"skill={_affinity(best_agent.name, task_type):.2f} "
            f"load={best_agent.load}/{_MAX_LOAD} "
            f"score={best_score:.3f}"
        )
        return Assignment(
            task_id    = task.get("id", ""),
            agent_name = best_agent.name,
            score      = round(best_score, 4),
            reason     = reason,
        )

    # ── Global rebalance ───────────────────────────────────────────────────────

    def rebalance(self, agents: list[AgentState],
                  tasks: list[dict]) -> list[Assignment]:
        """
        Globally assign all pending tasks to agents (greedy, priority-first).
        Mutates agent.queued_tasks counts to reflect in-flight assignments.
        Returns list of Assignments.
        """
        if not tasks or not agents:
            return []

        # Sort tasks: P1 first, then by wait time (starvation)
        def _task_sort_key(t):
            prio   = int(t.get("priority", 2))
            waited = time.time() - (t.get("created_at_ts") or time.time())
            return (prio, -waited)

        sorted_tasks = sorted(tasks, key=_task_sort_key)
        assignments: list[Assignment] = []

        for task in sorted_tasks:
            result = self.assign(task, agents)
            if result:
                # Speculatively increment queued_tasks so next iterations
                # account for this pending assignment
                for a in agents:
                    if a.name == result.agent_name:
                        a.queued_tasks += 1
                        break
                assignments.append(result)
            else:
                log.debug("no_agent_available task_id=%s", task.get("id"))

        return assignments

    # ── Workload report ────────────────────────────────────────────────────────

    def workload_report(self, agents: list[AgentState]) -> dict:
        """Summary of current workload distribution."""
        total_active = sum(a.active_tasks for a in agents)
        total_queued = sum(a.queued_tasks for a in agents)
        return {
            "agents": [
                {
                    "name":         a.name,
                    "active_tasks": a.active_tasks,
                    "queued_tasks": a.queued_tasks,
                    "load":         a.load,
                    "error_rate":   a.error_rate,
                    "alive":        a.is_alive,
                }
                for a in agents
            ],
            "total_active": total_active,
            "total_queued": total_queued,
            "agents_alive": sum(1 for a in agents if a.is_alive),
        }

    # ── DB-backed helpers ──────────────────────────────────────────────────────

    def load_agents_from_db(self, tenant_id: str) -> list[AgentState]:
        """
        Build AgentState list from live DB data (agents + task counts).
        Falls back to empty list on DB error.
        """
        try:
            from services.streaming.core.db import db
            with db() as conn:
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
                    cur.execute(
                        """
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = 'public'
                           AND table_name = 'heartbeats'
                        """
                    )
                    heartbeat_cols = {row["column_name"] for row in cur.fetchall()}
                    heartbeat_time_col = "beat_at" if "beat_at" in heartbeat_cols else "updated_at"
                    cur.execute(
                        """
                        SELECT a.name,
                               COUNT(t.{task_pk}) FILTER (WHERE t.status = 'in-progress')  AS active,
                               COUNT(t.{task_pk}) FILTER (WHERE t.status = 'pending')      AS queued,
                               COUNT(t.{task_pk}) FILTER (WHERE t.status = 'done')         AS done,
                               MAX(h.{heartbeat_time_col}) AS last_heartbeat
                        FROM agents a
                        LEFT JOIN tasks t
                            ON t.assigned_agent = a.name AND t.tenant_id = %s
                        LEFT JOIN heartbeats h ON h.agent_name = a.name
                        WHERE a.tenant_id = %s
                        GROUP BY a.name
                        """.format(task_pk=task_pk, heartbeat_time_col=heartbeat_time_col),
                        (tenant_id, tenant_id),
                    )
                    rows = cur.fetchall()

            states = []
            for row in rows:
                last_seen = row.get("last_heartbeat")
                last_seen_s = (
                    last_seen.timestamp()
                    if hasattr(last_seen, "timestamp") else 0.0
                )
                states.append(AgentState(
                    name         = row["name"],
                    active_tasks = row["active"] or 0,
                    queued_tasks = row["queued"] or 0,
                    total_done   = row["done"]   or 0,
                    last_seen_s  = last_seen_s or time.time(),
                ))
            return states
        except Exception as exc:
            log.warning("load_agents_from_db_error error=%s", exc)
            return []

    def load_pending_tasks_from_db(self, tenant_id: str,
                                   limit: int = 200) -> list[dict]:
        """
        Load unassigned pending tasks from DB with created_at_ts for starvation.
        """
        try:
            from services.streaming.core.db import db
            with db() as conn:
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
                    cur.execute(
                        """
                        SELECT {task_pk} AS id, title, description, task_type,
                               priority, project_id,
                               EXTRACT(EPOCH FROM created_at) AS created_at_ts
                        FROM tasks
                        WHERE tenant_id = %s
                          AND status = 'pending'
                          AND assigned_agent IS NULL
                        ORDER BY priority ASC, created_at ASC
                        LIMIT %s
                        """.format(task_pk=task_pk),
                        (tenant_id, limit),
                    )
                    return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            log.warning("load_pending_tasks_error error=%s", exc)
            return []


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[SwarmScheduler] = None


def get_scheduler() -> SwarmScheduler:
    global _instance
    if _instance is None:
        _instance = SwarmScheduler()
    return _instance


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    agents = [
        AgentState("ai engineer",          active_tasks=1, queued_tasks=0),
        AgentState("database agent",        active_tasks=0, queued_tasks=0),
        AgentState("qa agent",              active_tasks=2, queued_tasks=1),
        AgentState("code review agent",     active_tasks=0, queued_tasks=0),
        AgentState("ai security engineer",  active_tasks=3, queued_tasks=2),  # saturated
    ]

    tasks = [
        {"id": "T1", "task_type": "fix_bug",        "priority": 1},
        {"id": "T2", "task_type": "generate_schema", "priority": 2},
        {"id": "T3", "task_type": "create_test",    "priority": 2},
        {"id": "T4", "task_type": "review",          "priority": 3},
        {"id": "T5", "task_type": "fix_bug",         "priority": 1,
         "created_at_ts": time.time() - 600},   # 10 min wait → starvation boost
    ]

    sched = SwarmScheduler()
    print("=== Single assignments ===")
    for t in tasks:
        a = sched.assign(t, agents)
        if a:
            print(f"  {t['id']} ({t['task_type']:20s}) → {a.agent_name:25s}  {a.reason}")
        else:
            print(f"  {t['id']} ({t['task_type']:20s}) → NO AGENT")

    print("\n=== Rebalance ===")
    pairs = sched.rebalance(agents, tasks)
    for p in pairs:
        print(f"  {p.task_id} → {p.agent_name}  score={p.score}")

    print("\n=== Workload report ===")
    report = sched.workload_report(agents)
    print(json.dumps(report, indent=2, default=str))

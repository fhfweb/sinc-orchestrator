import math
import random
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict

log = logging.getLogger("orch.mcts")

# ── Context Utility ──────────────────────────────────────────────────────────

def _get_active_tenant() -> str:
    try:
        from .cognitive_orchestrator import get_context
        return get_context().tenant_id
    except (ImportError, Exception):
        return "local"

# ── Known action templates per task type ──────────────────────────────────────

KNOWN_ACTIONS: dict[str, list[str]] = {
    "create_route":    ["create_file", "define_schema", "add_validation",
                        "add_handler", "add_tests", "register_route"],
    "create_endpoint": ["create_file", "define_schema", "add_validation",
                        "add_handler", "add_tests", "register_route"],
    "fix_bug":         ["reproduce_bug", "isolate_scope", "trace_stack",
                        "apply_fix", "verify_fix", "add_regression_test"],
    "debug":           ["reproduce_bug", "isolate_scope", "trace_stack",
                        "apply_fix", "verify_fix"],
    "refactor":        ["identify_smells", "extract_method", "rename_vars",
                        "reduce_complexity", "add_types", "run_tests"],
    "generate_schema": ["analyze_entities", "define_relationships",
                        "normalize", "add_indexes", "generate_migration"],
    "create_migration": ["analyze_entities", "normalize",
                         "add_indexes", "generate_migration"],
    "create_test":     ["analyze_scope", "write_unit_tests",
                        "write_integration_tests", "run_tests"],
    "add_feature":     ["design_interface", "create_file", "add_handler",
                        "add_tests", "register_route", "update_docs"],
    "review":          ["analyze_scope", "check_patterns",
                        "identify_issues", "suggest_improvements"],
    "analyze_impact":  ["trace_dependencies", "check_callers",
                        "estimate_risk", "report"],
    "ingest":          ["scan_files", "extract_symbols", "embed_vectors",
                        "update_graph", "persist_index"],
}

# Deterministic templates — single best path for well-known types
DETERMINISTIC_TEMPLATES: dict[str, list[str]] = {
    "create_route":    ["create_file", "add_handler", "add_tests", "register_route"],
    "create_endpoint": ["create_file", "add_handler", "add_tests", "register_route"],
    "fix_bug":         ["trace_stack", "apply_fix", "add_regression_test"],
    "generate_schema": ["analyze_entities", "normalize", "generate_migration"],
    "create_migration": ["normalize", "add_indexes", "generate_migration"],
    "create_test":     ["analyze_scope", "write_unit_tests", "run_tests"],
    "ingest":          ["scan_files", "embed_vectors", "update_graph", "persist_index"],
}

# Heuristic weights (Static fallbacks)
_ACTION_PRIORITY: dict[str, float] = {
    "add_tests":            0.90,
    "add_regression_test":  0.90,
    "verify_fix":           0.85,
    "run_tests":            0.85,
    "apply_fix":            0.80,
    "generate_migration":   0.80,
    "persist_index":        0.75,
    "create_file":          0.70,
    "define_schema":        0.75,
    "add_handler":          0.70,
    "register_route":       0.65,
    "embed_vectors":        0.70,
    "update_graph":         0.70,
}

_RISKY_ACTIONS = {"delete_file", "drop_table", "override_auth", "force_push"}

_GOOD_ENDINGS = {
    "add_tests", "add_regression_test", "run_tests", "verify_fix",
    "generate_migration", "persist_index", "update_docs",
}


# ── MCTS Node ─────────────────────────────────────────────────────────────────

@dataclass
class MCTSNode:
    state: dict[str, Any]
    parent: "MCTSNode | None" = field(default=None, repr=False)
    children: list["MCTSNode"] = field(default_factory=list, repr=False)
    visits: int = 0
    wins: float = 0.0
    action: str = ""
    untried_actions: list[str] = field(default_factory=list)

    @property
    def ucb1(self) -> float:
        if self.visits == 0:
            return float("inf")
        parent_visits = self.parent.visits if self.parent else self.visits
        exploitation = self.wins / self.visits
        # Exploration factor: level 5 dynamic adjustment could happen here
        exploration  = math.sqrt(2 * math.log(max(parent_visits, 1)) / self.visits)
        return exploitation + exploration

    @property
    def is_terminal(self) -> bool:
        return self.state.get("depth", 0) >= 6

    @property
    def is_fully_expanded(self) -> bool:
        return len(self.untried_actions) == 0


# ── Strategic Reward Scorer (Pillar III) ──────────────────────────────────────

class DynamicRewardScorer:
    """
    Cognitive Reward Scorer.
    Incorporates Success Rate, Latency (Duration), and Cost (Tokens).
    """
    def __init__(self, tenant_id: Optional[str] = None, success_metrics: Dict[str, Any] = None):
        self.tenant_id = tenant_id or _get_active_tenant()
        self.metrics = success_metrics or {}

    def get_reward(self, action: str, agent_name: str = "orchestrator") -> float:
        # Fallback to static priorities if no real-world data
        static = _ACTION_PRIORITY.get(action, 0.5)
        
        # 1. Success Rate
        success_rate = self.metrics.get(
            f"{agent_name}:{action}:success",
            self.metrics.get(
                f"{agent_name}:all:success",
                self.metrics.get(f"{agent_name}:all", static),
            ),
        )
        
        # 2. Latency Penalty (Pillar III Maturity)
        # We penalize actions that are known to be slow (> 5s)
        avg_ms = self.metrics.get(f"{agent_name}:{action}:ms", 0.0)
        # duration_factor: 1.0 (0ms) down to 0.7 (10s+)
        duration_factor = 1.0 - min(0.3, avg_ms / 10000.0) if avg_ms > 0 else 1.0
        
        # 3. Cost Penalty (Tokens)
        avg_tokens = self.metrics.get(f"{agent_name}:{action}:tokens", 0.0)
        # cost_factor: 1.0 (0 tokens) down to 0.8 (5000+ tokens)
        cost_factor = 1.0 - min(0.2, avg_tokens / 5000.0) if avg_tokens > 0 else 1.0
        
        final_reward = success_rate * duration_factor * cost_factor
        
        # 4. Heuristic adjustments
        if action in _RISKY_ACTIONS:
            final_reward -= 0.4
        
        return max(0.0, min(1.0, final_reward))


# ── MCTS Planner ──────────────────────────────────────────────────────────────

class MCTSPlanner:
    """
    Strategic Monte Carlo Tree Search.
    Evolved to use dynamic rewards & asynchronous context.
    """

    def __init__(self, simulations: int = 100):
        self.simulations = simulations

    async def plan(self, task_type: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Main entry for MCTS planning.
        """
        actions = KNOWN_ACTIONS.get(task_type, [])
        if not actions:
            return {"steps": [], "planner": "llm"}

        context = context or {}
        tenant_id = context.get("tenant_id") or _get_active_tenant()
        
        # 1. Pre-fetch Dynamic Metrics from DB (Level 5 Optimization)
        # Avoids awaiting on every node expansion
        metrics = await self._fetch_metrics(tenant_id, task_type)
        scorer = DynamicRewardScorer(tenant_id, metrics)

        initial = {**context, "task_type": task_type, "depth": 0}
        root    = MCTSNode(state=initial, untried_actions=list(actions))

        # 2. Simulation Loop (Pure Sync and Fast)
        for _ in range(self.simulations):
            node  = self._select(root, actions)
            score = self._simulate(node, actions, scorer)
            self._backpropagate(node, score)

        # 3. Path Selection
        path = []
        current = root
        total_score = 0
        while current.children:
            best = max(current.children, key=lambda n: n.wins / max(n.visits, 1))
            path.append(best.action)
            total_score += (best.wins / max(best.visits, 1))
            current = best
            if current.is_terminal:
                break

        return {
            "steps": path,
            "planner": "mcts",
            "confidence": round(total_score / max(len(path), 1), 3) if path else 0.0,
            "best_agent": metrics.get("_best_agent_", "orchestrator")
        }

    async def _fetch_metrics(self, tenant_id: str, task_type: str) -> Dict[str, Any]:
        """
        Fetch real-world success rates prioritizing real-time durable signals (agent_reputation)
        to avoid the 'Materialized View Illusion'.
        """
        metrics = {}
        try:
            from services.streaming.core.db import async_db
            async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    # 1. Primary Source: agent_reputation (Durable Runtime Signal)
                    # This is updated in real-time by the ReputationEngine.
                    await cur.execute("""
                        SELECT agent_name, 
                               COALESCE(runtime_success_rate, reputation_fit_score, semantic_score, 0.5) AS success_rate,
                               COALESCE(runtime_avg_duration_ms, 0) AS avg_ms,
                               COALESCE(runtime_samples, 0) AS samples
                        FROM agent_reputation
                        WHERE tenant_id = %s OR tenant_id = 'system'
                    """, (tenant_id,))
                    rep_rows = await cur.fetchall()
                    
                    best_agent = "orchestrator"
                    max_score = -1.0
                    
                    for r in rep_rows:
                        agent = r['agent_name']
                        rate = float(r['success_rate'])
                        samples = int(r['samples'])
                        
                        # Thompson Sampling-lite: Add exploration bonus for low-sample agents
                        # New agents (samples < 5) get a 'curiosity' boost
                        exploration_bonus = 0.2 * (1.0 / (samples + 1))
                        effective_rate = min(1.0, rate + exploration_bonus)
                        
                        metrics[f"{agent}:all:success"] = effective_rate
                        metrics[f"{agent}:all:ms"]      = float(r['avg_ms'])
                        
                        if effective_rate > max_score:
                            max_score = effective_rate
                            best_agent = agent
                    
                    # 2. Secondary Source: Fallback/Blended task-specific view
                    # We query it but only use it to REFINE the metrics if it exists
                    await cur.execute("""
                        SELECT agent_name, success_rate, sample_count
                        FROM task_success_prediction
                        WHERE (tenant_id = %s OR tenant_id = 'system') AND task_type = %s
                    """, (tenant_id, task_type))
                    spec_rows = await cur.fetchall()
                    for r in spec_rows:
                        agent = r['agent_name']
                        key = f"{agent}:all:success"
                        if key in metrics:
                            # Blending: 70% Real-time Reputation, 30% Historical Specificity
                            metrics[key] = (metrics[key] * 0.7) + (float(r['success_rate']) * 0.3)
                    
                    metrics["_best_agent_"] = best_agent

        except Exception as e:
            log.warning("metrics_fetch_failed error=%s", e)

        # ── Graph Intelligence GDS Integration (Phase 4.4) ──────────────────────
        try:
            from services.graph_intelligence import get_graph_intelligence
            gi = get_graph_intelligence()
            for agent in metrics.keys():
                if agent.endswith(":all:success"):
                    name = agent.split(":")[0]
                    gds = gi.get_agent_metrics(name, tenant_id)
                    # Blend GDS PageRank into the success rate
                    # PageRank is typically small, we normalize or use it as a multiplier
                    pr_weight = gds.get("pagerank", 0.15)
                    # We'll use PageRank as a 'structural boost' (max 20% impact)
                    metrics[agent] = (metrics[agent] * 0.8) + (min(1.0, pr_weight * 5) * 0.2)
        except Exception as e:
            log.debug("gds_metrics_integration_failed error=%s", e)

        return metrics

    def _select(self, node: MCTSNode, actions: list[str]) -> MCTSNode:
        """UCB1 Selection: Balances exploitation vs exploration."""
        while not node.is_terminal:
            if node.untried_actions:
                return self._expand(node, actions)
            if not node.children:
                break
            node = max(node.children, key=lambda n: n.ucb1)
        return node

    def _expand(self, node: MCTSNode, actions: list[str]) -> MCTSNode:
        action    = node.untried_actions.pop(random.randrange(len(node.untried_actions)))
        new_state = {**node.state, "last_action": action,
                     "depth": node.state.get("depth", 0) + 1}
        child     = MCTSNode(
            state=new_state, parent=node, action=action,
            untried_actions=[a for a in actions if a != action],
        )
        node.children.append(child)
        return child

    def _simulate(self, node: MCTSNode, actions: list[str], scorer: DynamicRewardScorer) -> float:
        state = node.state.copy()
        agent = state.get("assigned_agent", "orchestrator")
        
        # Rollout (limited depth)
        for _ in range(3):
            # Audit finding H2 (MCTS): replace greedy max() with weighted random sampling.
            # This ensures multiple simulations explore different paths.
            weights = [max(0.01, scorer.get_reward(a, agent)) for a in actions]
            best_action = random.choices(actions, weights=weights, k=1)[0]
            state = {**state, "last_action": best_action, "depth": state.get("depth", 0) + 1}
        
        return self._score(state, scorer, agent)

    def _backpropagate(self, node: MCTSNode, score: float):
        while node:
            node.visits += 1
            node.wins   += score
            node          = node.parent

    def _score(self, state: dict, scorer: DynamicRewardScorer, agent: str) -> float:
        last = state.get("last_action", "")
        base_reward = scorer.get_reward(last, agent)
        
        if last in _GOOD_ENDINGS:
            base_reward += 0.2
            
        return max(0.0, min(1.0, base_reward))


# ── Hybrid Planner ────────────────────────────────────────────────────────────

class HybridPlanner:
    """
    Resolution order:
      1. Deterministic template (0ms)
      2. Strategic MCTS         (~5ms, learned metrics)
      3. LLM escalation         (Last resort)
    """

    def __init__(self, mcts: MCTSPlanner | None = None):
        self.mcts = mcts or MCTSPlanner(simulations=100)

    async def plan(self, task_type: str, context: dict[str, Any]) -> dict[str, Any]:
        # 1. Deterministic
        if task_type in DETERMINISTIC_TEMPLATES:
            return {
                "steps":      DETERMINISTIC_TEMPLATES[task_type],
                "planner":    "deterministic",
                "llm_needed": False,
                "hint":       "",
            }

        # 2. Strategic MCTS
        if task_type in KNOWN_ACTIONS:
            result = await self.mcts.plan(task_type, context)
            if result.get("steps"):
                return {
                    **result,
                    "llm_needed": False,
                    "hint": f"Suggested by Strategic MCTS (Confidence: {result.get('confidence')})"
                }

        # 3. LLM
        return {
            "steps":      [],
            "planner":    "llm",
            "llm_needed": True,
            "hint":       f"Unknown task type '{task_type}'. Need LLM for planning."
        }


# ── Convenience singleton ─────────────────────────────────────────────────────

_default_planner: HybridPlanner | None = None

def get_planner() -> HybridPlanner:
    global _default_planner
    if _default_planner is None:
        _default_planner = HybridPlanner()
    return _default_planner


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def test():
        planner = get_planner()
        for ttype in ["fix_bug", "create_route", "unknown_task"]:
            res = await planner.plan(ttype, {"tenant_id": "system"})
            print(f"[{ttype:15}] Planner={res['planner']:15} Steps={len(res['steps'])} Conf={res.get('confidence', 0)}")

    asyncio.run(test())

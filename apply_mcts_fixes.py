import os

file_path = r"g:\Fernando\project0\ai-orchestrator\services\mcts_planner.py"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Refactor _fetch_metrics to prioritize real-time agent_reputation
old_fetch_metrics = """    async def _fetch_metrics(self, tenant_id: str, task_type: str) -> Dict[str, Any]:
        \"\"\"Fetch real-world success rates from postgres (optimized for Elite V2).\"\"\"
        metrics = {}
        try:
            from services.streaming.core.db import async_db
            async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(\"\"\"
                        SELECT agent_name, success_rate, avg_duration_ms, avg_tokens_used, sample_count
                        FROM task_success_prediction
                        WHERE (tenant_id = %s OR tenant_id = 'system') AND task_type = %s
                    \"\"\", (tenant_id, task_type))
                    rows = await cur.fetchall()
                    
                    best_agent = \"orchestrator\"
                    max_score = 0.0
                    for r in rows:
                        agent = r['agent_name']
                        rate  = float(r['success_rate'])
                        dur   = float(r.get('avg_duration_ms') or 0.0)
                        cost  = float(r.get('avg_tokens_used') or 0.0)
                        samples = r['sample_count']
                        
                        # Weighted score: Success rate + Confidence (samples)
                        score = rate * (1 - (1 / (samples + 1)))
                        
                        metrics[f\"{agent}:all:success\"] = rate
                        metrics[f\"{agent}:all:ms\"]      = dur
                        metrics[f\"{agent}:all:tokens\"]  = cost
                        
                        if score > max_score:
                            max_score = score
                            best_agent = agent
                    if not rows:
                        await cur.execute(\"\"\"
                            SELECT agent_name,
                                   COALESCE(runtime_success_rate, reputation_fit_score, semantic_score, 0.5) AS score,
                                   COALESCE(tasks_total, 0) AS sample_count
                            FROM agent_reputation
                            WHERE tenant_id = %s
                            ORDER BY score DESC
                        \"\"\", (tenant_id,))
                        rep_rows = await cur.fetchall()
                        for r in rep_rows:
                            agent = r[\"agent_name\"]
                            rate = float(r[\"score\"])
                            metrics[f\"{agent}:all:success\"] = rate
                    metrics[\"_best_agent_\"] = best_agent

        except Exception:
            pass"""

new_fetch_metrics = """    async def _fetch_metrics(self, tenant_id: str, task_type: str) -> Dict[str, Any]:
        \"\"\"
        Fetch real-world success rates prioritizing real-time durable signals (agent_reputation)
        to avoid the 'Materialized View Illusion'.
        \"\"\"
        metrics = {}
        try:
            from services.streaming.core.db import async_db
            async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    # 1. Primary Source: agent_reputation (Durable Runtime Signal)
                    # This is updated in real-time by the ReputationEngine.
                    await cur.execute(\"\"\"
                        SELECT agent_name, 
                               COALESCE(runtime_success_rate, reputation_fit_score, semantic_score, 0.5) AS success_rate,
                               COALESCE(runtime_avg_duration_ms, 0) AS avg_ms,
                               COALESCE(runtime_samples, 0) AS samples
                        FROM agent_reputation
                        WHERE tenant_id = %s OR tenant_id = 'system'
                    \"\"\", (tenant_id,))
                    rep_rows = await cur.fetchall()
                    
                    best_agent = \"orchestrator\"
                    max_score = -1.0
                    
                    for r in rep_rows:
                        agent = r['agent_name']
                        rate = float(r['success_rate'])
                        samples = int(r['samples'])
                        
                        # Thompson Sampling-lite: Add exploration bonus for low-sample agents
                        # New agents (samples < 5) get a 'curiosity' boost
                        exploration_bonus = 0.2 * (1.0 / (samples + 1))
                        effective_rate = min(1.0, rate + exploration_bonus)
                        
                        metrics[f\"{agent}:all:success\"] = effective_rate
                        metrics[f\"{agent}:all:ms\"]      = float(r['avg_ms'])
                        
                        if effective_rate > max_score:
                            max_score = effective_rate
                            best_agent = agent
                    
                    # 2. Secondary Source: Fallback/Blended task-specific view
                    # We query it but only use it to REFINE the metrics if it exists
                    await cur.execute(\"\"\"
                        SELECT agent_name, success_rate, sample_count
                        FROM task_success_prediction
                        WHERE (tenant_id = %s OR tenant_id = 'system') AND task_type = %s
                    \"\"\", (tenant_id, task_type))
                    spec_rows = await cur.fetchall()
                    for r in spec_rows:
                        agent = r['agent_name']
                        key = f\"{agent}:all:success\"
                        if key in metrics:
                            # Blending: 70% Real-time Reputation, 30% Historical Specificity
                            metrics[key] = (metrics[key] * 0.7) + (float(r['success_rate']) * 0.3)
                    
                    metrics[\"_best_agent_\"] = best_agent

        except Exception as e:
            log.warning(\"metrics_fetch_failed error=%s\", e)"""

content = content.replace(old_fetch_metrics, new_fetch_metrics)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Successfully applied fixes to {file_path}")

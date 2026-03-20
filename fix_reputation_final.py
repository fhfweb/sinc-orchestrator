import os

engine_path = r"g:\Fernando\project0\ai-orchestrator\services\reputation_engine.py"

with open(engine_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Filter out the messy end and rewrite _check_reputation_drift properly
# We keep lines until the start of _check_reputation_drift (approx line 273)
clean_lines = []
in_drift_method = False
for line in lines:
    if "async def _check_reputation_drift" in line:
        in_drift_method = True
        clean_lines.append(line)
        continue
    if in_drift_method:
        if 'if __name__ == "__main__":' in line:
            break
        # Skip we will append the method body
    else:
        clean_lines.append(line)

# Now append the correct method body and footer
method_body = """        \"\"\"
        Phase 11: Reputation Drift Monitoring.
        Detects divergence between Postgres (Truth) and Redis (Cache).
        \"\"\"
        try:
            # 1. Get Postgres success_rate
            pg_rate = 0.5
            async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(\"\"\"
                        SELECT success_rate FROM agent_reputation 
                        WHERE agent_name = %s AND tenant_id = %s
                    \"\"\", (agent_name, tenant_id))
                    row = await cur.fetchone()
                    if row: pg_rate = float(row[0] or 0.5)

            # 2. Get Redis score
            from services.streaming.core.redis_ import async_get_agent_reputation_score
            redis_score = await async_get_agent_reputation_score(agent_name, tenant_id, default=0.5)

            # 3. Calculate drift
            drift = abs(pg_rate - redis_score)
            if drift > 0.3:
                log.warning(\"reputation_drift_detected agent=%s tenant=%s drift=%.2f pg=%.2f redis=%.2f\", 
                            agent_name, tenant_id, drift, pg_rate, redis_score)
        except Exception as e:
            log.debug(\"drift_check_failed error=%s\", e)

if __name__ == \"__main__\":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ReputationEngine().start())
"""

with open(engine_path, 'w', encoding='utf-8') as f:
    f.writelines(clean_lines)
    f.write(method_body)

print("Perfecly cleaned and fixed reputation_engine.py")

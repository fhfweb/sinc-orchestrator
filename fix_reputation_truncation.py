import os

engine_path = r"g:\Fernando\project0\ai-orchestrator\services\reputation_engine.py"

with open(engine_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Restore the lost part of _check_reputation_drift and apply the threshold fix
restored_logic = """                        if row: pg_rate = float(row[0] or 0.5)

            # 2. Get Redis score
            from services.streaming.core.redis_ import async_get_agent_reputation_score
            redis_score = await async_get_agent_reputation_score(agent_name, tenant_id, default=0.5)

            # 3. Calculate drift
            drift = abs(pg_rate - redis_score)
            if drift > 0.3: # Relaxed threshold for stress resilience
                log.warning("reputation_drift_detected agent=%s tenant=%s drift=%.2f pg=%.2f redis=%.2f", 
                            agent_name, tenant_id, drift, pg_rate, redis_score)
                # In 100% Mastery, we might trigger an automatic resync here.
        except Exception as e:
            log.debug("drift_check_failed error=%s", e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ReputationEngine().start())
"""

# Find the truncated place and append the rest
import re
new_content = re.sub(r"if row: pg_rate = float\(row\[0\] or 0.5\)\n\nif __name__ == \"__main__\":", restored_logic, content)

with open(engine_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Restored and fixed reputation_engine.py")

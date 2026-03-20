"""
Seed agent_reputation PostgreSQL table from reputation.json.
Run once after schema v2 migration.

Usage: python seed_reputation.py
"""
import json
import math
import os
from pathlib import Path
import psycopg

BASE = Path(__file__).parent.parent.parent
REPUTATION_FILE = BASE / "agents" / "reputation.json"

DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

def wilson_interval(successes: int, total: int, z: float = 1.96):
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    spread = (z * math.sqrt(p*(1-p)/total + z**2/(4*total**2))) / denom
    return max(0.0, centre - spread), min(1.0, centre + spread)

def seed():
    data = json.loads(REPUTATION_FILE.read_text(encoding="utf-8"))
    agents = data.get("agents", [])
    print(f"Seeding {len(agents)} agents into agent_reputation...")

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            for a in agents:
                samples = a.get("runtime_samples", 0)
                rate    = a.get("runtime_success_rate", 0.5)
                successes = int(rate * samples)
                lower, upper = wilson_interval(successes, samples)
                is_valid = samples >= 100
                level = "high" if samples >= 100 else ("medium" if samples >= 50 else "low")

                cur.execute("""
                    INSERT INTO agent_reputation (
                        agent_name, backend_affinity, frontend_affinity, db_affinity,
                        arch_affinity, qa_affinity, devops_affinity,
                        tasks_total, tasks_success, tasks_failure,
                        runtime_success_rate, runtime_avg_duration_ms, runtime_timeout_rate,
                        reputation_fit_score, runtime_samples,
                        confidence_lower, confidence_upper, confidence_level,
                        is_statistically_valid, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (agent_name) DO UPDATE SET
                        backend_affinity = EXCLUDED.backend_affinity,
                        frontend_affinity = EXCLUDED.frontend_affinity,
                        db_affinity = EXCLUDED.db_affinity,
                        arch_affinity = EXCLUDED.arch_affinity,
                        qa_affinity = EXCLUDED.qa_affinity,
                        devops_affinity = EXCLUDED.devops_affinity,
                        tasks_total = EXCLUDED.tasks_total,
                        tasks_success = EXCLUDED.tasks_success,
                        tasks_failure = EXCLUDED.tasks_failure,
                        runtime_success_rate = EXCLUDED.runtime_success_rate,
                        runtime_avg_duration_ms = EXCLUDED.runtime_avg_duration_ms,
                        runtime_timeout_rate = EXCLUDED.runtime_timeout_rate,
                        reputation_fit_score = EXCLUDED.reputation_fit_score,
                        runtime_samples = EXCLUDED.runtime_samples,
                        confidence_lower = EXCLUDED.confidence_lower,
                        confidence_upper = EXCLUDED.confidence_upper,
                        confidence_level = EXCLUDED.confidence_level,
                        is_statistically_valid = EXCLUDED.is_statistically_valid,
                        updated_at = NOW()
                """, (
                    a["agent"], a.get("backend",0.1), a.get("frontend",0.1),
                    a.get("db",0.1), a.get("arch",0.1), a.get("qa",0.1), a.get("devops",0.1),
                    a.get("tasks_total",0), a.get("tasks_success",0), a.get("tasks_failure",0),
                    rate, a.get("runtime_avg_duration_ms",0), a.get("runtime_timeout_rate",0),
                    a.get("reputation_fit_score",0.5), samples,
                    round(lower,4), round(upper,4), level, is_valid
                ))
                print(f"  {a['agent']}: fit={a.get('reputation_fit_score')}, "
                      f"CI=[{lower:.3f},{upper:.3f}], valid={is_valid}")
        conn.commit()
    print("Done.")

if __name__ == "__main__":
    seed()

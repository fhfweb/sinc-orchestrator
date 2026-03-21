import asyncio
import os
import json
from services.streaming.core.db import async_db

async def audit_system():
    print("--- 🔍 SINC System Audit ---")
    
    # 1. Check Tenants Table Schema
    async with async_db() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")
            rows = await cur.fetchall()
            cols = [r['column_name'] for r in rows]
            print(f"[DB] Tenants table columns: {cols}")
            if 'metadata' not in cols:
                print("!!! [WARNING] Column 'metadata' is MISSING in 'tenants' table.")
            else:
                print("OK: Column 'metadata' exists in 'tenants' table.")

    # 2. Check get_tenant usage patterns
    # (Verified via grep previously, but let's check one more suspicious file)
    
    # 3. Check Qdrant / Ollama Ingestion config
    try:
        from services.streaming.core.config import env_get
        q_host = env_get("QDRANT_HOST", default="localhost")
        print(f"[VEC] Qdrant Host: {q_host}")
        # Test connection? (Optional)
    except:
        print("[VEC] Could not load Qdrant config.")

if __name__ == "__main__":
    asyncio.run(audit_system())

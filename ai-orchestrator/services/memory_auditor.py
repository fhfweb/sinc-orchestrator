from services.streaming.core.config import env_get

import os
import json
from pathlib import Path
from datetime import datetime

from services.http_client import create_sync_resilient_client

# Simple Qdrant + LLM Auditor
class MemoryAuditor:
    def __init__(self, qdrant_url=None, llm_backend="ollama"):
        self.qdrant_url = qdrant_url or env_get("QDRANT_URL", default="http://localhost:6333")
        self.llm_backend = llm_backend

    def _call_llm(self, prompt):
        # Use the same logic as local_agent_runner but simplified
        from services.local_agent_runner import OLLAMA_HOST
        model = "qwen2.5-coder:7b"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        with create_sync_resilient_client(
            service_name="memory-auditor-llm",
            headers={"Content-Type": "application/json"},
            timeout=60,
        ) as client:
            resp = client.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    def audit_collection(self, collection_name="local_sinc_agent_memory"):
        print(f"--- starting audit for collection: {collection_name} ---")
        # 1. Scroll points from Qdrant
        scroll_url = f"{self.qdrant_url}/collections/{collection_name}/points/scroll"
        payload = {"limit": 100, "with_payload": True}
        try:
            with create_sync_resilient_client(
                service_name="memory-auditor-qdrant",
                headers={"Content-Type": "application/json"},
                timeout=30,
            ) as client:
                data = client.post(scroll_url, json=payload).json()
                points = data.get("result", {}).get("points", [])
        except Exception as e:
            print(f"Error scrolling Qdrant: {e}")
            return

        to_delete = []
        for p in points:
            content = p.get("payload", {}).get("content", "")
            if not content: continue
            
            # 2. Ask LLM to evaluate
            audit_prompt = (
                f"AUDIT TASK: Evaluate if this AI memory lesson is still useful or if it is junk/obsolete.\n"
                f"CONTENT: {content}\n\n"
                f"Rules: Return 'DELETE' if it is a generic log, failed attempt with no lesson, or redundant info.\n"
                f"Return 'KEEP' if it contains a clear pattern, fix, or architectural insight.\n"
                f"Decision (DELETE/KEEP):"
            )
            decision = self._call_llm(audit_prompt).upper()
            
            if "DELETE" in decision:
                print(f" [!] Marking for deletion: {p['id'][:8]}...")
                to_delete.append(p["id"])
            else:
                print(f" [+] Keeping: {p['id'][:8]}...")

        # 3. Batch Delete
        if to_delete:
            delete_url = f"{self.qdrant_url}/collections/{collection_name}/points/delete"
            del_payload = {"points": to_delete}
            with create_sync_resilient_client(
                service_name="memory-auditor-delete",
                headers={"Content-Type": "application/json"},
                timeout=30,
            ) as client:
                resp = client.post(delete_url, json=del_payload)
                resp.raise_for_status()
            print(f"--- deleted {len(to_delete)} points ---")
        else:
            print("--- no points to delete ---")

if __name__ == "__main__":
    auditor = MemoryAuditor()
    auditor.audit_collection()

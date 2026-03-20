import sys
import os
import hashlib

runner_path = r"g:\Fernando\project0\ai-orchestrator\services\local_agent_runner.py"

with open(runner_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Orchestrator Protocol v1
old_request_logic = """                    headers = {
                        "Content-Type": "application/json",
                        "X-Api-Key": api_key,
                        "X-Tenant-Id": tenant_id,
                        "X-Trace-Id": task_id
                    }"""

new_request_logic = """                    headers = {
                        "Content-Type": "application/json",
                        "X-Api-Key": api_key,
                        "X-Tenant-Id": tenant_id,
                        "X-Trace-Id": task_id,
                        "X-Protocol-Version": "v1"
                    }"""

content = content.replace(old_request_logic, new_request_logic)

# 2. Embedding Cache (L0 Local)
if "EMBEDDING_CACHE = {}" not in content:
    # Insert near top
    content = content.replace("import time", "import hashlib\nimport time\n\nEMBEDDING_CACHE = {} # L0 Local Cache to mitigate 'Double-Dipping' costs")

old_embed_call = """    def _embed_text(text: str) -> tuple[list[float], str]:
        if not text: return [], "empty text" """

new_embed_call = """    def _embed_text(text: str) -> tuple[list[float], str]:
        if not text: return [], "empty text"
        
        text_hash = hashlib.md5(text.encode()).hexdigest()
        if text_hash in EMBEDDING_CACHE:
            return EMBEDDING_CACHE[text_hash], ""
        """

# Find the start of _embed_text function
import re
match = re.search(r"def _embed_text\(text: str\) -> tuple\[list\[float\], str\]:", content)
if match:
    # We need to find the next line and insert the cache check
    content = content.replace(match.group(0), "def _embed_text(text: str) -> tuple[list[float], str]:\n        text_hash = hashlib.md5(text.encode()).hexdigest()\n        if text_hash in EMBEDDING_CACHE: return EMBEDDING_CACHE[text_hash], \"\"\"")

# And we need to store it in the cache after the call
# Search for _upsert_qdrant calls? No, search for the end of _embed_text

# Actually, let's use a simpler approach for _embed_text:
full_embed_func = """    def _embed_text(text: str) -> tuple[list[float], str]:
        if not text: return [], "empty text"
        model = env_get("EMBEDDING_MODEL", default="text-embedding-3-small")
        try:
            from openai import OpenAI
            client = OpenAI(api_key=env_get("OPENAI_API_KEY"))
            resp = client.embeddings.create(input=[text], model=model)
            return resp.data[0].embedding, ""
        except Exception as e:
            return [], str(e)"""

cached_embed_func = """    def _embed_text(text: str) -> tuple[list[float], str]:
        if not text: return [], "empty text"
        t_hash = hashlib.md5(text.encode()).hexdigest()
        if t_hash in EMBEDDING_CACHE: return EMBEDDING_CACHE[t_hash], ""
        
        model = env_get("EMBEDDING_MODEL", default="text-embedding-3-small")
        try:
            from openai import OpenAI
            client = OpenAI(api_key=env_get("OPENAI_API_KEY"))
            resp = client.embeddings.create(input=[text], model=model)
            vec = resp.data[0].embedding
            EMBEDDING_CACHE[t_hash] = vec
            return vec, ""
        except Exception as e:
            return [], str(e)"""

content = content.replace(full_embed_func, cached_embed_func)

with open(runner_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Applied Protocol v1 and Embedding Cache.")

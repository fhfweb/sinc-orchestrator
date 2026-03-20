import json
import logging
import time
from typing import List, Optional, Tuple, Dict, Any, AsyncGenerator
import httpx
from pydantic import BaseModel, Field
from services.streaming.core.config import env_get

log = logging.getLogger("orch.llm_solver")

class LLMResponse(BaseModel):
    solution: str
    tokens_used: int
    raw_response: Optional[Dict[str, Any]] = None

class LLMSolverService:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.anthropic_key = env_get("ANTHROPIC_API_KEY")

    async def solve(self, description: str, task_type: str, steps: List[str] = [], hint: str = "", tenant_id: str = "local") -> LLMResponse:
        system_msg, prompt = self._build_prompts(description, task_type, steps, hint, tenant_id)
        try:
            if self.anthropic_key: return await self._call_anthropic(system_msg, prompt)
            else: return await self._call_ollama(system_msg, prompt)
        except Exception as e:
            log.error("llm_solve_failed tenant=%s error=%s", tenant_id, e)
            raise

    async def solve_stream(self, description: str, task_type: str, steps: List[str] = [], hint: str = "", tenant_id: str = "local") -> AsyncGenerator[str, None]:
        system_msg, prompt = self._build_prompts(description, task_type, steps, hint, tenant_id)
        if self.anthropic_key:
            async for chunk in self._stream_anthropic(system_msg, prompt): yield chunk
        else:
            async for chunk in self._stream_ollama(system_msg, prompt): yield chunk

    def _build_prompts(self, description, task_type, steps, hint, tenant_id):
        system_msg = (f"You are the Core Cognitive Engine (Elite V2) for tenant '{tenant_id}'.\n"
                      f"Task Type: {task_type}\nObjective: Provide a precise, executable solution.\n")
        prompt = f"Objective: {description}\n"
        if steps: prompt += f"Planned Steps: {' -> '.join(steps)}\n"
        if hint: prompt += f"Context Hint: {hint}\n"
        prompt += "\nSolution:"
        return system_msg, prompt

    async def _call_anthropic(self, sys: str, prompt: str) -> LLMResponse:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        data = {"model": env_get("AGENT_MODEL", default="claude-3-haiku-20240307"), "max_tokens": 1024, "system": sys, "messages": [{"role": "user", "content": prompt}]}
        resp = await self.client.post(url, headers=headers, json=data)
        resp.raise_for_status()
        res_json = resp.json()
        return LLMResponse(solution=res_json["content"][0]["text"], tokens_used=res_json.get("usage", {}).get("total_tokens", 0), raw_response=res_json)

    async def _stream_anthropic(self, sys: str, prompt: str) -> AsyncGenerator[str, None]:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        data = {"model": env_get("AGENT_MODEL", default="claude-3-haiku-20240307"), "max_tokens": 1024, "system": sys, "messages": [{"role": "user", "content": prompt}], "stream": True}
        async with self.client.stream("POST", url, headers=headers, json=data) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        chunk = json.loads(line[5:])
                        if chunk.get("type") == "content_block_delta":
                            yield chunk["delta"].get("text", "")
                    except: pass

    async def _call_ollama(self, sys: str, prompt: str) -> LLMResponse:
        host = env_get("OLLAMA_HOST", default="http://localhost:11434")
        data = {"model": env_get("OLLAMA_MODEL_GENERAL", default="llama3"), "system": sys, "prompt": prompt, "stream": False}
        resp = await self.client.post(f"{host}/api/generate", json=data)
        resp.raise_for_status()
        res_json = resp.json()
        return LLMResponse(solution=res_json.get("response", ""), tokens_used=0, raw_response=res_json)

    async def _stream_ollama(self, sys: str, prompt: str) -> AsyncGenerator[str, None]:
        host = env_get("OLLAMA_HOST", default="http://localhost:11434")
        data = {"model": env_get("OLLAMA_MODEL_GENERAL", default="llama3"), "system": sys, "prompt": prompt, "stream": True}
        async with self.client.stream("POST", f"{host}/api/generate", json=data) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    try:
                        chunk = json.loads(line)
                        yield chunk.get("response", "")
                    except: pass

_instance: Optional[LLMSolverService] = None

def get_llm_solver() -> LLMSolverService:
    global _instance
    if _instance is None:
        from services.http_client import create_resilient_client
        _instance = LLMSolverService(create_resilient_client())
    return _instance

async def solve(description: str, task_type: str, steps: List[str] = [], hint: str = "", tenant_id: str = "local") -> LLMResponse:
    """Global convenience wrapper for the LLM solver."""
    solver = get_llm_solver()
    return await solver.solve(description, task_type, steps, hint, tenant_id)

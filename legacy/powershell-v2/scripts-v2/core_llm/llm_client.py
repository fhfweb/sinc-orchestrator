from __future__ import annotations

import json
import os
import time
from typing import Any

try:
    import litellm
except Exception:  # pragma: no cover - optional dependency
    litellm = None


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class LLMClient:
    """
    Thin wrapper around LiteLLM with explicit "enabled/disabled" semantics.
    """

    def __init__(self, model_override: str | None = None) -> None:
        # Force local model pricing map to avoid remote fetch noise/failure in offline runs.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
        self.model = model_override or os.getenv(
            "ORCHESTRATOR_LLM_MODEL",
            "ollama/llama3:8b",
        )
        self.api_base = (
            os.getenv("ORCHESTRATOR_LLM_API_BASE")
            or os.getenv("OLLAMA_API_BASE")
            or os.getenv("OLLAMA_HOST")
            or "http://127.0.0.1:11434"
        ).strip()
        self.api_key = (
            os.getenv("ORCHESTRATOR_LLM_API_KEY")
            or os.getenv("OLLAMA_API_KEY")
            or "ollama"
        ).strip()
        self.temperature = float(os.getenv("ORCHESTRATOR_LLM_TEMPERATURE", "0.2"))
        self.max_tokens = int(os.getenv("ORCHESTRATOR_LLM_MAX_TOKENS", "2048"))
        self.enabled = _as_bool(os.getenv("ORCHESTRATOR_LLM_ENABLED"), default=False)

        if litellm is None:
            self.enabled = False
            self.disabled_reason = "litellm-not-installed"
        else:
            litellm.drop_params = True
            if hasattr(litellm, "set_verbose"):
                litellm.set_verbose = False
            if hasattr(litellm, "suppress_debug_info"):
                litellm.suppress_debug_info = True
            self.disabled_reason = ""

    def is_enabled(self) -> bool:
        return self.enabled

    def get_disabled_reason(self) -> str:
        if self.enabled:
            return ""
        if self.disabled_reason:
            return self.disabled_reason
        return "llm-disabled-by-config"

    def parse_system_prompt(self, base_prompt: str, context_blocks: dict[str, str]) -> str:
        parts = [base_prompt.strip()]
        for title, content in context_blocks.items():
            if content and content.strip():
                parts.append(f"\n--- {title.upper()} ---\n{content.strip()}\n")
        return "\n".join(parts)

    def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: int | None = None,
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(self.get_disabled_reason())
        if litellm is None:
            raise RuntimeError("litellm-not-installed")

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens_override if max_tokens_override is not None else self.max_tokens,
        }
        is_ollama = self.model.strip().lower().startswith("ollama/")
        if is_ollama:
            if self.api_base:
                kwargs["api_base"] = self.api_base
            if self.api_key:
                kwargs["api_key"] = self.api_key
        if timeout_seconds is not None and timeout_seconds > 0:
            kwargs["timeout"] = timeout_seconds
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        started_at = time.perf_counter()
        response = litellm.completion(**kwargs)
        elapsed = max(time.perf_counter() - started_at, 0.0)
        message = response.choices[0].message

        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [],
            "usage": {},
            "cost_usd": None,
            "duration_seconds": round(elapsed, 6),
            "model": self.model,
            "provider": "ollama" if is_ollama else "unknown",
        }

        usage = getattr(response, "usage", None)
        if usage is not None:
            usage_dict = {}
            for key in ["prompt_tokens", "completion_tokens", "total_tokens"]:
                value = getattr(usage, key, None)
                if value is not None:
                    usage_dict[key] = value
            normalized["usage"] = usage_dict

        try:
            completion_cost = getattr(litellm, "completion_cost", None)
            if callable(completion_cost):
                normalized["cost_usd"] = float(completion_cost(response))
        except Exception:
            normalized["cost_usd"] = None

        for tool_call in getattr(message, "tool_calls", []) or []:
            args: dict[str, Any] = {}
            raw_args = getattr(tool_call.function, "arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except json.JSONDecodeError:
                    args = {}

            normalized["tool_calls"].append(
                {
                    "id": getattr(tool_call, "id", ""),
                    "name": getattr(tool_call.function, "name", ""),
                    "arguments": args,
                }
            )

        return normalized

from services.streaming.core.config import env_get
"""
Inference Router
================
Classifies a prompt and selects the appropriate model tier to minimize cost.

Tiers:
  small  — fast classification, greetings, short lookups
             → OLLAMA_MODEL_GENERAL (qwen2.5:7b)
  medium — structural analysis, architecture, code review, documentation
             → OLLAMA_MODEL_CODE (qwen2.5-coder:14b)
  large  — complex reasoning, bug investigation, multi-file implementation
             → OLLAMA_MODEL_REASONING (qwen2.5:32b)  or  claude-sonnet-4-6

The router also maps task intents to agent subsets (Agent Activation Policy),
so the scheduler only dispatches agents relevant to the request.

Usage:
    from services.inference_router import InferenceRouter
    router = InferenceRouter()
    tier, model, agents = router.route("Fix the authentication bug in UserController")
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from pydantic import BaseModel


# ──────────────────────────────────────────────
# MODEL CONFIGURATION
# ──────────────────────────────────────────────

MODEL_SMALL  = env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M")
MODEL_MEDIUM = env_get("OLLAMA_MODEL_CODE", default="qwen2.5-coder:14b-instruct-q4_K_M")
MODEL_LARGE  = env_get("OLLAMA_MODEL_REASONING", default="qwen2.5:32b-instruct-q4_K_M")

# When Anthropic API key is set, large tier can use Claude instead of local Ollama
MODEL_LARGE_CLOUD = env_get("AGENT_MODEL", default="claude-sonnet-4-6")
USE_CLOUD_FOR_LARGE = bool(env_get("ANTHROPIC_API_KEY", default=""))


# ──────────────────────────────────────────────
# INTENT → AGENTS MAPPING  (Agent Activation Policy)
# ──────────────────────────────────────────────

INTENT_AGENTS: dict[str, list[str]] = {
    "greeting":      [],
    "classification":["AI Product Manager"],

    # Strategy / Architecture
    "architecture":  ["AI Architect", "AI CTO", "Business Analyst"],
    "planning":      ["AI Product Manager", "Business Analyst", "AI Architect"],
    "estimation":    ["Estimation Agent", "Business Analyst"],

    # Construction
    "backend":       ["AI Engineer", "Code Review Agent"],
    "frontend":      ["AI Engineer Frontend", "Code Review Agent"],
    "database":      ["Database Agent", "AI Engineer"],
    "integration":   ["Integration Agent", "AI Engineer"],

    # Quality
    "bug":           ["Code Review Agent", "AI Engineer", "QA Agent"],
    "security":      ["AI Security Engineer", "Code Review Agent"],
    "performance":   ["Performance Agent", "AI Engineer"],
    "testing":       ["QA Agent", "AI Engineer"],
    "code_review":   ["Code Review Agent", "AI Engineer"],

    # Operations
    "devops":        ["AI DevOps Engineer", "DevOps Agent"],
    "deployment":    ["DevOps Agent", "AI DevOps Engineer", "Observability Agent"],
    "observability": ["Observability Agent", "AI DevOps Engineer"],
    "incident":      ["Incident Response Agent", "AI CTO", "Observability Agent"],

    # Intelligence / Meta
    "refactoring":   ["AI Engineer", "Code Review Agent", "Performance Agent"],
    "documentation": ["Documentation Agent", "AI Architect"],
    "memory":        ["Memory Agent", "Learning Agent"],
}

# ──────────────────────────────────────────────
# TIER RULES (evaluated in order — first match wins)
# ──────────────────────────────────────────────

@dataclass
class TierRule:
    tier:    str
    intents: list[str]
    patterns: list[str] = field(default_factory=list)
    """Regex patterns matched against the prompt (case-insensitive)."""


_TIER_RULES: list[TierRule] = [
    # SMALL — trivial requests
    TierRule("small", ["greeting", "classification"], patterns=[
        r"^(oi|olá|hello|hi|hey|status|ping|health)\b",
        r"\blist (tasks|agents|files)\b",
        r"\bstatus do (projeto|sistema|loop)\b",
    ]),

    # MEDIUM — structural analysis, documentation, estimation
    TierRule("medium", ["architecture", "planning", "estimation", "documentation",
                         "code_review", "testing", "refactoring"], patterns=[
        r"\b(arquitetura|architecture|design|refactor|documentat|review|analise|analys)\b",
        r"\b(estimate|estima|prazo|roadmap|sprint|backlog)\b",
        r"\b(test(e|ar|ing)?|qa|quality)\b",
        r"\b(documen|explain|explique|describe|diagrama)\b",
    ]),

    # LARGE — complex reasoning, multi-file implementation, debugging
    TierRule("large", ["bug", "security", "performance", "backend", "frontend",
                        "database", "devops", "incident", "integration"], patterns=[
        r"\b(bug|fix|corrig|erro|error|crash|exception|falha)\b",
        r"\b(implement(ar|e)?|cri(ar|e)|desenvolv|build|create)\b",
        r"\b(security|segurança|vulnerabilid|inject|csrf|xss)\b",
        r"\b(perform(ance)?|optim|slow|lento|bottleneck)\b",
        r"\b(deploy|prod(ução)?|kubernetes|docker|ci/cd|pipeline)\b",
        r"\b(incident|incidente|outage|alert|alarm)\b",
        r"\b(database|migration|migração|schema|sql|query)\b",
    ]),
]


# ──────────────────────────────────────────────
# DETERMINISTIC RULE ENGINE
# ──────────────────────────────────────────────

class DeterministicResult(BaseModel):
    tier: str
    intent: str
    agents: List[str]
    response: Optional[str] = None
    is_deterministic: bool = True

class DeterministicRuleEngine:
    """Handles trivial requests without calling any LLM."""
    def resolve(self, prompt: str) -> Optional[DeterministicResult]:
        p = prompt.lower().strip()
        
        # System Health / Status
        if re.search(r"^(status|ping|health|estado do sistema|versão)\b", p):
            return DeterministicResult(
                tier="small",
                intent="greeting",
                agents=[],
                response="System SINC v3.8 (Cognitive Runtime) is healthy. All engines active."
            )
            
        # Common Listings
        if re.search(r"\blist (tasks|agents|files|tarefas|agentes|arquivos)\b", p):
             return DeterministicResult(
                tier="small",
                intent="classification",
                agents=["AI Product Manager"],
            )

        # Environment / Configuration
        if re.search(r"\b(config|env|variaveis|ambiente)\b", p):
             return DeterministicResult(
                tier="small",
                intent="devops",
                agents=["AI DevOps Engineer"],
            )

        return None


# ──────────────────────────────────────────────
# ROUTER
# ──────────────────────────────────────────────

class InferenceRouter:
    """
    Routes a natural-language prompt to a model tier and agent subset.

    Returns:
        tier    — "small" | "medium" | "large"
        model   — model name string (Ollama or Anthropic)
        backend — "ollama" | "anthropic"
        agents  — list[str] of recommended agent names
        intent  — classified intent label
    """

    def __init__(self):
        self.rule_engine = DeterministicRuleEngine()

    def route(self, prompt: str) -> dict:
        prompt_lc = prompt.lower().strip()
        
        # 1. Try Deterministic Rules first (Cognitive Compression)
        det_res = self.rule_engine.resolve(prompt_lc)
        if det_res:
            return {
                "tier":    det_res.tier,
                "model":   "deterministic-rule",
                "backend": "local-rules",
                "agents":  det_res.agents,
                "intent":  det_res.intent,
                "response": det_res.response,
                "deterministic": True
            }

        # 2. Pattern-based classification
        tier, intent = self._classify(prompt_lc)
        model, backend = self._select_model(tier)
        agents = INTENT_AGENTS.get(intent, [])

        return {
            "tier":    tier,
            "model":   model,
            "backend": backend,
            "agents":  agents,
            "intent":  intent,
        }

    def _classify(self, prompt_lc: str) -> tuple[str, str]:
        """Return (tier, intent) — first matching rule wins."""
        for rule in _TIER_RULES:
            # Check patterns
            for pattern in rule.patterns:
                if re.search(pattern, prompt_lc, re.IGNORECASE):
                    return rule.tier, rule.intents[0]
            # If no patterns defined but we matched some other way, skip
        # Default: large + bug (safest — better to over-provision than under)
        return "large", "backend"

    def _select_model(self, tier: str) -> tuple[str, str]:
        if tier == "small":
            return MODEL_SMALL, "ollama"
        if tier == "medium":
            return MODEL_MEDIUM, "ollama"
        # large
        if USE_CLOUD_FOR_LARGE:
            return MODEL_LARGE_CLOUD, "anthropic"
        return MODEL_LARGE, "ollama"

    def cost_estimate_ms(self, tier: str) -> int:
        """Rough estimated latency per tier (for SLA planning)."""
        return {"small": 3_000, "medium": 15_000, "large": 45_000}.get(tier, 30_000)


# Singleton for import convenience
_router = InferenceRouter()


def route_prompt(prompt: str) -> dict:
    """Module-level convenience wrapper."""
    return _router.route(prompt)


def get_agents_for_intent(intent: str) -> list[str]:
    return INTENT_AGENTS.get(intent, [])


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "status"
    result = route_prompt(prompt)
    print(json.dumps(result, indent=2))

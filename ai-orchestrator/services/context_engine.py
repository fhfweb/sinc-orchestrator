from services.streaming.core.config import env_get
import logging
import json
import asyncio
import os
import time
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

log = logging.getLogger("orch.context_engine")

class ContextEngine:
    """
    Elite Context Engine (Nível 5)
    -----------------------------
    A dynamic memory-active system that:
    1. Ranks items by structural & temporal relevance (not just semantic similarity).
    2. Explicitly manages a "Live Context" with priority-based pruning.
    3. Auto-summarizes low-relevance items to preserve token budget.
    """

    def __init__(self, token_budget: int = 5000):
        self.token_budget = token_budget
        self._summarizer_model = env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M")
        self._priority_weights = {
            "error_pitfall": 1.2,    # Errors are critical
            "code_chunk": 1.0,       # Base code
            "past_solution": 0.9,    # Historical context
            "structural_node": 0.7   # Low level graph data
        }

    def _calculate_potency(self, item: Dict[str, Any]) -> float:
        """
        Elite Scoring: Semantic Score + Type Weight + Centrality + Path Boost + Decay.
        """
        base_score = item.get("score", 0.5)
        type_weight = self._priority_weights.get(item.get("type"), 1.0)
        centrality = item.get("meta", {}).get("centrality", 0.0)
        
        # Recency Decay
        timestamp = item.get("timestamp", time.time())
        if timestamp is None:
            timestamp = time.time()
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.timestamp()
            except Exception:
                timestamp = time.time()
        hours_old = (time.time() - timestamp) / 3600
        decay = 0.5 ** (hours_old / 24)
        
        # Architectural Boost (Folders like core, auth, services get priority)
        path_boost = self._get_architectural_boost(item.get("meta", {}).get("file", ""))
        
        # Formula: (Semantic + Centrality) * Weights * Boost * Decay
        potency = (base_score + (centrality * 0.5)) * type_weight * path_boost * decay
        return round(potency, 4)

    def _get_architectural_boost(self, file_path: str) -> float:
        if not file_path: return 1.0
        boosted_patterns = {
            "services/streaming/core/": 1.25,
            "services/auth/": 1.2,
            "models/": 1.15,
            "services/event_bus.py": 1.3
        }
        for pattern, boost in boosted_patterns.items():
            if pattern in file_path: return boost
        return 1.0

    def _deduplicate(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Context GC: Prunes redundant or overlapping information units.
        """
        seen_content = {}
        unique_items = []
        
        for item in candidates:
            # Simple content hash for exact matches
            content = item["content"].strip()
            if not content: continue
            
            # For code chunks: use file + normalized content snippet
            key = content[:200] # Signature
            if item["type"] == "code_chunk":
                key = f"{item['meta'].get('file')}:{content[:100]}"

            if key in seen_content:
                # Keep the one with higher base score
                if item["score"] > seen_content[key]["score"]:
                    seen_content[key] = item
            else:
                seen_content[key] = item
                
        return list(seen_content.values())

    async def build_active_context(self, task_description: str, signal_data: Dict[str, List[Dict[str, Any]]]) -> str:
        """
        Unifies all signals (Qdrant, Neo4j, Event Store) into a coherent, 
        token-optimized payload for the Strategic Planner.
        """
        all_candidates = []
        
        for category, items in signal_data.items():
            for item in items:
                # Normalize structure
                all_candidates.append({
                    "type": category,
                    "content": item.get("content", item.get("text", item.get("solution", ""))),
                    "score": item.get("hybrid_score", item.get("semantic_score", item.get("score", 0.5))),
                    "timestamp": item.get("timestamp", time.time()),
                    "meta": item
                })

        # 1. Context GC (Deduplication)
        deduped = self._deduplicate(all_candidates)

        # 2. Potency Ranking
        for item in deduped:
            item["potency"] = self._calculate_potency(item)
        
        deduped.sort(key=lambda x: x["potency"], reverse=True)

        # 3. Strategic Synthesis (New Layer 6)
        # We take the top 5 most potent items to generate a summary advisory
        top_signals = deduped[:5]
        advisory = await self.generate_strategic_advisory(task_description, top_signals)

        # 4. Budget Allocation & Compression
        context_parts = []
        if advisory:
            context_parts.append(f"## \ud83e\udde0 STRATEGIC ADVISORY (L5-L6 Synthesis)\n{advisory}")

        current_tokens = len(advisory) // 4 if advisory else 0
        
        for item in deduped:
            if item["potency"] < 0.25: continue # Aggressive noise pruning

            est_tokens = len(item["content"]) // 4
            
            if current_tokens + est_tokens <= self.token_budget:
                # High Potency: include full
                context_parts.append(self._format_item(item))
                current_tokens += est_tokens
            elif current_tokens < self.token_budget * 0.85:
                # Medium Potency: summarize
                summary = await self.summarize_item(item["content"], item["type"])
                summary_tokens = len(summary) // 4
                if current_tokens + summary_tokens <= self.token_budget:
                    context_parts.append(f"### [SUMMARIZED | {item['type'].upper()}]\n- {summary}")
                    current_tokens += summary_tokens

        return "\n\n".join(context_parts)

    async def generate_strategic_advisory(self, task: str, top_signals: List[Dict[str, Any]]) -> str:
        """
        Synthesizes multiple signals into a one-paragraph directive.
        """
        if not top_signals: return ""
        
        summary_payload = []
        for s in top_signals:
            summary_payload.append(f"[{s['type']}] {s['content'][:150]}...")
            
        prompt = (
            f"As a Cognitive Architect, synthesize these signals into a sharp, actionable strategy for this task:\n"
            f"TASK: {task}\n"
            f"SIGNALS: {json.dumps(summary_payload)}\n"
            f"RESPONSE: (single directive paragraph, focus on pitfalls and proven paths)"
        )
        try:
            from services.llm_solver import solve
            resp = await solve(prompt, "strategic_advisory", [], "Directive context synthesis.", "local")
            return resp.solution.strip()
        except:
            return "Proceed by prioritizing historical success patterns and avoiding recent failure clusters."

    def _format_item(self, item: Dict[str, Any]) -> str:
        t = item["type"].upper()
        file_info = f" | {item['meta'].get('file')}" if item['meta'].get('file') else ""
        return f"### [{t}{file_info} | Potency: {item['potency']}]\n{item['content']}"

    async def summarize_item(self, text: str, item_type: str) -> str:
        """
        Fast, internal-loop summarization.
        """
        if len(text) < 300: return text
        
        prompt = f"Summarize this technical {item_type} context into one information-dense sentence: {text}"
        try:
            from services.llm_solver import solve
            resp = await solve(prompt, "summarization", [], "One dense sentence.", "local")
            return resp.solution.strip()
        except:
            return text[:200] + "..."

# Singleton Access
_engine = ContextEngine()
def get_context_engine() -> ContextEngine:
    return _engine

"""
dynamic_rules.py
================
Dynamic Rule Engine — Plano Avançado v2, Parte 4.

Aprende regras de roteamento automaticamente a partir de padrões no histórico
de tarefas (PostgreSQL agent_events). Nenhuma LLM envolvida — puro algoritmo.

Lifecycle:
  1. `learn_rules_from_history()` — analisa agent_events e cria regras
  2. `evaluate(task_type, error_sig)` — aplica regra mais confiante
  3. Regras persistidas em `dynamic_rules` table; recarregadas no startup

Public API
----------
  engine = DynamicRuleEngine(db_func)
  await  engine.learn_rules_from_history()   # background task
  rule   = engine.evaluate("fix_bug", "NullPointerException")
  if rule:
      route_to = rule.action   # "route_to:database_agent" | "use_template:fix_bug"
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("orch.rules")

# Minimum occurrences before a rule is created
_MIN_PATTERN_COUNT   = 3
# Minimum confidence to activate a rule
_MIN_CONFIDENCE      = 0.75
# How many recent events to analyse per task_type
_ANALYSIS_WINDOW     = 200
# TTL for in-memory rule cache (seconds)
_RULE_CACHE_TTL      = 300


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class DynamicRule:
    rule_id:       str
    condition:     dict           # {"task_type": "fix_bug", "error_signature": "..."}
    action:        str            # "route_to:<agent>" | "use_template:<name>" | "skip_l2"
    confidence:    float
    created_from:  str            # "failure_pattern" | "success_pattern" | "manual"
    times_applied: int = 0
    created_at:    float = field(default_factory=time.time)

    def matches(self, task_type: str, error_sig: Optional[str]) -> bool:
        if self.condition.get("task_type") != task_type:
            return False
        cond_err = self.condition.get("error_signature")
        if cond_err and cond_err != error_sig:
            return False
        return self.confidence >= _MIN_CONFIDENCE


# ── DynamicRuleEngine ─────────────────────────────────────────────────────────

class DynamicRuleEngine:
    """
    Learns routing rules from historical task outcomes.

    Parameters
    ----------
    db : callable
        Context-manager factory → psycopg2/psycopg3 connection (same as `db()` in streaming.core.db).
    """

    def __init__(self, db: Callable):
        self._db     = db
        self._rules: list[DynamicRule] = []
        self._loaded_at: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, task_type: str,
                 error_sig: Optional[str] = None) -> Optional[DynamicRule]:
        """
        Return the highest-confidence rule matching (task_type, error_sig).
        Returns None if no confident rule exists.
        """
        self._maybe_reload()
        matching = [r for r in self._rules if r.matches(task_type, error_sig)]
        if not matching:
            return None
        best = max(matching, key=lambda r: r.confidence)
        best.times_applied += 1
        log.debug("rule_applied rule_id=%s action=%s confidence=%.3f",
                  best.rule_id, best.action, best.confidence)
        return best

    def learn_rules_from_history(self, tenant_id: str = ""):
        """
        Analyse recent agent_events and persist newly discovered rules.
        Call periodically (e.g. from a background thread every 5 min).
        """
        task_types = [
            "fix_bug", "create_route", "create_endpoint", "refactor",
            "generate_schema", "create_migration", "create_test",
            "add_feature", "review", "analyze_impact", "ingest",
        ]
        new_rules: list[DynamicRule] = []

        for task_type in task_types:
            try:
                events = self._fetch_events(task_type, tenant_id)
                new_rules.extend(self._mine_failure_patterns(task_type, events))
                new_rules.extend(self._mine_success_patterns(task_type, events))
            except Exception as exc:
                log.warning("rule_mining_error task_type=%s error=%s", task_type, exc)

        if new_rules:
            self._persist_rules(new_rules)
            # Merge into in-memory list (replace same rule_id)
            existing = {r.rule_id: r for r in self._rules}
            for r in new_rules:
                existing[r.rule_id] = r
            self._rules = list(existing.values())
            log.info("dynamic_rules_updated total=%d new=%d",
                     len(self._rules), len(new_rules))

    def load_from_db(self, tenant_id: str = ""):
        """Load all persisted rules into memory."""
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    q = "SELECT rule_id, condition, action, confidence, created_from, times_applied, created_at from dynamic_rules"
                    params: list = []
                    if tenant_id:
                        q += " WHERE tenant_id = %s OR tenant_id IS NULL"
                        params.append(tenant_id)
                    q += " ORDER BY confidence DESC"
                    cur.execute(q, params)
                    rows = cur.fetchall()
            self._rules = [
                DynamicRule(
                    rule_id      = r["rule_id"],
                    condition    = r["condition"] if isinstance(r["condition"], dict)
                                   else json.loads(r["condition"]),
                    action       = r["action"],
                    confidence   = float(r["confidence"]),
                    created_from = r["created_from"],
                    times_applied = r.get("times_applied", 0),
                )
                for r in rows
            ]
            self._loaded_at = time.time()
            log.info("dynamic_rules_loaded count=%d", len(self._rules))
        except Exception as exc:
            log.warning("dynamic_rules_load_error error=%s", exc)

    def get_all_rules(self) -> list[dict]:
        return [
            {
                "rule_id":       r.rule_id,
                "condition":     r.condition,
                "action":        r.action,
                "confidence":    r.confidence,
                "created_from":  r.created_from,
                "times_applied": r.times_applied,
            }
            for r in sorted(self._rules, key=lambda r: r.confidence, reverse=True)
        ]

    # ── Mining ────────────────────────────────────────────────────────────────

    def _fetch_events(self, task_type: str, tenant_id: str) -> list[dict]:
        """Fetch recent agent_events for a task_type from PostgreSQL."""
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = 'public'
                           AND table_name = 'tasks'
                        """
                    )
                    task_cols = {row["column_name"] for row in cur.fetchall()}
                    task_pk = "task_id" if "task_id" in task_cols else "id"
                    q = """
                        SELECT ae.event_type, ae.payload, t.assigned_agent,
                               t.status, t.{task_pk} AS task_id
                        FROM agent_events ae
                        JOIN tasks t ON t.{task_pk} = ae.task_id
                        WHERE t.task_type = %s
                    """.format(task_pk=task_pk)
                    params: list = [task_type]
                    if tenant_id:
                        q += " AND t.tenant_id = %s"
                        params.append(tenant_id)
                    q += " ORDER BY ae.created_at DESC LIMIT %s"
                    params.append(_ANALYSIS_WINDOW)
                    cur.execute(q, params)
                    rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _mine_failure_patterns(self, task_type: str,
                                events: list[dict]) -> list[DynamicRule]:
        """
        Pattern: task_type failed N times with same error → create route override.
        """
        rules: list[DynamicRule] = []
        failures = [
            e for e in events
            if e.get("event_type") in ("task_failed", "llm_error", "agent_error")
        ]
        if not failures:
            return rules

        # Count error signatures
        error_sigs: Counter = Counter()
        for e in failures:
            payload = e.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            sig = payload.get("error_type") or payload.get("error", "")[:40]
            if sig:
                error_sigs[sig] += 1

        for error_sig, count in error_sigs.most_common(5):
            if count < _MIN_PATTERN_COUNT:
                continue

            # Find which agent/approach succeeded after same error
            resolved_agents: Counter = Counter()
            for e in events:
                payload = e.get("payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                prev_err = payload.get("error_type") or payload.get("error", "")[:40]
                if (e.get("event_type") == "task_done"
                        and prev_err == error_sig
                        and e.get("assigned_agent")):
                    resolved_agents[e["assigned_agent"]] += 1

            if resolved_agents:
                best_agent, success_count = resolved_agents.most_common(1)[0]
                confidence = success_count / (count + success_count)
                if confidence >= _MIN_CONFIDENCE:
                    rule_id = f"fail_{task_type}_{hashlib.md5(error_sig.encode()).hexdigest()[:8]}"
                    rules.append(DynamicRule(
                        rule_id      = rule_id,
                        condition    = {"task_type": task_type,
                                        "error_signature": error_sig},
                        action       = f"route_to:{best_agent}",
                        confidence   = round(confidence, 4),
                        created_from = "failure_pattern",
                    ))
        return rules

    def _mine_success_patterns(self, task_type: str,
                                events: list[dict]) -> list[DynamicRule]:
        """
        Pattern: task_type succeeded N times via same agent → recommend that agent.
        """
        rules: list[DynamicRule] = []
        successes = [e for e in events if e.get("event_type") == "task_done"
                     and e.get("assigned_agent")]
        if len(successes) < _MIN_PATTERN_COUNT * 2:
            return rules

        agent_counts: Counter = Counter(e["assigned_agent"] for e in successes)
        top_agent, top_count = agent_counts.most_common(1)[0]
        confidence = top_count / len(successes)

        if confidence >= _MIN_CONFIDENCE:
            rule_id = f"succ_{task_type}_{hashlib.md5(top_agent.encode()).hexdigest()[:6]}"
            rules.append(DynamicRule(
                rule_id      = rule_id,
                condition    = {"task_type": task_type},
                action       = f"prefer_agent:{top_agent}",
                confidence   = round(confidence, 4),
                created_from = "success_pattern",
            ))
        return rules

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_rules(self, rules: list[DynamicRule]):
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    for r in rules:
                        cur.execute(
                            """
                            INSERT INTO dynamic_rules
                                (rule_id, condition, action, confidence, created_from, times_applied)
                            VALUES (%s, %s, %s, %s, %s, 0)
                            ON CONFLICT (rule_id) DO UPDATE
                                SET confidence = EXCLUDED.confidence,
                                    action     = EXCLUDED.action,
                                    updated_at = NOW()
                            """,
                            (r.rule_id, json.dumps(r.condition),
                             r.action, r.confidence, r.created_from),
                        )
                conn.commit()
        except Exception as exc:
            log.warning("dynamic_rules_persist_error error=%s", exc)

    def _maybe_reload(self):
        """Reload from DB if cache is stale (TTL = 5 min)."""
        if time.time() - self._loaded_at > _RULE_CACHE_TTL:
            self.load_from_db()


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[DynamicRuleEngine] = None


def get_rule_engine(db: Optional[Callable] = None) -> Optional[DynamicRuleEngine]:
    global _instance
    if _instance is None and db is not None:
        _instance = DynamicRuleEngine(db)
        _instance.load_from_db()
    return _instance

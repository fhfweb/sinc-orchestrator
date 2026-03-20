"""
autonomy_score.py
=================
Measurable index of AI Engineering System (AES) seniority.
Weights: Bypass Rate (30%), Simulation Accuracy (30%), Budget Efficiency (20%), Success Rate (20%).
"""
import logging
from dataclasses import dataclass
from typing import Dict, Any

log = logging.getLogger("orchestrator.autonomy")

@dataclass
class AutonomyMetrics:
    llm_bypass_rate: float        # 0.0 -> 1.0
    simulation_accuracy: float     # 0.0 -> 1.0
    budget_utilization: float      # 0.0 -> 1.0 (Higher is not always better, efficiency is)
    autonomous_success_rate: float # 0.0 -> 1.0

def calculate_autonomy_score(metrics: AutonomyMetrics) -> float:
    """
    Computes the Global Autonomy Index.
    Thresholds:
    - > 0.8: Senior AES
    - 0.5 - 0.8: Mid-level AES
    - < 0.5: Junior / Assistant
    """
    score = (
        metrics.llm_bypass_rate * 0.30 +
        metrics.simulation_accuracy * 0.30 +
        (1.0 - abs(metrics.budget_utilization - 0.7)) * 0.20 + # Optimal utilization is 70%
        metrics.autonomous_success_rate * 0.20
    )
    return round(score, 4)

def get_seniority_label(score: float) -> str:
    if score >= 0.85: return "Principal AES"
    if score >= 0.70: return "Senior AES"
    if score >= 0.50: return "Mid-level AES"
    return "Autonomous Junior"

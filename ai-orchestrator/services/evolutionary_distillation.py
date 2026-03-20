"""
evolutionary_distillation.py
===========================
Phase 13: The Self-Evolving Engine.
Extracts verified reasoning traces from memory and prepares them for fine-tuning.
"""
from __future__ import annotations
import json
import logging
import os
from typing import List, Dict, Any, Optional
from services.streaming.core.config import env_get

log = logging.getLogger("orch.evolution.distill")

class EvolutionaryDistillationService:
    """Service to harvest high-quality training pairs from verified execution data."""
    
    def __init__(self, storage_dir: str = "data/fine_tuning"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)

    async def extract_verified_traces(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Queries the 'solutions' collection for verified=True entries.
        Transforms them into (Instruction, Input, Output) triplets.
        """
        log.info("extracting_verified_traces tenant=%s limit=%d", tenant_id, limit)
        
        # In a real system, this would call Qdrant directly or via context_retriever.
        raw_data = [] 
        
        dataset = []
        for item in raw_data:
            payload = item.get("payload", {})
            if payload.get("verified") is True:
                dataset.append({
                    "instruction": "Solve the following task using the provided reasoning path.",
                    "input": f"Task: {payload.get('description')}\nType: {payload.get('task_type')}",
                    "output": payload.get("solution")
                })
        
        return dataset

    def export_to_jsonl(self, dataset: List[Dict[str, Any]], filename: str = "train_data.jsonl"):
        """Exports the dataset to JSONL format for LoRA/Fine-tuning."""
        path = os.path.join(self.storage_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            for item in dataset:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        log.info("dataset_exported path=%s count=%d", path, len(dataset))
        return path

def get_distillation_service():
    return EvolutionaryDistillationService()

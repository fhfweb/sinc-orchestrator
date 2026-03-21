import re
from typing import Dict, Any

class InfraProfile:
    """
    Identifica padrões de infraestrutura (Docker, Kubernetes).
    """
    @staticmethod
    def identify(filename: str, content: str, symbols: Dict[str, Any]):
        tags = []
        if filename.lower() == "dockerfile" or "FROM " in content:
            tags.append("Docker")
            tags.append("Containerization")
        
        if filename.endswith(".yaml") or filename.endswith(".yml"):
            if "kind: Deployment" in content or "apiVersion: v1" in content:
                tags.append("Kubernetes")
                tags.append("Orchestration")
            if "services:" in content and "version:" in content:
                tags.append("DockerCompose")

        if tags:
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))

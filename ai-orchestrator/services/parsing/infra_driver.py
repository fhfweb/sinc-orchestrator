import re
from typing import Dict, Any, List
from services.parsing.base_driver import BaseParser
from services.frameworks.manager import ProfileManager

class InfraParser(BaseParser):
    def get_supported_extensions(self) -> List[str]:
        return [".yaml", ".yml", "dockerfile"]

    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        """Simple parser for infra files to extract framework tags and service names."""
        symbols = {"services": [], "classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        filename = rel_path.split("/")[-1]
        
        # 1. Profile Tags
        ProfileManager.analyze_infra(filename, content, symbols)
        
        # 2. Docker Compose Extraction
        if "docker-compose" in filename.lower():
            # Pegamos nomes de serviços via regex simplificado (YAML parser seria melhor em prod)
            services = re.findall(r"^\s+([a-z0-9_-]+):", content, re.MULTILINE)
            # Removemos chaves reservadas do compose
            reserved = {"version", "services", "networks", "volumes", "environment", "build", "image"}
            symbols["services"] = [s for s in services if s not in reserved]

        return symbols

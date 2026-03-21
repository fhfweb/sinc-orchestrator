import re
from typing import Dict, Any

class NestJSProfile:
    """
    Identifica padrões profundos do framework NestJS em TS/JS.
    Analisa rotas, métodos e injeção de dependências.
    """
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        tags = []
        framework_metadata = symbols.get("framework_metadata", {})
        
        # 1. Extração de Controller Path
        controller_match = re.search(r"@Controller\(['\"]?([^'\"]*)['\"]?\)", content)
        if controller_match:
            tags.append("NestJS-Controller")
            base_path = controller_match.group(1) or "/"
            framework_metadata["controller_base"] = base_path
            
            # 2. Extração de Rotas (Heurística AST-like via Regex)
            routes = []
            route_pattern = r"@(Get|Post|Put|Delete|Patch)\(['\"]?([^'\"]*)['\"]?\)\s*(?:async\s+)?(\w+)\s*\("
            for m in re.finditer(route_pattern, content):
                routes.append({
                    "verb": m.group(1).upper(),
                    "path": f"{base_path}/{m.group(2)}".replace("//", "/"),
                    "handler": m.group(3)
                })
            if routes:
                framework_metadata["endpoints"] = routes
                tags.append("API-Gateway")

        # 3. Injeção de Dependências (Constructor DI)
        di_match = re.search(r"constructor\s*\(([^)]+)\)", content)
        if di_match:
            params = di_match.group(1)
            # Tenta pegar tipos injetados: private readonly service: MyService
            services = re.findall(r":\s*(\w+)", params)
            if services:
                framework_metadata["injected_services"] = services
                tags.append("Dependency-Injection")

        if "@Module" in content:
            tags.append("NestJS-Module")

        if tags:
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))
            symbols["framework_metadata"] = framework_metadata

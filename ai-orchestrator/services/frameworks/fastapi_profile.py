import re
from typing import List, Dict, Any

class FastAPIProfile:
    """
    Identifica padrões do framework FastAPI em arquivos Python.
    """
    
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        """
        Enriquece o dicionário de símbolos com metadados de FastAPI.
        """
        # 1. Detectar Rotas
        # Padrão: @app.get("/path") ou similar
        route_pattern = re.compile(r'@\w+\.(get|post|put|delete|patch|options)\s*\(\s*["\'](.*?)["\']')
        
        for fn in symbols.get("functions", []):
            # Tentar encontrar o decorador acima da linha da função
            # No Tree-sitter, poderíamos ser mais precisos, mas regex aqui é um bom complemento
            # Já que o parser TS nos deu a linha da função.
            pass

        # Para simplicidade inicial, buscaremos todas as rotas no conteúdo
        routes = []
        for m in route_pattern.finditer(content):
            routes.append({
                "verb": m.group(1).upper(),
                "path": m.group(2),
                "line": content[:m.start()].count("\n") + 1
            })
        
        if routes:
            symbols["framework_metadata"] = symbols.get("framework_metadata", {})
            symbols["framework_metadata"]["fastapi_routes"] = routes
            symbols["tags"] = symbols.get("tags", []) + ["FastAPI", "WebAPI"]

    @staticmethod
    def is_pydantic_model(cls_name: str, content: str) -> bool:
        """Heurística para detectar modelos Pydantic."""
        return f"class {cls_name}(BaseModel)" in content or "from pydantic import BaseModel" in content

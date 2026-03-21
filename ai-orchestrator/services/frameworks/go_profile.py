from typing import Dict, Any

class GoProfile:
    """
    Identifica padrões do ecossistema Go (Gin, Echo).
    """
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        tags = []
        if "github.com/gin-gonic/gin" in content:
            tags.append("Gin-Framework")
            tags.append("Microservices")
        
        if "github.com/labstack/echo" in content:
            tags.append("Echo-Framework")

        if tags:
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))

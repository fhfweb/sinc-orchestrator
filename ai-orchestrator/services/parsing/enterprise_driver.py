import re
from typing import Dict, Any, List
from services.parsing.base_driver import BaseParser
from services.frameworks.manager import ProfileManager

class EnterpriseParser(BaseParser):
    """
    Driver genérico para linguagens como Java, C#, C++, Dart.
    Foca na extração de tags e metadados de framework via ProfileManager.
    """
    def get_supported_extensions(self) -> List[str]:
        return [".java", ".cs", ".cpp", ".hpp", ".dart", ".kt", ".swift"]

    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        symbols = {"classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        
        # Heurística de imports genérica
        for m in re.finditer(r"^(?:import|using|include)\s+([\w\.]+)", content, re.MULTILINE):
            symbols["imports"].append(m.group(1))
            
        # Heurística de classes genérica
        for m in re.finditer(r"(?:class|struct|interface|trait)\s+(\w+)", content):
            symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})

        # Despachar para ProfileManager para detecção profunda (Spring, .NET, Flutter, Unity)
        ProfileManager.analyze_enterprise(content, symbols)
        return symbols

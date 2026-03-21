import re
from typing import List, Dict, Any

class ReactProfile:
    """
    Identifica padrões do framework React (e derivados) em JS/TS.
    """
    
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        """
        Enriquece os símbolos com metadados de componentes React.
        """
        # 1. Detectar Hooks
        hooks = re.findall(r'(use[A-Z]\w+)', content)
        if hooks:
            symbols["framework_metadata"] = symbols.get("framework_metadata", {})
            symbols["framework_metadata"]["react_hooks"] = list(set(hooks))
            symbols["tags"] = symbols.get("tags", []) + ["React", "Frontend"]

        # 2. Detectar se é um componente funcional (Heurística simples)
        # Se tem JSX ou export default function que retorna JSX
        if ("return (" in content or "return <" in content) and "<" in content:
             symbols["tags"] = list(set(symbols.get("tags", []) + ["ReactComponent"]))
             
    @staticmethod
    def is_react_file(rel_path: str, content: str) -> bool:
        return rel_path.endswith((".jsx", ".tsx")) or "import React" in content

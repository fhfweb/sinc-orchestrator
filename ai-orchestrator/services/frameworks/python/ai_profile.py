import re
from typing import Dict, Any

class AIProfile:
    """
    Identifica padrões profundos de IA e Agentes (LangChain, LlamaIndex, PyTorch).
    Extrai definições de Chains, Tools e Modelos.
    """
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        tags = []
        framework_metadata = symbols.get("framework_metadata", {})
        
        if "langchain" in content:
            tags.append("LangChain")
            
            # 1. Extração de Tools
            # @tool decorator ou classes Tool
            tool_matches = re.findall(r"@tool\s+def\s+(\w+)", content)
            if tool_matches:
                framework_metadata["ai_tools"] = tool_matches
                tags.append("IA-Tools")
            
            # 2. Extração de Chains (Heurística: uso de | em LCEL)
            if "|" in content and ("PromptTemplate" in content or "LLM" in content):
                tags.append("LCEL-Chain")
                framework_metadata["chain_type"] = "LCEL"

        if "llama_index" in content or "VectorStoreIndex" in content:
            tags.append("LlamaIndex")
            tags.append("RAG-Architecture")
            # Extração de índices
            indices = re.findall(r"(\w+)\s*=\s*VectorStoreIndex", content)
            if indices:
                framework_metadata["vector_indices"] = indices

        if "torch" in content or "nn.Module" in content:
            tags.append("PyTorch")
            # Extração de Modelos
            nn_classes = re.findall(r"class\s+(\w+)\s*\(nn\.Module\):", content)
            if nn_classes:
                framework_metadata["torch_models"] = nn_classes

        if tags:
            tags.append("AI/ML")
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))
            symbols["framework_metadata"] = framework_metadata

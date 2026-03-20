import logging
import json
import httpx
import os
from neo4j import GraphDatabase

class SemanticAnnotator:
    """
    Enriquece o grafo com intenções semânticas baseadas em análise de LLM.
    Focado em desobfuscar nomes "GoHorse".
    """
    def __init__(self, ollama_url=None):
        self.ollama_url = ollama_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_pass = os.getenv("NEO4J_PASS", "password")
        self.driver = GraphDatabase.driver(self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_pass))

    async def infer_intent(self, code_snippet: str) -> str:
        """Usa Ollama para inferir a intenção real de um código mal nomeado."""
        prompt = f"""
        Analyze the following code snippet and describe its PRIMARY INTENT in 2-3 words.
        If the names are random (GoHorse style), focus ONLY on the logic patterns.
        Return ONLY the label (e.g., 'User Authentication', 'Database Persistence', 'API Gateway').
        
        CODE:
        {code_snippet[:1000]}
        
        INTENT:"""
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{self.ollama_url}/api/generate", json={
                    "model": "llama3", # Ou outro modelo disponível
                    "prompt": prompt,
                    "stream": False
                })
                return resp.json().get("response", "Unknown Intent").strip()
        except Exception as e:
            return "Inference Failed"

    async def annotate_node(self, node_id: str, intent: str):
        """Persiste a intenção inferida no Neo4j."""
        query = "MATCH (f:Function) WHERE id(f) = toInteger($nid) SET f.semantic_intent = $intent"
        with self.driver.session() as session:
            session.run(query, nid=node_id, intent=intent)
            
    async def annotate_tenant(self, tenant_id: str):
        # ... logic summarized in previous step ...
        pass

    def close(self):
        self.driver.close()

if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    annotator = SemanticAnnotator()
    # Exemplo de uso: asyncio.run(annotator.annotate_tenant("local"))
    annotator.close()

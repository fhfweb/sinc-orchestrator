import logging
import os
import re
from neo4j import GraphDatabase

class CodeHealer:
    """
    Motor de Self-Healing. 
    Cura código mal nomeado (GoHorse) transformando-o em Clean Code.
    """
    def __init__(self, uri=None, user=None, password=None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASS") or os.getenv("NEO4J_AUTH", "neo4j/neo4j").split("/", 1)[-1]
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def get_healing_candidates(self, tenant_id: str):
        """Busca funções que precisam de cura (GOHORSE_WARNING + semantic_intent)."""
        query = """
        MATCH (f:Function {tenant_id: $tid})
        WHERE "GOHORSE_WARNING" IN f.tags AND f.semantic_intent IS NOT NULL
        RETURN id(f) as id, f.name as old_name, f.semantic_intent as intent, f.file as file
        """
        with self.driver.session() as session:
            return list(session.run(query, tid=tenant_id))

    def suggest_professional_name(self, intent: str) -> str:
        """Converte uma intenção em um nome de função Clean Code."""
        # Heurística simples ou chamada de LLM
        clean = intent.lower().replace(" ", "_").replace("-", "_")
        clean = re.sub(r'[^a-z0-9_]', '', clean)
        return clean if clean else "processed_logic"

    def apply_healing(self, node_id: int, new_name: str, project_root: str):
        """Executa a refatoração física e lógica."""
        # 1. Obter detalhes do nó
        with self.driver.session() as session:
            res = session.run("MATCH (f:Function) WHERE id(f) = $id RETURN f.name as old, f.file as file", id=node_id).single()
            if not res: return
            old_name = res["old"]
            file_path = os.path.join(project_root, res["file"])

            # 2. Renomear DEFINIÇÃO no arquivo original
            if os.path.exists(file_path):
                content = open(file_path, "r", encoding="utf-8").read()
                new_content = re.sub(rf"(\bdef\s+|\bfunction\s+){old_name}\b", rf"\1{new_name}", content)
                new_content = re.sub(rf"(\bimport\s+){old_name}\b", rf"\1{new_name}", new_content)
                new_content = re.sub(rf"(\bfrom\s+.*import\s+){old_name}\b", rf"\1{new_name}", new_content)
                # Chamadas locais
                new_content = re.sub(rf"(?<!['\"])\b{old_name}\b(?!=['\"])", new_name, new_content)
                with open(file_path, "w", encoding="utf-8") as f: f.write(new_content)

            # 2.1 Renomear CHAMADAS em TODOS os arquivos do projeto
            callers = session.run("MATCH (fil:File)-[:CONTAINS_CALL]->(c:Call {name: $old}) RETURN DISTINCT fil.path as path", old=old_name)
            for caller in callers:
                c_path = os.path.join(project_root, caller["path"])
                if os.path.exists(c_path) and c_path != file_path:
                    c_content = open(c_path, "r", encoding="utf-8").read()
                    # Substitui a chamada e imports
                    c_new = re.sub(rf"(?<!['\"])\b{old_name}\b(?!=['\"])", new_name, c_content)
                    with open(c_path, "w", encoding="utf-8") as f: f.write(c_new)

            # 3. Atualizar Grafo (Definição)
            session.run("""
                MATCH (f:Function) WHERE id(f) = $id 
                SET f.name = $new, f.tags = [t IN f.tags WHERE t <> "GOHORSE_WARNING"] + ["HEALED"]
            """, id=node_id, new=new_name)
            
            # 4. Atualizar Chamadores no Grafo (XRef)
            session.run("""
                MATCH (c:Call {name: $old}) WHERE (c)-[:RESOLVES_TO]->(:Function)
                OR (c)-[:CALLS_INTERNAL]-(:Function)
                SET c.name = $new
            """, old=old_name, new=new_name)

    def close(self):
        self.driver.close()

if __name__ == "__main__":
    healer = CodeHealer()
    # healer.apply_healing(...)
    healer.close()

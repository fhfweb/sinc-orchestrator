import logging
import os
from neo4j import GraphDatabase

class TaintTracker:
    """
    Motor de análise de fluxo de dados (Data Lineage).
    Conecta 'Sources' a 'Sinks' através do grafo de chamadas e referências.
    """
    def __init__(self, uri=None, user=None, password=None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASS") or os.getenv("NEO4J_AUTH", "neo4j/neo4j").split("/", 1)[-1]
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def find_all_lineages(self, tenant_id: str):
        """
        Encontra todos os caminhos entre Fontes de Dados e Destinos de Persistência.
        """
        query = """
        MATCH (src:Function) WHERE "DATA_SOURCE" IN src.tags AND src.tenant_id = $tid
        MATCH (snk:Function) WHERE "DATA_SINK" IN snk.tags AND snk.tenant_id = $tid
        MATCH path = (src)-[:CALLS_INTERNAL|RESOLVES_TO*1..20]->(snk)
        RETURN src.name as source, snk.name as sink, [n in nodes(path) | n.name] as steps
        LIMIT 5
        """
        
        lineages = []
        with self.driver.session() as session:
            result = session.run(query, tid=tenant_id)
            for record in result:
                lineages.append({
                    "source": record["source"],
                    "sink": record["sink"],
                    "path": record["steps"]
                })
        
        return lineages

    def close(self):
        self.driver.close()

if __name__ == "__main__":
    tracker = TaintTracker()
    # print(tracker.find_all_lineages("local"))
    tracker.close()

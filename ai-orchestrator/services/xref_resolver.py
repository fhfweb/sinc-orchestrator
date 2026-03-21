import logging
from neo4j import GraphDatabase
import os

class XRefResolver:
    """
    Serviço que resolve referências cruzadas entre arquivos no Neo4j.
    Transforma nomes de funções em links concretos para suas definições.
    """
    def __init__(self, uri=None, user=None, password=None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASS", "password")
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def close(self):
        self.driver.close()

    def resolve_global_calls(self, tenant_id: str):
        """
        Vincula chamadas genéricas a definições de funções/métodos no mesmo tenant.
        """
        query = """
        MATCH (c:Call {tenant_id: $tenant_id})
        MATCH (f:Function {name: c.name, tenant_id: $tenant_id})
        WHERE c.file_path <> f.file
        MERGE (c)-[r:RESOLVES_TO]->(f)
        RETURN count(r) as resolved_count
        """
        with self.driver.session() as session:
            result = session.run(query, tenant_id=tenant_id)
            count = result.single()["resolved_count"]
            logging.info(f"Resolved {count} global call references for tenant {tenant_id}")
            return count

    def resolve_class_references(self, tenant_id: str):
        """
        Vincula referências de tipos a definições de classes.
        """
        query = """
        MATCH (r:Reference {tenant_id: $tenant_id})
        MATCH (c:Class {name: r.name, tenant_id: $tenant_id})
        MERGE (r)-[rel:TYPE_OF]->(c)
        RETURN count(rel) as resolved_count
        """
        with self.driver.session() as session:
            result = session.run(query, tenant_id=tenant_id)
            count = result.single()["resolved_count"]
            logging.info(f"Resolved {count} class type references for tenant {tenant_id}")
            return count

    def resolve_network_calls(self, tenant_id: str):
        """
        Vincula chamadas de rede (URLs) a definições de endpoints de API.
        """
        query = """
        MATCH (c:Call {tenant_id: $tenant_id})
        WHERE c.name IN ["get", "post", "put", "delete", "fetch", "axios"]
        MATCH (f:Function {tenant_id: $tenant_id})
        WHERE f.url_endpoint IS NOT NULL AND c.args_content CONTAINS f.url_endpoint
        MERGE (c)-[r:NETWORK_RESOLVES_TO]->(f)
        RETURN count(r) as resolved_count
        """
        with self.driver.session() as session:
            result = session.run(query, tenant_id=tenant_id)
            count = result.single()["resolved_count"]
            logging.info(f"Resolved {count} network call references for tenant {tenant_id}")
            return count

    def run_all(self, tenant_id: str):
        c1 = self.resolve_global_calls(tenant_id)
        c2 = self.resolve_class_references(tenant_id)
        c3 = self.resolve_network_calls(tenant_id)
        return {"calls": c1, "classes": c2, "network": c3}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    resolver = XRefResolver()
    # Exemplo para teste local
    resolver.run_all("default")
    resolver.close()

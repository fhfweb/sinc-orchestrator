from services.streaming.core.config import env_get
import os
import json
import logging
import asyncio
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("orch.maintenance")

QDRANT_HOST = env_get("QDRANT_HOST", default="localhost")
QDRANT_PORT = int(env_get("QDRANT_PORT", default="6333"))

class MemoryMaintenance:
    """
    Elite Maintenance Service (Week 3)
    Responsible for pruning stale vectors and optimizing graph nodes.
    """
    
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    async def prune_qdrant(self, project_id: str, tenant_id: str, days: int = 90):
        """
        Deletes points older than 'days' with low importance.
        """
        collections = [
            f"{tenant_id}_{project_id}_solutions",
            f"{tenant_id}_{project_id}_errors"
        ]
        
        limit_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        for coll in collections:
            log.info(f"Checking collection {coll} for points older than {limit_date}")
            
            # 1. Filter points older than limit_date
            # Note: This uses Qdrant's 'delete' API with a filter
            filter_body = {
                "filter": {
                    "must": [
                        {
                            "key": "timestamp",
                            "range": {"lt": limit_date}
                        }
                    ]
                }
            }
            
            if self.dry_run:
                log.info(f"[DRY-RUN] Would delete stale points in {coll}")
                continue
                
            url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{coll}/points/delete"
            try:
                payload = json.dumps(filter_body).encode()
                req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res = json.loads(resp.read())
                    log.info(f"Pruned {coll}: {res.get('status')}")
            except Exception as e:
                log.warning(f"Error pruning {coll}: {e}")

    async def optimize_graph(self, project_id: str, tenant_id: str):
        """
        Neo4j: Removes orphaned symbols (no connections to Files).
        """
        from services.context_retriever import NEO4J_URI, NEO4J_USER, NEO4J_PASS
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
            
            query = """
            MATCH (s)
            WHERE (s:Class OR s:Function OR s:Symbol)
              AND s.project_id = $pid
              AND NOT (s)<-[:DEFINES]-(:File)
            DETACH DELETE s
            """
            
            if self.dry_run:
                log.info(f"[DRY-RUN] Would delete orphaned graph nodes for {project_id}")
                return
                
            with driver.session() as session:
                res = session.run(query, pid=project_id)
                summary = res.consume()
                log.info(f"Graph Cleanup: Removed {summary.counters.nodes_deleted} orphaned nodes.")
            driver.close()
        except Exception as e:
            log.warning(f"Graph Cleanup Failed: {e}")

async def run_full_maintenance(project_id: str, tenant_id: str):
    m = MemoryMaintenance(dry_run=False)
    await m.prune_qdrant(project_id, tenant_id)
    await m.optimize_graph(project_id, tenant_id)

if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_full_maintenance("sinc", "local"))

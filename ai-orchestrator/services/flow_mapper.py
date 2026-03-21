from services.streaming.core.config import env_get
import time

class FlowMapper:
    """
    Traces execution flows (Processes) in the Neo4j Knowledge Graph.
    A 'Process' is a sequence of CALLS originating from an Entry Point.
    """
    def __init__(self, driver):
        self.driver = driver

    def map_processes(self, project_id: str, tenant_id: str):
        """
        1. Identify Entry Points (Functions with specific naming or decoration).
        2. Trace deep CALLS chains.
        3. Tag nodes as part of a 'Process'.
        """
        with self.driver.session() as session:
            # Step 1: Identify HTTP Entry Points (Heuristic: Starts with 'get_', 'post_', 'delete_', etc. or in a routes/ file)
            # This can be improved to look for @router.get() if we had decorator info.
            print(f"[flow-mapper] Identifying entry points for {project_id}...")
            session.run("""
                MATCH (f:Function {project_id: $pid, tenant_id: $tid})
                WHERE f.file CONTAINS 'routes' OR f.name STARTS WITH 'get_' OR f.name STARTS WITH 'post_'
                SET f:EntryPoint
            """, pid=project_id, tid=tenant_id)

            # Step 2: Trace call chains up to 5 levels deep
            # We create a 'Process' node for each Entry Point
            print(f"[flow-mapper] Tracing call chains...")
            session.run("""
                MATCH (ep:EntryPoint {project_id: $pid, tenant_id: $tid})
                MERGE (p:Process {uid: ep.file + ':' + ep.name, project_id: $pid, tenant_id: $tid})
                SET p.name = ep.name + ' Flow', p.entry_point = ep.name
                MERGE (ep)-[:START_OF]->(p)
                
                WITH ep, p
                MATCH path = (ep)-[:CALLS*1..5]->(target:File)
                UNWIND nodes(path) as step_node
                MATCH (step_node:File)
                MERGE (step_node)-[:PART_OF_PROCESS]->(p)
            """, pid=project_id, tid=tenant_id)
            
            # Step 3: Map inter-function calls specifically (if they exist)
            session.run("""
                MATCH (f1:Function)-[:CALLS]->(f2:Function)
                WHERE f1.project_id = $pid AND f2.project_id = $pid
                MERGE (f1)-[:PART_OF_PROCESS]->(p:Process)
                WITH f2, p
                MERGE (f2)-[:PART_OF_PROCESS]->(p)
            """, pid=project_id, tid=tenant_id)

            print(f"[flow-mapper] Process mapping complete.")

    def get_process_stats(self, project_id: str, tenant_id: str):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (p:Process {project_id: $pid, tenant_id: $tid})
                OPTIONAL MATCH (n)-[:PART_OF_PROCESS]->(p)
                RETURN p.name as name, count(n) as node_count
            """, pid=project_id, tid=tenant_id)
            return [dict(record) for record in result]

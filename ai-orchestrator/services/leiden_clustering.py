class CommunityDetector:
    """
    Groups code nodes into 'Communities' based on connectivity and location.
    Fallback for when Neo4j GDS (Leiden/Louvain) is not available.
    """
    def __init__(self, driver):
        self.driver = driver

    def detect_communities(self, project_id: str, tenant_id: str):
        """
        1. Clean old community nodes.
        2. Create communities based on 'Processes'.
        3. Create communities based on 'Folders' for orphaned nodes.
        """
        with self.driver.session() as session:
            print(f"[clustering] Detecting communities for {project_id}...")
            
            # Step 1: Clean
            session.run("""
                MATCH (c:Community {project_id: $pid, tenant_id: $tid})
                DETACH DELETE c
            """, pid=project_id, tid=tenant_id)

            # Step 2: Communities based on Processes (Functional)
            session.run("""
                MATCH (p:Process {project_id: $pid, tenant_id: $tid})
                MERGE (c:Community {name: p.name, type: 'Functional', project_id: $pid, tenant_id: $tid})
                WITH p, c
                MATCH (n)-[:PART_OF_PROCESS]->(p)
                MERGE (n)-[:MEMBER_OF]->(c)
            """, pid=project_id, tid=tenant_id)

            # Step 3: Communities based on Folder Structure (Structural Fallback)
            # Group files in the same first-level directory
            session.run("""
                MATCH (f:File {project_id: $pid, tenant_id: $tid})
                WHERE NOT (f)-[:MEMBER_OF]->(:Community)
                WITH f, split(f.path, '/')[0] as folder
                MERGE (c:Community {name: folder, type: 'Structural', project_id: $pid, tenant_id: $tid})
                MERGE (f)-[:MEMBER_OF]->(c)
            """, pid=project_id, tid=tenant_id)

            print(f"[clustering] Community detection complete.")

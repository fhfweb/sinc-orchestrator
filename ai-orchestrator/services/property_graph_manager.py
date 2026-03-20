from services.streaming.core.config import env_get
"""
Property Graph Manager — Phase 15 structural GraphRAG
=====================================================
Uses LlamaIndex to unify Neo4j graph traversal with vector filtering.
"""

import os
import logging
from typing import Optional, List, Dict
from llama_index.core import PropertyGraphIndex
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.indices.property_graph import SchemaLLMPathExtractor

log = logging.getLogger("orch.graphrag")

class PropertyGraphManager:
    """
    Manages structural reasoning over the code knowledge graph.
    Unifies L2 (Vector) and L3 (Graph) logic.
    """
    def __init__(self, uri: str, user: str, password: str, embed_model=None):
        self.uri = uri
        self.user = user
        self.password = password
        self.embed_model = embed_model
        self._index = None
        
        # Initialize LlamaIndex Graph Store
        try:
            self.graph_store = Neo4jPropertyGraphStore(
                username=user,
                password=password,
                url=uri,
                database="neo4j"
            )
            log.info("graphrag_store_initialized")
        except Exception as exc:
            log.error("graphrag_init_failed error=%s", exc)
            self.graph_store = None

    def _get_index(self):
        """Lazy initialization of the PropertyGraphIndex."""
        if self._index is None and self.graph_store:
            try:
                # We assume the graph is already populated by the ingest pipeline
                # This index allows querying existing Neo4j structures
                self._index = PropertyGraphIndex.from_existing(
                    property_graph_store=self.graph_store,
                    embed_model=self.embed_model
                )
            except Exception as exc:
                log.debug("pg_index_load_failed error=%s", exc)
        return self._index

    def structural_query(self, query: str, task_type: str) -> Optional[Dict]:
        """
        Perform a structural GraphRAG query.
        Finds nodes related to the query and explores their dependencies.
        """
        index = self._get_index()
        if not index:
            return None
            
        try:
            # Create a retriever that knows how to traverse the property graph
            # with semantic constraints
            retriever = index.as_retriever(
                sub_retrievers=["vector", "keyword"],
                similarity_top_k=5
            )
            
            nodes = retriever.retrieve(query)
            if not nodes:
                return None
                
            # Combine node info into a structural hint
            hints = []
            for node in nodes:
                # Node content usually contains metadata and text
                hints.append(node.get_content())
                
            return {
                "structural_context": "\n".join(hints),
                "source": "L3_graph_rag",
                "confidence": 0.85
            }
        except Exception as exc:
            log.warning("pg_query_failed error=%s", exc)
            return None

def get_pg_manager() -> Optional[PropertyGraphManager]:
    """Singleton getter for the PG Manager."""
    uri  = env_get("NEO4J_URI", default="bolt://localhost:7687")
    user = env_get("NEO4J_USER", default="neo4j")
    pwd  = env_get("NEO4J_PASSWORD", default="neo4j")
    
    # We might want to pass a real embed model here later
    return PropertyGraphManager(uri, user, pwd)

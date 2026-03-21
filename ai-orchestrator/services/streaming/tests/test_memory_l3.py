import asyncio
import sys

# Patch paths if necessary
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.context_retriever import (
    ContextRetriever, 
    _embed_query, 
    _solutions_collection, 
    _qdrant_search
)

async def test_memory_l3():
    print("[TEST] Iniciando verificacao da Memoria L3 (Qdrant)...")
    
    retriever = ContextRetriever()
    project_id = "sinc-orch-test"
    tenant_id = "test-tenant"
    
    print("[1] Injetando solucao na Memoria L3...")
    retriever.store_solution(
        query="Como lidar com memory leaks no agent?",
        answer="Sempre force o garbage colector e libere o contexto do LLM depois de 5 turnos.",
        project_id=project_id,
        tenant_id=tenant_id,
        intent="bugfix",
        verified=True
    )
    
    print("[2] Aguardando propagacao do vetor (2s)...")
    await asyncio.sleep(2)
    
    print("[3] Consultando a Memoria L3 (Cache Semantico)...")
    check_query = "Me explique como lidar com vazamento de memoria (memory leaks) no agente"
    
    # Fazendo busca raw para ver o score
    vector = _embed_query(check_query)
    collection = _solutions_collection(project_id, tenant_id)
    raw_hits = _qdrant_search(collection, vector, top_k=3)
    
    print(f"\\n[DEBUG] Raw Hits do Qdrant: {len(raw_hits)}")
    for i, h in enumerate(raw_hits):
        print(f" Hit {i+1} Score: {h.get('score', 0)}")
        print(f" Hit {i+1} Payload: {h.get('payload', {}).get('answer')}")

    hit = retriever.check_semantic_cache(
        query=check_query,
        project_id=project_id,
        tenant_id=tenant_id,
        threshold=0.50
    )
    
    if hit:
        print(f"\\n✅ SUCESSO! Solucao encontrada.")
        print(f"SCORE: {hit['score']:.4f}")
        print(f"RESPOSTA RECUPERADA: {hit['answer']}")
    else:
        print("\\n❌ FALHA: Nenhuma solucao encontrada na memoria vetorial.")
        sys.exit(1)
        
    print("\\n[TEST] Verificacao da Memoria L3 concluida perfeitamente!")

if __name__ == "__main__":
    asyncio.run(test_memory_l3())

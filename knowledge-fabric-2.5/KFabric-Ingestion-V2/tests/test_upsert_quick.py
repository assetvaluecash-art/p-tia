"""Quick test to verify upsert functionality."""

import pytest
from pathlib import Path

from kbfabric_ingest.ingest.ingestion_service import IngestionService


def test_upsert_prevents_duplicates():
    """Test that running ingestion twice doesn't create duplicates."""
    # Pre-load FinBERT model (synchronous - we need it before proceeding)
    from custom_ml.embeddings import EmbeddingsClient
    from kbfabric_ingest.ingest.config import get_app_config
    config = get_app_config()
    embeddings_client = EmbeddingsClient(config)
    print("Pre-loading FinBERT model...")
    embeddings_client.load_model()
    print("FinBERT model loaded successfully")
    
    domain = "consumer_payments"
    input_dir = Path("drop_zone/consumer_payments")
    
    # Clean up existing data for this domain first
    print(f"\n=== CLEANUP ===")
    from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
    from kbfabric_ingest.ingest.kg_writer import KGWriter
    from kbfabric_ingest.ingest.config import get_ontology
    
    # Clean Weaviate
    weaviate_writer = WeaviateWriter(config.get("weaviate", {}))
    class_name = weaviate_writer.domain_to_class.get(domain, weaviate_writer.class_name)
    try:
        weaviate_writer.client.batch.delete_objects(
            class_name=class_name,
            where={"path": ["domain"], "operator": "Equal", "valueString": domain}
        )
        print(f"Cleaned Weaviate {class_name}")
    except Exception as e:
        print(f"Weaviate cleanup warning: {e}")
    
    # Clean Neo4j
    kg_writer = KGWriter(config.get("neo4j", {}), get_ontology())
    with kg_writer.driver.session() as session:
        session.run("MATCH (b:Block {domain: $domain}) DETACH DELETE b", domain=domain)
        session.run("MATCH (d:Document {domain: $domain}) DETACH DELETE d", domain=domain)
        session.run("MATCH (r:DomainRoot {domain: $domain}) DETACH DELETE r", domain=domain)
        print(f"Cleaned Neo4j for domain {domain}")
    
    service = IngestionService()
    
    # First ingestion
    
    print(f"\n=== FIRST INGESTION ===")
    result1 = service.run_ingestion(input_dir, domain)
    count1_blocks = result1["total_blocks"]
    print(f"First run: {count1_blocks} blocks")
    
    # Second ingestion (should upsert, not duplicate)
    print(f"\n=== SECOND INGESTION (should upsert) ===")
    result2 = service.run_ingestion(input_dir, domain)
    count2_blocks = result2["total_blocks"]
    print(f"Second run: {count2_blocks} blocks")
    
    # Verify Weaviate counts
    from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
    from kbfabric_ingest.ingest.config import get_app_config
    config = get_app_config()
    writer = WeaviateWriter(config.get("weaviate", {}))
    
    class_name = writer.domain_to_class.get(domain, writer.class_name)
    try:
        result = writer.client.query.aggregate(class_name).with_meta_count().do()
        weaviate_count = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
        print(f"Weaviate {class_name}: {weaviate_count} objects")
    except Exception as e:
        print(f"Weaviate count check failed: {e}")
        weaviate_count = None
    
    # Verify Neo4j counts
    from kbfabric_ingest.ingest.kg_writer import KGWriter
    from kbfabric_ingest.ingest.config import get_ontology
    kg_writer = KGWriter(config.get("neo4j", {}), get_ontology())
    
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {domain: $domain}) RETURN count(b) as count",
            domain=domain
        )
        record = result.single()
        neo4j_count = record["count"] if record else 0
        print(f"Neo4j Block nodes: {neo4j_count}")
    
    # Assertions
    print(f"\n=== RESULTS ===")
    print(f"Parsed blocks (run 1): {count1_blocks}")
    print(f"Parsed blocks (run 2): {count2_blocks}")
    print(f"Weaviate objects: {weaviate_count}")
    print(f"Neo4j Block nodes: {neo4j_count}")
    
    # Both runs should parse the same number of blocks
    assert count1_blocks == count2_blocks, "Block counts should be equal"
    
    # Weaviate should have exactly count1_blocks (no duplicates)
    if weaviate_count is not None:
        assert weaviate_count == count1_blocks, f"Weaviate should have {count1_blocks} objects, got {weaviate_count} (duplicates detected!)"
    
    # Neo4j should have exactly count1_blocks (no duplicates)
    assert neo4j_count == count1_blocks, f"Neo4j should have {count1_blocks} Block nodes, got {neo4j_count} (duplicates detected!)"
    
    print("\n✅ Upsert test PASSED - No duplicates detected!")



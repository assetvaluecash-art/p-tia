"""Direct upsert test that imports writers step by step to find where it hangs."""

import sys
import uuid
print("TEST FILE LOADED", file=sys.stderr, flush=True)

# Deterministic UUID namespace
BLOCK_ID_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

def generate_block_id(source_file: str, block_index: int, domain: str) -> str:
    """Generate deterministic UUID5 block_id."""
    stable_string = f"{domain}:{source_file}:{block_index}"
    return str(uuid.uuid5(BLOCK_ID_NAMESPACE, stable_string))


def test_upsert_direct():
    """Direct upsert test with step-by-step imports."""
    print("\n" + "="*60, flush=True)
    print("DIRECT UPSERT TEST", flush=True)
    print("="*60, flush=True)
    
    # Step 1: Load config
    print("\n[STEP 1] Loading config...", flush=True)
    from kbfabric_ingest.ingest.config import get_app_config, get_ontology
    config = get_app_config()
    ontology = get_ontology()
    print(f"  [OK] Config loaded", flush=True)
    
    # Step 2: Import WeaviateWriter (might hang here)
    print("\n[STEP 2] Importing WeaviateWriter...", flush=True)
    from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
    print("  [OK] WeaviateWriter imported", flush=True)
    
    # Step 3: Create WeaviateWriter instance (might hang here)
    print("\n[STEP 3] Creating WeaviateWriter instance...", flush=True)
    weaviate_writer = WeaviateWriter(config.get("weaviate", {}))
    print("  [OK] WeaviateWriter created", flush=True)
    
    # Step 4: Ensure schema (might hang here)
    print("\n[STEP 4] Ensuring Weaviate schema...", flush=True)
    weaviate_writer.ensure_schema()
    print("  [OK] Weaviate schema ensured", flush=True)
    
    # Step 5: Import KGWriter (might hang here)
    print("\n[STEP 5] Importing KGWriter...", flush=True)
    from kbfabric_ingest.ingest.kg_writer import KGWriter
    print("  [OK] KGWriter imported", flush=True)
    
    # Step 6: Create KGWriter instance (might hang here)
    print("\n[STEP 6] Creating KGWriter instance...", flush=True)
    kg_writer = KGWriter(config.get("neo4j", {}), ontology)
    print("  [OK] KGWriter created", flush=True)
    
    # Step 7: Ensure indexes (might hang here)
    print("\n[STEP 7] Ensuring Neo4j indexes...", flush=True)
    kg_writer.ensure_indexes()
    print("  [OK] Neo4j indexes ensured", flush=True)
    
    # Step 8: Test data
    print("\n[STEP 8] Creating test data...", flush=True)
    domain = "consumer_payments"
    class_name = weaviate_writer.domain_to_class.get(domain, weaviate_writer.class_name)
    test_source_file = "test_upsert_direct.txt"
    
    block_id = generate_block_id(test_source_file, 0, domain)
    dummy_embedding = [0.0] * 768
    print(f"  [OK] Created test block: {block_id[:8]}...", flush=True)
    
    # Step 9: Cleanup
    print("\n[STEP 9] Cleaning up existing test data...", flush=True)
    try:
        weaviate_writer.client.batch.delete_objects(
            class_name=class_name,
            where={"path": ["source_file"], "operator": "Equal", "valueString": test_source_file}
        )
        print("  [OK] Weaviate cleaned", flush=True)
    except Exception as e:
        print(f"  [WARN] Weaviate cleanup: {e}", flush=True)
    
    with kg_writer.driver.session() as session:
        session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) DETACH DELETE b",
            domain=domain,
            source_file=test_source_file
        )
        print("  [OK] Neo4j cleaned", flush=True)
    
    # Step 10: First insert - Weaviate
    print("\n[STEP 10] FIRST INSERT - Weaviate...", flush=True)
    with weaviate_writer.client.batch as batch:
        batch.batch_size = 10
        properties = {
            "domain": domain,
            "block_category": "Narrative",
            "kg_node_type": "NarrativeText",
            "masked_text": "Test content for upsert",
            "source_file": test_source_file,
            "block_index": 0,
            "block_type": "paragraph",
            "metadata": {"test": True},
            "kg_node_ids": {}
        }
        batch.add_data_object(
            data_object=properties,
            class_name=class_name,
            uuid=block_id,
            vector=dummy_embedding
        )
        batch.flush()
    print("  [OK] Weaviate insert complete", flush=True)
    
    # Step 11: First insert - Neo4j
    print("\n[STEP 11] FIRST INSERT - Neo4j...", flush=True)
    with kg_writer.driver.session() as session:
        def write_tx(tx):
            tx.run("MERGE (dom:DomainRoot {domain: $domain})", domain=domain)
            tx.run(
                "MERGE (d:Document {source_file: $source_file}) ON CREATE SET d.domain = $domain",
                source_file=test_source_file,
                domain=domain
            )
            block_props = {
                "block_id": block_id,
                "domain": domain,
                "block_category": "Narrative",
                "kg_node_type": "NarrativeText",
                "masked_text": "Test content for upsert",
                "block_type": "paragraph",
                "block_index": 0,
                "metadata": "{'test': True}",
                "embedding": dummy_embedding,
                "chunk_id": ""
            }
            # Use fallback directly (APOC often not available)
            merge_query = """
            MERGE (b:NarrativeText:Block {block_id: $block_id})
            ON CREATE SET b = $props
            ON MATCH SET b = $props
            RETURN id(b) as block_id
            """
            result = tx.run(merge_query, block_id=block_id, props=block_props)
            record = result.single()
            if not record:
                raise Exception("Neo4j insert returned no result")
        session.execute_write(write_tx)
    print("  [OK] Neo4j insert complete", flush=True)
    
    # Step 12: Verify counts
    print("\n[STEP 12] Verifying first insert counts...", flush=True)
    result = weaviate_writer.client.query.aggregate(class_name).with_where({
        "path": ["source_file"],
        "operator": "Equal",
        "valueString": test_source_file
    }).with_meta_count().do()
    weaviate_count = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
    print(f"  [OK] Weaviate count (test file): {weaviate_count}", flush=True)
    
    with kg_writer.driver.session() as session:
        # Check by block_id first (most reliable)
        result = session.run(
            "MATCH (b:Block {block_id: $block_id}) RETURN count(b) as count",
            block_id=block_id
        )
        neo4j_count_by_id = result.single()["count"]
        print(f"  [OK] Neo4j count (by block_id): {neo4j_count_by_id}", flush=True)
        
        # Also check by domain+source_file
        result = session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) RETURN count(b) as count",
            domain=domain,
            source_file=test_source_file
        )
        neo4j_count = result.single()["count"]
        print(f"  [OK] Neo4j count (by domain+file): {neo4j_count}", flush=True)
        
        # Use block_id count as primary
        neo4j_count = neo4j_count_by_id
    
    assert weaviate_count >= 1, f"Weaviate should have at least 1 object, got {weaviate_count}"
    assert neo4j_count >= 1, f"Neo4j should have at least 1 node, got {neo4j_count}"
    
    # Step 13: Second insert (upsert)
    print("\n[STEP 13] SECOND INSERT - Same UUID (should upsert)...", flush=True)
    with weaviate_writer.client.batch as batch:
        batch.batch_size = 10
        properties["masked_text"] = "Test content for upsert [UPDATED]"
        batch.add_data_object(
            data_object=properties,
            class_name=class_name,
            uuid=block_id,  # Same UUID = upsert
            vector=dummy_embedding
        )
        batch.flush()
    print("  [OK] Weaviate upsert complete", flush=True)
    
    with kg_writer.driver.session() as session:
        def write_tx(tx):
            block_props = {
                "block_id": block_id,
                "domain": domain,
                "block_category": "Narrative",
                "kg_node_type": "NarrativeText",
                "masked_text": "Test content for upsert [UPDATED]",
                "block_type": "paragraph",
                "block_index": 0,
                "metadata": "{'test': True}",
                "embedding": dummy_embedding,
                "chunk_id": ""
            }
            # Use fallback directly (APOC often not available)
            merge_query = """
            MERGE (b:NarrativeText:Block {block_id: $block_id})
            ON CREATE SET b = $props
            ON MATCH SET b = $props
            RETURN id(b) as block_id
            """
            result = tx.run(merge_query, block_id=block_id, props=block_props)
            record = result.single()
            if not record:
                raise Exception("Neo4j insert returned no result")
        session.execute_write(write_tx)
    print("  [OK] Neo4j upsert complete", flush=True)
    
    # Step 14: Verify upsert (counts should not increase)
    print("\n[STEP 14] Verifying upsert (counts should NOT increase)...", flush=True)
    
    # Check Weaviate by test file
    result = weaviate_writer.client.query.aggregate(class_name).with_where({
        "path": ["source_file"],
        "operator": "Equal",
        "valueString": test_source_file
    }).with_meta_count().do()
    weaviate_count_after = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
    print(f"  [OK] Weaviate count after: {weaviate_count_after} (was {weaviate_count})", flush=True)
    
    # Check Neo4j by block_id (most reliable)
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {block_id: $block_id}) RETURN count(b) as count",
            block_id=block_id
        )
        neo4j_count_after = result.single()["count"]
        print(f"  [OK] Neo4j count after: {neo4j_count_after} (was {neo4j_count})", flush=True)
    
    # Verify upsert worked - counts should be the same
    assert weaviate_count_after == weaviate_count, f"Weaviate upsert failed: count changed from {weaviate_count} to {weaviate_count_after}"
    assert neo4j_count_after == neo4j_count, f"Neo4j upsert failed: count changed from {neo4j_count} to {neo4j_count_after}"
    
    print("\n" + "="*60, flush=True)
    print("[SUCCESS] UPSERT TEST PASSED - No duplicates created!", flush=True)
    print("="*60 + "\n", flush=True)



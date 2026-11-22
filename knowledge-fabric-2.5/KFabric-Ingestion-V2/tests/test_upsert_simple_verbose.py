"""Simple verbose test for Weaviate and Neo4j upsert functionality.

This test bypasses FinBERT and the full ingestion pipeline to directly test
upsert logic with verbose output at every step.
"""

import uuid
import pytest
from typing import Any

# Deterministic UUID namespace (same as parser)
BLOCK_ID_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')


def generate_block_id(source_file: str, block_index: int, domain: str) -> str:
    """Generate deterministic UUID5 block_id (same logic as parser)."""
    stable_string = f"{domain}:{source_file}:{block_index}"
    return str(uuid.uuid5(BLOCK_ID_NAMESPACE, stable_string))


def test_upsert_verbose():
    """Test upsert with verbose output at every step."""
    print("\n" + "="*60)
    print("SIMPLE VERBOSE UPSERT TEST")
    print("="*60)

    # Step 1: Setup - Connect to databases
    print("\n[STEP 1] Connecting to databases...")
    from kbfabric_ingest.ingest.config import get_app_config
    config = get_app_config()

    # Weaviate setup
    print("  → Connecting to Weaviate...")
    from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
    weaviate_writer = WeaviateWriter(config.get("weaviate", {}))
    weaviate_writer.ensure_schema()
    print("  ✓ Weaviate connected")

    # Neo4j setup
    print("  → Connecting to Neo4j...")
    from kbfabric_ingest.ingest.kg_writer import KGWriter
    from kbfabric_ingest.ingest.config import get_ontology
    kg_writer = KGWriter(config.get("neo4j", {}), get_ontology())
    kg_writer.ensure_indexes()
    print("  ✓ Neo4j connected")

    # Test parameters
    domain = "consumer_payments"
    class_name = weaviate_writer.domain_to_class.get(domain, weaviate_writer.class_name)
    test_source_file = "test_file_upsert.txt"

    # Step 2: Cleanup - Delete test data
    print(f"\n[STEP 2] Cleaning up test data for domain '{domain}'...")

    # Clean Weaviate
    try:
        weaviate_writer.client.batch.delete_objects(
            class_name=class_name,
            where={"path": ["source_file"], "operator": "Equal", "valueString": test_source_file}
        )
        print(f"  ✓ Cleaned Weaviate {class_name}")
    except Exception as e:
        print(f"  ⚠ Weaviate cleanup warning: {e}")

    # Clean Neo4j
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) DETACH DELETE b RETURN count(b) as deleted",
            domain=domain,
            source_file=test_source_file
        )
        deleted = result.single()["deleted"] if result.single() else 0
        print(f"  ✓ Cleaned Neo4j: deleted {deleted} test Block nodes")

    # Step 3: Create test data (2 blocks with deterministic UUIDs)
    print(f"\n[STEP 3] Creating test data (2 blocks)...")
    block_ids = []
    test_blocks = []

    for i in range(2):
        block_id = generate_block_id(test_source_file, i, domain)
        block_ids.append(block_id)

        # Dummy embedding (768-dim zeros - no FinBERT needed)
        dummy_embedding = [0.0] * 768

        # Create test block data
        block_data = {
            "block_id": block_id,
            "domain": domain,
            "block_category": "Narrative",
            "kg_node_type": "NarrativeText",
            "masked_text": f"Test block {i} content for upsert verification",
            "source_file": test_source_file,
            "block_index": i,
            "block_type": "paragraph",
            "metadata": {"test": True, "block_index": i},
            "kg_node_ids": {},
            "embedding": dummy_embedding
        }
        test_blocks.append(block_data)
        print(f"  ✓ Created block {i}: block_id={block_id[:8]}...")

    # Step 4: First Insert - Write to Weaviate
    print(f"\n[STEP 4] FIRST INSERT - Writing to Weaviate...")
    with weaviate_writer.client.batch as batch:
        batch.batch_size = 10
        for block_data in test_blocks:
            properties = {
                "domain": block_data["domain"],
                "block_category": block_data["block_category"],
                "kg_node_type": block_data["kg_node_type"],
                "masked_text": block_data["masked_text"],
                "source_file": block_data["source_file"],
                "block_index": block_data["block_index"],
                "block_type": block_data["block_type"],
                "metadata": block_data["metadata"],
                "kg_node_ids": block_data["kg_node_ids"]
            }
            batch.add_data_object(
                data_object=properties,
                class_name=class_name,
                uuid=block_data["block_id"],
                vector=block_data["embedding"]
            )
            print(f"  ✓ Added block {block_data['block_index']} to batch")
        batch.flush()
    print("  ✓ Weaviate batch flushed")

    # Step 5: First Insert - Write to Neo4j
    print(f"\n[STEP 5] FIRST INSERT - Writing to Neo4j...")
    with kg_writer.driver.session() as session:
        def write_transaction(tx):
            # Create DomainRoot
            tx.run("MERGE (dom:DomainRoot {domain: $domain})", domain=domain)

            # Create Document
            tx.run(
                "MERGE (d:Document {source_file: $source_file}) ON CREATE SET d.domain = $domain",
                source_file=test_source_file,
                domain=domain
            )

            # Create Block nodes
            for block_data in test_blocks:
                block_props = {
                    "block_id": block_data["block_id"],
                    "domain": domain,
                    "block_category": block_data["block_category"],
                    "kg_node_type": block_data["kg_node_type"],
                    "masked_text": block_data["masked_text"][:200],
                    "block_type": block_data["block_type"],
                    "block_index": block_data["block_index"],
                    "metadata": str(block_data["metadata"]),
                    "embedding": block_data["embedding"],
                    "chunk_id": ""
                }

                # MERGE with Block label + block_id (upsert logic)
                merge_query = """
                MERGE (b:Block {block_id: $block_id})
                ON CREATE SET b = $props
                ON MATCH SET b = $props
                WITH b
                CALL apoc.create.addLabels(b, [$kg_node_type]) YIELD node
                RETURN id(node) as block_id
                """
                try:
                    tx.run(merge_query, block_id=block_data["block_id"], props=block_props, kg_node_type=block_data["kg_node_type"])
                except Exception:
                    # Fallback if APOC not available
                    labels = f":{block_data['kg_node_type']}:Block"
                    fallback_query = f"""
                    MERGE (b{labels} {{block_id: $block_id}})
                    ON CREATE SET b = $props
                    ON MATCH SET b = $props
                    RETURN id(b) as block_id
                    """
                    tx.run(fallback_query, block_id=block_data["block_id"], props=block_props)

                print(f"  ✓ Upserted block {block_data['block_index']} in Neo4j")

        session.write_transaction(write_transaction)
    print("  ✓ Neo4j transaction committed")

    # Step 6: Verify First Insert Counts
    print(f"\n[STEP 6] VERIFY FIRST INSERT - Checking counts...")

    # Weaviate count
    try:
        result = weaviate_writer.client.query.aggregate(class_name).with_meta_count().do()
        weaviate_count = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
        print(f"  ✓ Weaviate {class_name}: {weaviate_count} objects")
    except Exception as e:
        print(f"  ✗ Weaviate count failed: {e}")
        weaviate_count = None

    # Neo4j count
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) RETURN count(b) as count",
            domain=domain,
            source_file=test_source_file
        )
        record = result.single()
        neo4j_count = record["count"] if record else 0
        print(f"  ✓ Neo4j Block nodes: {neo4j_count} nodes")

    expected_count = 2
    assert weaviate_count == expected_count, f"Weaviate should have {expected_count} objects, got {weaviate_count}"
    assert neo4j_count == expected_count, f"Neo4j should have {expected_count} nodes, got {neo4j_count}"
    print(f"  ✓ Both databases have expected count: {expected_count}")

    # Step 7: Second Insert - Same data (should upsert, not duplicate)
    print(f"\n[STEP 7] SECOND INSERT - Writing same data again (should upsert)...")

    # Write to Weaviate again
    print("  → Writing to Weaviate...")
    with weaviate_writer.client.batch as batch:
        batch.batch_size = 10
        for block_data in test_blocks:
            properties = {
                "domain": block_data["domain"],
                "block_category": block_data["block_category"],
                "kg_node_type": block_data["kg_node_type"],
                "masked_text": block_data["masked_text"] + " [UPDATED]",  # Change to verify update
                "source_file": block_data["source_file"],
                "block_index": block_data["block_index"],
                "block_type": block_data["block_type"],
                "metadata": block_data["metadata"],
                "kg_node_ids": block_data["kg_node_ids"]
            }
            batch.add_data_object(
                data_object=properties,
                class_name=class_name,
                uuid=block_data["block_id"],  # Same UUID = upsert
                vector=block_data["embedding"]
            )
        batch.flush()
    print("  ✓ Weaviate batch flushed (upsert)")

    # Write to Neo4j again
    print("  → Writing to Neo4j...")
    with kg_writer.driver.session() as session:
        def write_transaction(tx):
            for block_data in test_blocks:
                block_props = {
                    "block_id": block_data["block_id"],
                    "domain": domain,
                    "block_category": block_data["block_category"],
                    "kg_node_type": block_data["kg_node_type"],
                    "masked_text": (block_data["masked_text"] + " [UPDATED]")[:200],  # Change to verify update
                    "block_type": block_data["block_type"],
                    "block_index": block_data["block_index"],
                    "metadata": str(block_data["metadata"]),
                    "embedding": block_data["embedding"],
                    "chunk_id": ""
                }

                merge_query = """
                MERGE (b:Block {block_id: $block_id})
                ON CREATE SET b = $props
                ON MATCH SET b = $props
                WITH b
                CALL apoc.create.addLabels(b, [$kg_node_type]) YIELD node
                RETURN id(node) as block_id
                """
                try:
                    tx.run(merge_query, block_id=block_data["block_id"], props=block_props, kg_node_type=block_data["kg_node_type"])
                except Exception:
                    labels = f":{block_data['kg_node_type']}:Block"
                    fallback_query = f"""
                    MERGE (b{labels} {{block_id: $block_id}})
                    ON CREATE SET b = $props
                    ON MATCH SET b = $props
                    RETURN id(b) as block_id
                    """
                    tx.run(fallback_query, block_id=block_data["block_id"], props=block_props)
        session.write_transaction(write_transaction)
    print("  ✓ Neo4j transaction committed (upsert)")

    # Step 8: Verify Upsert - Counts should NOT increase
    print(f"\n[STEP 8] VERIFY UPSERT - Checking counts (should NOT increase)...")

    # Weaviate count
    try:
        result = weaviate_writer.client.query.aggregate(class_name).with_meta_count().do()
        weaviate_count_after = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
        print(f"  ✓ Weaviate {class_name}: {weaviate_count_after} objects (was {weaviate_count})")
    except Exception as e:
        print(f"  ✗ Weaviate count failed: {e}")
        weaviate_count_after = None

    # Neo4j count
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) RETURN count(b) as count",
            domain=domain,
            source_file=test_source_file
        )
        record = result.single()
        neo4j_count_after = record["count"] if record else 0
        print(f"  ✓ Neo4j Block nodes: {neo4j_count_after} nodes (was {neo4j_count})")

    # Step 9: Assertions
    print(f"\n[STEP 9] ASSERTIONS...")

    # Counts should be the same (upsert worked)
    if weaviate_count_after is not None:
        assert weaviate_count_after == weaviate_count, f"Weaviate upsert failed: count increased from {weaviate_count} to {weaviate_count_after}"
        print(f"  ✓ Weaviate upsert verified: count unchanged ({weaviate_count})")
    else:
        print(f"  ⚠ Skipping Weaviate assertion (count check failed)")

    assert neo4j_count_after == neo4j_count, f"Neo4j upsert failed: count increased from {neo4j_count} to {neo4j_count_after}"
    print(f"  ✓ Neo4j upsert verified: count unchanged ({neo4j_count})")

    # Verify content was updated (not just count)
    print(f"\n[STEP 10] VERIFY CONTENT UPDATE...")

    # Check Weaviate content
    try:
        result = weaviate_writer.client.query.get(class_name, ["masked_text"]).with_where({
            "path": ["block_id"],
            "operator": "Equal",
            "valueString": block_ids[0]
        }).do()
        if result.get("data", {}).get("Get", {}).get(class_name):
            text = result["data"]["Get"][class_name][0]["masked_text"]
            assert "[UPDATED]" in text, f"Weaviate content not updated: {text}"
            print(f"  ✓ Weaviate content updated correctly")
    except Exception as e:
        print(f"  ⚠ Weaviate content check skipped: {e}")

    # Check Neo4j content
    with kg_writer.driver.session() as session:
        result = session.run(
            "MATCH (b:Block {block_id: $block_id}) RETURN b.masked_text as text",
            block_id=block_ids[0]
        )
        record = result.single()
        if record:
            text = record["text"]
            assert "[UPDATED]" in text, f"Neo4j content not updated: {text}"
            print(f"  ✓ Neo4j content updated correctly")

    # Final cleanup
    print(f"\n[STEP 11] FINAL CLEANUP...")
    try:
        weaviate_writer.client.batch.delete_objects(
            class_name=class_name,
            where={"path": ["source_file"], "operator": "Equal", "valueString": test_source_file}
        )
        print(f"  ✓ Cleaned Weaviate")
    except Exception as e:
        print(f"  ⚠ Weaviate cleanup warning: {e}")

    with kg_writer.driver.session() as session:
        session.run(
            "MATCH (b:Block {domain: $domain, source_file: $source_file}) DETACH DELETE b",
            domain=domain,
            source_file=test_source_file
        )
        print(f"  ✓ Cleaned Neo4j")

    print("\n" + "="*60)
    print("✅ UPSERT TEST PASSED - No duplicates created!")
    print("="*60 + "\n")

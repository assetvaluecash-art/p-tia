"""Minimal test that prints immediately to diagnose hanging issues."""

import sys
print("TEST FILE LOADED", file=sys.stderr, flush=True)

def test_minimal_print():
    """Minimal test that just prints."""
    print("="*60, flush=True)
    print("MINIMAL TEST STARTED", flush=True)
    print("="*60, flush=True)
    
    # Test 1: Basic Python works
    print("\n[TEST 1] Basic Python...", flush=True)
    assert 1 + 1 == 2
    print("  [OK] Basic Python works", flush=True)
    
    # Test 2: Import config (might hang here)
    print("\n[TEST 2] Importing config...", flush=True)
    try:
        from kbfabric_ingest.ingest.config import get_app_config
        print("  [OK] Config imported", flush=True)
        config = get_app_config()
        print(f"  [OK] Config loaded: {list(config.keys())}", flush=True)
    except Exception as e:
        print(f"  [ERROR] Config import failed: {e}", flush=True)
        raise
    
    # Test 3: Check Weaviate connection (might hang here)
    print("\n[TEST 3] Checking Weaviate connection...", flush=True)
    try:
        import weaviate
        weaviate_url = config.get("weaviate", {}).get("url", "http://localhost:8080")
        print(f"  [CONNECT] Connecting to {weaviate_url}...", flush=True)
        client = weaviate.Client(weaviate_url, timeout_config=(5, 5))  # 5 second timeout
        print("  [OK] Weaviate client created", flush=True)
        # Try a simple query
        schema = client.schema.get()
        print(f"  [OK] Weaviate connected: {len(schema.get('classes', []))} classes", flush=True)
    except Exception as e:
        print(f"  [ERROR] Weaviate connection failed: {e}", flush=True)
        print(f"  [INFO] Is Weaviate running at {weaviate_url}?", flush=True)
        raise
    
    # Test 4: Check Neo4j connection (might hang here)
    print("\n[TEST 4] Checking Neo4j connection...", flush=True)
    try:
        from neo4j import GraphDatabase
        neo4j_uri = config.get("neo4j", {}).get("uri", "bolt://localhost:7687")
        neo4j_user = config.get("neo4j", {}).get("user", "neo4j")
        neo4j_password = config.get("neo4j", {}).get("password", "")
        print(f"  [CONNECT] Connecting to {neo4j_uri}...", flush=True)
        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password), connection_timeout=5)
        print("  [OK] Neo4j driver created", flush=True)
        # Try a simple query
        with driver.session() as session:
            result = session.run("RETURN 1 as test")
            record = result.single()
            print(f"  [OK] Neo4j connected: test query returned {record['test']}", flush=True)
        driver.close()
    except Exception as e:
        print(f"  [ERROR] Neo4j connection failed: {e}", flush=True)
        print(f"  [INFO] Is Neo4j running at {neo4j_uri}?", flush=True)
        raise
    
    print("\n" + "="*60, flush=True)
    print("[SUCCESS] ALL CONNECTIVITY TESTS PASSED", flush=True)
    print("="*60 + "\n", flush=True)



"""Utility functions to reset/clear Weaviate and Neo4j datastores."""

import sys
from typing import Any

import weaviate
from neo4j import GraphDatabase

from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


def clear_weaviate(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Clear all data from Weaviate.
    
    Args:
        config: Optional Weaviate config dict. If None, reads from app config.
        
    Returns:
        Dictionary with deletion summary
    """
    if config is None:
        app_config = get_app_config()
        config = app_config["weaviate"]
    
    url = config["url"]
    api_key = config.get("api_key") or None
    
    results = {
        "classes_deleted": [],
        "objects_deleted": 0,
        "errors": []
    }
    
    try:
        # Initialize client with timeout to prevent hanging (10 second timeout)
        # For weaviate-client v3, timeout_config is a tuple: (query_timeout, insert_timeout)
        timeout_config = (10, 10)  # 10 second timeout for both query and insert
        try:
            if api_key:
                client = weaviate.Client(url, auth_client_secret=weaviate.AuthApiKey(api_key), timeout_config=timeout_config)
            else:
                client = weaviate.Client(url, timeout_config=timeout_config)
        except TypeError:
            # Fallback if timeout_config not supported in this version
            if api_key:
                client = weaviate.Client(url, auth_client_secret=weaviate.AuthApiKey(api_key))
            else:
                client = weaviate.Client(url)
        
        # Get all schema classes
        schema = client.schema.get()
        class_names = [cls.get("class") for cls in schema.get("classes", [])]
        
        if not class_names:
            logger.info("No Weaviate classes found to delete")
            print("[RESET] No Weaviate classes found", flush=True)
            return results
        
        print(f"[RESET] Found {len(class_names)} Weaviate classes", flush=True)
        
        # Delete objects from each class, then delete the class
        for class_name in class_names:
            try:
                # Count objects before deletion
                count_result = client.query.aggregate(class_name).with_meta_count().do()
                count = count_result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
                
                if count > 0:
                    # Delete all objects in the class using a match-all filter
                    # Weaviate requires a where clause, so we use a filter that matches everything
                    try:
                        client.batch.delete_objects(
                            class_name=class_name,
                            where={
                                "path": ["domain"],
                                "operator": "Like",
                                "valueString": "*"  # Match any domain
                            }
                        )
                        # Also try deleting without where clause (newer Weaviate versions)
                    except Exception:
                        # Fallback: delete class schema which will cascade delete objects
                        pass
                    results["objects_deleted"] += count
                    print(f"  [RESET] Deleted {count} objects from {class_name}", flush=True)
                
                # Delete the class schema
                client.schema.delete_class(class_name)
                results["classes_deleted"].append(class_name)
                print(f"  [RESET] Deleted class schema: {class_name}", flush=True)
                
            except Exception as e:
                error_msg = f"Failed to delete {class_name}: {str(e)}"
                results["errors"].append(error_msg)
                logger.warning("Failed to delete Weaviate class", class_name=class_name, error=str(e))
                print(f"  [RESET] ERROR: {error_msg}", flush=True)
        
        logger.info("Weaviate cleared", classes_deleted=len(results["classes_deleted"]), objects_deleted=results["objects_deleted"])
        print(f"[RESET] Weaviate cleared: {len(results['classes_deleted'])} classes, {results['objects_deleted']} objects", flush=True)
        
    except Exception as e:
        error_msg = f"Weaviate clear failed: {str(e)}"
        results["errors"].append(error_msg)
        logger.exception("Failed to clear Weaviate", error=str(e))
        print(f"[RESET] ERROR: {error_msg}", flush=True)
        raise
    
    return results


def clear_neo4j(config: dict[str, Any] | None = None, preserve_indexes: bool = True) -> dict[str, Any]:
    """
    Clear all data from Neo4j.
    
    Args:
        config: Optional Neo4j config dict. If None, reads from app config.
        preserve_indexes: If True, keeps indexes and constraints (default: True)
        
    Returns:
        Dictionary with deletion summary
    """
    if config is None:
        app_config = get_app_config()
        config = app_config["neo4j"]
    
    uri = config["uri"]
    user = config["user"]
    password = config["password"]
    
    results = {
        "nodes_deleted": 0,
        "relationships_deleted": 0,
        "errors": []
    }
    
    try:
        # Use connection pooling for reliability
        driver = GraphDatabase.driver(
            uri, 
            auth=(user, password),
            max_connection_pool_size=10,  # Small pool for reset operations
            connection_timeout=30
        )
        
        with driver.session() as session:
            # Count nodes and relationships before deletion
            count_result = session.run("MATCH (n) RETURN count(n) as node_count")
            record = count_result.single()
            node_count = record["node_count"] if record else 0
            
            count_result = session.run("MATCH ()-[r]->() RETURN count(r) as rel_count")
            record = count_result.single()
            rel_count = record["rel_count"] if record else 0
            
            print(f"[RESET] Found {node_count} nodes and {rel_count} relationships in Neo4j", flush=True)
            
            # Delete all nodes and relationships
            delete_result = session.run("MATCH (n) DETACH DELETE n RETURN count(n) as deleted")
            record = delete_result.single()
            deleted_count = record["deleted"] if record else 0
            
            results["nodes_deleted"] = deleted_count
            results["relationships_deleted"] = rel_count  # Relationships are deleted with nodes
            
            # Optionally drop indexes (but keep constraints)
            if not preserve_indexes:
                try:
                    indexes_result = session.run("SHOW INDEXES")
                    indexes = [record["name"] for record in indexes_result]
                    for index_name in indexes:
                        try:
                            session.run(f"DROP INDEX {index_name} IF EXISTS")
                            print(f"  [RESET] Dropped index: {index_name}", flush=True)
                        except Exception as e:
                            logger.warning("Failed to drop index", index_name=index_name, error=str(e))
                except Exception as e:
                    logger.warning("Failed to list/drop indexes", error=str(e))
            
            logger.info("Neo4j cleared", nodes_deleted=results["nodes_deleted"], relationships_deleted=results["relationships_deleted"])
            print(f"[RESET] Neo4j cleared: {results['nodes_deleted']} nodes, {results['relationships_deleted']} relationships", flush=True)
        
        driver.close()
        
    except Exception as e:
        error_msg = f"Neo4j clear failed: {str(e)}"
        results["errors"].append(error_msg)
        logger.exception("Failed to clear Neo4j", error=str(e))
        print(f"[RESET] ERROR: {error_msg}", flush=True)
        raise
    
    return results


def reset_all(preserve_indexes: bool = True) -> dict[str, Any]:
    """
    Reset both Weaviate and Neo4j datastores.
    
    Args:
        preserve_indexes: If True, keeps Neo4j indexes (default: True)
        
    Returns:
        Dictionary with reset summary from both datastores
    """
    print("\n" + "=" * 80, flush=True)
    print("RESETTING DATASTORES", flush=True)
    print("=" * 80, flush=True)
    
    app_config = get_app_config()
    
    results = {
        "weaviate": {},
        "neo4j": {},
        "success": True
    }
    
    # Clear Weaviate
    try:
        print("\n[RESET] Clearing Weaviate...", flush=True)
        results["weaviate"] = clear_weaviate(app_config.get("weaviate", {}))
    except Exception as e:
        results["weaviate"]["error"] = str(e)
        results["success"] = False
        logger.exception("Weaviate reset failed", error=str(e))
    
    # Clear Neo4j
    try:
        print("\n[RESET] Clearing Neo4j...", flush=True)
        results["neo4j"] = clear_neo4j(app_config.get("neo4j", {}), preserve_indexes=preserve_indexes)
    except Exception as e:
        results["neo4j"]["error"] = str(e)
        results["success"] = False
        logger.exception("Neo4j reset failed", error=str(e))
    
    print("\n" + "=" * 80, flush=True)
    if results["success"]:
        print("[OK] DATASTORES RESET COMPLETE", flush=True)
    else:
        print("[WARN] DATASTORES RESET COMPLETED WITH ERRORS", flush=True)
    print("=" * 80 + "\n", flush=True)
    
    return results


def main():
    """CLI entry point for reset_datastores."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Reset Weaviate and Neo4j datastores")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Reset both Weaviate and Neo4j (default)"
    )
    parser.add_argument(
        "--weaviate-only",
        action="store_true",
        help="Reset only Weaviate"
    )
    parser.add_argument(
        "--neo4j-only",
        action="store_true",
        help="Reset only Neo4j"
    )
    parser.add_argument(
        "--drop-indexes",
        action="store_true",
        help="Also drop Neo4j indexes (default: preserve indexes)"
    )
    
    args = parser.parse_args()
    
    # Default to --all if no specific option given
    if not args.weaviate_only and not args.neo4j_only:
        args.all = True
    
    app_config = get_app_config()
    
    if args.all:
        reset_all(preserve_indexes=not args.drop_indexes)
    elif args.weaviate_only:
        clear_weaviate(app_config.get("weaviate", {}))
    elif args.neo4j_only:
        clear_neo4j(app_config.get("neo4j", {}), preserve_indexes=not args.drop_indexes)
    
    sys.exit(0)


if __name__ == "__main__":
    main()



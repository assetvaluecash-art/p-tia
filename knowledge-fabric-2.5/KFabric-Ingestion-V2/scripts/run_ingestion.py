#!/usr/bin/env python
"""Direct ingestion script - No Ray required.

This script directly calls the IngestionService to ingest documents
without going through the Ray scheduler or API.

Usage:
    cd kbfabric-ingest
    python scripts/run_ingestion.py --domain consumer_retail
    python scripts/run_ingestion.py --domain wealth_securities_management --input-dir drop_zone/wealth_securities_management
    python scripts/run_ingestion.py --domain risk_compliance_fraud --clear-first
"""

import argparse
import io
import sys
import time
from datetime import datetime
from pathlib import Path


class _TeeWriter:
    """Write to both the original stream and a file."""

    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file

    def write(self, data):
        self._original.write(data)
        try:
            self._log_file.write(data)
        except Exception:
            pass

    def flush(self):
        self._original.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass

    def fileno(self):
        return self._original.fileno()

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbfabric_ingest._common.config import BASE_DIR, get_app_config, get_domains_list
from kbfabric_ingest._common.logger import get_logger
from kbfabric_ingest.ingest.service import IngestionService
from kbfabric_ingest.ingest.config import get_store_name_for_domain

logger = get_logger(__name__)


def clear_domain_data(domain: str) -> None:
    """Clear existing data for a domain from Weaviate and Neo4j."""
    config = get_app_config()
    
    print(f"\n{'='*60}")
    print(f"CLEARING EXISTING DATA FOR DOMAIN: {domain}")
    print(f"{'='*60}")
    
    # Clear Weaviate
    try:
        import weaviate
        weaviate_config = config.get("weaviate", {})
        # startup_period=None skips wait_for_weaviate() (needed for Weaviate Cloud)
        api_key = weaviate_config.get("api_key")
        client_kwargs = {
            "url": weaviate_config.get("url", "http://localhost:8080"),
            "startup_period": None,
        }
        if api_key:
            client_kwargs["auth_client_secret"] = weaviate.AuthApiKey(api_key)
        client = weaviate.Client(**client_kwargs)
        
        # Use get_store_name_for_domain to get the correct Weaviate class name
        class_name = get_store_name_for_domain(domain)
        
        if client.schema.exists(class_name):
            client.schema.delete_class(class_name)
            print(f"  [OK] Deleted Weaviate class: {class_name}")
        else:
            print(f"  [--] Weaviate class {class_name} does not exist")
            
    except Exception as e:
        print(f"  [FAIL] Weaviate clear failed: {e}")
    
    # Clear Neo4j
    try:
        from neo4j import GraphDatabase
        neo4j_config = config.get("neo4j", {})
        driver = GraphDatabase.driver(
            neo4j_config.get("uri", "bolt://localhost:7687"),
            auth=(neo4j_config.get("user", "neo4j"), neo4j_config.get("password", "test123$"))
        )
        
        with driver.session() as session:
            # Delete all nodes with this domain
            result = session.run(
                "MATCH (n {domain: $domain}) DETACH DELETE n RETURN count(n) as deleted",
                domain=domain
            )
            deleted = result.single()["deleted"]
            print(f"  [OK] Deleted {deleted} Neo4j nodes for domain: {domain}")
        
        driver.close()
        
    except Exception as e:
        print(f"  [FAIL] Neo4j clear failed: {e}")
    
    print()


def run_ingestion(domain: str, input_dir: Path, clear_first: bool = False, skip_text_ie: bool = False) -> dict:
    """Run ingestion for a domain.
    
    Args:
        domain: Domain name (e.g., 'consumer_retail')
        input_dir: Path to directory containing documents
        clear_first: If True, clear existing data before ingesting
        
    Returns:
        Dictionary with ingestion results
    """
    start_time = time.time()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print("\n" + "=" * 70)
    print("  KnowledgeFabric Direct Ingestion")
    print("=" * 70)
    print(f"  Domain:     {domain}")
    print(f"  Input Dir:  {input_dir}")
    print(f"  Started:    {timestamp}")
    print(f"  Clear First: {clear_first}")
    print(f"  Skip Text IE: {skip_text_ie}")
    print("=" * 70 + "\n")
    
    # Verify input directory exists
    if not input_dir.exists():
        print(f"ERROR: Input directory does not exist: {input_dir}")
        return {"status": "error", "error": f"Input directory not found: {input_dir}"}
    
    # List files to be processed
    files = list(input_dir.glob("*.*"))
    supported_extensions = {".docx", ".pdf", ".xlsx", ".pptx", ".txt"}
    doc_files = [f for f in files if f.suffix.lower() in supported_extensions]
    
    print(f"Found {len(doc_files)} documents to process:")
    for f in doc_files:
        print(f"  • {f.name}")
    print()
    
    if not doc_files:
        print("ERROR: No supported documents found")
        return {"status": "error", "error": "No documents found"}
    
    # Clear existing data if requested
    if clear_first:
        clear_domain_data(domain)
    
    # Initialize and run ingestion service
    try:
        print("Initializing IngestionService...")
        service = IngestionService()
        print("IngestionService initialized successfully\n")

        # Tee stdout to a log file so the report's Log tab has full content
        logs_dir = Path("logs/ingestion_jobs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_log_path = logs_dir / f"stdout_{domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        stdout_log_fh = open(stdout_log_path, "w", encoding="utf-8", errors="replace")
        original_stdout = sys.stdout
        sys.stdout = _TeeWriter(original_stdout, stdout_log_fh)

        try:
            print("Starting ingestion pipeline...")
            print("-" * 50)

            result = service.run_ingestion(
                str(input_dir), domain,
                skip_text_ie=skip_text_ie,
                job_output_log_path=str(stdout_log_path),
            )

            print("-" * 50)
            print("\nIngestion completed!")
        finally:
            sys.stdout = original_stdout
            stdout_log_fh.close()
            service.close()
        
    except Exception as e:
        logger.exception("Ingestion failed", error=str(e))
        print(f"\nERROR: Ingestion failed: {e}")
        return {"status": "error", "error": str(e)}
    
    # Calculate duration
    duration = time.time() - start_time
    
    # Print summary
    print("\n" + "=" * 70)
    print("  INGESTION SUMMARY")
    print("=" * 70)
    print(f"  Status:           {result.get('status', 'unknown')}")
    print(f"  Duration:         {duration:.1f} seconds")
    print(f"  Files Processed:  {result.get('total_files', 0)}")
    print(f"  Blocks Extracted: {result.get('total_blocks', 0)}")
    print(f"  Weaviate Chunks:  {result.get('weaviate_chunks', 0)}")
    print(f"  Neo4j Nodes:      {result.get('neo4j_nodes', 0)}")
    print(f"  Neo4j Relations:  {result.get('neo4j_relationships', 0)}")
    store_actual = result.get("store_actual", {})
    if store_actual:
        print(f"\n  --- Store Reconciliation (live query) ---")
        print(f"  Neo4j Actual Nodes:    {store_actual.get('neo4j_nodes', '?')}")
        print(f"  Neo4j Actual Rels:     {store_actual.get('neo4j_relationships', '?')}")
        print(f"  Neo4j Orphan Entities: {store_actual.get('neo4j_orphans', '?')}")
        print(f"  Weaviate Actual:       {store_actual.get('weaviate_chunks', '?')}")
        by_label = store_actual.get("neo4j_by_label", {})
        if by_label:
            print(f"  Neo4j by label:")
            for lbl, cnt in sorted(by_label.items(), key=lambda x: -x[1]):
                print(f"    {lbl}: {cnt}")
    spine = result.get("spine_counts", {})
    if spine:
        print(f"\n  Spine: {spine}")
    
    # Report paths
    report_paths = result.get("report_paths", {})
    if report_paths:
        print("\n  Reports Generated:")
        for report_type, path in report_paths.items():
            print(f"    • {report_type}: {path}")
    
    # Errors
    errors = result.get("errors", [])
    if errors:
        print(f"\n  ⚠️ Errors ({len(errors)}):")
        for err in errors[:5]:
            print(f"    • {err}")
        if len(errors) > 5:
            print(f"    ... and {len(errors) - 5} more")
    
    print("=" * 70 + "\n")
    
    return {
        "status": result.get("status", "unknown"),
        "duration_sec": duration,
        "domain": domain,
        "files_processed": result.get("total_files", 0),
        "blocks_extracted": result.get("total_blocks", 0),
        "weaviate_chunks": result.get("weaviate_chunks", 0),
        "neo4j_nodes": result.get("neo4j_nodes", 0),
        "neo4j_relationships": result.get("neo4j_relationships", 0),
        "report_paths": report_paths,
        "errors": errors,
    }


def main():
    # Get available domains dynamically from config
    try:
        available_domains = get_domains_list()
        if not available_domains:
            print("ERROR: No domains found in config. Check config/kbfabric.yaml")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load domains from config: {e}")
        sys.exit(1)
    
    parser = argparse.ArgumentParser(
        description="Direct ingestion script for KnowledgeFabric",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  python scripts/run_ingestion.py --domain consumer_retail
  python scripts/run_ingestion.py --domain wealth_securities_management --clear-first
  python scripts/run_ingestion.py --domain risk_compliance_fraud --input-dir drop_zone/risk_compliance_fraud

Available domains: {', '.join(available_domains)}
        """
    )
    
    parser.add_argument(
        "--domain", "-d",
        required=True,
        choices=available_domains,
        help=f"Domain to ingest documents for (choices: {', '.join(available_domains)})"
    )
    
    parser.add_argument(
        "--input-dir", "-i",
        type=str,
        default=None,
        help="Path to input directory (default: drop_zone/{domain})"
    )
    
    parser.add_argument(
        "--clear-first", "-c",
        action="store_true",
        help="Clear existing data for domain before ingesting"
    )
    
    parser.add_argument(
        "--skip-text-ie",
        action="store_true",
        help="Skip text IE processing (entity/relationship extraction) - useful for fast vision testing"
    )
    
    args = parser.parse_args()
    
    # Determine input directory
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.is_absolute():
            input_dir = BASE_DIR / args.input_dir
    else:
        input_dir = BASE_DIR / "samples" / args.domain
    
    # Run ingestion
    result = run_ingestion(args.domain, input_dir, args.clear_first, args.skip_text_ie)
    
    # Exit with appropriate code
    if result.get("status") in ["success", "completed"]:
        print("[SUCCESS] Ingestion completed successfully")
        sys.exit(0)
    else:
        print("[FAILED] Ingestion failed or had errors")
        sys.exit(1)


if __name__ == "__main__":
    main()


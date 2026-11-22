"""CLI entry point for ingestion."""

import sys
from pathlib import Path

from kbfabric_ingest.ingest.ingestion_service import IngestionService


def main() -> None:
    """Main CLI entry point."""
    # Parse --input-dir and --domain arguments
    input_dir = None
    domain = None
    
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--input-dir" and i < len(sys.argv) - 1:
            input_dir = sys.argv[i + 1]
        elif arg.startswith("--input-dir="):
            input_dir = arg.split("=", 1)[1]
        elif arg == "--domain" and i < len(sys.argv) - 1:
            domain = sys.argv[i + 1]
        elif arg.startswith("--domain="):
            domain = arg.split("=", 1)[1]
    
    if not input_dir:
        print("Usage: python -m kbfabric.ingest.cli --input-dir <directory> --domain <domain>")
        print("\nDomain must be one of: trade_finance, wealth_portfolio, consumer_payments")
        sys.exit(1)
    
    if not domain:
        print("Error: --domain argument is required")
        print("Usage: python -m kbfabric.ingest.cli --input-dir <directory> --domain <domain>")
        print("\nDomain must be one of: trade_finance, wealth_portfolio, consumer_payments")
        sys.exit(1)
    
    input_path = Path(input_dir)
    if not input_path.exists() or not input_path.is_dir():
        print(f"Error: Input directory does not exist: {input_dir}")
        sys.exit(1)
    
    # Run ingestion
    service = IngestionService()
    result = service.run_ingestion(input_path, domain=domain)
    
    # Print summary
    print("\n" + "=" * 60)
    print("Ingestion Summary")
    print("=" * 60)
    print(f"Input Directory: {result['input_dir']}")
    print(f"Domain: {result['domain']}")
    print(f"Total Files: {result['total_files']}")
    print(f"Total Blocks: {result['total_blocks']}")
    print(f"\nJSON Report: {result['report_paths']['json']}")
    print(f"HTML Report: {result['report_paths']['html']}")
    print("=" * 60)


if __name__ == "__main__":
    main()



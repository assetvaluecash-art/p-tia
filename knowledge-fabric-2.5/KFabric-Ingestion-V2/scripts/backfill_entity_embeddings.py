#!/usr/bin/env python3
"""
One-time backfill for KG entity embeddings in Neo4j.

Uses the configured offline EmbeddingsClient via LLMClient.
Does not touch Block, Document, or DomainRoot nodes.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kbfabric_ingest.ingest.config import get_app_config, get_ontology
from kbfabric_ingest.ingest.kg_writer import KGWriter
from kbfabric_ingest.llm.client import LLMClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing KG entity embeddings in Neo4j")
    parser.add_argument("--domain", required=True, help="Domain key, e.g. corporate_commercial")
    parser.add_argument("--dry-run", action="store_true", help="Report missing embeddings without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing non-empty embeddings")
    parser.add_argument("--batch-size", type=int, default=100, help="Embedding batch size")
    args = parser.parse_args()

    config = get_app_config()
    ontology = get_ontology()
    llm_client = LLMClient(config)
    kg_writer = KGWriter(config.get("neo4j", {}), ontology, llm_client=llm_client)

    domain = args.domain
    print(f"\n{'=' * 60}")
    print(f"KG ENTITY EMBEDDING BACKFILL — {domain}")
    print(f"{'=' * 60}")

    before = kg_writer.get_entity_embedding_stats(domain)
    print(
        f"\nBEFORE: total={before['total']} null={before['embedding_null']} "
        f"empty={before['embedding_empty']} populated={before['embedding_populated']}"
    )

    missing = before["embedding_null"] + before["embedding_empty"]
    if not args.force and missing == 0:
        print("\nNo missing embeddings found. Nothing to do.")
        kg_writer.driver.close()
        return 0

    if args.dry_run:
        print(f"\nDRY RUN: would backfill up to {missing} nodes (force={args.force})")
        kg_writer.driver.close()
        return 0

    result = kg_writer.ensure_missing_entity_embeddings(
        domain, batch_size=args.batch_size, force=args.force,
    )
    print(
        f"\nBACKFILL: scanned={result.get('scanned', 0)} embedded={result.get('embedded', 0)} "
        f"skipped={result.get('skipped', 0)} failed={result.get('failed', 0)}"
    )

    after = kg_writer.get_entity_embedding_stats(domain)
    print(
        f"\nAFTER: total={after['total']} null={after['embedding_null']} "
        f"empty={after['embedding_empty']} populated={after['embedding_populated']}"
    )

    with kg_writer.driver.session() as session:
        dim_rows = list(session.run(
            """
            MATCH (e)
            WHERE e.domain = $domain
              AND e.kg_node_type IS NOT NULL
              AND e.embedding IS NOT NULL
              AND size(e.embedding) > 0
              AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
            RETURN labels(e)[0] AS node_type,
                   count(*) AS count,
                   min(size(e.embedding)) AS min_dim,
                   max(size(e.embedding)) AS max_dim
            ORDER BY count DESC
            """,
            domain=domain,
        ))
        if dim_rows:
            print("\nEmbedding dimensions by label:")
            for row in dim_rows:
                print(
                    f"  {row['node_type']}: count={row['count']} "
                    f"min_dim={row['min_dim']} max_dim={row['max_dim']}"
                )

    kg_writer.driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

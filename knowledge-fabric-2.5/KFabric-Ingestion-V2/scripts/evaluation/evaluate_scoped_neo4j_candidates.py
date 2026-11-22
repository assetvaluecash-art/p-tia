#!/usr/bin/env python3
"""Read-only candidate recall and relationship coverage audit for the active dev customer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from neo4j import READ_ACCESS
    from kbfabric_ingest._common.env_config import load_dotenv_file
    from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
    from kbfabric_ingest.ingest.config import get_app_config
    from kbfabric_ingest.ingest.lane2_node_reconciliation import (
        IncomingNode, Neo4jCandidateReader, RealEmbeddingProvider,
        make_neo4j_driver_from_config, normalize_entity_value, score_candidates,
    )

    load_dotenv_file()
    customer = CustomerRepository().get_active_customer()
    domain = "consumer_retail"
    cases = json.loads((ROOT / "tests/evaluation/fixtures/kg_candidate_cases.json").read_text())
    driver = make_neo4j_driver_from_config(get_app_config())
    reader = Neo4jCandidateReader(
        driver, default_limit=1000, customer_id=customer.customer_id,
        tenant_id=str(customer.tenant_id),
    )
    from custom_ml.embeddings.embeddings_client import EmbeddingsClient
    from kbfabric_ingest.ingest.lane2_node_reconciliation import HIGH_FUSION_THRESHOLD
    embedding_model_dir = ROOT / "custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0"
    embedder = EmbeddingsClient({"embeddings": {
        "local_model_path": str(embedding_model_dir),
        "model_name": "Snowflake/snowflake-arctic-embed-l-v2.0",
        "dimension": 1024, "device": "cpu",
    }})
    semantic = RealEmbeddingProvider(lambda texts: embedder.encode(texts))
    results = []
    try:
        for case in cases:
            node_type = case["required_node_type"]
            incoming = IncomingNode(
                temporary_id=case["case_id"], node_type=node_type,
                name=case["query"], domain=domain,
            )
            candidates = reader.fetch_candidates(incoming, domain, limit=1000)
            gold_keys = {normalize_entity_value(value) for value in case["gold_name_aliases"]}
            gold_entity_ids = {
                candidate.entity_id for candidate in candidates
                if gold_keys.intersection({
                    normalize_entity_value(str(value))
                    for value in [candidate.name, candidate.name_display, *(candidate.aliases or [])]
                    if value
                })
            } if gold_keys else set()
            gold_found = bool(gold_entity_ids)
            ranked, _ = score_candidates(
                incoming, candidates, semantic=semantic, top_k=3, min_fusion=0.0
            )
            top = ranked[0] if ranked else None
            accepted = bool(top and top.fusion_score >= HIGH_FUSION_THRESHOLD)
            accepted_at_090 = bool(top and top.fusion_score >= 0.90)
            selected_correctly = bool(top and top.candidate.entity_id in gold_entity_ids)
            results.append({
                "case_id": case["case_id"], "gold_candidate_id": case["gold_candidate_id"],
                "gold_exists_in_retrieved_pool": gold_found,
                "retrieved_candidate_count": len(candidates),
                "top_candidate": (top.candidate.name_display or top.candidate.name) if top else None,
                "top_fusion_score": round(top.fusion_score, 6) if top else None,
                "accepted_at_current_0_80_threshold": accepted,
                "accepted_at_0_90_fusion_threshold": accepted_at_090,
                "selected_gold_candidate": selected_correctly,
                "accepted_prediction_correct": selected_correctly if accepted else None,
                "retrieved_candidates": [
                    {"entity_id": c.entity_id, "type": c.kg_node_type,
                     "name": c.name_display or c.name,
                     "has_embedding": bool(c.embedding),
                     "embedding_dimensions": len(c.embedding or [])}
                    for c in candidates
                ],
            })
            print(
                f"[neo4j-candidate-read] {case['case_id']}: required_type={node_type} "
                f"pool={len(candidates)} gold_present={gold_found} "
                f"top={(top.candidate.name_display or top.candidate.name) if top else None!r} "
                f"fusion={(top.fusion_score if top else 0.0):.3f} accepted_at_0.80={accepted}", flush=True,
            )
        with driver.session(default_access_mode=READ_ACCESS) as session:
            relationship_rows = list(session.run(
                """MATCH (source)-[r]->(target)
                   WHERE r.customer_id=$customer_id AND r.tenant_id=$tenant_id AND r.domain=$domain
                   RETURN type(r) AS rel_type,
                          coalesce(source.kg_node_type, labels(source)[0]) AS source_type,
                          source.name AS source_name,
                          coalesce(target.kg_node_type, labels(target)[0]) AS target_type,
                          target.name AS target_name""",
                customer_id=customer.customer_id, tenant_id=str(customer.tenant_id), domain=domain,
            ))
        expected = [
            {"source_type":"Process", "source_name":"Credit Limit Review", "rel_type":"GOVERNED_BY", "target_type":"Rule", "target_name":"Rule Syn 001"},
            {"source_type":"Rule", "source_name":"Rule Syn 001", "rel_type":"ENFORCED_BY", "target_type":"Regulation", "target_name":"Reg Syn 001"},
            {"source_type":"Control", "source_name":"Ctrl Syn 001", "rel_type":"MITIGATES", "target_type":"Rule", "target_name":"Rule Syn 001"},
        ]
        def edge_key(edge):
            return (edge["source_type"], normalize_entity_value(edge["source_name"]),
                    edge["rel_type"], edge["target_type"], normalize_entity_value(edge["target_name"]))
        observed = {edge_key(dict(row)) for row in relationship_rows}
        expected_rows = []
        for edge in expected:
            exists = edge_key(edge) in observed
            expected_rows.append({**edge, "present_in_graph": exists})
            print(f"[neo4j-edge-read] {edge['source_type']} -{edge['rel_type']}-> {edge['target_type']}: present={exists}", flush=True)
        summary = {
            "customer_id": customer.customer_id, "tenant_id": str(customer.tenant_id),
            "domain": domain, "candidate_reader_query_count": reader.read_query_count,
            "positive_cases": sum(case["gold_candidate_id"] is not None for case in cases),
            "positive_gold_found": sum(row["gold_exists_in_retrieved_pool"] for row in results if row["gold_candidate_id"] is not None),
            "positive_candidate_recall": round(
                sum(row["gold_exists_in_retrieved_pool"] for row in results if row["gold_candidate_id"] is not None)
                / max(1, sum(case["gold_candidate_id"] is not None for case in cases)), 4
            ),
            "existing_0_80_and_requested_0_90_fusion_thresholds": {
                str(threshold): {
                    "accepted_cases": sum(
                        row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold
                        for row in results
                    ),
                    "accepted_correct": sum(
                        row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold
                        and row["selected_gold_candidate"] for row in results
                    ),
                    "accepted_incorrect_or_false_link": sum(
                        row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold
                        and row["selected_gold_candidate"] is not True for row in results
                    ),
                    "observed_precision": round(
                        sum(row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold
                            and row["selected_gold_candidate"] for row in results)
                        / max(1, sum(row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold for row in results)), 4
                    ),
                    "positive_case_recall": round(
                        sum(row["top_fusion_score"] is not None and row["top_fusion_score"] >= threshold
                            and row["selected_gold_candidate"] and row["gold_candidate_id"] is not None for row in results)
                        / max(1, sum(row["gold_candidate_id"] is not None for row in results)), 4
                    ),
                }
                for threshold in (HIGH_FUSION_THRESHOLD, 0.90)
            },
            "score_note": "Weighted lexical/semantic fusion score is not a calibrated probability.",
            "expected_relationships": expected_rows,
            "relationship_coverage": round(sum(row["present_in_graph"] for row in expected_rows) / len(expected_rows), 4),
            "note": "Read-only current-graph candidate availability; separates absent gold entities from ranking errors.",
        }
        print(json.dumps({"summary": summary, "cases": results}, indent=2))
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

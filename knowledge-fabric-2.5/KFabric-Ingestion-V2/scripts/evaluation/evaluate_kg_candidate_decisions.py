#!/usr/bin/env python3
"""Evaluate local retrieval, RRF, reranking and abstention on labeled synthetic cases."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from custom_ml.reranking.decision_benchmark import (  # noqa: E402
    Candidate, eligible_candidates, ranking_metrics, reciprocal_rank_fusion,
)
from kbfabric_ingest.ingest.lane2_node_reconciliation import (  # noqa: E402
    cosine_similarity, lexical_similarity,
)


def _target_type_ranked_candidates(case: dict, embeddings: dict[str, list[float]]) -> list[Candidate]:
    query_vector = embeddings[case["query"]]
    eligible = eligible_candidates(
        [Candidate(**item) for item in case["candidates"]],
        required_node_type=case.get("required_node_type"),
    )
    scored = []
    for candidate in eligible:
        lexical = lexical_similarity(case["query"], candidate.text)
        semantic = cosine_similarity(query_vector, embeddings[candidate.text])
        scored.append((candidate, lexical, semantic))
    lexical_order = {
        row[0].candidate_id: rank
        for rank, row in enumerate(sorted(scored, key=lambda row: (-row[1], row[0].candidate_id)), 1)
    }
    semantic_order = {
        row[0].candidate_id: rank
        for rank, row in enumerate(sorted(scored, key=lambda row: (-row[2], row[0].candidate_id)), 1)
    }
    return [
        Candidate(
            candidate_id=candidate.candidate_id,
            text=candidate.text,
            lexical_rank=lexical_order[candidate.candidate_id],
            semantic_rank=semantic_order[candidate.candidate_id],
            node_type=candidate.node_type,
        )
        for candidate, _, _ in scored
    ]


def _threshold_report(rows: list[dict], target_precision: float) -> dict:
    """Select the broadest in-sample raw-score threshold meeting target precision."""
    thresholds = sorted({float(row["top_raw_score"]) for row in rows if row["top_raw_score"] is not None})
    feasible = []
    for threshold in thresholds:
        admitted = [row for row in rows if row["top_raw_score"] is not None and row["top_raw_score"] >= threshold]
        if not admitted:
            continue
        correct = sum(row["prediction"] == row["gold"] for row in admitted)
        precision = correct / len(admitted)
        if precision >= target_precision:
            feasible.append((len(admitted), threshold, precision, admitted))
    if not feasible:
        return {"target_precision": target_precision, "achievable": False, "note": "No observed raw-score threshold reached target precision."}
    count, threshold, precision, admitted = max(feasible, key=lambda item: (item[0], item[2], -item[1]))
    positives = sum(row["gold"] is not None for row in rows)
    true_positives = sum(row["prediction"] == row["gold"] and row["gold"] is not None for row in admitted)
    return {
        "target_precision": target_precision,
        "achievable": True,
        "raw_logit_threshold": round(threshold, 6),
        "empirical_precision": round(precision, 4),
        "admitted_cases": count,
        "coverage": round(count / len(rows), 4) if rows else 0.0,
        "positive_case_recall": round(true_positives / positives, 4) if positives else 0.0,
        "warning": "Exploratory in-sample threshold on a small synthetic set; not calibrated and not suitable for production admission.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/evaluation/fixtures/kg_candidate_cases.json")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--target-precision", type=float, default=0.90)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    if not 0 < args.target_precision <= 1:
        parser.error("--target-precision must be in (0, 1]")

    cases = json.loads(args.fixture.read_text())
    unique_texts = list(dict.fromkeys(
        [case["query"] for case in cases]
        + [candidate["text"] for case in cases for candidate in case["candidates"]]
    ))
    from custom_ml.embeddings.embeddings_client import EmbeddingsClient
    embedding_model_dir = ROOT / "custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0"
    embedder = EmbeddingsClient({"embeddings": {
        "local_model_path": str(embedding_model_dir),
        "model_name": "Snowflake/snowflake-arctic-embed-l-v2.0",
        "dimension": 1024,
        "device": "cpu",
    }})
    print(f"[retrieval] Encoding {len(unique_texts)} unique query/candidate descriptions using local Arctic model", flush=True)
    embedding_started = time.perf_counter()
    vectors = embedder.encode(unique_texts)
    embeddings = dict(zip(unique_texts, vectors))
    embedding_ms = (time.perf_counter() - embedding_started) * 1000
    print(f"[retrieval] Local embedding complete in {embedding_ms:.1f} ms", flush=True)

    from custom_ml.reranking.reranker_client import RerankerClient
    reranker_dir = ROOT / "custom_ml/reranking/models/mmarco-mMiniLMv2-L12-H384-v1"
    cross_encoder = RerankerClient({"reranking": {"local_model_path": str(reranker_dir)}})

    rows = []
    for case in cases:
        ranked_candidates = reciprocal_rank_fusion(
            _target_type_ranked_candidates(case, embeddings)
        )
        gold = case["gold_candidate_id"]
        gold_in_pool = any(c.candidate_id == gold for c in ranked_candidates) if gold else False
        shortlist = ranked_candidates[:args.top_k]
        gold_in_shortlist = any(c.candidate_id == gold for c in shortlist) if gold else False
        docs = [f"type: {c.node_type}; name and evidence: {c.text}" for c in shortlist]
        raw_scores = cross_encoder.predict([(case["query"], doc) for doc in docs]) if docs else []
        ce_ranked = sorted(zip(shortlist, raw_scores), key=lambda pair: (-float(pair[1]), pair[0].candidate_id))
        prediction = ce_ranked[0][0].candidate_id if ce_ranked else None
        top_raw_score = float(ce_ranked[0][1]) if ce_ranked else None
        reciprocal_rank = next((1.0 / i for i, c in enumerate(ce_ranked, 1) if c[0].candidate_id == gold), 0.0) if gold else 0.0
        correct = prediction == gold
        row = {
            "case_id": case["case_id"], "gold": gold, "prediction": prediction,
            "gold_in_type_eligible_pool": gold_in_pool,
            "gold_in_rrf_top_k": gold_in_shortlist,
            "rrf_recall_at_k": gold_in_shortlist,
            "cross_encoder_reciprocal_rank": reciprocal_rank,
            "top_raw_score": top_raw_score,
            "correct": correct,
            "false_link": gold is None and prediction is not None,
        }
        rows.append(row)
        print(
            f"[case] {case['case_id']}: gold_in_pool={gold_in_pool} gold_in_top{args.top_k}={gold_in_shortlist} "
            f"gold={gold!r} predicted={prediction!r} raw_score={top_raw_score!r} "
            f"correct={correct} false_link={row['false_link']}", flush=True,
        )

    positive_cases = [row for row in rows if row["gold"] is not None]
    summary = {
        "cases": len(rows),
        "positive_cases": len(positive_cases),
        "type_eligible_recall": round(mean(float(r["gold_in_type_eligible_pool"]) for r in positive_cases), 4) if positive_cases else 0.0,
        "rrf_recall_at_k": round(mean(float(r["gold_in_rrf_top_k"]) for r in positive_cases), 4) if positive_cases else 0.0,
        "cross_encoder_top1_accuracy": round(mean(float(r["correct"]) for r in rows), 4) if rows else 0.0,
        "cross_encoder_mrr": round(mean(float(r["cross_encoder_reciprocal_rank"]) for r in positive_cases), 4) if positive_cases else 0.0,
        "no_match_false_links": sum(bool(r["false_link"]) for r in rows),
        "target_precision_abstention_threshold": _threshold_report(rows, args.target_precision),
    }
    print(json.dumps({"fixture": str(args.fixture), "summary": summary, "cases": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

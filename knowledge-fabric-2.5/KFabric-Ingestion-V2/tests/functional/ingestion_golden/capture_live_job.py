#!/usr/bin/env python3
"""Persist live API snapshots and HITL evidence for a single QA job."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests


def append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6002")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--domain-id", required=True)
    parser.add_argument("--product-line-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--timeout-minutes", type=int, default=180)
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"}
    session = requests.Session()
    session.headers.update(headers)
    endpoint = f"{args.base_url.rstrip('/')}/jobs/{quote(args.job_id, safe='')}"
    snapshots = args.output_dir / "job-snapshots.jsonl"
    reviews_file = args.output_dir / "hitl-review-items.jsonl"
    deadline = time.monotonic() + max(1, args.timeout_minutes) * 60
    terminal = {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED"}

    while time.monotonic() < deadline:
        observed_at = datetime.now(timezone.utc).isoformat()
        try:
            response = session.get(endpoint, timeout=30)
            row: dict[str, object] = {
                "observed_at": observed_at,
                "http_status": response.status_code,
                "job_id": args.job_id,
            }
            if response.ok:
                detail = response.json()
                row.update({
                    "status": detail.get("status"),
                    "current_stage_id": detail.get("current_stage_id"),
                    "progress_pct": detail.get("progress_pct"),
                    "entities_found": detail.get("entities_found"),
                    "relationships_found": detail.get("relationships_found"),
                    "updated_at": detail.get("updated_at"),
                    "hitl_summary": detail.get("hitl_summary"),
                    "stages": [
                        {key: stage.get(key) for key in (
                            "stage_id", "status", "attempt", "started_at", "completed_at",
                            "duration_ms", "checkpoint_id", "error_json", "skip_reason", "progress_json",
                        )}
                        for stage in detail.get("stages", []) if isinstance(stage, dict)
                    ],
                })
                append_jsonl(snapshots, row)
                print(json.dumps({key: row.get(key) for key in (
                    "observed_at", "status", "current_stage_id", "progress_pct",
                    "entities_found", "relationships_found",
                )}), flush=True)
                if str(detail.get("status", "")).upper() in terminal:
                    break
            else:
                row["response_excerpt"] = response.text[:1000]
                append_jsonl(snapshots, row)
                print(json.dumps(row), flush=True)
        except Exception as exc:  # preserve transient API failures as evidence
            append_jsonl(snapshots, {
                "observed_at": observed_at,
                "job_id": args.job_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
        time.sleep(max(1, args.interval_seconds))

    review_url = f"{args.base_url.rstrip('/')}/hitl/reviews"
    params = {
        "ingestion_batch_id": args.batch_id,
        "domain_id": args.domain_id,
        "product_line_id": args.product_line_id,
        "status": "PENDING",
        "limit": 200,
        "offset": 0,
    }
    total = 0
    offset = 0
    while True:
        params["offset"] = offset
        try:
            response = session.get(review_url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            total = int(payload.get("total", 0) or 0)
            items = payload.get("items", []) or []
            for item in items:
                mapping = item.get("mapping_context_json", {}) or {}
                source = item.get("source_context_json", {}) or {}
                append_jsonl(reviews_file, {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "review_id": item.get("review_id"),
                    "queue_type": item.get("queue_type"),
                    "status": item.get("status"),
                    "reason_code": item.get("reason_code"),
                    "incoming_entity": mapping.get("incoming_entity"),
                    "mapping_decision": mapping.get("decision"),
                    "source_evidence": {
                        "source_file": source.get("source_file"),
                        "source_location": source.get("source_location"),
                        "masked_text": str(source.get("masked_text") or "")[:1200],
                    },
                    "context_resolution": item.get("context_resolution_json", []),
                    "execution_plan": item.get("execution_plan_json"),
                    "provenance": item.get("provenance_json"),
                })
            if offset + len(items) >= total or not items:
                break
            offset += len(items)
        except Exception as exc:
            append_jsonl(reviews_file, {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "offset": offset,
            })
            break
    summary = {
        "job_id": args.job_id,
        "batch_id": args.batch_id,
        "pending_review_total": total,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_rows": sum(1 for _ in snapshots.open(encoding="utf-8")) if snapshots.exists() else 0,
        "review_item_rows": sum(1 for _ in reviews_file.open(encoding="utf-8")) if reviews_file.exists() else 0,
    }
    (args.output_dir / "capture-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

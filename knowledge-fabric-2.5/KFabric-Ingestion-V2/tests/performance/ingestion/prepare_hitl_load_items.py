#!/usr/bin/env python3
"""Collect unique real pending review items and prepare valid varied decisions."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


def candidate_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    mapping = item.get("mapping_context_json") or {}
    candidates = mapping.get("candidate_scores") or []
    incoming = mapping.get("incoming_entity") or {}
    for raw in candidates:
        candidate = raw.get("candidate", raw) if isinstance(raw, dict) else {}
        if candidate.get("kg_node_type") == incoming.get("node_type"):
            return candidate
    return None


def decision_for(item: dict[str, Any], position: int) -> dict[str, Any]:
    queue_type = item.get("queue_type")
    mapping = item.get("mapping_context_json") or {}
    plan = item.get("execution_plan_json") or {}
    if queue_type == "NODE_ADMISSION":
        incoming = mapping.get("incoming_entity") or {}
        eligible = candidate_payload(item)
        options: list[dict[str, Any]] = []
        if incoming.get("name") and incoming.get("node_type"):
            options.append({"action": "APPROVE_AS_NEW", "notes": "Golden functional/load validation: approve persisted evidence as a new entity."})
        if eligible and eligible.get("entity_id"):
            options.append({"action": "APPROVE_REUSE", "selected_entity_id": eligible["entity_id"], "notes": "Golden functional/load validation: reuse a persisted ranked candidate."})
            if str(incoming.get("name") or "").casefold() != str(eligible.get("name") or "").casefold():
                options.append({"action": "APPROVE_REUSE_WITH_ALIAS", "selected_entity_id": eligible["entity_id"], "notes": "Golden functional/load validation: approve a scoped alias."})
        options.extend([
            {"action": "REJECT", "notes": "Golden functional/load validation: reject this candidate with audit evidence."},
            {"action": "LEAVE_UNRESOLVED", "notes": "Golden functional/load validation: preserve pending review state."},
        ])
        return options[position % len(options)]

    resolutions = plan.get("context_resolutions") or item.get("context_resolution_json") or []
    for resolution in resolutions:
        candidates = resolution.get("candidates") or []
        selected = next((candidate for candidate in candidates if candidate.get("entity_id") and candidate.get("node_type") == resolution.get("target_type")), None)
        if position % 3 == 1:
            return {"action": "REJECT", "notes": "Golden functional/load validation: reject with append-only reviewer evidence."}
        if position % 3 == 2:
            return {"action": "LEAVE_UNRESOLVED", "notes": "Golden functional/load validation: leave relationship pending."}
        if selected:
            return {
                "action": "APPROVE_REUSE",
                "notes": "Golden functional/load validation: select a persisted ontology-compatible endpoint candidate.",
                "endpoint_choices": [{
                    "rel_type": resolution["rel_type"],
                    "direction": resolution.get("direction", "outgoing"),
                    "target_type": resolution["target_type"],
                    "target_value": resolution["target_value"],
                    "entity_id": selected["entity_id"],
                }],
            }
    return {"action": "LEAVE_UNRESOLVED", "notes": "No persisted eligible endpoint candidate; retain item for review."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default="test-hitl-fixture-reader")
    parser.add_argument("--scopes", type=Path, help="JSON list of batch/domain/product_line scopes from the golden functional run")
    parser.add_argument("--journey-manifest", type=Path, help="Manifest emitted by run_industry_policy_matrix.py")
    parser.add_argument("--job-ids", nargs="*", help="Resolve scopes from job details; useful when preparing directly after golden runs")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum", type=int, default=140, help="Needs 20+30+40+50 unique pending items for full ramp")
    args = parser.parse_args()
    sources = sum(bool(value) for value in (args.scopes, args.journey_manifest, args.job_ids))
    if sources != 1:
        parser.error("provide exactly one of --scopes, --journey-manifest, or --job-ids")
    if args.journey_manifest:
        journey = json.loads(args.journey_manifest.read_text(encoding="utf-8"))
        scopes = [
            {key: job[key] for key in ("ingestion_batch_id", "domain_id", "product_line_id")}
            for job in journey.get("jobs", [])
            if all(job.get(key) for key in ("ingestion_batch_id", "domain_id", "product_line_id"))
        ]
    else:
        scopes = json.loads(args.scopes.read_text(encoding="utf-8")) if args.scopes else []
    if not isinstance(scopes, list):
        parser.error("--scopes must contain a JSON array")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {args.token}"
    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    failures: list[dict[str, Any]] = []
    scope_results: list[dict[str, Any]] = []
    for job_id in args.job_ids or []:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            response = session.get(f"{args.base_url.rstrip('/')}/jobs/{quote(job_id, safe='')}", timeout=30)
            if response.status_code != 200:
                failure = {"job_id": job_id, "http_status": response.status_code, "body": response.text[:300]}
                failures.append(failure)
                scope_results.append({"case_id": f"HITL-JOB-SCOPE-{len(scope_results)+1:03}", "method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py"], "started_at": started_at, "result": "FAIL", "expected": "HTTP 200 job detail with batch/domain/product-line scope", "actual": failure})
                continue
            detail = response.json()
            scope = {key: detail[key] for key in ("ingestion_batch_id", "domain_id", "product_line_id")}
            if not all(scope.values()):
                raise ValueError("job detail omitted one or more review scope identifiers")
            scopes.append(scope)
            scope_results.append({"case_id": f"HITL-JOB-SCOPE-{len(scope_results)+1:03}", "method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py"], "started_at": started_at, "result": "PASS", "expected": "HTTP 200 job detail with batch/domain/product-line scope", "actual": {"http_status": response.status_code, **scope}})
        except Exception as exc:
            failure = {"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            scope_results.append({"case_id": f"HITL-JOB-SCOPE-{len(scope_results)+1:03}", "method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py"], "started_at": started_at, "result": "FAIL", "expected": "job detail with batch/domain/product-line scope", "actual": failure})
    for scope in scopes:
        started_at = datetime.now(timezone.utc).isoformat()
        params = {
            "ingestion_batch_id": scope["ingestion_batch_id"],
            "domain_id": scope["domain_id"],
            "product_line_id": scope["product_line_id"],
            "status": "PENDING",
            "limit": 200,
            "offset": 0,
        }
        try:
            response = session.get(f"{args.base_url.rstrip('/')}/hitl/reviews", params=params, timeout=30)
            if response.status_code != 200:
                failure = {"scope": params, "http_status": response.status_code, "body": response.text[:300]}
                failures.append(failure)
                scope_results.append({"case_id": f"HITL-PREP-{len(scope_results)+1:03}", "method": "GET", "path": "/hitl/reviews", "touchpoints": ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py"], "started_at": started_at, "result": "FAIL", "expected": "HTTP 200 scoped review list", "actual": failure})
                continue
            body = response.json()
            scope_results.append({
                "case_id": f"HITL-PREP-{len(scope_results)+1:03}",
                "method": "GET", "path": "/hitl/reviews", "touchpoints": ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py"], "started_at": started_at,
                "result": "PASS",
                "expected": "HTTP 200 and only pending review items in this frozen scope",
                "actual": {"http_status": response.status_code, "pending_count": len(body.get("items", [])), "total": body.get("total")},
            })
            for item in body.get("items", []):
                review_id = str(item.get("review_id") or "")
                if not review_id or review_id in seen or item.get("status") != "PENDING":
                    continue
                seen.add(review_id)
                decision_request = decision_for(item, len(all_items))
                prepared = {
                    "review_id": review_id,
                    "ingestion_batch_id": params["ingestion_batch_id"],
                    "domain_id": params["domain_id"],
                    "product_line_id": params["product_line_id"],
                    "queue_type": item.get("queue_type"),
                    "expected_graph_write": decision_request.get("action", "").startswith("APPROVE"),
                    "decision_request": decision_request,
                }
                all_items.append(prepared)
        except Exception as exc:
            failure = {"scope": params, "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            scope_results.append({"case_id": f"HITL-PREP-{len(scope_results)+1:03}", "method": "GET", "path": "/hitl/reviews", "touchpoints": ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py"], "started_at": started_at, "result": "FAIL", "expected": "scoped review API response", "actual": failure})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows_output = args.output.with_suffix(".jsonl")
    if rows_output.exists():
        raise FileExistsError(f"Evidence already exists; choose a new output filename: {rows_output}")
    report = {
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "source": "real pending HITL items returned by scoped API reads",
        "count": len(all_items),
        "minimum_required": args.minimum,
        "result": "PASS" if len(all_items) >= args.minimum else "FAIL",
        "minimum_case": {
            "case_id": "HITL-PREP-MINIMUM-COUNT",
            "method": "GET",
            "path": "/hitl/reviews (aggregate across prepared scopes)",
            "touchpoints": ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py"],
            "expected": args.minimum,
            "actual": len(all_items),
            "result": "PASS" if len(all_items) >= args.minimum else "FAIL",
        },
        "distinct_actions": sorted({item["decision_request"]["action"] for item in all_items}),
        "expected_graph_write_count": sum(item["decision_request"]["action"].startswith("APPROVE") for item in all_items),
        "read_failures": failures,
        "scope_results": scope_results,
        "items": all_items,
    }
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    evidence_rows = [*scope_results, report["minimum_case"]]
    with rows_output.open("w", encoding="utf-8") as stream:
        for row in evidence_rows:
            row.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
            row["source"] = "real scoped HITL API reads; no synthetic review IDs"
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=2))
    print(f"Evidence rows: {rows_output}")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

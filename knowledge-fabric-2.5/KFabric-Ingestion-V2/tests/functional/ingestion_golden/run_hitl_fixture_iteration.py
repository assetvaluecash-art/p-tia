#!/usr/bin/env python3
"""Submit one regenerated 10-page fixture to a fresh QA customer and capture evidence."""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests


def append(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6002")
    parser.add_argument("--token", required=True)
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--domain-id", required=True)
    parser.add_argument("--product-line-id", required=True)
    parser.add_argument("--policy", choices=("review_all", "review_ambiguous"), default="review_ambiguous")
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--wait-minutes", type=int, default=60)
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.token}"})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    input_dir = args.output_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.asset, input_dir / args.asset.name)
    ledger = args.output_dir / "functional-cases.jsonl"
    started = datetime.now(timezone.utc).isoformat()
    request_body = {
        "customer_id": args.customer_id,
        "input_dir": str(input_dir),
        "domain": args.domain_id,
        "product_line_id": args.product_line_id,
        "document_nature": "qe_assets",
        "hitl_mode": args.policy,
        "force_rewrite": False,
        "context_tags": ["golden_fixture_iteration=true", f"review_policy={args.policy}"],
    }
    submit = session.post(f"{args.base_url.rstrip('/')}/jobs", json=request_body, timeout=120)
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-SUBMIT", "observed_at": started,
                    "method": "POST", "path": "/jobs", "http_status": submit.status_code,
                    "request": request_body, "response": submit.json() if submit.headers.get("content-type", "").startswith("application/json") else submit.text[:1000],
                    "result": "PASS" if submit.status_code in (200, 202) else "FAIL"})
    if not submit.ok:
        return 1
    job = submit.json()
    job_id = job["job_id"]
    detail_url = f"{args.base_url.rstrip('/')}/jobs/{quote(job_id, safe='')}"
    deadline = time.monotonic() + args.wait_minutes * 60
    snapshots = args.output_dir / "job-snapshots.jsonl"
    terminal = {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED"}
    while time.monotonic() < deadline:
        response = session.get(detail_url, timeout=60)
        try:
            detail = response.json()
        except Exception:
            detail = {"response_excerpt": response.text[:1000]}
        append(snapshots, {"observed_at": datetime.now(timezone.utc).isoformat(),
                           "http_status": response.status_code, "snapshot": detail})
        if response.ok and str(detail.get("status", "")).upper() in terminal:
            break
        time.sleep(5)
    else:
        detail = {"status": "POLL_TIMEOUT", "job_id": job_id}

    batch_id = detail.get("ingestion_batch_id") or job_id
    stages = detail.get("stages", []) or []
    stage_ids = {stage.get("stage_id") for stage in stages if isinstance(stage, dict)}
    expected_stage_ids = {
        "run.queued", "run.validated", "context.frozen", "dependencies.ready",
        "documents.parsed", "blocks.prepared", "privacy.applied", "lane2.pass1",
        "lane2.pass2", "ontology.admission", "entity.mapping",
        "relationship.resolution", "admission.plan", "stores.write", "run.verify_report",
    }
    stages_terminal = all(stage.get("status") in {"SUCCEEDED", "SKIPPED"}
                          for stage in stages if isinstance(stage, dict))
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-TERMINAL", "observed_at": datetime.now(timezone.utc).isoformat(),
                    "method": "GET", "path": f"/jobs/{job_id}", "http_status": response.status_code,
                    "job_id": job_id, "run_id": detail.get("run_id"), "batch_id": batch_id,
                    "customer_id": args.customer_id, "tenant_id": args.tenant_id,
                    "domain_id": args.domain_id, "product_line_id": args.product_line_id,
                    "policy": args.policy, "status": detail.get("status"),
                    "current_stage_id": detail.get("current_stage_id"), "progress_pct": detail.get("progress_pct"),
                    "entities_found": detail.get("entities_found"), "relationships_found": detail.get("relationships_found"),
                    "stages": detail.get("stages", []),
                    "result": "PASS" if detail.get("status") in terminal else "FAIL"})
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-15-STAGES",
                    "observed_at": datetime.now(timezone.utc).isoformat(), "job_id": job_id,
                    "expected": "All 15 registered stages are present and terminal.",
                    "actual": {"stage_count": len(stages), "stage_ids_match": stage_ids == expected_stage_ids,
                               "stages_terminal": stages_terminal, "stages": stages},
                    "result": "PASS" if stage_ids == expected_stage_ids and stages_terminal else "FAIL"})

    logs_response = session.get(f"{args.base_url.rstrip('/')}/jobs/{quote(job_id, safe='')}/logs", timeout=120)
    admission_stats = None
    if logs_response.ok:
        worker_logs = str((logs_response.json() or {}).get("logs", ""))
        (args.output_dir / "worker-logs.txt").write_text(worker_logs, encoding="utf-8")
        for line in worker_logs.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if event.get("event") == "kg_admission.completed":
                admission_stats = event
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-ADMISSION-EVIDENCE",
                    "observed_at": datetime.now(timezone.utc).isoformat(), "method": "GET",
                    "path": f"/jobs/{job_id}/logs", "http_status": logs_response.status_code,
                    "admission_event": admission_stats,
                    "result": "PASS" if logs_response.ok and admission_stats else "FAIL"})

    queue = []
    offset = 0
    total = 0
    while True:
        queue_response = session.get(f"{args.base_url.rstrip('/')}/hitl/reviews", params={
            "ingestion_batch_id": batch_id, "domain_id": args.domain_id,
            "product_line_id": args.product_line_id, "status": "PENDING",
            "limit": 200, "offset": offset,
        }, timeout=60)
        append(ledger, {"case_id": f"FIXTURE-{args.policy}-HITL-PAGE-{offset}",
                        "observed_at": datetime.now(timezone.utc).isoformat(), "method": "GET",
                        "path": "/hitl/reviews", "http_status": queue_response.status_code,
                        "offset": offset, "result": "PASS" if queue_response.ok else "FAIL"})
        if not queue_response.ok:
            break
        payload = queue_response.json()
        page = payload.get("items", []) or []
        total = int(payload.get("total", 0) or 0)
        queue.extend(page)
        if not page or offset + len(page) >= total:
            break
        offset += len(page)

    (args.output_dir / "hitl-review-items.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in queue), encoding="utf-8")

    # Reconcile every row in the run scope, not only pending rows. Admission
    # emits one ID per queued plan; all of those IDs must belong to this batch.
    batch_reviews = []
    batch_offset = 0
    while True:
        batch_response = session.get(f"{args.base_url.rstrip('/')}/hitl/reviews", params={
            "ingestion_batch_id": batch_id, "domain_id": args.domain_id,
            "product_line_id": args.product_line_id, "limit": 200, "offset": batch_offset,
        }, timeout=60)
        append(ledger, {"case_id": f"FIXTURE-{args.policy}-HITL-ALL-PAGE-{batch_offset}",
                        "observed_at": datetime.now(timezone.utc).isoformat(), "method": "GET",
                        "path": "/hitl/reviews", "http_status": batch_response.status_code,
                        "offset": batch_offset, "result": "PASS" if batch_response.ok else "FAIL"})
        if not batch_response.ok:
            break
        payload = batch_response.json()
        page = payload.get("items", []) or []
        batch_total = int(payload.get("total", 0) or 0)
        batch_reviews.extend(page)
        if not page or batch_offset + len(page) >= batch_total:
            break
        batch_offset += len(page)
    batch_review_ids = {str(item.get("review_id")) for item in batch_reviews}
    admission_review_ids = [str(value) for value in (admission_stats or {}).get("hitl_review_ids", [])]
    missing_review_ids = sorted(set(admission_review_ids) - batch_review_ids)
    unexpected_batch_ids = sorted(batch_review_ids - set(admission_review_ids))
    reconciliation_ok = bool(
        admission_stats is not None
        and len(admission_review_ids) == int(admission_stats.get("hitl_queued", -1))
        and not missing_review_ids
        and not unexpected_batch_ids
        and len(batch_reviews) == int(admission_stats.get("hitl_queued", -1))
    )
    (args.output_dir / "hitl-review-items-all.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in batch_reviews),
        encoding="utf-8",
    )
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-HITL-BATCH-RECONCILIATION",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "expected": "Each admission review ID is persisted exactly once in this run's batch scope.",
                    "actual": {"admission_hitl_queued": (admission_stats or {}).get("hitl_queued"),
                               "admission_id_count": len(admission_review_ids),
                               "current_batch_all_status_rows": len(batch_reviews),
                               "current_batch_pending_rows": len(queue),
                               "missing_admission_ids": missing_review_ids,
                               "unexpected_batch_ids": unexpected_batch_ids},
                    "result": "PASS" if reconciliation_ok else "FAIL"})
    resolutions = [resolution for item in queue for resolution in (item.get("context_resolution_json") or [])]
    ambiguous = [resolution for resolution in resolutions if str(resolution.get("decision", "")).upper() == "RELATIONSHIP_REVIEW_REQUIRED"]
    samples = [{"review_id": item.get("review_id"), "queue_type": item.get("queue_type"),
                "reason_code": item.get("reason_code"), "incoming_entity": item.get("mapping_context_json", {}).get("incoming_entity"),
                "source_evidence": {key: (item.get("source_context_json") or {}).get(key)
                                    for key in ("source_file", "source_location", "page_number", "block_index", "masked_text")},
                "context_resolution": item.get("context_resolution_json"),
                "execution_plan": item.get("execution_plan_json"), "provenance": item.get("provenance_json")}
               for item in queue]
    (args.output_dir / "candidate-samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    ambiguous_entity_reviews = []
    for item in queue:
        mapping = item.get("mapping_context_json") or {}
        incoming = mapping.get("incoming_entity") or {}
        candidates = [entry.get("candidate", entry) for entry in (mapping.get("candidate_scores") or [])]
        same_type = [candidate for candidate in candidates
                     if candidate.get("kg_node_type") == incoming.get("node_type")]
        if str(mapping.get("decision", "")).upper() == "REQUIRES_REVIEW" and len(same_type) >= 2:
            ambiguous_entity_reviews.append({"review_id": item.get("review_id"),
                                             "incoming_name": incoming.get("name"),
                                             "incoming_type": incoming.get("node_type"),
                                             "reason_code": item.get("reason_code"),
                                             "ranked_candidates": same_type})
    if args.policy == "review_all":
        policy_ok = bool(admission_stats and int(admission_stats.get("hitl_queued", 0)) > 0
                         and int(admission_stats.get("write_now", -1)) == 0
                         and int(admission_stats.get("hitl_queued", -1)) == total
                         and reconciliation_ok)
        policy_expected = "Write eligible admissions are routed to review, no entities write automatically, and every review plan persists in this batch."
    else:
        policy_ok = bool(ambiguous_entity_reviews and ambiguous and reconciliation_ok)
        policy_expected = "A source grounded entity has two same type ranked candidates and at least one relationship endpoint has two or more candidates requiring review; all review plans persist in this batch."
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-POLICY-GATE",
                    "observed_at": datetime.now(timezone.utc).isoformat(), "expected": policy_expected,
                    "actual": {"pending_queue_count": total, "captured_review_rows": len(queue),
                               "ambiguous_entity_review_count": len(ambiguous_entity_reviews),
                               "ambiguous_examples": ambiguous_entity_reviews[:5],
                               "relationship_review_count": len(ambiguous),
                               "batch_review_count": len(batch_reviews),
                               "hitl_reconciliation_ok": reconciliation_ok,
                               "missing_admission_ids": missing_review_ids,
                               "admission_event": admission_stats},
                    "result": "PASS" if policy_ok else "FAIL"})
    report_response = session.get(f"{args.base_url.rstrip('/')}/reports/get-report/{quote(job_id, safe='')}", timeout=120)
    append(ledger, {"case_id": f"FIXTURE-{args.policy}-REPORT", "observed_at": datetime.now(timezone.utc).isoformat(),
                    "method": "GET", "path": f"/reports/get-report/{job_id}",
                    "http_status": report_response.status_code,
                    "content_type": report_response.headers.get("content-type"),
                    "content_length": len(report_response.content),
                    "result": "PASS" if report_response.status_code == 200 else "FAIL"})
    summary = {"captured_at": datetime.now(timezone.utc).isoformat(), "job_id": job_id,
               "run_id": detail.get("run_id"), "batch_id": batch_id, "customer_id": args.customer_id,
               "tenant_id": args.tenant_id, "domain_id": args.domain_id,
               "product_line_id": args.product_line_id, "policy": args.policy,
               "status": detail.get("status"), "entities_found": detail.get("entities_found"),
               "relationships_found": detail.get("relationships_found"), "pending_review_count": total,
               "queue_rows_captured": len(queue), "relationship_review_rows": len(ambiguous),
               "ambiguous_entity_review_count": len(ambiguous_entity_reviews),
               "batch_review_count": len(batch_reviews),
               "hitl_reconciliation_ok": reconciliation_ok,
               "missing_admission_ids": missing_review_ids,
               "admission_event": admission_stats, "stage_count": len(stages),
               "relationship_review_examples": ambiguous[:5],
               "policy_gate_result": "PASS" if policy_ok else "FAIL",
               "result": "PASS" if detail.get("status") in terminal and stage_ids == expected_stage_ids and stages_terminal and policy_ok and report_response.status_code == 200 else "FAIL"}
    (args.output_dir / "capture-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if summary["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

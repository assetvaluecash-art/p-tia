#!/usr/bin/env python3
"""Separate, evidence-persisting API load ramps for ingestion or HITL.

Every request outcome is appended immediately to JSONL. The script intentionally
does not cancel, delete, or clean up any accepted job or review record.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parents[3]
RAMP_LEVELS = (20, 30, 40, 50)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[low], 2)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 2)


class Evidence:
    def __init__(self, run_id: str, mode: str) -> None:
        self.run_id = run_id
        self.mode = mode
        self.output_dir = ROOT / "reports" / "performance" / run_id
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.rows_path = self.output_dir / "request-results.jsonl"
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def record(self, row: dict[str, Any]) -> None:
        row.setdefault("recorded_at", utc_now())
        row["run_id"] = self.run_id
        row["mode"] = self.mode
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            with self.rows_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
            self.rows.append(row)

    def summary(self, extra: dict[str, Any]) -> dict[str, Any]:
        report = {
            "run_id": self.run_id,
            "mode": self.mode,
            "finished_at": utc_now(),
            "request_count": len(self.rows),
            "pass_count": sum(r.get("result") == "PASS" for r in self.rows),
            "fail_count": sum(r.get("result") == "FAIL" for r in self.rows),
            "rungs": extra.get("rungs", []),
            "limitations": extra.get("limitations", []),
        }
        (self.output_dir / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report


def post_json(base_url: str, path: str, token: str, body: dict[str, Any], timeout: int) -> requests.Response:
    return requests.post(
        base_url.rstrip("/") + path,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )


def terminal_state(base_url: str, job_id: str, token: str, timeout: int) -> tuple[str, float]:
    started = time.perf_counter()
    end = time.monotonic() + timeout
    last_status = "UNKNOWN"
    while time.monotonic() < end:
        response = requests.get(
            f"{base_url.rstrip('/')}/jobs/{quote(job_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        if response.ok:
            last_status = str(response.json().get("status", "UNKNOWN"))
            if last_status in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED"}:
                break
        time.sleep(2)
    return last_status, round((time.perf_counter() - started) * 1000, 2)


def run_ingestion(args: argparse.Namespace, evidence: Evidence) -> list[dict[str, Any]]:
    asset_root = Path(args.input_dir).resolve()
    if not asset_root.is_dir():
        raise SystemExit(f"Input directory does not exist: {asset_root}")
    run_inputs = ROOT / "uploads" / "performance" / args.run_id
    run_inputs.mkdir(parents=True, exist_ok=False)
    rungs: list[dict[str, Any]] = []
    input_files = [p for p in asset_root.rglob("*") if p.is_file()]
    if not input_files:
        raise SystemExit("Input directory contains no files")

    for concurrency in args.levels:
        if concurrency not in RAMP_LEVELS:
            raise SystemExit(f"Unsupported ramp rung {concurrency}; allowed levels: {RAMP_LEVELS}")
        rung_start = time.perf_counter()
        barrier = threading.Barrier(concurrency)
        records: list[dict[str, Any]] = []

        def submit(index: int) -> dict[str, Any]:
            actor = f"test-ingestion-{args.run_id}-{concurrency}-{index}"
            user_dir = run_inputs / f"rung-{concurrency}" / f"user-{index:03}"
            user_dir.mkdir(parents=True, exist_ok=True)
            for source in input_files:
                rel = source.relative_to(asset_root)
                target = user_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            body = {
                "customer_id": args.customer_id,
                "input_dir": str(user_dir),
                "domain": args.domain,
                "product_line_id": args.product_line_id,
                "document_nature": args.document_nature,
                "hitl_mode": args.hitl_mode,
                "force_rewrite": False,
            }
            barrier.wait(timeout=60)
            started = time.perf_counter()
            row: dict[str, Any] = {
                "case_id": f"LOAD-ING-{concurrency:02}-{index:03}",
                "actor_id": actor,
                "method": "POST",
                "path": "/jobs",
                "expected": "HTTP 200/202 with a unique accepted job ID",
                "started_at": utc_now(),
            }
            try:
                response = post_json(args.base_url, "/jobs", actor, body, args.request_timeout)
                row.update(http_status=response.status_code, response_ms=round((time.perf_counter() - started) * 1000, 2))
                payload = response.json() if response.content else {}
                row["job_id"] = payload.get("job_id")
                row["actual"] = {"status": payload.get("status"), "job_id": payload.get("job_id"), "detail": payload.get("detail")}
                row["result"] = "PASS" if response.status_code in {200, 202} and payload.get("job_id") else "FAIL"
            except Exception as exc:  # one worker failure must not cancel the rung
                row.update(result="FAIL", http_status=None, actual=f"{type(exc).__name__}: {exc}", response_ms=round((time.perf_counter() - started) * 1000, 2))
            evidence.record(row)
            return row

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(submit, index) for index in range(1, concurrency + 1)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    records.append(future.result())
                except Exception as exc:
                    failure = {"case_id": f"LOAD-ING-{concurrency:02}-WORKER", "result": "FAIL", "actual": f"{type(exc).__name__}: {exc}"}
                    evidence.record(failure)
                    records.append(failure)

        def observe_terminal(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("result") != "PASS" or not row.get("job_id"):
                return row
            try:
                status, duration = terminal_state(args.base_url, row["job_id"], row["actor_id"], args.job_timeout)
            except Exception as exc:
                status, duration = "UNKNOWN", None
                evidence.record({
                    "case_id": row["case_id"] + "-TERMINAL",
                    "actor_id": row["actor_id"], "method": "GET", "path": f"/jobs/{row['job_id']}",
                    "expected": "terminal job snapshot",
                    "http_status": None,
                    "actual": f"{type(exc).__name__}: {exc}",
                    "result": "FAIL",
                })
                row["terminal_status"] = status
                row["job_duration_ms"] = duration
                row["terminal_result"] = "FAIL"
                return row
            row["terminal_status"] = status
            row["job_duration_ms"] = duration
            row["terminal_result"] = "PASS" if status in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING"} else "FAIL"
            evidence.record({
                "case_id": row["case_id"] + "-TERMINAL",
                "actor_id": row["actor_id"], "method": "GET", "path": f"/jobs/{row['job_id']}",
                "expected": "COMPLETED or COMPLETED_WITH_REVIEW_PENDING",
                "http_status": 200 if status != "UNKNOWN" else None,
                "actual": {"job_id": row["job_id"], "status": status, "job_duration_ms": duration},
                "result": row["terminal_result"],
            })

            return row

        # Poll jobs concurrently. Serial polling can consume N times the per-job
        # timeout and falsely report that otherwise-progressing workers are stuck.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(records), 50))) as pool:
            records = list(pool.map(observe_terminal, records))

        active_unknown = [row.get("job_id") for row in records if row.get("terminal_status") == "UNKNOWN"]

        response_ms = [float(row["response_ms"]) for row in records if row.get("response_ms") is not None]
        duration_ms = [float(row["job_duration_ms"]) for row in records if row.get("job_duration_ms") is not None]
        elapsed = time.perf_counter() - rung_start
        rungs.append({
            "concurrency": concurrency,
            "submitted": len(records),
            "accepted": sum(row.get("result") == "PASS" for row in records),
            "terminal_completed": sum(row.get("terminal_status") in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING"} for row in records),
            "terminal_failed_or_unknown": sum(row.get("terminal_status") not in {None, "COMPLETED", "COMPLETED_WITH_REVIEW_PENDING"} for row in records),
            "unresolved_active_job_ids": active_unknown,
            "request_latency_ms": {f"p{p}": percentile(response_ms, p / 100) for p in (50, 90, 95, 99)},
            "end_to_end_latency_ms": {f"p{p}": percentile(duration_ms, p / 100) for p in (50, 90, 95, 99)},
            "elapsed_seconds": round(elapsed, 2),
            "completed_jobs_per_second": round(sum(row.get("terminal_status") in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING"} for row in records) / elapsed, 3) if elapsed else None,
            "http_status_counts": {str(code): sum(row.get("http_status") == code for row in records) for code in sorted({row.get("http_status") for row in records if row.get("http_status") is not None})},
        })
        (evidence.output_dir / f"rung-{concurrency:02}-summary.json").write_text(json.dumps(rungs[-1], indent=2) + "\n", encoding="utf-8")
        print(json.dumps(rungs[-1], indent=2), flush=True)
        if active_unknown:
            # Do not multiply a stuck queue by advancing to the next load rung.
            # The rung remains recorded as a failure and can be investigated.
            print(f"Stopping higher ingestion rungs because {len(active_unknown)} job(s) did not reach terminal state.", flush=True)
            break
    return rungs


def run_hitl(args: argparse.Namespace, evidence: Evidence) -> list[dict[str, Any]]:
    review_file = Path(args.review_items)
    payload = json.loads(review_file.read_text(encoding="utf-8"))
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("Review item input must be a JSON list or an object containing items")
    all_review_ids = [str(item.get("review_id") or "") for item in items]
    if any(not review_id for review_id in all_review_ids) or len(set(all_review_ids)) != len(all_review_ids):
        raise SystemExit("Review item file must contain unique, non-empty review_id values")
    rungs: list[dict[str, Any]] = []
    offset = 0
    for concurrency in args.levels:
        if concurrency not in RAMP_LEVELS:
            raise SystemExit(f"Unsupported ramp rung {concurrency}; allowed levels: {RAMP_LEVELS}")
        batch = items[offset:offset + concurrency]
        if len(batch) != concurrency:
            raise SystemExit(f"Rung {concurrency} requires {concurrency} unique pending review items; only {len(batch)} remain")
        if len({str(item.get("review_id")) for item in batch}) != len(batch):
            raise SystemExit("Review item file contains duplicate review IDs within a rung")
        offset += concurrency
        barrier = threading.Barrier(concurrency)
        rung_start = time.perf_counter()

        def act(index: int, item: dict[str, Any]) -> dict[str, Any]:
            actor = f"test-hitl-{args.run_id}-{concurrency}-{index}"
            decision = item.get("decision_request") or {"action": "LEAVE_UNRESOLVED", "notes": f"Load harness {args.run_id}; preserve pending state."}
            review_id = str(item["review_id"])
            batch_id = str(item["ingestion_batch_id"])
            domain = str(item["domain_id"])
            product_line = str(item["product_line_id"])
            params = {"ingestion_batch_id": batch_id, "domain_id": domain, "product_line_id": product_line}
            barrier.wait(timeout=60)
            started = time.perf_counter()
            row = {
                "case_id": f"LOAD-HITL-{concurrency:02}-{index:03}", "actor_id": actor,
                "review_id": review_id, "method": "POST", "path": f"/hitl/reviews/{review_id}/decision",
                "action": decision.get("action"), "expected": "HTTP 200 with persisted decision/write status",
                "started_at": utc_now(),
            }
            try:
                response = requests.post(
                    f"{args.base_url.rstrip('/')}/hitl/reviews/{quote(review_id, safe='')}/decision",
                    params=params, json=decision, headers={"Authorization": f"Bearer {actor}"}, timeout=args.request_timeout,
                )
                row["http_status"] = response.status_code
                row["response_ms"] = round((time.perf_counter() - started) * 1000, 2)
                body = response.json() if response.content else {}
                row["actual"] = {k: body.get(k) for k in ("status", "reviewer_action", "execution_status", "write_result", "write_error", "write_attempts") if k in body}
                action = str(decision.get("action") or "")
                needs_write = action in {"APPROVE_AS_NEW", "APPROVE_REUSE", "APPROVE_REUSE_WITH_ALIAS"}
                write_ok = not needs_write or body.get("execution_status") == "SUCCEEDED"
                row["result"] = "PASS" if response.status_code == 200 and write_ok else "FAIL"
            except Exception as exc:
                row.update(result="FAIL", actual=f"{type(exc).__name__}: {exc}", response_ms=round((time.perf_counter() - started) * 1000, 2))
            evidence.record(row)
            return row

        records: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(act, i, item) for i, item in enumerate(batch, 1)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    records.append(future.result())
                except Exception as exc:
                    row = {"case_id": f"LOAD-HITL-{concurrency:02}-WORKER", "result": "FAIL", "actual": f"{type(exc).__name__}: {exc}"}
                    evidence.record(row)
                    records.append(row)
        latencies = [float(row["response_ms"]) for row in records if row.get("response_ms") is not None]
        elapsed = time.perf_counter() - rung_start
        rung = {
            "concurrency": concurrency,
            "requests": len(records),
            "passed": sum(row.get("result") == "PASS" for row in records),
            "failed": sum(row.get("result") == "FAIL" for row in records),
            "request_latency_ms": {f"p{p}": percentile(latencies, p / 100) for p in (50, 90, 95, 99)},
            "elapsed_seconds": round(elapsed, 2),
            "decisions_per_second": round(len(records) / elapsed, 3) if elapsed else None,
            "http_status_counts": {str(code): sum(row.get("http_status") == code for row in records) for code in sorted({row.get("http_status") for row in records if row.get("http_status") is not None})},
        }
        rungs.append(rung)
        (evidence.output_dir / f"rung-{concurrency:02}-summary.json").write_text(json.dumps(rung, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(rung, indent=2), flush=True)
    return rungs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ingestion", "hitl"))
    parser.add_argument("--base-url", default=os.environ.get("LIVE_API_BASE_URL", "http://127.0.0.1:6004"))
    parser.add_argument("--run-id", default=f"load-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--levels", type=lambda value: [int(x) for x in value.split(",")], default=list(RAMP_LEVELS))
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--job-timeout", type=int, default=3600)
    parser.add_argument("--input-dir")
    parser.add_argument("--customer-id")
    parser.add_argument("--domain")
    parser.add_argument("--product-line-id")
    parser.add_argument("--document-nature", choices=("qe_assets", "technology_artifacts"), default="qe_assets")
    parser.add_argument("--hitl-mode", choices=("review_all", "review_ambiguous", "auto_write"), default="review_all")
    parser.add_argument("--review-items", help="JSON array of unique pending reviews with scope and decision_request")
    args = parser.parse_args()
    if args.mode == "ingestion" and not all((args.input_dir, args.customer_id, args.domain, args.product_line_id)):
        parser.error("ingestion mode requires --input-dir --customer-id --domain --product-line-id")
    if args.mode == "hitl" and not args.review_items:
        parser.error("hitl mode requires --review-items with unique persisted pending items")
    if args.levels != sorted(args.levels) or any(x not in RAMP_LEVELS for x in args.levels):
        parser.error(f"--levels must be an increasing subset of {RAMP_LEVELS}")
    evidence = Evidence(args.run_id, args.mode)
    limitations = [
        "Virtual users use distinct development test actor tokens. This is not equivalent to Kong JWT signature verification or user entitlement validation.",
        "Performance pass/fail thresholds must be agreed from a baseline; this runner records percentiles and status outcomes but does not invent SLO limits.",
    ]
    if args.mode == "ingestion":
        rungs = run_ingestion(args, evidence)
    else:
        rungs = run_hitl(args, evidence)
    report = evidence.summary({"rungs": rungs, "limitations": limitations})
    print(json.dumps(report, indent=2), flush=True)
    print(f"Evidence: {evidence.output_dir}", flush=True)
    return 1 if report["fail_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

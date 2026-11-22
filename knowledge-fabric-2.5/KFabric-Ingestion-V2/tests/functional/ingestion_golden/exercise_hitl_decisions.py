#!/usr/bin/env python3
"""Inspect a completed live HITL queue and exercise safe decision variants."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests


def record(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6002")
    parser.add_argument("--token", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--domain-id", required=True)
    parser.add_argument("--product-line-id", required=True)
    parser.add_argument("--approve-name", action="append", default=[])
    parser.add_argument("--reject-name")
    parser.add_argument("--leave-unresolved-name")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.token}"})
    scope = {
        "ingestion_batch_id": args.batch_id,
        "domain_id": args.domain_id,
        "product_line_id": args.product_line_id,
    }
    queue_url = f"{args.base_url.rstrip('/')}/hitl/reviews"
    items: list[dict[str, object]] = []
    offset = 0
    total = 0
    while True:
        response = session.get(queue_url, params={**scope, "status": "PENDING", "limit": 200, "offset": offset}, timeout=30)
        row_base = {"observed_at": datetime.now(timezone.utc).isoformat(), "method": "GET", "path": "/hitl/reviews", "http_status": response.status_code}
        if not response.ok:
            record(args.output_dir / "review-actions.jsonl", {**row_base, "case_id": "HITL-QUEUE", "result": "FAIL", "actual": response.text[:1000]})
            return 1
        payload = response.json()
        total = int(payload.get("total", 0) or 0)
        page = payload.get("items", []) or []
        items.extend(page)
        if offset + len(page) >= total or not page:
            break
        offset += len(page)

    evidence_path = args.output_dir / "candidate-samples.jsonl"
    for item in items:
        mapping = item.get("mapping_context_json", {}) or {}
        source = item.get("source_context_json", {}) or {}
        record(evidence_path, {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "review_id": item.get("review_id"),
            "queue_type": item.get("queue_type"),
            "reason_code": item.get("reason_code"),
            "status": item.get("status"),
            "incoming_entity": mapping.get("incoming_entity"),
            "mapping_decision": mapping.get("decision"),
            "source_evidence": {
                key: source.get(key) for key in ("source_file", "source_location", "page_number", "block_index", "masked_text")
            },
            "context_resolutions": item.get("context_resolution_json", []),
            "execution_plan": item.get("execution_plan_json"),
            "provenance": item.get("provenance_json"),
        })

    by_name: dict[str, dict[str, object]] = {}
    for item in items:
        incoming = ((item.get("mapping_context_json") or {}).get("incoming_entity") or {})
        name = str(incoming.get("name") or "").casefold()
        if name and name not in by_name:
            by_name[name] = item
    exercised: set[str] = set()

    def execute(case_id: str, item: dict[str, object], action: str, body: dict[str, object]) -> None:
        review_id = str(item.get("review_id"))
        params = {**scope}
        url = f"{args.base_url.rstrip('/')}/hitl/reviews/{quote(review_id, safe='')}/decision"
        observed = datetime.now(timezone.utc).isoformat()
        try:
            response = session.post(url, params=params, json=body, timeout=180)
            try:
                payload = response.json()
            except Exception:
                payload = {"response_excerpt": response.text[:1000]}
            passed = response.status_code == 200
            if action in {"APPROVE_AS_NEW", "APPROVE_REUSE"}:
                passed = passed and str(payload.get("execution_status", "")).upper() == "SUCCEEDED"
            elif action == "REJECT":
                passed = passed and str(payload.get("status", "")).upper() == "REJECTED"
            elif action == "LEAVE_UNRESOLVED":
                passed = passed and str(payload.get("status", "")).upper() == "PENDING"
            history_response = session.get(
                f"{args.base_url.rstrip('/')}/hitl/reviews/{quote(review_id, safe='')}/history",
                params=scope, timeout=30,
            )
            history = history_response.json() if history_response.ok else {}
            events = history.get("events", []) or []
            audit_present = any(str(event.get("action", "")).upper() == action for event in events)
            passed = passed and history_response.status_code == 200 and audit_present
            record(args.output_dir / "review-actions.jsonl", {
                "case_id": case_id,
                "observed_at": observed,
                "review_id": review_id,
                "action": action,
                "request": body,
                "http_status": response.status_code,
                "response": payload,
                "history_http_status": history_response.status_code,
                "history_event_count": len(events),
                "audit_action_present": audit_present,
                "result": "PASS" if passed else "FAIL",
            })
            print(f"{'PASS' if passed else 'FAIL'} {case_id} {action} review={review_id} response={json.dumps(payload, default=str)[:300]}", flush=True)
        except Exception as exc:
            record(args.output_dir / "review-actions.jsonl", {
                "case_id": case_id, "observed_at": observed, "review_id": review_id,
                "action": action, "request": body, "result": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"FAIL {case_id} {action} {type(exc).__name__}: {exc}", flush=True)
        exercised.add(review_id)

    # Approve only explicitly named, source-grounded records. These QA writes
    # are scoped to this newly onboarded test customer and seed the next run.
    for raw_name in args.approve_name:
        item = by_name.get(raw_name.casefold())
        if not item:
            record(args.output_dir / "review-actions.jsonl", {
                "case_id": f"HITL-APPROVE-{raw_name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "action": "APPROVE_AS_NEW", "result": "FAIL",
                "actual": "No pending review item with this exact extracted entity name.",
            })
            print(f"FAIL HITL-APPROVE-{raw_name} no exact candidate", flush=True)
            continue
        entity = (((item.get("mapping_context_json") or {}).get("incoming_entity") or {}))
        source = item.get("source_context_json", {}) or {}
        if str(entity.get("node_type")) != "System" or raw_name.casefold() not in str(source.get("masked_text", "")).casefold():
            record(args.output_dir / "review-actions.jsonl", {
                "case_id": f"HITL-APPROVE-{raw_name}",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "review_id": item.get("review_id"), "action": "APPROVE_AS_NEW",
                "result": "FAIL", "actual": "Candidate type/source-evidence guard failed.",
            })
            print(f"FAIL HITL-APPROVE-{raw_name} evidence guard", flush=True)
            continue
        execute(f"HITL-APPROVE-{raw_name}", item, "APPROVE_AS_NEW", {
            "action": "APPROVE_AS_NEW", "notes": "Golden QA: explicit System record is supported by the source inventory entry."
        })

    # Exercise one real relationship endpoint selection only when the persisted
    # review item carries two ontology-compatible ranked candidates.
    for item in items:
        review_id = str(item.get("review_id"))
        if review_id in exercised:
            continue
        resolution = next((r for r in item.get("context_resolution_json", []) or []
                           if isinstance(r, dict) and r.get("decision") == "RELATIONSHIP_REVIEW_REQUIRED"
                           and len(r.get("candidates", []) or []) >= 2), None)
        if not resolution:
            continue
        candidate = resolution["candidates"][0]
        choice = {
            "rel_type": resolution["rel_type"],
            "direction": resolution.get("direction", "outgoing"),
            "target_type": resolution["target_type"],
            "target_value": resolution["target_value"],
            "entity_id": candidate["entity_id"],
        }
        execute("HITL-RELATIONSHIP-APPROVE", item, "APPROVE_REUSE", {
            "action": "APPROVE_REUSE",
            "notes": "Golden QA: reviewer selected the first persisted ontology-valid endpoint candidate.",
            "endpoint_choices": [choice],
        })
        break

    # Reject and leave pending only when the caller names source-grounded rows.
    for raw_name, action, case_id in (
        (args.reject_name, "REJECT", "HITL-NODE-REJECT"),
        (args.leave_unresolved_name, "LEAVE_UNRESOLVED", "HITL-NODE-LEAVE-UNRESOLVED"),
    ):
        if not raw_name:
            continue
        item = by_name.get(raw_name.casefold())
        source = item.get("source_context_json", {}) if item else {}
        if (not item or item.get("queue_type") != "NODE_ADMISSION"
                or raw_name.casefold() not in str((source or {}).get("masked_text", "")).casefold()):
            record(args.output_dir / "review-actions.jsonl", {
                "case_id": case_id, "observed_at": datetime.now(timezone.utc).isoformat(),
                "action": action, "requested_name": raw_name, "result": "FAIL",
                "actual": "No pending source-grounded node item matched this exact name.",
            })
            print(f"FAIL {case_id} no source-grounded exact candidate", flush=True)
            continue
        execute(case_id, item, action, {
            "action": action,
            "notes": f"Golden QA: explicit {action.lower()} path validation for a source-grounded candidate.",
        })

    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "pending_items_before_actions": total,
        "candidate_rows": sum(1 for _ in evidence_path.open(encoding="utf-8")) if evidence_path.exists() else 0,
        "decision_rows": sum(1 for _ in (args.output_dir / "review-actions.jsonl").open(encoding="utf-8")) if (args.output_dir / "review-actions.jsonl").exists() else 0,
        "explicit_approve_names": args.approve_name,
    }
    (args.output_dir / "hitl-action-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

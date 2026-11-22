#!/usr/bin/env python3
"""Run a real API journey across selected industries, domains, and policies.

This mutates only newly created QA customer contexts and creates fresh jobs and
store writes. It never cleans or rewrites pre-existing customer data.
"""
from __future__ import annotations

import json
import argparse
import html
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from ray.job_submission import JobSubmissionClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.evaluation.live_api_validation import INDUSTRIES, LiveSuite  # noqa: E402
from kbfabric_ingest.ingest.repository.ingestion_run_repository import IngestionRunRepository  # noqa: E402


MATRIX = {
    "banking": [
        ("consumer_retail", "Retail Cards", "synthetic_banking_retail_cards_10p.docx", None),
        ("corporate_commercial", "Lending", "synthetic_banking_commercial_lending_10p.docx",
         "uploads/temp/job_ingest-corporate_commercial-20260218-105249-d534/SMB_Process_Flow.docx"),
        ("wealth_securities_management", "Wealth Management", "synthetic_banking_wealth_management_10p.docx",
         "uploads/temp/job_ingest-wealth_securities_management-20260213-061901-2fe0/Equity_Trading.docx"),
    ],
    "insurance": [
        ("health_insurance", "Individual & Family Health", "synthetic_insurance_health_10p.docx", None),
        ("property_casualty", "Personal Auto", "synthetic_insurance_personal_auto_10p.docx", None),
    ],
    "health_insurance_care_services": [
        ("care", "Care Management", "synthetic_healthcare_care_management_10p.docx", None),
        ("pharmacy", "Pharmacy & Pharmacy Benefits", "synthetic_healthcare_pharmacy_10p.docx", None),
    ],
}
POLICIES = ("review_all", "review_ambiguous", "auto_write")
TERMINAL = {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6002")
    parser.add_argument("--ray-url", default="http://127.0.0.1:8266")
    parser.add_argument("--wait-minutes", type=int, default=45)
    parser.add_argument(
        "--industries",
        nargs="+",
        choices=tuple(MATRIX),
        default=list(MATRIX),
        help="Industries to run; defaults to all configured industries.",
    )
    args = parser.parse_args()
    run_id = f"golden-matrix-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{__import__('uuid').uuid4().hex[:6]}"
    reports_dir = ROOT / "reports" / "functional" / run_id
    reports_dir.mkdir(parents=True, exist_ok=False)
    suite = LiveSuite(args.base_url, "test-golden-" + run_id, run_id, args.ray_url)
    service_checks: dict[str, dict[str, object]] = {}
    for service, url in (
        ("ingestion_api", args.base_url.rstrip("/") + "/health"),
        ("ray_jobs", args.ray_url.rstrip("/") + "/api/version"),
    ):
        try:
            response = requests.get(url, timeout=6)
            service_checks[service] = {"url": url, "http_status": response.status_code,
                                       "ready": response.ok, "response_excerpt": response.text[:200]}
        except Exception as exc:
            service_checks[service] = {"url": url, "ready": False,
                                       "error": f"{type(exc).__name__}: {exc}"}
    unavailable = [name for name, check in service_checks.items() if not check.get("ready")]
    if unavailable:
        # A failed infrastructure preflight is evidence, but it is not a
        # functional ingestion case and must not be represented as one.
        finished_at = datetime.now(timezone.utc).isoformat()
        blocker = {
            "run_id": run_id,
            "started_at": suite.started_at,
            "finished_at": finished_at,
            "status": "BLOCKED_BEFORE_INGESTION",
            "reason": "Required ingestion services unavailable: " + ", ".join(unavailable),
            "service_checks": service_checks,
            "functional_jobs_submitted": 0,
            "customers_created": 0,
            "next_step": "Restore Ray Jobs API and ingestion API, then rerun this matrix. No stores were cleared.",
        }
        (reports_dir / "preflight-blocker.json").write_text(json.dumps(blocker, indent=2) + "\n", encoding="utf-8")
        title = html.escape(f"Functional matrix preflight · {run_id}")
        reason = html.escape(blocker["reason"])
        next_step = html.escape(blocker["next_step"])
        service_rows = "".join(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(check.get('url')))}</td>"
            f"<td>{'READY' if check.get('ready') else 'UNAVAILABLE'}</td><td>{html.escape(str(check.get('error') or check.get('http_status') or ''))}</td></tr>"
            for name, check in service_checks.items()
        )
        (reports_dir / "PREVIEW.html").write_text(
            "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{title}</title><style>body{{font:16px/1.5 system-ui;max-width:900px;margin:48px auto;padding:0 24px;color:#18243a}}"
            "h1{color:#102f5f}.badge{display:inline-block;padding:6px 12px;border-radius:20px;background:#fff1d6;color:#8a4b00;font-weight:700}"
            "section{margin-top:24px;padding:20px;border:1px solid #dfe6ef;border-radius:12px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #dfe6ef}</style>"
            f"<h1>{title}</h1><p class='badge'>BLOCKED BEFORE INGESTION</p><section><h2>Observed blocker</h2><p>{reason}</p>"
            f"<h2>Service checks</h2><table><thead><tr><th>Service</th><th>Endpoint</th><th>State</th><th>Response</th></tr></thead><tbody>{service_rows}</tbody></table>"
            f"<h2>Run impact</h2><p>0 functional jobs submitted; 0 customers created. No stores were cleared.</p>"
            f"<h2>Next step</h2><p>{next_step}</p></section></html>\n",
            encoding="utf-8",
        )
        print(f"Preflight blocked; report: {reports_dir / 'PREVIEW.html'}")
        return 2
    ray_client = JobSubmissionClient(args.ray_url)
    asset_dir = ROOT / "tests/functional/ingestion_golden/assets"
    matrix_manifest: dict[str, object] = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "customers": [], "jobs": []}
    selected_industries = set(args.industries)

    for industry_id, prefix, _primary_domain, _primary_name in INDUSTRIES:
        if industry_id not in selected_industries:
            continue
        created = suite.expect_status(
            f"{industry_id}-ONBOARD", f"Onboard a new matrix customer for {industry_id}.",
            "POST", "/fabric-foundry/customers", 201,
            json_body={"customer_name": f"Golden-{industry_id}-{run_id}", "industry_id": industry_id}, timeout=60,
        )
        if not created or not created.ok:
            continue
        customer = created.json()
        customer_id = customer["customer_id"]
        customer_row = {"customer_id": customer_id, "industry_id": industry_id, "tenant_id": customer.get("tenant_id"), "domains": []}
        matrix_manifest["customers"].append(customer_row)
        selected = MATRIX[industry_id]

        for domain_id, product_line_name, asset_name, source_sample in selected:
            encoded_customer = quote(customer_id, safe="")
            domain_path = f"/fabric-foundry/customers/{encoded_customer}/ontology/contextualization/{quote(domain_id, safe='')}"
            snap = suite.expect_status(
                f"{industry_id}-{domain_id}-CONTEXT-READ", f"Read configured {domain_id} context.",
                "GET", domain_path, 200,
            )
            if not snap or not snap.ok:
                continue
            domain_data = snap.json()
            product_lines = domain_data.get("canonical_delta", {}).get("product_lines", [])
            chosen = next((pl for pl in product_lines if pl.get("name") == product_line_name), None)
            suite.check(
                f"{industry_id}-{domain_id}-PL-CONFIGURED", f"{product_line_name} exists in {domain_id}.",
                lambda value=chosen: value is not None, bool, "configured product line", str,
                {"method": "GET", "path": domain_path, "touchpoints": ["fabric_foundry/knowledge_contextualization/api.py", "fabric_foundry/knowledge_contextualization/service.py", "config/industries"]},
            )
            if chosen is None:
                continue

            ontology_response = suite.expect_status(
                f"{industry_id}-{domain_id}-ONTOLOGY", "Read active customer ontology version before contextualization.",
                "GET", f"/fabric-foundry/customers/{encoded_customer}/ontology", 200,
            )
            if not ontology_response or not ontology_response.ok:
                continue
            base_version = ontology_response.json().get("customer_ontology_version", 1)
            changes = []
            for pl in product_lines:
                applicable = pl.get("id") == chosen.get("id")
                changes.append({"operation": "SET_APPLICABILITY", "target": {"type": "PRODUCT_LINE", "canonical_id": pl["id"]}, "applicable": applicable})
                for function in pl.get("functions", []):
                    changes.append({"operation": "SET_APPLICABILITY", "target": {"type": "FUNCTION", "canonical_id": function["id"]}, "applicable": applicable})
                    for sub in function.get("sub_functions", []):
                        changes.append({"operation": "SET_APPLICABILITY", "target": {"type": "SUB_FUNCTION", "canonical_id": sub["id"]}, "applicable": applicable})
            save = suite.expect_status(
                f"{industry_id}-{domain_id}-CONTEXT-SAVE", f"Select {product_line_name} for this customer/domain.",
                "POST", f"/fabric-foundry/customers/{encoded_customer}/ontology/{quote(domain_id, safe='')}/contextualize", 200,
                json_body={"customer_id": customer_id, "domain_id": domain_id,
                           "base_customer_ontology_version": base_version,
                           "request_id": f"{run_id}-{industry_id}-{domain_id}",
                           "changes": changes, "change_reason": "Golden three-industry multi-domain matrix"}, timeout=120,
            )
            if not save or not save.ok:
                continue
            product_line_id = chosen["id"]
            customer_row["domains"].append({"domain_id": domain_id, "product_line_id": product_line_id, "product_line_name": product_line_name})
            source_asset = asset_dir / asset_name
            asset_manifest = json.loads(
                (asset_dir / "synthetic_asset_manifest.json").read_text(encoding="utf-8")
            )
            asset_spec = next(
                item for item in asset_manifest["assets"]
                if item["filename"] == asset_name
            )

            for policy in POLICIES:
                user_dir = reports_dir / "inputs" / industry_id / domain_id / policy
                user_dir.mkdir(parents=True, exist_ok=False)
                shutil.copy2(source_asset, user_dir / source_asset.name)
                if source_sample:
                    sample_path = ROOT / source_sample
                    shutil.copy2(sample_path, user_dir / sample_path.name)
                    job_record_source_sample = source_sample
                else:
                    job_record_source_sample = None
                actor_token = f"test-golden-{run_id}-{industry_id}-{domain_id}-{policy}"
                submit = suite.expect_status(
                    f"{industry_id}-{domain_id}-{policy}-SUBMIT",
                    f"Submit the ten-page {product_line_name} asset with policy {policy}.",
                    "POST", "/jobs", (200, 202),
                    json_body={"customer_id": customer_id, "input_dir": str(user_dir), "domain": domain_id,
                               "product_line_id": product_line_id, "document_nature": "qe_assets",
                               "hitl_mode": policy, "force_rewrite": False,
                               "context_tags": [f"golden_run={run_id}", f"industry={industry_id}"]},
                    headers={"Authorization": f"Bearer {actor_token}"}, timeout=120,
                )
                if not submit or not submit.ok:
                    continue
                job = submit.json()
                job_id = job.get("job_id")
                job_record = {"industry_id": industry_id, "customer_id": customer_id, "tenant_id": customer.get("tenant_id"),
                              "domain_id": domain_id, "product_line_id": product_line_id, "policy": policy,
                              "job_id": job_id, "source_sample": job_record_source_sample,
                              "submitted_at": datetime.now(timezone.utc).isoformat()}
                matrix_manifest["jobs"].append(job_record)
                if not job_id:
                    continue
                deadline = time.monotonic() + max(1, args.wait_minutes) * 60
                last: dict[str, object] = {}
                terminal_http_status: int | None = None
                while time.monotonic() < deadline:
                    try:
                        detail = suite.request("GET", f"/jobs/{quote(job_id, safe='')}", headers={"Authorization": f"Bearer {actor_token}"})
                    except Exception as exc:
                        suite.check(
                            f"{industry_id}-{domain_id}-{policy}-DETAIL-CONNECTION",
                            "The job detail endpoint remains reachable while polling.", lambda: False, bool,
                            "HTTP 200 job detail", lambda _value: f"{type(exc).__name__}: {exc}",
                            {"method": "GET", "path": f"/jobs/{job_id}", "http_status": None,
                             "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py"]},
                        )
                        break
                    terminal_http_status = detail.status_code
                    if detail.ok:
                        last = detail.json()
                        if str(last.get("status", "")).upper() in TERMINAL:
                            break
                        # Worker bootstrap can fail before it claims the durable
                        # run, leaving PostgreSQL at SUBMITTED indefinitely.
                        # Observe Ray's authoritative submission state too, so
                        # the matrix records the mismatch and continues.
                        try:
                            ray_info = ray_client.get_job_info(job_id)
                            ray_status = str(getattr(ray_info.status, "value", ray_info.status)).upper() if ray_info else None
                        except Exception as exc:
                            ray_status = None
                            job_record["ray_status_read_error"] = f"{type(exc).__name__}: {exc}"
                        if ray_status:
                            job_record["ray_job_status"] = ray_status
                        if ray_status in {"SUCCEEDED", "FAILED", "STOPPED"}:
                            job_record["ray_terminal_without_durable_terminal"] = str(last.get("status", "")).upper() not in TERMINAL
                            if job_record["ray_terminal_without_durable_terminal"]:
                                job_record["ray_job_error"] = getattr(ray_info, "message", None)
                                job_record["poll_outcome"] = "RAY_TERMINAL_DURABLE_RUN_NOT_TERMINAL"
                                break
                    time.sleep(4)
                job_record["terminal_status"] = last.get("status", "UNKNOWN")
                job_record["run_id"] = last.get("run_id")
                job_record["ingestion_batch_id"] = last.get("ingestion_batch_id")
                job_record["customer_ontology_version"] = last.get("customer_ontology_version")
                job_record["stages"] = last.get("stages", [])
                suite.check(
                    f"{industry_id}-{domain_id}-{policy}-TERMINAL", f"{industry_id}/{domain_id} {policy} run reaches a terminal state.",
                    lambda: last,
                    lambda value: str(value.get("status", "")).upper() in TERMINAL,
                    "COMPLETED, COMPLETED_WITH_REVIEW_PENDING, FAILED, or CANCELLED",
                    lambda value: str(value.get("status", "UNKNOWN")),
                    {"method": "GET", "path": f"/jobs/{job_id}", "http_status": terminal_http_status,
                     "touchpoints": ["scheduler/api.py", "scheduler/actor.py", "ingest/repository/ingestion_run_repository.py"]},
                )
                expected_stages = set(IngestionRunRepository.STAGES)
                actual_stages = {stage.get("stage_id") for stage in last.get("stages", []) if isinstance(stage, dict)}
                suite.check(
                    f"{industry_id}-{domain_id}-{policy}-15-STAGES", "The returned job details cover every persisted business stage.",
                    lambda actual=actual_stages, detail=last: (
                        actual == expected_stages
                        and all(stage.get("status") in {"SUCCEEDED", "SKIPPED"} for stage in detail.get("stages", []) if isinstance(stage, dict))
                    ), bool, f"all {len(expected_stages)} registered terminal stages", lambda value: json.dumps([
                        {"stage_id": stage.get("stage_id"), "status": stage.get("status"), "skip_reason": stage.get("skip_reason")}
                        for stage in last.get("stages", []) if isinstance(stage, dict)
                    ]),
                    {"method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py#STAGES"]},
                )
                extracted_entities = int(last.get("entities_found") or 0)
                extracted_relationships = int(last.get("relationships_found") or 0)
                suite.check(
                    f"{industry_id}-{domain_id}-{policy}-EXTRACTION-YIELD",
                    "The ten-page product-line asset yields business entities and relationships for graph admission.",
                    lambda entities=extracted_entities, relationships=extracted_relationships: entities > 0 and relationships > 0,
                    bool, "entities_found > 0 and relationships_found > 0",
                    lambda value: f"entities_found={extracted_entities}, relationships_found={extracted_relationships}",
                    {"method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["ingest/lane2_two_pass.py", "ingest/ontology_admission.py", "ingest/ingestion_service.py"]},
                )
                suite.expect_status(f"{industry_id}-{domain_id}-{policy}-REPORT", "A report is available for this terminal journey.",
                                    "GET", f"/reports/get-report/{quote(job_id, safe='')}", 200)
                if policy in {"review_all", "review_ambiguous"} and last.get("ingestion_batch_id"):
                    review_response = suite.check(
                        f"{industry_id}-{domain_id}-{policy}-HITL-QUEUE",
                        "Read the persisted pending review queue for this exact batch/domain/product line.",
                        lambda job=job_record, token=actor_token: suite.request(
                            "GET", "/hitl/reviews", headers={"Authorization": f"Bearer {token}"},
                            params={"ingestion_batch_id": job["ingestion_batch_id"],
                                    "domain_id": job["domain_id"],
                                    "product_line_id": job["product_line_id"],
                                    "status": "PENDING", "limit": 200},
                        ),
                        lambda response: response is not None and response.status_code == 200,
                        "HTTP 200 scoped HITL review list",
                        lambda response: f"HTTP {response.status_code}: {response.text[:300]}" if response is not None else "no response",
                        {"method": "GET", "path": "/hitl/reviews", "touchpoints": [
                            "ingest/hitl_api.py#list_hitl_reviews", "ingest/repository/hitl_review_repository.py"
                        ]},
                    )
                    review_payload = review_response.json() if review_response is not None and review_response.ok else {}
                    pending_items = list(review_payload.get("items", []))
                    expected_total = int(review_payload.get("total", 0) or 0)
                    for offset in range(200, expected_total, 200):
                        page_response = suite.check(
                            f"{industry_id}-{domain_id}-{policy}-HITL-QUEUE-PAGE-{offset}",
                            "Read the next persisted HITL queue page for the same frozen scope.",
                            lambda job=job_record, token=actor_token, page_offset=offset: suite.request(
                                "GET", "/hitl/reviews", headers={"Authorization": f"Bearer {token}"},
                                params={"ingestion_batch_id": job["ingestion_batch_id"],
                                        "domain_id": job["domain_id"],
                                        "product_line_id": job["product_line_id"],
                                        "status": "PENDING", "limit": 200, "offset": page_offset},
                            ),
                            lambda response: response is not None and response.status_code == 200,
                            "HTTP 200 scoped HITL review page",
                            lambda response: f"HTTP {response.status_code}: {response.text[:300]}" if response is not None else "no response",
                            {"method": "GET", "path": "/hitl/reviews", "query_offset": offset, "touchpoints": [
                                "ingest/hitl_api.py#list_hitl_reviews", "ingest/repository/hitl_review_repository.py"
                            ]},
                        )
                        if page_response is not None and page_response.ok:
                            pending_items.extend(page_response.json().get("items", []))
                    suite.check(
                        f"{industry_id}-{domain_id}-{policy}-HITL-POLICY-RESULT",
                        f"Policy {policy} produces persisted review candidates, when review is required by policy/evidence.",
                        lambda count=expected_total: count > 0,
                        bool, "at least one pending review item", lambda count: f"pending={count}",
                        {"method": "GET", "path": "/hitl/reviews", "touchpoints": [
                            "ingest/execution_gate.py#evaluate_execution_gate", "ingest/ingestion_service.py",
                            "ingest/hitl_api.py#list_hitl_reviews", "ingest/repository/hitl_review_repository.py"
                        ]},
                    )
                    if policy == "review_all":
                        evidenced_records = []
                        for item in pending_items:
                            mapping = item.get("mapping_context_json", {}) or {}
                            incoming = mapping.get("incoming_entity", {}) or {}
                            name = str(incoming.get("name") or "").strip()
                            source = str((item.get("source_context_json", {}) or {}).get("masked_text") or "")
                            if name and name.casefold() in source.casefold() and source:
                                evidenced_records.append({
                                    "review_id": item.get("review_id"),
                                    "node_type": incoming.get("node_type"),
                                    "entity_name": name[:180],
                                    "source_excerpt": source[:500],
                                    "decision": mapping.get("decision"),
                                    "execution_plan_action": (item.get("execution_plan_json", {}) or {}).get("action"),
                                })
                        suite.check(
                            f"{industry_id}-{domain_id}-review_all-EVIDENCED-CANDIDATE",
                            "A review_all item contains a meaningful extracted record grounded in its persisted source evidence.",
                            lambda evidence=evidenced_records: evidence,
                            lambda evidence: bool(evidence),
                            "at least one nonempty extracted entity name occurs in its masked source evidence",
                            lambda evidence: json.dumps(evidence[:3], ensure_ascii=False),
                            {"method": "GET", "path": "/hitl/reviews", "touchpoints": [
                                "ingest/ingestion_service.py", "ingest/repository/hitl_review_repository.py",
                                "ingest/hitl_api.py#list_hitl_reviews"
                            ]},
                        )
                        job_record["review_all_evidence_samples"] = evidenced_records[:3]
                        # Seed the exact, source-grounded competing endpoints
                        # before the subsequent review_ambiguous journey. Only
                        # standalone System nodes are approved here, so this
                        # setup cannot accidentally exercise an unrelated
                        # relationship write defect.
                        challenge = asset_spec.get("review_challenge", {})
                        seed_results = []
                        for candidate_name in challenge.get("candidates", []):
                            seed_item = next(
                                (
                                    item for item in pending_items
                                    if str(((item.get("mapping_context_json") or {}).get("incoming_entity") or {}).get("name") or "").casefold()
                                    == str(candidate_name).casefold()
                                    and str(((item.get("mapping_context_json") or {}).get("incoming_entity") or {}).get("node_type") or "") == "System"
                                    and candidate_name.casefold() in str((item.get("source_context_json") or {}).get("masked_text") or "").casefold()
                                    and not (item.get("execution_plan_json") or {}).get("relationship_operations")
                                    and not (item.get("execution_plan_json") or {}).get("unresolved_reference_operations")
                                ),
                                None,
                            )
                            if seed_item is None:
                                seed_results.append({"name": candidate_name, "result": "FAIL", "reason": "No exact source-grounded standalone System review item."})
                                suite.check(
                                    f"{industry_id}-{domain_id}-review_all-SEED-{len(seed_results)}",
                                    f"Find an exact source-grounded standalone System candidate to seed {policy} ambiguity coverage.",
                                    lambda: None, lambda value: value is not None,
                                    "one exact standalone System review item", lambda _value, name=candidate_name: name,
                                    {"method": "GET", "path": "/hitl/reviews", "touchpoints": ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py"]},
                                )
                                continue
                            review_id = str(seed_item.get("review_id"))
                            decision = suite.request(
                                "POST", f"/hitl/reviews/{quote(review_id, safe='')}/decision",
                                headers={"Authorization": f"Bearer {actor_token}"},
                                params={"ingestion_batch_id": job_record["ingestion_batch_id"],
                                        "domain_id": domain_id, "product_line_id": product_line_id},
                                json={"action": "APPROVE_AS_NEW", "notes": "Golden fixture setup: approve this exact source-grounded System so the next policy run has persisted competing endpoints."},
                                timeout=180,
                            )
                            try:
                                decision_payload = decision.json()
                            except Exception:
                                decision_payload = {"response_excerpt": decision.text[:500]}
                            seed_ok = (
                                decision.status_code == 200
                                and str(decision_payload.get("execution_status", "")).upper() == "SUCCEEDED"
                                and int((decision_payload.get("write_result") or {}).get("nodes", 0)) >= 1
                            )
                            suite.check(
                                f"{industry_id}-{domain_id}-review_all-SEED-{len(seed_results) + 1}",
                                f"Approve and persist the source-grounded System candidate {candidate_name} for the next ambiguity run.",
                                lambda ok=seed_ok: ok, bool,
                                "HTTP 200, APPROVED action, successful graph write with at least one node",
                                lambda _value, payload=decision_payload: json.dumps(payload, ensure_ascii=False),
                                {"method": "POST", "path": f"/hitl/reviews/{review_id}/decision", "http_status": decision.status_code,
                                 "touchpoints": ["ingest/hitl_api.py", "ingest/hitl_write_executor.py", "ingest/repository/hitl_review_repository.py"]},
                            )
                            seed_results.append({"name": candidate_name, "review_id": review_id,
                                                 "http_status": decision.status_code,
                                                 "response": decision_payload,
                                                 "result": "PASS" if seed_ok else "FAIL"})
                        job_record["ambiguous_fixture_seed_nodes"] = seed_results
                    ambiguity_evidence = []
                    for item in pending_items:
                        for resolution in item.get("context_resolution_json", []) or []:
                            candidates = resolution.get("candidates", []) if isinstance(resolution, dict) else []
                            if isinstance(resolution, dict) and resolution.get("decision") == "RELATIONSHIP_REVIEW_REQUIRED" and len(candidates) >= 2:
                                ambiguity_evidence.append({
                                    "review_id": item.get("review_id"),
                                    "relationship": {key: resolution.get(key) for key in ("rel_type", "target_type", "target_value", "decision", "reason_code")},
                                    "candidates": [{key: candidate.get(key) for key in ("name", "entity_id", "node_type", "fusion_score")} for candidate in candidates[:3]],
                                })
                    if policy == "review_ambiguous":
                        suite.check(
                            f"{industry_id}-{domain_id}-review_ambiguous-TWO-CANDIDATES",
                            "The ambiguous-review journey contains a persisted relationship decision with at least two ontology-valid target candidates.",
                            lambda evidence=ambiguity_evidence: evidence,
                            lambda evidence: len(evidence) > 0,
                            "one or more RELATIONSHIP_REVIEW_REQUIRED items with >=2 candidates",
                            lambda evidence: json.dumps(evidence[:3], ensure_ascii=False),
                            {"method": "GET", "path": "/hitl/reviews", "touchpoints": [
                                "ingest/relationship_endpoint_resolution.py#decide_context_resolution",
                                "ingest/execution_gate.py#evaluate_execution_gate", "ingest/hitl_api.py#list_hitl_reviews"
                            ]},
                        )
                        job_record["ambiguous_relationship_review_evidence"] = ambiguity_evidence[:3]
                    job_record["pending_review_count"] = expected_total
                job_record["end_to_end_result"] = "PASS" if (
                    str(last.get("status", "")).upper() in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING"}
                    and actual_stages == expected_stages
                    and all(stage.get("status") in {"SUCCEEDED", "SKIPPED"} for stage in last.get("stages", []) if isinstance(stage, dict))
                    and extracted_entities > 0 and extracted_relationships > 0
                ) else "FAIL"

    matrix_manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    (reports_dir / "journey-manifest.json").write_text(json.dumps(matrix_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = {
        "run_id": run_id,
        "started_at": suite.started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(suite.results),
        "passed": sum(row["result"] == "PASS" for row in suite.results),
        "failed": sum(row["result"] == "FAIL" for row in suite.results),
        "first_pass_rate_percent": round(sum(row["result"] == "PASS" for row in suite.results) * 100 / len(suite.results), 2) if suite.results else 0,
        "matrix_job_count": len(matrix_manifest["jobs"]),
        "results": suite.results,
    }
    (reports_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    cards = "".join(
        f"<tr><td>{html.escape(str(row.get('industry_id', '')))}</td><td>{html.escape(str(row.get('domain_id', '')))}</td>"
        f"<td>{html.escape(str(row.get('policy', '')))}</td><td>{html.escape(str(row.get('job_id', '')))}</td>"
        f"<td>{html.escape(str(row.get('terminal_status', 'UNKNOWN')))}</td><td>{int(row.get('pending_review_count', 0) or 0)}</td>"
        f"<td>{html.escape(str(row.get('end_to_end_result', 'N/A')))}</td><td>{html.escape(str(row.get('source_sample') or 'synthetic fixture'))}</td></tr>"
        for row in matrix_manifest["jobs"]
    )
    report_title = html.escape(f"Functional ingestion matrix · {run_id}")
    (reports_dir / "PREVIEW.html").write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{report_title}</title><style>body{{font:14px/1.5 system-ui;max-width:1400px;margin:36px auto;padding:0 24px;color:#18243a}}"
        "h1{color:#102f5f}.metrics{display:flex;gap:12px;flex-wrap:wrap}.metric,section{padding:16px;border:1px solid #dfe6ef;border-radius:10px}"
        "table{width:100%;border-collapse:collapse;margin-top:16px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;vertical-align:top}th{background:#f5f8fc}"
        ".pass{color:#16834a}.fail{color:#b42318}</style>"
        f"<h1>{report_title}</h1><div class='metrics'><div class='metric'>Cases: {report['case_count']}</div>"
        f"<div class='metric pass'>Passed: {report['passed']}</div><div class='metric fail'>Failed: {report['failed']}</div>"
        f"<div class='metric'>Jobs submitted: {report['matrix_job_count']}</div></div><section><h2>Journey results</h2>"
        "<div style='overflow:auto'><table><thead><tr><th>Industry</th><th>Domain</th><th>Policy</th><th>Job</th><th>Status</th>"
        "<th>Pending reviews</th><th>Journey</th><th>Source companion</th></tr></thead><tbody>"
        f"{cards}</tbody></table></div></section><section><h2>Evidence</h2><p>Case-level evidence: <code>"
        f"{html.escape(suite.rows_path.name)}</code>. Exact inputs and customer scopes are in <code>journey-manifest.json</code>.</p></section></html>\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    print(f"Per-case evidence: {suite.rows_path}")
    print(f"Journey manifest: {reports_dir / 'journey-manifest.json'}")
    print(f"HTML report: {reports_dir / 'PREVIEW.html'}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

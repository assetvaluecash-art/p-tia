#!/usr/bin/env python3
"""
API-only E2E test for ingestion + retrieval.

Sequence:
1) Upload 8 docs via ingestion API (/jobs/upload)
2) Q1/Q2/Q3 checks (API only)
3) QE-agent style retrieval queries (API only)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


INGEST_BASE = os.environ.get("KBF_INGEST_API", "http://localhost:6102").rstrip("/")
RETRIEVE_BASE = os.environ.get("KBF_RETRIEVE_API", "http://localhost:6104").rstrip("/")
WEAVIATE_URL = os.environ.get("KBF_WEAVIATE_URL", "http://localhost:8080").rstrip("/")
NEO4J_HTTP = os.environ.get("KBF_NEO4J_HTTP", "http://localhost:7474").rstrip("/")
NEO4J_USER = os.environ.get("KBF_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("KBF_NEO4J_PASSWORD", "test123$")
DOC_DIR = os.environ.get(
    "KBF_DOC_DIR",
    "/mnt/c/data/qiba_pulse_knowledge_fabric/kbfabric/kbfabric-ingest/drop_zone/full_wealth_portfolio",
)

POLL_INTERVAL_SEC = int(os.environ.get("KBF_POLL_INTERVAL_SEC", "10"))
JOB_TIMEOUT_SEC = int(os.environ.get("KBF_JOB_TIMEOUT_SEC", str(2 * 60 * 60)))


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: dict[str, Any]


def _get_json(url: str) -> dict[str, Any]:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _weaviate_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(f"{WEAVIATE_URL}/v1/graphql", json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _neo4j_query(statements: list[dict[str, Any]]) -> dict[str, Any]:
    resp = requests.post(
        f"{NEO4J_HTTP}/db/neo4j/tx/commit",
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        json={"statements": statements},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _domain_to_class(domain: str) -> str:
    parts = [p for p in domain.split("_") if p]
    class_suffix = "".join(p[:1].upper() + p[1:] for p in parts)
    return f"KBChunk_{class_suffix}"


def wait_for_ready(base_url: str, ready_path: str = "/health/ready", timeout_sec: int = 600) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}{ready_path}", timeout=15)
            if resp.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise TimeoutError(f"Service not ready: {base_url}{ready_path}")


def assert_no_running_jobs() -> None:
    jobs_resp = _get_json(f"{INGEST_BASE}/jobs?limit=50")
    active = []
    for job in jobs_resp.get("jobs", []) or []:
        status = (job.get("status") or "").upper()
        if status and status not in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED", "SUCCEEDED"}:
            active.append({"job_id": job.get("job_id"), "status": status})
    if active:
        raise RuntimeError(f"Active ingestion jobs detected: {active}")


def pick_domain() -> str:
    domains = _get_json(f"{INGEST_BASE}/domains").get("domains", [])
    names = [d.get("name") if isinstance(d, dict) else d for d in domains]
    if "wealth_management" in names:
        return "wealth_management"
    if "wealth_portfolio" in names:
        return "wealth_portfolio"
    if names:
        return names[0]
    raise RuntimeError("No domains available from ingestion API")


def upload_job(domain: str, doc_dir: str, force_rewrite: bool = True) -> dict[str, Any]:
    doc_path = Path(doc_dir)
    if not doc_path.exists():
        raise FileNotFoundError(f"Doc dir not found: {doc_dir}")

    files = []
    for item in sorted(doc_path.iterdir()):
        if not item.is_file():
            continue
        files.append(("files", (item.name, item.open("rb"), "application/octet-stream")))

    if len(files) < 8:
        raise RuntimeError(f"Expected 8 docs, found {len(files)} in {doc_dir}")

    data = {"domain": domain, "force_rewrite": str(force_rewrite).lower()}
    resp = requests.post(f"{INGEST_BASE}/jobs/upload", data=data, files=files, timeout=300)
    resp.raise_for_status()
    return resp.json()


def poll_job(job_id: str) -> dict[str, Any]:
    deadline = time.time() + JOB_TIMEOUT_SEC
    last_status = None
    last_phase = None
    last_progress = None
    while time.time() < deadline:
        status = _get_json(f"{INGEST_BASE}/jobs/{job_id}")
        last_status = status
        phase = status.get("phase")
        progress = status.get("progress_pct")
        state = status.get("status")
        if phase != last_phase or progress != last_progress:
            print(f"[E2E] job={job_id} status={state} phase={phase} progress={progress}%", flush=True)
            last_phase = phase
            last_progress = progress
        if status.get("status") in {"COMPLETED", "FAILED", "CANCELLED", "SUCCEEDED"}:
            return status
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"Job {job_id} timed out. Last status: {last_status}")


def fetch_job_logs(job_id: str) -> str:
    resp = requests.get(f"{INGEST_BASE}/jobs/{job_id}/logs", timeout=60)
    if resp.status_code != 200:
        return f"[logs unavailable] status={resp.status_code}"
    return resp.text


def q1_reports_check(expected_files: list[str]) -> CheckResult:
    reports = _get_json(f"{INGEST_BASE}/reports").get("reports", [])
    report_names = [r.get("name") for r in reports]
    latest_ingestion = next((r for r in report_names if r and "semantic_ingestion/ingestion_" in r), None)
    latest_ingestion_json = next((r for r in report_names if r and "semantic_ingestion/ingestion_" in r and r.endswith(".json")), None)
    latest_kg = next((r for r in report_names if r and "kg_ingestion/kg_report_" in r), None)

    details: dict[str, Any] = {
        "latest_ingestion_report": latest_ingestion,
        "latest_ingestion_json": latest_ingestion_json,
        "latest_kg_report": latest_kg,
    }

    if not latest_ingestion:
        return CheckResult("Q1_reports", False, details)

    html = requests.get(f"{INGEST_BASE}/reports/{latest_ingestion}", timeout=60).text
    missing = [name for name in expected_files if name not in html]
    details["missing_files_in_report"] = missing
    details["report_contains_total_files"] = "Total Files" in html
    details["report_contains_total_blocks"] = "Total Blocks" in html

    # Full-text check for ingestion JSON report
    full_text_ok = False
    missing_full_text_keys = []
    if latest_ingestion_json:
        json_payload = _get_json(f"{INGEST_BASE}/reports/{latest_ingestion_json}")
        files_summary = json_payload.get("files_summary", []) or []
        for file_entry in files_summary:
            for blk in file_entry.get("blocks", []) or []:
                if "text_full" not in blk or "original_text_full" not in blk:
                    missing_full_text_keys.append(file_entry.get("file", "unknown"))
                if "text_preview" in blk:
                    missing_full_text_keys.append(file_entry.get("file", "unknown"))
            if file_entry.get("blocks"):
                full_text_ok = True
    details["full_text_in_json_report"] = full_text_ok and not missing_full_text_keys
    details["full_text_missing_files"] = sorted(set(missing_full_text_keys))

    ok = (
        not missing
        and details["report_contains_total_files"]
        and details["report_contains_total_blocks"]
        and details["full_text_in_json_report"]
    )
    return CheckResult("Q1_reports", ok, details)


def query_retrieval(domain: str, query: str, top_k_semantic: int = 12, top_k_graph: int = 12) -> dict[str, Any]:
    return _post_json(
        f"{RETRIEVE_BASE}/query",
        {
            "query": query,
            "domain": domain,
            "top_k_semantic": top_k_semantic,
            "top_k_graph": top_k_graph,
        },
    )


def _collect_texts(response: dict[str, Any]) -> list[str]:
    texts = []
    for section in ("chunks", "paths", "reranked_chunks", "reranked_paths"):
        for item in response.get(section, []) or []:
            text = item.get("text") or item.get("path_text") or ""
            if text:
                texts.append(text)
    return texts


def _has_any_token(texts: list[str], tokens: list[str]) -> bool:
    joined = "\n".join(texts).lower()
    return any(token.lower() in joined for token in tokens)


def q2_live_queries(domain: str) -> CheckResult:
    checks = []
    queries = [
        {"query": "TC-WP-001", "must_include": ["TC-WP-001"], "must_category": "TestCase"},
        {"query": "INC-2024-001", "must_include": ["INC-2024-001"]},
        {"query": "DEF-2024-101", "must_include": ["DEF-2024-101"]},
        {"query": "Acceptance criteria for wealth portfolio", "must_include": ["AC-"]},
    ]

    for q in queries:
        resp = query_retrieval(domain, q["query"])
        texts = _collect_texts(resp)
        ok = _has_any_token(texts, q.get("must_include", []))

        if ok and q.get("must_category"):
            ok = any(
                (c.get("category") == q["must_category"] or c.get("kg_node_type") == q["must_category"])
                for c in resp.get("reranked_chunks", [])
            )

        checks.append(
            {
                "query": q["query"],
                "ok": ok,
                "matches": q.get("must_include", []),
                "top_chunks": resp.get("reranked_chunks", [])[:3],
                "top_paths": resp.get("reranked_paths", [])[:3],
            }
        )

    ok_all = all(c["ok"] for c in checks)
    return CheckResult("Q2_live_queries", ok_all, {"checks": checks})


def q3_change_based(domain: str) -> CheckResult:
    queries = [
        {
            "query": "TC-WP-002",
            "must_include": ["TC-WP-002"],
            "must_category": "TestCase",
        },
        {
            "query": "wealth portfolio incidents and defects",
            "must_include": ["INC-", "DEF-"],
        },
    ]

    results = []
    for q in queries:
        resp = query_retrieval(domain, q["query"])
        texts = _collect_texts(resp)
        ok = _has_any_token(texts, q.get("must_include", []))
        if ok and q.get("must_category"):
            ok = any(
                (c.get("category") == q["must_category"] or c.get("kg_node_type") == q["must_category"])
                for c in resp.get("reranked_chunks", [])
            )
        results.append(
            {
                "query": q["query"],
                "ok": ok,
                "matches": q.get("must_include", []),
                "top_chunks": resp.get("reranked_chunks", [])[:3],
                "top_paths": resp.get("reranked_paths", [])[:3],
            }
        )

    ok_all = all(r["ok"] for r in results)
    return CheckResult("Q3_change_based", ok_all, {"checks": results})


def store_checks(domain: str) -> CheckResult:
    class_name = _domain_to_class(domain)
    details: dict[str, Any] = {"weaviate": {}, "neo4j": {}, "retriever": {}}

    # Weaviate: table rows present for DOCX test cases
    table_query = f"""
    {{
      Get {{
        {class_name}(
          where: {{path: ["source_file"], operator: Equal, valueString: "wealth_portfolio_test_cases.docx"}}
        ) {{
          block_type
          aliases
          metadata {{ table_row_index }}
        }}
      }}
    }}
    """.strip()
    table_resp = _weaviate_query(table_query)
    rows = (((table_resp or {}).get("data") or {}).get("Get") or {}).get(class_name, []) or []
    row_hits = [
        r for r in rows
        if (r.get("metadata") or {}).get("table_row_index") is not None or r.get("block_type") == "table_row"
    ]
    details["weaviate"]["table_rows_found"] = len(row_hits)
    details["weaviate"]["table_rows_total"] = len(rows)

    # Weaviate: aliases populated (sample)
    alias_query = f"""
    {{
      Get {{
        {class_name}(limit: 200) {{
          aliases
        }}
      }}
    }}
    """.strip()
    alias_resp = _weaviate_query(alias_query)
    alias_rows = (((alias_resp or {}).get("data") or {}).get("Get") or {}).get(class_name, []) or []
    alias_count = sum(1 for r in alias_rows if r.get("aliases"))
    details["weaviate"]["alias_chunks_with_values"] = alias_count

    # Neo4j: aliases and orphans + provenance normalization
    stmt_alias = {
        "statement": "MATCH (n) WHERE n.domain = $domain AND size(coalesce(n.aliases, [])) > 0 RETURN labels(n)[0] as label, count(*) as count",
        "parameters": {"domain": domain},
    }
    stmt_orphans = {
        "statement": "MATCH (n) WHERE n.domain = $domain AND NOT n:Block AND NOT n:Document AND NOT n:DomainRoot AND size((n)--()) = 0 RETURN labels(n)[0] as label, count(*) as count",
        "parameters": {"domain": domain},
    }
    stmt_prov = {
        "statement": "MATCH (n) WHERE n.domain = $domain AND NOT n:Block AND NOT n:Document AND NOT n:DomainRoot RETURN count(*) as total, sum(CASE WHEN n.source_file IS NULL THEN 1 ELSE 0 END) as missing_source_file",
        "parameters": {"domain": domain},
    }
    neo_resp = _neo4j_query([stmt_alias, stmt_orphans, stmt_prov])
    neo_results = neo_resp.get("results", [])
    aliases_rows = neo_results[0].get("data", []) if len(neo_results) > 0 else []
    orphans_rows = neo_results[1].get("data", []) if len(neo_results) > 1 else []
    prov_rows = neo_results[2].get("data", []) if len(neo_results) > 2 else []
    details["neo4j"]["aliases_by_label"] = aliases_rows
    details["neo4j"]["orphans_by_label"] = orphans_rows
    details["neo4j"]["provenance"] = prov_rows
    alias_total = sum((r.get("row", [None, 0])[1] or 0) for r in aliases_rows if isinstance(r, dict))
    orphan_total = sum((r.get("row", [None, 0])[1] or 0) for r in orphans_rows if isinstance(r, dict))
    missing_source_file = 0
    if prov_rows:
        row = prov_rows[0].get("row", [0, 0])
        missing_source_file = row[1] or 0
    details["neo4j"]["alias_total"] = alias_total
    details["neo4j"]["orphan_total"] = orphan_total
    details["neo4j"]["missing_source_file"] = missing_source_file

    # Retriever: orphan path contract
    orphan_query = query_retrieval(domain, "EsgBand", top_k_semantic=6, top_k_graph=12)
    orphan_paths = orphan_query.get("reranked_paths", []) or orphan_query.get("paths", [])
    orphan_present = any(
        (p.get("relationship_type") == "NO_ESTABLISHED_RELATIONSHIPS")
        or ("NO_ESTABLISHED_RELATIONSHIPS" in (p.get("path_text") or ""))
        for p in orphan_paths
    )
    details["retriever"]["orphan_path_present"] = orphan_present

    ok = (
        details["weaviate"]["table_rows_found"] > 0
        and details["weaviate"]["alias_chunks_with_values"] > 0
        and alias_total > 0
        and orphan_total > 0
        and missing_source_file == 0
        and orphan_present
    )
    return CheckResult("Store_checks", ok, details)


def run_qe_agent_queries(domain: str) -> CheckResult:
    queries = [
        "Summarize the ESG policy for wealth portfolio management.",
        "What test cases cover client account verification?",
        "What incidents are recorded for wealth portfolio operations?",
        "How are defects tracked in the wealth portfolio documents?",
    ]
    outputs = []
    for q in queries:
        resp = query_retrieval(domain, q)
        outputs.append(
            {
                "query": q,
                "chunks": resp.get("reranked_chunks", [])[:3],
                "paths": resp.get("reranked_paths", [])[:3],
            }
        )
    ok = all(o.get("chunks") or o.get("paths") for o in outputs)
    return CheckResult("QE_agent_queries", ok, {"queries": outputs})


def write_report(payload: dict[str, Any]) -> str:
    reports_dir = Path("reports") / "analysis"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = reports_dir / f"e2e_api_latest_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(out_path)


def main() -> int:
    wait_for_ready(INGEST_BASE, "/health/ready")
    wait_for_ready(RETRIEVE_BASE, "/health/ready")

    domain = pick_domain()
    expected_files = sorted([p.name for p in Path(DOC_DIR).iterdir() if p.is_file()])

    existing_job_id = os.environ.get("KBF_JOB_ID")
    if existing_job_id:
        job_id = existing_job_id
        job = {"job_id": job_id, "reused": True}
        print(f"[E2E] Reusing job: job_id={job_id}", flush=True)
    else:
        assert_no_running_jobs()
        job = upload_job(domain, DOC_DIR, force_rewrite=True)
        job_id = job.get("job_id")
        if not job_id:
            raise RuntimeError(f"Upload response missing job_id: {job}")
        print(f"[E2E] Upload started: job_id={job_id}", flush=True)

    job_status = poll_job(job_id)
    logs = fetch_job_logs(job_id)

    checks: list[CheckResult] = []
    checks.append(q1_reports_check(expected_files))
    checks.append(q2_live_queries(domain))
    checks.append(q3_change_based(domain))
    checks.append(run_qe_agent_queries(domain))
    checks.append(store_checks(domain))

    achievements = [c.name for c in checks if c.ok]
    gaps = [c.name for c in checks if not c.ok]

    payload = {
        "domain": domain,
        "job": job,
        "job_status": job_status,
        "job_logs_excerpt": logs[-4000:],
        "checks": [c.__dict__ for c in checks],
        "achievements": achievements,
        "gaps": gaps,
    }

    report_path = write_report(payload)
    print(f"[E2E] Report written: {report_path}")

    ok = all(c.ok for c in checks) and job_status.get("status") == "COMPLETED"
    if not ok:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

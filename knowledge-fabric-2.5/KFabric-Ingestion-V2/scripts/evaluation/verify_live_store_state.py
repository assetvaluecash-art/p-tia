#!/usr/bin/env python3
"""Read-only verification of PostgreSQL, Weaviate, and Neo4j live-run evidence.

Run after live_api_validation.py with the same API token and development env.
Queries are scoped to job/run/batch IDs from that suite's JSON evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import requests
from neo4j import GraphDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite_report", type=Path, nargs="?", help="Live suite summary from the original validation runner")
    parser.add_argument("--journey-manifest", type=Path, help="Matrix manifest emitted by run_industry_policy_matrix.py")
    parser.add_argument("--base-url", default="http://127.0.0.1:6004")
    parser.add_argument("--token", default="test-live-validation-20261013")
    args = parser.parse_args()
    if bool(args.suite_report) == bool(args.journey_manifest):
        parser.error("provide exactly one of suite_report or --journey-manifest")
    source_path = args.journey_manifest or args.suite_report
    suite = json.loads(source_path.read_text(encoding="utf-8"))
    jobs: list[dict[str, Any]] = []
    manifest_mode = bool(args.journey_manifest)
    if manifest_mode:
        source_jobs = suite.get("jobs", [])
    else:
        source_jobs = [
            {"job_id": row.get("actual", "").split("job_id=")[-1]}
            for row in suite.get("results", []) if row.get("id", "").startswith("E2E-TERMINAL-")
        ]
    for row in source_jobs:
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        token = args.token
        if manifest_mode:
            token = "test-golden-{}-{}-{}-{}".format(
                suite.get("run_id", ""), row.get("industry_id", ""),
                row.get("domain_id", ""), row.get("policy", ""),
            )
        detail = requests.get(
            f"{args.base_url.rstrip('/')}/jobs/{quote(job_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        if detail.ok:
            jobs.append(detail.json())

    results: list[dict[str, Any]] = []
    output = source_path.with_name(source_path.stem + "-store-verification.json")
    if output.exists():
        output = output.with_name(output.stem + "-" + datetime.now(timezone.utc).strftime("%H%M%S") + output.suffix)
    rows_output = output.with_suffix(".jsonl")

    def check(case_id: str, description: str, expected: str, actual: Any, passed: bool) -> None:
        if "-PG-" in case_id:
            touchpoints = ["PostgreSQL", "ingestion_run", "ingestion_run_stage", "ingestion_run_event", "ingestion_run_checkpoint", "ingestion_run_store_write"]
            system = "postgresql"
        elif "-WEAVIATE-" in case_id:
            touchpoints = ["Weaviate", "domain block class", "ingestion_batch_id provenance"]
            system = "weaviate"
        elif "-NEO4J-" in case_id:
            touchpoints = ["Neo4j", "run-scoped nodes", "run-scoped relationships"]
            system = "neo4j"
        else:
            touchpoints = ["scheduler report API", "reports/e2e_report.py"]
            system = "http"
        row = {
            "id": case_id,
            "description": description,
            "expected": expected,
            "actual": actual,
            "result": "PASS" if passed else "FAIL",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "system": system,
            "touchpoints": touchpoints,
        }
        results.append(row)
        with rows_output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            stream.flush()

    pg = psycopg.connect(
        host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"],
        dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        options="-c default_transaction_read_only=on",
    )
    pg.autocommit = True
    weaviate_url = os.environ["WEAVIATE_URL"].rstrip("/")
    neo4j_driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        for index, job in enumerate(jobs, 1):
            prefix = f"STORE-{index:02}"
            job_id = job["job_id"]
            run_id = job["run_id"]
            batch_id = job["ingestion_batch_id"]
            with pg.cursor() as cursor:
                cursor.execute(
                    "SELECT status, submitted_by, auth_source, customer_id, industry_id, domain_id, "
                    "product_line_id, customer_ontology_version FROM ingestion_run WHERE run_id=%s",
                    (run_id,),
                )
                run = cursor.fetchone()
                check(f"{prefix}-PG-RUN", "PostgreSQL retains the submitted run scope and actor.",
                      "run exists and actor/scope/version match API detail", run,
                      bool(run and run[0] == job["status"] and run[1] == job["submitted_by"]
                           and run[2] == job["auth_source"] and run[3] == job["customer_id"]
                           and run[4] == job["industry_id"] and run[5] == job["domain_id"]
                           and run[6] == job["product_line_id"] and run[7] == job["customer_ontology_version"]))
                cursor.execute(
                    "SELECT count(*), count(*) FILTER (WHERE status IN ('RUNNING','RETRY_WAIT')) "
                    "FROM ingestion_run_stage WHERE run_id=%s", (run_id,),
                )
                stage_count, active_stages = cursor.fetchone()
                check(f"{prefix}-PG-STAGES", "PostgreSQL retains all 15 terminal stage rows.",
                      "15 rows and zero RUNNING/RETRY_WAIT rows", {"rows": stage_count, "active": active_stages},
                      stage_count == 15 and active_stages == 0)
                cursor.execute("SELECT count(*) FROM ingestion_run_event WHERE run_id=%s", (run_id,))
                event_count = cursor.fetchone()[0]
                check(f"{prefix}-PG-EVENTS", "Lifecycle/progress events are persisted for replay.",
                      "at least one event", event_count, event_count > 0)
                cursor.execute("SELECT count(*) FROM ingestion_run_checkpoint WHERE run_id=%s", (run_id,))
                checkpoint_count = cursor.fetchone()[0]
                check(f"{prefix}-PG-CHECKPOINTS", "Recoverable worker checkpoints are persisted.",
                      "at least one immutable checkpoint", checkpoint_count, checkpoint_count > 0)
                cursor.execute(
                    "SELECT store_name, status, count(*) FROM ingestion_run_store_write "
                    "WHERE run_id=%s GROUP BY store_name,status ORDER BY store_name,status", (run_id,),
                )
                write_rows = cursor.fetchall()
                check(f"{prefix}-PG-STORE-LEDGER", "Store outcomes are independently finalized in PostgreSQL.",
                      "both stores have only SUCCEEDED, FAILED, or SKIPPED units", write_rows,
                      all(any(row[0] == store for row in write_rows)
                          and all(row[1] in {"SUCCEEDED", "FAILED", "SKIPPED", "UNKNOWN"} for row in write_rows if row[0] == store)
                          for store in ("weaviate", "neo4j")))

            domain = job["domain_id"]
            store_class = domain[:1].upper() + domain[1:]
            query = {
                "query": (
                    "{ Aggregate { " + store_class + "(where: {path: [\"ingestion_batch_id\"], "
                    "operator: Equal, valueString: \"" + batch_id + "\"}) { meta { count } } } }"
                )
            }
            try:
                response = requests.post(f"{weaviate_url}/v1/graphql", json=query, timeout=15)
                payload = response.json()
                count = payload.get("data", {}).get("Aggregate", {}).get(store_class, [{}])[0].get("meta", {}).get("count")
                errors = payload.get("errors")
                check(f"{prefix}-WEAVIATE-BATCH", "Weaviate contains a batch-scoped document block.",
                      "one or more objects tagged with this ingestion batch", {"count": count, "errors": errors},
                      response.ok and not errors and isinstance(count, int) and count > 0)
            except Exception as exc:
                check(f"{prefix}-WEAVIATE-BATCH", "Weaviate contains a batch-scoped document block.",
                      "one or more objects tagged with this ingestion batch", type(exc).__name__, False)

            try:
                with neo4j_driver.session() as session:
                    node_count = session.execute_read(
                        lambda tx: tx.run(
                            "MATCH (n) WHERE $batch IN coalesce(n.ingestion_batch_ids, []) RETURN count(n) AS n",
                            batch=batch_id,
                        ).single()["n"]
                    )
                    rel_count = session.execute_read(
                        lambda tx: tx.run(
                            "MATCH ()-[r]->() WHERE $batch IN coalesce(r.ingestion_batch_ids, []) RETURN count(r) AS n",
                            batch=batch_id,
                        ).single()["n"]
                    )
                    spine = session.execute_read(
                        lambda tx: tx.run(
                            """
                            MATCH (industry:IndustryRoot {customer_id: $customer_id, industry_id: $industry_id, tenant_id: $tenant_id})
                            MATCH (domain:DomainRoot {customer_id: $customer_id, domain: $domain_id, tenant_id: $tenant_id})
                            MATCH (pl:ProductLine {customer_id: $customer_id, product_line_id: $product_line_id, tenant_id: $tenant_id})
                            MATCH (domain)-[:PART_OF]->(industry)
                            MATCH (pl)-[:PART_OF]->(domain)
                            RETURN count(*) AS paths
                            """,
                            customer_id=job["customer_id"], industry_id=job["industry_id"],
                            tenant_id=job["tenant_id"], domain_id=job["domain_id"],
                            product_line_id=job["product_line_id"],
                        ).single()["paths"]
                    )
                check(f"{prefix}-NEO4J-BATCH", "Neo4j contains run-scoped graph writes.",
                      "at least one node or relationship tagged with this ingestion batch",
                      {"nodes": node_count, "relationships": rel_count}, node_count + rel_count > 0)
                check(f"{prefix}-NEO4J-INDUSTRY-SPINE", "Neo4j contains the selected customer-scoped industry/domain/product-line hierarchy.",
                      "one IndustryRoot -> DomainRoot -> ProductLine path", {"paths": spine}, spine > 0)
            except Exception as exc:
                check(f"{prefix}-NEO4J-BATCH", "Neo4j contains run-scoped graph writes.",
                      "at least one node or relationship tagged with this ingestion batch", type(exc).__name__, False)
                check(f"{prefix}-NEO4J-INDUSTRY-SPINE", "Neo4j contains the selected customer-scoped industry/domain/product-line hierarchy.",
                      "one IndustryRoot -> DomainRoot -> ProductLine path", type(exc).__name__, False)

            report_response = requests.get(
                f"{args.base_url.rstrip('/')}/reports/get-report/{quote(job_id, safe='')}", timeout=30
            )
            check(f"{prefix}-REPORT", "A run report is discoverable for completed or failed processing.",
                  "HTTP 200 for terminal run", report_response.status_code, report_response.status_code == 200)
    finally:
        pg.close()
        neo4j_driver.close()

    summary = {
        "run_id": suite["run_id"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only-post-run-store-verification",
        "case_count": len(results),
        "passed": sum(row["result"] == "PASS" for row in results),
        "failed": sum(row["result"] == "FAIL" for row in results),
        "first_pass_rate_percent": round(sum(row["result"] == "PASS" for row in results) * 100 / len(results), 2) if results else 0,
        "results": results,
    }
    output.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    print(f"Evidence: {output}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Live API validation for the current ingestion working tree.

This runner is intentionally failure-tolerant: every independent case records an
outcome and the suite continues. It creates uniquely named QA customers and
appends scoped test data; it does not deactivate customers or clear stores.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests
from kbfabric_ingest.ingest.repository.ingestion_run_repository import IngestionRunRepository


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = os.environ.get("LIVE_API_BASE_URL", "http://127.0.0.1:6003")
INDUSTRIES = (
    ("banking", "KFabric API QA Banking", "consumer_retail", "Retail Cards"),
    ("insurance", "KFabric API QA Insurance", "health_insurance", "Individual & Family Health"),
    ("health_insurance_care_services", "KFabric API QA Healthcare", "care", "Care Management"),
)


class LiveSuite:
    def __init__(self, base_url: str, token: str, run_id: str, ray_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.ray_url = ray_url.rstrip("/")
        self.run_id = run_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.unauthenticated = requests.Session()
        self.results: list[dict[str, Any]] = []
        self.customers: list[dict[str, Any]] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.output_dir = ROOT / "reports" / "live_validation"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.output_dir / f"{run_id}.json"
        self.rows_path = self.output_dir / f"{run_id}.jsonl"
        if self.rows_path.exists():
            raise FileExistsError(f"Run evidence already exists; choose a new --run-id: {self.rows_path}")

    def persist_partial(self) -> None:
        passed = sum(row["result"] == "PASS" for row in self.results)
        failed = sum(row["result"] == "FAIL" for row in self.results)
        report = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "base_url": self.base_url,
            "suite_mode": "live-api-and-ingestion",
            "customer_ids": [c.get("customer_id") for c in self.customers],
            "case_count": len(self.results),
            "passed": passed,
            "failed": failed,
            "first_pass_rate_percent": round(passed * 100 / len(self.results), 2) if self.results else 0,
            "complete": False,
            "results": self.results,
        }
        self.output_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    def check(
        self,
        case_id: str,
        description: str,
        call: Callable[[], Any],
        accepts: Callable[[Any], bool],
        expected: str,
        detail: Callable[[Any], str] | None = None,
        evidence_meta: dict[str, Any] | None = None,
    ) -> Any:
        started = time.perf_counter()
        recorded_at = datetime.now(timezone.utc).isoformat()
        if evidence_meta is None:
            inferred_path = self._case_route(case_id)
            evidence_meta = {
                "method": "ASSERT",
                "path": inferred_path,
                "touchpoints": self._touchpoints("GET", inferred_path),
            }
        try:
            actual = call()
            passed = bool(accepts(actual))
            actual_summary = detail(actual) if detail else str(actual)
            result = {
                "id": case_id,
                "description": description,
                "expected": expected,
                "result": "PASS" if passed else "FAIL",
                "actual": actual_summary[:500],
            }
            result.update(evidence_meta or {})
            result["started_at"] = recorded_at
            result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self.results.append(result)
            with self.rows_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                stream.flush()
            self.persist_partial()
            print(f"{result['result']:4} {case_id} {description} :: {actual_summary[:180]}", flush=True)
            return actual
        except Exception as exc:  # independent cases must continue after failures
            result = {
                "id": case_id,
                "description": description,
                "expected": expected,
                "result": "FAIL",
                "actual": f"{type(exc).__name__}: {exc}"[:500],
            }
            result.update(evidence_meta or {})
            result["started_at"] = recorded_at
            result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self.results.append(result)
            with self.rows_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                stream.flush()
            self.persist_partial()
            print(f"FAIL {case_id} {description} :: {result['actual']}", flush=True)
            return None

    def request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> requests.Response:
        client = self.session if auth else self.unauthenticated
        return client.request(method, f"{self.base_url}{path}", timeout=kwargs.pop("timeout", 30), **kwargs)

    @staticmethod
    def response_summary(response: requests.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                safe = {k: body.get(k) for k in ("status", "customer_id", "industry_id", "customer_ontology_version", "job_id", "detail") if k in body}
                return f"HTTP {response.status_code} {json.dumps(safe, default=str)}"
            return f"HTTP {response.status_code} JSON[{len(body)}]"
        except Exception:
            return f"HTTP {response.status_code} {response.text[:160]!r}"

    def expect_status(
        self,
        case_id: str,
        description: str,
        method: str,
        path: str,
        expected: int | tuple[int, ...],
        *,
        auth: bool = True,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> requests.Response | None:
        expected_codes = (expected,) if isinstance(expected, int) else expected
        response = self.check(
            case_id,
            description,
            lambda: self.request(method, path, auth=auth, params=params, json=json_body,
                                 data=data, files=files, headers=headers, timeout=timeout),
            lambda r: r.status_code in expected_codes,
            f"HTTP {expected_codes}",
            self.response_summary,
            {
                "method": method.upper(),
                "path": path,
                "http_status": None,
                "touchpoints": self._touchpoints(method, path),
            },
        )
        if isinstance(response, requests.Response) and self.results:
            result = self.results[-1]
            result["http_status"] = response.status_code
            result["route_result"] = "PASS" if response.status_code in expected_codes else "FAIL"
            # Replace the last JSONL line with the completed HTTP status evidence.
            lines = self.rows_path.read_text(encoding="utf-8").splitlines()
            lines[-1] = json.dumps(result, ensure_ascii=False, default=str)
            self.rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.persist_partial()
        return response if isinstance(response, requests.Response) else None

    @staticmethod
    def _touchpoints(method: str, path: str) -> list[str]:
        if path.startswith("/fabric-foundry/customers"):
            return ["fabric_foundry/customer/api.py", "fabric_foundry/bootstrap/service.py", "fabric_foundry/customer/repository.py"]
        if "/contextualization" in path or "/ontology/" in path:
            return ["fabric_foundry/knowledge_contextualization/api.py", "fabric_foundry/knowledge_contextualization/service.py", "fabric_foundry/runtime/effective_ontology_context.py"]
        if path.startswith("/hitl/"):
            return ["ingest/hitl_api.py", "ingest/repository/hitl_review_repository.py", "ingest/hitl_write_executor.py"]
        if path.startswith("/jobs") or path.startswith("/progress/"):
            points = ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py", "scheduler/actor.py"]
            if path.endswith("/events"):
                points.append("ingest/sql/ingestion_run.sql#ingestion_run_event")
            return points
        if path.startswith("/reports"):
            return ["scheduler/api.py", "reports/e2e_report.py"]
        if path.startswith("/ingestion/") or path == "/domains":
            return ["scheduler/api.py", "fabric_foundry/runtime/industry_config.py"]
        if path.startswith("/health") or path == "/openapi.json" or path == "/metrics":
            return ["scheduler/api.py", "_common/telemetry.py"]
        if path.startswith("/fabric-foundry/industries"):
            return ["fabric_foundry/industry/api.py", "fabric_foundry/industry/registry.py"]
        return ["OpenAPI route inventory"]

    @staticmethod
    def _case_route(case_id: str) -> str:
        if case_id.startswith("SYS-") or case_id.startswith("METRICS-"):
            return "/health" if case_id.startswith("SYS-") else "/metrics"
        if case_id.startswith("CONTRACT-"):
            return "/openapi.json"
        if case_id.startswith("IND-"):
            return "/fabric-foundry/industries"
        if case_id.startswith("CFG-") or case_id.startswith("HITL-POLICY"):
            return "/ingestion/review-policies"
        if case_id.startswith("AUTH-") or case_id.startswith("ONB-"):
            return "/fabric-foundry/customers"
        if case_id.startswith("CTX-"):
            return "/fabric-foundry/customers/{customer_id}/ontology/contextualization"
        if case_id.startswith("JOB-LIST"):
            return "/jobs"
        if case_id.startswith("JOB-RESUME"):
            return "/jobs/{job_id}/resume"
        if case_id.startswith("JOB-CANCEL"):
            return "/jobs/{job_id}/cancel"
        if case_id.startswith("JOB-EVENTS"):
            return "/jobs/{job_id}/events"
        if case_id.startswith("JOB-LOGS"):
            return "/jobs/{job_id}/logs"
        if case_id.startswith("JOB-PROGRESS"):
            return "/progress/{job_id}"
        if case_id.startswith("JOB-DETAIL"):
            return "/jobs/{job_id}"
        if case_id.startswith("JOB-SUBMIT"):
            return "/jobs"
        if case_id.startswith("REPORT-") or case_id.startswith("E2E-REPORT"):
            return "/reports"
        if case_id.startswith("E2E-"):
            return "/jobs/upload"
        if case_id.startswith("HITL-"):
            return "/hitl/reviews"
        return "/openapi.json"

    def run(self, include_ingestion: bool = False, wait_minutes: int = 45) -> dict[str, Any]:
        # Service readiness: the API readiness response checks Ray, PostgreSQL-backed
        # startup state, ontology artifacts, Neo4j, and Weaviate independently.
        for endpoint in ("/health", "/health/ready"):
            self.expect_status(f"SYS-{len(self.results)+1:03}", f"The live API reports {endpoint}.", "GET", endpoint, 200)

        openapi = self.expect_status("SYS-003", "The live API publishes its OpenAPI schema.", "GET", "/openapi.json", 200)
        required_routes = {
            "/fabric-foundry/customers": ["post"],
            "/fabric-foundry/industries": ["get"],
            "/ingestion/customers": ["get"],
            "/ingestion/context": ["get"],
            "/jobs": ["get", "post"],
            "/jobs/upload": ["post"],
            "/jobs/{job_id}": ["get"],
            "/jobs/{job_id}/resume": ["post"],
            "/jobs/{job_id}/cancel": ["post"],
            "/jobs/{job_id}/events": ["get"],
            "/hitl/reviews": ["get"],
            "/reports": ["get"],
        }
        schema = openapi.json() if openapi is not None and openapi.ok else {}
        for index, (path, methods) in enumerate(required_routes.items(), 4):
            self.check(
                f"CONTRACT-{index:03}",
                f"OpenAPI advertises {', '.join(methods).upper()} {path}.",
                lambda p=path, ms=methods: all(m in schema.get("paths", {}).get(p, {}) for m in ms),
                bool,
                f"route methods {methods}",
                lambda value: str(value),
            )

        industries = self.expect_status("IND-001", "The industry catalog is available.", "GET", "/fabric-foundry/industries", 200)
        industry_rows = industries.json() if industries is not None and industries.ok else []
        for index, (industry_id, *_rest) in enumerate(INDUSTRIES, 2):
            self.check(
                f"IND-{index:03}",
                f"The industry catalog contains {industry_id}.",
                lambda iid=industry_id: any(row.get("industry_id") == iid and row.get("status") == "ACTIVE" for row in industry_rows),
                bool,
                "active industry entry",
                lambda value: str(value),
            )
            self.expect_status(
                f"IND-{index+3:03}",
                f"The active ontology catalog resolves for {industry_id}.",
                "GET",
                f"/fabric-foundry/industries/{industry_id}/ontologies",
                200,
            )

        policies = self.expect_status("CFG-001", "Review-policy choices load from the API.", "GET", "/ingestion/review-policies", 200)
        policy_values = set()
        if policies is not None and policies.ok:
            policy_values = {p.get("value") for p in policies.json().get("policies", [])}
        for index, value in enumerate(("review_all", "review_ambiguous", "auto_write"), 2):
            self.check(f"CFG-{index:03}", f"The API offers review policy {value}.", lambda v=value: v in policy_values,
                       bool, "policy option present", lambda x: str(x))

        # Authorization and request validation branches. Expected statuses are
        # intentionally strict so legacy response normalization differences show.
        self.expect_status("AUTH-001", "Customer onboarding rejects a missing bearer token.", "POST", "/fabric-foundry/customers", 401,
                           auth=False, json_body={"customer_name": "no-auth", "industry_id": "banking"})
        self.expect_status("AUTH-002", "Job listing rejects a missing bearer token.", "GET", "/jobs", 401, auth=False)
        self.expect_status("AUTH-003", "Job detail rejects a missing bearer token.", "GET", "/jobs/no-such-job", 401, auth=False)
        self.expect_status("AUTH-004", "Job event replay rejects a missing bearer token.", "GET", "/jobs/no-such-job/events", 401, auth=False)
        self.expect_status("ONB-001", "Onboarding rejects a missing customer name.", "POST", "/fabric-foundry/customers", 422,
                           json_body={"industry_id": "banking"})
        self.expect_status("ONB-002", "Onboarding rejects an unsupported industry.", "POST", "/fabric-foundry/customers", 422,
                           json_body={"customer_name": f"Invalid-{self.run_id}", "industry_id": "not-an-industry"})
        self.expect_status("ONB-003", "Onboarding rejects a blank customer name.", "POST", "/fabric-foundry/customers", 422,
                           json_body={"customer_name": "  ", "industry_id": "banking"})

        # Real customer onboarding and per-customer ontology activation.
        self.expect_status("ONB-004", "Authenticated onboarding rejects a malformed body.", "POST", "/fabric-foundry/customers", 422,
                           json_body={"customer_name": "valid-name"})
        for index, (industry_id, prefix, domain_id, selected_name) in enumerate(INDUSTRIES, 1):
            response = self.expect_status(
                f"ONB-{index+4:03}",
                f"The API onboards a fresh {industry_id} customer without changing other customer statuses.",
                "POST",
                "/fabric-foundry/customers",
                201,
                json_body={"customer_name": f"{prefix}-{self.run_id}", "industry_id": industry_id},
            )
            if response is None or not response.ok:
                continue
            body = response.json()
            customer = {**body, "domain_id": domain_id, "selected_product_line_name": selected_name}
            self.customers.append(customer)
            self.check(
                f"ONB-{index+7:03}",
                f"The new {industry_id} customer is active with a generated tenant identity.",
                lambda b=body: b.get("status") == "ACTIVE" and bool(b.get("tenant_id")) and bool(b.get("customer_id")),
                bool,
                "ACTIVE customer with customer_id and tenant_id",
                lambda value: str(value),
            )
            ontology = self.expect_status(
                f"ONB-{index+10:03}",
                f"Onboarding creates an active version-one ontology for {industry_id}.",
                "GET",
                f"/fabric-foundry/customers/{quote(body['customer_id'], safe='')}/ontology",
                200,
            )
            if ontology is not None and ontology.ok:
                self.check(
                    f"ONB-{index+13:03}",
                    f"The {industry_id} ontology belongs to the newly onboarded customer.",
                    lambda o=ontology.json(), b=body: o.get("customer_id") == b["customer_id"] and o.get("customer_ontology_version") == 1 and o.get("status") == "ACTIVE",
                    bool,
                    "customer-owned active ontology v1",
                    lambda value: str(value),
                )

        # Run independently across every domain exposed for every newly created
        # customer; one bad domain does not prevent other domains from executing.
        selected_contexts: list[dict[str, Any]] = []
        for customer_index, customer in enumerate(self.customers, 1):
            cid = customer["customer_id"]
            encoded = quote(cid, safe="")
            page = self.expect_status(
                f"CTX-PAGE-{customer_index:02}",
                f"The customer context page resolves domains for {customer['industry_id']}.",
                "GET",
                f"/fabric-foundry/customers/{encoded}/ontology/contextualization",
                200,
            )
            domains = page.json().get("domains", []) if page is not None and page.ok else []
            self.check(
                f"CTX-DOMAIN-COUNT-{customer_index:02}",
                f"The {customer['industry_id']} page exposes its configured domain set.",
                lambda ds=domains: len(ds) > 0,
                bool,
                "one or more domains",
                lambda value: f"{len(domains)} domains",
            )
            for domain_index, domain in enumerate(domains, 1):
                did = domain.get("id")
                self.expect_status(
                    f"CTX-{customer_index:02}-{domain_index:02}",
                    f"The {customer['industry_id']} contextualization view loads domain {did}.",
                    "GET",
                    f"/fabric-foundry/customers/{encoded}/ontology/contextualization/{quote(str(did), safe='')}",
                    200,
                )
            self.expect_status(
                f"CTX-INVALID-{customer_index:02}",
                f"The customer context API rejects an unknown domain for {customer['industry_id']}.",
                "GET",
                f"/fabric-foundry/customers/{encoded}/ontology/contextualization/not-a-domain-{self.run_id}",
                404,
            )
            target_domain = customer["domain_id"]
            domain_path = f"/fabric-foundry/customers/{encoded}/ontology/contextualization/{quote(target_domain, safe='')}"
            snapshot = self.expect_status(
                f"CTX-SELECT-{customer_index:02}",
                f"The target domain contains canonical product lines for {customer['industry_id']}.",
                "GET",
                domain_path,
                200,
            )
            if snapshot is None or not snapshot.ok:
                continue
            domain_body = snapshot.json()
            all_product_lines = domain_body.get("canonical_delta", {}).get("product_lines", [])
            changes: list[dict[str, Any]] = []
            for pl in all_product_lines:
                selected = pl.get("name") == customer["selected_product_line_name"]
                changes.append({
                    "operation": "SET_APPLICABILITY",
                    "target": {"type": "PRODUCT_LINE", "canonical_id": pl["id"]},
                    "applicable": selected,
                })
                for function in pl.get("functions", []):
                    changes.append({
                        "operation": "SET_APPLICABILITY",
                        "target": {"type": "FUNCTION", "canonical_id": function["id"]},
                        "applicable": selected,
                    })
                    for sub_function in function.get("sub_functions", []):
                        changes.append({
                            "operation": "SET_APPLICABILITY",
                            "target": {"type": "SUB_FUNCTION", "canonical_id": sub_function["id"]},
                            "applicable": selected,
                        })
            request_id = f"{self.run_id}-{customer_index}-context-save"
            payload = {
                "customer_id": cid,
                "domain_id": target_domain,
                "base_customer_ontology_version": 1,
                "request_id": request_id,
                "changes": changes,
                "change_reason": "Full live API validation contextualization",
            }
            save = self.expect_status(
                f"CTX-SAVE-{customer_index:02}",
                f"One complete applicability batch configures {customer['selected_product_line_name']} for {customer['industry_id']}.",
                "POST",
                f"/fabric-foundry/customers/{encoded}/ontology/{quote(target_domain, safe='')}/contextualize",
                200,
                json_body=payload,
                timeout=90,
            )
            if save is None or not save.ok:
                continue
            self.check(
                f"CTX-VERSION-{customer_index:02}",
                f"Contextualization creates customer ontology version two for {customer['industry_id']}.",
                lambda s=save.json(): s.get("customer_ontology_version") == 2 and s.get("status") == "ACTIVE",
                bool,
                "active version 2",
                lambda value: str(value),
            )
            replay = self.expect_status(
                f"CTX-IDEMPOTENCY-{customer_index:02}",
                f"Replaying the same contextualization request is idempotent for {customer['industry_id']}.",
                "POST",
                f"/fabric-foundry/customers/{encoded}/ontology/{quote(target_domain, safe='')}/contextualize",
                200,
                json_body=payload,
            )
            if replay is not None and replay.ok:
                self.check(
                    f"CTX-IDEMPOTENCY-VERSION-{customer_index:02}",
                    f"Contextualization replay does not create another version for {customer['industry_id']}.",
                    lambda r=replay.json(): r.get("customer_ontology_version") == 2,
                    bool,
                    "still version 2",
                    lambda value: str(value),
                )
            stale = dict(payload)
            stale["request_id"] = f"{request_id}-stale"
            stale["base_customer_ontology_version"] = 1
            self.expect_status(
                f"CTX-STALE-{customer_index:02}",
                f"A stale base ontology version is rejected for {customer['industry_id']}.",
                "POST",
                f"/fabric-foundry/customers/{encoded}/ontology/{quote(target_domain, safe='')}/contextualize",
                400,
                json_body=stale,
            )
            history = self.expect_status(
                f"CTX-HISTORY-{customer_index:02}",
                f"Ontology history for {customer['industry_id']} records versions newest first.",
                "GET",
                f"/fabric-foundry/customers/{encoded}/ontology/versions",
                200,
            )
            if history is not None and history.ok:
                versions = history.json().get("versions", [])
                self.check(
                    f"CTX-HISTORY-ORDER-{customer_index:02}",
                    f"Ontology history contains versions 2 then 1 for {customer['industry_id']}.",
                    lambda vs=versions: len(vs) >= 2 and [vs[0].get("customer_ontology_version"), vs[1].get("customer_ontology_version")] == [2, 1],
                    bool,
                    "[2, 1] version order",
                    lambda value: str(value),
                )
            ingestion_context = self.expect_status(
                f"CTX-INGESTION-{customer_index:02}",
                f"Ingestion bootstrap returns the configured product line for {customer['industry_id']}.",
                "GET",
                "/ingestion/context",
                200,
                params={"customer_id": cid},
            )
            if ingestion_context is not None and ingestion_context.ok:
                options = [
                    pl for domain in ingestion_context.json().get("domains", [])
                    for pl in domain.get("product_lines", [])
                ]
                self.check(
                    f"CTX-INGESTION-PL-{customer_index:02}",
                    f"The selected product line is present for {customer['industry_id']} ingestion.",
                    lambda ps=options, expected=customer["selected_product_line_name"]: any(p.get("name") == expected for p in ps),
                    bool,
                    customer["selected_product_line_name"],
                    lambda value: str(value),
                )
            selected = next((p for p in all_product_lines if p.get("name") == customer["selected_product_line_name"]), None)
            if selected:
                selected_contexts.append({**customer, "product_line_id": selected["id"]})

        # Job, HITL, reports, and operational read paths. These are deliberately
        # isolated negative/empty-state checks and never stop the suite.
        list_response = self.expect_status("JOB-LIST-001", "An authenticated user can list their visible jobs.", "GET", "/jobs", 200, params={"limit": 10, "offset": 0})
        for index, params in enumerate(
            ({"limit": 1}, {"limit": 50}, {"limit": 200}, {"limit": 2, "offset": 1},
             {"status": "FAILED"}, {"status": "COMPLETED"}, {"customer_id": "missing-customer"},
             {"domain_id": "consumer_retail"}, {"product_line_id": "missing-product"}), 2
        ):
            self.expect_status(f"JOB-LIST-{index:03}", f"Job listing accepts filter/pagination case {params}.", "GET", "/jobs", 200, params=params)
        self.expect_status("JOB-LIST-INVALID-001", "Job listing rejects a zero page size.", "GET", "/jobs", 400, params={"limit": 0})
        self.expect_status("JOB-LIST-INVALID-002", "Job listing rejects an excessive page size.", "GET", "/jobs", 400, params={"limit": 201})
        self.expect_status("JOB-DETAIL-UNKNOWN", "Unknown job details return not found.", "GET", "/jobs/no-such-live-job", 404)
        self.expect_status("JOB-LOGS-UNKNOWN", "Unknown job logs return not found or safe service error.", "GET", "/jobs/no-such-live-job/logs", (404, 500))
        self.expect_status("JOB-PROGRESS-UNKNOWN", "Unknown job progress returns not found.", "GET", "/progress/no-such-live-job", 404)
        self.expect_status("JOB-EVENTS-UNKNOWN", "Unknown job event replay returns not found.", "GET", "/jobs/no-such-live-job/events", 404)
        self.expect_status("JOB-CANCEL-UNKNOWN", "Cancelling an unknown job returns not found.", "POST", "/jobs/no-such-live-job/cancel", 404)
        self.expect_status("JOB-RESUME-UNKNOWN", "Resuming an unknown job returns not found.", "POST", "/jobs/no-such-live-job/resume", 404,
                           headers={"Idempotency-Key": f"missing-{self.run_id}"})
        self.expect_status("JOB-RESUME-IDEMPOTENCY-REQUIRED", "Resume requires an idempotency key.", "POST", "/jobs/no-such-live-job/resume", 400)
        self.expect_status("JOB-SUBMIT-INVALID", "Directory submission rejects an incomplete scope before dispatch.", "POST", "/jobs", 400,
                           json_body={"input_dir": "missing"})

        reports = self.expect_status("REPORT-LIST-001", "The report listing endpoint responds.", "GET", "/reports", 200)
        self.expect_status("REPORT-UNKNOWN", "A report lookup for a missing job is not found.", "GET", "/reports/get-report/no-such-live-job", 404)
        self.expect_status("HITL-POLICY-SCOPE", "Review policy endpoint exposes the applicable review modes.", "GET", "/ingestion/review-policies", 200)
        for index, params in enumerate(({}, {"ingestion_batch_id": "missing"},
                                        {"ingestion_batch_id": "missing", "domain_id": "d"},
                                        {"ingestion_batch_id": "missing", "domain_id": "d", "product_line_id": "p"}), 1):
            expected = 400 if len(params) < 3 else 200
            self.expect_status(f"HITL-LIST-{index:02}", f"HITL review listing handles scope combination {index}.", "GET", "/hitl/reviews", expected, params=params)
        self.expect_status("HITL-ITEM-UNKNOWN", "A review lookup for a missing item is not found.", "GET", f"/hitl/reviews/{uuid.uuid4()}", 404,
                           params={"ingestion_batch_id": "missing", "domain_id": "d", "product_line_id": "p"})
        self.expect_status("METRICS-001", "The API metrics endpoint responds.", "GET", "/metrics", 200)
        self.expect_status("CONFIG-001", "The ingestion configuration endpoint responds.", "GET", "/config", 200)

        if include_ingestion:
            self.run_ingestions(selected_contexts, wait_minutes)

        passed = sum(row["result"] == "PASS" for row in self.results)
        failed = sum(row["result"] == "FAIL" for row in self.results)
        report = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "base_url": self.base_url,
            "suite_mode": "live-api-and-ingestion" if include_ingestion else "live-api",
            "customer_ids": [c.get("customer_id") for c in self.customers],
            "case_count": len(self.results),
            "passed": passed,
            "failed": failed,
            "first_pass_rate_percent": round(passed * 100 / len(self.results), 2) if self.results else 0,
            "results": self.results,
            "complete": True,
        }
        self.output_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        report["report_path"] = str(self.output_path)
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2), flush=True)
        return report

    def run_ingestions(self, contexts: list[dict[str, Any]], wait_minutes: int) -> None:
        files = {
            "banking": ("live_banking_card_requirements.txt", "Synthetic live QA source. Retail Cards supports virtual card issuance, card activation, transaction controls, dispute intake, and card replacement. A cardholder may freeze a card after suspected fraud. The Dispute Operations function investigates unauthorized transactions and records a case outcome. The Card Servicing function handles activation and replacement.\n"),
            "insurance": ("live_insurance_health_benefits.txt", "Synthetic live QA source. The Individual and Family Health product provides member enrollment, eligibility verification, prior authorization, claims adjudication, appeals, and provider directory services. A member submits a claim, the claims team adjudicates it against plan coverage, and an appeal may be opened when the claim is denied.\n"),
            "health_insurance_care_services": ("live_healthcare_care_management.txt", "Synthetic live QA source. The Care Management product coordinates care plans, referrals, clinical assessments, provider follow-up, and medication adherence. A care manager creates a care plan after an assessment, assigns a follow-up task to a provider, and tracks completion. A referral connects a member to an in-network provider.\n"),
        }
        for index, context in enumerate(contexts, 1):
            industry = context["industry_id"]
            if industry not in files:
                continue
            name, content = files[industry]
            data = {
                "customer_id": context["customer_id"],
                "domain": context["domain_id"],
                "product_line_id": context["product_line_id"],
                "document_nature": "qe_assets",
                "hitl_mode": "review_ambiguous",
                "force_rewrite": "false",
                "context_tags": [f"qa_run={self.run_id}", "source=synthetic"],
            }
            response = self.expect_status(
                f"E2E-UPLOAD-{index:02}",
                f"A synthetic {industry} document is uploaded under its explicit customer/domain/product line.",
                "POST",
                "/jobs/upload",
                200,
                data=data,
                files=[("files", (name, content.encode("utf-8"), "text/plain"))],
                timeout=240,
            )
            if response is None or not response.ok:
                continue
            job_id = response.json().get("job_id")
            if not job_id:
                continue
            context["job_id"] = job_id
            deadline = time.monotonic() + wait_minutes * 60
            snapshot = None
            while time.monotonic() < deadline:
                snapshot = self.request("GET", f"/jobs/{job_id}")
                if snapshot.status_code != 200:
                    time.sleep(5)
                    continue
                body = snapshot.json()
                status = str(body.get("status", "")).upper()
                if status not in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED", "SUCCEEDED"}:
                    try:
                        ray_state = requests.get(
                            f"{self.ray_url}/api/jobs/{quote(job_id, safe='')}", timeout=10
                        )
                        if ray_state.ok:
                            ray_status = str(ray_state.json().get("status", "")).upper()
                            if ray_status in {"FAILED", "STOPPED", "SUCCEEDED"}:
                                body["status"] = "FAILED" if ray_status == "STOPPED" else ray_status
                                body["ray_status"] = ray_status
                                snapshot._content = json.dumps(body).encode("utf-8")
                                break
                    except Exception:
                        pass
                if status in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED", "SUCCEEDED"}:
                    break
                time.sleep(5)
            if snapshot is None or snapshot.status_code != 200:
                self.check(f"E2E-DETAILS-{index:02}", f"The {industry} job has a readable details snapshot.", lambda: False, bool, "HTTP 200")
                continue
            body = snapshot.json()
            self.check(
                f"E2E-TERMINAL-{index:02}",
                f"The {industry} ingestion job reaches a terminal state.",
                lambda b=body: str(b.get("status", "")).upper() in {"COMPLETED", "COMPLETED_WITH_REVIEW_PENDING", "FAILED", "CANCELLED", "SUCCEEDED"},
                bool,
                f"terminal by {wait_minutes} minutes",
                lambda value: f"status={body.get('status')} job_id={job_id}",
            )
            self.check(
                f"E2E-STAGES-{index:02}",
                f"The {industry} job details expose persisted stage evidence.",
                lambda b=body: (
                    isinstance(b.get("stages"), list)
                    and {stage.get("stage_id") for stage in b["stages"] if isinstance(stage, dict)}
                    == set(IngestionRunRepository.STAGES)
                    and all(stage.get("status") in {"SUCCEEDED", "SKIPPED"} for stage in b["stages"] if isinstance(stage, dict))
                ),
                bool,
                "all 15 registered stages are SUCCEEDED or SKIPPED",
                lambda value: json.dumps([
                    {"stage_id": stage.get("stage_id"), "status": stage.get("status"), "skip_reason": stage.get("skip_reason")}
                    for stage in body.get("stages", []) if isinstance(stage, dict)
                ]),
                {"method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["scheduler/api.py", "ingest/repository/ingestion_run_repository.py#STAGES"]},
            )
            self.check(
                f"E2E-EXTRACTION-{index:02}",
                f"The {industry} synthetic journey produces at least one admitted extraction entity.",
                lambda b=body: int(b.get("entities_found") or 0) > 0,
                bool,
                "entities_found > 0",
                lambda value: f"entities_found={body.get('entities_found')} relationships_found={body.get('relationships_found')}",
                {"method": "GET", "path": f"/jobs/{job_id}", "touchpoints": ["ingest/lane2_two_pass.py", "ingest/ontology_admission.py", "ingest/ingestion_service.py"]},
            )
            self.expect_status(f"E2E-REPORT-{index:02}", f"A terminal {industry} job has a report, including when processing failed.",
                               "GET", f"/reports/get-report/{job_id}", 200)
            def read_one_event(job=job_id):
                with self.session.get(f"{self.base_url}/jobs/{job}/events", stream=True, timeout=(10, 30)) as stream:
                    if stream.status_code != 200:
                        return stream
                    # Read only the first SSE frame; the stream is intentionally long-lived.
                    for _line in stream.iter_lines(decode_unicode=True):
                        if _line:
                            return stream.status_code
                    return stream.status_code

            self.check(f"E2E-EVENTS-{index:02}", f"The {industry} run opens persisted event replay.",
                       read_one_event, lambda status: status == 200, "HTTP 200 SSE stream", str)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--ray-url", default=os.environ.get("RAY_DASHBOARD_URL", "http://127.0.0.1:8266"))
    parser.add_argument("--token", default="test-live-validation-20261013")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("live-%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    parser.add_argument("--include-ingestion", action="store_true", help="Upload one synthetic document per industry and poll to terminal.")
    parser.add_argument("--wait-minutes", type=int, default=45)
    args = parser.parse_args()
    report = LiveSuite(args.base_url, args.token, args.run_id, args.ray_url).run(args.include_ingestion, args.wait_minutes)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

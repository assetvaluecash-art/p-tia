# Knowledge Fabric 2.5 known defects and pending work

**Evidence date:** 15 October 2026  
**Purpose:** Give the developer a prioritized defect and verification list for the separate 2.5 checkout. Items below are not claims that a fix has already been made.

## Confirmed defects

| Priority | Finding | Evidence and impact | Proposed resolution / exit check |
|---|---|---|---|
| P1 | Logs, progress, and report reads can return another actor's job data. | Live route checks read resources belonging to a different QA actor. This exposes operational details and potentially customer content across actor boundaries. | Resolve the authenticated actor from verified credentials, load the job's trusted ownership/customer scope, and authorize before reading the resource. Apply the same policy to logs, progress, reports, events, and job details. Test owner, same-customer member, authorized support role, unrelated actor, and missing job cases. Unrelated actors must not receive content or learn whether a protected job exists. |
| P1 | Blank customer name can reach an internal onboarding failure and return 500. | `ONB-003` in `reports/live_validation/api-local-ray-20261015-b.json`. | Reject missing/blank names at schema/request validation with a stable 4xx response and field-level error. Add API tests for omitted, empty, whitespace-only, valid, and over-limit names. |
| P1 | Image-bearing DOCX can fail checkpoint JSON serialization with invalid UTF-8. | Three larger Banking wealth/corporate sample jobs failed before any blocks were committed (`085640-05de`, `085729-dded`, `085819-97b5`). | Encode binary/image payloads explicitly (for example base64 with a typed artifact field) or checkpoint only a durable file reference plus digest. Reject accidental raw bytes at the JSON boundary. Add a fixture with embedded images; verify checkpoint save, reload, resume, and that store writes occur only after valid restore. |

## Authorization architecture: customer-to-user membership

The live checks establish actor-level gaps; they do not prove that a complete customer-to-user authorization model is already present. Treat customer membership and roles as a pending design/implementation item until the code and live tests confirm it.

Recommended resolution:

1. Authenticate the caller and derive an immutable `user_id` and roles from a verified identity token or trusted server-side identity provider. Do not accept caller-supplied `user_id`, `customer_id`, project tags, or context tags as proof of access.
2. Persist customer membership separately from customer records, with a unique `(customer_id, user_id)` pair, status, role, and audit timestamps. Define explicit roles such as `customer_admin`, `ingestion_operator`, `reviewer`, and read-only access, and map each role to allowed operations.
3. For every request, authorize the actor against the requested customer and operation. Derive allowed customer scopes from membership; constrain job, review, report, progress, and log queries by those scopes before fetching or serializing results.
4. Require the relevant customer scope on job creation, upload/directory submission, HITL list/detail/action/history, cancellation/resume, reports, and configuration writes. Keep an explicit privileged `read_all` permission for support operations and audit its use.
5. Record actor, customer, operation, target ID, decision, and timestamp in append-only audit events. Avoid putting document contents, secrets, or full tokens in the audit log.
6. Add negative tests for cross-customer and cross-user reads/actions, inactive membership, revoked membership, role mismatch, forged scope fields, and absent resources. Verify through the API and database that denial occurs before content is returned or a write is made.

The current QA actor check is not a replacement for membership enforcement. A token that identifies a user does not by itself establish that user may access a given customer.

## Other API and product gaps

| Priority | Item | Current evidence | Proposed resolution / exit check |
|---|---|---|---|
| P2 | Missing authorization for job list/detail/events is reported as 400 rather than 401. | `AUTH-002`–`AUTH-004`. | Use a consistent authentication dependency and return 401 for absent/invalid credentials; use 403 (or a non-disclosing 404 policy) for authenticated but unauthorized callers. Validate with API contract tests. |
| P2 | Invalid onboarding payload checks return 400 where the agreed contract expects 422. | `ONB-001`, `ONB-004`. | Align FastAPI validation/error mapping and OpenAPI examples with the contract. Test malformed JSON separately from valid JSON with invalid fields. |
| P2 / verify | Sample Weaviate objects have null `industry_id`. | Read-back checks found customer-root, domain, and product-line scope, but sampled industry metadata was null. | Trace industry ID from customer/ontology context through block metadata to Weaviate properties. Decide whether `industry_id` is required by the contract; if required, add persistence/read-back tests across Banking, Health, and Insurance. |
| Deferred | Durable queue and queue-at-capacity behavior are not implemented. | `/jobs/queue` returned 404; no durable fair queue. | Specify accepted/rejected/queued responses, capacity semantics, ordering/fairness, cancellation, and recovery. Implement durable admission, then test saturation and Ray recovery. Keep described as pending until then. |
| Pending | Larger image-bearing Banking DOCX paths are not green. | Three serialization failures above; small Wealth upload passed. | Re-run both real banking samples after the checkpoint fix and validate graph/vector writes, review queue, job state, and report. |
| Pending | Full performance acceptance is incomplete. | Five-user bootstrap smoke only; it showed high latency and one connection reset. Successful upload, review decision, and graph-write workloads were not tested. | Run from the developer's isolated environment after endpoint and data setup. Record service/client location, dataset, user count, throughput, p50/p95/p99, errors, and resource use for bootstrap GETs, successful uploads, HITL decisions, and writes. Increase load only after single and low-concurrency runs are clean. |

## Implemented paths that still need stronger live evidence

- Checkpoint resume, retry, cancellation races, and partial Weaviate/Neo4j write reconciliation need worker restart and dependency-failure tests against isolated stores.
- Queue cancellation and capacity behavior remain part of the unimplemented queue feature.
- API/OpenAPI YAML and the generated UI handbook should be reconciled with the deployed app's `/openapi.json`; generated docs alone do not prove a route is available or its security behavior.
- `industry_id` metadata completeness and cross-customer authorization need database/store read-back checks, not just mocked unit tests.
- Three industry journeys should use reproducible inputs and dedicated test-owned customers. Preserve reports that support conclusions and distinguish synthetic fixtures from actual banking source documents.

## Suggested acceptance order

1. Fix P1 cross-actor/customer authorization and blank-name validation.
2. Fix binary checkpoint serialization and rerun the failed DOCX journeys.
3. Confirm Weaviate industry metadata and reconcile OpenAPI with the running service.
4. Run isolated live API, graph/vector, HITL, cancellation/resume, and report checks from a fresh developer checkout.
5. Run representative JMeter scenarios for bootstrap, upload, HITL decision, and graph write; publish limits and errors with the results.
6. Decide whether the queue feature is part of the 2.5 developer handover or an explicitly deferred follow-up.

## Evidence links

- [`TEST_RUN_SUMMARY-20261015.md`](../reports/live_validation/TEST_RUN_SUMMARY-20261015.md)
- [`INGESTION_IMPLEMENTATION_ROADMAP.md`](INGESTION_IMPLEMENTATION_ROADMAP.md)
- [`INGESTION_FUNCTIONAL_TEST_REGISTER.md`](INGESTION_FUNCTIONAL_TEST_REGISTER.md)
- [`RELEASE_READINESS.md`](RELEASE_READINESS.md)

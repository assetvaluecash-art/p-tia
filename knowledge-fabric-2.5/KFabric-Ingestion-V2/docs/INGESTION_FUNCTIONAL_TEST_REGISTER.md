# Ingestion Functional Test Register

This register is the durable record of functional verification for Ingestion Studio, job tracking, HITL, and the publish path. Test statements are written in plain English so product, UI, and engineering teams can review the same expected behavior.

## How to read the register

- **Passed** means the listed test ran and passed in the stated environment.
- **Planned** means the scenario is required but has not yet been run.
- **Blocked** means a dependency or environment prevented execution; record the reason and do not count it as a pass.
- Mocked repository/API tests verify code contracts. They do not prove PostgreSQL, Ray, Neo4j, Weaviate, gateway, or deployed-service behavior.
- Every run must record the command, date, environment, result, and whether external services were contacted. Update this file after each functional test batch.

## Test runs completed

### TR-001 — PostgreSQL run record, worker context, auth identity, job detail, and SSE contract

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks explicitly skipped.
- **Command:**

  ```text
  .venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py tests/ingest/test_ingestion_job_read_api.py
  ```

- **Result:** 14 passed, 2 dependency warnings.
- **External services:** No live PostgreSQL, Ray, Neo4j, or Weaviate checks were performed. Persistence and read API tests use mocks; this was not a deployed-service or ingestion E2E run.
- **Covered in English:**
  1. When an ingestion run is created, it stores the actor and frozen scope, creates all 15 stable stage rows, and writes the initial audit events in the same database transaction.
  2. When a stage changes state, its stage row and event are written together; an unknown stage name is rejected before database access.
  3. When safe aggregate progress is recorded, the event contains the supplied phase and counters rather than document text or model output.
  4. When worker context is constructed, customer and ontology lineage remain frozen for the run.
  5. When authentication is resolved, a development token is distinguished from a Kong JWT, permissions are retained, missing identity is rejected, and submitter/context audit fields are available.
  6. When a user requests job details, the submitter can read them, an unrelated user receives the same 404 as a missing job, and the response contains the stage rows and current progress fields.
  7. When an authenticated user reconnects to the event stream with a prior event ID, the API resolves that job-scoped cursor and emits the next persisted event with an SSE ID and event name.
  8. The runtime OpenAPI schema exposes the authorization header for job details and event streaming.

### TR-002 — Durable-run, auth, job-list, details, and SSE focused checks

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks explicitly skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py`
- **Result:** PASS: 15 passed, 2 dependency warnings.
- **External services:** No live PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway calls. Database and service boundaries are mocked.
- **Covered in English:** Includes TR-001 cases plus a job-list test proving actor identity, admin permission, filters, offset, and limit are forwarded to the repository.

### TR-003 — Source manifest and durable metadata focused checks

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks explicitly skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py`
- **Result:** PASS: 18 passed, 2 dependency warnings.
- **External services:** No live PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway calls. Database and service boundaries remain mocked.
- **Covered in English:** Directory manifests include deterministic relative file names, sizes, SHA-256 values, and a manifest digest for supported file types; non-ingestion files are ignored; missing directories are marked as incomplete; submission metadata and source-manifest JSON are serialized into the PostgreSQL insert parameters; the durable-list SQL scopes by submitter, applies requested filters, caps page size, and passes offset parameters.

### TR-004 — Independent store outcome and run-failure checks

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks explicitly skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py`
- **Result:** PASS: 22 passed, 7 dependency/deprecation warnings.
- **External services:** None; the tests exercise pure status logic and mocked repository/API contracts.
- **Covered in English:** A Weaviate success remains visible when Neo4j fails, a validation failure marks only its affected store as failed, and a run without enriched blocks marks both stores skipped instead of claiming writes. Store failures are now critical run failures while their per-store outcome summaries remain persisted in the `stores.write` stage. A stage re-entering from `FAILED` or `RETRY_WAIT` increments its attempt and starts a fresh stage timer.

### TR-005 — Store outcomes, HITL run activity, stage attempts, and job details

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_hitl_review_repository.py tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py`
- **Result:** PASS: 30 passed, 7 dependency/deprecation warnings (including the checkpoint repository contract added in TR-006).
- **External services:** None. Repository, API, and stage behavior are tested with mocks; no live PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway was contacted.
- **Covered in English:** A stage retry starts a new attempt and timer; a status-only stage update preserves its previous progress counters; a run that completes with unresolved reviews is represented as `COMPLETED_WITH_REVIEW_PENDING`; HITL decisions and write outcomes append safe events to the associated ingestion run; resolving the final pending decision updates the run state; and store results continue to distinguish Neo4j and Weaviate outcomes.

### TR-006 — Checkpoint ledger contract

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_run_persistence.py`
- **Result:** PASS: 13 passed, 1 dependency warning.
- **External services:** None; PostgreSQL is mocked.
- **Covered in English:** Checkpoint artifacts are written atomically as typed JSON, verified by SHA-256, and bound to the immutable source manifest and frozen run context before restore. The worker saves parse/grouping output and enrichment output; a retry restores the latest supported stage artifact instead of repeating completed parsing or LLM extraction.

### TR-007 — Safe checkpoint resume contract

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py`
- **Result:** PASS: 25 passed, 2 dependency warnings.
- **External services:** None. API, PostgreSQL, and Ray calls are mocked; source digest tests use temporary local files.
- **Covered in English:** The resume API verifies the latest supported checkpoint artifact, content hash, source inventory digest, and frozen-context digest. It preserves successful stage history through the checkpoint, resets only later stages, claims one idempotent attempt, and asks Ray to restore the artifact before continuing. Writes that began publishing remain ineligible until independent store reconciliation is available. Live PostgreSQL, Ray/shared-volume behavior, failure injection, and cancellation-triggered resume remain unverified.

### TR-008 — Combined run lifecycle and checkpoint-resume regression checks

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q -s tests/ingest/test_hitl_review_repository.py tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_job_read_api.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_run_context.py tests/security/test_auth_actor_context.py`
- **Result:** PASS: 36 passed, 7 dependency/deprecation warnings.
- **External services:** None. PostgreSQL and Ray are mocked; source inventory validation uses temporary files. No graph/vector stores or LLM gateway were contacted.
- **Covered in English:** The combined suite checks run/stage persistence, HITL activity and status, independent store outcomes, actor checks, source-manifest validation, checkpoint integrity/provenance, idempotent checkpoint-resume claims, publishing-stage resume rejection, and Ray redispatch contract. It does not prove real concurrent database claims, worker cancellation, shared-volume behavior across Ray nodes, resume after partial store writes, or live failure injection.

### TR-009 — Typed stage artifact restore and checkpoint-resume contract

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_ingestion_checkpoint_artifacts.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_job_read_api.py`
- **Result:** PASS: 34 passed, 2 dependency warnings.
- **External services:** None in the passing run. The initial run without `--skip-service-checks` aborted at repository service health checks before collecting tests; it is not counted as a functional test run.
- **Covered in English:** Checkpoint files are atomically written and checksum verified; tampering is rejected. Typed enriched blocks preserve image bytes and embeddings; a worker restore reconstructs them only when source and context digests match. Resume claims retain the completed stage checkpoint, reopen downstream stage rows only, identify both the restored checkpoint stage and the next stage, and dispatch one idempotent Ray attempt. An interrupted store stage can replay customer-scoped deterministic upserts from the saved admission plan. Force rewrite is skipped on a resumed worker so recovery cannot clear customer graph/vector scope again. This run does not exercise a live worker restart or actual service recovery.

### TR-010 — Cooperative cancellation and publish-boundary arbitration

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_job_read_api.py`
- **Result:** PASS: 31 passed, 2 dependency warnings.
- **External services:** None. PostgreSQL and API behavior are mocked; no Ray worker or stores were contacted.
- **Covered in English:** A cancel request persists its actor and run status only while publishing remains pending. A queued cancel is checked atomically before the worker changes the run to RUNNING; an active worker checks after its Pass 2 or admission checkpoint and before publishing. PostgreSQL row locking decides whether cancellation or store publishing wins; when cancellation wins, the worker preserves the latest checkpoint for a later resume. Tests are mocked and do not verify live concurrent transactions or real Ray worker acknowledgement.

### TR-011 — Bounded automatic retry policy and checkpoint redispatch

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_retry_policy.py tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_job_read_api.py`
- **Result:** PASS: 36 passed, 2 dependency warnings.
- **External services:** None. PostgreSQL and Ray are mocked; no ingestion was submitted.
- **Covered in English:** Only clear timeout, throttling/service-unavailable, and temporary network errors are retryable; ontology/data errors are not. Retry is disabled by default, its configured attempt limit is capped at five, the supervisor uses the normal checkpoint verification and Ray resume dispatch path, and PostgreSQL increments an automatic-only retry counter. Candidates with exhausted budgets, invalid publishing boundaries, or completed stores are excluded or suppressed. Live database restart, timing/backoff, and actual transient Ray-worker failure tests remain outstanding.

### TR-012 — Store-unit audit ledger and report-only recovery

- **Date:** 2026-10-13
- **Environment:** local Python virtual environment; service health checks explicitly skipped.
- **Command:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_ingestion_run_persistence.py tests/ingest/test_ingestion_checkpoint_artifacts.py tests/ingest/test_ingestion_job_read_api.py`
- **Result:** PASS: 51 passed, 7 dependency/deprecation warnings.
- **External services:** The test suite uses mocked persistence and writer boundaries; this does not prove actual partial writes or cross-node shared-volume recovery. Separate read-only checks reached Neo4j (`RETURN 1`) and Weaviate (`is_ready()`). PostgreSQL was reachable, but catalog inspection found the checkpoint/retry/store-ledger schema absent; no migration was applied. The Ray dashboard endpoint was unreachable. No store data was changed and no ingestion job was submitted.
- **Covered in English:** PostgreSQL code records each Neo4j transaction batch and Weaviate’s verified bounded writer call with status, attempt, counts, and safe error code. Unknown outcomes remain visible for replay. Report-only recovery restores a checksum- and source/context-bound report-input artifact, claims a resume at `run.verify_report`, and invokes only the unified report generator. Tests assert neither writer is called during report-only recovery, and the API accepts/report-labels the `REPORT_ONLY` mode only when the checkpoint payload verifies. Live per-store failure injection, applying the migration, and Ray worker restart remain required.

### TR-013 — Live API onboarding, contextualization, jobs, and three-industry ingestion

- **Date:** 2026-10-13
- **Environment:** Current working-tree API on localhost, local Ray head, development PostgreSQL/Neo4j/Weaviate endpoints from `env`; API launched with current repository code. Three uniquely named QA customers were created. No customer was deactivated and no store was cleared.
- **Commands:**

  ```text
  .venv/bin/python scripts/evaluation/live_api_validation.py --base-url http://127.0.0.1:6004 --ray-url http://127.0.0.1:8266 --run-id live-20261013-fifthpass --include-ingestion --wait-minutes 5
  set -a; source ../env; set +a
  .venv/bin/python scripts/evaluation/verify_live_store_state.py reports/live_validation/live-20261013-fifthpass.json --base-url http://127.0.0.1:6004
  ```

- **Results:** 144 API/context/job/E2E assertions: 136 passed, 8 failed (94.44%). Six are real contract defects: missing-token checks on job routes return HTTP 400 instead of 401; missing/malformed onboarding payloads return 400 instead of 422; a blank customer name returns HTTP 500. Two failures were test expectation errors: reports are generated for failed terminal jobs too, so those report lookups correctly returned 200. Independent store verification added 24 cases: 16 passed, 8 failed (66.67%).
- **Journey outcome:** Banking completed with all 15 persisted stage rows and a report; Weaviate has one block tagged with the run batch. Neo4j has no node or relationship tagged with the run batch, so this run did not prove a run-scoped graph write. Insurance and care-management jobs failed at publishing because the worker's Weaviate writer resolved domain classes from banking-only config (`health_insurance` and `care` are rejected). Their per-store rows were left `IN_PROGRESS`; the Neo4j run-context validator also logged “KGWriter run context has not been configured.”
- **Persistence evidence:** All three API details and PostgreSQL rows retain customer, industry, domain, product line, user, auth source, and customer ontology v2. Each run has 15 stage rows, persisted events, and four checkpoints. PostgreSQL shows only a Weaviate ledger row for banking, and only an `IN_PROGRESS` Weaviate row for each failed industry. The direct batch-scoped Neo4j query returned zero nodes and zero relationships for all three.
- **Observability:** API startup reported the OTEL collector at the configured address was unreachable. This run does not pass OTEL export or Langfuse capture.
- **Full case table:** [168 live assertions with outcomes](INGESTION_LIVE_TEST_RESULTS_20261013.md). Raw API and store evidence: `reports/live_validation/live-20261013-fifthpass.json` and `reports/live_validation/live-20261013-fifthpass-store-verification.json`.

### TR-014 — Full safe repository regression suite

- **Date:** 2026-10-13
- **Command:** `.venv/bin/pytest --skip-service-checks -q --ignore=tests/ingest/test_full_ingestion_journey.py --ignore=tests/test_upsert_quick.py --ignore=tests/test_kv_cache_integration.py`
- **Result:** 545 passed, 25 failed, 3 skipped, 4 errors in 496.58 seconds.
- **Excluded with reason:** `test_full_ingestion_journey.py` temporarily suspends every active customer except its golden customer; `test_upsert_quick.py` deletes all objects for a domain before its test; `test_kv_cache_integration.py` fails test collection because it imports removed `AppConfig` and calls an obsolete `LLMClient` constructor.
- **Observed failure themes:** live ontology E2E fixtures assume `node_types` is present in canonical banking ontology; ESG tests load an absent/empty legacy `config/kbfabric.yaml`; several KV-cache/prompt expectations and parser security expectations no longer match current behavior; two direct KG writer tests do not establish a run context; XLSX row text expectations use `=` while implementation emits `:`. These failures are preserved for triage; they were not hidden by stopping after the first failure.
- **External services:** The suite contacted the development PostgreSQL and Neo4j services in its live contextualization/runtime cases. No broad customer deactivation or domain-wide cleanup test was executed. Real-store ingestion was exercised separately in TR-013.

### TR-015 — Full golden-suite and independent performance harness preparation

- **Date:** 2026-10-13
- **Scope:** Establish persistent, per-case evidence and separate functional from performance sources under `tests/functional/ingestion_golden/` and `tests/performance/ingestion/`.
- **Golden coverage plan:** `tests/functional/ingestion_golden/coverage_plan.yaml` records API families, per-industry domain/product-line and review-policy combinations, provenance assertions, fault branches, and stage/store requirements.
- **Synthetic source assets:** `tests/functional/ingestion_golden/generate_synthetic_assets.py` deterministically creates six ten-section/page-marked synthetic DOCX BRDs (two contexts per industry) and writes each content hash to `assets/synthetic_asset_manifest.json`. These are synthetic quality assets, not source customer documents.
- **Performance harness:** `tests/performance/ingestion/concurrent_user_ramp.py` runs ingestion or HITL independently at 20/30/40/50 virtual users, records one JSONL row per request plus terminal job observation, and reports p50/p90/p95/p99 and status/throughput. It refuses unsupported concurrency sizes, never clears data, and blocks higher ingestion rungs while earlier jobs remain active. `prepare_hitl_load_items.py` collects unique real pending review IDs and prepares only decisions allowed by persisted evidence; it fails closed unless the 140 unique rows required by all four rungs exist.
- **Implementation regression:** Runtime Weaviate class resolution now comes from the frozen industry's domain catalog; Neo4j spine seeding adds customer-scoped `IndustryRoot -> DomainRoot`; HITL approved writes retain industry and link approved entities to the selected product line.
- **Validation:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_industry_aware_store_routing.py tests/ingest/test_hitl_write_executor.py tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_run_context.py` — **12 passed**, 6 warnings.
- **Live performance result:** Not run yet. Requires a successful full functional golden run that produces 140 unique pending HITL records, fixed industry-specific store routing, and completed prior ingestion rungs before higher load is dispatched. Each future execution writes a unique `reports/performance/<run-id>/` directory.

## Functional coverage matrix

These are the required English scenarios. Each row stays **Planned** until the named test actually runs. The final suite should use pairwise coverage for broad configuration combinations and exhaustive cases for security, state transitions, and write-safety boundaries.

### A. Ingestion Studio submission and frozen context

| ID | English test | Required assertions | Status |
|---|---|---|---|
| SUB-01 | A user submits a server-side directory for an active customer, configured domain, and configured product line. | A run, job, and batch identity are persisted before Ray dispatch; actor, auth source, tenant/customer/domain/product line, ontology/policy versions, document nature, and review policy are frozen. The manifest records supported source file names, sizes, hashes, worker path, and an inventory digest. | Partial implementation; helper/persistence serialization covered in TR-003; no submission-to-live-DB E2E |
| SUB-02 | A user uploads files using the same context and policy choices. | The upload path persists the same audit fields plus file names, byte sizes, SHA-256 hashes, and worker input directory for later reconciliation. | Partial implementation; upload HTTP-to-live-DB/artifact-retention validation remains |
| SUB-03 | A user submits a domain or product line that is not configured for the selected customer. | The request is rejected before a Ray job or run is accepted; no cross-scope records are written. | Planned |
| SUB-04 | A user selects each of the three review policies. | `review_all`, `review_ambiguous`, and `auto_write` are persisted and applied consistently to eligible node and relationship decisions; hard scope and ontology constraints remain enforced. | Planned |
| SUB-05 | QE assets and technology artefacts are submitted with equivalent content. | Both use the same ingestion pipeline and preserve the distinct `document_nature` value in run and chunk provenance. | Planned |
| SUB-06 | A submission includes no optional project IDs or context tags, then another includes multiple values. | Both submissions succeed; supplied values are persisted, while absent values remain empty and never act as authorization evidence. | Partial implementation; live DB validation remains |
| SUB-07 | A submission requests force rewrite under all four combinations of environment switch enabled/disabled and caller permission granted/denied. | Rewrite proceeds only when both the environment switch and permission allow it; accepted rewrite is scoped to customer/domain/product line. | Planned |
| SUB-08 | A submission is made with each of the three industries and configured domains/product lines. | Industry-specific ontology and extraction policy versions are captured; invalid industry/domain/product-line combinations fail closed. | Planned |
| SUB-09 | A scheduled time is in the past, within the current delay limit, and beyond that limit. | The API reports the actual scheduling behavior and never claims durable queue acceptance for a delayed Ray worker. | Planned |

### B. Job registry, stage history, progress, and SSE

| ID | English test | Required assertions | Status |
|---|---|---|---|
| JOB-01 | An authorized user lists jobs across multiple customers and product lines. | Results come from PostgreSQL, limited to the submitter or `ingestion:jobs:read_all`; customer entitlement scoping is still pending. | Partial implementation; see TR-002; live DB validation remains |
| JOB-02 | A job traverses the full pipeline, including a route where some stages do not apply. | All 15 stages end in truthful states with start/end timestamps, attempts, counts, and explicit skip reasons. | Planned |
| JOB-03 | Parsing, LLM extraction, admission, Weaviate write, or Neo4j write fails independently. | The correct stage and run fail with a safe shared error code; completed sibling work and partial store outcomes remain visible. | Partial implementation; store outcome classifier tests pass in TR-004; pipeline failure-injection E2E remains |
| JOB-04 | A client disconnects during SSE and reconnects with `Last-Event-ID`. | It receives every later committed lifecycle event once in sequence; unknown or cross-job cursors are rejected; reconnect does not duplicate or omit committed state changes. | Planned |
| JOB-05 | Many clients subscribe to job events concurrently. | PostgreSQL connections, polling or notification load, event latency, and disconnect handling remain within agreed SLOs. | Planned |
| JOB-06 | A job details request is made by its submitter, an authorized jobs administrator, an unrelated user, and an unauthenticated caller. | Correct allow/deny behavior is consistent for details, events, logs, and reports; unauthorized callers cannot infer job existence. | Planned |
| JOB-07 | A completed run has no pending HITL rows, and another completed run has pending HITL rows. | Their statuses are respectively `COMPLETED` and `COMPLETED_WITH_REVIEW_PENDING`; reviewer actions update run-level counts and activity history. | Partial implementation; mocked status/event paths passed in TR-005; live DB and full run/HITL E2E remain |

### C. HITL evidence, decisions, and writes

| ID | English test | Required assertions | Status |
|---|---|---|---|
| HITL-01 | A node candidate is queued for review. | The UI response includes source block, document identity, full provenance, ontology context, candidate ranking/scores, and the reason for review. | Planned |
| HITL-02 | A reviewer approves a node as new. | One Neo4j node is written using the approved execution plan; action, reviewer, timestamp, scope, and result are audited. | Planned |
| HITL-03 | A reviewer reuses an existing node and supplies an alias. | The alias is persisted against the selected existing entity and future matching can reuse that decision without weakening customer/tenant scope. | Planned |
| HITL-04 | A relationship candidate has several eligible endpoint alternatives. | Alternatives come from the ranked candidate evidence persisted with the review item; the API rejects arbitrary IDs, wrong endpoint types, and invalid triples. | Planned |
| HITL-05 | A reviewer approves, rejects, changes an eligible endpoint, or leaves the item unresolved. | Each action produces the correct state transition and append-only audit event; only an approved valid plan writes to Neo4j. | Planned |
| HITL-06 | Two reviewers submit decisions for the same item at nearly the same time. | Exactly one decision claims the pending item; the other receives a conflict/current-state response; no duplicate write occurs. | Planned |
| HITL-07 | A Neo4j write fails after an approved review action and is retried. | The decision remains audited, the write state moves through failure/retry/success, and deterministic MERGE behavior prevents duplicates. | Planned |
| HITL-08 | A reviewer attempts to access or action an item outside batch/domain/product-line/customer/project entitlement. | The API denies access consistently without revealing evidence or candidate data. | Planned |
| HITL-09 | A reviewer decides a queued item and the approved write succeeds, fails, or is retried. | The ingestion job activity records decision/write outcome without source text or raw error leakage; job details show accurate decision and write counts; concurrent updates do not duplicate events or writes. | Partial implementation; event persistence paths covered with mocks in TR-005; live PostgreSQL and graph-write E2E remain |

### D. Checkpoint, retry, resume, and safe cancellation

| ID | English test | Required assertions | Status |
|---|---|---|---|
| REC-01 | A worker fails after each recoverable stage boundary. | PostgreSQL points to a durable artifact and cursor whose hash/version matches the run; a status row alone never qualifies as a checkpoint. | Partial; parse/group, extraction, and admission artifact round trips are unit-tested; live worker crash/restart remains |
| REC-02 | An authorized user resumes a failed or cancelled run from its latest valid checkpoint. | PostgreSQL claims one attempt; Ray keeps the same run/batch identity; worker restores typed output, validates source/context digests, preserves completed stage history, and enters the next stage. | Partial; mocked API/repository/worker restore contract passed in TR-009; live PostgreSQL and Ray shared-volume dispatch remain |
| REC-03 | A resume request is repeated or races with another resume request. | Only one attempt is accepted; repeated idempotency keys return the same attempt and concurrent claims cannot fork the run. | Partial implementation; repeat-key repository contract covered in TR-007; concurrent DB race test remains |
| REC-04 | A run has a changed input manifest or changed frozen ontology/policy context. | Resume is rejected and the user is directed to create a new run. | Partial implementation; changed-source hash rejection covered in TR-007; frozen-context mismatch API case and live data remain |
| REC-05 | A Weaviate write succeeds and a Neo4j write fails, then the run resumes. | Resume restores the admission plan and replays deterministic Weaviate UUID upserts and Neo4j MERGE writes; PostgreSQL shows per-store unit outcomes and uncertain units; store results are verified again. | Partial; deterministic identities and replay eligibility covered in TR-009; per-store ledger persistence is mocked in TR-012; live cross-store failure injection remains |
| REC-06 | Stores succeed but report generation fails, then the user resumes. | The worker restores a source/context-verified report-input checkpoint and reruns only unified report generation; neither store writer is called. | Partial; report-input artifact restore, report-only dispatch and resume claim are tested in TR-012; live worker restart remains |
| REC-07 | A run fails with a transient service error or a permanent policy/data error. | Only transient failures enter opt-in automatic retry; attempts are bounded and resume from the latest verified checkpoint; exhaustion or permanent failures remain visible for user action. | Partial; classifier, attempt cap, and mocked supervisor dispatch covered in TR-011; live retry/backoff and restart checks remain |
| CAN-01 | A user cancels a queued job or a running job before the publishing boundary. | Cancellation is persisted, worker stops at a safe boundary, partial work is reported, and resume is offered only when a valid checkpoint exists. | Partial implementation; mocked repository contracts pass in TR-010; queued-job handling and live worker acknowledgement remain |
| CAN-02 | Cancellation races with the atomic transition into publishing. | Exactly one transition wins; after publishing begins the API rejects cancellation and does not interrupt a store write. | Partial implementation; PostgreSQL row-locking boundary is implemented and mocked in TR-010; concurrent live race remains |
| CAN-03 | A user requests cancellation after the job is terminal or after publishing begins. | API returns the documented conflict/current state and leaves completed store writes intact. | Partial implementation; repository rejects terminal/publishing states; API integration tests remain |
| CAN-04 | A job is stopped through the legacy direct Ray stop path. | The UI and audit record do not label this as safe cancellation or claim it is resumable without a verified checkpoint. | Planned |

### E. Queue, concurrency, scale, and industry E2E

| ID | English test | Required assertions | Status |
|---|---|---|---|
| SCALE-01 | Submissions exceed available worker capacity. | Each request is either durably accepted with a queue position or explicitly rejected; no UI reports a rejected request as queued. | Planned |
| SCALE-02 | Several users ingest simultaneously within one customer, domain, and product line. | There are no accidental global/domain locks, lost updates, duplicate identities, or conflicting ontology writes; documented serialization rules are honored. | Planned |
| SCALE-03 | Fifty concurrent ingestions send Pass 1/Pass 2 calls through the configured LLM gateway. | Bounded concurrency, timeout, retry, circuit-breaker, and rate-limit behavior protect both the gateway and workers; queue/error behavior is observable. | Planned |
| SCALE-04 | One hundred users mix job submission, job monitoring, reports, and HITL actions. | Authorization remains correct; API/database/Ray load stays within agreed latency and error SLOs; SSE reconnects remain reliable. | Planned |
| E2E-01 | A fresh banking customer ingests a QE document through the real API, Ray, Weaviate, Neo4j, and report generation. | Frozen scope and all 15 stages persisted; one Weaviate block and report were verified. Neo4j produced no batch-scoped nodes/relationships, so the graph-write acceptance remains **Failed**. | Partial; TR-013 |
| E2E-02 | A fresh insurance customer completes the equivalent journey using its industry policy. | Run reached failed terminal status; worker used banking-only Weaviate domain configuration and left the write ledger in progress; no scoped Neo4j writes. | Failed; TR-013 |
| E2E-03 | A fresh healthcare customer completes the equivalent journey using its industry policy. | Run reached failed terminal status; worker used banking-only Weaviate domain configuration and left the write ledger in progress; no scoped Neo4j writes. | Failed; TR-013 |
| E2E-04 | A cross-product-line relationship is formed within one domain. | Same-batch, same-product-line, and other-product-line-in-domain candidate tiers are considered in order; cross-domain candidates remain excluded for this release. | Planned |

## Completion rule

The functional release gate is not “all tests collected” or “all mocked tests pass.” It requires all unit/API cases, required real-service E2Es, store reconciliation checks, and the agreed scale tests to pass. Every planned row must have a test reference and a recorded outcome before the final golden run and PR packaging. A failed or blocked row remains visible with its reason and owner; it is never silently counted as passed.

E2E setup and teardown must use test-owned customer/run identifiers. Never deactivate other customers or clear a shared customer, domain, or store as test setup.

### TR-016 — Functional matrix, per-test touchpoint map, and load ramp readiness

- **Date:** 2026-10-13
- **New functional runner:** `tests/functional/ingestion_golden/run_industry_policy_matrix.py` defines one fresh customer per industry, two domain/product-line selections per customer, and all three review policies per selection (18 unique ten-page-document jobs). Each assertion is separately appended to a JSONL row with HTTP method/path/status and implementation touchpoints. The journey manifest records customer/tenant, domain/product line, policy, job/run/batch, terminal status, and stage snapshots.
- **Separate performance runners:** `tests/performance/ingestion/concurrent_user_ramp.py` runs ingestion and HITL as separate test modes and ramps 20, 30, 40, 50. Ingestion waits for each rung to drain before advancing and records request latency, end-to-end latency, p50/p90/p95/p99, throughput, and status counts. HITL accepts only unique real pending review IDs with varied evidence-valid decisions. `prepare_hitl_load_items.py` can read the golden journey manifest and persists every scope-read and minimum-count result independently.
- **Per-test code touchpoints:** `scripts/evaluation/build_test_touchpoint_map.py` mapped 464 pytest functions to static imports of product modules; 0 files had parse errors and 4 tests had no direct product imports. The map labels itself static-import evidence, not a runtime call graph.
- **Focused regression:** `.venv/bin/pytest --skip-service-checks -q tests/ingest/test_industry_aware_store_routing.py tests/ingest/test_hitl_write_executor.py tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_run_context.py` — **12 passed**, 6 dependency/deprecation warnings.
- **Live API readiness run:** `live-readiness-blocked-20261013` persisted **67 independent rows**: 0 passed, 67 failed because the API was unavailable. API startup was attempted with the current code and exited during Fabric Foundry bootstrap because PostgreSQL at `192.168.68.17:5432` timed out/unavailable. Ray dashboard was reachable. Weaviate at `192.168.68.17:8080` did not respond. These are environment readiness failures, not product-level assertions, and they must not be included in the application first-pass rate.
- **Industry/policy matrix attempt:** `golden-matrix-20261013T181017-c0a199` persisted 3 independent onboarding rows (banking, insurance, healthcare): 0 passed, 3 blocked by the same unavailable local API. No new customers or jobs were created. The 18-jobs matrix runner is ready, but none of its industry journeys ran.
- **Ingestion ramp attempt:** `ingestion-ramp-readiness-blocked-20261013` attempted 20 simultaneous POST `/jobs` requests. **0 accepted, 20 failed** before any job reached Ray. The calculated p90/p95 (~14.5 ms) measures local connection refusal only; it is not a service capacity metric. Evidence: `reports/performance/ingestion-ramp-readiness-blocked-20261013/request-results.jsonl` and `summary.json`. No 30/40/50 rung was started because the 20 rung had no accepted jobs.
- **HITL ramp preparation:** the previous run's three job IDs could not be read because the local API was unavailable; 3 job-detail scope reads failed, 0 real pending items were collected, and the **140 unique item prerequisite failed**. No fabricated IDs or decisions were submitted. Evidence: `reports/performance/hitl-load-preparation-blocked-20261013.jsonl` and `.json`.
- **Product-scope gaps:** the 18-case live matrix, full SSE replay/reconnect assertions, actual cancel/resume from an in-flight checkpoint, each real HITL decision/write/retry path, and both 20/30/40/50 service load ramps remain unexecuted until PostgreSQL, Neo4j, Weaviate, and the API are reachable. The prior TR-013 store defects (industry-specific Weaviate routing, missing Neo4j run-scoped writes, and missing IndustryRoot path) remain mandatory revalidation criteria after the current fixes.

### TR-017 — Network-restored live run diagnosis and resumed golden matrix

- **Date:** 2026-10-14 (Asia/Kolkata; evidence files retain their UTC-derived run IDs).
- **Live API suite:** `reports/live_validation/live-golden-20261013-enabled.json` recorded 144 independent cases: **138 passed, 6 failed (95.83%)**. The failures were four missing-token requests returning 400 instead of 401 and two malformed/missing customer-name validation requests returning 400/500 instead of 422. Three fresh customer onboarding, contextualization, terminal status, 15-stage snapshot, SSE endpoint, and report checks were otherwise exercised.
- **Extraction evidence from the three initial jobs:** Lane 2 Pass 1 and Pass 2 did execute. Banking Pass 2 returned valid JSON with 0 entities and 0 relationships from the short QA source. Insurance returned 7 entities and 1 relationship; healthcare returned 8 entities and 0 relationships. The nonempty results reached mapping but each candidate annotation failed with `cannot access local variable 'semantic_query' where it is not associated with a value`; the error was caught and the pipeline continued with zero admissions and zero HITL rows. The old E2E status-only assertion therefore marked a no-op as completed. The live suite now treats nonempty extraction yield as part of E2E acceptance.
- **Store verification:** `reports/live_validation/live-golden-20261013-enabled-store-verification.json` recorded 27 checks: 18 passed and 9 failed. Weaviate had the scoped block; PostgreSQL stage rows/events/checkpoints were present. Neo4j did seed the configured spine (banking: 1 product line, 9 functions, 65 subfunctions; care: 1, 5, 31), but the seed used the legacy tenant label `tenant bank 1` while the frozen run context carried a UUID tenant ID, causing the tenant-scoped industry hierarchy assertion to fail. Empty/failed mapping also meant no run-scoped admitted KG writes. The detailed condition is being rechecked against the resumed matrix.
- **Code correction in progress:** `lane2_node_reconciliation.score_candidates` now assembles `semantic_query` before logging its length; `KGWriter.pre_seed_spine` now writes the frozen run-context tenant ID instead of a legacy caller value. `tests/ingest/test_industry_aware_store_routing.py` covers both regressions. Focused result after the correction: **3 passed**.
- **Resumed matrix:** `golden-matrix-20261013T182833-b9b9d1` created 3 QA customers and eight job rows. Two banking retail jobs failed at the pre-fix checkpoint boundary; the post-fix banking retail `auto_write` job completed all 15 stages with 338 entities and 78 relationships. Insurance health `review_all` completed with 342 entities, 48 relationships, and 327 pending reviews; its `review_ambiguous`, insurance property `review_all`, care `review_all`, and pharmacy `review_all` jobs also reached terminal states. The remaining mode/domain combinations are still uncovered. The manifest and summary are partial because the matrix runner had already started before its terminal-formatter correction was loaded.
- **Performance/load tests:** the real HITL ramp has completed at 20/30/40/50 concurrency; see TR-018. The ingestion 20/30/40/50 ramp remains to be run. Development actor tokens do not test Kong JWT verification or user entitlements.
- **Long-document checkpoint failure:** the first 10-page banking `review_all` job (`ingest-consumer_retail-20261013-182841-bdd9`) ran for 19 minutes. Logs show Pass 2 output and hundreds of candidate-scoring events, with no `semantic_query` mapper errors. It then failed while committing the Lane 2 checkpoint: the stage had been re-entered and was at stage attempt 2, while the checkpoint writer used whole-run attempt 1 (`Ingestion stage attempt not found: <run>/lane2.pass2/1`). The durable result had zero admitted entities and skipped publishing; this is a checkpoint-attempt mismatch, not a Lane 2 extraction failure.
- **Additional corrections:** `commit_checkpoint` now locks/reads the current persisted stage attempt and uses it for the checkpoint; `start_worker` completes `run.queued`, which the old code left PENDING (a completion snippet was unreachable after `return True`). The matrix runner's terminal detail formatter now handles its boolean assertion result rather than calling `.get()` on a boolean. Regression evidence: `reports/functional/pytest-checkpoint-attempt-20261014.jsonl`, **30 passed**.
- **Resume boundary correction:** if an upstream stage fails before any enriched blocks exist, the store stage now remains `PENDING` with reason `upstream_stage_failed` rather than being marked `SKIPPED`. This preserves the eligibility rule for resuming from an earlier valid checkpoint. Evidence: `reports/functional/pytest-resume-boundary-rerun-20261014.jsonl`, **34 passed**. An initial 34-test attempt had one assertion-only failure because the test expected the helper to discard preexisting store counters; the invariant was narrowed to status/reason and the rerun passed.
- **Second matrix job:** `ingest-consumer_retail-20261013-184826-88dc` (`review_ambiguous`) was accepted before the checkpoint-attempt, queued-stage, store-pending, and runner fixes were applied. It reproduced the same checkpoint mismatch after about 20 minutes, ended `FAILED`, left `stores.write=SKIPPED`, and exposed `resume_available=false`; `run.queued` also remained `PENDING`. The running matrix process loaded the earlier terminal formatter, so its terminal assertion rows need independent API cross-checks after the process finishes.
- **First post-fix live job:** `ingest-consumer_retail-20261013-190923-10dd` (`auto_write`) completed all 15 stages. The `lane2.pass2` checkpoint was attached to stage attempt 2 (`5b0c2d81-f1a5-4967-af2f-af8797998d6e`); extraction yielded 338 entities and 78 relationships; no mapper or attempt mismatch errors occurred. `stores.write` and `run.verify_report` succeeded, and the report endpoint returned HTTP 200. Read-only direct graph/vector provenance verification is still required.


## TR-018 — Live HITL quality, audit, graph-write, and concurrency validation

**Run:** `golden-matrix-20261013T182833-b9b9d1`; QA customer is newly onboarded insurance / health_insurance / Individual & Family Health. Source is the synthetic ten-page insurance health document. No pre-existing customer data was deleted or rewritten.

### Candidate evidence and quality findings

- Job `ingest-health_insurance-20261013-194618-2e4d` completed as `COMPLETED_WITH_REVIEW_PENDING`; detail reported 342 entities, 48 relationships, and 327 pending reviews. Its status detail shows all pipeline stages through report complete.
- The scoped API returned 327 review items: all 327 are `NODE_ADMISSION`; there are no distinct relationship review rows in this batch. Node-type distribution: Requirement 153, DataEntity 72, Control 42, System 35, Process 12, Actor 10, TestCase 3.
- Scope/provenance and source evidence are present on all 327. Stored mapping confidence is 0 for all 327; there are no node `candidate_scores` in this queue. Four extracted names are sentence-like. This is a candidate/extraction quality defect to resolve; a human review policy should not make poor extraction look acceptable.
- Relationship audit found 96 embedded triplet resolutions across 63 node-review items: all `RESOLVED`, one type-compatible endpoint candidate each, and mean recorded confidence 1.0 (86 `IMPACTS`, 10 `GOVERNED_BY`). There are zero dedicated `CONTEXT_RESOLUTION` / `UNRESOLVED_TARGET` queue rows. The sample `Req-01-01 -[IMPACTS]-> Source Provenance` points to a persisted DataEntity candidate with exact canonical/alias match. These scores show endpoint ranking evidence is available, but 12 subsequent approve writes failing on missing/out-of-scope endpoints show the recorded resolution is not sufficient proof that the endpoint is writable. Full aggregate: `hitl-candidate-audit/relationship-evidence-quality.json`.
- Sample records and full masked synthetic queue are preserved in `reports/functional/golden-matrix-20261013T182833-b9b9d1/hitl-candidate-audit/summary.json`, `candidate-samples.jsonl`, and `all-pending-review-items.jsonl`.

### Functional decisions and persistence

| Action | Sample | API/status | Graph/audit outcome |
|---|---|---|---|
| Approve as new | `rv-6e0edbb49df6`, Control “Minimum-Necessary Access” | HTTP 200, `APPROVED` | Neo4j write `SUCCEEDED`, 1 node; PostgreSQL history contains reviewer decision and write event. |
| Reject | `rv-ab34c2d50f74`, sentence-like Process extraction | HTTP 200, `REJECTED` | `NOT_REQUIRED`; PostgreSQL decision event records actor and notes. |
| Leave unresolved | `rv-29caa5b8cb28`, Requirement “Req-01-01” | HTTP 200, remains `PENDING` | No graph write; append-only event records `LEAVE_UNRESOLVED`. |

Per-action responses and audit snapshots: `hitl-candidate-audit/action-results.jsonl`.

### Separate 20/30/40/50 HITL ramp

Run `hitl-20261014-golden-20261013T182833-b9b9d1` submitted 140 unique review actions on that QA batch: 47 approve-as-new, 47 reject, 46 leave-unresolved. Each request returned HTTP 200. Reconciled GET detail/history for all 140 confirms 47 APPROVED, 47 REJECTED, 46 PENDING and an actor-matched event for each. Of 47 approvals, 35 graph writes succeeded and 12 failed; HTTP 200 alone was not sufficient to call the operation successful.

| Concurrent reviewers | HTTP requests | p90 ms | p95 ms | p99 ms | Throughput/sec | Write failures in rung |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 20 | 5509.63 | 5718.18 | 5957.26 | 3.302 | 3 |
| 30 | 30 | 5722.01 | 6104.77 | 6336.41 | 4.692 | 1 |
| 40 | 40 | 6271.57 | 6769.94 | 7203.94 | 5.458 | 1 |
| 50 | 50 | 7231.72 | 7946.77 | 8638.30 | 5.690 | 7 |

**Failure:** all 12 approved-write failures report `Relationship endpoints are missing or outside the frozen customer/domain scope`. A retry of `rv-6426b75a89d0` returned HTTP 200, incremented persisted `write_attempts` from 1 to 2, and appended a second `WRITE_EXECUTION` event, but the write still failed with the same missing endpoint. Thus the retry bookkeeping/audit path works; retry recovery and the approval plan's relationship endpoint readiness do not. See `reports/performance/hitl-20261014-golden-20261013T182833-b9b9d1/persisted-write-reconciliation-summary.json`, `persisted-write-reconciliation.jsonl`, and `retry-one-failed-write.json`. The first-pass HTTP-only summary is retained but must not be interpreted as 140 successful decisions/writes.


### Three-industry × domain/product-line × review-policy execution matrix

Each product line must be exercised with `review_all`, `review_ambiguous`, and `auto_write` using the ten-page synthetic domain asset. The matrix run below is partial; the 18 permutations are the required complete set. “Terminal” is not a pass unless extraction yield, all stage states, stores, report, and scope evidence also pass.

| Case | Industry | Domain | Product line | Policy | Current evidence/status |
|---|---|---|---|---|---|
| BANK-RET-RA | banking | consumer_retail | Retail Cards | review_all | Failed before checkpoint fix; rerun required. |
| BANK-RET-RM | banking | consumer_retail | Retail Cards | review_ambiguous | Failed before checkpoint fix; rerun required. |
| BANK-RET-AW | banking | consumer_retail | Retail Cards | auto_write | Completed all 15 stages; extraction 338 entities / 78 relationships; stores and report stages succeeded. Direct store provenance audit pending. |
| BANK-COM-RA | banking | corporate_commercial | Lending | review_all | Not run. Previous runner used nonexistent “Commercial Lending” label and skipped this domain. |
| BANK-COM-RM | banking | corporate_commercial | Lending | review_ambiguous | Not run. |
| BANK-COM-AW | banking | corporate_commercial | Lending | auto_write | Not run. |
| INS-HLTH-RA | insurance | health_insurance | Individual & Family Health | review_all | Completed; 342 entities / 48 relationships / 327 review items. Candidate and three-action evidence audited in TR-018. |
| INS-HLTH-RM | insurance | health_insurance | Individual & Family Health | review_ambiguous | Completed; 357 entities / 44 relationships; final persistence/store audit pending. |
| INS-HLTH-AW | insurance | health_insurance | Individual & Family Health | auto_write | Not run. |
| INS-PROP-RA | insurance | property_casualty | Personal Auto | review_all | Completed; 347 entities / 43 relationships / 337 review items. |
| INS-PROP-RM | insurance | property_casualty | Personal Auto | review_ambiguous | Not run. |
| INS-PROP-AW | insurance | property_casualty | Personal Auto | auto_write | Not run. |
| CARE-CARE-RA | health_insurance_care_services | care | Care Management | review_all | Completed; 364 entities / 111 relationships / 364 review items. |
| CARE-CARE-RM | health_insurance_care_services | care | Care Management | review_ambiguous | Not run. |
| CARE-CARE-AW | health_insurance_care_services | care | Care Management | auto_write | Not run. |
| CARE-PHARM-RA | health_insurance_care_services | pharmacy | Pharmacy & Pharmacy Benefits | review_all | Completed; 355 entities / 68 relationships / 337 review items. |
| CARE-PHARM-RM | health_insurance_care_services | pharmacy | Pharmacy & Pharmacy Benefits | review_ambiguous | Not run. |
| CARE-PHARM-AW | health_insurance_care_services | pharmacy | Pharmacy & Pharmacy Benefits | auto_write | Not run. |

The worktree runner now uses the configured banking `Lending` product-line name and captures its returned terminal snapshot correctly. That correction applies to future runs only; it does not alter the already-persisted first matrix result.

### Harness correction / outstanding scope

The next `concurrent_user_ramp.py` run now counts an approval as passed only when `execution_status=SUCCEEDED`; ingestion job terminal polling is concurrent, avoiding an N-times-timeout serial wait. The matrix runner now captures the returned detail object for terminal assertions and uses the configured banking corporate product line (`Lending` rather than nonexistent `Commercial Lending`). The already-running matrix did not load these corrections. Continue the missing domain/policy combinations, per-store Neo4j/Weaviate and PostgreSQL verification, relationship-review actions, cancellation/resume, SSE replay, and 20/30/40/50 ingestion ramp.

### TR-019 — Ingestion concurrency ramp (20-user rung, in progress)

Run `ingestion-20261014-golden-ramp20-retry1` submitted 20 simultaneous `POST /jobs` requests to one QA banking customer/domain/product line using the same ten-page synthetic input. At the first snapshot, 7 jobs were accepted, 11 rejected with HTTP 429, and 2 rejected with HTTP 409 because that domain already had an active ingestion. All seven accepted jobs were still in Lane 2 when sampled; no terminal or percentile result is available yet. The 429 message says `Max concurrent jobs (4) reached` while reporting 4 through 7 current running jobs, which indicates the capacity guard may race under concurrent submissions. Active snapshots also show `lane2.pass1=RUNNING` and `lane2.pass2=RUNNING` simultaneously while `current_stage_id=lane2.pass1`; check stage lifecycle sequencing in final reconciliation.

Per-request outcomes are appended at `reports/performance/ingestion-20261014-golden-ramp20-retry1/request-results.jsonl`; `rung-20-interim.json` is an interim snapshot, not a completed performance result. Do not run the 30-user rung until the accepted jobs drain and this rung has final p50/p90/p95/p99, terminal counts, and error distribution.

Worker logs sampled for the first accepted job showed Lane 2 pass-3 candidate scoring/reranking while one API read still reported `current_stage_id=lane2.pass1` and both Lane 2 rows RUNNING. A later snapshot of all seven jobs converged to `lane2.pass1=SUCCEEDED`, `lane2.pass2=RUNNING`, `current_stage_id=lane2.pass2`; treat the first read as a transition-lag observation, not a persistent contradiction. Both snapshots are retained in `rung-20-stage-consistency-snapshot.json` and `rung-20-stage-consistency-followup.json`; SSE ordering and convergence latency remain to be tested.

### TR-020 — Synthetic HITL challenge revision and live-run interruption

The initial synthetic BRDs were not a valid `review_ambiguous` acceptance fixture: they mostly described new records, so on fresh customer contexts they produced `CREATE_NEW` decisions and did not create persisted two-candidate relationship reviews. The prior `review_ambiguous` run with zero pending rows is therefore a dataset/coverage failure, not evidence that ambiguity routing works.

The first live `review_all` journey exposed two fixture defects. The extractor classified the relationship paragraph as `Narrative` and correctly rejected `INTEGRATES_WITH` for that category; it also extracted repeated QA boilerplate such as generic job/checkpoint records as business entities. The extracted systems included `Card Authorization Switch` and `Card Fraud Authorization Service`, but the run had not proved a persisted two-candidate relationship review. The source generator has now been rewritten around domain-specific business content, with one pair of distinct System inventory entries and one explicitly typed `TechnicalSystem` interface conflict. Expected behavior now lives in the test manifest, not in repeated source paragraphs. The refreshed assets and hashes require a new live validation. The banking corporate fixture uses configured product-line name `Lending`.

`run_industry_policy_matrix.py` now adds independent scoped queue assertions and paginates through all pending items. `review_all` requires persisted pending rows and at least one nonempty extracted entity name grounded in its masked source. `review_ambiguous` additionally requires at least one stored `RELATIONSHIP_REVIEW_REQUIRED` result with two or more candidates and records those candidates as evidence. The matrix may still fail these assertions if extraction, candidate retrieval, or persistence does not honor the challenge; that is an intentional product failure signal.

The local ingestion API and Ray head were initially stopped. After restarting against the configured PostgreSQL and creating a local Ray runtime, direct PostgreSQL inspection found all seven TR-019 accepted jobs still `RUNNING` at `lane2.pass2`; the former Ray cluster was no longer reachable. Their durable rows are now recorded as stranded, not terminal-state unknown. They were not modified. The local API on port 6002 and Ray Jobs API on port 8266 are now running for the corrected live matrix.

Fixture authoring checks are `tests/functional/ingestion_golden/test_synthetic_hitl_assets.py`. Default pytest preflight exited before collection because external health checks were unavailable; `.venv/bin/pytest --skip-service-checks -q tests/functional/ingestion_golden/test_synthetic_hitl_assets.py` then passed **2 tests** (one dependency warning). Independent result rows are in `reports/functional/synthetic-hitl-fixture-20261014/case-results.jsonl`; summary is `summary.json`. These checks prove intended challenge evidence is present in all six assets; they do not prove live LLM extraction or HITL routing. Live verification is pending service restoration and a fresh run ID.

### TR-021 — Worker bootstrap mismatch and corrected live matrix

The first live submission after API startup used the environment's container worker path (`/app/kbfabric-ingest/.venv/bin/python`) against a local host Ray cluster. Ray marked the job `FAILED` with exit 127 before ingestion started, while PostgreSQL incorrectly remained `SUBMITTED` with no failure detail. The matrix runner's PostgreSQL-only terminal poll would have waited the full timeout; it has now been changed to observe Ray terminal status as well, record the API/Ray mismatch, and continue to the next case.

That partial run is preserved at `reports/functional/golden-matrix-20261014T015614-a516c8/`: six setup/submission assertions passed, two Ray bootstrap/durable-status assertions failed (75% first-pass across eight recorded assertions). The launch failure is an environment mismatch; the stale `SUBMITTED` state is a product persistence defect. The API has since been restarted with `RAY_WORKER_DIR` and `KBFABRIC_INGEST_PERSIST_DIR` pointed at the local checkout and the local embedding-model path. A fresh matrix run `golden-matrix-20261014T020020-2d5b71` is currently executing: customer onboarding and Retail Cards contextualization passed, the corrected Ray worker is RUNNING, and the durable timeline has entered Lane 2 Pass 1. This run has extracted the intentional same-type System candidates and at least one relationship from the challenge text; the terminal mapping/HITL decision is still pending.

### TR-022 — Live source-grounded HITL policy validation and fixture iteration

The first `review_all` run (`ingest-consumer_retail-20261014-020024-f79b`) completed all 15 stages with 392 extracted entities, 71 relationships, and 306 pending reviews. This input predates the cleanup and repeated QA boilerplate created unrelated entity candidates, so retain it as a diagnostic run rather than the final representative fixture. The persisted sample contains source-backed Systems “Card Authorization Switch” and “Card Fraud Authorization Service.” Two explicit `APPROVE_AS_NEW` actions each succeeded with one node write; `Card Account` was rejected without a write; `Authorization` was left pending. PostgreSQL `hitl_review_event` rows and API history confirmed the actor, action, and write execution.

The cleaned ten-page asset was submitted under `review_ambiguous` (`ingest-consumer_retail-20261014-023934-72a7`). It completed all 15 stages and produced 55 entities, 11 relationships, 16 Weaviate blocks, and 8 review items. The two source-grounded System candidates each have two persisted same-type candidates and `LLM_AMBIGUOUS`; ranked evidence includes “Card Fraud Authorization Service” and “Card Authorization Switch.” An `APPROVE_REUSE` selected the exact matching System and the graph still contains exactly one customer-scoped node for that stable entity ID. A separate candidate was rejected and another was left pending; both actions were audited.

The run did **not** create a relationship endpoint review: the source alias was extracted as an ambiguous System entity, while all 11 extracted relationships resolved. This remains a failed coverage case for `RELATIONSHIP_REVIEW_REQUIRED`. The fixture was revised again to phrase the integration as a natural system-to-system REST API sentence (“X integrates with Y via REST API”); fixture authoring tests pass 2/2. A clean `review_all` run using the previous cleaned asset completed under `ingest-consumer_retail-20261014-025355-7768` with 56 entities and 8 relationships. Its admission event reports 54 queued items, but only 46 pending review rows are scoped to that batch; the other 8 review IDs belong to the previous batch (one approved, one rejected, six pending). The runner now treats this queue-count mismatch as a failure. The revised natural integration asset is currently running under `review_ambiguous` as `ingest-consumer_retail-20261014-030740-ed87`.

Two independent persistence issues were found and recorded as failures:

- `stores.write` reported domain-wide Neo4j totals as this run’s writes. For the first run it reported 732 nodes / 95 relationships despite zero admitted graph blocks; after its two HITL approvals, the batch query showed 2 System nodes and 0 relationships. The cleaned run reported 779 / 106 while exact batch-scoped Cypher returned 45 nodes / 11 relationships. `KGWriter.verify_write` filters by domain only and overwrites run counters with those global totals.
- `project_ids` and `context_tags` are stored in submission metadata, but the scheduler does not pass them to `scripts/run_ingestion_job.py`. The live request included two context tags; the 16 Weaviate objects had `context_tags=[]`.
- Reingesting an identical source under a new run reuses 8 prior HITL review IDs because the plan identity is not batch-scoped. The admission counter says 54 queued while the current batch exposes 46 rows. The prior review statuses include an approval and a rejection, yet these IDs appear in the new admission event. This is a per-run audit and queue-count reconciliation defect.

Per-test results, including the failures above, are independent rows in `reports/live_validation/golden-matrix-20261014T020020-2d5b71.jsonl`; the current result set is 28 cases, 24 pass and 4 fail (85.71%). Queue snapshots, candidate evidence, action responses, worker evidence, and direct PostgreSQL/Neo4j/Weaviate reads are under `reports/functional/golden-matrix-20261014T020020-2d5b71/`. This is still a partial banking/Retail Cards validation run; it is not the completed three-industry matrix or the 20/30/40/50 load run.

### TR-023 — Natural HITL fixture and run-scoped review identity

The regenerated ten-page banking/Retail Cards source now contains explicit business and system-integration evidence. A live `review_ambiguous` submission (`ingest-consumer_retail-20261014-030740-ed87`) completed all 15 stages and the report route. It extracted 40 entities and 7 relationships. PostgreSQL contained two genuinely ambiguous, source-grounded System candidates with ranked same-type alternatives: **Fraud Monitoring Service** (Card Fraud Authorization Service, Card Authorization Switch, and Fraud Monitoring Service) and **Card Fraud Authorization Service** (the named service and two alternatives). The candidate excerpts and scores are in `candidate-quality-review.json`.

The same run did **not** produce the intended relationship endpoint review. Pass 1 classified the mixed interface evidence as `Requirement`; Pass 2 emitted `IMPACTS` relationships from Requirements to Systems, not `INTEGRATES_WITH` from a System to one of two System candidates. Treat the fixture's relationship coverage as failed; do not count the presence of the source sentence alone as proof that the runtime exercised endpoint review.

The admission event emitted 32 review IDs, but PostgreSQL showed those IDs belonged to three batches: 6 from the earlier `review_ambiguous` run (including one already approved and one rejected), 22 from the earlier `review_all` run, and only 4 from the current batch. This identified the source defect: execution plan IDs were `exec-{block_id}-{index}`, although `hitl_review.plan_id` is globally unique. The repository's `ON CONFLICT (plan_id)` path silently returned the old review row. Plan identity now includes `ingestion_batch_id`, preserving deterministic retries within a run while separating runs. The regression and asset tests pass **3/3**. The live `review_all` verification with the fix is running separately; its result will be recorded after batch reconciliation.

On the four current-batch pending rows, `APPROVE_REUSE` selected a persisted exact same-type System candidate and returned `SUCCEEDED`; `REJECT` persisted a rejection without a graph write; `LEAVE_UNRESOLVED` remained pending. Each action has a matching history event. Evidence is in `hitl-decisions-v4.jsonl`; its synthetic scope is the isolated golden banking customer.

The original runner policy gate reported `PASS` because it only checked for an ambiguous node candidate. It missed the batch-ID collision and did not assert relationship-review coverage. The runner now reconciles all review statuses and admission IDs to the batch and fails the policy gate on a mismatch. The v4 run's corrected outcome is **FAIL** for both queue reconciliation and relationship endpoint review, despite its terminal pipeline/report success.

The master ledger now has **36 independent cases: 29 pass, 7 fail (80.56% first-pass)**. These are partial banking/Retail Cards results. The all-industry mode matrix, remaining HITL actions, relationship endpoint approval, cancellation/resume checks, and 20/30/40/50-user load rungs remain open.

### TR-024 — Completed `review_all`, live candidate/action evidence, and fixture iteration

The run-scoped `review_all` journey (`ingest-consumer_retail-20261014-032229-4593`, run `9da55fc3-7cfa-41f8-a10f-7a96b0d8fa70`) completed all 15 stages and generated its report. Admission queued 44 plans; all 44 IDs resolved to distinct review rows in that batch, and zero were written automatically. This is a passing `review_all` policy and batch-reconciliation result. Its stored stage/checkpoint/queue evidence is in `fixture-iteration-5-review-all-run-scoped/`.

The source-grounded action checks recorded on that batch were: `APPROVE_REUSE` succeeded; `REJECT` persisted with no write; `LEAVE_UNRESOLVED` left the item pending; and `APPROVE_AS_NEW` succeeded for a source-backed item. A second create-new decision with a relationship to an in-batch placeholder failed because the endpoint was not a persisted customer/domain node; its retry failed the same way. These two failures are preserved as product defects, not treated as successful actions. PostgreSQL also lacked an explicit terminal Neo4j `SKIPPED` store-ledger row when review-all admitted no KG records, and Weaviate objects had empty context tags despite tags in the request. Both are separate failures in the master ledger.

The revised `review_ambiguous` run (`ingest-consumer_retail-20261014-033850-cc59`, run `0629b5a6-2512-414b-a6bc-de624872639c`) completed all 15 stages and report generation. It extracted 42 entities and 8 relationships, queued 33 current-batch review rows, reconciled all 33 IDs, and exposed 15 source-grounded same-type System ambiguity rows. The candidate evidence is summarized in `fixture-iteration-6-review-ambiguous-strong-interface/candidate-quality-evidence-v6.json`; full persisted rows and action histories remain beside it. On this run `APPROVE_REUSE` succeeded, `REJECT` persisted, and `LEAVE_UNRESOLVED` remained pending, each with an API history event.

The fixture's endpoint-ambiguity acceptance criterion **failed**: zero review rows had `RELATIONSHIP_REVIEW_REQUIRED` with multiple System endpoint candidates. The source contained the conflict, but the extractor produced Systems without an `INTEGRATES_WITH` relationship for the alias. A first attempt to submit with the unrelated `QIBA_AUTH_TOKEN` was rejected with 401 before job creation; the run was retried with the documented development test actor, and that accepted run is counted separately.

The ten-page generator was refined to add a policy-shaped system-to-system integration statement while retaining the distinct interface conflict. The six regenerated asset files and manifest pass the authoring checks; run-scoped plan identity and fixture checks pass **3/3**. A new live banking iteration (`ingest-consumer_retail-20261014-035254-e453`, run `42617ef9-b818-477b-82c7-7fc8d8c71f6b`) was accepted and remained in `lane2.pass2` for an extended gateway call at report time. It is still running; no terminal result is claimed. The run is preserved at `fixture-iteration-7-review-ambiguous-policy-shaped/`.

The master test register is now **54 independent cases: 41 pass and 13 fail (75.93% first-pass)**. This is a partial banking/Retail Cards functional validation. Insurance and healthcare journeys, relationship endpoint decision coverage, all cancellation/resume branches, and the staged 20/30/40/50-user load runs are still open.

### TR-025 — Review policy continuation and consolidated defect register

The `review_all` iteration 5 is already terminal and passed its 15-stage, batch-scoped queue reconciliation; it does not need to be resumed. Its action evidence includes successful reuse, rejection, unresolved retention, and one successful create-new write, plus a failed create-new placeholder-endpoint write and failed retry. Continue that coverage on a fresh run only after the open defects are fixed; do not replay or mutate the completed run.

Iteration 7 (`ingest-consumer_retail-20261014-035254-e453`) completed as `COMPLETED_WITH_REVIEW_PENDING`: 15/15 stages terminal, 41 entities, 13 relationships, 32 current-batch reviews, 14 same-type entity ambiguity rows, and zero relationship endpoint review rows. All 32 admission IDs reconciled. The original policy gate was too weak and marked it PASS on entity ambiguity; an independent relationship endpoint assertion has been appended as FAIL, and the capture summary is corrected to FAIL. Pass 2 took about 61 minutes. Three scoped HITL actions have since been exercised; see TR-026. Full evidence is in `fixture-iteration-7-review-ambiguous-policy-shaped/`.

The consolidated defect IDs, priorities, repro evidence, and next steps are in [`INGESTION_GOLDEN_RUN_DEFECTS_20261014.md`](INGESTION_GOLDEN_RUN_DEFECTS_20261014.md). The key product failures are run-wide Neo4j counters, dropped context tags, missing relationship-review candidates, failed placeholder endpoint writes, and absent explicit Neo4j `SKIPPED` store status. Run-scoped plan IDs were corrected in the working tree and have 3/3 focused regression/fixture checks passing.

### TR-026 — Iteration 7 audited actions, including a triplet write attempt

Three source-grounded actions were applied to the isolated test customer and independently persisted: `APPROVE_REUSE` on review `rv-9aa359603ed8`, `REJECT` on `rv-b8a563d8c1a8`, and `LEAVE_UNRESOLVED` on `rv-8a65351ff35f`. Reject persisted as `REJECTED`; unresolved remained `PENDING`; both have history events. The reuse selected same-type System `Card Authorization Switch` for `Card Authorization Service`. Its frozen plan included an `INTEGRATES_WITH` relationship to persisted System `Digital Banking App`, providing a concrete triplet sample. The decision was recorded APPROVED, but graph execution failed with `Relationship endpoints are missing or outside the frozen customer/domain scope`. Treat this as a failed triplet write, not success.

Across the captured iteration 5–7 review items, none contained `RELATIONSHIP_REVIEW_REQUIRED` with two or more ranked endpoint candidates. Therefore endpoint-choice `APPROVE_REUSE`, relationship `REJECT`, and relationship `LEAVE_UNRESOLVED` remain untested; a node-level decision does not substitute for those tests. The action evidence, source excerpt, plan, selected endpoint, API response, and history are in `fixture-iteration-7-review-ambiguous-policy-shaped/review-actions-followup.jsonl`.

The append-only master ledger now contains **58 cases: 43 PASS, 15 FAIL (74.14%)**. One failure is the initial wrong-token harness attempt; the rest are application or acceptance-coverage failures. See `reports/live_validation/golden-matrix-20261014T020020-2d5b71.jsonl` and its `.summary.json` companion.

### TR-027 — Partial banking/insurance policy matrix and policy-mode findings

Run `golden-matrix-20261014T063312-d45e61` used fresh test customers and ten-page synthetic assets for Banking Retail Cards, Banking Lending, Insurance Individual & Family Health, and Insurance Personal Auto. Across these 12 completed jobs, each reached a terminal state with all 15 stage rows terminal, produced a report (HTTP 200), and extracted nonzero entities and relationships. The matrix ledger has **108 rows: 100 PASS, 8 FAIL (92.59%)** and correctly records `complete: false` because execution was interrupted at the user's direction to skip healthcare. The eight failures are the two `review_ambiguous` assertions on each of the four domain/product-line combinations: zero pending review rows and zero `RELATIONSHIP_REVIEW_REQUIRED` items with multiple endpoint candidates.

Four follow-up scoped reads checked the terminal `auto_write` batches. They returned **26, 26, 19, and 29** pending HITL rows respectively. Every row had mapping decision `REQUIRES_REVIEW`, plan action `QUEUE_FOR_REVIEW`, write status `NOT_STARTED`, and the reason “The primary entity identity requires review”. Treat this as a policy-path failure requiring correction: the mode did not produce the expected automatic writes for these test candidates. The additional per-batch outcomes are in `reports/live_validation/golden-matrix-20261014T063312-d45e61-followup.jsonl`.

The matrix had already submitted one Care Management `review_all` job (`ingest-care-20261014-085133-80d7`) when the user asked to skip healthcare. It completed as `COMPLETED_WITH_REVIEW_PENDING` before the matrix was interrupted. No further healthcare jobs were submitted, and this one was not fully scored by the matrix. The result is recorded in the follow-up JSONL; pharmacy and all other healthcare policy combinations remain unrun.

Representative proposed nodes and triplets from the scoped PostgreSQL HITL records, plus the earlier approved-but-failed `INTEGRATES_WITH` action, are preserved in `reports/functional/golden-matrix-20261014T063312-d45e61/hitl-node-triplet-samples.json`. These review_all plans are not evidence of Neo4j writes. The approved Banking triplet is explicitly recorded as a failed graph write due to endpoint scope validation.

No performance run was started alongside this matrix. The prior HITL performance run in TR-018 completed the separate 20/30/40/50 rungs with 140/140 HTTP actions, but 12 of its 47 approved writes failed. The ingestion 20-user rung in TR-019 remains incomplete: seven accepted jobs were stranded after the old Ray cluster disappeared, and the result has no terminal/percentile reconciliation. Do not start a new ingestion rung until those jobs and the changed policy behavior are reconciled.

### TR-028 — Policy gate correction and confirmatory test preflight

The execution gate now honors `auto_write` for uncertain mappings by selecting the highest-ranked same-type candidate and retaining the original decision and ranking evidence in plan provenance. Ambiguous relationship endpoints use the same explicit strategy. If a compatible candidate is missing, the plan remains reviewable rather than writing an invalid edge or silently omitting it. This implementation is opt-in through the already persisted run policy; hard invalid ontology triples are still rejected before policy handling.

The isolated regression set (`test_execution_gate.py`, `test_execution_gate_matrix.py`, `test_execution_plan.py`) passed **124/124** with `--skip-service-checks`; each testcase is persisted in `reports/functional/policy-gate-fix-20261014/case-results.jsonl`, with the JUnit report and raw pytest output alongside it. The live API/Ray preflight was recorded separately and is blocked: neither `127.0.0.1:6002` nor `127.0.0.1:8266` accepted a connection. No confirmatory live jobs or performance ramp are running now.

The latest `review_ambiguous` matrix run did not write the fixture's two competing System endpoint candidates into the new test customer's graph before submitting the ambiguous run. The follow-up confirmatory sequence must seed both only from exact, source-grounded `review_all` candidates using `APPROVE_AS_NEW`, verify their successful PostgreSQL audit and graph writes, then submit `review_ambiguous` and require a persisted `RELATIONSHIP_REVIEW_REQUIRED` item with at least two same-type candidates. Rerun Banking and Insurance only; healthcare remains deferred as requested.

### TR-029 — Banking and Insurance policy matrix with seeded candidates

Run `golden-matrix-20261014T091809-d15beb` completed on 2026-10-14 using fresh isolated Banking and Insurance QA customers. It exercised two Banking domains and two Insurance domains, each under `review_all`, `review_ambiguous`, and `auto_write`: 12 real API/Ray ingestion jobs and **110 independent persisted case rows**. The overall first-pass result was **105 PASS / 5 FAIL (95.45%)**. Every job reached a terminal status, returned all 15 terminal stage records, emitted nonzero entity and relationship extraction, and had an HTTP 200 report. Per-case JSONL: `reports/live_validation/golden-matrix-20261014T091809-d15beb.jsonl`; job/customer/stage manifest: `reports/functional/golden-matrix-20261014T091809-d15beb/journey-manifest.json`; summary: `reports/functional/golden-matrix-20261014T091809-d15beb/summary.json`.

`review_all` completed all four domain legs with persisted pending HITL rows and source-grounded entity candidates. The matrix approved two standalone source-grounded System candidates per domain through the review API to seed the following ambiguity run. `auto_write` completed all four domain legs with zero pending HITL items. The targeted gate regression suite remains 124/124 passing. These results confirm the in-tree auto-write decision path for this matrix but do not close run-scoped Neo4j/Weaviate reconciliation defects.

The five failures are all explicit ambiguity acceptance failures: (1) Banking Retail Cards, (2) Banking Lending, and (3) Insurance Individual & Family Health each created some pending entity-admission reviews but no `RELATIONSHIP_REVIEW_REQUIRED` item with at least two typed endpoint candidates; (4) Insurance Personal Auto `review_ambiguous` created zero pending review rows; and (5) that same Auto leg had no multi-candidate relationship review item. Candidate retrieval is not equivalent to persisted review evidence: the Banking Retail logs showed two same-type Systems with lexical scores 1.0 and 0.813559, but the final HITL records did not contain a relationship endpoint decision with ranked candidates. This confirms open DEF-03/DEF-09 and also exposes a candidate-evidence persistence gap for reviewer usability. Exact failures are the `*-TWO-CANDIDATES` rows plus `insurance-property_casualty-review_ambiguous-HITL-POLICY-RESULT` in the per-case ledger.

Representative live PostgreSQL HITL nodes and planned triplets from Banking Retail, Insurance Health, and Insurance Personal Auto, plus a persisted `LLM_AMBIGUOUS` System review, are in `reports/functional/golden-matrix-20261014T091809-d15beb/hitl-node-triplet-samples.json`. The triplets are review-plan evidence with typed endpoints; they do not establish graph-write success. A previous approved Banking `INTEGRATES_WITH` action failed endpoint scope validation, so successful relationship-write and endpoint-choice actions remain unverified.

Remaining functional scope: relationship endpoint ambiguity must be generated and persisted with ranked same-type candidates; test triplet approve/reuse, reject, and leave-unresolved, including successful graph writes and audit history; diagnose the zero-review P&C ambiguous leg; reconcile Neo4j and Weaviate writes by exact batch/customer/domain/product line; verify industry→domain→product-line→function→subfunction hierarchy and prevent cross-scope pollution; verify submitted project IDs/context tags in PostgreSQL and Weaviate; exercise SSE replay/order/reconnect; run auth/scope and validation/error permutations for all UI APIs; exercise upload, failure/retry, checkpoint resume after user cancel and infrastructure loss, and cancel-versus-publish boundary. Healthcare remains skipped by instruction.

No performance test ran concurrently with this functional matrix. The separate performance work is still pending: reconcile the prior incomplete ingestion-20 rung and stranded jobs, then execute ingestion and HITL ramps independently at 20/30/40/50 users with request and terminal outcome evidence, p50/p90/p95/p99, queue/capacity observations, and error distributions. Do not interpret the older HITL run's 140/140 HTTP responses as 140 successful writes; 12 of 47 approved graph writes failed.

### TR-030 — Live HITL action and graph-write confirmation

Follow-up actions on TR-029's fresh synthetic records exercised four review outcomes through the actual API and read the persisted item/history back. `APPROVE_REUSE` on Banking Retail's same-type System candidate returned `APPROVED` / `SUCCEEDED` with one node write. `REJECT` on an Insurance Health Requirement returned `REJECTED` / `NOT_REQUIRED` with zero writes. `LEAVE_UNRESOLVED` on an Insurance Personal Auto candidate returned `PENDING` / `NOT_REQUIRED` and recorded the reviewer action. Approving a Banking Retail `System` item whose persisted plan included `INTEGRATES_WITH` returned HTTP 200 and persisted `APPROVED`, but execution was `FAILED` with “Relationship endpoints are missing or outside the frozen customer/domain scope.” This confirms the contract distinguishes action persistence from write success and reproduces DEF-04 on the fresh run.

Exact responses and event IDs: `reports/functional/golden-matrix-20261014T091809-d15beb/hitl-decision-actions.jsonl`. Sample node/triplet plans: `reports/functional/golden-matrix-20261014T091809-d15beb/hitl-node-triplet-samples.json`. This adds node approve/reuse, reject, leave-unresolved, and a real triplet-bearing approval attempt; it still does **not** cover relationship-endpoint-choice approval/rejection because the pipeline has not persisted a `RELATIONSHIP_REVIEW_REQUIRED` item with multiple candidates.

### TR-031 — Shared statistical node and endpoint ambiguity threshold

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/ambiguity-policy-20261014-5/case-results.jsonl tests/ingest/test_shared_statistical_ambiguity_policy.py tests/ingest/test_relationship_endpoint_resolution_direction.py`
- **Result:** PASS: 9 passed, 1 dependency warning. The evidence plugin persisted 9 independent rows; summary reports 9 PASS, 0 FAIL, 0 SKIP.
- **External services:** None. No PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway calls were made.
- **Covered in English:** Node and relationship endpoints use the same lexical/semantic fusion confidence; only confidence strictly greater than `0.8` auto-resolves; exactly `0.8`, lower scores, and absent candidates go to HITL; type validity remains enforced; LLM callbacks cannot override node or relationship matching; graph-neighborhood evidence is attached to candidate review evidence when available.
- **Evidence:** `reports/functional/ambiguity-policy-20261014-5/case-results.jsonl`, `reports/functional/ambiguity-policy-20261014-5/case-results.summary.json`, and captured pytest output. Earlier iterations are retained in sibling `ambiguity-policy-20261014-*` folders.
- **Limit:** These tests establish deterministic unit behavior only. They do not prove live candidate quality, per-industry HITL row generation, saved PostgreSQL evidence, graph writes, or that the deployed reranker is available and correctly calibrated.


### TR-034 — Submission metadata survives Ray dispatch and resume

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/def02-metadata-handoff-20261014-2/case-results.jsonl tests/security/test_auth_actor_context.py tests/ingest/test_ingestion_checkpoint_artifacts.py tests/ingest/test_ingestion_metadata_contract.py tests/ingest/test_ingestion_run_persistence.py`
- **Result:** PASS: 51 passed, 0 failed, 0 skipped. Evidence summary reports 51 PASS, 0 FAIL, 0 SKIP.
- **Covered in English:** Run-record persistence retains project IDs/context tags; the shared worker-context helper attaches metadata without mutating its input for both fresh dispatch and resume; worker normalization converts arrays to the ingestion service's semicolon-delimited arguments, including empty values and legacy strings. Directory and upload paths both call this helper in the implementation.
- **Evidence:** `reports/functional/def02-metadata-handoff-20261014-2/case-results.jsonl` and `case-results.summary.json`.
- **External services:** None. No PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway calls were made. A fresh live run is still required to verify the resulting block, graph, and HITL provenance values in deployed services.


### TR-032 — Environment-configurable ambiguity cutoff

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Configuration:** `INGESTION_AUTO_RESOLUTION_CONFIDENCE_THRESHOLD=0.8` in development and test environment files. The environment loader requires the value and validates it is finite and within `[0, 1]`.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/ambiguity-policy-config-20261014-final/case-results.jsonl tests/ingest/test_shared_statistical_ambiguity_policy.py tests/ingest/test_relationship_endpoint_resolution_direction.py`
- **Result:** PASS: 11 passed, 1 dependency warning. Evidence summary reports 11 PASS, 0 FAIL, 0 SKIP.
- **Covered in English:** Both node matching and triplet endpoint matching read one shared cutoff from environment-built application config; strict greater-than behavior remains; a caller-supplied alternate cutoff changes the decision boundary; invalid out-of-range values are rejected during config parsing.
- **Evidence:** `reports/functional/ambiguity-policy-config-20261014-final/case-results.jsonl` and `reports/functional/ambiguity-policy-config-20261014/case-results.summary.json`.
- **External services:** None. No PostgreSQL, Ray, Neo4j, Weaviate, or LLM gateway calls were made.


### TR-033 — Approved HITL relationship temporary endpoint mapping

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/def04-hitl-endpoint-fix-20261014-2/case-results.jsonl tests/ingest/test_hitl_write_executor.py tests/ingest/test_hitl_api_contract.py tests/ingest/test_execution_plan.py`
- **Result:** PASS: 31 passed, 1 dependency warning. Evidence summary reports 31 PASS, 0 FAIL, 0 SKIP.
- **Covered in English:** An approved new primary node's temporary ID is translated to its deterministic customer-scoped graph ID before an outgoing triplet write; existing typed candidate validation, incoming relationship direction, API plan construction, and execution-plan behavior remain covered.
- **Evidence:** `reports/functional/def04-hitl-endpoint-fix-20261014-2/case-results.jsonl` and its summary JSON.
- **External services:** The read-only API preflight has two independently persisted BLOCKED rows in `reports/functional/def04-hitl-endpoint-fix-20261014-2/live-api-preflight.jsonl`: all probed paths failed to connect at `127.0.0.1:6002` and `192.168.68.17:6002`. No live ingestion, PostgreSQL query, or Neo4j write was performed. DEF-04 remains open pending confirmation against a service running this code.


### TR-035 — Business-first HTML report and run-scoped store evidence

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/report-redesign-20261014-5/case-results.jsonl tests/ingest/test_unified_report.py tests/ingest/test_kg_writer_run_context.py tests/ingest/test_ingestion_store_stage_status.py tests/ingest/test_ingestion_run_persistence.py`
- **Result:** PASS: 40 passed, 0 failed, 0 skipped; the evidence summary records 40 PASS, 0 FAIL, 0 SKIP.
- **Covered in English:** The report keeps the established `{job_id}.html` filename and business-impact-first layout; safely escapes user-controlled labels; summarizes run-scoped admission, HITL, stages, and store outcomes; does not mislabel global inventory as this run's writes; shows writer-reported counts when scoped read-back is unavailable; and omits legacy technical HTML dumps. Neo4j verification is scoped to the frozen customer/domain/product line/batch and unavailable verification does not overwrite writer counters. Graph path previews are constrained to the same frozen run context.
- **Evidence:** `reports/functional/report-redesign-20261014-5/case-results.jsonl` and `case-results.summary.json`.
- **External services:** None. This run used mocks/local fixtures only; it does not establish live Neo4j/Weaviate reconciliation or report rendering through the running API. A fresh golden ingestion is required to confirm those behaviors against actual stores.


### TR-036 — Attributed submission and refreshable HITL report audit

- **Date:** 2026-10-14
- **Environment:** local Python virtual environment; external service checks skipped.
- **Command:** `PYTHONPATH=scripts/evaluation .venv/bin/pytest --skip-service-checks -q -p pytest_jsonl_reporter --test-evidence-file reports/functional/report-audit-20261014-4/case-results.jsonl tests/ingest/test_unified_report.py tests/ingest/test_hitl_review_repository.py tests/ingest/test_ingestion_run_persistence.py`
- **Result:** PASS: 35 passed, 0 failed, 0 skipped. Evidence summary records 35 PASS, 0 FAIL, 0 SKIP.
- **Covered in English:** The report renders submitter, auth source, and submission time; lists append-only reviewer actions with reviewer identity, action, timestamp, decision status, and write execution outcomes; refreshes these sections from persisted state when the existing report URL is requested; and escapes reviewer/customer-provided labels. The audit query requires the exact batch, domain, and product-line scope.
- **Evidence:** `reports/functional/report-audit-20261014-4/case-results.jsonl` and `case-results.summary.json`. Updated visual sample: `reports/PREVIEW-SAMPLE-20261014.html`.
- **Limit:** Tests use local fixtures/mocks. The unchanged report route and PostgreSQL-backed refresh need live API confirmation. Existing report-route authorization remains a separate hardening item; report access should be limited to authorized customer scope before sharing actor identities broadly.

# Ingestion Studio, Job Details, and HITL UI Guidelines

**Status key:** **Implemented** means the behavior is present and covered by the cited verification. **Partial** means a route or workflow exists but misses one or more requirements listed here. **Planned** means it is target behavior with no complete implementation yet. A route existing does not imply production-ready authorization or scale behavior.

## 1. Product goals

The UI must let a business user configure an ingestion run, understand what the pipeline is doing, review proposed knowledge with evidence, and see exactly what was written. Every run uses one frozen customer, tenant, industry, domain, product line, and ontology/policy context. Domain and product line are explicitly selected from customer configuration; document content never selects them.

PostgreSQL is authoritative for run identity, audit, job/stage history, checkpoints, and HITL decisions. Ray schedules and executes workers. Neo4j is the runtime graph projection; Weaviate stores retrievable block content. SSE is a replayable delivery mechanism for persisted events, not the source of truth.

## 2. User journey and screens

### 2.1 Ingestion Studio

The submission screen should present:

1. Customer context (selected by the user's authenticated customer context when available; explicit selection may be used until that integration exists).
2. Configured domain, followed by only product lines configured for that domain/customer.
3. Document nature: **QE assets** or **Technology artefacts**. Both call the same ingestion service; this is a UI classification.
4. Documents and optional project IDs/context tags. Optional metadata is stored when supplied and is not required for ingestion.
5. Review policy: `review_all`, `review_ambiguous`, or `auto_write` (display name: “Skip review”). Explain that review policy affects eligible decision handling; it never bypasses customer/tenant/domain/product-line, endpoint-type, ontology-version, or allowed-triple constraints.
6. Force rewrite only when enabled by the environment and authorized for the caller. It is scoped to the selected customer/domain/product line and must not replace customer contextualization.
7. Submit. Return a durable run/job/batch reference and its initial status.

**Partial:** `GET /ingestion/customers`, `GET /ingestion/context`, `GET /ingestion/review-policies`, `POST /jobs`, and the upload submission endpoint exist. The submit flow validates configured scope and records submitter/frozen-context audit metadata. Force rewrite has an environment kill switch and permission check in the current working tree. Customer/project entitlement binding, a durable queue, and complete submission metadata persistence are not in place.

**Planned:** customer/project entitlement-driven defaults when the platform auth integration is ready; a durable queue/capacity response; queue position and estimated wait when the job is accepted but waiting.

### 2.2 Job list

Show only jobs the authenticated user is allowed to see. Provide filters for status, customer, domain, product line, submitter, and date range, subject to entitlement checks. Show queued/running/awaiting-review/completed/failed counts and a clear capacity message.

**Partial:** `GET /jobs` now reads the durable PostgreSQL run registry, applies submitter scoping (or `ingestion:jobs:read_all`), and supports status/customer/domain/product-line filters with pagination. Customer/project entitlement checks are not wired yet, so broad `read_all` remains an operator permission rather than tenant-aware authorization. Progress counters and Ray execution details are not joined into this list yet.

**Planned:** PostgreSQL-backed job listing with authorization filtering, queue position, queue reason, retry/resume availability, and pending HITL count. Capacity must either be accepted into a durable queue or rejected with a standard error; never imply that an unaccepted request is queued.

### 2.3 Job details

The job page contains:

- Summary: job/run/batch IDs; submitter; submitted/started/updated/completed timestamps; customer/tenant/domain/product line; frozen ontology, canonical model, and policy versions; document nature; review policy; optional project IDs/tags.
- A four-chapter, 15-stage timeline (section 3), with current progress, attempts, durations, counters, skipped reasons, and errors.
- A live activity stream and reconnect/replay behavior.
- A clear cancel boundary, resume eligibility, and store-write reconciliation state.
- HITL pending/approved/rejected/unresolved counts and a link to scoped review items.
- Per-run outputs: Weaviate blocks, Neo4j nodes/relationships, warnings, reports, logs, and trace links.

**Partial:** `GET /jobs/{job_id}` reads the durable run and 15 stage rows for the submitter or `ingestion:jobs:read_all`; it supplements the response with a local progress file. Stage attempts, per-attempt duration, HITL decision/write totals, and PostgreSQL store-unit ledger rows are included. Neo4j records transaction-batch outcomes; Weaviate records its verified bounded writer call. `UNKNOWN` means the write may have partially committed and will be reconciled by deterministic replay from the admission plan. The `stores.write` stage reports independent outcomes, preserving the sibling store result when one fails. A verified report-input checkpoint supports `REPORT_ONLY` recovery after store publishing succeeds. The SSE route replays persisted events but polls PostgreSQL every two seconds. `GET /progress/{job_id}` and `GET /jobs/{job_id}/logs` remain file/Ray-backed; logs and report routes do not enforce the same job ownership boundary. Detailed per-stage counters remain incomplete.

**Planned:** complete entitlement-filtered job snapshot; PostgreSQL-backed stage and HITL activity history; explicit run-to-HITL and run-to-report links; event transport suitable for expected concurrency.

## 3. Business-facing stage timeline

Stages are stable records for every run. A stage not applicable to a run is `SKIPPED` with a reason. Never emit success for work that did not execute.

| # | Stage ID | Chapter | Display label | Main progress evidence | Checkpoint boundary |
|---|---|---|---|---|---|
| 1 | `run.queued` | Preparing your run | Ingestion queued | queue state, position/reason | Run request and immutable ID recorded |
| 2 | `run.validated` | Preparing your run | Scope confirmed | actor, customer, domain/product line, policy validation | Validation outcome recorded; invalid requests stop here |
| 3 | `context.frozen` | Preparing your run | Knowledge context locked | ontology/canonical/policy IDs and versions | Frozen context and digest persisted |
| 4 | `dependencies.ready` | Preparing your run | AI services ready | model route and dependency check outcomes | Preflight outcome recorded |
| 5 | `documents.parsed` | Understanding your documents | Sources being read | per-file accepted/failed/unsupported counts | Each document parse artifact and source hash saved |
| 6 | `blocks.prepared` | Understanding your documents | Content organized | block counts and source provenance | Stable block manifest saved |
| 7 | `privacy.applied` | Understanding your documents | Privacy safeguards applied | scan/mask/quarantine counts | Masked block output saved; no PII in events |
| 8 | `lane2.pass1` | Understanding your documents | Document intent understood | classified/unmatched/invalid block counts | Pass 1 result saved per block |
| 9 | `lane2.pass2` | Building connected knowledge | Business knowledge extracted | entities/relationships by type, failures | Pass 2 result saved per block/category |
| 10 | `ontology.admission` | Building connected knowledge | Ontology alignment checked | admitted/rejected nodes and triples, reasons | Admission result saved per block |
| 11 | `entity.mapping` | Building connected knowledge | Knowledge matched to your graph | ranked candidates, reuse/create/review counts | Mapping decision and evidence persisted |
| 12 | `relationship.resolution` | Building connected knowledge | Connections explored | candidate counts and outcomes for the three in-domain tiers | Resolution persisted per proposed relationship |
| 13 | `admission.plan` | Building connected knowledge | Admission decisions prepared | auto decisions, review queue, unresolved counts | Execution plan and HITL rows committed |
| 14 | `stores.write` | Publishing and reporting | Knowledge published | independent Weaviate and Neo4j child states | Per-store write unit ledger committed; uncertain units replay from the admission plan |
| 15 | `run.verify_report` | Publishing and reporting | Run evidence ready | reconciled counts, warnings, report links | Report-input artifact saved after store success; report-only recovery skips store writers |

Stage 14 must show separate Weaviate and Neo4j outcomes. The stores can run in parallel and are not one atomic transaction. A partial result must be visible and reconciled before reporting success or resuming.

HITL rows are currently written to a separate review store and reviewers act after extraction. Run status does not yet calculate pending review counts, and reviewer decisions/write outcomes are not appended to `ingestion_run_event`; the `COMPLETED_WITH_REVIEW_PENDING` lifecycle remains planned.

## 4. Status model and transitions

### Job statuses

- `SUBMITTED`: request/audit record accepted.
- `QUEUED`: accepted and waiting for worker capacity; include `queue_reason` and position when available.
- `VALIDATING`: scope and frozen context checks are running.
- `RUNNING`: a worker is executing stages; include `current_stage_id`.
- `RETRY_WAIT`: automatic transient retry is scheduled; include retry time and attempt.
- `CANCEL_REQUESTED`: cooperative cancellation accepted before publishing; worker is stopping at a safe boundary.
- `CANCELLED`: stopped before publishing; checkpoint remains available if safe.
- `COMPLETED`: pipeline completed and no pending HITL action blocks the stated outcome.
- `COMPLETED_WITH_REVIEW_PENDING`: processing completed; one or more review items await action.
- `FAILED`: no automatic retry remains or a non-retryable failure occurred; include error code, retryability, checkpoint, and partial-write reconciliation data.

Warnings and partial-write details are fields, not hidden inside an ambiguous success label. A retry/resume creates a new attempt in the same run lineage and retains the original batch/provenance. A changed document set or frozen context requires a new run.

### Stage statuses

`PENDING`, `RUNNING`, `RETRY_WAIT`, `SUCCEEDED`, `SKIPPED`, `FAILED`. A retry creates an attempt record; it does not erase prior errors. Job and stage transitions use compare-and-set/transactional guards so cancellation, resume, and publishing cannot race into conflicting states.

## 5. UI operations and API contract

| UI operation | API | Availability |
|---|---|---|
| Load configured customer context | `GET /ingestion/customers`; `GET /ingestion/context?customer_id=...` | Partial; configuration is available, but caller entitlement is not enforced |
| Load review policy choices | `GET /ingestion/review-policies` | Implemented for setup-time policy choices |
| Submit server-side input | `POST /jobs` | Partial; records frozen context and dispatches to Ray, but has no durable queue or complete input manifest |
| Submit uploaded files | Upload submission route | Partial; dispatches to Ray, but retention/checkpoint manifest is not durable |
| List jobs | `GET /jobs` | Partial; PostgreSQL-backed and submitter/read_all scoped, but not customer/project entitlement-filtered |
| Load job details | `GET /jobs/{job_id}` | Partial; durable base snapshot with 15 stage rows, but not all promised status, HITL, store, and checkpoint fields |
| Load progress snapshot | `GET /progress/{job_id}` | Partial; local progress-file snapshot, not durable stage history |
| Stream/replay activity | `GET /jobs/{job_id}/events` | Partial; PostgreSQL replay exists, but uses polling and lacks scale/load validation |
| Cancel before publishing | `POST /jobs/{job_id}/cancel` | Planned safe operation; current `DELETE /jobs/{job_id}` directly stops Ray |
| Resume eligible run | `POST /jobs/{job_id}/resume` | Partial; restores a verified parse, extraction, or admission artifact; interrupted stores replay idempotently from the admission plan. A completed store stage still needs report-only recovery. |
| Load job logs | `GET /jobs/{job_id}/logs` | Partial; Ray logs exist, but caller/job ownership is not checked |
| List/get HITL items and history | `GET /hitl/reviews`, `GET /hitl/reviews/{review_id}`, `GET /hitl/reviews/{review_id}/history` | Partial; batch/domain/product-line filters and auth identity exist, but customer/project entitlement is not enforced |
| Record HITL decision | `POST /hitl/reviews/{review_id}/decision` | Partial; decision audit and approved-plan write exist, but run-level event/status integration is missing |
| Retry failed approved HITL write | `POST /hitl/reviews/{review_id}/execute` | Partial; approved write retry exists, but is not linked into the run activity ledger |
| Open report | `GET /reports/get-report/{job_id}` | Partial; existing path is retained. On download, the report refreshes submitter/auth context and batch/domain/product-line-scoped HITL decision/write history from PostgreSQL. Report-route job ownership/tenant authorization remains to be enforced. |

The current `DELETE /jobs/{job_id}` calls Ray `stop_job` directly. It can terminate a worker mid-stage or mid-write, so it must not be treated as the safe UI cancellation contract.

### Target submission response

Return `job_id`, `run_id`, `ingestion_batch_id`, `status`, submitted actor/timestamp, frozen scope/version references, and links for `details`, `events`, `cancel` (when eligible), and reports/HITL when available. A queue acceptance includes queue reason/position; a rejected capacity response contains a standard error envelope and `Retry-After` where applicable.

### Target job snapshot

Return job metadata, top-level status, `current_stage_id`, weighted progress/counters, all 15 stage snapshots with timestamps/attempts, `resume_available`, `cancel_available`, HITL counts, store reconciliation state, warnings/errors, and report/log links. Do not return prompt text, raw model reasoning, or source PII in the operational snapshot.

## 6. Target SSE event envelope

SSE is backed by persisted PostgreSQL events. The client loads a REST snapshot, subscribes to SSE, and reconnects using the last event ID. Event persistence precedes publication so disconnects do not lose history.

```json
{
  "schema_version": 1,
  "event_id": "evt_<opaque-id>",
  "sequence": 42,
  "run_id": "run_<id>",
  "job_id": "ingest_<id>",
  "ingestion_batch_id": "batch_<id>",
  "stage_id": "lane2.pass2",
  "stage_label": "Business knowledge extracted",
  "event_type": "stage.progress",
  "status": "RUNNING",
  "occurred_at": "2026-10-13T12:00:00Z",
  "attempt": 1,
  "progress": {"completed": 28, "total": 40, "unit": "blocks", "counts": {"entities": 72, "relationships": 14}},
  "checkpoint": {"available": true, "checkpoint_id": "cp_<id>", "resume_from": "lane2.pass2"},
  "error": null,
  "trace_id": "<trace-id>"
}
```

Event types include `job.status_changed`, `stage.started`, `stage.progress`, `stage.completed`, `stage.skipped`, `stage.failed`, `stage.retry_scheduled`, `checkpoint.committed`, `hitl.queue_changed`, `store.write_status_changed`, and `report.ready`. Progress events may be coalesced; lifecycle and checkpoint events must not be dropped. Never stream prompts, raw model responses, hidden chain-of-thought, or unmasked source content.

## 7. Checkpoint and resume policy

A checkpoint references durable output and a cursor; a status update by itself is not a checkpoint. The worker now atomically retains typed JSON artifacts on the shared persistence volume and PostgreSQL indexes their hashes, input/context digests, cursors, and events at `blocks.prepared`, `lane2.pass2`, and `admission.plan`.

- On submit: persist request, actor, scope, frozen context, and version digest.
- Parse: checkpoint each completed document and its source hash/artifact.
- Prepare blocks: persist block manifest and provenance.
- PII, Pass 1, Pass 2, admission: persist completed per-block results so completed LLM work is not repeated.
- Mapping/resolution: persist completed decision units with ranked candidate/evidence snapshots.
- Before stores: commit immutable execution plan and HITL review rows.
- Stores: record Weaviate and Neo4j results separately at object/bounded-batch granularity; resume reconciles completed writes idempotently.
- Reports: persist final verification and report manifest.

Checkpoint frequency is at each completed recoverable unit (document for parsing, block for LLM output, decision unit for mapping/resolution, and idempotent object/bounded batch for writes). Progress SSE can be more frequent and coalesced. Database transactions/pooling and bounded batches must be load-tested to avoid creating excessive per-block connection overhead.

The implemented user action is **Resume run**. It is allowed for a failed or cancelled run only when a supported checkpoint artifact verifies against unchanged sources and frozen context. The API claims an idempotent run attempt in PostgreSQL, preserves completed stage history through the checkpoint, and submits a distinct Ray execution using the frozen customer/ontology/policy context. The worker restores parsed/grouped blocks, enriched blocks, or the completed admission plan and starts at the next stage. If publishing was interrupted after the admission checkpoint, both stores are replayed using deterministic upserts; the response identifies this as `IDEMPOTENT_STORE_REPLAY`. If publishing completed and the final unified report failed, a report-input artifact enables `REPORT_ONLY` recovery; neither store writer is invoked. Force rewrite is never repeated during resume. Changed/missing inputs, unavailable frozen context, or corrupt artifacts are rejected.

Automatic retry is opt-in through `INGESTION_AUTO_RETRY_ENABLED` (default `false`). When enabled, the service retries only clearly classified timeouts, throttling/service-unavailable responses, and temporary network failures. `INGESTION_AUTO_RETRY_MAX_ATTEMPTS` defaults to 2 and is capped at 5; retry delay grows from 5 seconds to at most 60 seconds. PostgreSQL stores the retry count and error classification, and the API supervisor reuses the checkpoint-validated resume path. Job details expose `automatic_retry_pending` and `auto_retry_count`, plus per-store write units and `store_reconciliation_required`. Neo4j transaction batches are separately tracked; Weaviate is currently tracked as one bounded writer call rather than individual objects. Upload sources are retained for the configured `KBFABRIC_RESUME_SOURCE_RETENTION_HOURS` window (default 72 hours) after a failed/cancelled run; directory sources remain customer-managed and must remain mounted and unchanged. Live multi-node retry/shared-volume verification remains future work. A human cancellation is explicit; a later resume is a separate user action.

## 8. Safe cancel policy

The UI shows **Cancel ingestion** while the run is active and `cancel_available=true`. `POST /jobs/{job_id}/cancel` persists the authenticated actor and a cooperative cancel flag. The current worker observes cancellation after Pass 2 or admission has reached a durable checkpoint, then stops before publishing; work in the current stage may finish first. Transition into Stage 14 publishing and cancellation contend under a PostgreSQL row lock; if publishing wins, the API returns `RESOURCE_CONFLICT` and does not interrupt an in-flight store write. The UI hides/disables cancel once Stage 14 starts. Queued-job immediate cancellation and live worker acknowledgement still need verification. After processing completes, HITL withdrawal is a separate audited action, not job cancellation.

## 9. Errors, authorization, and audit

Use the shared platform error catalogue first: `INVALID_REQUEST`, `VALIDATION_ERROR`, `AUTHORIZATION_DENIED`/`INSUFFICIENT_PERMISSIONS`, `RESOURCE_CONFLICT`, `RATE_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `LLM_SERVICE_UNAVAILABLE`, `LLM_REQUEST_FAILED`, `LLM_TIMEOUT`, `DEPENDENCY_FAILURE`, `FILE_PROCESSING_FAILED`, and `INTERNAL_SERVER_ERROR` as applicable. Add an ingestion-specific code only when no shared code expresses the condition (for example, an unrecoverable checkpoint restore failure), and register its HTTP status and user-facing message consistently.

Every job and review action records authenticated actor, auth source, timestamp, scope, action, and result. Authorize every job, event, report, and HITL lookup against customer/domain/product-line scope. Project-level filtering depends on the forthcoming user/project entitlement integration; persist project IDs now, but do not treat free-text project metadata as an authorization check. Force rewrite requires both the environment kill switch and explicit permission.

## 10. Golden and scale acceptance criteria

The final E2E/scalability pack should verify:

1. New customer setup through frozen run context and explicitly selected product line.
2. All 15 stages produce truthful states, including conditional skips and per-stage counters.
3. SSE initial snapshot, event replay after disconnect, ordering, and no event loss for state transitions.
4. Retry and resume from injected worker, gateway, database, parse, and store failures; no duplicate writes and no repeated completed extraction calls.
5. Cancel is accepted before publishing, rejected once publishing starts, and never leaves an unreported partial write.
6. HITL node/relationship evidence, scoped decisions, actor audit, writes, and retry outcomes appear in job history.
7. Per-run Neo4j/Weaviate reconciliation uses batch provenance and distinguishes prior global data.
8. Capacity behavior with concurrent jobs/users: durable queue or explicit rejection, bounded gateway calls, fair tenant limits, and no fail-open over-admission.
9. A 50-concurrent-ingestion scenario and a 100-user mixed-feature scenario, including injected 429/503/timeouts and SSE reconnects, with defined throughput, latency, and error-rate SLOs.

This document describes current behavior and the target UI/API contract. Items marked **Partial** or **Planned** must not be treated as production-ready or already implemented.

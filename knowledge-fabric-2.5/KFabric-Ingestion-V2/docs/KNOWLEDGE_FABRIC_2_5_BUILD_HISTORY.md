# Knowledge Fabric 2.5 ingestion build history

## Purpose and scope

This note records the ingestion capabilities assembled in `KFabric-Ingestion-V2` during the recent two-week build and the evidence available on 15 October 2026. It is a handover record for engineering review and local verification. It is not a claim that every production acceptance gate has passed.

The build extends the existing ingestion service with customer-scoped onboarding and context, durable job lifecycle behavior, extraction and knowledge-store publication, human review, and user-facing reports. The new 2.5 repository is intended to be tested independently. The running service and its deployment are to remain unchanged during that verification.

## Delivered capability areas

### Customer and ontology setup

- Customer workspace creation and active-customer lookup.
- Customer-specific configured domains and product lines, backed by the customer ontology/context APIs.
- Industry and ontology metadata discovery for setup flows.
- Customer-root graph structure in which customer roots own domain roots and product-line roots; the agreed graph shape excludes an industry root.
- Extraction policies and ontology definitions for banking, health/care, and insurance domains.

### Ingestion jobs and durable progress

- Server-directory and multipart upload submission paths.
- Job listing and details backed by durable records, with stage and progress information.
- Persisted job events available for replay, operational logs, and report lookup.
- Safe cancellation before publishing, retry/resume paths using persisted checkpoints, and stage/store outcome reporting.
- Store publication to Weaviate and Neo4j with customer/domain/product-line scope metadata.

Some lifecycle behaviors are implemented but still need live fault-injection, restart, and authorization coverage. See [`KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md`](KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md).

### Extraction, reconciliation, and review

- Document parsing and extraction across supported document formats, with industry-aware ontology/policy context.
- Candidate retrieval and entity reconciliation before graph admission.
- Review-all and alternate HITL policy paths, ranked choices/evidence, reviewer decisions, write execution/retry, and append-only action history.
- Human review of uncertain entity identity before the associated knowledge-graph write is allowed.

### Reporting and UI integration

- Job progress and stage detail endpoints, job events, logs, review APIs, and unified HTML report generation.
- OpenAPI YAML and UI guidance for the Ingestion Studio API and user journeys.
- A generated handbook document exists in the workspace; compare it against the live OpenAPI document when preparing a deployment.

### Testing and operational evidence

- Unit and contract tests for customer APIs, ingestion stages, store routing, checkpoint/resume boundaries, HITL actions, reports, and security actor context.
- Synthetic, reproducible industry fixtures and functional journey runners.
- Route-level live API checks, store read-back checks, HTML report samples, and JMeter smoke evidence.
- Performance and functional evidence has been retained under `reports/`; select representative artifacts for the clean 2.5 handover rather than shipping every transient run file.

## Validation snapshot (15 October 2026)

The local ingestion API and local Ray were used for submissions. PostgreSQL, Weaviate, and Neo4j were the configured development services. No shared store was cleared during this validation.

- Route checks recorded 123/129 assertions passing (95.35%). This includes known authorization and input-validation failures; see the defects note for exact findings.
- Banking corporate/lending completed with review pending: 2 files, 243 blocks, 442 mapped entities, and 62 relationships. The report was generated.
- A small Banking wealth-management upload completed with 1 block, 10 mapped entities, and 10 review items.
- Three larger image-bearing Banking DOCX runs failed during JSON checkpoint serialization with invalid UTF-8; no blocks were committed for those runs.
- Health care management completed with 41 entities and 8 relationships. Its scoped HITL decision, graph write, and review history were verified.
- Insurance individual/family health completed with 36 entities and 2 relationships. Its scoped HITL decision, graph write, and review history were verified.
- Read-only graph checks found customer roots with domain and product-line roots and no industry-root node. Sampled Weaviate objects carried customer-root, domain, and product-line scope; sampled `industry_id` values were null and need confirmation.
- A bounded JMeter smoke run used five virtual users and one loop (45 requests). Aggregate p50 was 2.505 seconds and p95 was 20.468 seconds; customer-list p95 was 26.497 seconds. One review-policy request reset its TCP connection. This is a latency warning, not a capacity benchmark. Successful upload, HITL write, and graph-write load scenarios were not covered.

Detailed run evidence: [`reports/live_validation/TEST_RUN_SUMMARY-20261015.md`](../reports/live_validation/TEST_RUN_SUMMARY-20261015.md), route evidence in `reports/live_validation/api-local-ray-20261015-b.json`, functional reports in `reports/functional/`, and JMeter output in `reports/performance/jmeter-20261015/`.

## Intended handover boundary

The 2.5 repo should contain runnable service code, required configuration and ontology files, focused tests and fixtures, setup/deployment instructions, and a small set of traceable validation reports. It must not contain live credentials, developer virtual environments, dependency caches, or transient runtime data. The developer's local Neo4j and Weaviate are the target for isolated verification; document the needed PostgreSQL and Ray endpoints alongside them.

The current deployment is a separate system and remains unchanged. Do not point a local 2.5 service at shared development stores unless the test is explicitly designed around dedicated QA customer and data scopes.

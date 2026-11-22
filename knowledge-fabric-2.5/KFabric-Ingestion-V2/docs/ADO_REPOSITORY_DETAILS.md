# ADO repository details — Knowledge Fabric 2.5 ingestion handoff

Use this note to populate the Azure DevOps repository's summary and handoff details. It describes the staged code as of 15 October 2026. It does not represent a production approval.

## Repository metadata

| ADO field | Proposed value |
|---|---|
| Repository name | `Knowledge Fabric 2.5 ingestion handoff` |
| Description | Knowledge Fabric 2.5 document ingestion service: customer-scoped ingestion, ontology-guided extraction, Ray job execution, PostgreSQL-backed lifecycle and HITL, Neo4j/Weaviate publication, API/UI contracts, tests, and developer handover. Validation candidate; known defects documented. |
| Default branch | `main` |
| Visibility | Follow the QiBa project's access policy; grant only the developer/team members who need to validate this candidate. |
| README | Repository root `README.md` |
| Source lineage | Curated copy of local `KFabric-Ingestion-V2`; existing V2 repository and deployment are separate and must remain unchanged. |

## Purpose

Provide the developer with an isolated checkout of the Knowledge Fabric 2.5 ingestion work for local review and validation against their own services. This lets the team inspect the new customer/context APIs, durable job lifecycle, extraction and HITL work separately from the currently deployed V2 service.

## Functional scope

- Customer workspace, domain, product-line, industry and ontology setup APIs.
- Directory-based and multipart upload job submission.
- Ray-backed job processing with durable PostgreSQL records, stage/progress snapshots, persisted events, cancellation and checkpoint-based resume paths.
- Document parsing, PII handling, ontology- and extraction-policy-guided entity/relationship extraction, candidate retrieval and reconciliation.
- HITL review listing/detail/history, reviewer decisions, graph write execution and eligible retry paths.
- Knowledge publication to Weaviate and Neo4j with customer/domain/product-line scope.
- HTML reports, operational logs, metrics, FastAPI OpenAPI output and Ingestion Studio UI guidance.
- Banking, health/care and insurance configuration, synthetic fixtures, maintained test suites and JMeter assets.

The planned durable queue and queue-at-capacity behavior are not part of the implemented scope in this candidate; the `/jobs/queue` endpoint was not available during validation.

## Component and dependency map

| Component | Role | Required environment/dependency |
|---|---|---|
| FastAPI ingestion service | Bootstrap, customer/context, jobs, HITL, report and health APIs | Python dependencies in `KFabric-Ingestion-V2/requirements.txt` and `pyproject.toml` |
| Ray | Executes ingestion jobs and exposes job submission/runtime | Ray cluster/dashboard endpoint; separate Ray job dependency files and `Dockerfile.ray-ingest` |
| PostgreSQL | Durable customer, job, review and lifecycle state | PostgreSQL URI/host, database, user and credentials in local environment configuration |
| Weaviate | Vector/chunk and embedding-backed knowledge storage | Weaviate endpoint/API key and compatible schema |
| Neo4j | Knowledge graph nodes and relationships | Neo4j Bolt endpoint and credentials |
| Ollama or LiteLLM model router | Text and vision model calls | Model service endpoint, model names, router URLs and token as applicable |
| Embedding and reranker models | Local semantic embedding and candidate ranking | Weight files are not committed; developer supplies the licensed models in `custom_ml/.../models/` |
| Prometheus/OpenTelemetry | Metrics and optional telemetry | Optional collector/export settings |

The `env.example` and `env.test.example` files enumerate application variable names with blank values. Configure them locally. Do not add actual secrets, copied developer env files, or private infrastructure credentials to ADO.

## API documentation and UI handoff

- At runtime, use `http(s)://<ingestion-api-host>:<configured-port>/docs` for Swagger UI and `/openapi.json` for the generated live contract.
- Source OpenAPI contract: `KFabric-Ingestion-V2/docs/openapi/ingestion-studio-job-hitl.openapi.yaml`.
- UI/API handbook: `KFabric-Ingestion-V2/docs/INGESTION_STUDIO_API_AND_UI_HANDBOOK.docx`.
- UI interaction guidance: `KFabric-Ingestion-V2/docs/INGESTION_STUDIO_JOB_HITL_UI_GUIDELINES.md`.
- The live generated contract must be compared with the source YAML before a deployment is presented as complete.

## Validation status and evidence limits

The build-history note records route checks at 123/129 assertions passing (95.35%), partial successful ingestion journeys across Banking, Health and Insurance, selected HITL write/history checks, scoped graph/vector read-backs, and a low-volume JMeter smoke run. The five-user/one-loop JMeter result is a baseline only; it does not establish capacity. Several image-bearing Banking DOCX jobs failed checkpoint serialization. The staged checkout's small Docker E2E attempt was blocked by a local Docker storage I/O/read-only filesystem failure.

The curated repo does not currently include all raw live reports referenced in the build-history and defects notes. Re-run validations from the developer's environment and attach reviewed, sanitized reports before using them as release evidence.

## Defects and release gates

P1 items documented in `KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md`:

1. Enforce actor/customer authorization for job data, logs, progress, events, reviews and reports; complete the customer-to-user membership/role model.
2. Validate blank customer names at the API boundary and return a stable client error.
3. Make checkpoint persistence safe for binary/image-bearing document content and prove save/reload/resume.

Further gates include API status-code alignment, confirmation of industry metadata in Weaviate, reconciliation of runtime OpenAPI and source contract, isolated journey checks for cancellation/resume and write failure/retry, and JMeter workloads for bootstrap, upload, HITL decisions and graph publication. The queue remains deferred/unimplemented.

## Recommended ADO tags

`knowledge-fabric`, `ingestion`, `2.5`, `developer-validation`, `ray`, `hitl`, `neo4j`, `weaviate`

## Handoff checklist

- [ ] Ensure repo permissions match the QiBa project policy.
- [ ] Keep the existing `KBFabricIngestion` repository and service deployment unchanged.
- [ ] Developer creates local environment files from the blank templates and supplies the model directories under their license terms.
- [ ] Developer configures isolated PostgreSQL, Ray, Weaviate, Neo4j and model endpoints.
- [ ] Developer follows the documented clean-checkout install path and reports dependency/version issues.
- [ ] Resolve P1 authorization, onboarding validation and checkpoint serialization before production consideration.
- [ ] Record fresh API, three-industry functional, store read-back, HITL alternate-path and performance results in the repo.

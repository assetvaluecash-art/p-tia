# Knowledge Fabric 2.5 — Ingestion Handoff

Developer validation repository for the Knowledge Fabric document ingestion service. This repository packages the 2.5 ingestion work from `KFabric-Ingestion-V2` as a separate checkout so it can be reviewed and tested without changing the existing V2 repository or deployment.

> **Readiness:** This is a developer validation candidate, not a production release. API and industry journey checks found open defects. The local end-to-end smoke test could not be completed because Docker storage failed. Review the defect list before deploying or using shared data stores.

## Repository contents

| Area | Location | Purpose |
|---|---|---|
| FastAPI services, workers, extraction, HITL and storage writers | [`KFabric-Ingestion-V2/kbfabric_ingest/`](KFabric-Ingestion-V2/kbfabric_ingest/) | Ingestion application source |
| Ontologies, industry policies and runtime configuration | [`KFabric-Ingestion-V2/config/`](KFabric-Ingestion-V2/config/) | Banking, health/care and insurance configuration |
| Maintained tests and synthetic fixtures | [`KFabric-Ingestion-V2/tests/`](KFabric-Ingestion-V2/tests/) | Unit, API, security, functional and performance checks |
| API contract and UI guidance | [`KFabric-Ingestion-V2/docs/openapi/`](KFabric-Ingestion-V2/docs/openapi/) and [`KFabric-Ingestion-V2/docs/INGESTION_STUDIO_API_AND_UI_HANDBOOK.docx`](KFabric-Ingestion-V2/docs/INGESTION_STUDIO_API_AND_UI_HANDBOOK.docx) | Ingestion Studio integration |
| Build, defect and packaging notes | [`KFabric-Ingestion-V2/docs/`](KFabric-Ingestion-V2/docs/) | Handover and known limitations |
| Environment templates | [`env.example`](env.example), [`env.test.example`](env.test.example) | Blank templates; fill locally and keep populated files out of Git |

The embedding and reranker model weights are omitted at the owner's request. Before model-backed validation, copy the matching model directories into `KFabric-Ingestion-V2/custom_ml/embeddings/models/` and `KFabric-Ingestion-V2/custom_ml/reranking/models/`. The corresponding source clients are included. Keep model files and any redistributed copy aligned with their upstream licenses.

## What the service does

The service accepts documents from an uploaded file set or a server-side input directory, parses and splits content, applies PII handling, extracts entities and relationships with domain ontology/policy context, reconciles candidate entities, and publishes scoped knowledge to PostgreSQL, Weaviate, and Neo4j. It exposes durable job status/progress, event and log views, report retrieval, and human-in-the-loop (HITL) review and write actions. Ray runs ingestion jobs; Ollama or the configured LiteLLM model router supplies model calls.

Customer, domain and product-line context are carried through ingestion and store writes. The agreed graph root shape is customer root → domain root → product-line root; industry is context metadata, not a graph root.

## First checkout

1. Install a supported Python version and the system packages documented in [`KFabric-Ingestion-V2/README.md`](KFabric-Ingestion-V2/README.md) and the dependency files. The Ray job environment is separately described by `ray_job_requirements.txt` and `ray_job_requirements.lock`.
2. Copy `env.example` to `env` for a local setup, or `env.test.example` to `env.test` and set `KBF_ENV=test`. Fill in service URLs, credentials, model settings and paths for your own isolated environment. Do not commit populated `env` files.
3. Place the embedding and reranker model directories in the paths above, or configure an approved external model source if the implementation supports it.
4. Start PostgreSQL, Weaviate, Neo4j, Ray and the configured model service. Use dedicated local/test data and customer scopes.
5. Follow the service startup instructions in [`KFabric-Ingestion-V2/README.md`](KFabric-Ingestion-V2/README.md). The FastAPI OpenAPI UI is served at `/docs` on the configured ingestion API host and port; `/openapi.json` provides the generated contract.

Required variables are enumerated in `env.example`. Templates intentionally contain blank values and are not ready to run until configured. Confirm dependency versions and service compatibility with the team's target environment before a clean install.

## Validation record

The 15 October 2026 handover notes summarize the checks performed against the local API/Ray setup and configured development stores. Route assertions were 123/129 passing (95.35%). Banking, Health and Insurance journeys reached varied completion states; some HITL writes and store read-backs passed. Three larger image-bearing Banking DOCX jobs failed during checkpoint serialization. A five-user, one-loop JMeter smoke run was not a capacity benchmark and did not cover successful upload, HITL write or graph-write load. The local Docker-based smoke test for this staged checkout did not run successfully because Docker storage returned an I/O/read-only filesystem error.

The summary includes known shortcomings and is not a substitute for running the checks from a clean developer checkout. See [`KNOWLEDGE_FABRIC_2_5_BUILD_HISTORY.md`](KFabric-Ingestion-V2/docs/KNOWLEDGE_FABRIC_2_5_BUILD_HISTORY.md) and [`KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md`](KFabric-Ingestion-V2/docs/KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md). Some raw run reports referenced by the notes are not included in this curated handoff.

## Known priority items

- **P1 authorization:** logs, progress and reports can expose another actor's job data; complete customer membership and role authorization is pending.
- **P1 onboarding validation:** blank customer names can trigger an internal error instead of a client validation response.
- **P1 checkpointing:** image-bearing DOCX runs can fail JSON checkpoint serialization with invalid UTF-8.
- **Queue:** durable queue and queue-at-capacity behavior are not implemented.
- **Coverage:** larger Banking files, full cancellation/resume fault cases, cross-customer authorization, successful upload/HITL/write performance loads, and deployment verification need follow-up.

See the defect note for evidence and suggested exit checks. Do not treat an implemented route or an OpenAPI entry as proof that its authorization and runtime behavior are complete.

## ADO repository details

Suggested repository description: **“Knowledge Fabric 2.5 document ingestion service: customer-scoped ingestion, ontology-guided extraction, Ray job execution, PostgreSQL-backed lifecycle and HITL, Neo4j/Weaviate publication, API/UI contracts, tests, and developer handover. Validation candidate; known defects documented.”**

See [`KFabric-Ingestion-V2/docs/ADO_REPOSITORY_DETAILS.md`](KFabric-Ingestion-V2/docs/ADO_REPOSITORY_DETAILS.md) for the detailed scope, architecture, dependency inventory, validation status, and proposed ADO metadata.

## Safety and data handling

- Do not point unreviewed tests at shared or production stores. Use isolated services and dedicated test customers.
- Do not add real customer documents, populated environment files, tokens, database credentials, or generated runtime state to the repository.
- Review the defect list and current API contract before integration or deployment.
- Keep this repository's history and deployment separate from the existing `KBFabricIngestion` V2 repository.

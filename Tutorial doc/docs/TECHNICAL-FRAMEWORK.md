# TIA — Technical Framework (as implemented)

| | |
|---|---|
| **Version** | 2.0 |
| **Status** | Describes the code in `production/tia/` as of 2026-11-23 |
| **Replaces** | Technical Framework v1.3 (2026-10-29, the proposed baseline written before the build; archived) |
| **Document set** | PRD.md v3.2 (scope and acceptance criteria) · this document (engineering model and rules) · [ARCHITECTURE-AS-BUILT.md](ARCHITECTURE-AS-BUILT.md) (modules, layers, tables, flows; generated from the code) · [GRAPH-SCHEMA.md](GRAPH-SCHEMA.md) (every node and relationship type; generated) · [API.md](API.md) (every route; generated) |

> **How to read this.** Section numbers are the same as in v1.3, so references from the PRD (§5.2, §5.4, §7, §14, §15) still land in the
> right place. Each section says what is built. Where the build differs from v1.3, a **Differs from v1.3** note says how. Section 20 lists
> everything v1.3 asked for that is **not built**. The long lists (node types, relationship types, routes, modules) are generated from the
> code into the documents above, so they cannot drift.

---

## 1. Purpose

TIA answers, for a proposed code, API, event or data-schema change:

> Which downstream components may be affected, what evidence supports each finding, what verification is required, and what remains unknown?

The answer is a deterministic, evidence-backed report, not an AI-written summary. No language model is used anywhere in the analysis.

## 2. Architectural principles (all built)

| Principle | How the code meets it |
|---|---|
| Evidence before conclusions | every graph edge carries status, strategy, rule and file/line; every finding carries its path, evidence status, resolution strategy and severity rule |
| Deterministic analysis | the same inputs, configuration and evidence snapshot give byte-identical structured output; `POST /impact-analyses/{id}/reproduce` re-runs and compares |
| Explicit uncertainty | evidence statuses (§5.3), `unknowns` with reasons, and analysis statuses (§7.3); a missing edge is never read as "no dependency" |
| Version-aware relationships | graph versions are content-addressed by the extraction inputs; relationships record the revisions they were seen at, and go `stale` when the evidence changes |
| Incremental processing | extraction is cached per git blob; an unchanged file is never parsed twice; graph versions are reused when their inputs are unchanged |
| Separation of concerns | ten layers with an import rule checked on every test run (ARCHITECTURE-AS-BUILT §2) |
| Ingest, then analyse | connectors write into TIA's stores; analysis reads only those stores and the local git clones, pinned to an evidence snapshot. It never calls GitHub, ITSM, telemetry or Knowledge Fabric |
| No false completeness | `complete` is only reported when nothing in scope is missing; otherwise `complete_with_gaps` or `partial`, with the reasons |
| Security from day one | tenant isolation by forced row-level security, RBAC on every route, per-repository redaction, audit chain (§9, §11) |

## 3. Technology stack (as built)

| Layer | Built with | Differs from v1.3 |
|---|---|---|
| User interface | one HTML page (`tia/web/index.html`) with plain JavaScript and Cytoscape (vendored); a server-rendered report page | v1.3 proposed React/TypeScript |
| API | Python, FastAPI, Pydantic | — |
| Java | tree-sitter Java + Spring adapters (controllers, RestTemplate, WebClient, OpenFeign, JPA, Flyway/Liquibase) | — |
| Python | `ast` + FastAPI, Flask, Django/DRF adapters (requests, httpx, SQLAlchemy/SQLModel, Django ORM, Alembic, Django migrations) | — |
| Other languages | Go, C#, Kotlin, TypeScript adapters; a structural adapter for 39 more languages (tree-sitter-language-pack 0.13, bundled grammars) | delivered through the adapter SDK, as planned for Release 4 |
| Contracts | OpenAPI parsing and REST/event schema diff | — |
| Metadata, jobs, evidence, graph | **PostgreSQL only**: graph versions stored as immutable JSONB and traversed in memory; job queue in PostgreSQL | v1.3 left RDF vs property graph open; decided by benchmark in [GRAPH-STORE.md](GRAPH-STORE.md) |
| Knowledge Fabric integration | optional Neo4j mirror of the graph and the TIA ↔ Knowledge Fabric bridge ([NEO4J.md](NEO4J.md), [KF-BRIDGE.md](KF-BRIDGE.md)) | v1.3 said "no multiple graph databases by default"; Neo4j is added for integration with Knowledge Fabric (not as TIA's store), recorded in GRAPH-STORE.md |
| Source integration | GitHub App and webhooks; GitLab, Bitbucket, Azure DevOps connectors | — |
| Delivery | one container image (server, worker, migrate), Docker Compose, Helm chart, Azure Terraform | — |

No Kafka, Kubernetes dependency, vector database or LLM framework is used.

## 4. Logical modules (as built)

The modules sit in ten layers ([ARCHITECTURE-AS-BUILT.md](ARCHITECTURE-AS-BUILT.md) lists every one, with its size and imports).

| v1.3 module | Built as | Notes |
|---|---|---|
| 4.1 Connectors | `repos`, `upload`, `github`, `githubapp`, `webhooks`, `scmhosts`, `ado`, `itsm`, `access`, `publish`, `neo4jgraph`, `kflinks` | each connector reports status and errors; audit records every external write |
| 4.2 Extraction | `extraction`, `extract_*`, `lang/*`, `sqllineage`, `streams`, `testcases`, `registry` | never imports, runs or builds customer code; emits facts, never graph edges |
| 4.3 Identity resolution and graph writer | `resolve`, `graph`, `graphstructure`, `graphstore`, `data` | the only code that creates relationships, using the strategies in §5.5 |
| 4.4 Impact engine | `impact`, `enrich`, `report`, `testscope`, `regression`, `coverage`, … | reads stores only; **differs from v1.3:** when no graph version exists for the analysed inputs yet, the analysis asks `graphstore.get_or_build` to build and store it (content-addressed and idempotent), rather than failing |
| 4.5 Policy engine | `policy` | outcomes `pass`, `fail`, `not_evaluated`, `requires_review`; an incomplete analysis never yields `pass`; modes `advisory` (default) and `enforcing` |
| 4.6 Reporting and API | `api`, `analysis_service`, `report`, `reportpage`, `mcp`, `kfadapter` | PR comment, check run, ITSM attachment, report page, MCP tools, Knowledge Fabric adapter; findings redacted per reader |

## 5. Data model (as built)

### 5.1 Entities

The graph's node types are generated into [GRAPH-SCHEMA.md](GRAPH-SCHEMA.md) (schema `gs-3`, 26 types). How v1.3's entities are held:

| v1.3 entity | As built |
|---|---|
| Repository, Commit, File | repository records and revisions in PostgreSQL; files and lines on every node and edge (not separate nodes) |
| FunctionVersion | `function` nodes in a graph version (the version pins the revision) |
| Service, APIEndpoint, OutboundCallSite, Schema | `service`, `endpoint`, `callsite`, `schema` / `event_schema` nodes |
| DataEntity, Table, Migration | `entity` (ORM class), `table`, `column`, `migration`, `procedure` nodes |
| TestCase | `test` nodes (automated tests) and written test cases (`testcases/*.csv|yaml|json`) |
| Owner | `ownership` in the tenant configuration and `owner` in `tia-services.yaml` |
| EvidenceRecord, EvidenceSnapshot, AnalysisRun | PostgreSQL tables (`evidence_records`, snapshots, analyses) |
| Event | `topic` and `listener` nodes |
| TestRun, BuildArtifact, Deployment, Environment, ChangeRecord, Incident | **evidence records** (kinds `test_run`, `coverage`, `build`, `deployment`, `release`, `incident`, `incident_link`, …), not graph nodes |
| Policy | rules in the versioned tenant configuration (`policies`) |

Added beyond v1.3 (for the tester use cases): `application`, `screen`, `element`, `locator` (UI automation), `request`, `response`,
`field`, `example` (API testing), `rule` (business rules found in code), `type` (classes and inheritance), `contract_ref`, `runtime_ref`.

### 5.2 Relationship types

The relationship types are generated into [GRAPH-SCHEMA.md](GRAPH-SCHEMA.md) with their allowed from/to types and how a change travels
along each.

**In the graph and named as in v1.3:** `CALLS`, `HANDLES`, `EXPOSES`, `ISSUES`, `CONSUMES_API`, `USES_SCHEMA`, `READS`, `WRITES`,
`MIGRATES`, `TESTS`, `CONSUMES_EVENT`.

**Differs from v1.3:**

| v1.3 name | As built |
|---|---|
| `PRODUCES_EVENT` | named `PUBLISHES` (function or listener → topic) |
| `EXECUTED_IN`, `COVERS` (test run → code) | test runs and coverage are evidence records, matched to tests and code during the analysis |
| `BUILT_FROM`, `DEPLOYED_AS`, `DEPLOYED_TO` | builds and deployments are evidence records, matched to services and revisions during the analysis |
| `OBSERVED_CALL_TO` | runtime observations are `runtime_edge` evidence; they confirm or add `CONSUMES_API` with status `runtime_observed` (strategy R6) |
| `GOVERNED_BY` | policies are rules in the tenant configuration, evaluated per analysis |
| `ASSOCIATED_WITH_INCIDENT` | incidents and their links are evidence records (`incident`, `incident_link`) |
| `CONTAINS`, `CHANGED_IN`, `OWNED_BY` | not edges: containment is the file on each node, changes come from the diff, ownership from configuration |

**Added beyond v1.3:**
- `INHERITS`, `MAPS_TO`, `MAPS_COLUMN`;
- the UI structure: `HAS_APPLICATION`, `HAS_SCREEN`, `HAS_ELEMENT`, `LOCATED_BY`, `NAVIGATES_TO`, `CALLS_API`;
- the API structure: `ACCEPTS`, `RETURNS`, `HAS_FIELD`, `HAS_EXAMPLE`;
- `APPLIES_RULE`.

The Neo4j mirror adds the derived `IMPACTS` edges (the direction a change travels) and the written test cases' `COVERS`.

The bridge to Knowledge Fabric uses its own relationship names: `REALIZES`, `SAME_AS`, `STORES`, `ENFORCES`, and `IMPLEMENTS_CHANGE`, which
is reported only. They are listed in [kf/TIA-KF-BRIDGE-ONTOLOGY.yaml](kf/TIA-KF-BRIDGE-ONTOLOGY.yaml).

### 5.3 Evidence and provenance

Every edge records:
- its evidence status;
- the resolution strategy, or the inference rule;
- the source file and line;
- the graph version, which pins the revisions.

Evidence records (§9) carry their source, repository, service, environment, revision, observation window and a digest.

Evidence statuses (lower snake case everywhere):

| Status | Meaning |
|---|---|
| `source_confirmed` | explicit in code or configuration |
| `contract_confirmed` | declared by an API, event or schema definition, or by an owner's declaration |
| `runtime_observed` | seen in ingested telemetry |
| `corroborated` | two or more independent kinds of evidence agree |
| `inferred` | produced by a named rule in §5.4 |
| `unknown` | not enough evidence |
| `stale` | the evidence may no longer describe the current revision |

### 5.4 Inference rules (as built)

| Rule | Produces | Condition |
|---|---|---|
| `INF-PATH-UNIQUE` | `CONSUMES_API` | a literal path template matches exactly one in-scope endpoint (method + normalised template) and the base URL is unresolved |
| `INF-TEST-NAMING` | `TESTS` | the test name follows the naming convention (configurable, `tests.naming`) and no import or call association exists |
| `INF-CALL-AMBIGUOUS` | `CALLS` | a call's simple name matches several functions in the repository: one inferred edge per candidate, also counted as an unknown |
| `INF-INHERITS-AMBIGUOUS` | `INHERITS` | a base-class name matches several classes: one inferred edge per candidate |
| `INF-UI-CALL-AMBIGUOUS` | `CALLS_API` | a screen's API call matches several endpoints: one inferred edge per candidate |
| `INF-PROC-NAME` | procedure call | a called procedure exists in no known database of the service but exactly once in the landscape |
| `INF-SCHEMA-FILE-NAME` | `USES_SCHEMA` (event) | an event schema is tied to a topic only by its file name |
| `INF-KF-NAME` | bridge `REALIZES` / `STORES` | the exact Knowledge Fabric key of a name matches one node in the customer and domain (can be switched off: `knowledge_fabric.link_name_matches`) |

Never a relationship source: an LLM, documentation co-occurrence, temporal proximity, or *similar* names. Rule proposals and gap
suggestions in the Knowledge Fabric bridge use word overlap, but they are only proposals until a person confirms them.

**Differs from v1.3:**
- v1.3 listed three rules. `INF-SCHEMA-SHARED` is **not built** (§20).
- The last six rules in the table above were added during the build.
- `INF-PROC-NAME`, `INF-SCHEMA-FILE-NAME` and `INF-KF-NAME` rely on an *exact* name. v1.3 forbade "name similarity alone". These rules
  never use similarity, but they do rest on a name. They are kept as `inferred`, capped in severity (§7.2), and listed here so they can be
  reviewed.

### 5.5 Cross-repository resolution (as built)

| ID | Strategy | Status |
|---|---|---|
| R1 | generated or shared client package mapped to the provider | `source_confirmed` |
| R2 | fully resolved HTTP call (path template + base-URL key resolved through the service map, gateway routes or registry) | `source_confirmed` |
| R3 | declared contract (consumer OpenAPI reference, Pact-style file) | `contract_confirmed` |
| R4 | service map `tia-services.yaml` (input to R1/R2) | — |
| R5 | unique path match (`INF-PATH-UNIQUE`) | `inferred` |
| R6 | runtime observation (ingested trace spans) | `runtime_observed` |
| E1 / E2 | event producer/consumer from code (E1) or declared in `tia-services.yaml` (E2) | `source_confirmed` / `contract_confirmed` |

Every unresolved call site is listed as an unknown with file and line. The resolution rate is reported overall and per strategy.

Path normalisation:
- static segments are lower-cased;
- every path-variable syntax becomes `{}`;
- trailing slashes and query strings are stripped;
- gateway prefixes are rewritten (`strip_prefixes`).

## 6. Change-analysis processing (as built)

**Ingestion** (may call external systems):
1. accept the request;
2. validate revisions and permissions;
3. fetch and extract the changed files;
4. refresh evidence;
5. resolve and store the graph version.

**Analysis** (local only):
6. pin the evidence snapshot;
7. traverse within the configured depth and result limits;
8. correlate tests, coverage, contracts, runtime observations, builds and deployments;
9. classify findings;
10. evaluate policies;
11. persist the report;
12. publish the PR comment, check run or ITSM attachment.

A result stays bound to its revision pair. Only the newest revision of a pull request updates its comment.

## 7. Result contract (as built)

### 7.1 Fields

The report holds:
- `analysis_id`, `base_revision`, `proposed_revision`, `evidence_snapshot_id`;
- `status`, `coverage` (with the resolution rate per strategy);
- `findings`, `unknowns`, `policy_results`;
- `generated_at`, `configuration_version`, `extractor_versions`.

The tester views add `test_scope`, `regression` and the tester pack. With the bridge on, `business_impact` is added.

Every finding records:
- the component and impact type;
- the severity and `severity_rule`;
- the evidence status and the `resolution_strategy` or `inference_rule`;
- the path, with the evidence of each step;
- source references and associated tests;
- limitations, recommended verification and `material`.

### 7.2 Severity rules (as built)

| Rule | When |
|---|---|
| `SEV-BREAKING-CRITICAL` | breaking change reaching a critical component |
| `SEV-BREAKING-CONFIRMED` | breaking change, confirmed path |
| `SEV-NONBREAKING-CRITICAL` | non-breaking change reaching a critical component |
| `SEV-NONBREAKING-CONFIRMED` | non-breaking change, confirmed path |
| `SEV-LOCAL` | the effect stays inside the changed repository |
| `SEV-UNCLASSIFIED` | the nature of the change is unknown |
| `SEV-INFERRED-CAP` / `SEV-STALE-CAP` | a path that relies on inferred or stale evidence is capped (default Medium; the tenant may change or remove the cap) |
| `SEV-DATA-MAPPING-BROKEN` | the changed repository still maps something its own migration removes |
| `SEV-DATA-UPDATED` | the mapping is already updated by the same change |

Tenant overrides are recorded on the finding. **Not built:** `SEV-POLICY-FAIL`. A failed policy is reported as a policy outcome, not as a
severity (§20).

### 7.3 Analysis status

| Status | Meaning |
|---|---|
| `running` | in progress |
| `complete` | everything in scope analysed, no limit reached, no unknown affecting a material finding |
| `complete_with_gaps` | finished, with unknowns or missing optional sources |
| `partial` | a limit was reached, evidence was stale, or a repository failed |
| `failed` | no findings could be produced |

## 8. API (as built)

Every route, with its request and response models, is in [API.md](API.md) and `openapi.json` (generated). Conventions:
- validated models and structured errors;
- idempotent job submission keyed by repository, revisions and configuration;
- job ids for asynchronous work;
- limits on traversal and results;
- authorisation on every route (§11) and redaction of what the caller cannot read.

v1.3's proposed endpoints, as built:

| v1.3 | As built |
|---|---|
| `POST /api/v1/repositories` | built |
| `POST /api/v1/impact-analyses`, `GET /impact-analyses/{id}`, `/paths`, `/unknowns` | built (plus `/findings`, `/evidence`, `/comment`, `/graph`, `/tester-pack`, `/policy`, `/reproduce`, `/publish`, …) |
| `POST /api/v1/scans`, `GET /scans/{id}` | **not at this path:** `POST /repositories/{name}/sync`, `POST /graph/rescan`, uploads; job status at `GET /jobs/{id}` |
| `GET /api/v1/components/{id}/dependencies` | **not at this path:** `GET /graph/query?question=dependencies&component=…` (also consumers, tests, covers) |
| `GET /api/v1/policies`, `POST /policy-evaluations` | **not at these paths:** policies are part of `GET/PUT /tenant/config`; evaluation is `GET /impact-analyses/{id}/policy` |

Beyond v1.3, the API also covers:
- tenant configuration and versions;
- evidence ingestion (CI, coverage, runtime, deployments, builds);
- releases, incidents and verdicts;
- tester views (coverage, automation, flows, scripts);
- the MCP server (`/mcp`);
- the Knowledge Fabric adapter (`/query/tia`) and bridge (`/kf/*`);
- administration (identity providers, users, tokens, access).

## 9. Persistence and consistency (as built)

### 9.1 Multi-tenancy

- Every tenant-owned table has `tenant_id`, with row-level security **enabled and forced**. The application role has no `BYPASSRLS` and
  owns no tables. Each transaction sets the tenant first; without it, no rows are visible. A CI gate checks every tenant table.
- Jobs carry the tenant, and the queue has per-tenant concurrency limits. Tenant lifecycle covers suspend, resume, and offboard with a
  deletion certificate.
- Each tenant has its own data-encryption key, optionally wrapped by the customer's KMS (AWS KMS, Azure Key Vault, Vault Transit).
  **Differs from v1.3:** that key encrypts the tenant's **secrets**. Evidence is held in PostgreSQL behind row-level security, and repository
  clones are in per-tenant folders (when sign-in is required). There is no separate object store, and evidence is not encrypted with the
  tenant key (§20).

### 9.2 Stores and consistency

PostgreSQL holds everything: metadata, jobs, configuration versions, evidence records, snapshots, analyses, graph versions and the audit
chain. The Neo4j mirror is a derived copy for Knowledge Fabric. It is rebuilt from PostgreSQL whenever it is missing or out of date, never
read back as truth.

- **Identifiers:** stable node ids built from repository, file and symbol (for example `payments-api:app/services/payments.py::list_payments`).
- **Graph versions:** identified by a hash of their inputs.
- **Evidence:** records are immutable and deduplicated by digest. Snapshots reference record ids rather than copying them.
- **Retention and deletion:** configurable per tenant (`retention`), with audit reporting.

## 10. User interface (as built)

The web UI consumes the API and never recomputes impact. Its sections:
1. ingest repositories (git URL, organisation discovery, local path, zip/folder upload);
2. code graph (levels, search, focus, impact overlay, "ask the graph");
3. analyse a change;
4. the Change Impact Report (findings, paths, what to test, regression selection, business impact, structure, unknowns, coordination, verdict,
   PR comment);
5. tester views (shared components, coverage, API and UI automation inventories, test-case check, execution flow, automation script);
   5a. releases, environments and incidents;
   5b. insights (trends, rule precision);
   5c. Knowledge Fabric rule review;
6. configuration (versioned, with validation, diff and restore);
7. administration (identity providers, users, tokens, repository access).

The report page (`/analyses/{id}`), the PR comment and the CLI show the same distinctions:
- no findings versus failed;
- missing evidence versus a failed test;
- a test existing versus a test that ran and passed;
- confirmed versus inferred versus runtime-observed;
- a policy that passed versus one that was not evaluated.

**Differs from v1.3:** v1.3 listed nine screens. There is no separate Overview, Services, Contracts or Policies screen. Services and
contracts are shown through the graph and the report; policies are edited in the configuration section. A Knowledge Fabric management
section is planned ([plans/KF-UI-PLAN.md](../../../development/docs/plans/KF-UI-PLAN.md)).

## 11. Security and governance (as built)

- **Sign-in:** OIDC and SAML 2.0, scoped service tokens and sessions.
- **Roles:** five roles (`viewer`, `engineer`, `release_manager`, `auditor`, `administrator`) on a deny-by-default route table.
- **Repository access:** mirrored from GitHub, GitLab, Bitbucket and Azure DevOps. Unreadable repositories answer 404, and reports are
  redacted per reader.
- **Secrets:** envelope encryption with secret references; values found in source are scrubbed and never stored in the graph or reports.
- **Transport and network:** TLS enforced for external connections; outbound connections are guarded (no private addresses unless
  allowed); air-gap mode.
- **Parsers** never execute repository code or build scripts.
- **Audit:** a hash-chained audit log for ingestion, analysis, exports, external writes and administration.
- Threat model and review in [SECURITY.md](SECURITY.md); controls mapped to SOC 2 and ISO 27001 in [COMPLIANCE.md](COMPLIANCE.md).

## 12. Reliability and performance (as measured)

The system handles:
- retries with backoff;
- duplicate webhook events, deduplicated by key;
- jobs superseded by a newer revision;
- lease expiry for interrupted jobs;
- parser failures, reported as unknowns;
- traversal limits, reported as `partial`.

Measured on generated landscapes of 500 repositories (up to 0.6 M lines): PR analysis P95 5.3–8.9 s, report P95 0.07–0.19 s, 50-PR burst
comment P95 under 165 s ([PERFORMANCE.md](PERFORMANCE.md)). **Not demonstrated:** a 20 M-line scan, availability targets, and a
multi-node failover drill ([OPERATIONS.md](OPERATIONS.md)).

## 13. Testing (as built)

About 1,440 automated tests run on a real embedded PostgreSQL, and against a real Neo4j when it is configured.

| Kind | What it covers |
|---|---|
| Unit | parsers, normalisation, resolution strategies, inference and severity rules, policies |
| Integration | API, persistence, graph, connectors against stand-ins |
| Golden corpus | the reference landscapes in `development/reference/` (two independent real-code Java landscapes and a synthetic one), with deliberately unresolvable and ambiguous calls (`tools/accuracy.py`) |
| Determinism | re-run on the same snapshot gives identical output |
| Security | authorisation, cross-repository redaction, secrets, malicious input, audit |
| End to end | PR → analysis → comment, report and policy outcome |
| Documentation | the generated documents and the docs match the code |

## 14. Observability (as built)

- **Prometheus metrics:** `tia_analysis_*` (runs, duration, findings, unknowns, traversal size, truncation), `tia_resolution_*` (calls and
  rate by strategy), `tia_extraction_*`, `tia_connector_*`, `tia_jobs_*`, `tia_graph_nodes` / `tia_graph_edges`, `tia_publish_total`,
  `tia_http_*`, `tia_rate_limited_total`, `tia_secrets_withheld_total`, `tia_worker_last_poll_timestamp_seconds`.
- JSON logs with a request id, OTLP traces, and health and readiness endpoints for the server and the worker.
- Platform health (`/api/v1/health`, readiness) is separate from analysis completeness (the report status).

## 15. Releases (status)

Releases and gates are defined in PRD §13 and §19. Status (from [RELEASE-GATES.md](RELEASE-GATES.md)):

| Release | Functional criteria | Open |
|---|---|---|
| R1 | Met (simulated: GitHub check run against a stand-in) | real GitHub App install |
| R2 | Met (simulated) | live ITSM, telemetry and CI connectors with real credentials |
| R3 | Met | HA failover drill, timed restore drill, SOC 2 / ISO audits |
| R4 | Met (simulated) | accuracy of the R2–R4 languages, events and SQL on a labelled real-code landscape |

For every release: no independent penetration test yet; containers built and run on Docker Desktop; Helm and Terraform not deployed.

## 16. Acceptance criteria (status)

All thirteen v1.3 criteria hold in automated tests, with these exceptions:
- **Criterion 3**, "only §5.2 relationship types": paths use the relationship types as built (§5.2 above).
- **Criterion 6:** every inferred relationship names a rule, but six rules are additions to v1.3's list (§5.4).
- **Criterion 13**, "performance tests cover the release scope": only at the sizes measured (§12).

## 17. Decisions

**Decided during the build:**
- **Graph store:** PostgreSQL only; revisit above about 1 million nodes per tenant landscape (GRAPH-STORE.md).
- **Identifiers:** repository + file + symbol (§9.2).
- **Traversal limits:** configurable per tenant (`limits.max_depth` default 8, `max_results` default 200, a time budget).
- **Policy language:** rules in the tenant configuration (FR-033); modes advisory and enforcing.
- **Runtime and deployment integration:** through ingested evidence records.
- **Private deployment:** single-tenant install of the same image, and air-gap mode.

**Still to decide:**
- whether the three exact-name rules (§5.4) stay;
- whether to build the items in §20;
- evidence freshness thresholds beyond revision changes;
- the composition of a labelled real-code reference landscape for languages other than Java.

## 18. Definition of success

TIA answers, with inspectable evidence:
- what changed;
- which components may be affected and why;
- which tests and contract checks verify them;
- which production observations matter;
- what is missing or stale;
- which policies passed, failed or need review.

With the Knowledge Fabric bridge on, it also answers which business systems, requirements and business test cases the change reaches
([KF-BRIDGE.md](KF-BRIDGE.md)).

## 19. Document history

| Version | Date | Notes |
|---|---|---|
| 1.0–1.3 | 2026-10-28/29 | Proposed technical baseline, written before the build (archived) |
| 2.0 | 2026-11-23 | Rewritten to describe the implementation; differences from v1.3 marked in each section; gaps listed in §20 |

## 20. Not built (from v1.3)

| v1.3 item | State |
|---|---|
| `INF-SCHEMA-SHARED` (a consumer's data class structurally identical to a provider schema, with a `CONSUMES_API` between them) | not built |
| `SEV-POLICY-FAIL` severity rule | not built; policy failures are policy outcomes |
| API paths `/scans`, `/components/{id}/dependencies`, `/policies`, `/policy-evaluations` | equivalents exist under other paths (§8) |
| Evidence objects encrypted with the per-tenant key, in an object store | evidence in PostgreSQL behind row-level security; the tenant key encrypts secrets |
| Separate Overview, Services, Contracts and Policies screens | covered by the graph, the report and the configuration section (§10) |
| Relationship names `PRODUCES_EVENT` and the Release 2–3 edges | `PUBLISHES`; evidence records instead of edges (§5.2) |

# TIA ↔ Knowledge Fabric 2.5 bridge (as built)

This document describes how TIA's software graph is connected to Knowledge Fabric's business graph, and how to run and operate that link.
It records what was built, how to run it, and why it works the way it does.

**Verification level.** The bridge is tested against a real Neo4j holding a Knowledge Fabric graph that was written the way Knowledge
Fabric 2.5's writer writes it (`fixtures/kf/kf_world.py`). TIA's identity code is checked against Knowledge Fabric's own functions, read
from its source. It has **not** yet been run against a Knowledge Fabric instance filled by Knowledge Fabric's own ingestion. Do that
before production use (see "First connection to a real Knowledge Fabric").

---

## 1. What it does, in one picture

```
  TIA (software lane, domain-neutral)                    Knowledge Fabric 2.5 (business lanes, one customer)
  ───────────────────────────────────                    ──────────────────────────────────────────────────
  (:TiaService {service:"statement-service"}) ──REALIZES──▶ (:System {name:"Statement Service", domain, customer_id})
                                                                 ▲ IMPACTS          ▲ IMPACTS       ▲ USES
                                                          (:Requirement)    (:Defect/:Incident)  (:Process)
                                                                 ▲ COVERS
  (:TiaTestCase {case_id:"NW-11"}) ────────SAME_AS───────▶ (:TestCase {name:"NW-11"})
```

- **Two graphs, one database.** Each side keeps its own labels, ontology and owner. TIA writes only `Tia*` nodes and the bridge edges.
  It never creates, changes or deletes a Knowledge Fabric node.
- **The bridge edges** are `REALIZES` (service → System) and `SAME_AS` (test case → TestCase). Every bridge edge carries the following
  properties:
  - `bridge=true` and `owner='tia'`;
  - `tenant`, `kf_customer_id` and `kf_tenant_id`;
  - `status` (`contract_confirmed`, `source_confirmed` or `inferred`), `strategy`, `rule` and `source`;
  - `id_check` (`computed` or `looked_up`);
  - `tia_graph_version`, `kf_fingerprint` and `bridge_version` (`kb-1`).
- **Living.** A bridge run happens in three cases: when TIA's graph moves, when Knowledge Fabric's customer graph changes (fingerprint
  poll), and when the mirror disappears (for example after Knowledge Fabric's reset script). Each run adds new links, keeps unchanged ones
  and removes links whose end is gone.
- **Business impact in every output.** Analyses pin the business context the last run captured, and never call Knowledge Fabric live.
  That context is shown in the report, the PR comment, the report page, the web UI, MCP and the Knowledge Fabric adapter.

## 2. TIA stays domain-neutral

TIA's own graph has no business domain. The domain is a property of the Knowledge Fabric side only, and the setting `domains` decides which
of the customer's domains the bridge links to:
- `"auto"` (the default) links into every domain the customer has. A service whose System exists in two domains gets two links.
- a list restricts linking to those domains. A listed domain the customer does not have is reported as an `unknown_domains` gap.

Turning domains "off" therefore means leaving `domains: auto`: TIA never asks which business domain the code belongs to.

## 3. How a link is decided

For each TIA service, in this order (first hit wins per domain):

| Source | Example | Evidence status |
|---|---|---|
| tenant setting `knowledge_fabric.system_aliases` | `mobile-bff: [Mobile Banking App]` | `contract_confirmed` |
| `business_systems` in the repository's `tia-services.yaml` | `business_systems: [Statement Service]` | `contract_confirmed` |
| exact name, after Knowledge Fabric's own normalisation (`payments-api` → `Payments Api`) | | `inferred`, rule `INF-KF-NAME` (switch off with `link_name_matches: false`) |

How a name is found in Knowledge Fabric:
1. TIA computes Knowledge Fabric's `entity_id`, which is `sha256(scope:domain:EntityType:normalized)` with scope
   `tenant:{T}|customer:{C}` (`tia/kfidentity.py`, a copy of Knowledge Fabric's formula).
2. When no node has that id, TIA looks the name up in the customer's nodes by `name`, `name_display` or `aliases`. This covers nodes that
   Knowledge Fabric's reconciliation pinned to another id. Such links are marked `id_check: looked_up`.
3. When a name matches more than one node in a domain, nothing is linked and an `ambiguous` gap is reported.

Test cases are linked by their ID (`NW-11` ↔ TestCase `NW-11`) within the same customer, with status `source_confirmed`.

A link can never reach another customer. The computed id includes the customer, every lookup filters on `customer_id`, and the writer
refuses a target whose `customer_id` differs.

## 4. Configuration

The tenant setting is versioned and audited like every other setting (`PUT /api/v1/tenant/config`):

```json
"knowledge_fabric": {
  "enabled": true,
  "customer_id": "Pulse AI_Northwind_6b1f0c1e-…",
  "kf_tenant_id": null,
  "domains": "auto",
  "system_aliases": {"mobile-bff": ["Mobile Banking App"]},
  "link_name_matches": true,
  "auto_relink": true,
  "poll_minutes": 15,
  "max_context_items": 100
}
```

What each setting does:
- **`customer_id`** is required when the bridge is enabled. Knowledge Fabric creates it at onboarding, so it is runtime data, not a
  constant. Pick it from `GET /api/v1/kf/customers` or `tia kf-customers`, which list the CustomerRoot nodes with their tenant id, industry
  and domains.
- **`kf_tenant_id`** defaults to the tenant on the customer's CustomerRoot. Set it only when Knowledge Fabric holds the customer under more
  than one tenant; the run refuses to guess.
- **`poll_minutes`** is between 5 and 1440.
- **`max_context_items`** limits the requirements, test cases and other items kept per System in the captured context.

In a repository, the owner can declare the System their service realises:

```yaml
# tia-services.yaml
service: statement-service
business_systems: [Statement Service]
```

## 5. Running it

| How | What |
|---|---|
| `POST /api/v1/kf/relink` | run now (`?async=true` queues a `kf_bridge` job) |
| automatically | after every graph-head move and every change to the `knowledge_fabric` setting (`auto_relink`), and by the `kf_poll` job every `poll_minutes` |
| `GET /api/v1/kf/links`, `GET /api/v1/kf/gaps` | the latest run's links (with evidence) and gaps |
| `GET /api/v1/kf/rule-proposals`, `POST /api/v1/kf/rule-proposals/{id}` | rule pairs to review, and a person's decision (web UI section 5c) |
| `GET /api/v1/graph/ontology` (`?format=yaml`) | TIA's software ontology and the bridge ontology (also in `docs/kf/`) |
| `tia kf-customers`, `tia kf-link --repo … --customer …` | the same from the command line, without TIA's database (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`) |
| SDK | `kf_customers()`, `kf_relink()`, `kf_bridge_links()`, `kf_gaps()`, `kf_rule_proposals()`, `kf_decide_rule(id, decision)`, `ontology()`, `business_impact(id)` |
| MCP | `get_business_impact` |

Permissions:
- `kf/customers` needs `config_write`;
- `relink` needs `repo_write`;
- `links`, `gaps` and the ontology need `read`.

Relink, links and gaps also need a caller who can read every repository of the tenant, because the mirror holds all of them.

### Gaps (what to fix)

| Gap | Meaning | Usual fix |
|---|---|---|
| `unmapped_service` | a TIA service matched no System | add `system_aliases` or `business_systems` (a test-only repository can stay unmapped) |
| `systems_without_code` | a customer System no service realises | alias it, or accept it (for example a vendor product) |
| `declared_not_found` | a declared System name does not exist in Knowledge Fabric | fix the spelling, or ingest the System into Knowledge Fabric |
| `ambiguous` | the name matches more than one node in one domain | declare the exact System, or reconcile the duplicates in Knowledge Fabric |
| `test_cases_only_in_tia` / `test_cases_only_in_kf` | a test case ID exists on one side only | usually expected; check that the ID format is the same on both sides |
| `unknown_domains` | a restricted domain the customer does not have | fix `domains` |

### Each run is recorded

Every run records two pieces of evidence:
- `kf_bridge_run`: the summary, up to 5,000 links, the gaps, what was written, any warnings and the run time.
- `kf_context`: the business context per service (the System with its requirements, user stories, processes, defects, incidents,
  integrations and test cases). It carries the customer, the Knowledge Fabric fingerprint and TIA's graph version.

Audit events are `kf_bridge_completed`, `kf_bridge_failed` and `kf_mirror_restored`.

## 6. In an analysis

When the setting is enabled, the analysis takes the latest `kf_context` (pinned in the evidence snapshot, so a reproduction gives the same
answer). It works out the services the change affects: the changed service, plus every service with a material finding. It then reports:

- `business_impact.summary.headline`, for example "Knowledge Fabric: 6 business test case(s) cover requirements on the 4 system(s) this
  change affects (4 not in your repository suite)." (the `feature/remove-disputed` scenario of the showcase);
- `business_impact.systems`: per affected System, its domain, the link evidence, requirements, processes, open defects and incidents;
- `business_impact.test_cases`: the Knowledge Fabric test cases, the requirements each one covers, the evidence status of the weakest link
  on its chain, and `also_in_repository_suite`;
- `test_scope.regression.knowledge_fabric`: the counts next to TIA's own regression selection.

Two kinds of unknowns make gaps visible instead of hiding them:
- `kf_context_unavailable`: the bridge is enabled but has not run yet.
- `kf_systems_unmapped`: an affected service has no System.

Redaction removes the Systems and test cases that come only through repositories the reader cannot see.

The same answer is available in a single Cypher query over both graphs, for Knowledge Fabric agents
(`neo4jgraph.BUSINESS_CYPHER` / `business_regression`). A test checks that the query and the report agree.

## 7. Operating notes and security

- **Knowledge Fabric's retriever has no customer filter.** Its graph search does not filter on `customer_id`. Agents that query Knowledge
  Fabric directly can therefore see other customers' nodes in the same database. TIA's own queries always filter on the customer. Give each
  customer its own database, or fix the retriever before several customers share one database.
- **Knowledge Fabric's `reset_datastores` wipes the whole database**, including TIA's mirror and the bridge edges. The next poll sees the
  mirror is missing, re-syncs it and relinks (audited as `kf_mirror_restored`). No manual step is needed.
- **No webhook exists.** Knowledge Fabric has a jobs API but sends no change events, so TIA polls a cheap fingerprint: per label, a count
  and the latest `updated_at`, plus a relationship count.
- **Neo4j needs to hold both graphs.** Point `NEO4J_*` at Knowledge Fabric's database. Grant agents read access only to the whole-landscape
  view (see [NEO4J.md](NEO4J.md#security)).
- **Upgrading Knowledge Fabric.** Knowledge Fabric's identity functions are copied in `tia/kfidentity.py`, and
  `tests/test_kfbridge.py::test_identity_matches_knowledge_fabrics_own_code` compares them with the Knowledge Fabric source in
  `development/reference/knowledge-fabric-2.5/` whenever that source is present. Re-run that test after every Knowledge Fabric upgrade.

## 8. First connection to a real Knowledge Fabric

1. Point `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD` at Knowledge Fabric's Neo4j, then run `tia kf-customers` and choose the customer.
2. Run `tia kf-link --repo … --customer "<id>" --format json` and read the gaps. Add aliases until the important services are linked.
3. Save the same setting on the tenant, then run `POST /api/v1/kf/relink`.
4. Run one analysis and check that `business_impact` shows the Knowledge Fabric test cases the business testers expect.
5. Report any difference to the TIA team. The fixture `fixtures/kf/kf_world.py` should then be updated to match what Knowledge Fabric
   really wrote.

## 9. Code and tests

| Module | Layer | Role |
|---|---|---|
| `tia/kfidentity.py` | foundations | Knowledge Fabric's name normalisation and `entity_id` |
| `tia/kfsettings.py` | rules | the `knowledge_fabric` setting: defaults, validation, canonical form |
| `tia/kfontology.py` | graph | the software and bridge ontologies (generated from `graphschema`) and the allowed bridge triples |
| `tia/kfbridge.py` | graph | link planning, gaps, business impact and its redaction |
| `tia/neo4jgraph.py` | connectors | Knowledge Fabric reads (customers, domains, index, fingerprint, context), the bridge writer, the business Cypher |
| `tia/analysis_service.py` | orchestration | `kf_link`, `kf_bridge_run`, the `kf_bridge` and `kf_poll` jobs |
| `migrations/0020_kf_bridge_evidence.sql` | | the two new evidence kinds |

Tests:
- `tests/test_kfbridge.py` runs without a server: identity examples, the contract with Knowledge Fabric's source and with its
  `retriever_api.py`, the ontologies, the setting, planning and business impact.
- `tests/test_kfbridge_live.py` needs `TIA_TEST_NEO4J_URI` and `TIA_TEST_NEO4J_PASSWORD`. It covers:
  - customers and domains;
  - auto versus restricted domains, and name matching switched off;
  - the reconciled node;
  - customer isolation, and Knowledge Fabric's subgraph left untouched;
  - an idempotent re-run;
  - a System deleted in Knowledge Fabric, and a repository removed from TIA;
  - an unknown customer and an ambiguous tenant;
  - the report's business impact on the showcase, a reproduction, and parity with the Cypher query;
  - the API;
  - poll triggers and a wiped mirror being restored.

## 10. Phase 7: screens, data, business rules and user stories

Phase 7 links more of the software graph to Knowledge Fabric's business graph. Every link stays inside one customer.

| Link | How it is decided | Evidence |
|---|---|---|
| screen `REALIZES` FormCapture | the tenant setting `screen_aliases` (keyed by route or component), or `business_screens` in `tia-services.yaml`; else the exact key of the component name or of the route's last segment (`/accounts/:id` → `Accounts`) | declared → `contract_confirmed`; name → `inferred` |
| ORM entity `STORES` DataEntity | `data_aliases` or `business_data`; else the exact key of the class name, or of the name without a technical suffix (`PaymentRecord` → `Payment`; Record, Entity, Model, Dto, View, Row, ...) | as above |
| table `STORES` DataEntity | as above; else the exact key of the table name or of its singular (`payments` → `Payment`) | as above |
| code rule `ENFORCES` Rule | **never by name.** TIA proposes pairs; a person confirms or rejects each one | confirmed → `contract_confirmed` |
| change `IMPLEMENTS_CHANGE` UserStory | an issue key (`PAY-4521`) in the change's commit messages that Knowledge Fabric holds as a user story's name or alias | `source_confirmed`; reported in the analysis, not written to Neo4j |

A declared name that does not match anything is reported as a gap. TIA never falls back to a name match in that case. New gap kinds:
`unlinked_screens`, `unlinked_data`, `forms_without_screen` and `data_entities_without_code`.

### Business rules: TIA proposes, a person decides

A rule found in code is a condition, such as `not p['disputed']`. A Knowledge Fabric Rule is a sentence, such as "Disputed Payments Excluded
From Statements". Names alone cannot link them, so TIA proposes candidate pairs:
- **Score:** 0.6 × the share of the rule's field words that appear in the Rule's name or description, plus 0.4 × the word overlap of the
  condition, its fields and its function with the Rule's name and description.
- **Limit:** at most three proposals per code rule.

To decide:
- Open web UI section 5c, or call `GET /api/v1/kf/rule-proposals` and `POST /api/v1/kf/rule-proposals/{id}` with `{"decision": "confirm" | "reject"}`.
- Every decision is evidence (`kf_link_decision`) and an audit event (`kf_rule_link_decided`).
- The newest decision on a pair wins.
- A confirmed pair becomes an `ENFORCES` link on the next bridge run. A rejected pair is not proposed again.
- Switch proposals off with `propose_rules: false`.

### In an analysis

Screens, entities, tables and rules are shown only when the change touches them:
- the changed component itself;
- a node on a finding's path;
- a table whose column changed, and the entities that map it;
- a rule that the change's rule gap names.

For each touched item, the report shows the Knowledge Fabric node, the requirements impacting it, the processes governed by or producing it,
its defects and incidents, and the test cases covering those requirements. User stories are found by the issue keys in the commit messages
between base and head. Pull request titles and branch names are not read yet, so put the key in a commit message.

On the showcase:
- **Dropping `payments.reference`** shows the `Payment` data entity, its requirement, test case `TC-DATA-003` (not in the repository suite)
  and defect `DEF-91`.
- **A commit "PAY-4521 hide disputed payments ..."** shows user story PAY-4521, the requirement it implements and the test cases covering
  it. After review, it also shows the business rule the change makes `mobile-bff` miss, with the process it governs.

Keys a commit names that Knowledge Fabric does not have are listed in the unknown `kf_issue_keys_not_found`.

### Settings added

`screen_aliases`, `data_aliases` (each a name → list of Knowledge Fabric names), `propose_rules` (default true) and `link_issue_keys`
(default true). The bridge version is `kb-2`. Migration `0021_kf_link_decisions.sql` adds the decision evidence kind.

### Tests

- `tests/test_kfbridge_phase7.py` runs without a server: names, linking, the settings, proposals and decisions, issue keys, business
  impact and redaction.
- `tests/test_kfbridge_live_phase7.py` runs against Neo4j:
  - links and their context;
  - customer isolation;
  - a review leading to an `ENFORCES` link and its effect on the report;
  - the story found from a commit;
  - the data entity reached by a dropped column;
  - the review API.

Verification level: as for the rest of the bridge, these run against a simulated Knowledge Fabric graph. They have not been run against
real ingestion.

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

Turning domains "off" therefore means leaving `domains: auto`.

Since section 11, a person **may** place a repository (or a service) in the customer's business tree: domain > product line > function. The
code graph stays domain-neutral (the same code builds the same graph, with or without Knowledge Fabric); the place is a fact of the bridge,
chosen by a person or declared by the repository's owner, never guessed from code. A mapped repository is linked only in its domains.

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

The usual way is the web UI, section **5c. Knowledge Fabric** ([USER-GUIDE.md](USER-GUIDE.md)): a status strip with *Relink now*, and the tabs Status, Connect & settings (setup, aliases, turn off), Links, Gaps (with fixes), Review, Products and Ontology; plus *Show Knowledge Fabric links* in the code graph. The API below does the same.

| How | What |
|---|---|
| `GET /api/v1/kf/status` | state (`off`, `never_run`, `running`, `failed`, `connected`), the last run, counts of links, gaps and items to review, the bridge job and the next poll; names, warnings and the failure text only for a principal who can read every repository |
| `PATCH /api/v1/kf/settings` | part of the setting, and single aliases added or removed, as a new configuration version (`expected_version` → 409 when someone saved since) |
| `GET /api/v1/kf/overlay`, `GET /api/v1/kf/node?id=` | the latest run's links for the graph view, and one node's links with its business context; both filtered to the repositories you can read |
| `GET /api/v1/kf/decisions`, `GET /api/v1/kf/review-measures` | every decision people made, newest first; the review measures |
| `DELETE /api/v1/kf/links` | remove TIA's links from Knowledge Fabric's graph (turning off; `config_write`) |
| `POST /api/v1/kf/relink` | run now (`?async=true` queues a `kf_bridge` job, which `GET /api/v1/jobs/{id}` shows to a principal who can read every repository) |
| automatically | after every graph-head move and every change to the `knowledge_fabric` setting (`auto_relink`), and by the `kf_poll` job every `poll_minutes` |
| `GET /api/v1/kf/links`, `GET /api/v1/kf/gaps` | the latest run's links (with evidence) and gaps (`?suggest=true`: up to three close names per unlinked item, never linked by themselves) |
| `GET /api/v1/kf/rule-proposals`, `POST /api/v1/kf/rule-proposals/{id}` | rule pairs to review, and a person's decision (web UI section 5c) |
| `GET /api/v1/graph/ontology` (`?format=yaml`, `&download=true`) | TIA's software ontology and the bridge ontology (also in `docs/kf/`) |
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

Audit events are `kf_bridge_completed`, `kf_bridge_failed`, `kf_mirror_restored`, `kf_settings_changed` and `kf_links_removed`.

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
| `tia/kfreview.py` | graph | review items, their actions and what a decision records (section 12) |
| `tia/kfrouter.py` | surface | the reference router: one question, both retrievers (section 13) |
| `tia/kfinbox.py` | connectors | TIA's items in Knowledge Fabric's review screen: the client, the signed callback (section 12) |
| `migrations/0022_kf_review_items.sql`, `0023_change_issue_keys.sql` | | the review queue; the index for analyses by issue key |

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

## 11. Product mapping: domain > product line > function

Knowledge Fabric keeps one business tree per customer: `CustomerRoot <- DomainRoot <- ProductLine <- Function <- SubFunction`, joined by
`PART_OF`, from the industry ontology adjusted per customer. Its ingestion runs are frozen to one domain and product line, chosen from the
customer's configuration, never from the content. TIA follows the same rule for code.

**The catalogue.** Each bridge run reads the tree from the `PART_OF` edges (`neo4jgraph.kf_catalog`; never from a function's
`product_line_id` property, which keeps only the last of its product lines) and keeps it with the run: `GET /api/v1/kf/catalog`,
`tia kf-catalog --customer <id>`. A product line Knowledge Fabric's review writer created has a `product_line_id` but no `entity_id`; TIA
links to it by that id.

**Where a place comes from**, first match wins per service:

| Source | Who | Applied |
|---|---|---|
| `knowledge_fabric.service_products` | an administrator | yes, `contract_confirmed` |
| `knowledge_fabric.repository_products` (`PUT /api/v1/kf/products/{repository}`, the *Products* panel of section 5c) | an administrator | yes, `contract_confirmed` |
| `business_products` in the repository's `tia-services.yaml` | the repository's owner | yes, `contract_confirmed` |
| a reviewer accepting a suggestion (section 12) | a reviewer | yes, `contract_confirmed`, source `review` |
| the places of the Systems the service is linked to | TIA | **no**: a `product_suggestion` to review, never applied by itself |

```yaml
# tia-services.yaml
service: payments-api
business_products:
  - {domain: consumer_retail, product_line: Retail Cards, function: Payment Processing}
```

**What a place changes.**
- *Domain scope.* A mapped repository's services, screens, data entities and test cases are linked only in the mapped domains (a place wins over
  `domains`). Unmapped repositories keep the `domains` rule.
- *Ties.* When a name finds several nodes in one domain, the one under the mapped product line wins: status `inferred`, rule `INF-KF-PRODUCT`.
- *The edge.* `(:TiaService)-[:PLACED_IN {product_line_id, level, source, status}]->(:ProductLine | :Function | :SubFunction)`, a bridge edge
  like the others (bridge version `kb-3`). Not `PART_OF` (Knowledge Fabric's own tree walks), not `BELONGS_TO` (its path renderer hides that
  name), not `SUPPORTS` (its vision ontology uses it between sub-functions). It carries the product line, because one function can sit under
  two product lines of a domain (Delinquency Management under Lending and Supply Chain Finance in the banking ontology).
- *The roll-up.* Every report's business impact gains `products`: one row per product line the change reaches, the changed service's first,
  with the services and how each got there (declared, or inherited through a linked System and therefore `inferred`), the functions, the entry
  points to test, the business test cases and the user stories. The headline starts with them ("Affects Retail Cards (consumer_retail)."),
  so the pull-request comment does too. `POST /query/tia` answers them as `business_product` chunks and filters by `domain` and `product_line`.

**Problems are gaps**, never silent: `product_not_found` (a named place that is not in the tree, with a hint when a name is close, e.g. a
rename), `unmapped_repositories` (when mapping is in use), `product_conflicts` (none of the Systems the service's names find sits in its
mapped place; it becomes a review item), `unknown_domains`.

## 12. Review of uncertain links (human in the loop)

Every bridge run turns what it is not sure of into review items, kept in the table `kf_review_items`:

| Kind | Asked when | Actions |
|---|---|---|
| `guessed_link` | a link found by name (`inferred`) | confirm, confirm_with_alias, reject |
| `ambiguous` | a name matches several nodes in one domain | choose, reject (none of them) |
| `suggested_link` | an unlinked service, screen, entity or table has close Knowledge Fabric names (shared words) | choose, confirm_with_alias, reject |
| `rule_pair` | a rule in code and a Knowledge Fabric Rule share fields or words | confirm, reject |
| `product_suggestion` | a service without a place; its Systems' place | confirm (accept), choose (another place), reject |
| `product_conflict` | a declared place disagrees with where the Systems sit | confirm (keep), choose (change the place) |
| `story_not_found` | a recent change names an issue key with no user story, or several | choose (one of several), reject (ignore) |
| `stale_decision` | a decided Knowledge Fabric node disappeared (re-ingestion, rename) | choose (move the decision), reject (drop it) |

Every item can also be deferred (`defer`: open again on the next run) and, once decided, undone (`reset`). The actions are Knowledge
Fabric's review vocabulary (`APPROVE_REUSE`, `APPROVE_REUSE_WITH_ALIAS`, `REJECT`, `LEAVE_UNRESOLVED`); `APPROVE_AS_NEW` has no TIA meaning,
because TIA never creates business nodes.

- **Identity.** An item's id is a hash of (kind, TIA item, sorted candidates): each run finds the same item again; a different set of
  candidates is a new question. Items no longer asked are `superseded`; decided items stay decided.
- **Decisions are evidence** (`kf_link_decision`, append-only, with actor, time and comment), newest per pair wins. A decision is keyed on
  (TIA item, Knowledge Fabric `entity_id`); Knowledge Fabric's ids are deterministic, so decisions survive a wipe and re-ingestion, and an id
  that changes comes back as `stale_decision`. A confirmed or chosen pair is linked as `contract_confirmed`, source `review`, strategy
  `reviewed`, and settles that domain; a rejected pair is never linked (gap `rejected_by_review`).
- **Who.** Permission `kf_review` (engineer, release manager, administrator) and read access to the item's repository. `confirm_with_alias`
  (it adds the Knowledge Fabric name to `system_aliases`, `screen_aliases` or `data_aliases`) and a product change also need `config_write`;
  they are saved as a new configuration version. `review_policy: two_person`: a positive decision waits for a second, different person
  (status `awaiting_second`); a rejection counts at once.
- **API.** `GET /api/v1/kf/reviews` (filters: status, kind, domain, product line, repository), `GET /api/v1/kf/reviews/{id}` (the question,
  both sides of the evidence, every candidate and why), `GET /api/v1/kf/reviews/{id}/history`, `POST /api/v1/kf/reviews/{id}/decision`
  (`{action, kf_entity_id?, place?, comment?}`), `POST /api/v1/kf/reviews/decisions` (up to 200). The first decision wins: a later one answers
  409. The older `POST /api/v1/kf/rule-proposals/{id}` decides the rule pair's item and also accepts `reset`. Web UI: section 5c.

### Knowledge Fabric's review screen (phase 2)

With `review_inbox: tia_and_kf` and `TIA_KF_REVIEW_URL` set, each bridge run and poll sends the open items of **mapped** services whose
candidates are Knowledge Fabric nodes (guessed, ambiguous, suggested, rule pair, stale decision) to Knowledge Fabric's review screen, and
closes the ones decided or no longer asked in TIA. Knowledge Fabric's review routes require a batch, a domain and a product line: TIA uses one
stable batch per product line, `tia-links:<TIA tenant>:<product_line_id>`, and the place from section 11. Product and story items stay in TIA.

**Knowledge Fabric 2.5 does not have what this needs.** Its review queue is filled only by its ingestion pipeline, its queue types are fixed
by a database check, and an approval runs its graph writer, which refuses anything that is not one of its types. The contract TIA implements
and asks of Knowledge Fabric (tested only against a stand-in service, `tests/test_kf_review.py`, `tests/test_kf_review_api.py`):

| Direction | Call |
|---|---|
| TIA -> KF | `POST {TIA_KF_REVIEW_URL}/hitl/reviews`: a `SOFTWARE_LINK` item (`plan_id` = TIA's item id, idempotent; scope; candidates; the question; `allowed_actions`; an execution plan with no entity operations) -> `{review_id}` |
| TIA -> KF | `POST {TIA_KF_REVIEW_URL}/hitl/reviews/{review_id}/external-decision?ingestion_batch_id=&domain_id=&product_line_id=`: `{action, selected_entity_id, reviewer_id: "tia:<actor>", notes, decided_at}`; `WITHDRAWN` when TIA no longer asks |
| KF -> TIA | `POST /api/v1/webhooks/kf-review`, signed `X-KF-Signature: sha256=<HMAC-SHA256 of the body>` with `TIA_KF_REVIEW_WEBHOOK_SECRET`: `{event_id, review_id, plan_id, ingestion_batch_id, customer_id, action, selected_entity_id, reviewer_id, notes, decided_at}` |

On Knowledge Fabric's side this means: a `SOFTWARE_LINK` queue type (enum and the check in `sql/hitl_review.sql`), the two routes above, no
graph write for these items (`write_status = NOT_REQUIRED`) but the signed callback, and a *Software links* inbox in its review UI. TIA keeps
the decision; the callback is recorded with the actor `kf:<reviewer>`, and the batch names the tenant, whose customer must match. An alias
approved there is recorded as a confirmation: adding the alias changes TIA's configuration, so only TIA does it. `review_inbox_detail:
minimal` (default) sends names, never file paths.

## 13. One question, two retrievers

Knowledge Fabric's `POST /query` takes the intent from its caller (one of nine pattern names; there is no classifier) and searches its
documents and graph. TIA's `POST /query/tia` takes a named change and returns its finished analysis. Both answer in the same `QueryResponse`.
The reference router `tia/kfrouter.py` is the step in front of them, for Knowledge Fabric's retriever to call or copy:

1. **intent**: the agent's, or phrase rules (`TRIGGERS`); fallback `any`;
2. **anchors**: exact identifiers only (issue key, pull request, commit, endpoint, test case id, and repository or service names the caller
   knows); look-alikes such as `UTF-8` or `ISO-20022` are not issue keys; TIA resolves them exactly (`POST /query/tia/resolve`);
3. **lanes**: `change_impact`, `regression_scope`, `test_coverage` and `any` ask TIA when a change TIA has analysed is named; the other
   patterns are Knowledge Fabric's; `software_tia` is TIA's alone;
4. **retrieve**: both lanes in parallel; the TIA lane has a time limit (3 s by default) and a failure is a note in `metadata.errors`, never an
   error for the question;
5. **merge**: chunks and paths of both, reranked with the caller's reranker (else the two lanes' rankings alternate, the lane that owns the
   intent first); TIA's `unknown` chunks are added after the ranked list; content is never rewritten. `metadata.lanes` says who answered.

`python tools/ask.py "What does user story PAY-4521 impact?" --tia <url> --kf <url>` runs it; `--kf-standin` answers the Knowledge Fabric
lane from a Knowledge Fabric graph in Neo4j with a stand-in (story -> requirement -> test case), for demos on the simulated graph. The stand-in
is not Knowledge Fabric's retrieval.

**What Knowledge Fabric must change** before answers are mixed for a real customer: its graph search picks its starting nodes across the whole
database, without a customer filter (`_expand_paths`); and its `any` pattern can already walk from a System across TIA's bridge edges into
TIA's code nodes, which skips TIA's per-repository permissions (the older registry links default to `IMPLEMENTS`, which its `change_impact`
pattern follows). Treat the bridge relationships (`REALIZES`, `SAME_AS`, `STORES`, `ENFORCES`, `PLACED_IN`) and every `Tia*` node as
plumbing unless a pattern opts in.

### Tests

- `tests/test_kf_products.py`, `tests/test_kf_review.py` (no server): the setting, places in the tree, scope, ties, conflicts, suggestions,
  decisions on every link kind, two-person review, the roll-up; review items and decisions; the router (intents, anchors, routing, merge, a
  slow or failing lane); the inbox contract against a stand-in service.
- `tests/test_kf_review_api.py` (PostgreSQL): the queue, permissions, the first decision winning, reset and defer, bulk decisions,
  two-person review, alias decisions, product mapping with its version check, `/query/tia` by issue key and the customer check,
  `/query/tia/resolve`, the signed callback and the inbox push.
- `tests/test_kfbridge_live_products.py` (Neo4j): the tree from `PART_OF`, a mapped repository linked and placed (also through a
  `product_line_id`), a place not found, a conflict, suggestions, a shared engine in two places, the tie-break in the graph, the roll-up in a
  report, decisions surviving a wipe, and one question answered by both lanes (TIA through its API, Knowledge Fabric by the stand-in).

Verification level: as for the rest of the bridge, against a simulated Knowledge Fabric graph; the review-screen contract against a stand-in
service; the Knowledge Fabric lane of a question against a stand-in. Not yet against real Knowledge Fabric ingestion, its review service or
its retriever.

# Neo4j mirror of the TIA graph

Written for whoever runs TIA next to Knowledge Fabric 2.5, and for KF developers who query TIA's graph with Cypher.

## What it is

TIA's graph is built, stored and walked in **PostgreSQL**, which stays the system of record (see [GRAPH-STORE.md](GRAPH-STORE.md) for
why). The **Neo4j mirror** copies the current graph version of each tenant into a Neo4j database, so that:
- KF agents can walk from a change to the entry points and written test cases it reaches, in Cypher;
- TIA nodes can be linked to the KF ontology (domain, product line, function, sub-function, requirement, process, rule).

It is "step 0" of the reviewer's list: one fixed graph structure in Knowledge Fabric's Neo4j. The code is [tia/neo4jgraph.py](../../tia/neo4jgraph.py).

## Setting it up

1. Install the driver: `pip install 'tia[neo4j]'`. The container image already includes it.
2. Point TIA at the database:

   | Variable | Meaning |
   |---|---|
   | `NEO4J_URI` | for example `bolt://kf-neo4j.internal:7687` |
   | `NEO4J_USER` | default `neo4j` |
   | `NEO4J_PASSWORD` | required: there is no default password |
   | `NEO4J_DATABASE` | optional; the server's default database otherwise |
   | `TIA_NEO4J_AUTO_SYNC` | `true` to mirror automatically every time the landscape graph advances to a new version |

   Air-gapped installations must list the Neo4j host in `TIA_AIRGAP_ALLOWED_HOSTS`.
3. Mirror the graph, in one of three ways:
   - **Automatic:** set `TIA_NEO4J_AUTO_SYNC=true`. A failed sync is written to the audit log (`neo4j_sync_failed`) and never fails the request that advanced the graph.
   - **API:** `POST /api/v1/graph/neo4j-sync`. It needs `repo_write`, and a caller who may read every repository of the tenant. Its result gives the node, relationship and test-case counts.
   - **Command line:** `tia neo4j-sync --repo NAME=PATH ... [--tenant-id ID]`.

For a local Neo4j on a developer machine:

```
docker run -d --name tia-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/<choose-a-password> neo4j:5.26.4-community
```

## What is written

| In Neo4j | From TIA |
|---|---|
| `(:TiaNode:Tia<Type>)` | Every graph node, with its TIA type as a label: `TiaService`, `TiaEndpoint`, `TiaFunction`, `TiaCallsite`, `TiaSchema`, `TiaTest`, `TiaTable`, `TiaTopic`, and so on. Properties: `key` (`<tenant>\|<id>`, unique), `id`, `tenant`, `graph_version`, `type`, `name`, `repo`, `service`, `file`, `line`; endpoints also have `method`, `route` and `path`. |
| `-[:CALLS]->`, `-[:HANDLES]->`, `-[:CONSUMES_API]->`, `-[:TESTS]->`, `-[:READS]->`, ... | Every TIA edge, with TIA's own name and direction (consumer to provider). Properties: `status` (the evidence status, never upgraded), `strategy`, `rule`, `file`, `line`, `tia: true`. |
| `-[:IMPACTS {via}]->` | The same edges in the direction a change travels, which is what TIA's impact walk follows. "What does a change to X reach" becomes one pattern. |
| `(:TiaNode:TiaTestCase)-[:COVERS]->(:TiaEndpoint)` | The written test cases (`testcases/*.csv\|yaml\|json`) and the endpoints they test: named in the case, or called by the screen it names (`via_page`). `status` is `source_confirmed` when the case's *Entry point* column names it, `inferred` when it is matched from the text. |
| `(:TiaGraphVersion)` | One per tenant: the graph version mirrored, when, and the counts. |

The `Tia` prefix keeps these nodes apart from the KF ontology's own labels. KF has a business *Function*, which is not a code function.

Each sync updates the nodes and relationships it writes, then deletes that tenant's TIA nodes and relationships from other graph versions.
Neo4j therefore always holds the **current** landscape, while PostgreSQL keeps every version. Nothing outside the `Tia*` labels and
`tia: true` relationships is touched.

## Use case 1 in Cypher: which test cases a change reaches

```cypher
MATCH (c:TiaNode {tenant: $tenant}) WHERE c.id IN $changed          // e.g. ['payments-app:app.py::balance_of']
MATCH p = (c)-[:IMPACTS*0..8]->(e:TiaEndpoint)
WHERE all(n IN nodes(p) WHERE NOT n:TiaTest)
WITH DISTINCT e, min(length(p)) AS hops
MATCH (tc:TiaTestCase {tenant: $tenant})-[cov:COVERS]->(e)
RETURN tc.case_id, tc.title, e.method + ' ' + coalesce(e.route, e.path) AS entry_point, hops, cov.status, cov.via_page
```

`tia.neo4jgraph.regression_tests(driver, tenant, changed)` runs this query. On the regression demo it returns exactly the test cases TIA's
own engine selects, for all three feature branches (`tests/test_neo4jgraph.py`). For `balance_of` that is TC-01, TC-02, TC-03 (through the
Pay screen) and TC-07.

## Automation and API testing in Cypher

The screens, elements, locators, requests, responses, fields and business rules of [GRAPH-SCHEMA.md](GRAPH-SCHEMA.md) are mirrored too.

**Elements of a screen, with the locator to use and the API the screen calls:**

```cypher
MATCH (:TiaApplication {tenant: $tenant})-[:HAS_SCREEN]->(sc:TiaScreen {route: '/pay'})-[:HAS_ELEMENT]->(el:TiaElement)
MATCH (el)-[:LOCATED_BY]->(l:TiaLocator {best: true})
OPTIONAL MATCH (sc)-[:CALLS_API]->(ep:TiaEndpoint)
RETURN el.name, el.mandatory, el.options, l.css, l.xpath, l.playwright, ep.method + ' ' + ep.route AS calls
```

**What a request needs, and what the response can be:**

```cypher
MATCH (ep:TiaEndpoint {tenant: $tenant, method: 'POST', route: '/payments'})-[:ACCEPTS]->(:TiaRequest)-[:HAS_FIELD]->(f:TiaField)
RETURN f.name, f.in, f.field_type, f.required, f.allowed_values, f.min_length, f.max_length, f.minimum, f.maximum, f.pattern

MATCH (:TiaEndpoint {tenant: $tenant, method: 'POST', route: '/payments'})-[:RETURNS]->(r:TiaResponse)
RETURN r.success_status, r.error_statuses, r.schema
```

**Business rules and where they apply:**

```cypher
MATCH (f:TiaFunction {tenant: $tenant})-[:APPLIES_RULE]->(r:TiaRule) RETURN f.name, r.expr, r.fields, r.file, r.line
```

`tests/test_neo4jgraph.py` runs these queries against a real Neo4j.

## Linking to the Knowledge Fabric ontology

> **Use the bridge for Knowledge Fabric 2.5.** [KF-BRIDGE.md](KF-BRIDGE.md) links services to Knowledge Fabric's real `System` nodes and test cases to its `TestCase` nodes, with Knowledge Fabric's own entity ids, per customer, kept current automatically. The explicit mapping below remains for other graphs and for one-off links; the label names in its examples (`SystemExtract`, `SubFunction`) are placeholders that Knowledge Fabric 2.5 does not have.

Links are created **only from an explicit mapping**; TIA does not guess KF's schema. The mapping is a CSV with two columns:
- `tia`: an endpoint as `METHOD /route`, a business rule by its expression (`not p['disputed']`), a screen by its name, or a TIA node id;
- `kf`: the key value of the KF node.

```
tia neo4j-sync --repo payments-app=. --kf-links links.csv --kf-label SystemExtract --kf-key extract_id
```

This writes `(:TiaEndpoint)-[:IMPLEMENTS {kf_link: true}]->(:SystemExtract {extract_id: ...})` for every pair whose two ends exist. Change
the relationship type with `--kf-rel`. Pairs that cannot be linked are returned with the reason, for example:
- "no TIA node matches";
- "more than one TIA node matches";
- "no :SystemExtract node with extract_id = ...".

Links survive re-syncs while both ends exist.

The KF label and key names (`SystemExtract`, `SubFunction`, `Rule`, `TestCase` and their properties) must come from the KF team; the
example above uses placeholders. Once KF links its own test cases to sub-functions, the query above can be extended:

endpoint → `IMPLEMENTS` → system extract → sub-function → KF test case.

## Linking to Knowledge Fabric documents: the API registry and test cases

Knowledge Fabric ingests documents that live outside the code. An API registry spreadsheet becomes registry nodes, and test cases become
chunks in Weaviate plus nodes in Neo4j. TIA links its graph to those nodes on **shared identifiers only**, never on similar wording. This
answers the review of 17 Nov 2026. The code is [tia/kflinks.py](../../tia/kflinks.py) and
the writers in [tia/neo4jgraph.py](../../tia/neo4jgraph.py).

| Knowledge Fabric node | Joined to | On | Relationship |
|---|---|---|---|
| API registry entry | `TiaEndpoint` | service, HTTP method and normalised path | `(:TiaEndpoint)-[:IMPLEMENTS {source: 'api_registry', status, strategy, registry_row}]->(:<registry label>)` |
| Test case | `TiaTestCase` | the test case id (`NW-02`), trimmed | `(:TiaTestCase)-[:SAME_AS {source: 'test_case_id'}]->(:<test case label>)` |

### How a registry row is matched

The registry can be a CSV or XLSX file, or Knowledge Fabric's own registry nodes. Columns are recognised by their header: for example
*API ID*, *Service Name*, *HTTP Method*, *Endpoint*. Name a column with `--column key=<header>` when its header is not recognised. A path
may be a full URL (the host is dropped) or carry its method (`GET /a/{id}`). Every row gets exactly one outcome:

| Outcome | When | Linked? |
|---|---|---|
| `confirmed` | The row's service is an analysed service, or a declared alias of one, and exactly one of its endpoints has the row's method and normalised path. | Yes |
| `inferred` | Exactly one endpoint matches, on weaker ground, and the reason says which: the service is missing or unknown and the path is unique in the landscape; the row names no method; or the code's path is the end of the registry path (a gateway prefix such as `/payments-api/v1`). | Yes, marked `inferred` (`--confirmed-only` leaves them out) |
| `ambiguous` | More than one endpoint matches. The candidates are listed. | No |
| `no_match` | No endpoint matches. The reason says what was looked for and what exists, e.g. "payments-api has /payments/{} only as GET, not DELETE". | No |
| `invalid` | The row has no key or no path. | No |

Endpoints in the code that no row names are listed as `unregistered`: APIs missing from the registry.

A **service alias** file (columns `registry`, `tia`) says that the registry's "Payment Service" is TIA's `payments-api`. It turns
"unknown service, unique path" rows into confirmed ones.

Registry links follow the latest registry: links of the tenant that the newest match no longer has are removed.

### Try it on the Northwind Pay showcase

`fixtures/kf/northwind-api-registry.csv` exercises every outcome. To check the registry without Neo4j:

```
tia kf-registry --repo payments-api=showcase/repos/payments-api --repo statement-service=showcase/repos/statement-service \
    --repo mobile-bff=showcase/repos/mobile-bff --registry fixtures/kf/northwind-api-registry.csv \
    [--aliases fixtures/kf/northwind-service-aliases.csv] [--format json]
```

Without aliases this gives 3 confirmed, 3 inferred, 2 not matched and 1 invalid, plus 2 unregistered endpoints (`GET /fx/rates`, the
mobile activity API). With the alias file it gives 4 confirmed and 2 inferred.

To mirror and link in one run, with Knowledge Fabric's real labels and key properties in place of the placeholders:

```
tia neo4j-sync --repo ... --registry fixtures/kf/northwind-api-registry.csv --registry-label ApiRegistryEntry --registry-key api_id \
    --aliases fixtures/kf/northwind-service-aliases.csv --testcase-label TestCase --testcase-key tc_id
```

If Knowledge Fabric already holds the registry as nodes, leave out `--registry` and name their properties instead:
`--registry-path-prop path --registry-method-prop method --registry-service-prop service`.

The same is available over HTTP:
- `POST /api/v1/graph/registry-match` (permission `read`): matching only, nothing written, only the repositories the caller may read.
- `POST /api/v1/graph/kf-links` (permission `repo_write`): mirror, then link.

In the Python SDK these are `registry_match` and `kf_links`.

### The traversal the review asked for: "help me automate this test case"

A Weaviate search finds the test case's chunk. Its node leads into TIA's graph, and from there to the API, its registry entry, the request
payload with its rules, and the screen with its locators. Labels other than `Tia*` are placeholders.

```cypher
MATCH (:TestCase {tc_id: 'NW-02'})<-[:SAME_AS]-(tc:TiaTestCase {tenant: $tenant})-[:COVERS]->(ep:TiaEndpoint)
OPTIONAL MATCH (ep)-[:IMPLEMENTS]->(api:ApiRegistryEntry)
OPTIONAL MATCH (ep)-[:ACCEPTS]->(:TiaRequest)-[:HAS_FIELD]->(f:TiaField)
OPTIONAL MATCH (sc:TiaScreen)-[:CALLS_API]->(ep)
OPTIONAL MATCH (sc)-[:HAS_ELEMENT]->(el:TiaElement)-[:LOCATED_BY]->(l:TiaLocator {best: true})
RETURN tc.title, ep.method + ' ' + ep.route AS entry_point, collect(DISTINCT api.api_id) AS registry,
       collect(DISTINCT f.name) AS payload, collect(DISTINCT sc.route) AS screens, collect(DISTINCT l.playwright) AS locators
```

On the showcase this returns `POST /payments`, registry entry `API-002`, the payment fields, the `/pay` screen and its locators.
`tests/test_kflinks.py` runs it against a real Neo4j.

### Still needed from the Knowledge Fabric team

- ~~the real labels and key properties of their registry and test-case nodes~~: read from the Knowledge Fabric 2.5 source and used by the bridge ([KF-BRIDGE.md](KF-BRIDGE.md)); Knowledge Fabric has no API-registry node type, so registry links still use a CSV/XLSX or whatever label is configured;
- whether their Weaviate chunks carry the id of the node or document they came from (a test case chunk needs its test case id);
- access to their Neo4j 2.5 instance.

## Security

- The mirror holds **every repository of the tenant**, and Neo4j does not apply TIA's per-repository visibility. Grant access to the Neo4j database only to principals and agents that may read the whole landscape. The API refuses a sync started by a caller who cannot read every repository.
- Every node and relationship carries `tenant`. When tenants share one Neo4j database, give each tenant's agents a database or role that filters on it, or use one database per tenant (`NEO4J_DATABASE`).
- Values already kept out of TIA's records (secrets in source, configuration values) are not in the graph, so they are not in the mirror.

## Tests

`tests/test_neo4jgraph.py` tests the projection without a server. With `TIA_TEST_NEO4J_URI` and `TIA_TEST_NEO4J_PASSWORD` set, it also tests
against a real Neo4j:
- the sync;
- the Cypher regression query against the engine;
- re-sync and stale removal;
- tenant isolation;
- ontology links;
- the API.

CI starts `neo4j:5.26.4-community`, pinned by digest, as a service for these tests.

# TIA graph schema

*Generated from [tia/graphschema.py](../../tia/graphschema.py) by `tools/gen_graph_schema.py`; do not edit by hand (a test fails when it is out of
date). Schema version `gs-3`.*

Written for the people who decide how TIA's knowledge is represented, and for anyone who queries it (Knowledge Fabric agents, Cypher, the
API). This is the fixed representation every use case is answered from (regression selection, automation, API testing, execution flow: see [USER-GUIDE.md](USER-GUIDE.md)). It changes only by a
deliberate, versioned decision: a change to [tia/graphschema.py](../../tia/graphschema.py) bumps the schema version, which gives every graph a
new version id.

## Where it is stored

* **PostgreSQL** is the system of record: each graph version is immutable and content-addressed, and it is stored with the repository
  commits it was built from ([GRAPH-STORE.md](GRAPH-STORE.md)). The impact engine walks it in memory.
* **Neo4j** holds a mirror of each tenant's current version for Knowledge Fabric ([NEO4J.md](NEO4J.md)). A node type `x` becomes the
  label `TiaX`, and relationships keep the names below.
* **Enforcement:** `tests/test_graphschema.py` builds the graph of every demo landscape and fails on any node type or relationship this
  schema does not allow.

## The picture

```
Service ─EXPOSES→ Endpoint ←HANDLES─ Function ─CALLS→ Function ─READS/WRITES→ Entity / Table / Column
   │                 │  ↑                │   └─APPLIES_RULE→ Rule
   │                 │  └─CONSUMES_API── Callsite ←ISSUES─ Function (another service)
   │                 ├─ACCEPTS→ Request ─HAS_FIELD→ Field
   │                 ├─RETURNS→ Response ─HAS_FIELD→ Field
   │                 └─USES_SCHEMA→ Schema
   └─HAS_APPLICATION→ Application ─HAS_SCREEN→ Screen ─HAS_ELEMENT→ Element ─LOCATED_BY→ Locator
                                                 └─CALLS_API→ Endpoint
Test ─TESTS→ Endpoint / Function          (Neo4j adds) TestCase ─COVERS→ Endpoint,  Endpoint/Rule ─IMPLEMENTS→ Knowledge Fabric node
```

## How a change travels (impact)

Only relationships with an impact direction carry a change. A relationship marked *not traversed* is structure that use cases read
(screens, fields, rules). Adding such structure never changes an impact result.


## Node types

### Code and services

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `service` | `TiaService` | A deployable service (one per analysed repository). | `service`, `repo` |
| `endpoint` | `TiaEndpoint` | An HTTP API entry point a service exposes. | `method`, `route`, `path`, `repo`, `service`, `file`, `line` |
| `function` | `TiaFunction` | A function or method (qualified name). | `name`, `repo`, `service`, `file`, `line` |
| `type` | `TiaType` | A class, interface or record (for inheritance). | `name`, `kind`, `repo`, `file`, `line` |
| `schema` | `TiaSchema` | A request or response body type declared in code or OpenAPI. | `name`, `repo`, `file`, `line` |
| `callsite` | `TiaCallsite` | An outbound HTTP call in code (the consumer side of CONSUMES_API). | `repo`, `file`, `line`; when declared: `client_pkg`, `client_attr` |
| `test` | `TiaTest` | An automated test in a repository. | `name`, `repo`, `file`, `line` |
| `contract_ref` | `TiaContractRef` | A consumer contract declared in a repository whose code does not show the call. | `repo`, `service`, `file` |
| `runtime_ref` | `TiaRuntimeRef` | A caller seen only in runtime telemetry (added to an analysis's own copy of the graph). | `service`, `repo` |

### Data

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `entity` | `TiaEntity` | An ORM-mapped class (JPA, SQLAlchemy, Django). | `name`, `orm`, `table`, `database`, `repo`, `service`, `file`, `line` |
| `table` | `TiaTable` | A database table. | `name`, `database` |
| `column` | `TiaColumn` | A table column. | `database`, `table`, `column` |
| `migration` | `TiaMigration` | A schema migration (Flyway, Liquibase, Alembic, Django, SQL). | `name`, `tool`, `repo`, `file` |
| `procedure` | `TiaProcedure` | A stored procedure, view or trigger. | `name`, `kind`, `database` |

### Events

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `topic` | `TiaTopic` | A message topic (Kafka). | `name` |
| `event_schema` | `TiaEventSchema` | The schema of the messages on a topic. | `name`, `repo`, `file`, `line` |
| `listener` | `TiaListener` | Code that consumes a topic. | `name`, `repo`, `file`, `line` |

### Automation: Application > Screen > Element > Locator

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `application` | `TiaApplication` | The front end of a repository (all of its screens). | `name`, `frameworks`, `repo`, `service` |
| `screen` | `TiaScreen` | A page or screen: its route, component and framework. | `name`, `route`, `component`, `framework`, `repo`, `file` |
| `element` | `TiaElement` | A UI element on a screen: tag and type, label, mandatory, input rules, dropdown values. *(many per parent: left out of overview pictures)* | `name`, `tag`, `input_type`, `label`, `mandatory`, `mandatory_source`, `repeated`, `repo`, `file`, `line`; when declared: `options`, `options_source`, `max_length`, `min_length`, `minimum`, `maximum`, `exclusive_minimum`, `exclusive_maximum`, `pattern`, `format` |
| `locator` | `TiaLocator` | One way to find an element when the application runs: CSS, XPath and Playwright forms, whether it is unique, and whether it is the one to use. *(many per parent: left out of overview pictures)* | `strategy`, `css`, `xpath`, `playwright`, `unique`, `best`, `rank` |

### API testing: Endpoint > Request / Response > Field

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `request` | `TiaRequest` | What an endpoint accepts: its parameters and request body. | `name`, `body_schema`, `repo`, `service` |
| `response` | `TiaResponse` | What an endpoint answers: body schema, success status, shape, error statuses. | `name`, `schema`, `success_status`, `shape`, `error_statuses`, `repo`, `service` |
| `field` | `TiaField` | One input or output field with its definition: where it goes (path, query, header, body), type, required, allowed values and limits. *(many per parent: left out of overview pictures)* | `name`, `in`, `field_type`, `required`, `multi`, `nullable`, `repo`, `service`; when declared: `allowed_values`, `max_length`, `min_length`, `minimum`, `maximum`, `exclusive_minimum`, `exclusive_maximum`, `pattern`, `format` |
| `example` | `TiaExample` | An example or expected response: from the OpenAPI specification, or what a test asserts about the response (partial when the test checks only some fields). The body is JSON text. *(many per parent: left out of overview pictures)* | `name`, `status`, `body_json`, `partial`, `source`, `repo`, `service`, `file`, `line`; when declared: `test` |

### Business rules

| Type | Neo4j label | Meaning | Properties |
|---|---|---|---|
| `rule` | `TiaRule` | A business rule found in code: a condition a function applies to the data it handles ("disputed payments are left out"). The link to a Knowledge Fabric rule is made by mapping (docs/NEO4J.md). *(many per parent: left out of overview pictures)* | `name`, `expr`, `fields`, `function`, `repo`, `service`, `file`, `line` |

## Relationships

| Relationship | From | To | Impact travels | Meaning |
|---|---|---|---|---|
| `EXPOSES` | `service` | `endpoint` | not traversed | A service exposes the endpoint. |
| `HANDLES` | `function` | `endpoint` | source → target | The function handles requests to the endpoint. |
| `CALLS` | `function` | `function`, `procedure` | target → source | A function calls another function or a stored procedure. |
| `INHERITS` | `type` | `type` | target → source | A type extends or implements another. |
| `ISSUES` | `function` | `callsite`, `listener` | target → source | A function makes an outbound call or registers a listener. |
| `CONSUMES_API` | `callsite`, `contract_ref`, `runtime_ref` | `endpoint` | target → source | A consumer calls the endpoint (resolved call, declared contract or runtime observation). |
| `USES_SCHEMA` | `endpoint`, `topic` | `event_schema`, `schema` | target → source | An endpoint or topic uses the schema for its messages. |
| `TESTS` | `test` | `endpoint`, `function` | target → source | An automated test exercises the endpoint or function. |
| `PUBLISHES` | `function`, `service` | `topic` | source → target | Code publishes to the topic. |
| `CONSUMES_EVENT` | `listener` | `topic` | target → source | A listener consumes the topic. |
| `MAPS_TO` | `entity` | `table` | target → source | An entity maps the table. |
| `MAPS_COLUMN` | `entity` | `column` | target → source | An entity maps the column. |
| `READS` | `function`, `procedure` | `column`, `entity`, `table` | target → source | Code reads the data. |
| `WRITES` | `function`, `procedure` | `column`, `entity`, `table` | target → source | Code writes the data. |
| `MIGRATES` | `migration` | `table` | not traversed | A migration changes the table. |
| `HAS_APPLICATION` | `service` | `application` | not traversed | The service's repository has this front end. |
| `HAS_SCREEN` | `application` | `screen` | not traversed | The application has the screen. |
| `HAS_ELEMENT` | `screen` | `element` | not traversed | The screen shows the element. |
| `LOCATED_BY` | `element` | `locator` | not traversed | One way to find the element (best first: rank 1). |
| `NAVIGATES_TO` | `screen` | `screen` | not traversed | The screen sends the user to another screen (a link, or a navigation call in its code). |
| `CALLS_API` | `screen` | `endpoint` | not traversed | The screen calls the endpoint (matched by method and path; inferred when more than one endpoint matches). |
| `ACCEPTS` | `endpoint` | `request` | not traversed | The endpoint's request. |
| `RETURNS` | `endpoint` | `response` | not traversed | The endpoint's response. |
| `HAS_FIELD` | `request`, `response` | `field` | not traversed | A field of the request or response. |
| `HAS_EXAMPLE` | `response` | `example` | not traversed | An example or expected response. |
| `APPLIES_RULE` | `function` | `rule` | not traversed | The function applies the business rule. |

Every relationship also records its evidence: `status` (`source_confirmed`, `contract_confirmed`, `runtime_observed`, `inferred`, ...), the `strategy` that found it, the inference `rule` when it is inferred, and the repository, file, line and commit it came from.

## How each use case reads it

| Use case | Path through the graph |
|---|---|
| **Blast radius and regression tests** (use case 1) | changed Function, following impact relationships (Neo4j: `IMPACTS`), to an Endpoint; written TestCase `COVERS` Endpoint. See [NEO4J.md](NEO4J.md). |
| **Automation script for a flow** "screen A to screen B" | Application `HAS_SCREEN` Screen A and Screen B; each Screen `HAS_ELEMENT` Element (mandatory, rules, dropdown values); each Element `LOCATED_BY` Locator with `best = true` (rank 1 is tried first); Screen `CALLS_API` Endpoint for the requests the flow sends. |
| **API test cases / calling an API** | Endpoint `ACCEPTS` Request `HAS_FIELD` Field (where it goes, type, required, allowed values, limits); Endpoint `RETURNS` Response (success status, error statuses, shape) `HAS_FIELD` Field. |
| **Business rules** | Function `APPLIES_RULE` Rule (the condition and the fields it tests). Endpoint `HANDLES`/`CALLS` paths show which entry points a rule sits behind. |
| **Knowledge Fabric ontology** | an Endpoint, Rule or Screen `IMPLEMENTS` a KF node, from an explicit mapping only ([NEO4J.md](NEO4J.md)). The KF test case and sub-function are then reached from there. |

## What is not in it (yet)

* **Expected response examples:** OpenAPI `example(s)` and response bodies from tests are not captured; a Response has its schema, success status and error statuses.
* **The order of screens in a flow:** a flow "A to B" is given by the person or agent asking. Navigation between screens is not extracted.
* **Rules linked to the fields of a specific endpoint:** a Rule names the fields it tests; linking them to Field nodes needs the endpoint whose data it filters, which the rule-gap analysis works out per use (tia.testscope).
* **Values filled at run time:** dropdown values or element ids built by code are marked dynamic, never guessed.

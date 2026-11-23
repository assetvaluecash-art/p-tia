"""The TIA graph schema: every node type and relationship type, what each relationship connects, and which ones a change travels along.

This is the representation Pawan asked to be fixed once (docs/USER-GUIDE.md): each use case is answered from it, and it changes only by
a deliberate, versioned decision. tests/test_graphschema.py checks every graph the fixtures build against it, and docs/GRAPH-SCHEMA.md is
generated from it (python tools/gen_graph_schema.py), so the code, the check and the document cannot drift apart.

`impact` says how a change travels along a relationship in TIA's impact walk (tia.graph.Graph.add_edge):
  "dst->src"  a change to the target reaches the source (a callee change reaches its caller)
  "src->dst"  a change to the source reaches the target (a handler change reaches its endpoint)
  None        not traversed: structure and detail that use cases read, but that does not carry impact
"""
from __future__ import annotations

GRAPH_SCHEMA_VERSION = "gs-3"        # part of every graph version's identity (tia.graphstore): a schema change builds new versions
LIMITS = ("max_length", "min_length", "minimum", "maximum", "exclusive_minimum", "exclusive_maximum", "pattern", "format")

# group: what the node belongs to, for the document. detail: many per screen/endpoint; overview views leave them out unless asked.
NODE_TYPES = {
    # ---- code and services ------------------------------------------------------------------------------------------------------------
    "service": {"group": "code", "description": "A deployable service (one per analysed repository).",
                "properties": ["service", "repo"]},
    "endpoint": {"group": "code", "description": "An HTTP API entry point a service exposes.",
                 "properties": ["method", "route", "path", "repo", "service", "file", "line"]},
    "function": {"group": "code", "description": "A function or method (qualified name).", "properties": ["name", "repo", "service", "file", "line"]},
    "type": {"group": "code", "description": "A class, interface or record (for inheritance).", "properties": ["name", "kind", "repo", "file", "line"]},
    "schema": {"group": "code", "description": "A request or response body type declared in code or OpenAPI.", "properties": ["name", "repo", "file", "line"]},
    "callsite": {"group": "code", "description": "An outbound HTTP call in code (the consumer side of CONSUMES_API).", "properties": ["repo", "file", "line"],
                 "optional": ["client_pkg", "client_attr"]},
    "test": {"group": "code", "description": "An automated test in a repository.", "properties": ["name", "repo", "file", "line"]},
    "contract_ref": {"group": "code", "description": "A consumer contract declared in a repository whose code does not show the call.",
                     "properties": ["repo", "service", "file"]},
    "runtime_ref": {"group": "code", "description": "A caller seen only in runtime telemetry (added to an analysis's own copy of the graph).",
                    "properties": ["service", "repo"]},
    # ---- data and events --------------------------------------------------------------------------------------------------------------
    "entity": {"group": "data", "description": "An ORM-mapped class (JPA, SQLAlchemy, Django).",
               "properties": ["name", "orm", "table", "database", "repo", "service", "file", "line"]},
    "table": {"group": "data", "description": "A database table.", "properties": ["name", "database"]},
    "column": {"group": "data", "description": "A table column.", "properties": ["database", "table", "column"]},
    "migration": {"group": "data", "description": "A schema migration (Flyway, Liquibase, Alembic, Django, SQL).", "properties": ["name", "tool", "repo", "file"]},
    "procedure": {"group": "data", "description": "A stored procedure, view or trigger.", "properties": ["name", "kind", "database"]},
    "topic": {"group": "events", "description": "A message topic (Kafka).", "properties": ["name"]},
    "event_schema": {"group": "events", "description": "The schema of the messages on a topic.", "properties": ["name", "repo", "file", "line"]},
    "listener": {"group": "events", "description": "Code that consumes a topic.", "properties": ["name", "repo", "file", "line"]},
    # ---- automation: Application > Screen > Element > Locator ------------------------------------------------------------------------
    "application": {"group": "automation", "description": "The front end of a repository (all of its screens).",
                    "properties": ["name", "frameworks", "repo", "service"]},
    "screen": {"group": "automation", "description": "A page or screen: its route, component and framework.",
               "properties": ["name", "route", "component", "framework", "repo", "file"]},
    "element": {"group": "automation", "detail": True,
                "description": "A UI element on a screen: tag and type, label, mandatory, input rules, dropdown values.",
                "properties": ["name", "tag", "input_type", "label", "mandatory", "mandatory_source", "repeated", "repo", "file", "line"],
                "optional": ["options", "options_source", *LIMITS]},
    "locator": {"group": "automation", "detail": True,
                "description": "One way to find an element when the application runs: CSS, XPath and Playwright forms, whether it is unique, "
                               "and whether it is the one to use.",
                "properties": ["strategy", "css", "xpath", "playwright", "unique", "best", "rank"]},
    # ---- API testing: Endpoint > Request / Response > Field --------------------------------------------------------------------------
    "request": {"group": "api", "description": "What an endpoint accepts: its parameters and request body.",
                "properties": ["name", "body_schema", "repo", "service"]},
    "response": {"group": "api", "description": "What an endpoint answers: body schema, success status, shape, error statuses.",
                 "properties": ["name", "schema", "success_status", "shape", "error_statuses", "repo", "service"]},
    "field": {"group": "api", "detail": True,
              "description": "One input or output field with its definition: where it goes (path, query, header, body), type, required, "
                            "allowed values and limits.",
              "properties": ["name", "in", "field_type", "required", "multi", "nullable", "repo", "service"],
              "optional": ["allowed_values", *LIMITS]},
    "example": {"group": "api", "detail": True,
                "description": "An example or expected response: from the OpenAPI specification, or what a test asserts about the response "
                               "(partial when the test checks only some fields). The body is JSON text.",
                "properties": ["name", "status", "body_json", "partial", "source", "repo", "service", "file", "line"], "optional": ["test"]},
    # ---- business rules --------------------------------------------------------------------------------------------------------------
    "rule": {"group": "rules", "detail": True,
             "description": "A business rule found in code: a condition a function applies to the data it handles (\"disputed payments "
                            "are left out\"). The link to a Knowledge Fabric rule is made by mapping (docs/NEO4J.md).",
             "properties": ["name", "expr", "fields", "function", "repo", "service", "file", "line"]},
}

# rel: from types, to types, impact direction, meaning
EDGE_TYPES = {
    "EXPOSES": {"from": {"service"}, "to": {"endpoint"}, "impact": None, "description": "A service exposes the endpoint."},
    "HANDLES": {"from": {"function"}, "to": {"endpoint"}, "impact": "src->dst", "description": "The function handles requests to the endpoint."},
    "CALLS": {"from": {"function"}, "to": {"function", "procedure"}, "impact": "dst->src", "description": "A function calls another function or a stored procedure."},
    "INHERITS": {"from": {"type"}, "to": {"type"}, "impact": "dst->src", "description": "A type extends or implements another."},
    "ISSUES": {"from": {"function"}, "to": {"callsite", "listener"}, "impact": "dst->src", "description": "A function makes an outbound call or registers a listener."},
    "CONSUMES_API": {"from": {"callsite", "contract_ref", "runtime_ref"}, "to": {"endpoint"}, "impact": "dst->src",
                     "description": "A consumer calls the endpoint (resolved call, declared contract or runtime observation)."},
    "USES_SCHEMA": {"from": {"endpoint", "topic"}, "to": {"schema", "event_schema"}, "impact": "dst->src",
                    "description": "An endpoint or topic uses the schema for its messages."},
    "TESTS": {"from": {"test"}, "to": {"endpoint", "function"}, "impact": "dst->src", "description": "An automated test exercises the endpoint or function."},
    "PUBLISHES": {"from": {"function", "service"}, "to": {"topic"}, "impact": "src->dst", "description": "Code publishes to the topic."},
    "CONSUMES_EVENT": {"from": {"listener"}, "to": {"topic"}, "impact": "dst->src", "description": "A listener consumes the topic."},
    "MAPS_TO": {"from": {"entity"}, "to": {"table"}, "impact": "dst->src", "description": "An entity maps the table."},
    "MAPS_COLUMN": {"from": {"entity"}, "to": {"column"}, "impact": "dst->src", "description": "An entity maps the column."},
    "READS": {"from": {"function", "procedure"}, "to": {"entity", "table", "column"}, "impact": "dst->src", "description": "Code reads the data."},
    "WRITES": {"from": {"function", "procedure"}, "to": {"entity", "table", "column"}, "impact": "dst->src", "description": "Code writes the data."},
    "MIGRATES": {"from": {"migration"}, "to": {"table"}, "impact": None, "description": "A migration changes the table."},
    # automation
    "HAS_APPLICATION": {"from": {"service"}, "to": {"application"}, "impact": None, "description": "The service's repository has this front end."},
    "HAS_SCREEN": {"from": {"application"}, "to": {"screen"}, "impact": None, "description": "The application has the screen."},
    "HAS_ELEMENT": {"from": {"screen"}, "to": {"element"}, "impact": None, "description": "The screen shows the element."},
    "LOCATED_BY": {"from": {"element"}, "to": {"locator"}, "impact": None, "description": "One way to find the element (best first: rank 1)."},
    "NAVIGATES_TO": {"from": {"screen"}, "to": {"screen"}, "impact": None,
                     "description": "The screen sends the user to another screen (a link, or a navigation call in its code)."},
    "CALLS_API": {"from": {"screen"}, "to": {"endpoint"}, "impact": None,
                  "description": "The screen calls the endpoint (matched by method and path; inferred when more than one endpoint matches)."},
    # API testing
    "ACCEPTS": {"from": {"endpoint"}, "to": {"request"}, "impact": None, "description": "The endpoint's request."},
    "RETURNS": {"from": {"endpoint"}, "to": {"response"}, "impact": None, "description": "The endpoint's response."},
    "HAS_FIELD": {"from": {"request", "response"}, "to": {"field"}, "impact": None, "description": "A field of the request or response."},
    "HAS_EXAMPLE": {"from": {"response"}, "to": {"example"}, "impact": None, "description": "An example or expected response."},
    # rules
    "APPLIES_RULE": {"from": {"function"}, "to": {"rule"}, "impact": None, "description": "The function applies the business rule."},
}

TRAVERSED_SRC_TO_DST = tuple(r for r, e in EDGE_TYPES.items() if e["impact"] == "src->dst")
TRAVERSED_DST_TO_SRC = tuple(r for r, e in EDGE_TYPES.items() if e["impact"] == "dst->src")
DETAIL_TYPES = frozenset(t for t, n in NODE_TYPES.items() if n.get("detail"))
STRUCTURE_TYPES = frozenset(t for t, n in NODE_TYPES.items() if n["group"] in ("automation", "api", "rules"))


def violations(g) -> list:
    """Every node or relationship of a graph that the schema does not allow (an empty list means the graph conforms)."""
    out = []
    for nid, a in g.nodes.items():
        if a.get("type") not in NODE_TYPES:
            out.append(f"node {nid}: unknown type {a.get('type')!r}")
    for e in g.edges:
        spec = EDGE_TYPES.get(e.rel)
        st, dt = (g.nodes.get(e.src) or {}).get("type"), (g.nodes.get(e.dst) or {}).get("type")
        if spec is None:
            out.append(f"edge {e.src} -{e.rel}-> {e.dst}: unknown relationship")
        elif st not in spec["from"] or dt not in spec["to"]:
            out.append(f"edge {e.rel}: {st} -> {dt} is not allowed (allowed: {sorted(spec['from'])} -> {sorted(spec['to'])})")
    return out

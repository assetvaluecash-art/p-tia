"""Use-case structure in the graph (docs/GRAPH-SCHEMA.md): screens, elements and locators for automation; requests, responses and fields for
API testing; business rules found in code. The facts are the ones the extractors already produce (tia.extract_ui pages, schemas and
endpoint parameters, function filters); this module stores them as nodes and relationships so a retriever can walk them.

None of these relationships carries impact: adding them changes no analysis result. Values a UI loads at run time stay "dynamic" as the
extractor reports them; nothing is guessed.
"""
from __future__ import annotations

import json

from .graphschema import LIMITS
from .models import normalize_path
from .models import INFERRED, SOURCE_CONFIRMED, Edge


def _limits(constraints: dict) -> dict:
    """Constraint values as flat scalar properties (a graph store keeps no nested maps)."""
    out = {}
    for k, v in (constraints or {}).items():
        if k in LIMITS and isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif k in LIMITS:
            out[k] = json.dumps(v, sort_keys=True, default=str)
    return out


def _options(el: dict) -> list:
    out = []
    for o in el.get("options", []):
        v = o.get("value")
        if v == "":
            continue                                    # the "Choose ..." placeholder is not a value
        v = v if v is not None else o.get("text")
        if v not in (None, "") and str(v) not in out:
            out.append(str(v))
    return out


def _statuses(ex, handler_id: str) -> list:
    """Error statuses the handler (and the functions it calls, resolved by unique name) answers with."""
    by_id = {f.id: f for f in ex.functions}
    by_name: dict = {}
    for f in ex.functions:
        by_name.setdefault(f.qualname.split(".")[-1], []).append(f)
    seen, todo, out = {handler_id}, [handler_id], set()
    while todo:
        f = by_id.get(todo.pop())
        if f is None:
            continue
        out |= {int(s["status"]) for s in f.statuses if str(s.get("status", "")).isdigit()}
        for c in f.calls:
            cands = by_name.get(c, [])
            if len(cands) == 1 and cands[0].id not in seen:
                seen.add(cands[0].id)
                todo.append(cands[0].id)
    return sorted(out)


def add_structure(g, extracts: dict):
    endpoints_by_key: dict = {}
    for ex in extracts.values():
        for e in ex.endpoints:
            endpoints_by_key.setdefault((e.method, e.norm_path), []).append(e.id)
    for repo, ex in sorted(extracts.items()):
        ev = lambda file, line, repo=repo, ex=ex: {"repo": repo, "file": file, "line": line, "commit": ex.commit}    # noqa: E731
        _api(g, repo, ex, ev)
        _ui(g, repo, ex, ev, endpoints_by_key)
        _rules(g, repo, ex, ev)


def _api(g, repo, ex, ev):
    schemas = {s.name: s for s in ex.schemas}
    enums = ex.enums or {}
    examples: dict = {}
    # every collection is walked in a fixed order: the same inputs must build the same graph, whether extracted in full or incrementally
    for x in sorted(ex.api_examples, key=lambda x: (x["method"], x["norm_path"], x.get("status") or 0, x["source"], x.get("file") or "",
                                                    x.get("line") or 0, json.dumps(x.get("body"), sort_keys=True, default=str))):
        examples.setdefault((x["method"], x["norm_path"]), []).append(x)

    def field(owner: str, place: str, name: str, spec: dict):
        fid = f"{owner}:{place}:{name}"
        t = str(spec.get("type") or "")
        base = t.split("[")[-1].split("<")[-1].strip("]> ").split(".")[-1]
        allowed = [str(v) for v in enums.get(base, [])] or [str(v) for v in (spec.get("constraints") or {}).get("enum", []) or []]
        g.add_node(fid, type="field", repo=repo, service=ex.service, name=name, field_type=t or None, required=bool(spec.get("required")),
                   multi=bool(spec.get("multi")), nullable=bool(spec.get("nullable")), **{"in": place},
                   **({"allowed_values": allowed} if allowed else {}), **_limits(spec.get("constraints") or {}))
        return fid

    for e in sorted(ex.endpoints, key=lambda e: e.id):
        rid, sid = f"{e.id}#request", f"{e.id}#response"
        body = next((schemas[n] for n in e.request_schemas if n in schemas), None)
        g.add_node(rid, type="request", repo=repo, service=ex.service, name=f"{e.method} {e.path} request", file=e.file, line=e.line,
                   body_schema=body.name if body else None)
        g.add_edge(Edge(e.id, "ACCEPTS", rid, SOURCE_CONFIRMED, "extract", None, ev(e.file, e.line)))
        for p in sorted(e.params, key=lambda p: (p.get("in") or "query", p["name"])):
            fid = field(rid, p.get("in") or "query", p["name"], {"type": p.get("type"), "required": p.get("required"), "multi": p.get("multi"),
                                                                 "constraints": p.get("constraints") or {}})
            g.add_edge(Edge(rid, "HAS_FIELD", fid, SOURCE_CONFIRMED, "extract", None, ev(e.file, e.line)))
        if body:
            for name, spec in sorted(body.fields.items()):
                fid = field(rid, "body", name, spec)
                g.add_edge(Edge(rid, "HAS_FIELD", fid, SOURCE_CONFIRMED, "extract", None, ev(body.file, body.line)))
        resp = schemas.get(e.response_schema or "")
        g.add_node(sid, type="response", repo=repo, service=ex.service, name=f"{e.method} {e.path} response", file=e.file, line=e.line,
                   schema=resp.name if resp else None, success_status=e.success_status, shape=e.response_shape,
                   error_statuses=_statuses(ex, e.handler_id) if e.source == "code" else [])
        g.add_edge(Edge(e.id, "RETURNS", sid, SOURCE_CONFIRMED, "extract", None, ev(e.file, e.line)))
        if resp:
            for name, spec in sorted(resp.fields.items()):
                fid = field(sid, "body", name, spec)
                g.add_edge(Edge(sid, "HAS_FIELD", fid, SOURCE_CONFIRMED, "extract", None, ev(resp.file, resp.line)))
        for n, x in enumerate(examples.get((e.method, e.norm_path), []), 1):
            xid = f"{sid}:example{n}"
            g.add_node(xid, type="example", repo=repo, service=ex.service, file=x.get("file"), line=x.get("line"),
                       name=f"{x.get('status') or ''} {x['source']} example".strip(), status=x.get("status"), source=x["source"],
                       partial=bool(x.get("partial")), body_json=json.dumps(x.get("body"), sort_keys=True, default=str),
                       **({"test": x["test"]} if x.get("test") else {}))
            g.add_edge(Edge(sid, "HAS_EXAMPLE", xid, SOURCE_CONFIRMED, f"{x['source']}-example", None, ev(x.get("file"), x.get("line"))))


def _ui(g, repo, ex, ev, endpoints_by_key):
    if not ex.ui_pages:
        return
    app = f"{repo}:app"
    g.add_node(app, type="application", repo=repo, service=ex.service, name=ex.service,
               frameworks=sorted({p.get("framework") for p in ex.ui_pages if p.get("framework")}))
    g.add_edge(Edge(f"svc:{ex.service}", "HAS_APPLICATION", app, SOURCE_CONFIRMED, "extract", None, ev(None, None)))
    screens = {normalize_path(p["route"]): f"{repo}:screen:{p['page']}" for p in ex.ui_pages if p.get("route")}
    for p in sorted(ex.ui_pages, key=lambda p: (p.get("route") or "~", p["page"])):
        sid = f"{repo}:screen:{p['page']}"
        file = (p.get("files") or [None])[0]
        g.add_node(sid, type="screen", repo=repo, service=ex.service, name=p["page"], route=p.get("route"), component=p.get("component"),
                   framework=p.get("framework"), file=file)
        g.add_edge(Edge(app, "HAS_SCREEN", sid, SOURCE_CONFIRMED, "extract", None, ev(file, None)))
        for i, el in enumerate(p["elements"], 1):
            eid = f"{sid}#el{i}"
            name = el.get("label") or el.get("name") or el.get("text") or el.get("placeholder") or el["tag"]
            g.add_node(eid, type="element", repo=repo, service=ex.service, name=str(name)[:120], tag=el["tag"], input_type=el.get("type"),
                       label=el.get("label"), mandatory=bool(el.get("mandatory")), mandatory_source=el.get("mandatory_source"),
                       repeated=bool(el.get("repeated")), file=file, line=el.get("line"),
                       **({"options": _options(el), "options_source": el.get("options_source")} if el["tag"] == "select" else {}),
                       **_limits(el.get("constraints") or {}))
            g.add_edge(Edge(sid, "HAS_ELEMENT", eid, SOURCE_CONFIRMED, "extract", None, ev(file, el.get("line"))))
            best = el.get("locator") or {}
            for rank, loc in enumerate(el.get("locators") or [], 1):
                lid = f"{eid}#loc{rank}"
                g.add_node(lid, type="locator", repo=repo, service=ex.service, name=loc["strategy"], strategy=loc["strategy"], css=loc.get("css"),
                           xpath=loc.get("xpath"), playwright=loc.get("playwright"), unique=bool(loc.get("unique")),
                           best=loc.get("strategy") == best.get("strategy") and loc.get("xpath") == best.get("xpath"), rank=rank)
                g.add_edge(Edge(eid, "LOCATED_BY", lid, SOURCE_CONFIRMED, "extract", None, ev(file, el.get("line"))))
        for nav in p.get("navigations", []):
            dest = screens.get(normalize_path(nav["to"]))
            if dest and dest != sid:
                g.add_edge(Edge(sid, "NAVIGATES_TO", dest, SOURCE_CONFIRMED, f"ui-{nav['via']}", None, ev(file, nav.get("line"))))
        for c in p.get("api_calls", []):
            hits = endpoints_by_key.get((c.get("method"), c.get("norm_path")), [])
            for hit in hits:
                g.add_edge(Edge(sid, "CALLS_API", hit, SOURCE_CONFIRMED if len(hits) == 1 else INFERRED, "ui-call",
                                None if len(hits) == 1 else "INF-UI-CALL-AMBIGUOUS", ev(file, c.get("line"))))


def _rules(g, repo, ex, ev):
    for f in sorted(ex.functions, key=lambda f: f.id):
        for n, flt in enumerate(sorted(f.filters, key=lambda x: (x.get("line") or 0, x["expr"])), 1):
            rid = f"{f.id}#rule{n}"
            g.add_node(rid, type="rule", repo=repo, service=ex.service, name=flt["expr"], expr=flt["expr"], fields=list(flt.get("fields") or []),
                       function=f.qualname, file=f.file, line=flt.get("line"))
            g.add_edge(Edge(f.id, "APPLIES_RULE", rid, SOURCE_CONFIRMED, "extract", None, ev(f.file, flt.get("line"))))

"""Execution flow and design view (docs/USER-GUIDE.md: "show me the execution flow, the main entities, the controllers and
models - the design, not which method calls which").

For an entry point: the handler that serves it, the code it runs (CALLS), the other services it calls (ISSUES -> CONSUMES_API), the data
it reads and writes (READS / WRITES), the topics it publishes to and the business rules it applies, as a tree with each step's role in
the design (controller, service, data access, client, model). For a service: its controllers, the models its API uses, its main entities
with the entry points that read or write them, and how its code splits over those roles.

Everything is read from the graph (docs/GRAPH-SCHEMA.md). The order of calls inside one function is not recorded, so siblings are listed
by name, not in execution order; the tree says so. Ambiguous (inferred) calls are shown and marked.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import INFERRED

FOLDER_ROLE = {"controllers": "controller", "controller": "controller", "api": "controller", "web": "controller", "routes": "controller",
               "views": "controller", "services": "service", "service": "service", "application": "service", "domain": "model",
               "model": "model", "models": "model", "entities": "model", "schemas": "model", "data": "data access",
               "repository": "data access", "repositories": "data access", "dao": "data access", "persistence": "data access",
               "clients": "client", "client": "client", "integrations": "client"}
_FOLDER = re.compile(r"(?:^|/)(" + "|".join(sorted(FOLDER_ROLE, key=len, reverse=True)) + r")(?:/|$)", re.I)
NAME_ROLE = ((re.compile(r"(Repository|Repo|Dao|DAO|Store)$"), "data access"), (re.compile(r"(Controller|Resource|Handler|View)$"), "controller"),
             (re.compile(r"(Service|Manager|UseCase)$"), "service"), (re.compile(r"(Client|Gateway|Adapter)$"), "client"))
MAX_DEPTH, MAX_STEPS = 8, 300


def _index(g) -> dict:
    by_src: dict = {}
    for e in g.edges:
        by_src.setdefault(e.src, []).append(e)
    return by_src


def role_of(g, nid: str, by_src: dict, handlers: set) -> str:
    """controller / service / data access / client / model / function, from the folder, then the class name, then the graph."""
    a = g.nodes[nid]
    if nid in handlers:
        return "controller"
    m = _FOLDER.search(str(a.get("file") or "").replace("\\", "/").lower())
    if m:
        return FOLDER_ROLE[m.group(1).lower()]
    cls = str(a.get("name") or "").split(".")[0]
    for rx, role in NAME_ROLE:
        if "." in str(a.get("name") or "") and rx.search(cls):
            return role
    rels = {e.rel for e in by_src.get(nid, [])}
    if rels & {"READS", "WRITES"}:
        return "data access"
    if "PUBLISHES" in rels:
        return "events"
    if "ISSUES" in rels and any(g.nodes.get(e.dst, {}).get("type") == "callsite" for e in by_src.get(nid, [])):
        return "client"
    return "service"


def _endpoint_name(a: dict) -> str:
    return f"{a.get('method')} {a.get('route') or a.get('path')}"


def flow(g, endpoint: str, max_depth: int = MAX_DEPTH, by_src: Optional[dict] = None) -> dict:
    """The execution tree of one endpoint node."""
    by_src = by_src if by_src is not None else _index(g)
    handlers = {e.src for e in g.edges if e.rel == "HANDLES"}
    ep = g.nodes[endpoint]
    count = [0]
    truncated = [False]

    def step(nid: str, kind: str, path: set, depth: int, edge=None) -> Optional[dict]:
        if count[0] >= MAX_STEPS:
            truncated[0] = True
            return None
        count[0] += 1
        a = g.nodes[nid]
        s = {"node": nid, "kind": kind, "name": a.get("name") or nid.split("::")[-1], "service": a.get("service"), "repo": a.get("repo"),
             "file": a.get("file"), "line": a.get("line")}
        if edge is not None and edge.status == INFERRED:
            s["inferred"] = edge.rule or "inferred"
        if kind in ("handler", "function"):
            s["role"] = role_of(g, nid, by_src, handlers)
            if nid in path:
                s["recursive"] = True
                return s
            if depth >= max_depth:
                s["truncated"] = True
                truncated[0] = True
                return s
            kids = []
            for e in sorted(by_src.get(nid, []), key=lambda e: (e.rel, g.nodes.get(e.dst, {}).get("name") or e.dst)):
                d = g.nodes.get(e.dst)
                if d is None:
                    continue
                if e.rel == "CALLS" and d["type"] == "function":
                    c = step(e.dst, "function", path | {nid}, depth + 1, e)
                elif e.rel == "CALLS" and d["type"] == "procedure":
                    c = {**_leaf(d, e.dst), "kind": "stored procedure"}
                elif e.rel == "ISSUES" and d["type"] == "callsite":
                    targets = [x for x in by_src.get(e.dst, []) if x.rel == "CONSUMES_API"]
                    pkg = (d.get("client_pkg"), d.get("client_attr"))
                    if not targets and pkg[0] and any(x.rel == "PUBLISHES" for x in by_src.get(nid, [])):
                        continue                             # a message producer's send(): shown as the topic it publishes to
                    if not targets:
                        c = {**_leaf(d, e.dst), "kind": "outbound call",
                             "name": f"call through {pkg[0]}.{pkg[1]}" if pkg[0] else "unresolved outbound call",
                             "note": "the called service could not be resolved"}
                    else:
                        for x in targets[:-1]:
                            kids.append(_call(g, x))
                        c = _call(g, targets[-1])
                elif e.rel in ("READS", "WRITES"):
                    c = {**_leaf(d, e.dst), "kind": "reads" if e.rel == "READS" else "writes", "data_type": d["type"]}
                elif e.rel == "PUBLISHES":
                    c = {**_leaf(d, e.dst), "kind": "publishes"}
                elif e.rel == "APPLIES_RULE":
                    c = {**_leaf(d, e.dst), "kind": "rule", "name": d.get("expr")}
                else:
                    continue
                if c:
                    kids.append(c)
            if kids:
                s["steps"] = kids
        return s

    roots = [step(e.src, "handler", set(), 1, e) for e in g.edges if e.rel == "HANDLES" and e.dst == endpoint]
    return {"entry_point": _endpoint_name(ep), "node": endpoint, "service": ep.get("service"), "repo": ep.get("repo"),
            "file": ep.get("file"), "line": ep.get("line"), "steps": [r for r in roots if r],
            "declared_only": not roots, "truncated": truncated[0],
            "note": "Steps under one function are listed by name: the order of calls inside a function is not recorded."}


def _leaf(a: dict, nid: str) -> dict:
    return {"node": nid, "name": a.get("name") or a.get("table") or nid.split("::")[-1], "service": a.get("service"), "repo": a.get("repo"),
            "file": a.get("file"), "line": a.get("line")}


def _call(g, edge) -> dict:
    d = g.nodes[edge.dst]
    return {"node": edge.dst, "kind": "calls service", "name": _endpoint_name(d), "service": d.get("service"), "repo": d.get("repo"),
            "file": d.get("file"), "line": d.get("line"), **({"inferred": edge.rule or "inferred"} if edge.status == INFERRED else {})}


def flows(g, endpoint: Optional[str] = None, repo: Optional[str] = None, max_depth: int = MAX_DEPTH) -> list:
    """Execution trees of the endpoints (optionally one, written 'METHOD /route', or one repository's)."""
    by_src = _index(g)
    out = []
    for nid, a in sorted(g.nodes.items()):
        if a.get("type") != "endpoint" or (repo and a.get("repo") != repo):
            continue
        if endpoint and endpoint.strip().upper() != _endpoint_name(a).upper() and endpoint != nid:
            continue
        out.append(flow(g, nid, max_depth, by_src))
    return out


def design(g, repo: Optional[str] = None) -> list:
    """Per service: controllers (entry-point handlers), models (request/response schemas), main entities with the entry points that use
    them, and how the code splits over design roles."""
    by_src = _index(g)
    handlers = {e.src for e in g.edges if e.rel == "HANDLES"}
    services: dict = {}
    for nid, a in g.nodes.items():
        if a.get("type") == "service" and (repo is None or a.get("repo") == repo):
            services[a["service"]] = {"service": a["service"], "repo": a.get("repo"), "controllers": [], "models": [], "entities": [], "roles": {}}
    serves: dict = {}
    for e in g.edges:
        if e.rel == "HANDLES":
            serves.setdefault(e.src, []).append(_endpoint_name(g.nodes[e.dst]))
    for nid, a in g.nodes.items():
        s = services.get(a.get("service"))
        if s is None:
            continue
        if a.get("type") == "function":
            r = role_of(g, nid, by_src, handlers)
            s["roles"][r] = s["roles"].get(r, 0) + 1
            if nid in handlers:
                s["controllers"].append({"function": a.get("name"), "file": a.get("file"), "line": a.get("line"), "serves": sorted(serves[nid])})
    # models: schemas an endpoint uses; entities: ORM classes and the entry points whose code reads or writes them
    reach: dict = {}
    for ep in [n for n, a in g.nodes.items() if a.get("type") == "endpoint"]:
        name = _endpoint_name(g.nodes[ep])
        for e in by_src.get(ep, []):
            if e.rel == "USES_SCHEMA":
                svc = services.get(g.nodes[e.dst].get("service"))
                if svc is not None:
                    reach.setdefault(("model", e.dst), set()).add(name)
        for nid in _below(g, ep, by_src):
            for e in by_src.get(nid, []):
                if e.rel in ("READS", "WRITES") and g.nodes.get(e.dst, {}).get("type") in ("entity", "table"):
                    reach.setdefault(("data", e.dst), set()).add(f"{name} ({'reads' if e.rel == 'READS' else 'writes'})")
    for (kind, nid), eps in sorted(reach.items()):
        a = g.nodes[nid]
        s = services.get(a.get("service"))
        if s is None:
            continue
        row = {"name": a.get("name") or a.get("table"), "file": a.get("file"), "line": a.get("line"), "used_by": sorted(eps)}
        (s["models"] if kind == "model" else s["entities"]).append(row)
    for s in services.values():
        s["controllers"].sort(key=lambda c: (c["file"] or "", c["line"] or 0))
        s["entities"].sort(key=lambda x: (-len(x["used_by"]), x["name"]))
    return sorted(services.values(), key=lambda s: s["service"])


def _below(g, endpoint: str, by_src: dict) -> set:
    """The functions an endpoint runs (handler and everything it calls, in its own service)."""
    start = [e.src for e in g.edges if e.rel == "HANDLES" and e.dst == endpoint]
    seen, todo = set(start), list(start)
    while todo:
        n = todo.pop()
        for e in by_src.get(n, []):
            if e.rel == "CALLS" and g.nodes.get(e.dst, {}).get("type") == "function" and e.dst not in seen:
                seen.add(e.dst)
                todo.append(e.dst)
    return seen


def render(flow_: dict) -> str:
    """A flow as indented text: one line per step with its role and place in the code."""
    lines = [f"{flow_['entry_point']}  ({flow_['service']})"]
    if flow_["declared_only"]:
        lines.append("  declared in a specification only: no handler in the analysed code")

    def walk(steps, prefix, depth):
        for i, s in enumerate(steps, 1):
            num = f"{prefix}{i}"
            where = f"  {s['file']}:{s['line']}" if s.get("file") else ""
            tag = f" [{s['role']}]" if s.get("role") else f" [{s['kind']}]"
            extra = " (inferred)" if s.get("inferred") else " (recursive)" if s.get("recursive") else " ..." if s.get("truncated") else ""
            svc = f" on {s['service']}" if s["kind"] == "calls service" else ""
            lines.append(f"{'  ' * depth}{num} {s['name']}{svc}{tag}{extra}{where}")
            walk(s.get("steps", []), num + ".", depth + 1)
    walk(flow_["steps"], "", 1)
    return "\n".join(lines)

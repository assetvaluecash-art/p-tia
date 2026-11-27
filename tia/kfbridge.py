"""The TIA <-> Knowledge Fabric bridge: settings, link planning and business impact (docs/KF-BRIDGE.md).

Pure: no database, no Neo4j. tia.neo4jgraph reads the Knowledge Fabric index and writes the links this module plans; tia.analysis_service
runs both as a job; tia.impact adds the business impact of a change to its report from the context the job captured.

TIA's own graph is domain-neutral (code has no business domain). The domain appears only here, on the links: in `auto` mode a service is
linked to the matching Knowledge Fabric System in every domain of the customer (Knowledge Fabric keeps one System node per domain), and a
domain list restricts that. The customer is never optional: every link stays inside one Knowledge Fabric customer.

Link evidence uses TIA's vocabulary: an owner's declaration is `contract_confirmed`, a shared identifier (a test case id) is
`source_confirmed`, an exact Knowledge Fabric key of a name is `inferred` (rule INF-KF-NAME). Similar names never become links.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from . import kfidentity as kid

from .kfsettings import DEFAULTS, alias_entity_id, canonical, validate   # noqa: F401  (re-exported: the setting lives in the rules layer)
DECLARED, ID_MATCH, NAME_MATCH = "contract_confirmed", "source_confirmed", "inferred"
RANK = {"source_confirmed": 3, "contract_confirmed": 2, "inferred": 1}
GAP_KINDS = ("unmapped_service", "systems_without_code", "declared_not_found", "ambiguous", "test_cases_only_in_tia",
             "test_cases_only_in_kf", "unknown_domains", "unlinked_screens", "unlinked_data", "forms_without_screen",
             "data_entities_without_code",
             # product mapping and review (docs/KF-BRIDGE.md sections 11 and 12)
             "unmapped_repositories", "product_not_found", "product_conflicts", "rejected_by_review", "stale_decisions")
LEVELS = ("ProductLine", "Function", "SubFunction")
POSITIVE = ("confirm", "choose")                     # link decisions that make a link; "reject" keeps a pair out; "reset" undoes
# phase 7: TIA item kind -> (TIA label, bridge relationship, Knowledge Fabric label, alias setting, tia-services.yaml key)
ITEM_KINDS = {"screen": ("TiaScreen", "REALIZES", "FormCapture", "screen_aliases", "business_screens"),
              "entity": ("TiaEntity", "STORES", "DataEntity", "data_aliases", "business_data"),
              "table": ("TiaTable", "STORES", "DataEntity", "data_aliases", "business_data")}
TECH_SUFFIXES = ("Record", "Entity", "Model", "Dto", "DTO", "View", "Row", "Table", "Bean", "Po", "Do")
ISSUE_KEY = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,6})(?![A-Za-z0-9])")
STOP = frozenset("the and for are not with from that this must should shall will can any all its into only than then each per who what when "
                 "where which while been being have has had was were get set list item items data value values true false none null".split())


# ---- planning -----------------------------------------------------------------------------------------------------------------------------
def resolve_domains(setting, available: list) -> tuple[list, list]:
    """-> (domains to use, listed domains the customer does not have). `auto` = every domain the customer has."""
    have = sorted(set(available))
    if setting == "auto" or not setting:
        return have, []
    want = sorted(set(setting))
    return [d for d in want if d in have], [d for d in want if d not in have]


class _Index:
    """The Knowledge Fabric nodes of one label, by (domain, entity_id) and by (domain, key of the name or of an alias)."""

    def __init__(self, label: str, rows: list):
        self.label, self.by_id, self.by_key, self.by_eid, self.rows = label, {}, {}, {}, rows
        for r in rows:
            self.by_id[(r["domain"], r["entity_id"])] = r
            self.by_eid[r["entity_id"]] = r
            keys = {kid.key(label, r.get("name") or "")} | {kid.key(label, a) for a in (r.get("aliases") or []) if isinstance(a, str)}
            for k in keys - {None}:
                self.by_key.setdefault((r["domain"], k), {})[r["entity_id"]] = r

    def find(self, scope_id: str, domain: str, name: str) -> tuple[list, str]:
        """-> (matching nodes, how found). An alias written id:<entity id> names one node directly (it settles a name that matches two);
        otherwise the computed entity id first, then name and aliases within the domain."""
        eid = alias_entity_id(name)
        if eid is not None:
            return ([self.by_id[(domain, eid)]], "entity_id") if (domain, eid) in self.by_id else ([], "entity_id")
        k = kid.key(self.label, name)
        if not k:
            return [], "dropped"
        computed = kid.entity_id(scope_id, domain, self.label, k)
        if (domain, computed) in self.by_id:
            return [self.by_id[(domain, computed)]], "computed"
        hits = list((self.by_key.get((domain, k)) or {}).values())
        return sorted(hits, key=lambda r: r["entity_id"]), "looked_up"


def tia_id(key: str) -> str:
    """A bridge key (`<tenant>|svc:payments-api`) -> the TIA node id decisions are kept under (`svc:payments-api`)."""
    return key.split("|", 1)[1] if "|" in key else key


def _pl_keys(names) -> set:
    return {k for k in (kid.key("ProductLine", n) for n in names or []) if k}


def _pick(hits: list, prefer: set) -> tuple[list, bool]:
    """Several nodes for one name: the one under the mapped product line wins (rule INF-KF-PRODUCT). -> (hits, decided by product line)."""
    if len(hits) > 1 and prefer:
        inside = [h for h in hits if _pl_keys(h.get("product_lines")) & prefer]
        if len(inside) == 1:
            return inside, True
    return hits, False


def _option(h: dict) -> dict:
    return {"entity_id": h["entity_id"], "name": h.get("name"), "domain": h.get("domain"), "product_lines": list(h.get("product_lines") or [])}


def by_tia(decisions: dict) -> dict:
    """{(tia_id, target): decision} -> {tia_id: {target: decision}}."""
    out: dict = {}
    for (t, target), d in (decisions or {}).items():
        out.setdefault(t, {})[target] = d
    return out


def plan(services: list, test_cases: list, index: dict, settings: dict, kf_tenant_id: Optional[str], domains: list,
         items: Optional[dict] = None, rules: Optional[list] = None, decisions: Optional[dict] = None,
         products: Optional[dict] = None) -> dict:
    """Plan the bridge links. services: [{"key", "service", "repo", "declared": [System names from tia-services.yaml]}];
    test_cases: [{"key", "id", "repo"}]; index: {label: [rows]} with rows {entity_id, name, aliases, domain[, description, product_lines,
    functions]} for System, TestCase and (phase 7) FormCapture, DataEntity, Rule; items: {"screen"|"entity"|"table": [item]} (see
    _plan_items); rules: TIA's rules found in code (see propose_rules); decisions: the review decisions, {(tia_id, entity id): decision}
    (latest_decisions); products: the services' places in the business tree (resolve_products).
    A service with a place is linked only inside the domains of its places, and when a name finds several nodes in a domain the one under the
    mapped product line wins (INF-KF-PRODUCT); a service without one is linked in `domains`. A person's decision always wins: a confirmed or
    chosen pair is linked as contract_confirmed (source review) and settles that domain, a rejected pair is never linked.
    -> {"links", "gaps", "proposals", "product_suggestions", "link_suggestions"} in a deterministic order."""
    scope_id = kid.scope(settings["customer_id"], kf_tenant_id)
    systems, cases = _Index("System", index.get("System", [])), _Index("TestCase", index.get("TestCase", []))
    aliases = settings.get("system_aliases") or {}
    links, gaps = [], {k: [] for k in GAP_KINDS}
    realised: set = set()
    products = products or {}
    dec = by_tia(decisions)
    loaded = sorted({r["domain"] for rows in index.values() for r in rows} | set(domains))
    services_of_repo: dict = {}
    for s in services:
        services_of_repo.setdefault(s.get("repo"), []).append(s["service"])

    def places_of(service: Optional[str], repo: Optional[str] = None) -> list:
        if service and products.get(service):
            return products[service]
        return [p for sv in services_of_repo.get(repo, []) for p in products.get(sv) or []] if repo else []

    def scope(service: Optional[str], repo: Optional[str] = None) -> list:
        places = places_of(service, repo)
        return sorted({p["domain"] for p in places}) if places else list(domains)

    def prefer(service, repo, domain) -> set:
        return _pl_keys(p["product_line"] for p in places_of(service, repo) if p["domain"] == domain)

    def link(src, rel, node, domain, status, strategy, source, how, rule=None, to_label=None, from_label=None, **extra):
        links.append({"from": src, "from_label": from_label or ("TiaService" if rel == "REALIZES" else "TiaTestCase"), "rel": rel,
                      "to": node["entity_id"], "to_label": to_label or ("System" if rel == "REALIZES" else "TestCase"),
                      "to_name": node.get("name"), "domain": domain, "status": status, "strategy": strategy, "rule": rule, "source": source,
                      "id_check": how, **{k: v for k, v in extra.items() if v is not None}})

    def reviewed(tid: str, idx: "_Index") -> list:
        """The pairs a person confirmed or chose for this TIA item, whose node is still in Knowledge Fabric: [(node, decision)]."""
        return [(idx.by_eid[e], d) for e, d in sorted((dec.get(tid) or {}).items()) if d.get("decision") in POSITIVE and e in idx.by_eid]

    def rejected(tid: str) -> set:
        return {e for e, d in (dec.get(tid) or {}).items() if d.get("decision") == "reject"}

    def note_rejected(tid, hits, rej, **who):
        for h in hits:
            if h["entity_id"] in rej and not any(g["tia_id"] == tid and g["entity_id"] == h["entity_id"] for g in gaps["rejected_by_review"]):
                gaps["rejected_by_review"].append({"tia_id": tid, "entity_id": h["entity_id"], "name": h.get("name"), "domain": h.get("domain"),
                                                   **who})

    def match_service(s: dict, domain: str, rej: set, pref: set) -> tuple[list, list]:
        """-> ([(node, status, strategy, source, how, rule)], [ambiguous gap]) for one service in one domain."""
        for source, names in (("tenant_config", aliases.get(s["service"]) or []), ("tia-services.yaml", s.get("declared") or [])):
            amb = []
            for name in names:
                hits, how = systems.find(scope_id, domain, name)
                note_rejected(tia_id(s["key"]), hits, rej, service=s["service"], label="System")
                hits, tb = _pick([h for h in hits if h["entity_id"] not in rej], pref)
                if len(hits) == 1:
                    return [(hits[0], NAME_MATCH if tb else DECLARED, "declared", source, how, "INF-KF-PRODUCT" if tb else None)], []
                if len(hits) > 1:
                    amb.append({"service": s["service"], "repo": s.get("repo"), "tia_id": tia_id(s["key"]), "name": name, "domain": domain,
                                "label": "System", "candidates": [h["entity_id"] for h in hits], "options": [_option(h) for h in hits]})
            if names:
                return [], amb
        if not settings.get("link_name_matches", True):
            return [], []
        seen: dict = {}
        for name in dict.fromkeys(n for n in (s["service"], s.get("repo")) if n):
            hits, how = systems.find(scope_id, domain, name)
            note_rejected(tia_id(s["key"]), hits, rej, service=s["service"], label="System")
            for h in hits:
                if h["entity_id"] not in rej:
                    seen.setdefault(h["entity_id"], (h, how))
        nodes, tb = _pick([v[0] for v in seen.values()], pref)
        if len(nodes) == 1:
            return [(nodes[0], NAME_MATCH, "name", "name", seen[nodes[0]["entity_id"]][1], "INF-KF-PRODUCT" if tb else "INF-KF-NAME")], []
        if len(nodes) > 1:
            return [], [{"service": s["service"], "repo": s.get("repo"), "tia_id": tia_id(s["key"]), "name": s["service"], "domain": domain,
                         "label": "System", "candidates": sorted(seen), "options": [_option(seen[e][0]) for e in sorted(seen)]}]
        return [], []

    for s in sorted(services, key=lambda x: x["service"]):
        tid, rej = tia_id(s["key"]), rejected(tia_id(s["key"]))
        found_any, settled = False, set()
        for node, d in reviewed(tid, systems):
            link(s["key"], "REALIZES", node, node["domain"], DECLARED, "reviewed", "review", "confirmed", service=s["service"],
                 decided_by=d.get("by"), decided_at=d.get("at"))
            realised.add(node["entity_id"])
            settled.add(node["domain"])
            found_any = True
        for domain in scope(s["service"], s.get("repo")):
            if domain in settled:
                continue
            got, amb = match_service(s, domain, rej, prefer(s["service"], s.get("repo"), domain))
            gaps["ambiguous"] += amb
            for node, status, strategy, source, how, rule in got:
                link(s["key"], "REALIZES", node, domain, status, strategy, source, how, rule=rule, service=s["service"])
                realised.add(node["entity_id"])
                found_any = True
        if not found_any:
            gaps["unmapped_service"].append({"service": s["service"], "repo": s.get("repo"), "tia_id": tid})
        for source, names in (("tenant_config", aliases.get(s["service"]) or []), ("tia-services.yaml", s.get("declared") or [])):
            for name in names:
                if not any(systems.find(scope_id, d, name)[0] for d in loaded):
                    gaps["declared_not_found"].append({"service": s["service"], "name": name, "source": source})
        # a declared place that disagrees with where the service's Systems sit: none of the Systems its names find sits in a mapped place
        # (a System of the same name in another domain is what a mapping chooses against, not a conflict)
        places = products.get(s["service"]) or []
        if places:
            mapped = {(p["domain"], k) for p in places for k in _pl_keys([p["product_line"]])}
            in_scope = {p["domain"] for p in places}
            found, agree = [], False
            for domain in loaded:
                got, amb = match_service(s, domain, rej, set())
                for node in [g[0] for g in got] + [systems.by_eid[e] for a in amb for e in a["candidates"] if e in systems.by_eid]:
                    pls = _pl_keys(node.get("product_lines"))
                    if domain in in_scope and (not pls or {(domain, k) for k in pls} & mapped):
                        agree = True
                    elif not any(f["entity_id"] == node["entity_id"] for f in found):
                        found.append({"system": node.get("name"), "entity_id": node["entity_id"], "domain": domain,
                                      "product_lines": list(node.get("product_lines") or []), "functions": list(node.get("functions") or [])})
            if found and not agree:
                gaps["product_conflicts"].append({"service": s["service"], "repo": s.get("repo"), "tia_id": tid,
                                                  "places": [place_label(p) for p in places], "found": found})
    for r in sorted(systems.rows, key=lambda r: (r["domain"], r.get("name") or "")):
        if r["entity_id"] not in realised and r["domain"] in domains:
            gaps["systems_without_code"].append({"system": r.get("name"), "domain": r["domain"], "entity_id": r["entity_id"]})
    kf_case_ids, tia_case_keys = set(), set()
    for tc in sorted(test_cases, key=lambda x: (x["repo"], x["id"])):
        k = kid.key("TestCase", tc["id"])
        if k:
            tia_case_keys.add(k)
        tid, rej = tia_id(tc["key"]), rejected(tia_id(tc["key"]))
        matched, settled = False, set()
        for node, d in reviewed(tid, cases):
            link(tc["key"], "SAME_AS", node, node["domain"], DECLARED, "reviewed", "review", "confirmed", test_case=tc["id"],
                 decided_by=d.get("by"), decided_at=d.get("at"))
            kf_case_ids.add(node["entity_id"])
            settled.add(node["domain"])
            matched = True
        for domain in scope(None, tc["repo"]):
            if domain in settled:
                continue
            hits, how = cases.find(scope_id, domain, tc["id"])
            note_rejected(tid, hits, rej, test_case=tc["id"], label="TestCase")
            for h in hits:                                       # the same id in one domain twice is the same test case
                if h["entity_id"] in rej:
                    continue
                link(tc["key"], "SAME_AS", h, domain, ID_MATCH, "test_case_id", "test_case_id", how, test_case=tc["id"])
                kf_case_ids.add(h["entity_id"])
                matched = True
        if not matched:
            gaps["test_cases_only_in_tia"].append({"id": tc["id"], "repo": tc["repo"]})
    for r in sorted(cases.rows, key=lambda r: (r["domain"], r.get("name") or "")):
        if r["entity_id"] not in kf_case_ids and kid.key("TestCase", r.get("name") or "") not in tia_case_keys and r["domain"] in domains:
            gaps["test_cases_only_in_kf"].append({"id": r.get("name"), "domain": r["domain"]})
    _plan_items(items or {}, index, settings, scope_id, scope, prefer, dec, link, gaps, domains)
    for svc, places in sorted(products.items()):
        key = next((s["key"] for s in services if s["service"] == svc), None)
        if not key:
            continue
        for p in places:
            link(key, "PLACED_IN", {"entity_id": p["target"]["value"], "name": p["target"]["name"]}, p["domain"], DECLARED,
                 "reviewed" if p["source"] == "review" else "declared", p["source"], "catalogue", to_label=p["level"], from_label="TiaService",
                 service=svc, product_line=p["product_line"], product_line_id=p["product_line_id"], function=p.get("function"),
                 sub_function=p.get("sub_function"), level=p["level"], to_key=p["target"]["key"], decided_by=p.get("decided_by"),
                 decided_at=p.get("decided_at"))
    proposals = []
    if rules is not None:
        kf_rules = [r for r in index.get("Rule", []) if r["domain"] in domains]
        by_id = {r["entity_id"]: r for r in index.get("Rule", [])}
        rule_by_id = {r["id"]: r for r in rules}
        for (tid, eid), d in sorted((decisions or {}).items()):
            if d.get("decision") == "confirm" and tid in rule_by_id and eid in by_id:
                r, k = rule_by_id[tid], by_id[eid]
                link(r["key"], "ENFORCES", k, k["domain"], DECLARED, "reviewed", "review", "confirmed", to_label="Rule", from_label="TiaRule",
                     tia_id=tid, tia_name=r.get("expr"), service=r.get("service"), decided_by=d.get("by"), decided_at=d.get("at"))
        if settings.get("propose_rules", True):
            proposals = propose_rules(rules, kf_rules, decisions or {})
    # a decided pair whose Knowledge Fabric node is gone (re-ingested, renamed): the decision waits for a person
    all_ids = {r["entity_id"] for rows in index.values() for r in rows}
    for (tid, eid), d in sorted((decisions or {}).items()):
        if d.get("decision") in POSITIVE and d.get("kind", "link") in ("link", "rule") and eid not in all_ids and eid and d.get("label"):
            label = d["label"]
            k = kid.key(label, d.get("kf_name") or "")
            cands = [r for r in index.get(label, []) if k and (kid.key(label, r.get("name") or "") == k
                                                                 or k in {kid.key(label, a) for a in r.get("aliases") or []})]
            gaps["stale_decisions"].append({"tia_id": tid, "kind": d.get("kind", "link"), "label": label, "entity_id": eid,
                                            "kf_name": d.get("kf_name"), "rel": d.get("rel"), "decided_by": d.get("by"), "decided_at": d.get("at"),
                                            "candidates": [c["entity_id"] for c in cands], "options": [_option(c) for c in cands]})
    links.sort(key=lambda x: (x["rel"], x["from"], x["domain"], x["to"]))
    return {"links": links, "gaps": gaps, "proposals": proposals,
            "product_suggestions": product_suggestions(services, links, systems, products, decisions or {}),
            "link_suggestions": link_suggestions(gaps, index, items or {}, services, links, scope)}


def _plan_items(items: dict, index: dict, settings: dict, scope_id: str, scope, prefer, dec: dict, link, gaps: dict, domains: list) -> None:
    """Screens -> FormCapture (REALIZES), ORM entities and tables -> DataEntity (STORES): a person's decision first, then a declared alias
    (tenant setting, then tia-services.yaml), else one exact Knowledge Fabric key (inferred); inside the domains of the item's service's
    places when it has them. items: {kind: [{"key", "id", "name", "service", "repo", "declared": {alias name: [Knowledge Fabric names]},
    + route/component (screen) or table (entity)}]}."""
    linked: dict = {"FormCapture": set(), "DataEntity": set()}
    indexes = {lab: _Index(lab, index.get(lab, [])) for lab in ("FormCapture", "DataEntity")}
    for kind in ("screen", "entity", "table"):
        tia_label, rel, label, setting, _ = ITEM_KINDS[kind]
        idx, al = indexes[label], settings.get(setting) or {}
        if not idx.rows:
            continue
        for it in sorted(items.get(kind) or [], key=lambda x: x["id"]):
            alias_keys, candidates = item_names(kind, it)
            declared = [("tenant_config", n) for a in alias_keys for n in al.get(a, [])] + \
                       [("tia-services.yaml", n) for a in alias_keys for n in (it.get("declared") or {}).get(a, [])]
            tid = it["id"]
            rej = {e for e, d in (dec.get(tid) or {}).items() if d.get("decision") == "reject"}
            who = {"item": tid, "label": label}
            common = dict(to_label=label, from_label=tia_label, tia_id=tid, tia_name=it.get("name"), item_kind=kind, service=it.get("service"))
            found, settled = False, set()
            for e, d in sorted((dec.get(tid) or {}).items()):
                if d.get("decision") in POSITIVE and e in idx.by_eid:
                    node = idx.by_eid[e]
                    link(it["key"], rel, node, node["domain"], DECLARED, "reviewed", "review", "confirmed", decided_by=d.get("by"),
                         decided_at=d.get("at"), **common)
                    linked[label].add(e)
                    settled.add(node["domain"])
                    found = True
            for domain in scope(it.get("service"), it.get("repo")):
                if domain in settled:
                    continue
                pref = prefer(it.get("service"), it.get("repo"), domain)
                done = False
                for source, name in declared:
                    hits, how = idx.find(scope_id, domain, name)
                    _note(gaps, tid, hits, rej, who)
                    hits, tb = _pick([h for h in hits if h["entity_id"] not in rej], pref)
                    if len(hits) == 1:
                        link(it["key"], rel, hits[0], domain, NAME_MATCH if tb else DECLARED, "declared", source, how,
                             rule="INF-KF-PRODUCT" if tb else None, **common)
                        linked[label].add(hits[0]["entity_id"])
                        done = found = True
                        break
                    if len(hits) > 1:
                        gaps["ambiguous"].append({"item": tid, "tia_id": tid, "kind": kind, "service": it.get("service"), "repo": it.get("repo"),
                                                  "name": name, "domain": domain, "label": label,
                                                  "candidates": [h["entity_id"] for h in hits], "options": [_option(h) for h in hits]})
                if done or declared or not settings.get("link_name_matches", True):
                    continue
                seen: dict = {}
                for name in candidates:
                    hits, how = idx.find(scope_id, domain, name)
                    _note(gaps, tid, hits, rej, who)
                    for h in hits:
                        if h["entity_id"] not in rej:
                            seen.setdefault(h["entity_id"], (h, how))
                nodes, tb = _pick([v[0] for v in seen.values()], pref)
                if len(nodes) == 1:
                    link(it["key"], rel, nodes[0], domain, NAME_MATCH, "name", "name", seen[nodes[0]["entity_id"]][1],
                         rule="INF-KF-PRODUCT" if tb else "INF-KF-NAME", **common)
                    linked[label].add(nodes[0]["entity_id"])
                    found = True
                elif len(nodes) > 1:
                    gaps["ambiguous"].append({"item": tid, "tia_id": tid, "kind": kind, "service": it.get("service"), "repo": it.get("repo"),
                                              "name": it.get("name"), "domain": domain, "label": label, "candidates": sorted(seen),
                                              "options": [_option(seen[e][0]) for e in sorted(seen)]})
            for source, name in declared:
                if not any(idx.find(scope_id, d, name)[0] for d in {r["domain"] for r in idx.rows}):
                    gaps["declared_not_found"].append({"item": tid, "name": name, "source": source, "label": label})
            if not found:
                gaps["unlinked_screens" if kind == "screen" else "unlinked_data"].append(
                    {"item": tid, "tia_id": tid, "kind": kind, "name": it.get("name"), "service": it.get("service"), "repo": it.get("repo")})
    for label, gap in (("FormCapture", "forms_without_screen"), ("DataEntity", "data_entities_without_code")):
        for r in sorted(indexes[label].rows, key=lambda r: (r["domain"], r.get("name") or "")):
            if r["entity_id"] not in linked[label] and r["domain"] in domains:
                gaps[gap].append({"name": r.get("name"), "domain": r["domain"], "entity_id": r["entity_id"]})


def _note(gaps: dict, tid: str, hits: list, rej: set, who: dict) -> None:
    for h in hits:
        if h["entity_id"] in rej and not any(g["tia_id"] == tid and g["entity_id"] == h["entity_id"] for g in gaps["rejected_by_review"]):
            gaps["rejected_by_review"].append({"tia_id": tid, "entity_id": h["entity_id"], "name": h.get("name"), "domain": h.get("domain"), **who})


# ---- product mapping: domain > product line > function > sub-function (docs/KF-BRIDGE.md section 11) ----------------------------------------
def place_key(p: dict) -> str:
    """A stable text for one place: `consumer_retail|retail cards|payment processing`, keys under Knowledge Fabric's normalisation."""
    return "|".join([p["domain"]] + [kid.key(lab, p[f]) or "" for lab, f in (("ProductLine", "product_line"), ("Function", "function"),
                                                                             ("SubFunction", "sub_function")) if p.get(f)])


def place_label(p: dict) -> str:
    """`Retail Cards > Payment Processing (consumer_retail)`."""
    return " > ".join(x for x in (p.get("product_line"), p.get("function"), p.get("sub_function")) if x) + f" ({p['domain']})"


def _find_named(nodes: list, label: str, name: str) -> list:
    k = kid.key(label, name)
    if not k:
        return []
    exact = [n for n in nodes if kid.key(label, n.get("name") or "") == k]
    return exact or [n for n in nodes if k in {kid.key(label, a) for a in n.get("aliases") or []}]


def _closest(nodes: list, name: str) -> Optional[str]:
    """A hint for a name that is not in the tree: the node with the most words in common (a rename or a typo)."""
    w = _words(name)
    best = max(((len(w & _words(" ".join([n.get("name") or ""] + list(n.get("aliases") or [])))), n.get("name") or "") for n in nodes),
               default=(0, None))
    return best[1] if best[0] else None


def resolve_place(catalog: dict, p: dict) -> tuple[Optional[dict], Optional[dict]]:
    """One place named in a setting -> (the place with its Knowledge Fabric node, None) or (None, why not: {"missing", "hint"})."""
    dom = next((d for d in (catalog or {}).get("domains") or [] if d["domain"] == p["domain"]), None)
    if dom is None:
        return None, {"missing": "domain", "hint": None}
    pls = _find_named(dom.get("product_lines") or [], "ProductLine", p["product_line"])
    if len(pls) != 1:
        return None, {"missing": "product_line", "hint": _closest(dom.get("product_lines") or [], p["product_line"]) if not pls else None,
                      **({"ambiguous": [x.get("name") for x in pls]} if pls else {})}
    pl = pls[0]
    out = {"domain": p["domain"], "product_line": pl["name"],
           "product_line_id": pl.get("product_line_id") or f"DOMAIN:{p['domain']}|PRODUCT_LINE:{pl['name']}", "level": "ProductLine"}
    node = pl
    if p.get("function"):
        fns = _find_named(pl.get("functions") or [], "Function", p["function"])
        if len(fns) != 1:
            return None, {"missing": "function", "hint": _closest(pl.get("functions") or [], p["function"]) if not fns else None}
        node, out["function"], out["level"] = fns[0], fns[0]["name"], "Function"
        if p.get("sub_function"):
            sfs = _find_named(fns[0].get("sub_functions") or [], "SubFunction", p["sub_function"])
            if len(sfs) != 1:
                return None, {"missing": "sub_function", "hint": _closest(fns[0].get("sub_functions") or [], p["sub_function"]) if not sfs else None}
            node, out["sub_function"], out["level"] = sfs[0], sfs[0]["name"], "SubFunction"
    if node.get("entity_id"):
        out["target"] = {"key": "entity_id", "value": node["entity_id"], "name": node["name"]}
    elif out["level"] == "ProductLine":                       # written by Knowledge Fabric's review writer, keyed by product_line_id only
        out["target"] = {"key": "product_line_id", "value": out["product_line_id"], "name": node["name"]}
    else:
        return None, {"missing": "id", "hint": None}
    return out, None


def resolve_products(services: list, settings: dict, catalog: Optional[dict], decisions: Optional[dict] = None) -> tuple[dict, dict]:
    """Every service's places in the business tree, from the first source that names any (A person always chooses them):
    1. knowledge_fabric.service_products, 2. knowledge_fabric.repository_products, 3. business_products in the repository's
    tia-services.yaml (services: {"declared_products": [place]}), 4. a reviewer's accepted suggestion. A suggestion from the linked Systems
    is never applied here (product_suggestions lists it for review). -> ({service: [place with its node, source, status]},
    {"product_not_found": [...], "unmapped_repositories": [...], "unknown_domains": [domain]})."""
    accepted: dict = {}
    for (t, target), d in sorted((decisions or {}).items()):
        if d.get("kind") == "product" and d.get("decision") == "accept" and d.get("place"):
            accepted.setdefault(t, []).append({**d["place"], "_d": d})
    out, not_found, unknown = {}, [], set()
    for s in sorted(services, key=lambda x: x["service"]):
        sources = [("tenant_config", (settings.get("service_products") or {}).get(s["service"])),
                   ("tenant_config", (settings.get("repository_products") or {}).get(s.get("repo"))),
                   ("tia-services.yaml", s.get("declared_products")), ("review", accepted.get(tia_id(s["key"])))]
        source, places = next(((src, ps) for src, ps in sources if ps), (None, None))
        if not places:
            continue
        got = []
        for p in places:
            d = p.pop("_d", None) if isinstance(p, dict) else None
            r, why = resolve_place(catalog or {}, p)
            if r is None:
                if why["missing"] == "domain":
                    unknown.add(p.get("domain"))
                not_found.append({"service": s["service"], "repo": s.get("repo"), "source": source, "place": place_label(p), **why})
                continue
            got.append({**r, "source": source, "status": DECLARED,
                        **({"decided_by": d.get("by"), "decided_at": d.get("at")} if d else {})})
        if got:
            out[s["service"]] = sorted({place_key(g): g for g in got}.values(), key=place_key)
    repos: dict = {}
    for s in services:
        repos.setdefault(s.get("repo"), []).append(s["service"])
    unmapped = [{"repo": r, "services": sorted(sv)} for r, sv in sorted(repos.items(), key=lambda x: str(x[0])) if not any(out.get(x) for x in sv)]
    return out, {"product_not_found": not_found, "unmapped_repositories": unmapped, "unknown_domains": sorted(d for d in unknown if d)}


def product_suggestions(services: list, links: list, systems: "_Index", products: dict, decisions: dict) -> list:
    """For a service without a place: the places of the Systems it is linked to (their PART_OF chain in Knowledge Fabric's tree). A suggestion
    is for a person to accept or reject (docs/KF-BRIDGE.md section 12); it is never applied by itself. A rejected place is not suggested again."""
    out = []
    for s in sorted(services, key=lambda x: x["service"]):
        if products.get(s["service"]):
            continue
        tid, seen = tia_id(s["key"]), {}
        for ln in links:
            if ln["from"] != s["key"] or ln["rel"] != "REALIZES" or ln["to_label"] != "System":
                continue
            node = systems.by_eid.get(ln["to"]) or {}
            pls = list(node.get("product_lines") or [])
            fns = list(node.get("functions") or [])
            for pl in pls:
                p = {"domain": ln["domain"], "product_line": pl, **({"function": fns[0]} if len(pls) == 1 and len(fns) == 1 else {})}
                d = decisions.get((tid, place_key(p)))
                if d and d.get("decision") == "reject":
                    continue
                cur = seen.setdefault(place_key(p), {"place": p, "via": []})
                cur["via"].append({"system": ln["to_name"], "entity_id": ln["to"], "status": ln["status"]})
        for k, v in sorted(seen.items()):
            out.append({"service": s["service"], "repo": s.get("repo"), "tia_id": tid, "place": v["place"], "place_key": k,
                        "label": place_label(v["place"]), "via": v["via"], "status": NAME_MATCH})
    return out


def link_suggestions(gaps: dict, index: dict, items: dict, services: list, links: list, scope, per_item: int = 3,
                     min_score: float = 0.34) -> list:
    """Up to three Knowledge Fabric names close to an unlinked service, screen, entity or table (shared words), for a person to link or
    dismiss. Only nodes no other TIA item is linked to are suggested. Never linked by themselves."""
    claimed = {ln["to"] for ln in links}
    out = []

    def best(name_words: set, label: str, domains: list) -> list:
        scored = []
        for r in index.get(label, []):
            if r["entity_id"] in claimed or r["domain"] not in domains:
                continue
            kw = _words(" ".join([r.get("name") or ""] + list(r.get("aliases") or [])))
            if not kw or not name_words:
                continue
            score = round(len(name_words & kw) / len(name_words | kw), 3)
            if score >= min_score:
                scored.append({**_option(r), "score": score, "common": sorted(name_words & kw)})
        return sorted(scored, key=lambda c: (-c["score"], c["name"] or "", c["entity_id"]))[:per_item]
    repo_of = {s["service"]: s.get("repo") for s in services}
    for g in gaps.get("unmapped_service") or []:
        c = best(_words(f"{g['service']} {g.get('repo') or ''}"), "System", scope(g["service"], g.get("repo")))
        if c:
            out.append({"tia_id": g["tia_id"], "kind": "service", "label": "System", "rel": "REALIZES", "service": g["service"],
                        "repo": repo_of.get(g["service"]), "name": g["service"], "candidates": c})
    for gap, label in (("unlinked_screens", "FormCapture"), ("unlinked_data", "DataEntity")):
        for g in gaps.get(gap) or []:
            it = next((x for x in items.get(g["kind"]) or [] if x["id"] == g["item"]), {})
            names = item_names(g["kind"], it)[1] if it else [g.get("name") or ""]
            c = best(set().union(*[_words(n) for n in names]) if names else set(), label, scope(g.get("service"), g.get("repo")))
            if c:
                out.append({"tia_id": g["item"], "kind": g["kind"], "label": label, "rel": ITEM_KINDS[g["kind"]][1],
                            "service": g.get("service"), "repo": g.get("repo"), "name": g.get("name"), "candidates": c})
    return out


def _singular(word: str) -> str:
    w = word
    for a, b in (("ies", "y"), ("sses", "ss"), ("xes", "x"), ("ches", "ch"), ("shes", "sh")):
        if w.lower().endswith(a) and len(w) > len(a) + 1:
            return w[: -len(a)] + b
    return w[:-1] if w.lower().endswith("s") and not w.lower().endswith("ss") and len(w) > 3 else w


def _strip_suffix(name: str) -> str:
    for suf in TECH_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf) + 1 and name[-len(suf) - 1].islower():
            return name[: -len(suf)]
    return name


def item_names(kind: str, item: dict) -> tuple[list, list]:
    """-> (names an alias may be declared under, names tried for an exact Knowledge Fabric key), for one TIA screen, entity or table."""
    if kind == "screen":
        route = str(item.get("route") or item.get("name") or "")
        seg = next((p for p in reversed(route.strip("/").split("/")) if p and not p.startswith((":", "{", "[", "*"))), "")
        return (list(dict.fromkeys(x for x in (route, item.get("component"), item.get("name")) if x)),
                [x for x in (item.get("component"), seg) if x])
    if kind == "entity":
        n = str(item.get("name") or "")
        return [x for x in (n, item.get("table")) if x], list(dict.fromkeys(x for x in (n, _strip_suffix(n)) if x))
    n = str(item.get("name") or "")
    return [n] if n else [], list(dict.fromkeys(x for x in (n, _singular(n)) if x))


# ---- rules: TIA proposes, a person decides --------------------------------------------------------------------------------------------------
def _words(text: str) -> set:
    """Lower-case word stems of identifiers and sentences: camelCase and snake_case split, short and common words dropped."""
    out = set()
    for w in re.findall(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])", re.sub(r"[^A-Za-z0-9]+", " ", str(text or ""))):
        w = w.lower()
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        if len(w) >= 3 and w not in STOP:
            out.add(w)
    return out


def proposal_id(tia_id: str, entity_id: str) -> str:
    return "kfr-" + hashlib.sha256(f"{tia_id}|{entity_id}".encode()).hexdigest()[:16]


def propose_rules(rules: list, kf_rules: list, decisions: dict, per_rule: int = 3, min_score: float = 0.15) -> list:
    """Candidate (code rule, Knowledge Fabric Rule) pairs for a person to confirm or reject. A code rule is a condition (`not p['disputed']`)
    and a Knowledge Fabric rule a sentence, so names never link by themselves: a pair is proposed when the rule's fields appear in the
    sentence (shared field words) or the words of the condition, its fields and its function overlap the sentence enough. Score: 0.6 x the
    share of the rule's field words found + 0.4 x word overlap (Jaccard). Up to `per_rule` per code rule; rejected pairs are not proposed
    again, decided pairs carry their decision. rules: [{"key", "id", "expr", "fields", "function", "service", "repo"}];
    kf_rules: [{entity_id, name, description, domain}]."""
    kf = [(r, _words(f"{r.get('name') or ''} {r.get('description') or ''}")) for r in kf_rules]
    out = []
    for r in sorted(rules, key=lambda x: x["id"]):
        fw = set().union(*[_words(f) for f in r.get("fields") or []]) if r.get("fields") else set()
        tw = fw | _words(r.get("expr") or "") | _words(str(r.get("function") or "").split("::")[-1])
        scored = []
        for k, kw in kf:
            shared = fw & kw
            jac = len(tw & kw) / len(tw | kw) if tw | kw else 0.0
            score = round((0.6 * len(shared) / len(fw) if fw else 0.0) + 0.4 * jac, 3)
            if (shared or jac >= min_score) and score >= min_score:
                scored.append((score, k, sorted(shared), sorted(tw & kw)))
        scored.sort(key=lambda x: (-x[0], x[1].get("name") or "", x[1]["entity_id"]))
        for score, k, shared, common in scored:
            d = decisions.get((r["id"], k["entity_id"]))
            if d and d.get("decision") == "reject":
                continue
            out.append({"id": proposal_id(r["id"], k["entity_id"]), "tia_rule": r["id"], "expr": r.get("expr"), "fields": r.get("fields") or [],
                        "function": r.get("function"), "service": r.get("service"), "repo": r.get("repo"),
                        "kf_rule": k.get("name"), "kf_entity_id": k["entity_id"], "kf_description": (k.get("description") or "")[:300] or None,
                        "domain": k["domain"], "score": score, "shared_fields": shared, "common_words": common,
                        "status": (d or {}).get("decision", "pending").replace("confirm", "confirmed"),
                        **({"decided_by": d.get("by"), "decided_at": d.get("at")} if d else {})})
            if sum(1 for p in out if p["tia_rule"] == r["id"]) >= per_rule:
                break
    return out


POSITIVE_ANY = ("confirm", "choose", "accept")


def decision_target(d: dict) -> tuple[Optional[str], Optional[str], str]:
    """A kf_link_decision record -> (tia_id, target, kind). Rule decisions recorded before the review queue carry `tia_rule` and no kind."""
    kind = d.get("kind") or ("rule" if d.get("tia_rule") else "link")
    t = d.get("tia_id") or d.get("tia_rule")
    target = d.get("place_key") if kind == "product" else d.get("kf_entity_id")
    return t, target, kind


def latest_decisions(records: list, two_person: bool = False) -> dict:
    """kf_link_decision evidence records (newest first) -> {(tia_id, target): decision}. target: the Knowledge Fabric entity id (links,
    rules, stories) or the place key (products). The newest decision on each pair wins; `reset` (undo) and `defer` mean no decision. With
    `two_person`, a positive decision (confirm, choose, accept) counts only when two different people made it since the pair's last other
    decision; until then the pair is `awaiting_second`, which links nothing. A rejection counts at once."""
    grouped: dict = {}
    for rec in records:
        d = rec.get("data") or rec
        t, target, kind = decision_target(d)
        if t is not None and target is not None:
            grouped.setdefault((t, target), []).append({**d, "kind": kind})
    out: dict = {}
    for k, ds in grouped.items():
        top = ds[0]
        if top.get("decision") in ("reset", "defer", None):
            continue
        same = []
        for d in ds:
            if d.get("decision") != top["decision"]:
                break
            same.append(d)
        actors = list(dict.fromkeys(d.get("actor") for d in same if d.get("actor")))
        decision = top["decision"]
        if two_person and decision in POSITIVE_ANY and len(actors) < 2:
            decision = "awaiting_second"
        out[k] = {"decision": decision, "by": top.get("actor"), "at": top.get("at"), "comment": top.get("comment"), "kind": top["kind"],
                  "label": top.get("label") or ("Rule" if top["kind"] == "rule" else None), "kf_name": top.get("kf_name") or top.get("kf_rule"),
                  "rel": top.get("rel"), "approvals": actors, **({"place": top["place"]} if top.get("place") else {}),
                  **({"item_id": top["item_id"]} if top.get("item_id") else {})}
    return out


def issue_keys(text: str) -> list:
    """Jira-style issue keys (PAY-4521) in a text, in order of appearance, without repeats."""
    return list(dict.fromkeys(ISSUE_KEY.findall(str(text or ""))))


def summary(planned: dict) -> dict:
    links = planned["links"]
    by_rel: dict = {}
    for x in links:
        k = f"{x.get('from_label', '')}-{x['rel']}->{x['to_label']}"
        by_rel[k] = by_rel.get(k, 0) + 1
    props = planned.get("proposals") or []
    return {"realizes": sum(1 for x in links if x["rel"] == "REALIZES" and x["to_label"] == "System"),
            "same_as": sum(1 for x in links if x["rel"] == "SAME_AS"),
            "screens": sum(1 for x in links if x["to_label"] == "FormCapture"),
            "data": sum(1 for x in links if x["to_label"] == "DataEntity"),
            "rules": sum(1 for x in links if x["rel"] == "ENFORCES"),
            "products": sum(1 for x in links if x["rel"] == "PLACED_IN"),
            "reviewed": sum(1 for x in links if x.get("source") == "review"),
            "product_suggestions": len(planned.get("product_suggestions") or []),
            "link_suggestions": len(planned.get("link_suggestions") or []),
            "by_relationship": dict(sorted(by_rel.items())),
            "by_status": {s: sum(1 for x in links if x["status"] == s) for s in sorted({x["status"] for x in links})},
            "rule_proposals_pending": sum(1 for p in props if p["status"] == "pending"),
            "gaps": {k: len(v) for k, v in planned["gaps"].items() if v}}


# ---- business impact (in an analysis) ------------------------------------------------------------------------------------------------------
LIMITS = ["Knowledge Fabric's relationships (requirement impacts system, test case covers requirement, ...) come from its extraction of the "
          "customer's documents; TIA does not verify them. The evidence status shown is the TIA side's link from code to the system.",
          "Business context is the state captured by the last bridge run (see fingerprint and pulled_at), pinned in this analysis's evidence "
          "snapshot; later changes in Knowledge Fabric show in the next analysis."]
ITEM_WORDS = {"FormCapture": "form", "DataEntity": "data entity", "Rule": "business rule"}


def touched_item(tia_id: str, touched: set) -> bool:
    """Is this screen, entity, table or rule part of the change: the node itself, a rule's function (rule ids are `<function>#ruleN`), or a
    table whose column changed (`col:<db>:<table>.<column>` -> `table:<db>:<table>`)."""
    if tia_id in touched or tia_id.split("#")[0] in touched:
        return True
    if tia_id.startswith("table:"):
        pre = "col:" + tia_id[len("table:"):] + "."
        return any(t.startswith(pre) for t in touched)
    return False


def products_rollup(context: dict, services: list, repo_of: dict, test_cases: list, stories: list, entry_points: dict) -> list:
    """The product lines a change reaches (docs/KF-BRIDGE.md section 11): one row per (domain, product line) of the affected services'
    places, with the services and how each got there (declared by a person, or inherited through a linked System and therefore inferred),
    the functions, the entry points to test, the business test cases and the user stories. The changed service's product lines first."""
    places = context.get("products") or {}
    rows: dict = {}
    for i, svc in enumerate(services):
        for p in places.get(svc) or []:
            k = (p["domain"], p["product_line_id"])
            row = rows.setdefault(k, {"domain": p["domain"], "product_line": p["product_line"], "product_line_id": p["product_line_id"],
                                      "functions": [], "services": [], "changed": False, "status": p["status"]})
            if p.get("function") and p["function"] not in row["functions"]:
                row["functions"].append(p["function"])
            if not any(x["service"] == svc for x in row["services"]):
                row["services"].append({"service": svc, "repo": repo_of.get(svc), "source": p["source"], "status": p["status"],
                                        **({"function": p["function"]} if p.get("function") else {}),
                                        **({"via": p["via"]} if p.get("via") else {})})
            row["changed"] = row["changed"] or i == 0
            if RANK.get(p["status"], 0) > RANK.get(row["status"], 0):
                row["status"] = p["status"]
    out = []
    for row in rows.values():
        svcs = {x["service"] for x in row["services"]}
        row["entry_points"] = sum(entry_points.get(s, 0) for s in svcs)
        row["test_cases"] = sorted({c["id"] for c in test_cases if any(v.get("service") in svcs for v in c["via"])})
        row["user_stories"] = sorted({st["key"] for st in stories}) if row["changed"] else []
        row["functions"].sort()
        out.append(row)
    return sorted(out, key=lambda r: (not r["changed"], r["domain"], r["product_line"]))


def products_phrase(rows: list, limit: int = 3) -> str:
    """`Affects Retail Cards (consumer_retail) and Corporate Cards (corporate_commercial).`"""
    names = [f"{r['product_line']} ({r['domain']})" for r in rows]
    if not names:
        return ""
    shown = names[:limit] + ([f"{len(names) - limit} more"] if len(names) > limit else [])
    return "Affects " + (shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]) + "."


def business_impact(context: Optional[dict], services: list, repo_of: dict, suite_ids: set, settings: dict,
                    touched: Optional[set] = None, issue_refs: Optional[list] = None, target_repo: Optional[str] = None,
                    entry_points: Optional[dict] = None) -> tuple[Optional[dict], list]:
    """The business side of a change. context: the data of the latest `kf_context` evidence record; services: the services the change
    affects (the changed one first); repo_of: service -> repository; suite_ids: the written test case ids of the analysed repositories;
    touched: the TIA node ids the change and its findings reach (screens, entities, tables and rules are shown only when touched);
    issue_refs: [{"key", "commit", "subject"}] issue keys found in the change's commit messages; target_repo: the changed repository;
    entry_points: service -> the number of entry points to test (the report's test scope), for the product roll-up.
    -> (business_impact section or None, unknowns)."""
    unknowns = []
    if not settings.get("enabled"):
        return None, []
    if not context:
        return None, [{"area": "kf_context_unavailable", "affects_confidence": False,
                       "reason": "The Knowledge Fabric bridge is enabled but has not run yet for this tenant: no business impact is shown. "
                                 "Run it with POST /api/v1/kf/relink."}]
    by_service = context.get("services") or {}
    suite_keys = {kid.key("TestCase", i) for i in suite_ids} - {None}
    systems, cases, unmapped = [], {}, []
    for svc in services:
        entries = by_service.get(svc) or []
        if not entries:
            unmapped.append(svc)
            continue
        for e in entries:
            systems.append({"service": svc, "repo": repo_of.get(svc), "system": e["system"], "domain": e["domain"], "entity_id": e["entity_id"],
                            "link": e["link"], **{k: e.get(k, []) for k in ("requirements", "user_stories", "processes", "defects", "incidents",
                                                                              "integrations")},
                            **({"truncated": e["truncated"]} if e.get("truncated") else {})})
            for tc in e.get("test_cases", []):
                k = (e["domain"], tc["entity_id"])
                via = {"service": svc, "repo": repo_of.get(svc), "system": e["system"], "label": "System", "rel": "REALIZES",
                       "requirements": tc.get("covers", []), "status": e["link"]["status"]}
                cur = cases.get(k)
                if cur is None:
                    in_suite = bool(set(tc.get("tia_case_ids") or []) & suite_ids) or kid.key("TestCase", tc["name"]) in suite_keys
                    cases[k] = cur = {"id": tc["name"], "entity_id": tc["entity_id"], "domain": e["domain"], "via": [],
                                      "evidence_status": via["status"], "also_in_repository_suite": in_suite,
                                      **({"source_file": tc["source_file"]} if tc.get("source_file") else {})}
                cur["via"].append(via)
                if RANK.get(via["status"], 0) > RANK.get(cur["evidence_status"], 0):
                    cur["evidence_status"] = via["status"]

    def add_case(tc, domain, via):
        k = (domain, tc["entity_id"])
        cur = cases.get(k)
        if cur is None:
            in_suite = bool(set(tc.get("tia_case_ids") or []) & suite_ids) or kid.key("TestCase", tc["name"]) in suite_keys
            cases[k] = cur = {"id": tc["name"], "entity_id": tc["entity_id"], "domain": domain, "via": [], "evidence_status": via["status"],
                              "also_in_repository_suite": in_suite}
        cur["via"].append(via)
        if RANK.get(via["status"], 0) > RANK.get(cur["evidence_status"], 0):
            cur["evidence_status"] = via["status"]

    # phase 7: the forms, data entities and business rules the change touches, and what Knowledge Fabric ties to them
    items = []
    for svc in services:
        for it in (context.get("items") or {}).get(svc) or []:
            if touched is not None and not touched_item(it["tia_id"], touched):
                continue
            items.append({"service": svc, "repo": repo_of.get(svc), **{k: v for k, v in it.items() if k != "test_cases"},
                          "test_cases": [tc["name"] for tc in it.get("test_cases", [])]})
            for tc in it.get("test_cases", []):
                add_case(tc, it["domain"], {"service": svc, "repo": repo_of.get(svc), "system": it["kf_name"], "label": it["kf_label"],
                                            "rel": it["rel"], "through": f"{it['kind']} {it['tia_name']}", "requirements": tc.get("covers", []),
                                            "status": it["link"]["status"]})
    # phase 7: user stories named by an issue key in the change's commit messages
    stories, missing = [], []
    index = context.get("stories") or {}
    for ref in issue_refs or []:
        found = index.get(ref["key"]) or []
        if not found:
            if ref["key"] not in missing:
                missing.append(ref["key"])
            continue
        for st in found:
            cur = next((s for s in stories if s["entity_id"] == st["entity_id"]), None)
            if cur is None:
                cur = {"key": ref["key"], "story": st["name"], "entity_id": st["entity_id"], "domain": st["domain"], "repo": target_repo,
                       "status": ID_MATCH, "commits": [], **{k: st.get(k, []) for k in ("requirements", "systems")},
                       "test_cases": [tc["name"] for tc in st.get("test_cases", [])]}
                stories.append(cur)
                through = f"commit {ref['commit'][:7]}" if ref.get("commit") else str(ref.get("source") or "pull request").replace("_", " ")
                for tc in st.get("test_cases", []):
                    add_case(tc, st["domain"], {"service": services[0] if services else None, "repo": target_repo,
                                                "system": f"user story {ref['key']}", "label": "UserStory", "rel": "IMPLEMENTS_CHANGE",
                                                "through": through, "requirements": tc.get("covers", []), "status": ID_MATCH})
            if ref.get("commit") and ref["commit"][:12] not in cur["commits"]:
                cur["commits"].append(ref["commit"][:12])
            if ref.get("source") and ref["source"] not in cur.setdefault("named_in", []):
                cur["named_in"].append(ref["source"])
    if missing:
        unknowns.append({"area": "kf_issue_keys_not_found", "affects_confidence": False, "count": len(missing),
                         "reason": f"The change's commit messages name {len(missing)} issue key(s) that Knowledge Fabric has no user story for "
                                   f"({', '.join(missing[:10])}): if they are Jira stories, ingest them into Knowledge Fabric (the key as the "
                                   "story's name or alias).", "items": missing})
    test_cases = sorted(cases.values(), key=lambda c: (c["also_in_repository_suite"], -RANK.get(c["evidence_status"], 0), c["domain"], c["id"]))
    if unmapped:
        unknowns.append({"area": "kf_systems_unmapped", "affects_confidence": False, "count": len(unmapped),
                         "reason": f"{len(unmapped)} affected service(s) are linked to no Knowledge Fabric System ({', '.join(unmapped[:10])}): "
                                   "their business requirements and test cases are not shown. Declare the System names in "
                                   "knowledge_fabric.system_aliases or in business_systems of tia-services.yaml.",
                         "items": [{"service": s, "repo": repo_of.get(s)} for s in unmapped]})
    new_cases = [c for c in test_cases if not c["also_in_repository_suite"]]
    products = products_rollup(context, services, repo_of, test_cases, stories, entry_points or {})
    unplaced = [s for s in services if s in by_service and not (context.get("products") or {}).get(s)]
    if context.get("products") is not None and unplaced:
        unknowns.append({"area": "kf_services_without_product", "affects_confidence": False, "count": len(unplaced),
                         "reason": f"{len(unplaced)} affected service(s) have no place in Knowledge Fabric's business tree "
                                   f"({', '.join(unplaced[:10])}): the products they serve are not shown. Map their repositories to a "
                                   "domain and product line (knowledge_fabric.repository_products, or business_products in tia-services.yaml).",
                         "items": [{"service": s, "repo": repo_of.get(s)} for s in unplaced]})
    if systems or items or stories:
        where = [f"{len(systems)} system(s)"] if systems else []
        for label in ("FormCapture", "DataEntity", "Rule"):
            n = len({i["entity_id"] for i in items if i["kf_label"] == label})
            if n:
                where.append(f"{n} {ITEM_WORDS[label]}(s)")
        headline = (f"Knowledge Fabric: {len(test_cases)} business test case(s) cover requirements on the {', '.join(where) or 'items'} "
                    "this change affects" + (f" ({len(new_cases)} not in your repository suite)" if test_cases else "") + ".")
        if stories:
            headline += f" The change implements user stor{'y' if len(stories) == 1 else 'ies'} {', '.join(s['key'] for s in stories)}."
    else:
        headline = "Knowledge Fabric: no affected service is linked to a Knowledge Fabric System."
    if products:
        headline = products_phrase(products) + " " + headline
    requirements = {r["entity_id"] for s in systems for r in s["requirements"]} | {r["entity_id"] for i in items for r in i.get("requirements", [])} \
        | {r["entity_id"] for s in stories for r in s.get("requirements", [])}
    section = {"source": "knowledge_fabric", "kf_customer_id": context.get("customer_id"), "kf_tenant_id": context.get("tenant_id"),
               "fingerprint": context.get("fingerprint"), "pulled_at": context.get("pulled_at"), "bridge_version": context.get("bridge_version"),
               "products": products, "systems": systems, "items": items, "user_stories": stories, "test_cases": test_cases,
               "summary": {"products": len(products), "systems": len(systems),
                           "forms": len({i["entity_id"] for i in items if i["kf_label"] == "FormCapture"}),
                           "data_entities": len({i["entity_id"] for i in items if i["kf_label"] == "DataEntity"}),
                           "business_rules": len({i["entity_id"] for i in items if i["kf_label"] == "Rule"}),
                           "user_stories": len(stories), "requirements": len(requirements),
                           "test_cases": len(test_cases), "test_cases_not_in_repository_suite": len(new_cases),
                           "open_defects": len({d["entity_id"] for s in systems for d in s["defects"]} | {d["entity_id"] for i in items for d in i.get("defects", [])}),
                           "incidents": len({i["entity_id"] for s in systems for i in s["incidents"]} | {x["entity_id"] for i in items for x in i.get("incidents", [])}),
                           "headline": headline},
               "limits": list(LIMITS) + (["User stories are found by issue keys in the change's commit messages, and in the pull request's "
                                           "title and branch name when the source-control host sent them."] if settings.get("link_issue_keys", True) else [])}
    return section, unknowns


def redact(section: Optional[dict], readable: set) -> int:
    """Keep only what a reader may see: systems and test case chains of services in repositories they cannot read are dropped."""
    if not section:
        return 0
    keys = ("systems", "items", "user_stories", "test_cases", "products")
    before = sum(len(section.get(k) or []) for k in keys)
    for k in ("systems", "items", "user_stories"):
        section[k] = [s for s in section.get(k) or [] if s.get("repo") in readable]
    for c in section["test_cases"]:
        c["via"] = [v for v in c["via"] if v.get("repo") in readable]
    section["test_cases"] = [c for c in section["test_cases"] if c["via"]]
    for row in section.get("products") or []:
        row["services"] = [x for x in row["services"] if x.get("repo") in readable]
    if "products" in section:
        section["products"] = [r for r in section["products"] if r["services"]]
    return before - sum(len(section.get(k) or []) for k in keys)

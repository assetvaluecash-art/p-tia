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

from .kfsettings import DEFAULTS, canonical, validate          # noqa: F401  (re-exported: the setting lives in the rules layer)
DECLARED, ID_MATCH, NAME_MATCH = "contract_confirmed", "source_confirmed", "inferred"
RANK = {"source_confirmed": 3, "contract_confirmed": 2, "inferred": 1}
GAP_KINDS = ("unmapped_service", "systems_without_code", "declared_not_found", "ambiguous", "test_cases_only_in_tia",
             "test_cases_only_in_kf", "unknown_domains", "unlinked_screens", "unlinked_data", "forms_without_screen",
             "data_entities_without_code")
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
        self.label, self.by_id, self.by_key, self.rows = label, {}, {}, rows
        for r in rows:
            self.by_id[(r["domain"], r["entity_id"])] = r
            keys = {kid.key(label, r.get("name") or "")} | {kid.key(label, a) for a in (r.get("aliases") or []) if isinstance(a, str)}
            for k in keys - {None}:
                self.by_key.setdefault((r["domain"], k), {})[r["entity_id"]] = r

    def find(self, scope_id: str, domain: str, name: str) -> tuple[list, str]:
        """-> (matching nodes, how found). The computed entity id first; then name and aliases within the domain."""
        k = kid.key(self.label, name)
        if not k:
            return [], "dropped"
        computed = kid.entity_id(scope_id, domain, self.label, k)
        if (domain, computed) in self.by_id:
            return [self.by_id[(domain, computed)]], "computed"
        hits = list((self.by_key.get((domain, k)) or {}).values())
        return sorted(hits, key=lambda r: r["entity_id"]), "looked_up"


def plan(services: list, test_cases: list, index: dict, settings: dict, kf_tenant_id: Optional[str], domains: list,
         items: Optional[dict] = None, rules: Optional[list] = None, decisions: Optional[dict] = None) -> dict:
    """Plan the bridge links. services: [{"key", "service", "repo", "declared": [System names from tia-services.yaml]}];
    test_cases: [{"key", "id", "repo"}]; index: {label: [rows]} with rows {entity_id, name, aliases, domain[, description]} for System,
    TestCase and (phase 7) FormCapture, DataEntity, Rule; items: {"screen"|"entity"|"table": [item]} (see plan_items); rules: TIA's rules
    found in code (see propose_rules); decisions: the review decisions on rule pairs.
    -> {"links": [...], "gaps": {kind: [...]}, "proposals": [...]} in a deterministic order."""
    scope_id = kid.scope(settings["customer_id"], kf_tenant_id)
    systems, cases = _Index("System", index.get("System", [])), _Index("TestCase", index.get("TestCase", []))
    aliases = settings.get("system_aliases") or {}
    links, gaps = [], {k: [] for k in GAP_KINDS}
    realised: set = set()

    def link(src, rel, node, domain, status, strategy, source, how, rule=None, to_label=None, from_label=None, **extra):
        links.append({"from": src, "from_label": from_label or ("TiaService" if rel == "REALIZES" else "TiaTestCase"), "rel": rel,
                      "to": node["entity_id"], "to_label": to_label or ("System" if rel == "REALIZES" else "TestCase"),
                      "to_name": node.get("name"), "domain": domain, "status": status, "strategy": strategy, "rule": rule, "source": source,
                      "id_check": how, **extra})

    for s in sorted(services, key=lambda x: x["service"]):
        found_any = False
        for domain in domains:
            done = False
            for source, names in (("tenant_config", aliases.get(s["service"]) or []), ("tia-services.yaml", s.get("declared") or [])):
                if done or not names:
                    continue
                for name in names:
                    hits, how = systems.find(scope_id, domain, name)
                    if len(hits) == 1:
                        link(s["key"], "REALIZES", hits[0], domain, DECLARED, "declared", source, how, service=s["service"])
                        realised.add(hits[0]["entity_id"])
                        done = found_any = True
                    elif len(hits) > 1:
                        gaps["ambiguous"].append({"service": s["service"], "name": name, "domain": domain, "label": "System",
                                                  "candidates": [h["entity_id"] for h in hits]})
            if done or not settings.get("link_name_matches", True):
                continue
            seen_nodes: dict = {}
            for name in dict.fromkeys(n for n in (s["service"], s.get("repo")) if n):
                hits, how = systems.find(scope_id, domain, name)
                for h in hits:
                    seen_nodes.setdefault(h["entity_id"], (h, how))
            if len(seen_nodes) == 1:
                h, how = next(iter(seen_nodes.values()))
                link(s["key"], "REALIZES", h, domain, NAME_MATCH, "name", "name", how, rule="INF-KF-NAME", service=s["service"])
                realised.add(h["entity_id"])
                found_any = True
            elif len(seen_nodes) > 1:
                gaps["ambiguous"].append({"service": s["service"], "name": s["service"], "domain": domain, "label": "System",
                                          "candidates": sorted(seen_nodes)})
        if not found_any:
            gaps["unmapped_service"].append({"service": s["service"], "repo": s.get("repo")})
        for source, names in (("tenant_config", aliases.get(s["service"]) or []), ("tia-services.yaml", s.get("declared") or [])):
            for name in names:
                if not any(systems.find(scope_id, d, name)[0] for d in domains):
                    gaps["declared_not_found"].append({"service": s["service"], "name": name, "source": source})
    for r in sorted(systems.rows, key=lambda r: (r["domain"], r.get("name") or "")):
        if r["entity_id"] not in realised:
            gaps["systems_without_code"].append({"system": r.get("name"), "domain": r["domain"], "entity_id": r["entity_id"]})
    kf_case_ids, tia_case_keys = set(), set()
    for tc in sorted(test_cases, key=lambda x: (x["repo"], x["id"])):
        k = kid.key("TestCase", tc["id"])
        if k:
            tia_case_keys.add(k)
        matched = False
        for domain in domains:
            hits, how = cases.find(scope_id, domain, tc["id"])
            for h in hits:                                       # the same id in one domain twice is the same test case
                link(tc["key"], "SAME_AS", h, domain, ID_MATCH, "test_case_id", "test_case_id", how, test_case=tc["id"])
                kf_case_ids.add(h["entity_id"])
                matched = True
        if not matched:
            gaps["test_cases_only_in_tia"].append({"id": tc["id"], "repo": tc["repo"]})
    for r in sorted(cases.rows, key=lambda r: (r["domain"], r.get("name") or "")):
        if r["entity_id"] not in kf_case_ids and kid.key("TestCase", r.get("name") or "") not in tia_case_keys:
            gaps["test_cases_only_in_kf"].append({"id": r.get("name"), "domain": r["domain"]})
    _plan_items(items or {}, index, settings, scope_id, domains, link, gaps)
    proposals = []
    if rules is not None:
        kf_rules = [r for r in index.get("Rule", []) if r["domain"] in domains]
        by_id = {r["entity_id"]: r for r in kf_rules}
        rule_by_id = {r["id"]: r for r in rules}
        for (tia_id, eid), d in sorted((decisions or {}).items()):
            if d.get("decision") == "confirm" and tia_id in rule_by_id and eid in by_id:
                r, k = rule_by_id[tia_id], by_id[eid]
                link(r["key"], "ENFORCES", k, k["domain"], DECLARED, "reviewed", "review", "confirmed", to_label="Rule", from_label="TiaRule",
                     tia_id=tia_id, tia_name=r.get("expr"), service=r.get("service"), decided_by=d.get("by"), decided_at=d.get("at"))
        if settings.get("propose_rules", True):
            proposals = propose_rules(rules, kf_rules, decisions or {})
    links.sort(key=lambda x: (x["rel"], x["from"], x["domain"], x["to"]))
    return {"links": links, "gaps": gaps, "proposals": proposals}


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


def _plan_items(items: dict, index: dict, settings: dict, scope_id: str, domains: list, link, gaps: dict) -> None:
    """Screens -> FormCapture (REALIZES), ORM entities and tables -> DataEntity (STORES): a declared alias first (tenant setting, then
    tia-services.yaml), else one exact Knowledge Fabric key (inferred). items: {kind: [{"key", "id", "name", "service", "repo",
    "declared": {alias name: [Knowledge Fabric names]}, + route/component (screen) or table (entity)}]}."""
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
            found = False
            for domain in domains:
                done = False
                for source, name in declared:
                    hits, how = idx.find(scope_id, domain, name)
                    if len(hits) == 1:
                        link(it["key"], rel, hits[0], domain, DECLARED, "declared", source, how, to_label=label, from_label=tia_label,
                             tia_id=it["id"], tia_name=it.get("name"), item_kind=kind, service=it.get("service"))
                        linked[label].add(hits[0]["entity_id"])
                        done = found = True
                        break
                    if len(hits) > 1:
                        gaps["ambiguous"].append({"item": it["id"], "name": name, "domain": domain, "label": label,
                                                  "candidates": [h["entity_id"] for h in hits]})
                if done or declared or not settings.get("link_name_matches", True):
                    continue
                seen: dict = {}
                for name in candidates:
                    hits, how = idx.find(scope_id, domain, name)
                    for h in hits:
                        seen.setdefault(h["entity_id"], (h, how))
                if len(seen) == 1:
                    h, how = next(iter(seen.values()))
                    link(it["key"], rel, h, domain, NAME_MATCH, "name", "name", how, rule="INF-KF-NAME", to_label=label, from_label=tia_label,
                         tia_id=it["id"], tia_name=it.get("name"), item_kind=kind, service=it.get("service"))
                    linked[label].add(h["entity_id"])
                    found = True
                elif len(seen) > 1:
                    gaps["ambiguous"].append({"item": it["id"], "name": it.get("name"), "domain": domain, "label": label,
                                              "candidates": sorted(seen)})
            for source, name in declared:
                if not any(idx.find(scope_id, d, name)[0] for d in domains):
                    gaps["declared_not_found"].append({"item": it["id"], "name": name, "source": source, "label": label})
            if not found:
                gaps["unlinked_screens" if kind == "screen" else "unlinked_data"].append(
                    {"item": it["id"], "kind": kind, "name": it.get("name"), "service": it.get("service"), "repo": it.get("repo")})
    for label, gap in (("FormCapture", "forms_without_screen"), ("DataEntity", "data_entities_without_code")):
        for r in sorted(indexes[label].rows, key=lambda r: (r["domain"], r.get("name") or "")):
            if r["entity_id"] not in linked[label] and r["domain"] in domains:
                gaps[gap].append({"name": r.get("name"), "domain": r["domain"], "entity_id": r["entity_id"]})


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


def latest_decisions(records: list) -> dict:
    """kf_link_decision evidence records (newest first) -> {(tia_rule, kf_entity_id): {decision, by, at, comment}}: the newest decision
    on each pair wins."""
    out: dict = {}
    for rec in records:
        d = rec.get("data") or rec
        k = (d.get("tia_rule"), d.get("kf_entity_id"))
        if None not in k and k not in out:
            out[k] = {"decision": d.get("decision"), "by": d.get("actor"), "at": d.get("at"), "comment": d.get("comment")}
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


def business_impact(context: Optional[dict], services: list, repo_of: dict, suite_ids: set, settings: dict,
                    touched: Optional[set] = None, issue_refs: Optional[list] = None, target_repo: Optional[str] = None) -> tuple[Optional[dict], list]:
    """The business side of a change. context: the data of the latest `kf_context` evidence record; services: the services the change
    affects (the changed one first); repo_of: service -> repository; suite_ids: the written test case ids of the analysed repositories;
    touched: the TIA node ids the change and its findings reach (screens, entities, tables and rules are shown only when touched);
    issue_refs: [{"key", "commit", "subject"}] issue keys found in the change's commit messages; target_repo: the changed repository.
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
                for tc in st.get("test_cases", []):
                    add_case(tc, st["domain"], {"service": services[0] if services else None, "repo": target_repo,
                                                "system": f"user story {ref['key']}", "label": "UserStory", "rel": "IMPLEMENTS_CHANGE",
                                                "through": f"commit {ref['commit'][:7]}", "requirements": tc.get("covers", []), "status": ID_MATCH})
            if ref["commit"][:12] not in cur["commits"]:
                cur["commits"].append(ref["commit"][:12])
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
    requirements = {r["entity_id"] for s in systems for r in s["requirements"]} | {r["entity_id"] for i in items for r in i.get("requirements", [])} \
        | {r["entity_id"] for s in stories for r in s.get("requirements", [])}
    section = {"source": "knowledge_fabric", "kf_customer_id": context.get("customer_id"), "kf_tenant_id": context.get("tenant_id"),
               "fingerprint": context.get("fingerprint"), "pulled_at": context.get("pulled_at"), "bridge_version": context.get("bridge_version"),
               "systems": systems, "items": items, "user_stories": stories, "test_cases": test_cases,
               "summary": {"systems": len(systems), "forms": len({i["entity_id"] for i in items if i["kf_label"] == "FormCapture"}),
                           "data_entities": len({i["entity_id"] for i in items if i["kf_label"] == "DataEntity"}),
                           "business_rules": len({i["entity_id"] for i in items if i["kf_label"] == "Rule"}),
                           "user_stories": len(stories), "requirements": len(requirements),
                           "test_cases": len(test_cases), "test_cases_not_in_repository_suite": len(new_cases),
                           "open_defects": len({d["entity_id"] for s in systems for d in s["defects"]} | {d["entity_id"] for i in items for d in i.get("defects", [])}),
                           "incidents": len({i["entity_id"] for s in systems for i in s["incidents"]} | {x["entity_id"] for i in items for x in i.get("incidents", [])}),
                           "headline": headline},
               "limits": list(LIMITS) + (["User stories are found by issue keys in the change's commit messages only; pull request titles and "
                                           "branch names are not read."] if settings.get("link_issue_keys", True) else [])}
    return section, unknowns


def redact(section: Optional[dict], readable: set) -> int:
    """Keep only what a reader may see: systems and test case chains of services in repositories they cannot read are dropped."""
    if not section:
        return 0
    keys = ("systems", "items", "user_stories", "test_cases")
    before = sum(len(section.get(k) or []) for k in keys)
    for k in ("systems", "items", "user_stories"):
        section[k] = [s for s in section.get(k) or [] if s.get("repo") in readable]
    for c in section["test_cases"]:
        c["via"] = [v for v in c["via"] if v.get("repo") in readable]
    section["test_cases"] = [c for c in section["test_cases"] if c["via"]]
    return before - sum(len(section.get(k) or []) for k in keys)

"""Review of the links between code and Knowledge Fabric (human in the loop; docs/KF-BRIDGE.md section 12).

Pure: no database, no Neo4j. tia.analysis_service keeps the items (table kf_review_items) and the decisions (evidence kf_link_decision);
tia.kfbridge applies the decisions on every bridge run; tia.kfinbox shows the same items in Knowledge Fabric's review screen.

Every bridge run turns what it is not sure of into review items:

  guessed_link        a link found by name (inferred)                       confirm, confirm_with_alias, reject
  ambiguous           a name matches several nodes in one domain            choose, reject (none of them)
  suggested_link      an unlinked item has close Knowledge Fabric names      choose, confirm_with_alias, reject (dismiss)
  rule_pair           a rule in code and a Knowledge Fabric Rule            confirm, reject
  product_suggestion  a service without a place: its Systems' place         confirm (accept), choose (another place), reject
  product_conflict    a declared place disagrees with where its Systems sit confirm (keep), choose (change the place)
  story_not_found     an issue key in commits with no story, or several     choose (one of several), reject (ignore)
  stale_decision      a decided Knowledge Fabric node disappeared           choose (move the decision), reject (drop it)

and every kind can be deferred (`defer`: open again on the next run) and, once decided, undone (`reset`). The actions are Knowledge Fabric's
review vocabulary (KF_ACTION), so a decision made in either screen means the same thing.

An item's id is a hash of (kind, TIA item, sorted candidates): each run finds the same item again instead of creating a new one, and a
different set of candidates is a new question.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Optional

from .kfbridge import NAME_MATCH, place_key, place_label, tia_id
from .kfsettings import place as normal_place

KINDS = ("guessed_link", "ambiguous", "suggested_link", "rule_pair", "product_suggestion", "product_conflict", "story_not_found", "stale_decision")
ACTIONS = {
    "guessed_link": ("confirm", "confirm_with_alias", "reject", "defer"),
    "ambiguous": ("choose", "reject", "defer"),
    "suggested_link": ("choose", "confirm_with_alias", "reject", "defer"),
    "rule_pair": ("confirm", "reject", "defer"),
    "product_suggestion": ("confirm", "choose", "reject", "defer"),
    "product_conflict": ("confirm", "choose", "defer"),
    "story_not_found": ("choose", "reject", "defer"),
    "stale_decision": ("choose", "reject", "defer"),
}
STATUSES = ("open", "awaiting_second", "deferred", "decided", "superseded")
POSITIVE = ("confirm", "choose", "confirm_with_alias")
# TIA action -> Knowledge Fabric's ReviewAction (kbfabric_ingest/ingest/hitl_api.py). APPROVE_AS_NEW has no TIA meaning: TIA never creates
# business nodes in Knowledge Fabric.
KF_ACTION = {"confirm": "APPROVE_REUSE", "choose": "APPROVE_REUSE", "confirm_with_alias": "APPROVE_REUSE_WITH_ALIAS", "reject": "REJECT",
             "defer": "LEAVE_UNRESOLVED"}
ALIAS_SETTING = {"System": "system_aliases", "FormCapture": "screen_aliases", "DataEntity": "data_aliases"}
REL_OF = {"System": "REALIZES", "FormCapture": "REALIZES", "DataEntity": "STORES", "Rule": "ENFORCES", "TestCase": "SAME_AS"}
WORD = {"System": "business system", "FormCapture": "form", "DataEntity": "data entity", "Rule": "business rule", "TestCase": "test case",
        "UserStory": "user story"}


class ReviewError(ValueError):
    """The decision does not fit the item: an action its kind does not allow, or a candidate it does not have."""


def item_id(kind: str, tid: str, candidates: list) -> str:
    return "kfi-" + hashlib.sha256(f"{kind}|{tid}|{','.join(sorted(str(c) for c in candidates))}".encode()).hexdigest()[:16]


def _cand(o: dict, **extra) -> dict:
    return {"id": o["entity_id"], "entity_id": o["entity_id"], "name": o.get("name"), "domain": o.get("domain"),
            "product_lines": list(o.get("product_lines") or []), **{k: v for k, v in extra.items() if v is not None}}


def _what(it: dict) -> str:
    """`service payments-api`, `screen /pay`, `entity PaymentRecord`."""
    kind = it.get("kind") or ("service" if str(it.get("tia_id", "")).startswith("svc:") else "item")
    return f"{kind} {it.get('name') or it.get('service') or it.get('tia_id')}"


def build_items(run: dict, products: Optional[dict] = None, story_gaps: Optional[list] = None, hot: Optional[set] = None,
                node_info: Optional[Callable[[str], dict]] = None) -> list:
    """The review items of one bridge run. run: kfbridge.plan's output ({"links", "gaps", "proposals", "product_suggestions",
    "link_suggestions"}); products: service -> places (for the review scope a Knowledge Fabric inbox needs: domain and product line);
    story_gaps: [{"key", "analyses", "options"}] issue keys from recent analyses that name no story, or several; hot: services a recent
    change touched (their items come first); node_info: TIA node id -> {"file", "line", "type", "name"} (the code side of the evidence).
    -> [item] sorted by priority, kind and id."""
    products, hot, out = products or {}, hot or set(), []

    def add(kind, tid, candidates, service, repo, title, question, evidence, label=None, rel=None, domain=None):
        cands = [c["id"] for c in candidates] or ["none"]
        places = products.get(service) or []
        scope = places[0] if places else None
        info = (node_info(tid) if node_info else None) or {}
        out.append({"item_id": item_id(kind, tid, cands), "kind": kind, "tia_id": tid, "service": service, "repo": repo, "label": label,
                    "rel": rel, "domain": domain or (candidates[0].get("domain") if candidates else None),
                    "product_line_id": scope["product_line_id"] if scope else None, "scope_domain": scope["domain"] if scope else None,
                    "priority": "high" if service in hot else "normal", "title": title, "question": question,
                    "candidates": candidates, "actions": list(ACTIONS[kind]),
                    "evidence": {"tia": {"id": tid, "service": service, "repo": repo, **{k: v for k, v in info.items() if v is not None}},
                                 **evidence}})

    for ln in run.get("links") or []:
        if ln["status"] != NAME_MATCH or ln.get("source") == "review":
            continue
        label = ln["to_label"]
        tid = tia_id(ln["from"])
        what = f"{ln.get('item_kind') or 'service'} {ln.get('tia_name') or ln.get('service') or ln.get('test_case') or tid}"
        add("guessed_link", tid, [_cand({"entity_id": ln["to"], "name": ln["to_name"], "domain": ln["domain"]})], ln.get("service"),
            _repo(run, ln), f"{what} -> {WORD.get(label, label)} '{ln['to_name']}'?",
            f"Is the {what} the {WORD.get(label, label)} '{ln['to_name']}' ({ln['domain']})? TIA found it by name ({ln.get('rule')}).",
            {"kf": {"label": label, "entity_id": ln["to"], "name": ln["to_name"], "domain": ln["domain"]},
             "how": {"rule": ln.get("rule"), "strategy": ln.get("strategy"), "source": ln.get("source"), "id_check": ln.get("id_check")}},
            label=label, rel=ln["rel"], domain=ln["domain"])
    for g in (run.get("gaps") or {}).get("ambiguous") or []:
        label = g.get("label", "System")
        tid = g.get("tia_id") or (f"svc:{g['service']}" if g.get("service") and not g.get("item") else g.get("item"))
        cands = [_cand(o) for o in g.get("options") or [{"entity_id": e} for e in g.get("candidates") or []]]
        add("ambiguous", tid, cands, g.get("service"), g.get("repo"), f"'{g['name']}' matches {len(cands)} {WORD.get(label, label)}s in {g['domain']}",
            f"Which {WORD.get(label, label)} in {g['domain']} is '{g['name']}' ({_what({**g, 'tia_id': tid})})? Choose one, or none.",
            {"kf": {"label": label, "domain": g["domain"], "name": g["name"]}}, label=label, rel=REL_OF.get(label), domain=g["domain"])
    for sg in run.get("link_suggestions") or []:
        label = sg["label"]
        cands = [_cand(c, score=c.get("score"), why="shared words: " + ", ".join(c.get("common") or [])) for c in sg["candidates"]]
        add("suggested_link", sg["tia_id"], cands, sg.get("service"), sg.get("repo"),
            f"{sg['kind']} {sg.get('name')} has no {WORD.get(label, label)}: {len(cands)} close name(s)",
            f"Is the {sg['kind']} {sg.get('name')} one of these {WORD.get(label, label)}s? TIA suggests them from shared words; nothing is "
            "linked until you choose.", {"kf": {"label": label}}, label=label, rel=sg["rel"])
    for p in run.get("proposals") or []:
        if p.get("status") != "pending":
            continue
        add("rule_pair", p["tia_rule"], [_cand({"entity_id": p["kf_entity_id"], "name": p["kf_rule"], "domain": p["domain"]}, score=p["score"],
                                                why=("shared fields: " + ", ".join(p["shared_fields"])) if p.get("shared_fields") else None)],
            p.get("service"), p.get("repo"), f"rule `{p['expr']}` enforces '{p['kf_rule']}'?",
            f"Does the rule in code `{p['expr']}` ({p.get('service')}) enforce the business rule '{p['kf_rule']}'?",
            {"kf": {"label": "Rule", "description": p.get("kf_description")}, "proposal": p["id"],
             "how": {"score": p["score"], "shared_fields": p.get("shared_fields"), "common_words": p.get("common_words")}},
            label="Rule", rel="ENFORCES", domain=p["domain"])
    for sg in run.get("product_suggestions") or []:
        cand = {"id": sg["place_key"], "place": sg["place"], "name": sg["label"], "domain": sg["place"]["domain"],
                "via": [v["system"] for v in sg["via"]]}
        add("product_suggestion", sg["tia_id"], [cand], sg["service"], sg.get("repo"), f"{sg['service']} belongs to {sg['label']}?",
            f"Does the service {sg['service']} belong to {sg['label']}? Its business system(s) {', '.join(cand['via'])} sit there in "
            "Knowledge Fabric's tree. Accept, choose another place, or reject.", {"via": sg["via"]}, domain=sg["place"]["domain"])
    for g in (run.get("gaps") or {}).get("product_conflicts") or []:
        cands = [_cand({**f, "name": f.get("system")}, functions=f.get("functions") or None) for f in g["found"]]
        add("product_conflict", g["tia_id"], cands, g["service"], g.get("repo"),
            f"{g['service']} is mapped to {', '.join(g['places'])}, its systems sit elsewhere",
            f"The service {g['service']} is mapped to {', '.join(g['places'])}, but business system(s) of the same name sit in "
            + "; ".join(f"{f['system']} ({f['domain']}: {', '.join(f['product_lines']) or 'no product line'})" for f in g["found"])
            + ". Keep the mapping, or change it.", {"places": g["places"]})
    for sg in story_gaps or []:
        tid = f"issue:{sg['key']}"
        cands = [_cand(o) for o in sg.get("options") or []]
        several = len(cands) > 1
        add("story_not_found", tid, cands, sg.get("service"), sg.get("repo"),
            f"issue key {sg['key']}: " + (f"{len(cands)} user stories" if several else "no user story in Knowledge Fabric"),
            (f"Which user story is {sg['key']}? Knowledge Fabric has {len(cands)} with that key." if several else
             f"Recent changes name {sg['key']} in their commit messages, but Knowledge Fabric has no user story with that key. Ingest the "
             "story into Knowledge Fabric (the key as its name or alias), or ignore the key."),
            {"analyses": sg.get("analyses") or []}, label="UserStory")
        if not several:
            out[-1]["actions"] = ["reject", "defer"]
    for g in (run.get("gaps") or {}).get("stale_decisions") or []:
        cands = [_cand(o) for o in g.get("options") or []]
        add("stale_decision", g["tia_id"], cands, None, None,
            f"decided {WORD.get(g['label'], g['label'])} '{g.get('kf_name')}' is gone from Knowledge Fabric",
            f"A person linked {g['tia_id']} to the {WORD.get(g['label'], g['label'])} '{g.get('kf_name')}' ({g.get('decided_by')}, "
            f"{g.get('decided_at')}); that node is no longer in Knowledge Fabric. Move the decision to "
            + ("one of these nodes" if cands else "another node (none has that name now)") + ", or drop it.",
            {"old": {"entity_id": g["entity_id"], "kind": g.get("kind"), "rel": g.get("rel")}}, label=g["label"], rel=g.get("rel"))
        if not cands:
            out[-1]["actions"] = ["reject", "defer"]
    seen, uniq = set(), []
    for it in out:
        if it["item_id"] not in seen:
            seen.add(it["item_id"])
            uniq.append(it)
    return sorted(uniq, key=lambda x: (x["priority"] != "high", KINDS.index(x["kind"]), x["item_id"]))


def _repo(run: dict, ln: dict) -> Optional[str]:
    return ln.get("repo") or (run.get("repo_of") or {}).get(ln.get("service"))


def decide(item: dict, action: str, kf_entity_id: Optional[str] = None, place: Optional[dict] = None) -> dict:
    """A person's action on an item -> {"records": [kf_link_decision data], "config": change to the tenant setting or None}.
    The records name the pairs the decision is about, so tia.kfbridge applies it on the next run and `reset` can undo exactly them.
    Raises ReviewError when the action does not fit the item."""
    kind = item["kind"]
    if action == "reset":
        targets = (item.get("decision") or {}).get("targets") or []
        if not targets:
            raise ReviewError("only a decided item can be reset")
        return {"records": [{**t, "decision": "reset"} for t in targets], "config": None}
    if action not in ACTIONS[kind] or action not in (item.get("actions") or ACTIONS[kind]):
        raise ReviewError(f"'{action}' is not an action for a {kind} item (use one of: {', '.join(item.get('actions') or ACTIONS[kind])})")
    ids = [c["id"] for c in item["candidates"]]
    base = {"tia_id": item["tia_id"], "item_id": item["item_id"], "item_kind": kind, "label": item.get("label"), "rel": item.get("rel")}

    def pair(eid, decision, link_kind="link"):
        c = next((c for c in item["candidates"] if c["id"] == eid), {})
        return {**base, "kind": link_kind, "kf_entity_id": eid, "kf_name": c.get("name"), "decision": decision}
    if action == "defer":
        return {"records": [{**base, "kind": kind, "kf_entity_id": item["item_id"], "decision": "defer"}], "config": None}
    if kind in ("guessed_link", "rule_pair"):
        lk = "rule" if kind == "rule_pair" else "link"
        rec = pair(ids[0], "reject" if action == "reject" else "confirm", lk)
        return {"records": [rec], "config": _alias(item, ids[0]) if action == "confirm_with_alias" else None}
    if kind in ("ambiguous", "suggested_link", "story_not_found"):
        lk = "story" if kind == "story_not_found" else "link"
        if action == "reject":
            return {"records": [pair(e, "reject", lk) for e in ids if e != "none"] or [pair("none", "reject", lk)], "config": None}
        if kf_entity_id not in ids or kf_entity_id == "none":
            raise ReviewError(f"choose one of the item's candidates: {', '.join(i for i in ids if i != 'none') or '(none)'}")
        return {"records": [pair(kf_entity_id, "choose", lk)], "config": _alias(item, kf_entity_id) if action == "confirm_with_alias" else None}
    if kind == "product_suggestion":
        suggested = item["candidates"][0]
        if action == "reject":
            return {"records": [{**base, "kind": "product", "place_key": suggested["id"], "place": suggested["place"], "decision": "reject"}],
                    "config": None}
        p = suggested["place"] if action == "confirm" else normal_place(place)
        if p is None:
            raise ReviewError('choose a place: {"domain", "product_line"[, "function"[, "sub_function"]]}')
        return {"records": [{**base, "kind": "product", "place_key": place_key(p), "place": p, "label_text": place_label(p),
                             "decision": "accept"}], "config": None}
    if kind == "product_conflict":
        if action == "confirm":
            return {"records": [{**base, "kind": "product_conflict", "kf_entity_id": item["item_id"], "decision": "confirm"}], "config": None}
        p = normal_place(place)
        if p is None:
            raise ReviewError('choose the new place: {"domain", "product_line"[, "function"[, "sub_function"]]}')
        return {"records": [{**base, "kind": "product_conflict", "kf_entity_id": item["item_id"], "decision": "choose", "place": p}],
                "config": {"service_products": {item["service"]: [p]}}}
    if kind == "stale_decision":
        old = item["evidence"]["old"]
        lk = old.get("kind") or "link"
        drop = {**base, "kind": lk, "kf_entity_id": old["entity_id"], "decision": "reset"}
        if action == "reject":
            return {"records": [drop], "config": None}
        if kf_entity_id not in ids:
            raise ReviewError(f"choose one of the item's candidates: {', '.join(ids)}")
        return {"records": [drop, pair(kf_entity_id, "confirm" if lk == "rule" else "choose", lk)], "config": None}
    raise ReviewError(f"unknown item kind {kind}")


def _alias(item: dict, eid: str) -> Optional[dict]:
    """confirm_with_alias: keep Knowledge Fabric's name under the TIA name in the tenant setting, so the link no longer depends on review."""
    setting = ALIAS_SETTING.get(item.get("label"))
    c = next((c for c in item["candidates"] if c["id"] == eid), None)
    if not setting or not c or not c.get("name"):
        raise ReviewError("confirm_with_alias needs a System, form or data entity candidate with a name")
    tia = item.get("evidence", {}).get("tia", {})
    if setting == "system_aliases":
        under = item.get("service") or item["tia_id"].removeprefix("svc:")
    elif setting == "screen_aliases":
        under = tia.get("route") or tia.get("component") or tia.get("name") or item["tia_id"]
    else:
        under = tia.get("name") or item["tia_id"]
    return {setting: {under: [c["name"]]}}


def targets_of(records: list) -> list:
    """What a decision touched, kept on the item so `reset` undoes exactly that."""
    keep = ("tia_id", "item_id", "item_kind", "kind", "kf_entity_id", "kf_name", "label", "rel", "place_key", "place")
    return [{k: r[k] for k in keep if k in r} for r in records if r.get("decision") not in ("reset", "defer")]


def kf_to_tia(item: dict, kf_action: str, selected_entity_id: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Knowledge Fabric's review action on a SOFTWARE_LINK item -> (TIA action, chosen candidate). Raises ReviewError for APPROVE_AS_NEW
    (TIA never creates business nodes) or an unknown action."""
    if kf_action == "APPROVE_AS_NEW":
        raise ReviewError("APPROVE_AS_NEW is not offered for software links: TIA never creates business nodes in Knowledge Fabric")
    if kf_action == "REJECT":
        return "reject", None
    if kf_action == "LEAVE_UNRESOLVED":
        return "defer", None
    if kf_action not in ("APPROVE_REUSE", "APPROVE_REUSE_WITH_ALIAS"):
        raise ReviewError(f"unknown Knowledge Fabric review action {kf_action}")
    allowed = ACTIONS[item["kind"]]
    if kf_action == "APPROVE_REUSE_WITH_ALIAS":
        return "confirm_with_alias", selected_entity_id or (item["candidates"][0]["id"] if item["candidates"] else None)
    if "choose" in allowed and selected_entity_id and (item["kind"] != "product_suggestion"):
        return "choose", selected_entity_id
    return "confirm", None


def inbox_payload(item: dict, tenant: str, customer_id: str, kf_tenant_id: Optional[str], detail: str = "minimal") -> dict:
    """The item as a SOFTWARE_LINK review for Knowledge Fabric's review screen (docs/KF-BRIDGE.md section 12, the contract asked of
    Knowledge Fabric). Scope: one stable batch per product line, `tia-links:<tenant>:<product_line_id>`. With detail `minimal` the code side
    carries names only (service, route, entity), never file paths."""
    tia = dict(item["evidence"].get("tia") or {})
    if detail != "full":
        tia = {k: v for k, v in tia.items() if k in ("id", "service", "repo", "type", "name")}
    return {"plan_id": item["item_id"], "queue_type": "SOFTWARE_LINK", "ingestion_batch_id": f"tia-links:{tenant}:{item['product_line_id']}",
            "domain_id": item["scope_domain"], "product_line_id": item["product_line_id"], "customer_id": customer_id, "tenant_id": kf_tenant_id,
            "priority": "HIGH" if item["priority"] == "high" else "NORMAL", "reason_code": item["kind"].upper(),
            "admission_behavior": "NODE_CAN_EXIST", "short_summary": item["title"][:300], "decision_question": item["question"][:2000],
            "allowed_actions": sorted({KF_ACTION[a] for a in item["actions"] if a in KF_ACTION}),
            "candidates": [{"entity_id": c["id"], "name": c.get("name"), "domain": c.get("domain")} for c in item["candidates"]],
            "source_context": {"tia": tia}, "execution_plan": {"action": "QUEUE_FOR_REVIEW", "entity_operations": [],
                                                                 "relationship_operations": []}}

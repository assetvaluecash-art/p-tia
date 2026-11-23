"""Knowledge Fabric V2.5 adapter: a finished Change Impact Report in the shape KF agents already consume (QueryResponse).

Contract source: TIA_KF_V2_INTEGRATION.docx section 5 (the field lists of QueryRequest / QueryResponse / ChunkItem / PathItem /
RetrievalMetadata in kbfabric_ingest/retrieval/retriever_api.py). The KF source file itself was not available; the models below follow the
document field by field and must be re-checked against retriever_api.py before production use.

Rules this adapter keeps (document F1-F8, N3-N5):
* It serves FINISHED reports and never starts an analysis: an agent's question is not parsed to guess a repository or a change (no invented
  entities). The caller names the analysis (`analysis_id`) or the repository plus a pull request / revision, and gets the latest such report.
* Evidence status is never upgraded: it is carried in every chunk (extracted_fields) and path (source_evidence), and inferred or stale findings
  score lower than confirmed ones.
* Gaps stay visible: every unknown becomes a chunk (category "unknown"), and a partial or gapped report is stated in metadata.errors.
* Redaction is the caller's: the report is the one this principal may read, already redacted.
* The tester's view rides along: every entry point to test, rule gap and consistency check of the report's test scope is a chunk
  (categories test_scope / rule_gap / consistency_check) and every entry point's impact path a path, so a KF agent asked "what must I test
  for this change" gets the entry points, not the functions.
* Directions are TIA's own: TIA's edges point consumer -> provider; an impact path runs from the change outward, so a hop shows
  (changed)<-[:CONSUMES_API]-(consumer), never a relationship name TIA does not have.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SEVERITY_SCORE = {"critical": 1.0, "high": 0.85, "medium": 0.6, "low": 0.35}
WEAK_EVIDENCE = {"inferred": 0.5, "stale": 0.5, "unknown": 0.3}
INTENT = "software_tia"
FORWARD = {"HANDLES", "PUBLISHES"}                       # the one traversed relationship whose stored direction is the direction of impact


# ---- the KF V2 wire format (document section 5); extra keys are refused so the contract cannot drift silently -----------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRequest(_Strict):
    intent: str = INTENT
    query: str = ""
    domain: str = ""
    top_k_semantic: int = Field(10, ge=1, le=100)
    top_k_graph: int = Field(10, ge=1, le=100)
    # additive selectors for the TIA door (document 5.1 says software_tia is additive); the question text is never parsed
    analysis_id: Optional[str] = None
    repository: Optional[str] = None
    pull_request: Optional[int] = None
    revision: Optional[str] = None
    include_tester_pack: bool = True       # coverage gate and gaps, API test matrices and UI locator packs of the touched entry points


class EntityRef(_Strict):
    name: str
    type: str


class ChunkItem(_Strict):
    text: str
    source_file: str
    category: str
    score: float
    weaviate_uuid: str
    block_id: str
    page_number: Optional[int]
    block_index: Optional[int]
    block_type: str
    kg_node_type: str
    domain: str
    extracted_fields: Optional[str]
    extracted_knowledge: Optional[str]


class PathItem(_Strict):
    path_text: str
    score: float
    from_entity: EntityRef
    relationship_type: str
    to_entity: EntityRef
    source_block_id: str
    source_file: str
    source_page: Optional[int]
    source_evidence: str


class DetectedDomain(_Strict):
    domain: str
    confidence: float


class RetrievalMetadata(_Strict):
    intent: str
    pattern: str
    max_depth: int
    latency_ms: float
    chunks_considered: int
    paths_considered: int
    errors: list[str]


class QueryResponse(_Strict):
    query: str
    domain: str
    detected_domains: list[DetectedDomain]
    chunks: list[ChunkItem]
    reranked_chunks: list[ChunkItem]
    paths: list[PathItem]
    reranked_paths: list[PathItem]
    metadata: RetrievalMetadata


# ---- projection ------------------------------------------------------------------------------------------------------------------
def score_of(f: dict) -> float:
    s = SEVERITY_SCORE.get(f.get("severity"), 0.2) * WEAK_EVIDENCE.get(f.get("evidence_status"), 1.0)
    return round(s, 4)


def _node_label(p: dict) -> str:
    return str(p.get("node") or "[redacted]")


def path_text(f: dict) -> str:
    """The impact path as a symbolic chain, each hop in TIA's stored direction."""
    nodes, edges = f.get("path") or [], f.get("path_edges") or []
    if not nodes:
        return ""
    out = f'({nodes[0].get("type")}:"{_node_label(nodes[0])}")'
    for i, e in enumerate(edges):
        nxt = nodes[i + 1] if i + 1 < len(nodes) else {"type": "unknown", "node": "?"}
        rel = e.get("rel", "RELATED")
        hop = f'-[:{rel}]->' if rel in FORWARD else f'<-[:{rel}]-'
        out += f'{hop}({nxt.get("type")}:"{_node_label(nxt)}")'
    return out


def _fields(rep: dict, f: dict, extra: Optional[dict] = None) -> str:
    d = {"analysis_id": rep["analysis_id"], "finding": f.get("id"), "severity": f.get("severity"), "material": bool(f.get("material")),
         "evidence_status": f.get("evidence_status"), "impact_type": f.get("impact_type"), "component": f.get("component"),
         "repository": f.get("repository"), "severity_rule": f.get("severity_rule"), "resolution_strategy": f.get("resolution_strategy"),
         "inference_rule": f.get("inference_rule"), "report_status": rep.get("status"), "configuration_version": rep.get("configuration_version"),
         **(extra or {})}
    return json.dumps({k: v for k, v in d.items() if v is not None}, sort_keys=True)


def chunk_for(rep: dict, i: int, f: dict, domain: str) -> ChunkItem:
    ev = f.get("evidence") or {}
    verify = " ".join(f.get("required_verification") or [])
    status = f.get("evidence_status", "unknown")
    caveat = "" if status in ("source_confirmed", "contract_confirmed", "corroborated") else f" Evidence is {status.upper()}: treat this as a lead, not a fact."
    text = (f"{(f.get('severity') or '').upper()} {f.get('impact_type', '')} impact on {f.get('component')} ({status})."
            f" {f.get('reason', '')}{caveat}" + (f" Verify: {verify}" if verify else "")).strip()
    return ChunkItem(text=text, source_file=str(ev.get("file") or ""), category=str(f.get("impact_type") or "finding"), score=score_of(f), weaviate_uuid="",
                     block_id=f"{rep['analysis_id']}#{i}", page_number=None, block_index=i, block_type="tia_finding", kg_node_type=str(f.get("node_type") or ""),
                     domain=domain, extracted_fields=_fields(rep, f), extracted_knowledge=None)


def path_for(rep: dict, i: int, f: dict) -> Optional[PathItem]:
    nodes, edges = f.get("path") or [], f.get("path_edges") or []
    if len(nodes) < 2 or not edges:
        return None
    ev = f.get("evidence") or {}
    first, last = nodes[0], nodes[-1]
    strategy = f" via {f['resolution_strategy']}" if f.get("resolution_strategy") else ""
    evidence = (f"{f.get('evidence_status')}{strategy}: {f.get('reason', '')}"
                + (f" [{ev.get('repo')}:{ev.get('file')}:{ev.get('line')}]" if ev.get("file") else ""))
    return PathItem(path_text=path_text(f), score=score_of(f), from_entity=EntityRef(name=_node_label(first), type=str(first.get("type") or "unknown")),
                    relationship_type=str(edges[0].get("rel") or "RELATED"), to_entity=EntityRef(name=_node_label(last), type=str(last.get("type") or "unknown")),
                    source_block_id=f"{rep['analysis_id']}#{i}", source_file=str(ev.get("file") or ""), source_page=None, source_evidence=evidence)


def unknown_chunk(rep: dict, j: int, u: dict, domain: str) -> ChunkItem:
    items = u.get("items") or []
    text = f"UNKNOWN ({u.get('area')}): {u.get('reason', '')}" + (f" {len(items)} item(s)." if items else "")
    return ChunkItem(text=text, source_file="", category="unknown", score=0.0, weaviate_uuid="", block_id=f"{rep['analysis_id']}#unknown-{j}", page_number=None,
                     block_index=None, block_type="tia_unknown", kg_node_type="unknown", domain=domain,
                     extracted_fields=json.dumps({"analysis_id": rep["analysis_id"], "area": u.get("area"), "affects_confidence": bool(u.get("affects_confidence")),
                                                  "items": len(items)}, sort_keys=True), extracted_knowledge=None)


# ---- the tester's view (report["test_scope"]): entry points to test, rule gaps, consistency checks -------------------------------------
EFFECT_SCORE = {"breaking": 0.9, "behaviour_change": 0.6, "unknown": 0.4, "compatible_change": 0.3, "no_functional_change": 0.1}
EFFECT_TEXT = {"breaking": "BREAKING change", "behaviour_change": "behaviour change", "compatible_change": "compatible change",
               "no_functional_change": "no functional change", "unknown": "unclassified (file-level) change"}


def _caveat(status: str) -> str:
    return "" if status in ("source_confirmed", "contract_confirmed", "corroborated") else f" Evidence is {str(status).upper()}: treat this as a lead, not a fact."


def scope_chunks(rep: dict, domain: str) -> list:
    """One chunk per entry point to test, rule gap and consistency check, highest priority first."""
    ts = rep.get("test_scope") or {}
    aid, out = rep["analysis_id"], []
    for k, e in enumerate(ts.get("entry_points", []) + ts.get("code_without_entry_point", [])):
        tests = ", ".join(t["name"] for t in e["tests"][:5]) or "none found in the analysed sources"
        label = f"{e['label']}: " if e.get("label") else ""
        text = f"TEST {label}{e['entry_point']} ({e['service']}) - {EFFECT_TEXT[e['change_effect']]}. {e['why']} Tests found: {tests}.{_caveat(e['evidence_status'])}"
        fields = {"analysis_id": aid, "entry_point": e["entry_point"], "service": e["service"], "repository": e["repo"], "change_effect": e["change_effect"],
                  "evidence_status": e["evidence_status"], "cross_repository": e["cross_repository"], "has_entry_point": e["type"] == "endpoint",
                  "tests": len(e["tests"]), **({"label": e["label"]} if e.get("label") else {})}
        out.append(ChunkItem(text=text, source_file=str(e.get("file") or ""), category="test_scope",
                             score=round(EFFECT_SCORE[e["change_effect"]] * WEAK_EVIDENCE.get(e["evidence_status"], 1.0), 4), weaviate_uuid="",
                             block_id=f"{aid}#test-{k}", page_number=None, block_index=None, block_type="tia_test_target", kg_node_type=e["type"],
                             domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    for k, gp in enumerate(ts.get("rule_gaps", [])):
        head = "RULE MOVED INTO SHARED SERVICE" if gp.get("resolved_by_this_change") else "POSSIBLE RULE GAP"
        fields = {"analysis_id": aid, "shared_entry_point": gp["shared_entry_point"], "service": gp["service"], "rule_fields": gp["rule"]["fields"],
                  "rule": gp["rule"]["expr"], "applied_by": [a["entry_point"] for a in gp["applied_by"]], "missing_in": [m["entry_point"] for m in gp["missing_in"]],
                  "resolved_by_this_change": bool(gp.get("resolved_by_this_change")), "evidence_status": gp["evidence_status"]}
        out.append(ChunkItem(text=f"{head}: {gp['reason']}", source_file=str(gp["applied_by"][0].get("function_file") or ""), category="rule_gap", score=0.8,
                             weaviate_uuid="", block_id=f"{aid}#rule-{k}", page_number=None, block_index=None, block_type="tia_rule_gap",
                             kg_node_type="endpoint", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    for k, c in enumerate(ts.get("consistency_checks", [])):
        fields = {"analysis_id": aid, "shared_entry_point": c["shared_entry_point"], "service": c["service"],
                  "entry_points": [x["entry_point"] for x in c["entry_points"]]}
        out.append(ChunkItem(text=f"CONSISTENCY CHECK: {c['check']}", source_file="", category="consistency_check", score=0.5, weaviate_uuid="",
                             block_id=f"{aid}#check-{k}", page_number=None, block_index=None, block_type="tia_consistency_check",
                             kg_node_type="endpoint", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    for k, c in enumerate(ts.get("config_checks", [])):
        fields = {"analysis_id": aid, "key": c["key"], "change": c["change"], "file": c["file"], "repository": c["repo"],
                  "entry_points": [e["entry_point"] for e in c["entry_points"]]}
        out.append(ChunkItem(text=f"CONFIGURATION CHECK: {c['check']}", source_file=str(c["file"]), category="config_check", score=0.7,
                             weaviate_uuid="", block_id=f"{aid}#config-{k}", page_number=None, block_index=None, block_type="tia_config_check",
                             kg_node_type="config_key", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True), extracted_knowledge=None))
    reg = ts.get("regression") or {}
    if (reg.get("summary") or {}).get("test_cases"):
        s = reg["summary"]
        out.append(ChunkItem(text=f"REGRESSION SELECTION: {s['headline']} {reg['basis']}", source_file="", category="regression_summary",
                             score=0.9, weaviate_uuid="", block_id=f"{aid}#regression", page_number=None, block_index=None,
                             block_type="tia_regression_summary", kg_node_type="analysis", domain=domain,
                             extracted_fields=json.dumps({"analysis_id": aid, **{k2: s[k2] for k2 in ("test_cases", "selected", "not_selected", "unmapped")}},
                                                         sort_keys=True),
                             extracted_knowledge=json.dumps({"selected": [c["id"] for c in reg["selected"]], "unmapped": [c["id"] for c in reg["unmapped"]],
                                                             "not_selected": [c["id"] for c in reg["not_selected"]]}, sort_keys=True)))
        for k, c in enumerate(reg["selected"]):
            fields = {"analysis_id": aid, "test_case": c["id"], "title": c["title"], "priority": c["priority"], "repository": c["repo"],
                      "change_effect": c["change_effect"], "entry_points": [r["entry_point"] for r in c["reasons"]]}
            out.append(ChunkItem(text=f"REGRESSION TEST {c['id']} {c['title']}: {c['reason']}.", source_file=str(c.get("file") or ""),
                                 category="regression_test", score=round(EFFECT_SCORE[c["change_effect"]], 4), weaviate_uuid="",
                                 block_id=f"{aid}#regression-{k}", page_number=None, block_index=None, block_type="tia_regression_test",
                                 kg_node_type="test_case", domain=domain, extracted_fields=json.dumps(fields, sort_keys=True),
                                 extracted_knowledge=json.dumps({"reasons": c["reasons"]}, sort_keys=True, default=str)))
        for k, c in enumerate(reg["unmapped"]):
            out.append(ChunkItem(text=f"TEST CASE NOT MAPPED {c['id']} {c['title']}: {c['reason']}.", source_file=str(c.get("file") or ""),
                                 category="regression_unmapped", score=0.4, weaviate_uuid="", block_id=f"{aid}#unmapped-{k}", page_number=None,
                                 block_index=None, block_type="tia_regression_unmapped", kg_node_type="test_case", domain=domain,
                                 extracted_fields=json.dumps({"analysis_id": aid, "test_case": c["id"], "repository": c["repo"]}, sort_keys=True),
                                 extracted_knowledge=None))
    return out


def scope_paths(rep: dict) -> list:
    """The impact path to each entry point to test, from the change to the entry point."""
    ts = rep.get("test_scope") or {}
    out = []
    for k, e in enumerate(ts.get("entry_points", []) + ts.get("code_without_entry_point", [])):
        nodes, edges = e.get("path") or [], e.get("path_edges") or []
        if len(nodes) < 2 or not edges:
            continue
        out.append(PathItem(path_text=path_text(e), score=round(EFFECT_SCORE[e["change_effect"]] * WEAK_EVIDENCE.get(e["evidence_status"], 1.0), 4),
                            from_entity=EntityRef(name=_node_label(nodes[0]), type=str(nodes[0].get("type") or "unknown")),
                            relationship_type=str(edges[0].get("rel") or "RELATED"),
                            to_entity=EntityRef(name=_node_label(nodes[-1]), type=str(nodes[-1].get("type") or "unknown")),
                            source_block_id=f"{rep['analysis_id']}#test-{k}", source_file=str(e.get("file") or ""), source_page=None,
                            source_evidence=f"{e['evidence_status']}: {e['why']}"))
    return out


# ---- the tester pack (tia.testerpack): coverage gate and gaps, API test matrices, UI locator packs -------------------------------------
GATE_SCORE = {"gaps": 0.9, "not_evaluated": 0.6, "covered": 0.3}
PACK_RANK = {"coverage_gate": 0, "coverage_gap": 1, "api_matrix": 2, "ui_locators": 3}
MAX_CASES, MAX_ELEMENTS = 60, 80          # per chunk: the full lists stay in GET /api/v1/impact-analyses/{id}/tester-pack


def _chunk(aid: str, bid: str, category: str, block_type: str, node_type: str, text: str, score: float, source: str, domain: str,
           fields: dict, knowledge: Optional[dict] = None) -> ChunkItem:
    return ChunkItem(text=text, source_file=source or "", category=category, score=round(score, 4), weaviate_uuid="", block_id=f"{aid}#{bid}",
                     page_number=None, block_index=None, block_type=block_type, kg_node_type=node_type, domain=domain,
                     extracted_fields=json.dumps({"analysis_id": aid, **fields}, sort_keys=True, default=str),
                     extracted_knowledge=json.dumps(knowledge, sort_keys=True, default=str) if knowledge is not None else None)


def pack_chunks(pack: Optional[dict], domain: str) -> list:
    """The pack as chunks an agent can narrate or turn into tests: one gate verdict, one chunk per entry point that is not fully
    covered, one API matrix per touched endpoint, one locator pack per page that calls a touched endpoint. Structured content is in
    extracted_knowledge (JSON). Coverage is static: a chunk never says a test passes."""
    if not pack:
        return []
    aid, out = pack["analysis_id"], []
    gate = pack["gate"]
    out.append(_chunk(aid, "gate", "coverage_gate", "tia_coverage_gate", "analysis",
                      f"COVERAGE GATE: {gate['outcome'].upper().replace('_', ' ')}. {gate['headline']} 'Covered' means a test addresses the "
                      "technical aspect, not that it passes; 'not evaluated' is not 'safe'.",
                      GATE_SCORE[gate["outcome"]], "", domain, {"outcome": gate["outcome"], **gate["counts"]}))
    for k, r in enumerate(gate["entry_points"]):
        if r["outcome"] == "covered":
            continue
        name = f"{r['label']}: {r['entry_point']}" if r.get("label") else r["entry_point"]
        miss = "; ".join(r["missing_aspects"][:8]) + (f"; and {len(r['missing_aspects']) - 8} more" if len(r["missing_aspects"]) > 8 else "")
        text = (f"COVERAGE GAP {name} ({r['service']}): {r['covered']} of {r['total']} technical aspects covered. Not covered: {miss}."
                if r["outcome"] == "gaps" else f"COVERAGE NOT EVALUATED {name} ({r['service']}): {r['reason']}.")
        if r["failing_tests"]:
            text += f" Linked tests that FAILED in the last CI run: {', '.join(r['failing_tests'][:5])}."
        out.append(_chunk(aid, f"gap-{k}", "coverage_gap", "tia_coverage_gap", "page" if r["kind"] == "page" else "endpoint", text,
                          GATE_SCORE[r["outcome"]], "", domain,
                          {k2: r[k2] for k2 in ("entry_point", "kind", "service", "repo", "outcome", "covered", "partial", "missing", "total", "tests_found")},
                          {"missing_aspects": r["missing_aspects"], "failing_tests": r["failing_tests"]}))
    for k, it in enumerate(pack["api"]["endpoints"]):
        ins = "; ".join(f"{i['name']} ({'required' if i['required'] else 'optional'}{', ' + i['rules'] if i['rules'] else ''})" for i in it["inputs"][:12])
        errs = ", ".join(str(s["status"]) for s in it["error_statuses"]) or "none declared"
        text = (f"API TEST MATRIX {it.get('label') + ': ' if it.get('label') else ''}{it['entry_point']} ({it['service']}): {len(it['inputs'])} input(s), "
                f"{it['case_count']} generated case(s), error statuses {errs}. Inputs: {ins or 'none'}.")
        out.append(_chunk(aid, f"api-{k}", "api_matrix", "tia_api_matrix", "endpoint", text, 0.5, str(it.get("file") or ""), domain,
                          {"entry_point": it["entry_point"], "service": it["service"], "repo": it["repo"], "inputs": len(it["inputs"]),
                           "cases": it["case_count"], "cases_in_chunk": min(it["case_count"], MAX_CASES)},
                          {"method": it["method"], "path": it["path"],
                           "inputs": [{k2: i.get(k2) for k2 in ("name", "in", "type", "required", "multi", "rules", "valid", "invalid")} for i in it["inputs"]],
                           "error_statuses": it["error_statuses"], "cases": it["cases"][:MAX_CASES]}))
    for k, p in enumerate(pack["ui"]["pages"]):
        s = p["summary"]
        calls = "; ".join(f"{c['method']} {c['path']}" for c in p["api_calls"][:5]) or "none found"
        text = (f"UI LOCATORS page {p['page']} ({p['framework']}, {p['repo']}): {s['elements']} element(s), {s['mandatory']} mandatory, "
                f"{s['without_unique_locator']} without a unique locator (position-based, fragile). Calls: {calls}.")
        els = [{"name": e.get("label") or e.get("text") or e.get("name") or e["tag"], "tag": e["tag"], "type": e.get("type"), "mandatory": bool(e.get("mandatory")),
                "locator": e["locator"], **({"locator_note": e["locator_note"]} if e.get("locator_note") else {}), "rules": e.get("rules"),
                **({"options": [o.get("value") if o.get("value") is not None else o.get("text") for o in e.get("options", []) if o.get("value") != ""],
                    "options_source": e.get("options_source")} if e["tag"] == "select" else {})} for e in p["elements"][:MAX_ELEMENTS]]
        out.append(_chunk(aid, f"ui-{k}", "ui_locators", "tia_ui_locators", "page", text, 0.5, str((p.get("files") or [""])[0]), domain,
                          {"page": p["page"], "framework": p["framework"], "repo": p["repo"], "elements": s["elements"],
                           "elements_in_chunk": len(els), "without_unique_locator": s["without_unique_locator"]},
                          {"elements": els, "api_calls": [{"method": c["method"], "path": c["path"],
                                                           "endpoints": [x["entry_point"] for x in c["endpoints"]]} for c in p["api_calls"]]}))
    return out


def empty(req: QueryRequest, errors: list[str], t0: float) -> QueryResponse:
    return QueryResponse(query=req.query, domain=req.domain, detected_domains=[DetectedDomain(domain=req.domain, confidence=1.0)] if req.domain else [],
                         chunks=[], reranked_chunks=[], paths=[], reranked_paths=[],
                         metadata=RetrievalMetadata(intent=req.intent, pattern="", max_depth=0, latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                                                    chunks_considered=0, paths_considered=0, errors=errors))


BUSINESS_RANK = {"business_summary": 0, "business_user_story": 1, "business_test_case": 2, "business_system": 3, "business_item": 4}
# the request's intent (Knowledge Fabric's traversal pattern names) puts the categories that answer it first; the rest keep their order
INTENT_FIRST = {"regression_scope": ("regression_summary", "regression_test", "business_summary", "business_test_case"),
                "test_coverage": ("coverage_gate", "coverage_gap", "regression_summary"),
                "change_impact": ("contract", "transitive", "direct", "data", "event", "business_summary", "business_user_story", "business_system",
                                  "business_item")}


def business_chunks(rep: dict, domain: str) -> tuple[list, list]:
    """Knowledge Fabric's business impact of the change (docs/KF-BRIDGE.md) as chunks and paths: one summary, one chunk
    per business test case (with the chain from the changed code), one per affected system. The evidence status is the TIA side's link from
    code to the system; Knowledge Fabric's own relationships are its document extraction, not verified by TIA."""
    bi = rep.get("business_impact")
    if not bi:
        return [], []
    aid, chunks, paths = rep["analysis_id"], [], []
    chunks.append(_chunk(aid, "business", "business_summary", "tia_business_summary", "System", f"BUSINESS IMPACT: {bi['summary']['headline']}",
                         0.9, "", domain, {"kf_customer_id": bi.get("kf_customer_id"), "fingerprint": bi.get("fingerprint")},
                         {"summary": bi["summary"], "limits": bi["limits"]}))
    for k, c in enumerate(bi["test_cases"]):
        v = c["via"][0]
        weak = WEAK_EVIDENCE.get(c["evidence_status"], 1.0)
        req = v["requirements"][0] if v["requirements"] else None
        text = (f"BUSINESS TEST CASE {c['id']}: covers {req or 'a requirement'} on system {v['system']} ({c['domain']}), which "
                f"{v['service']} realises" + (" (also in the repository suite)" if c["also_in_repository_suite"] else "") + "." + _caveat(c["evidence_status"]))
        chunks.append(_chunk(aid, f"business-test-{k}", "business_test_case", "tia_business_test_case", "TestCase", text,
                             (0.6 if c["also_in_repository_suite"] else 0.85) * weak, c.get("source_file") or "", domain,
                             {"test_case": c["id"], "evidence_status": c["evidence_status"]}, {"via": c["via"], "domain": c["domain"]}))
        label, rel = v.get("label", "System"), v.get("rel", "REALIZES")
        if label == "UserStory":
            story = v["system"].replace("user story ", "")
            chain = (f'(TiaChange:"{v.get("through", "")}")-[:IMPLEMENTS_CHANGE]->(UserStory:"{story}")'
                     + (f'-[:IMPLEMENTS]->(Requirement:"{req}")<-[:COVERS]-(TestCase:"{c["id"]}")' if req else ""))
        elif label == "System":
            chain = (f'(TiaService:"{v["service"]}")-[:REALIZES]->(System:"{v["system"]}")'
                     + (f'<-[:IMPACTS]-(Requirement:"{req}")<-[:COVERS]-(TestCase:"{c["id"]}")' if req else ""))
        else:
            chain = (f'(Tia:"{v.get("through") or v["service"]}")-[:{rel}]->({label}:"{v["system"]}")'
                     + (f'<-[:IMPACTS]-(Requirement:"{req}")<-[:COVERS]-(TestCase:"{c["id"]}")' if req else ""))
        paths.append(PathItem(path_text=chain, score=round(0.85 * weak, 4), from_entity=EntityRef(name=v["service"], type="TiaService"),
                              relationship_type=rel, to_entity=EntityRef(name=c["id"], type="TestCase"),
                              source_block_id=f"{aid}#business-test-{k}", source_file=c.get("source_file") or "", source_page=None,
                              source_evidence=f"bridge link {c['evidence_status']}; Knowledge Fabric relationships from its documents"))
    for k, sy in enumerate(bi["systems"]):
        text = (f"AFFECTED SYSTEM {sy['system']} ({sy['domain']}), realised by {sy['service']}: {len(sy['requirements'])} requirement(s), "
                f"{len(sy['processes'])} process(es)" + (f", defects {', '.join(d['name'] for d in sy['defects'][:5])}" if sy["defects"] else "")
                + (f", incidents {', '.join(i['name'] for i in sy['incidents'][:5])}" if sy["incidents"] else "") + ".")
        chunks.append(_chunk(aid, f"business-system-{k}", "business_system", "tia_business_system", "System", text,
                             0.7 * WEAK_EVIDENCE.get(sy["link"]["status"], 1.0), "", domain,
                             {"service": sy["service"], "system": sy["system"], "link": sy["link"]},
                             {k2: sy[k2] for k2 in ("requirements", "user_stories", "processes", "defects", "incidents", "integrations")}))
    for k, it in enumerate(bi.get("items") or []):
        procs = f", processes {', '.join(p['name'] for p in it['processes'][:5])}" if it.get("processes") else ""
        text = (f"AFFECTED {it['kf_label'].upper()} {it['kf_name']} ({it['domain']}): the change touches {it['kind']} {it['tia_name']} in "
                f"{it['service']}, which {it['rel'].lower()} it; {len(it.get('requirements', []))} requirement(s){procs}." + _caveat(it["link"]["status"]))
        chunks.append(_chunk(aid, f"business-item-{k}", "business_item", "tia_business_item", it["kf_label"], text,
                             0.7 * WEAK_EVIDENCE.get(it["link"]["status"], 1.0), "", domain,
                             {"service": it["service"], "tia": it["tia_name"], "kf": it["kf_name"], "link": it["link"]},
                             {k2: it.get(k2, []) for k2 in ("requirements", "processes", "defects", "incidents", "test_cases")}))
    for k, st in enumerate(bi.get("user_stories") or []):
        title = f" ({st['story']})" if st["story"] != st["key"] else ""
        text = (f"USER STORY {st['key']}{title}: named in commit(s) {', '.join(c[:7] for c in st['commits'][:3])} of this change; implements "
                f"{len(st.get('requirements', []))} requirement(s), covered by {len(st.get('test_cases', []))} test case(s).")
        chunks.append(_chunk(aid, f"business-story-{k}", "business_user_story", "tia_business_user_story", "UserStory", text, 0.9, "", domain,
                             {"issue_key": st["key"], "commits": st["commits"]},
                             {k2: st.get(k2, []) for k2 in ("requirements", "systems", "test_cases")}))
    return chunks, paths


def to_query_response(rep: dict, req: QueryRequest, t0: Optional[float] = None, pack: Optional[dict] = None) -> QueryResponse:
    """Project one (already redacted) report, and its tester pack when given. Material findings rank first in reranked_*; unknowns are
    always included."""
    t0 = t0 if t0 is not None else time.perf_counter()
    findings = rep.get("findings") or []
    chunks = [chunk_for(rep, i, f, req.domain) for i, f in enumerate(findings)]
    unknown_chunks = [unknown_chunk(rep, j, u, req.domain) for j, u in enumerate(rep.get("unknowns") or [])]
    paths = [p for p in (path_for(rep, i, f) for i, f in enumerate(findings)) if p is not None]
    order = sorted(range(len(findings)), key=lambda i: (not findings[i].get("material"), -score_of(findings[i]), i))
    reranked_chunks = [chunks[i] for i in order][: req.top_k_semantic]
    by_block = {p.source_block_id: p for p in paths}
    reranked_paths = [by_block[f"{rep['analysis_id']}#{i}"] for i in order if f"{rep['analysis_id']}#{i}" in by_block]
    # the tester's view: what to test because of the change, ranked on its own (rule gaps first) and bounded by the same top_k
    tchunks = scope_chunks(rep, req.domain)
    tpaths = scope_paths(rep)
    rank = {"rule_gap": 0, "test_scope": 1, "consistency_check": 2}
    # the regression answer (which written test cases to rerun) and the configuration checks are ranked on their own, with their own top_k,
    # so they never push the entry points to test out of the answer
    rrank = {"regression_summary": 0, "regression_test": 1, "config_check": 2, "regression_unmapped": 3}
    rchunks = [c for c in tchunks if c.category in rrank]
    tchunks = [c for c in tchunks if c.category in rank]
    reranked_chunks = (reranked_chunks + sorted(tchunks, key=lambda c: (rank[c.category], -c.score, c.block_id))[: req.top_k_semantic]
                       + sorted(rchunks, key=lambda c: (rrank[c.category], -c.score, c.block_id))[: req.top_k_semantic])
    reranked_paths = (reranked_paths + sorted(tpaths, key=lambda p: (-p.score, p.source_block_id)))[: req.top_k_graph]
    # the tester pack: the gate verdict first, then gaps, API matrices and locator packs, each kind bounded by the same top_k
    pchunks = pack_chunks(pack, req.domain)
    for cat in PACK_RANK:
        reranked_chunks = reranked_chunks + [c for c in pchunks if c.category == cat][: req.top_k_semantic]
    bchunks, bpaths = business_chunks(rep, req.domain)
    reranked_chunks = reranked_chunks + sorted(bchunks, key=lambda c: (BUSINESS_RANK[c.category], -c.score, c.block_id))[: req.top_k_semantic + 1]
    reranked_paths = (reranked_paths + sorted(bpaths, key=lambda p: (-p.score, p.source_block_id)))[: req.top_k_graph]
    first = INTENT_FIRST.get(req.intent)
    if first:
        reranked_chunks = sorted(reranked_chunks, key=lambda c: first.index(c.category) if c.category in first else len(first))
    chunks, paths = chunks + tchunks + rchunks + pchunks + bchunks, paths + tpaths + bpaths
    # unknowns are never ranked away: they ride along after the findings, whatever top_k says
    reranked_chunks = reranked_chunks + unknown_chunks
    errors = []
    if pack and pack["basis"]["unavailable_repositories"]:
        errors.append("tester pack: could not read " + ", ".join(pack["basis"]["unavailable_repositories"]) + " at the analysed revision")
    if rep.get("status") in ("partial", "complete_with_gaps", "failed"):
        errors.append(f"report status {rep['status']}: " + ("the analysis is INCOMPLETE" if rep["status"] == "partial" else
                                                            "the analysis FAILED" if rep["status"] == "failed" else "finished with unknowns that limit confidence"))
    errors += [f"unknown: {u.get('area')}" for u in rep.get("unknowns") or [] if u.get("affects_confidence")]
    if (rep.get("redacted") or {}).get("findings"):
        errors.append(rep["redacted"]["note"])
    trav = (rep.get("coverage") or {}).get("traversal") or {}
    if trav.get("truncated"):
        errors.append("traversal truncated by the configured limits")
    return QueryResponse(
        query=req.query, domain=req.domain, detected_domains=[DetectedDomain(domain=req.domain, confidence=1.0)] if req.domain else [],
        chunks=chunks + unknown_chunks, reranked_chunks=reranked_chunks, paths=paths, reranked_paths=reranked_paths,
        metadata=RetrievalMetadata(intent=req.intent, pattern=f"{rep['analysis_id']} ({rep.get('status')}) {rep.get('change', {}).get('repository', '')}".strip(),
                                   max_depth=int(trav.get("max_depth") or 0), latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                                   chunks_considered=len(chunks) + len(unknown_chunks), paths_considered=len(paths), errors=errors))

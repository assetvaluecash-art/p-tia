"""The reference router: one question, two retrievers (docs/KF-BRIDGE.md section 13).

Knowledge Fabric's POST /query does not work out the intent: the caller names one of its nine traversal patterns. TIA's POST /query/tia does
not read the question: the caller names the change. Both answer in the same QueryResponse shape. This module is the step in front of them,
for Knowledge Fabric's retriever to call or copy (the plan puts the router there, because its agents already call it):

  1. intent    the agent's, or classified from the question by phrase rules (TRIGGERS); fallback `any`
  2. anchors   exact identifiers in the question: issue key, pull request, commit, endpoint, test case id, and repository or service names
               the caller knows (known names); nothing fuzzy, and TIA looks each one up exactly (POST /query/tia/resolve)
  3. lanes     the routing table (ROUTES): Knowledge Fabric only, TIA only, or both
  4. retrieve  both lanes in parallel; the TIA lane has a time limit, and a failure is reported in metadata.errors, never raised
  5. merge     one QueryResponse: the chunks and paths of both, reranked together (the caller's reranker, else alternating the two lanes'
               own rankings, the lane that owns the intent first); TIA's unknown chunks are added after the ranked list, never ranked away;
               chunk and path content is never rewritten, so TIA's evidence words stay as they are

Pure: the lanes are callables (HTTP, in-process, or stand-ins), so it runs inside Knowledge Fabric, in a test, or in tools/ask.py.
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import time
from typing import Callable, Optional

from .kfbridge import ISSUE_KEY

PATTERNS = ("knowledge_discovery", "change_impact", "test_coverage", "regression_scope", "defect_triage", "root_cause", "compliance_control",
            "cross_domain_impact", "any")
# phrase rules per pattern, most specific first; matched on the lower-cased question
TRIGGERS = (
    ("regression_scope", ("which tests", "which test cases", "regression", "rerun", "re-run", "retest", "run again")),
    ("test_coverage", ("covered", "coverage", "untested", "not tested")),
    ("root_cause", ("root cause", "why did", "outage", "incident")),
    ("defect_triage", ("defect", "bug", "triage")),
    ("compliance_control", ("regulation", "compliance", "comply", "control")),
    ("cross_domain_impact", ("cross-domain", "across domains", "other domains")),
    ("change_impact", ("impact", "blast radius", "what breaks", "break", "affected by", "affect")),
    ("knowledge_discovery", ("what do we know", "explain", "describe", "what is")),
)
# how each intent uses the TIA lane: only when the question names a change TIA has analysed, always (TIA's own intent), or never
ROUTES = {"change_impact": "when_anchored", "regression_scope": "when_anchored", "test_coverage": "when_anchored", "any": "when_anchored",
          "software_tia": "tia_only"}
LEAD_INTENTS = ("change_impact", "regression_scope", "test_coverage", "software_tia")
NOT_ISSUE_PREFIXES = {"UTF", "ISO", "SHA", "TLS", "SSL", "RFC", "HTTP", "IPV", "AES", "RSA", "MD", "TCP", "UDP", "PCI", "GDPR", "SOC", "CVE"}
PR = re.compile(r"\b(?:pull request|merge request|PR|MR)\s*#?\s*(\d{1,7})\b|(?<![\w/&])#(\d{1,7})\b", re.I)
COMMIT = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-./{}:]*)")
TEST_CASE = re.compile(r"\bTC-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")


def classify(question: str, given: Optional[str] = None) -> dict:
    """-> {"intent", "how": given | rule | default, "matched"}. A given intent wins when it is a pattern name or software_tia."""
    if given and given in PATTERNS + ("software_tia",):
        return {"intent": given, "how": "given", "matched": None}
    q = " " + " ".join(str(question or "").lower().split()) + " "
    for intent, phrases in TRIGGERS:
        for ph in phrases:
            if re.search(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", q):
                return {"intent": intent, "how": "rule", "matched": ph}
    return {"intent": "any", "how": "default", "matched": None}


def anchors(question: str, known: tuple = ()) -> list:
    """Exact identifiers in the question -> [{"type", "value"[, "repository"]}]. An issue-key-shaped token (NW-11) is offered both as an issue
    key and as a test case id: the lookup says which it is. known: repository and service names the caller may match, whole words only."""
    q = str(question or "")
    out = []

    def add(**a):
        if a not in out:
            out.append(a)
    names = sorted({n for n in known if n}, key=len, reverse=True)
    repo = None
    for n in names:
        if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", q, re.I):
            add(type="repository", value=n)
            add(type="service", value=n)
            repo = repo or n
    for k in ISSUE_KEY.findall(q):
        if k.split("-")[0] in NOT_ISSUE_PREFIXES:
            continue
        add(type="issue_key", value=k)
        add(type="test_case", value=k)
    for m in PR.finditer(q):
        add(type="pull_request", value=m.group(1) or m.group(2), **({"repository": repo} if repo else {}))
    for m in COMMIT.finditer(q):
        v = m.group(1)
        if re.search(r"[0-9]", v) and re.search(r"[a-f]", v):
            add(type="commit", value=v, **({"repository": repo} if repo else {}))
    for m in ENDPOINT.finditer(q):
        add(type="endpoint", value=f"{m.group(1)} {m.group(2)}")
    for m in TEST_CASE.finditer(q):
        add(type="test_case", value=m.group(0))
    return out


def lanes(intent: str, resolved: Optional[dict]) -> dict:
    """The routing table. resolved: TIA's answer to the anchors (POST /query/tia/resolve). -> {"kf", "tia", "why"}."""
    route = ROUTES.get(intent, "kf_only")
    named = bool((resolved or {}).get("analyses"))
    if route == "tia_only":
        return {"kf": False, "tia": named, "why": "TIA's own intent" + ("" if named else ": no analysed change is named")}
    if route == "when_anchored":
        return {"kf": True, "tia": named, "why": ("the question names a change TIA has analysed" if named
                                                  else "no change or code item named: TIA does not guess one from the question")}
    return {"kf": True, "tia": False, "why": f"{intent} is answered from Knowledge Fabric's documents"}


def tia_request(question: str, intent: str, resolved: dict, domain: str = "", product_line: Optional[str] = None,
                customer_id: Optional[str] = None, top_k: int = 10) -> Optional[dict]:
    """The POST /query/tia request for the newest analysis the anchors resolved to, or None."""
    an = (resolved or {}).get("analyses") or []
    if not an:
        return None
    return {"intent": intent if intent in ("change_impact", "regression_scope", "test_coverage", "any") else "software_tia", "query": question,
            "domain": domain or "", "analysis_id": an[0]["analysis_id"], "top_k_semantic": top_k, "top_k_graph": top_k,
            **({"product_line": product_line} if product_line else {}), **({"customer_id": customer_id} if customer_id else {})}


def _alternate(first: list, second: list) -> list:
    out = []
    for i in range(max(len(first), len(second))):
        out += [x for x in (first[i] if i < len(first) else None, second[i] if i < len(second) else None) if x is not None]
    return out


def merge(question: str, intent: str, kf: Optional[dict], tia: Optional[dict], top_k: int = 10,
          rerank: Optional[Callable[[str, list], list]] = None, errors: Optional[list] = None, lane_info: Optional[dict] = None) -> dict:
    """Two QueryResponse dicts -> one. rerank(question, texts) -> scores, the caller's reranker (Knowledge Fabric's); without one the two
    lanes' own rankings alternate, the lane that owns the intent first. TIA's `unknown` chunks are kept after the ranked list."""
    kf, tia = kf or {}, tia or {}
    unknowns = [c for c in tia.get("reranked_chunks") or [] if c.get("category") == "unknown"]
    tia_ranked = [c for c in tia.get("reranked_chunks") or [] if c.get("category") != "unknown"]
    lead_tia = intent in LEAD_INTENTS
    if rerank:
        pool = list(kf.get("reranked_chunks") or []) + tia_ranked
        scores = rerank(question, [c.get("text", "") for c in pool]) if pool else []
        ranked = [c for _, c in sorted(zip(scores, pool), key=lambda x: -x[0])][:top_k]
        ppool = list(kf.get("reranked_paths") or []) + list(tia.get("reranked_paths") or [])
        pscores = rerank(question, [p.get("source_evidence") or p.get("path_text", "") for p in ppool]) if ppool else []
        paths = [p for _, p in sorted(zip(pscores, ppool), key=lambda x: -x[0])][:top_k]
    else:
        a, b = (tia_ranked, kf.get("reranked_chunks") or []) if lead_tia else (kf.get("reranked_chunks") or [], tia_ranked)
        ranked = _alternate(list(a), list(b))[:top_k]
        pa, pb = (tia.get("reranked_paths") or [], kf.get("reranked_paths") or []) if lead_tia else (kf.get("reranked_paths") or [],
                                                                                                    tia.get("reranked_paths") or [])
        paths = _alternate(list(pa), list(pb))[:top_k]
    km, tm = kf.get("metadata") or {}, tia.get("metadata") or {}
    used = [x for x, r in (("kf", kf), ("tia", tia)) if r]
    return {"query": question, "domain": kf.get("domain") or tia.get("domain") or "",
            "detected_domains": kf.get("detected_domains") or tia.get("detected_domains") or [],
            "chunks": list(kf.get("chunks") or []) + list(tia.get("chunks") or []), "reranked_chunks": ranked + unknowns,
            "paths": list(kf.get("paths") or []) + list(tia.get("paths") or []), "reranked_paths": paths,
            "metadata": {"intent": intent, "pattern": km.get("pattern") or tm.get("pattern") or "",
                         "max_depth": max(km.get("max_depth") or 0, tm.get("max_depth") or 0),
                         "latency_ms": round(max(km.get("latency_ms") or 0, tm.get("latency_ms") or 0), 2),
                         "chunks_considered": (km.get("chunks_considered") or 0) + (tm.get("chunks_considered") or 0),
                         "paths_considered": (km.get("paths_considered") or 0) + (tm.get("paths_considered") or 0),
                         "errors": list(km.get("errors") or []) + list(tm.get("errors") or []) + list(errors or []),
                         "lanes": used, **({"tia_analysis_id": tm["pattern"].split(" ")[0]} if tia and tm.get("pattern") else {}),
                         **({"routing": lane_info} if lane_info else {})}}


def ask(question: str, *, call_kf: Optional[Callable[[dict], dict]], call_tia_resolve: Optional[Callable[[dict], dict]],
        call_tia: Optional[Callable[[dict], dict]], intent: Optional[str] = None, domain: str = "", product_line: Optional[str] = None,
        customer_id: Optional[str] = None, known: tuple = (), top_k: int = 10, tia_timeout_s: float = 3.0,
        rerank: Optional[Callable[[str, list], list]] = None) -> dict:
    """The whole flow. call_kf(request) -> Knowledge Fabric's QueryResponse; call_tia_resolve({"anchors", "customer_id"}) -> resolved;
    call_tia(request) -> TIA's QueryResponse. A lane that fails or (TIA) takes longer than tia_timeout_s is reported in metadata.errors."""
    t0 = time.perf_counter()
    c = classify(question, intent)
    errors, resolved = [], None
    found = anchors(question, known)
    if found and call_tia_resolve and ROUTES.get(c["intent"]) in ("when_anchored", "tia_only"):
        try:
            resolved = call_tia_resolve({"anchors": found, **({"customer_id": customer_id} if customer_id else {})})
        except Exception as e:                                       # noqa: BLE001 - a lane problem never fails the question
            errors.append(f"software lane unavailable: {type(e).__name__}: {str(e)[:200]}")
    route = lanes(c["intent"], resolved)
    kf_req = {"intent": c["intent"] if c["intent"] in PATTERNS else "any", "query": question, "domain": domain or "corporate_commercial",
              "top_k_semantic": top_k, "top_k_graph": top_k}
    treq = tia_request(question, c["intent"], resolved or {}, domain, product_line, customer_id, top_k) if route["tia"] else None
    kf_resp = tia_resp = None
    ex = cf.ThreadPoolExecutor(max_workers=2)
    try:
        fk = ex.submit(call_kf, kf_req) if route["kf"] and call_kf else None
        ft = ex.submit(call_tia, treq) if treq and call_tia else None
        if ft is not None:
            try:
                tia_resp = ft.result(timeout=tia_timeout_s)
            except cf.TimeoutError:
                errors.append(f"software lane unavailable: no answer within {tia_timeout_s:g} s")
            except Exception as e:                                   # noqa: BLE001
                errors.append(f"software lane unavailable: {type(e).__name__}: {str(e)[:200]}")
        if fk is not None:
            try:
                kf_resp = fk.result()
            except Exception as e:                                   # noqa: BLE001
                errors.append(f"knowledge lane unavailable: {type(e).__name__}: {str(e)[:200]}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)                 # a slow TIA lane does not hold the answer back
    out = merge(question, c["intent"], kf_resp, tia_resp, top_k, rerank, errors,
                {**route, "intent_how": c["how"], "matched": c["matched"], "anchors": found,
                 "resolved": {k: len(v) for k, v in (resolved or {}).items() if isinstance(v, list)}})
    out["metadata"]["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out

"""TIA's graph mirrored into Neo4j (docs/NEO4J.md).

PostgreSQL stays the system of record: graph versions are built, stored and traversed there (tia.graphstore, docs/GRAPH-STORE.md). This
module projects ONE graph version of a tenant into a Neo4j database (Knowledge Fabric 2.5 runs on Neo4j), so KF agents and Cypher can walk
from a change to the entry points and the written test cases it reaches, and so TIA nodes can be linked to the KF ontology.

What is written (all idempotent MERGEs keyed by `key` = "<tenant>|<TIA node id>", in batches):
  (:TiaNode:Tia<Type> {key, id, tenant, graph_version, type, name, repo, service, file, line, ...})   every graph node, its TIA type as a
                                         label (TiaFunction, TiaEndpoint, TiaTest, TiaSchema, TiaCallsite, TiaService, TiaTable, ...). The Tia
                                         prefix keeps them apart from the KF ontology's own labels (KF has a business "Function").
  -[:<REL> {tia: true, status, strategy, rule, file, line, graph_version}]->   every TIA edge, with TIA's own relationship names
                                         (CALLS, HANDLES, CONSUMES_API, TESTS, READS, WRITES, ...) and directions (consumer -> provider);
                                         `status` is the evidence status, never upgraded.
  -[:IMPACTS {tia: true, via: <REL>, status}]->   the same edges in the direction a change travels (what TIA's impact walk follows), so
                                         "what does a change to X reach" is one variable-length pattern.
  (:TiaNode:TiaTestCase {id, title, entry_point, priority, ...})-[:COVERS {tia: true, status, strategy, via_page}]->(:TiaEndpoint)
                                         the written test cases (testcases/*.csv|yaml|json) and the endpoints they test (named in the
                                         case, or called by the screen it names); status is source_confirmed when the case's entry-point
                                         column names it, inferred when matched from its text.
  (:TiaGraphVersion {key, tenant, graph_version, synced_at, nodes, relationships})
After a sync, the tenant's TIA nodes and relationships of other graph versions are deleted: Neo4j holds the CURRENT landscape of each
tenant. Nothing outside the Tia* labels and `tia: true` relationships is ever touched, except the explicit ontology links below.

Ontology links (link_ontology) are created only from an explicit mapping (TIA entry point or node id -> KF node key) with the KF label and
key property given by the caller: TIA does not guess KF's schema. They are marked {kf_link: true} and survive re-syncs while both ends
exist. Two kinds are computed rather than listed by hand (docs/NEO4J.md "Linking to Knowledge Fabric documents"):
  link_registry     API registry rows -> endpoints, matched by tia.kflinks on service, method and normalised path; each link records
                    {source: 'api_registry', status: confirmed | inferred, strategy, registry_row}
  link_test_cases   KF test-case nodes -> TiaTestCase, joined on the test case id {source: 'test_case_id'}

Access: the mirror holds every repository of the tenant. Neo4j does not apply TIA's per-repository visibility, so grant access to the
Neo4j database only to principals that may read the whole landscape. The connection comes from NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
(NEO4J_DATABASE optional); there is no default password.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional

from . import airgap, kfontology
from .graphschema import TRAVERSED_DST_TO_SRC, TRAVERSED_SRC_TO_DST
from .regression import _endpoints, _pages, targets_of

BATCH = 2000
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SKIP_PROPS = {"key", "tenant", "graph_version", "id"}


class Neo4jUnavailable(Exception):
    """Neo4j is not configured, the driver is not installed, or the server cannot be reached."""


# ---- connection --------------------------------------------------------------------------------------------------------------------------
def config_from_env(env=None) -> Optional[dict]:
    """{"uri", "user", "password", "database"} from NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE, or None when NEO4J_URI is unset."""
    env = os.environ if env is None else env
    uri = (env.get("NEO4J_URI") or "").strip()
    if not uri:
        return None
    if not env.get("NEO4J_PASSWORD"):
        raise Neo4jUnavailable("NEO4J_URI is set but NEO4J_PASSWORD is not (there is no default password)")
    return {"uri": uri, "user": env.get("NEO4J_USER") or "neo4j", "password": env["NEO4J_PASSWORD"],
            "database": (env.get("NEO4J_DATABASE") or "").strip() or None}


def connect(cfg: dict):
    """A verified neo4j driver. Raises Neo4jUnavailable with the reason."""
    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import AuthError, ServiceUnavailable
    except ImportError:
        raise Neo4jUnavailable("the neo4j driver is not installed: pip install 'tia[neo4j]'")
    airgap.guard(cfg["uri"], "Neo4j")
    try:                                                       # the server's notices (unknown property keys, ...) are not for TIA's logs
        drv = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]), notifications_min_severity="OFF")
    except TypeError:                                          # drivers older than 5.7 have no such option
        drv = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        drv.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError) as e:
        drv.close()
        raise Neo4jUnavailable(f"cannot reach Neo4j at {cfg['uri']}: {str(e)[:200]}")
    return drv


# ---- projection (pure: no server needed) -----------------------------------------------------------------------------------------------
label_of = kfontology.label_of          # TIA node type -> Neo4j label (TiaEndpoint, TiaEventSchema, ...)


def _scalar(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return list(v)
    return None


def _impact_reversed() -> set:
    return set(TRAVERSED_DST_TO_SRC)


def project(g, tenant: str, graph_version: str, extracts: Optional[dict] = None) -> dict:
    """The rows to write: {"nodes": {label: [row]}, "rels": {type: [row]}, "impacts": [row], "test_cases": [row], "covers": [row]}."""
    k = lambda nid: f"{tenant}|{nid}"                                    # noqa: E731
    nodes: dict = {}
    for nid, a in g.nodes.items():
        props = {p: s for p, v in a.items() if p not in SKIP_PROPS and (s := _scalar(v)) is not None}
        nodes.setdefault(label_of(a.get("type")), []).append({"key": k(nid), "id": nid, "props": props})
    rels: dict = {}
    impacts, seen = [], set()
    rev = _impact_reversed()
    for e in g.edges:
        if e.src not in g.nodes or e.dst not in g.nodes:
            continue
        if not NAME.match(e.rel or ""):
            continue
        ev = e.evidence or {}
        props = {"status": e.status, "strategy": e.strategy, "rule": e.rule, "file": _scalar(ev.get("file")), "line": _scalar(ev.get("line"))}
        rkey = f"{tenant}|{e.src}|{e.rel}|{e.dst}|{e.strategy or ''}"
        rels.setdefault(e.rel, []).append({"src": k(e.src), "dst": k(e.dst), "rkey": rkey, "props": {p: v for p, v in props.items() if v is not None}})
        if e.rel in TRAVERSED_SRC_TO_DST:
            frm, to = e.src, e.dst
        elif e.rel in rev:
            frm, to = e.dst, e.src
        else:
            continue                                                     # not traversed by TIA (tia.graphschema: impact None)
        ikey = f"{tenant}|{frm}|IMPACTS|{to}|{e.rel}"
        if ikey not in seen:
            seen.add(ikey)
            impacts.append({"src": k(frm), "dst": k(to), "rkey": ikey, "props": {"via": e.rel, "status": e.status}})
    cases, covers = [], []
    if extracts:
        endpoints, pages = _endpoints(extracts), _pages(extracts)
        for repo, ex in sorted(extracts.items()):
            for tc in ex.manual_tests:
                tid = f"testcase:{repo}:{tc['id']}"
                cases.append({"key": k(tid), "id": tid, "props": {"case_id": tc["id"], "repo": repo, "type": "test_case",
                                                                  **{f: tc.get(f) or "" for f in ("title", "entry_point", "priority", "steps",
                                                                                                    "expected", "file")},
                                                                  "line": tc.get("line")}})
                nodes_hit, via_page, named = targets_of(tc, endpoints, pages)
                page_of = {n: p["page"] for p, n in via_page}
                status = "source_confirmed" if (tc.get("entry_point") or "").strip() else "inferred"
                for p in named:                                       # the screen itself (TiaScreen), as well as the APIs behind it
                    sid = f"{p['repo']}:screen:{p['page']}"
                    if sid in g.nodes:
                        covers.append({"src": k(tid), "dst": k(sid), "rkey": f"{tenant}|{tid}|COVERS|{sid}",
                                       "props": {"status": status, "strategy": "screen-named"}})
                for n in sorted(nodes_hit):
                    if n in g.nodes:
                        covers.append({"src": k(tid), "dst": k(n), "rkey": f"{tenant}|{tid}|COVERS|{n}",
                                       "props": {"status": status, "strategy": "screen" if n in page_of else "entry-point",
                                                 **({"via_page": page_of[n]} if n in page_of else {})}})
    return {"nodes": nodes, "rels": rels, "impacts": impacts, "test_cases": cases, "covers": covers}


# ---- writing -----------------------------------------------------------------------------------------------------------------------------
def _batches(rows: list):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def ensure_schema(session):
    session.run("CREATE CONSTRAINT tia_node_key IF NOT EXISTS FOR (n:TiaNode) REQUIRE n.key IS UNIQUE").consume()
    session.run("CREATE CONSTRAINT tia_graph_version_key IF NOT EXISTS FOR (n:TiaGraphVersion) REQUIRE n.key IS UNIQUE").consume()
    session.run("CREATE INDEX tia_node_tenant IF NOT EXISTS FOR (n:TiaNode) ON (n.tenant)").consume()


def _write_nodes(session, label: str, rows: list, tenant: str, version: str):
    assert NAME.match(label)
    q = (f"UNWIND $rows AS r MERGE (n:TiaNode {{key: r.key}}) SET n:{label} "
         "SET n += r.props, n.id = r.id, n.tenant = $tenant, n.graph_version = $version")
    for b in _batches(rows):
        session.execute_write(lambda tx, b=b: tx.run(q, rows=b, tenant=tenant, version=version).consume())


def _write_rels(session, rel: str, rows: list, tenant: str, version: str):
    assert NAME.match(rel)
    q = (f"UNWIND $rows AS r MATCH (a:TiaNode {{key: r.src}}) MATCH (b:TiaNode {{key: r.dst}}) "
         f"MERGE (a)-[x:{rel} {{rkey: r.rkey}}]->(b) SET x += r.props, x.tia = true, x.tenant = $tenant, x.graph_version = $version")
    for b in _batches(rows):
        session.execute_write(lambda tx, b=b: tx.run(q, rows=b, tenant=tenant, version=version).consume())


def sync(driver, proj: dict, tenant: str, graph_version: str, database: Optional[str] = None) -> dict:
    """Write one projection and remove the tenant's TIA nodes and relationships of other graph versions. -> counts."""
    with driver.session(database=database) as s:
        ensure_schema(s)
        for label, rows in sorted(proj["nodes"].items()):
            _write_nodes(s, label, rows, tenant, graph_version)
        _write_nodes(s, "TiaTestCase", proj["test_cases"], tenant, graph_version)
        for rel, rows in sorted(proj["rels"].items()):
            _write_rels(s, rel, rows, tenant, graph_version)
        _write_rels(s, "IMPACTS", proj["impacts"], tenant, graph_version)
        _write_rels(s, "COVERS", proj["covers"], tenant, graph_version)
        def drain(q):                     # delete in bounded transactions until nothing of another graph version is left
            total = 0
            while True:
                n = s.execute_write(lambda tx: tx.run(q, t=tenant, v=graph_version, lim=BATCH * 5).single()["n"])
                total += n
                if n == 0:
                    return total
        stale_rels = drain("MATCH (:TiaNode {tenant: $t})-[x]->() WHERE x.tia = true AND x.tenant = $t AND x.graph_version <> $v "
                           "WITH x LIMIT $lim DELETE x RETURN count(*) AS n")
        stale_nodes = drain("MATCH (n:TiaNode {tenant: $t}) WHERE n.graph_version <> $v WITH n LIMIT $lim DETACH DELETE n RETURN count(*) AS n")
        counts = s.execute_read(lambda tx: tx.run(
            "MATCH (n:TiaNode {tenant: $t}) WITH count(n) AS nodes "
            "OPTIONAL MATCH (:TiaNode {tenant: $t})-[x]->() WHERE x.tia = true RETURN nodes, count(x) AS rels", t=tenant).single())
        s.execute_write(lambda tx: tx.run(
            "MERGE (v:TiaGraphVersion {key: $t}) SET v.tenant = $t, v.graph_version = $v, v.synced_at = $at, v.nodes = $n, v.relationships = $r",
            t=tenant, v=graph_version, at=datetime.now(timezone.utc).isoformat(), n=counts["nodes"], r=counts["rels"]).consume())
    return {"tenant": tenant, "graph_version": graph_version, "nodes": counts["nodes"], "relationships": counts["rels"],
            "test_cases": len(proj["test_cases"]), "covers": len(proj["covers"]),
            "removed_stale": {"nodes": stale_nodes, "relationships": stale_rels}}


def link_ontology(driver, tenant: str, mapping: list, kf_label: str, kf_key: str, rel: str = "IMPLEMENTS",
                  database: Optional[str] = None) -> dict:
    """Explicit links from TIA nodes to KF ontology nodes. mapping: [{"tia": "<METHOD /route> | <rule expression> | <screen name> | <TIA node
    id>", "kf": "<KF key value>"}].
    The KF node is (:<kf_label> {<kf_key>: kf}). Only pairs where both ends exist are linked; the rest are returned, never guessed."""
    for name in (kf_label, kf_key, rel):
        if not NAME.match(name or ""):
            raise ValueError(f"not a valid Neo4j name: {name!r}")
    linked, missing = 0, []
    q_find = ("MATCH (t:TiaNode {tenant: $t}) WHERE t.id = $ref OR (t:TiaEndpoint AND t.method + ' ' + coalesce(t.route, t.path) = $ref) "
              "OR ((t:TiaRule OR t:TiaScreen) AND t.name = $ref) RETURN t.key AS key LIMIT 2")
    q_link = (f"MATCH (t:TiaNode {{key: $tkey}}) MATCH (k:{kf_label} {{{kf_key}: $kf}}) "
              f"MERGE (t)-[x:{rel}]->(k) SET x.kf_link = true, x.tenant = $t RETURN count(x) AS n")
    with driver.session(database=database) as s:
        for m in mapping:
            ref, kf = str(m.get("tia") or "").strip(), m.get("kf")
            keys = [r["key"] for r in s.execute_read(lambda tx: list(tx.run(q_find, t=tenant, ref=ref)))]
            if len(keys) != 1:
                missing.append({**m, "reason": "no TIA node matches" if not keys else "more than one TIA node matches"})
                continue
            n = s.execute_write(lambda tx: tx.run(q_link, tkey=keys[0], kf=kf, t=tenant).single()["n"])
            if n:
                linked += 1
            else:
                missing.append({**m, "reason": f"no :{kf_label} node with {kf_key} = {kf!r}"})
    return {"linked": linked, "not_linked": missing}


def _check_names(*names):
    for name in names:
        if not NAME.match(name or ""):
            raise ValueError(f"not a valid Neo4j name: {name!r}")


def registry_from_neo4j(driver, label: str, key: str, path: str, method: Optional[str] = None, service: Optional[str] = None,
                        database: Optional[str] = None) -> list:
    """The API registry as Knowledge Fabric already holds it: one row per (:<label>) node, with the given property names. The rows have the
    columns key, service, method and path, ready for tia.kflinks.parse_registry."""
    _check_names(label, key, path, *(n for n in (method, service) if n))
    ret = [f"toString(n.{key}) AS key", f"toString(n.{path}) AS path",
           f"toString(n.{method}) AS method" if method else "'' AS method", f"toString(n.{service}) AS service" if service else "'' AS service"]
    q = f"MATCH (n:{label}) WHERE n.{key} IS NOT NULL RETURN {', '.join(ret)} ORDER BY key LIMIT 100000"
    with driver.session(database=database) as s:
        return s.execute_read(lambda tx: [{k: v or "" for k, v in r.data().items()} for r in tx.run(q)])


def link_registry(driver, tenant: str, matched: dict, kf_label: str, kf_key: str, rel: str = "IMPLEMENTS",
                  include_inferred: bool = True, database: Optional[str] = None, customer_id: Optional[str] = None) -> dict:
    """Write the registry links of tia.kflinks.match_registry: (:TiaEndpoint)-[:<rel> {kf_link, source: 'api_registry', status, strategy,
    registry_row}]->(:<kf_label> {<kf_key>: key}). The tenant's earlier registry links that this result no longer has are removed, so the
    links always reflect the latest registry. -> {"linked", "removed", "not_linked"}."""
    _check_names(kf_label, kf_key, rel)
    want = ("confirmed", "inferred") if include_inferred else ("confirmed",)
    rows = [{"tkey": f"{tenant}|{r['endpoint']}", "kf": r["key"], "status": r["status"], "strategy": r["strategy"], "row": r["row"]}
            for r in matched["rows"] if r["status"] in want]
    q = (f"UNWIND $rows AS r OPTIONAL MATCH (t:TiaEndpoint {{key: r.tkey}}) OPTIONAL MATCH (k:{kf_label} {{{kf_key}: r.kf}}) "
         "WHERE $cust IS NULL OR k.customer_id = $cust "
         f"WITH r, t, collect(k) AS ks "
         f"FOREACH (k IN CASE WHEN t IS NULL OR size(ks) <> 1 THEN [] ELSE ks END | "
         f"  MERGE (t)-[x:{rel}]->(k) SET x.kf_link = true, x.tenant = $t, x.source = 'api_registry', x.status = r.status, "
         f"  x.strategy = r.strategy, x.registry_row = r.row) "
         f"RETURN r.row AS row, r.kf AS kf, t IS NOT NULL AS tia, size(ks) AS kf_nodes")
    q_clean = (f"MATCH (t:TiaEndpoint {{tenant: $t}})-[x:{rel} {{source: 'api_registry'}}]->(k:{kf_label}) "
               f"WHERE NOT [t.key, toString(k.{kf_key})] IN $keep DELETE x RETURN count(x) AS n")
    with driver.session(database=database) as s:
        res = s.execute_write(lambda tx: [r.data() for r in tx.run(q, rows=rows, t=tenant, cust=customer_id)])
        ok = [r for r in res if r["tia"] and r["kf_nodes"] == 1]
        keep = [[x["tkey"], str(x["kf"])] for x in rows if any(o["row"] == x["row"] for o in ok)]
        removed = s.execute_write(lambda tx: tx.run(q_clean, t=tenant, keep=keep).single()["n"])
    not_linked = [{"row": r["row"], "key": r["kf"],
                   "reason": "the TIA endpoint is not in the mirror (run the sync first)" if not r["tia"]
                   else f"no :{kf_label} node with {kf_key} = {r['kf']!r}" if r["kf_nodes"] == 0
                   else f"{r['kf_nodes']} :{kf_label} nodes have {kf_key} = {r['kf']!r}"} for r in res if not (r["tia"] and r["kf_nodes"] == 1)]
    return {"linked": len(ok), "removed": removed, "not_linked": not_linked}


def link_test_cases(driver, tenant: str, kf_label: str, kf_key: str, rel: str = "SAME_AS", database: Optional[str] = None,
                    customer_id: Optional[str] = None) -> dict:
    """Join TIA's written test cases to Knowledge Fabric's test-case nodes on the test case id: (:TiaTestCase {case_id})-[:<rel>
    {kf_link, source: 'test_case_id', status: 'source_confirmed'}]->(:<kf_label> {<kf_key>: case_id}). Ids are compared exactly (after
    trimming). -> {"linked", "tia_without_kf", "kf_without_tia", "duplicate_kf_ids"}."""
    _check_names(kf_label, kf_key, rel)
    q_link = (f"MATCH (tc:TiaTestCase {{tenant: $t}}) WHERE tc.case_id IS NOT NULL "
              f"MATCH (k:{kf_label}) WHERE trim(toString(k.{kf_key})) = trim(tc.case_id) AND ($cust IS NULL OR k.customer_id = $cust) "
              f"MERGE (tc)-[x:{rel}]->(k) SET x.kf_link = true, x.tenant = $t, x.source = 'test_case_id', x.status = 'source_confirmed' "
              f"RETURN count(x) AS n")
    q_clean = (f"MATCH (tc:TiaTestCase {{tenant: $t}})-[x:{rel} {{source: 'test_case_id'}}]->(k:{kf_label}) "
               f"WHERE trim(toString(k.{kf_key})) <> trim(tc.case_id) DELETE x")
    q_tia = (f"MATCH (tc:TiaTestCase {{tenant: $t}}) WHERE NOT (tc)-[:{rel} {{source: 'test_case_id'}}]->(:{kf_label}) "
             f"RETURN tc.case_id AS id, tc.repo AS repo ORDER BY id")
    q_kf = (f"MATCH (k:{kf_label}) WHERE k.{kf_key} IS NOT NULL AND ($cust IS NULL OR k.customer_id = $cust) AND NOT (:TiaTestCase {{tenant: $t}})-[:{rel} {{source: 'test_case_id'}}]->(k) "
            f"RETURN toString(k.{kf_key}) AS id ORDER BY id LIMIT 1000")
    q_dup = (f"MATCH (k:{kf_label}) WHERE k.{kf_key} IS NOT NULL AND ($cust IS NULL OR k.customer_id = $cust) WITH trim(toString(k.{kf_key})) AS id, count(*) AS n WHERE n > 1 "
             f"RETURN id, n ORDER BY id")
    with driver.session(database=database) as s:
        s.execute_write(lambda tx: tx.run(q_clean, t=tenant).consume())
        n = s.execute_write(lambda tx: tx.run(q_link, t=tenant, cust=customer_id).single()["n"])
        tia_only = s.execute_read(lambda tx: [r.data() for r in tx.run(q_tia, t=tenant)])
        kf_only = s.execute_read(lambda tx: [r["id"] for r in tx.run(q_kf, t=tenant, cust=customer_id)])
        dups = s.execute_read(lambda tx: [r.data() for r in tx.run(q_dup, cust=customer_id)])
    return {"linked": n, "tia_without_kf": tia_only, "kf_without_tia": kf_only, "duplicate_kf_ids": dups}


# ---- reading: use case 1 in Cypher --------------------------------------------------------------------------------------------------------
REGRESSION_CYPHER = """
MATCH (c:TiaNode {tenant: $tenant}) WHERE c.id IN $changed
MATCH p = (c)-[:IMPACTS*0..%d]->(e:TiaEndpoint)
WHERE all(n IN nodes(p) WHERE NOT n:TiaTest)
WITH DISTINCT e, min(length(p)) AS hops
MATCH (tc:TiaTestCase {tenant: $tenant})-[cov:COVERS]->(e)
RETURN tc.case_id AS test_case, tc.title AS title, tc.repo AS repo, tc.priority AS priority,
       collect(DISTINCT {entry_point: e.method + ' ' + coalesce(e.route, e.path), hops: hops, covers: cov.status,
                         via_page: cov.via_page}) AS reached
ORDER BY test_case
"""


def regression_tests(driver, tenant: str, changed: list, max_depth: int = 8, database: Optional[str] = None) -> list:
    """The written test cases a change reaches, read from Neo4j: changed TIA node ids -> [{test_case, title, repo, priority, reached}]."""
    q = REGRESSION_CYPHER % max(0, min(int(max_depth), 20))
    with driver.session(database=database) as s:
        return s.execute_read(lambda tx: [r.data() for r in tx.run(q, tenant=tenant, changed=list(changed))])


# ---- the Knowledge Fabric bridge (docs/KF-BRIDGE.md; planning is in tia.kfbridge) --------------------------------------------------
# Knowledge Fabric's graph is multi-customer: every read below is confined to one customer (and its tenant), and every bridge edge records
# both sides' scope. TIA never creates, edits or deletes a Knowledge Fabric node; it writes only edges from its own nodes (bridge=true,
# owner='tia') and prunes only those.
KF_CONTEXT_LABELS = ("System", "TestCase", "Requirement", "UserStory", "Process", "Defect", "Incident", "AcceptanceCriteria", "FormCapture",
                     "DataEntity", "Rule")
KF_CONTEXT_RELS = ("COVERS", "IMPACTS", "USES", "IMPLEMENTS", "VALIDATES", "INTEGRATES_WITH", "MISSED_BY", "GOVERNED_BY", "PRODUCES")


def kf_customers(driver, database: Optional[str] = None) -> list:
    """Knowledge Fabric's customers, from its CustomerRoot nodes: [{customer_id, tenant_id, industry_id, domains}]."""
    q = ("MATCH (c:CustomerRoot) OPTIONAL MATCH (d:DomainRoot {customer_id: c.customer_id}) "
         "RETURN c.customer_id AS customer_id, c.tenant_id AS tenant_id, c.industry_id AS industry_id, "
         "[x IN collect(DISTINCT d.domain) WHERE x IS NOT NULL] AS domains ORDER BY customer_id, tenant_id")
    with driver.session(database=database) as s:
        return s.execute_read(lambda tx: [{**r.data(), "domains": sorted(r["domains"])} for r in tx.run(q)])


def kf_customer(driver, customer_id: str, database: Optional[str] = None) -> Optional[dict]:
    """The customer's CustomerRoot (its tenant id is part of Knowledge Fabric's identity scope); None if Knowledge Fabric has no such customer."""
    rows = [c for c in kf_customers(driver, database) if c["customer_id"] == customer_id]
    if not rows:
        return None
    if len({r["tenant_id"] for r in rows}) > 1:
        return {**rows[0], "tenant_ids": sorted({r["tenant_id"] for r in rows if r["tenant_id"]}), "ambiguous_tenant": True}
    return rows[0]


def kf_domains(driver, customer_id: str, tenant_id: Optional[str], database: Optional[str] = None) -> list:
    """The domains the customer has in Knowledge Fabric: its DomainRoot nodes, and any domain its Systems and TestCases carry."""
    q1 = "MATCH (d:DomainRoot) WHERE d.customer_id = $c AND ($t IS NULL OR d.tenant_id IS NULL OR d.tenant_id = $t) RETURN DISTINCT d.domain AS domain"
    q2 = "MATCH (n) WHERE (n:System OR n:TestCase) AND n.customer_id = $c AND ($t IS NULL OR n.tenant_id = $t) RETURN DISTINCT n.domain AS domain"
    with driver.session(database=database) as s:
        found = {r["domain"] for q in (q1, q2) for r in s.execute_read(lambda tx, q=q: list(tx.run(q, c=customer_id, t=tenant_id)))}
    return sorted(d for d in found if d)


def kf_index(driver, customer_id: str, tenant_id: Optional[str], domains: list, labels: tuple = ("System", "TestCase"),
             database: Optional[str] = None) -> dict:
    """The customer's nodes of each label in the given domains: {label: [{entity_id, name, aliases, domain, description, product_lines,
    functions}]}. product_lines / functions: where the node sits in Knowledge Fabric's business tree (its PART_OF chain), which the bridge
    uses to break a tie between two nodes of one name (docs/KF-BRIDGE.md section 11)."""
    out = {}
    with driver.session(database=database) as s:
        for label in labels:
            assert NAME.match(label)
            q = (f"MATCH (n:{label}) WHERE n.customer_id = $c AND ($t IS NULL OR n.tenant_id = $t) AND n.domain IN $domains "
                 "AND n.entity_id IS NOT NULL "
                 "OPTIONAL MATCH (n)-[:PART_OF*1..3]->(pl:ProductLine) WHERE pl.customer_id = $c "
                 "WITH n, collect(DISTINCT pl.name) AS pls "
                 "OPTIONAL MATCH (n)-[:PART_OF*1..2]->(fn:Function) WHERE fn.customer_id = $c "
                 "WITH n, pls, collect(DISTINCT fn.name) AS fns "
                 "RETURN n.entity_id AS entity_id, n.name AS name, coalesce(n.aliases, []) AS aliases, n.domain AS domain, "
                 "n.description AS description, [x IN pls WHERE x IS NOT NULL] AS product_lines, [x IN fns WHERE x IS NOT NULL] AS functions "
                 "ORDER BY domain, entity_id")
            rows = s.execute_read(lambda tx, q=q: [r.data() for r in tx.run(q, c=customer_id, t=tenant_id, domains=list(domains))])
            for r in rows:
                r["product_lines"], r["functions"] = sorted(r["product_lines"]), sorted(r["functions"])
            out[label] = rows
    return out


_CATALOG_Q = """
MATCH (d:DomainRoot) WHERE d.customer_id = $c AND ($t IS NULL OR d.tenant_id IS NULL OR d.tenant_id = $t)
OPTIONAL MATCH (pl:ProductLine)-[:PART_OF]->(d) WHERE pl.customer_id = $c
OPTIONAL MATCH (fn:Function)-[:PART_OF]->(pl) WHERE fn.customer_id = $c
OPTIONAL MATCH (sf:SubFunction)-[:PART_OF]->(fn) WHERE sf.customer_id = $c
RETURN d.domain AS domain, pl.name AS pl, pl.entity_id AS pl_id, pl.product_line_id AS pl_pid, coalesce(pl.aliases, []) AS pl_al,
       fn.name AS fn, fn.entity_id AS fn_id, coalesce(fn.aliases, []) AS fn_al, sf.name AS sf, sf.entity_id AS sf_id, coalesce(sf.aliases, []) AS sf_al
"""


def kf_catalog(driver, customer_id: str, tenant_id: Optional[str], database: Optional[str] = None) -> dict:
    """The customer's business tree, read from the PART_OF edges Knowledge Fabric writes (never from a function's product_line_id property,
    which keeps only the last of its product lines): {"domains": [{"domain", "product_lines": [{"name", "entity_id", "product_line_id",
    "aliases", "functions": [{"name", "entity_id", "aliases", "sub_functions": [...]}]}]}], "functions_present": bool}. A product line that
    Knowledge Fabric's review writer created carries a product_line_id but no entity_id."""
    with driver.session(database=database) as s:
        rows = s.execute_read(lambda tx: [r.data() for r in tx.run(_CATALOG_Q, c=customer_id, t=tenant_id)])
    doms: dict = {}
    for r in rows:
        d = doms.setdefault(r["domain"], {"domain": r["domain"], "product_lines": {}})
        if not r["pl"]:
            continue
        pid = r["pl_pid"] or f"DOMAIN:{r['domain']}|PRODUCT_LINE:{r['pl']}"
        pl = d["product_lines"].setdefault(pid, {"name": r["pl"], "entity_id": r["pl_id"], "product_line_id": pid,
                                                 "aliases": sorted(set(r["pl_al"])), "functions": {}})
        if not r["fn"]:
            continue
        fn = pl["functions"].setdefault(r["fn_id"] or r["fn"], {"name": r["fn"], "entity_id": r["fn_id"], "aliases": sorted(set(r["fn_al"])),
                                                              "sub_functions": {}})
        if r["sf"]:
            fn["sub_functions"].setdefault(r["sf_id"] or r["sf"], {"name": r["sf"], "entity_id": r["sf_id"], "aliases": sorted(set(r["sf_al"]))})
    out, functions = [], False
    for dn in sorted(doms):
        pls = []
        for pl in sorted(doms[dn]["product_lines"].values(), key=lambda x: x["name"]):
            fns = [{**f, "sub_functions": sorted(f["sub_functions"].values(), key=lambda x: x["name"])}
                   for f in sorted(pl["functions"].values(), key=lambda x: x["name"])]
            functions = functions or bool(fns)
            pls.append({**pl, "functions": fns})
        out.append({"domain": dn, "product_lines": pls})
    return {"domains": out, "functions_present": functions}


def kf_fingerprint(driver, customer_id: str, tenant_id: Optional[str], database: Optional[str] = None) -> str:
    """A cheap fingerprint of the customer's business graph: per label the node count and the latest update, plus the number of
    relationships among those nodes. It changes when an ingestion or a review writes anything the bridge or the business context uses."""
    import hashlib
    parts = []
    with driver.session(database=database) as s:
        for label in KF_CONTEXT_LABELS:
            r = s.execute_read(lambda tx, label=label: tx.run(
                f"MATCH (n:{label}) WHERE n.customer_id = $c AND ($t IS NULL OR n.tenant_id = $t) "
                "RETURN count(n) AS n, toString(max(n.updated_at)) AS u, toString(max(n.created_at)) AS cr", c=customer_id, t=tenant_id).single())
            parts.append(f"{label}:{r['n']}:{r['u']}:{r['cr']}")
        rels = s.execute_read(lambda tx: tx.run(
            "MATCH (a)-[r]->(b) WHERE a.customer_id = $c AND b.customer_id = $c AND type(r) IN $rels AND ($t IS NULL OR a.tenant_id = $t) "
            "RETURN count(r) AS n", c=customer_id, t=tenant_id, rels=list(KF_CONTEXT_RELS)).single()["n"])
        parts.append(f"rels:{rels}")
    return "kf-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def mirror_version(driver, tenant: str, database: Optional[str] = None) -> Optional[str]:
    """The graph version mirrored for this tenant, or None when the mirror is missing (for example after Knowledge Fabric's reset script,
    which deletes every node in the database)."""
    with driver.session(database=database) as s:
        r = s.execute_read(lambda tx: tx.run("MATCH (v:TiaGraphVersion {key: $t}) OPTIONAL MATCH (n:TiaNode {tenant: $t}) "
                                             "RETURN v.graph_version AS v, count(n) AS n", t=tenant).single())
    return r["v"] if r and r["v"] and r["n"] else None


def write_bridge(driver, tenant: str, customer_id: str, kf_tenant_id: Optional[str], links: list, meta: dict,
                 database: Optional[str] = None) -> dict:
    """Write the planned bridge links (tia.kfbridge.plan) and delete this tenant's bridge edges to this customer that the plan no longer
    has. meta: {tia_graph_version, kf_fingerprint}. -> {"added", "kept", "removed", "not_written"}."""
    from datetime import datetime as _dt, timezone as _tz
    from .kfontology import BRIDGE_VERSION, allowed
    rows, not_written = [], []
    for ln in links:
        src_label = ln.get("from_label") or ("TiaService" if ln["rel"] == "REALIZES" else "TiaTestCase")
        if not allowed(src_label, ln["rel"], ln["to_label"]) or not NAME.match(ln["rel"]) or not NAME.match(ln["to_label"]):
            raise ValueError(f"not a bridge relationship: ({src_label})-[:{ln['rel']}]->({ln['to_label']})")
        if not NAME.match(src_label):
            raise ValueError(f"not a TIA label: {src_label}")
        to_key = ln.get("to_key") or "entity_id"
        if to_key not in ("entity_id", "product_line_id"):
            raise ValueError(f"not a Knowledge Fabric key property: {to_key}")
        rows.append({"src": ln["from"], "src_label": src_label, "dst": ln["to"], "label": ln["to_label"], "rel": ln["rel"], "to_key": to_key,
                     "bkey": f"{tenant}|{ln['from']}|{ln['rel']}|{ln['to']}",
                     "props": {"status": ln["status"], "strategy": ln["strategy"], "rule": ln.get("rule"), "source": ln["source"],
                               "id_check": ln["id_check"], "domain": ln["domain"],
                               **{k: ln.get(k) for k in ("product_line", "product_line_id", "level", "decided_by", "decided_at")}}})
    now = _dt.now(_tz.utc).isoformat()
    common = {"tenant": tenant, "customer": customer_id, "kft": kf_tenant_id, "gv": meta.get("tia_graph_version"),
              "fp": meta.get("kf_fingerprint"), "bv": BRIDGE_VERSION, "at": now}
    with driver.session(database=database) as s:
        before = set(s.execute_read(lambda tx: [r["k"] for r in tx.run(
            "MATCH (:TiaNode {tenant: $tenant})-[x]->() WHERE x.bridge = true AND x.kf_customer_id = $customer RETURN x.bkey AS k",
            tenant=tenant, customer=customer_id)]))
        written = set()
        for (src_label, rel, label, to_key) in sorted({(r["src_label"], r["rel"], r["label"], r["to_key"]) for r in rows}):
            batch = [r for r in rows if r["rel"] == rel and r["label"] == label and r["src_label"] == src_label and r["to_key"] == to_key]
            q = (f"UNWIND $rows AS r MATCH (a:{src_label} {{key: r.src}}) "
                 f"MATCH (b:{label} {{{to_key}: r.dst}}) WHERE b.customer_id = $customer "
                 f"MERGE (a)-[x:{rel} {{bkey: r.bkey}}]->(b) "
                 "SET x += r.props, x.bridge = true, x.owner = 'tia', x.tenant = $tenant, x.kf_customer_id = $customer, x.kf_tenant_id = $kft, "
                 "x.tia_graph_version = $gv, x.kf_fingerprint = $fp, x.bridge_version = $bv, x.linked_at = coalesce(x.linked_at, $at) "
                 "RETURN r.bkey AS k")
            for b in _batches(batch):
                written |= set(s.execute_write(lambda tx, b=b: [r["k"] for r in tx.run(q, rows=b, **common)]))
        not_written = [r["bkey"] for r in rows if r["bkey"] not in written]
        removed = s.execute_write(lambda tx: tx.run(
            "MATCH (:TiaNode {tenant: $tenant})-[x]->() WHERE x.bridge = true AND x.kf_customer_id = $customer AND NOT x.bkey IN $keep "
            "DELETE x RETURN count(x) AS n", tenant=tenant, customer=customer_id, keep=sorted(written)).single()["n"])
    return {"added": len(written - before), "kept": len(written & before), "removed": removed, "not_written": not_written}


_CONTEXT_Q = """
MATCH (s:TiaService {tenant: $tenant})-[b:REALIZES]->(sys:System) WHERE b.bridge = true AND b.kf_customer_id = $customer
WITH s, b, sys ORDER BY s.service, sys.domain, sys.name
OPTIONAL MATCH (r:Requirement)-[:IMPACTS]->(sys) WHERE r.customer_id = $customer
WITH s, b, sys, collect(DISTINCT r)[..$cap] AS reqs
OPTIONAL MATCH (u:UserStory)-[:IMPACTS]->(sys) WHERE u.customer_id = $customer
WITH s, b, sys, reqs, collect(DISTINCT u)[..$cap] AS stories
OPTIONAL MATCH (p:Process)-[:USES]->(sys) WHERE p.customer_id = $customer
WITH s, b, sys, reqs, stories, collect(DISTINCT p)[..$cap] AS procs
OPTIONAL MATCH (d:Defect)-[:IMPACTS]->(sys) WHERE d.customer_id = $customer
WITH s, b, sys, reqs, stories, procs, collect(DISTINCT d)[..$cap] AS defects
OPTIONAL MATCH (i:Incident)-[:IMPACTS]->(sys) WHERE i.customer_id = $customer
WITH s, b, sys, reqs, stories, procs, defects, collect(DISTINCT i)[..$cap] AS incidents
OPTIONAL MATCH (sys)-[:INTEGRATES_WITH]-(o:System) WHERE o.customer_id = $customer
WITH s, b, sys, reqs, stories, procs, defects, incidents, collect(DISTINCT o)[..$cap] AS others
UNWIND (CASE WHEN size(reqs) = 0 THEN [null] ELSE reqs END) AS req
OPTIONAL MATCH (tc:TestCase)-[:COVERS]->(req) WHERE tc.customer_id = $customer
OPTIONAL MATCH (tc)<-[sa:SAME_AS]-(t:TiaTestCase {tenant: $tenant}) WHERE sa.bridge = true
WITH s, b, sys, reqs, stories, procs, defects, incidents, others, req, tc, collect(DISTINCT t.case_id) AS tia_ids
WITH s, b, sys, reqs, stories, procs, defects, incidents, others,
     collect(CASE WHEN tc IS NULL THEN null ELSE {tc: tc, req: req.name, tia_ids: tia_ids} END)[..$cap2] AS cover_rows
RETURN s.service AS service, sys.entity_id AS entity_id, sys.name AS system, sys.domain AS domain,
       {status: b.status, strategy: b.strategy, source: b.source, rule: b.rule} AS link,
       reqs, stories, procs, defects, incidents, others, cover_rows
"""


def _item(n) -> dict:
    p = dict(n)
    out = {"entity_id": p.get("entity_id"), "name": p.get("name_display") or p.get("name")}
    if p.get("description"):
        out["description"] = str(p["description"])[:300]
    src = p.get("source_document_ids") or []
    if src:
        out["source_documents"] = list(src)[:5]
    blocks = p.get("source_blocks") or []
    if blocks:
        out["source_blocks"] = list(blocks)[:5]
    return out


def read_context(driver, tenant: str, customer_id: str, max_items: int = 100, database: Optional[str] = None) -> dict:
    """The business context of every linked service: per (service, System, domain) the requirements and user stories that impact the
    system, the processes that use it, the test cases covering those requirements (with the TIA test case ids they are SAME_AS), defects,
    incidents and integrated systems. Bounded by max_items per kind; a kind cut short is listed under `truncated`. -> {service: [entry]}."""
    with driver.session(database=database) as s:
        rows = s.execute_read(lambda tx: [r for r in tx.run(_CONTEXT_Q, tenant=tenant, customer=customer_id, cap=max_items + 1,
                                                            cap2=(max_items + 1) * 4)])
    out: dict = {}
    for r in rows:
        trunc = {}

        def cut(kind, items):
            if len(items) > max_items:
                trunc[kind] = True
                items = items[:max_items]
            return items
        cases: dict = {}
        for cr in r["cover_rows"]:
            if cr is None:
                continue
            node = _item(cr["tc"])
            c = cases.setdefault(node["entity_id"], {**node, "covers": [], "tia_case_ids": []})
            if cr["req"] and cr["req"] not in c["covers"]:
                c["covers"].append(cr["req"])
            c["tia_case_ids"] = sorted(set(c["tia_case_ids"]) | {x for x in cr["tia_ids"] if x})
        entry = {"system": r["system"], "entity_id": r["entity_id"], "domain": r["domain"], "link": r["link"],
                 "requirements": cut("requirements", [_item(x) for x in r["reqs"]]),
                 "user_stories": cut("user_stories", [_item(x) for x in r["stories"]]),
                 "processes": cut("processes", [_item(x) for x in r["procs"]]),
                 "defects": cut("defects", [_item(x) for x in r["defects"]]),
                 "incidents": cut("incidents", [_item(x) for x in r["incidents"]]),
                 "integrations": cut("integrations", [{"name": dict(x).get("name"), "domain": dict(x).get("domain"),
                                                       "entity_id": dict(x).get("entity_id")} for x in r["others"]]),
                 "test_cases": cut("test_cases", sorted(cases.values(), key=lambda c: c["name"] or ""))}
        for k in ("requirements", "user_stories", "processes", "defects", "incidents", "integrations", "test_cases"):
            entry[k] = sorted(entry[k], key=lambda x: (x.get("name") or "", x.get("entity_id") or ""))
        if trunc:
            entry["truncated"] = trunc
        out.setdefault(r["service"], []).append(entry)
    return out


def _cover_cases(cover_rows) -> list:
    """Rows {tc, req, tia_ids} -> test cases with the requirements they cover and the TIA test case ids they are SAME_AS."""
    cases: dict = {}
    for cr in cover_rows:
        if cr is None:
            continue
        node = _item(cr["tc"])
        c = cases.setdefault(node["entity_id"], {**node, "covers": [], "tia_case_ids": []})
        if cr["req"] and cr["req"] not in c["covers"]:
            c["covers"].append(cr["req"])
        c["tia_case_ids"] = sorted(set(c["tia_case_ids"]) | {x for x in cr["tia_ids"] if x})
    return sorted(cases.values(), key=lambda c: (c.get("name") or "", c["entity_id"] or ""))


_ITEM_CONTEXT_Q = """
MATCH (t:TiaNode {tenant: $tenant})-[b]->(k) WHERE b.bridge = true AND b.kf_customer_id = $customer AND k.customer_id = $customer
  AND type(b) IN ['REALIZES', 'STORES', 'ENFORCES'] AND NOT t:TiaService
WITH t, b, k ORDER BY t.id, k.domain, k.name
OPTIONAL MATCH (r:Requirement)-[:IMPACTS]->(k) WHERE r.customer_id = $customer
WITH t, b, k, collect(DISTINCT r)[..$cap] AS reqs
OPTIONAL MATCH (p:Process)-[:GOVERNED_BY|PRODUCES]->(k) WHERE p.customer_id = $customer
WITH t, b, k, reqs, collect(DISTINCT p)[..$cap] AS procs
OPTIONAL MATCH (d:Defect)-[:IMPACTS]->(k) WHERE d.customer_id = $customer
WITH t, b, k, reqs, procs, collect(DISTINCT d)[..$cap] AS defects
OPTIONAL MATCH (i:Incident)-[:IMPACTS]->(k) WHERE i.customer_id = $customer
WITH t, b, k, reqs, procs, defects, collect(DISTINCT i)[..$cap] AS incidents
UNWIND (CASE WHEN size(reqs) = 0 THEN [null] ELSE reqs END) AS req
OPTIONAL MATCH (tc:TestCase)-[:COVERS]->(req) WHERE tc.customer_id = $customer
OPTIONAL MATCH (tc)<-[sa:SAME_AS]-(tt:TiaTestCase {tenant: $tenant}) WHERE sa.bridge = true
WITH t, b, k, reqs, procs, defects, incidents, req, tc, collect(DISTINCT tt.case_id) AS tia_ids
WITH t, b, k, reqs, procs, defects, incidents,
     collect(CASE WHEN tc IS NULL THEN null ELSE {tc: tc, req: req.name, tia_ids: tia_ids} END)[..$cap2] AS cover_rows
RETURN t.id AS tia_id, t.service AS service, t.repo AS repo, coalesce(t.name, t.id) AS tia_name, labels(t) AS tlabels, type(b) AS rel,
       labels(k) AS klabels, k.entity_id AS entity_id, k.name AS kf_name, k.domain AS domain,
       {status: b.status, strategy: b.strategy, source: b.source, rule: b.rule} AS link, reqs, procs, defects, incidents, cover_rows
"""
ITEM_KIND_OF = {"TiaScreen": "screen", "TiaEntity": "entity", "TiaTable": "table", "TiaRule": "rule"}


def read_item_context(driver, tenant: str, customer_id: str, service_of_repo: dict, max_items: int = 100,
                      database: Optional[str] = None) -> dict:
    """Phase 7 context: every linked screen, entity, table and rule with its Knowledge Fabric FormCapture, DataEntity or Rule, the
    requirements, processes, defects and incidents tied to that node, and the test cases covering those requirements. A table has no
    service of its own: the service of its repository is used. -> {service: [item]}."""
    with driver.session(database=database) as s:
        rows = s.execute_read(lambda tx: [r for r in tx.run(_ITEM_CONTEXT_Q, tenant=tenant, customer=customer_id, cap=max_items,
                                                            cap2=max_items * 4)])
    out: dict = {}
    for r in rows:
        kind = next((ITEM_KIND_OF[x] for x in r["tlabels"] if x in ITEM_KIND_OF), None)
        kf_label = next((x for x in r["klabels"] if x in ("FormCapture", "DataEntity", "Rule")), None)
        svc = r["service"] or service_of_repo.get(r["repo"])
        if not kind or not kf_label or not svc:
            continue
        item = {"kind": kind, "tia_id": r["tia_id"], "tia_name": r["tia_name"], "rel": r["rel"], "kf_label": kf_label, "kf_name": r["kf_name"],
                "entity_id": r["entity_id"], "domain": r["domain"], "link": r["link"],
                "requirements": sorted((_item(x) for x in r["reqs"]), key=lambda x: x["name"] or ""),
                "processes": sorted((_item(x) for x in r["procs"]), key=lambda x: x["name"] or ""),
                "defects": sorted((_item(x) for x in r["defects"]), key=lambda x: x["name"] or ""),
                "incidents": sorted((_item(x) for x in r["incidents"]), key=lambda x: x["name"] or ""),
                "test_cases": _cover_cases(r["cover_rows"])}
        out.setdefault(svc, []).append(item)
    return out


_STORY_Q = """
UNWIND $ids AS id MATCH (u:UserStory {entity_id: id}) WHERE u.customer_id = $customer
OPTIONAL MATCH (u)-[:IMPLEMENTS]->(r:Requirement) WHERE r.customer_id = $customer
WITH u, collect(DISTINCT r)[..$cap] AS reqs
OPTIONAL MATCH (u)-[:IMPACTS]->(s:System) WHERE s.customer_id = $customer
WITH u, reqs, collect(DISTINCT s)[..$cap] AS systems
UNWIND (CASE WHEN size(reqs) = 0 THEN [null] ELSE reqs END) AS req
OPTIONAL MATCH (tc:TestCase)-[:COVERS]->(req) WHERE tc.customer_id = $customer
OPTIONAL MATCH (tc)<-[sa:SAME_AS]-(tt:TiaTestCase {tenant: $tenant}) WHERE sa.bridge = true
WITH u, reqs, systems, req, tc, collect(DISTINCT tt.case_id) AS tia_ids
WITH u, reqs, systems, collect(CASE WHEN tc IS NULL THEN null ELSE {tc: tc, req: req.name, tia_ids: tia_ids} END)[..$cap2] AS cover_rows
RETURN u.entity_id AS entity_id, u.name AS name, u.domain AS domain, reqs, systems, cover_rows
"""


def read_stories(driver, tenant: str, customer_id: str, kf_tenant_id: Optional[str], domains: list, max_items: int = 100,
                 max_stories: int = 20000, database: Optional[str] = None) -> dict:
    """Phase 7: the customer's user stories whose name or aliases hold a Jira-style issue key (Knowledge Fabric names a story by its tool
    id when the source has one), with the requirements they implement, the systems they impact and the test cases covering those
    requirements. -> {issue key: [story]}."""
    from .kfbridge import issue_keys
    with driver.session(database=database) as s:
        rows = s.execute_read(lambda tx: [r.data() for r in tx.run(
            "MATCH (u:UserStory) WHERE u.customer_id = $c AND ($t IS NULL OR u.tenant_id = $t) AND u.domain IN $domains "
            "RETURN u.entity_id AS entity_id, u.name AS name, coalesce(u.aliases, []) AS aliases ORDER BY entity_id LIMIT $lim",
            c=customer_id, t=kf_tenant_id, domains=list(domains), lim=max_stories)])
        keyed = {}
        for r in rows:
            for k in issue_keys(" ".join([str(r["name"] or "")] + [str(a) for a in r["aliases"]])):
                keyed.setdefault(r["entity_id"], []).append(k)
        if not keyed:
            return {}
        found = s.execute_read(lambda tx: [r for r in tx.run(_STORY_Q, ids=sorted(keyed), tenant=tenant, customer=customer_id, cap=max_items,
                                                             cap2=max_items * 4)])
    out: dict = {}
    for r in found:
        story = {"entity_id": r["entity_id"], "name": r["name"], "domain": r["domain"],
                 "requirements": sorted((_item(x) for x in r["reqs"]), key=lambda x: x["name"] or ""),
                 "systems": sorted(({"name": dict(x).get("name"), "entity_id": dict(x).get("entity_id")} for x in r["systems"]),
                                   key=lambda x: x["name"] or ""),
                 "test_cases": _cover_cases(r["cover_rows"])}
        for k in keyed.get(r["entity_id"], []):
            out.setdefault(k, []).append(story)
    return {k: sorted(v, key=lambda x: (x["domain"] or "", x["entity_id"])) for k, v in sorted(out.items())}


BUSINESS_CYPHER = """
MATCH (c:TiaNode {tenant: $tenant}) WHERE c.id IN $changed
OPTIONAL MATCH p = (c)-[:IMPACTS*0..%d]->(e:TiaEndpoint)
WHERE all(n IN nodes(p) WHERE NOT n:TiaTest)
WITH collect(DISTINCT e.service) + collect(DISTINCT c.service) AS svcs
UNWIND svcs AS svc
WITH DISTINCT svc WHERE svc IS NOT NULL
MATCH (s:TiaService {tenant: $tenant, service: svc})-[b:REALIZES]->(sys:System)
WHERE b.bridge = true AND b.kf_customer_id = $customer AND sys.customer_id = $customer
OPTIONAL MATCH (r:Requirement)-[:IMPACTS]->(sys) WHERE r.customer_id = $customer
OPTIONAL MATCH (tc:TestCase)-[:COVERS]->(r) WHERE tc.customer_id = $customer
RETURN s.service AS service, sys.name AS system, sys.domain AS domain, b.status AS link,
       [x IN collect(DISTINCT r.name) WHERE x IS NOT NULL] AS requirements,
       [x IN collect(DISTINCT tc.name) WHERE x IS NOT NULL] AS business_test_cases
ORDER BY service, domain, system
"""


def business_regression(driver, tenant: str, customer_id: str, changed: list, max_depth: int = 8, database: Optional[str] = None) -> list:
    """Code change -> Knowledge Fabric test cases, in one Cypher query: the services the changed TIA nodes reach (through the impact walk to
    endpoints, and their own service), the Systems they realise, the requirements impacting those Systems and the test cases covering them."""
    q = BUSINESS_CYPHER % max(0, min(int(max_depth), 20))
    with driver.session(database=database) as s:
        return s.execute_read(lambda tx: [r.data() for r in tx.run(q, tenant=tenant, customer=customer_id, changed=list(changed))])

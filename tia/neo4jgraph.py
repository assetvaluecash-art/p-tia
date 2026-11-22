"""TIA's graph mirrored into Neo4j (docs/MEETING-PAWAN.md step 0; docs/NEO4J.md).

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

from . import airgap
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
    drv = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        drv.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError) as e:
        drv.close()
        raise Neo4jUnavailable(f"cannot reach Neo4j at {cfg['uri']}: {str(e)[:200]}")
    return drv


# ---- projection (pure: no server needed) -----------------------------------------------------------------------------------------------
def label_of(node_type: str) -> str:
    """TIA node type -> Neo4j label: endpoint -> TiaEndpoint, event_schema -> TiaEventSchema."""
    lab = "Tia" + "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", str(node_type or "unknown")) if p)
    return lab if NAME.match(lab) else "TiaUnknown"


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
                  include_inferred: bool = True, database: Optional[str] = None) -> dict:
    """Write the registry links of tia.kflinks.match_registry: (:TiaEndpoint)-[:<rel> {kf_link, source: 'api_registry', status, strategy,
    registry_row}]->(:<kf_label> {<kf_key>: key}). The tenant's earlier registry links that this result no longer has are removed, so the
    links always reflect the latest registry. -> {"linked", "removed", "not_linked"}."""
    _check_names(kf_label, kf_key, rel)
    want = ("confirmed", "inferred") if include_inferred else ("confirmed",)
    rows = [{"tkey": f"{tenant}|{r['endpoint']}", "kf": r["key"], "status": r["status"], "strategy": r["strategy"], "row": r["row"]}
            for r in matched["rows"] if r["status"] in want]
    q = (f"UNWIND $rows AS r OPTIONAL MATCH (t:TiaEndpoint {{key: r.tkey}}) OPTIONAL MATCH (k:{kf_label} {{{kf_key}: r.kf}}) "
         f"WITH r, t, collect(k) AS ks "
         f"FOREACH (k IN CASE WHEN t IS NULL OR size(ks) <> 1 THEN [] ELSE ks END | "
         f"  MERGE (t)-[x:{rel}]->(k) SET x.kf_link = true, x.tenant = $t, x.source = 'api_registry', x.status = r.status, "
         f"  x.strategy = r.strategy, x.registry_row = r.row) "
         f"RETURN r.row AS row, r.kf AS kf, t IS NOT NULL AS tia, size(ks) AS kf_nodes")
    q_clean = (f"MATCH (t:TiaEndpoint {{tenant: $t}})-[x:{rel} {{source: 'api_registry'}}]->(k:{kf_label}) "
               f"WHERE NOT [t.key, toString(k.{kf_key})] IN $keep DELETE x RETURN count(x) AS n")
    with driver.session(database=database) as s:
        res = s.execute_write(lambda tx: [r.data() for r in tx.run(q, rows=rows, t=tenant)])
        ok = [r for r in res if r["tia"] and r["kf_nodes"] == 1]
        keep = [[x["tkey"], str(x["kf"])] for x in rows if any(o["row"] == x["row"] for o in ok)]
        removed = s.execute_write(lambda tx: tx.run(q_clean, t=tenant, keep=keep).single()["n"])
    not_linked = [{"row": r["row"], "key": r["kf"],
                   "reason": "the TIA endpoint is not in the mirror (run the sync first)" if not r["tia"]
                   else f"no :{kf_label} node with {kf_key} = {r['kf']!r}" if r["kf_nodes"] == 0
                   else f"{r['kf_nodes']} :{kf_label} nodes have {kf_key} = {r['kf']!r}"} for r in res if not (r["tia"] and r["kf_nodes"] == 1)]
    return {"linked": len(ok), "removed": removed, "not_linked": not_linked}


def link_test_cases(driver, tenant: str, kf_label: str, kf_key: str, rel: str = "SAME_AS", database: Optional[str] = None) -> dict:
    """Join TIA's written test cases to Knowledge Fabric's test-case nodes on the test case id: (:TiaTestCase {case_id})-[:<rel>
    {kf_link, source: 'test_case_id', status: 'source_confirmed'}]->(:<kf_label> {<kf_key>: case_id}). Ids are compared exactly (after
    trimming). -> {"linked", "tia_without_kf", "kf_without_tia", "duplicate_kf_ids"}."""
    _check_names(kf_label, kf_key, rel)
    q_link = (f"MATCH (tc:TiaTestCase {{tenant: $t}}) WHERE tc.case_id IS NOT NULL "
              f"MATCH (k:{kf_label}) WHERE trim(toString(k.{kf_key})) = trim(tc.case_id) "
              f"MERGE (tc)-[x:{rel}]->(k) SET x.kf_link = true, x.tenant = $t, x.source = 'test_case_id', x.status = 'source_confirmed' "
              f"RETURN count(x) AS n")
    q_clean = (f"MATCH (tc:TiaTestCase {{tenant: $t}})-[x:{rel} {{source: 'test_case_id'}}]->(k:{kf_label}) "
               f"WHERE trim(toString(k.{kf_key})) <> trim(tc.case_id) DELETE x")
    q_tia = (f"MATCH (tc:TiaTestCase {{tenant: $t}}) WHERE NOT (tc)-[:{rel} {{source: 'test_case_id'}}]->(:{kf_label}) "
             f"RETURN tc.case_id AS id, tc.repo AS repo ORDER BY id")
    q_kf = (f"MATCH (k:{kf_label}) WHERE k.{kf_key} IS NOT NULL AND NOT (:TiaTestCase {{tenant: $t}})-[:{rel} {{source: 'test_case_id'}}]->(k) "
            f"RETURN toString(k.{kf_key}) AS id ORDER BY id LIMIT 1000")
    q_dup = (f"MATCH (k:{kf_label}) WHERE k.{kf_key} IS NOT NULL WITH trim(toString(k.{kf_key})) AS id, count(*) AS n WHERE n > 1 "
             f"RETURN id, n ORDER BY id")
    with driver.session(database=database) as s:
        s.execute_write(lambda tx: tx.run(q_clean, t=tenant).consume())
        n = s.execute_write(lambda tx: tx.run(q_link, t=tenant).single()["n"])
        tia_only = s.execute_read(lambda tx: [r.data() for r in tx.run(q_tia, t=tenant)])
        kf_only = s.execute_read(lambda tx: [r["id"] for r in tx.run(q_kf, t=tenant)])
        dups = s.execute_read(lambda tx: [r.data() for r in tx.run(q_dup)])
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

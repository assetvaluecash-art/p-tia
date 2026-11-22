#!/usr/bin/env python3
"""
Post-ingestion test harness for Neo4j and Weaviate.

Two modes:

1) Full test run (no --query): runs Neo4j + Weaviate validation and writes report.
   python scripts/run_post_ingestion_harness.py --domain corporate_commercial --output reports/post_ingestion_harness_results.json

2) Direct query (with --query): retrieves top-n chunks and paths for a query string, prints top 3 to screen.
   python scripts/run_post_ingestion_harness.py --domain corporate_commercial --query "string string" --items 5

Uses config from config/kbfabric.yaml and config/golden_ingestion_expectations.yaml.
Does NOT run ingestion; run this after scripts/run_ingestion_job.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent for imports
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BASE_DIR))


def load_expectations() -> dict:
    """Load golden_ingestion_expectations.yaml."""
    path = BASE_DIR / "config" / "golden_ingestion_expectations.yaml"
    if not path.exists():
        return {"domain": "corporate_commercial", "golden_files_ingested": [], "neo4j_node_types_to_check": [], "neo4j_relationship_types_to_check": []}
    import yaml
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_config() -> dict:
    """Load kbfabric.yaml (Neo4j, Weaviate)."""
    from kbfabric_ingest.ingest.config import get_app_config
    return get_app_config()


def get_weaviate_class(domain: str) -> str:
    """Resolve Weaviate class name for domain."""
    from kbfabric_ingest.ingest.config import get_store_name_for_domain
    return get_store_name_for_domain(domain)


# -----------------------------------------------------------------------------
# Neo4j checks
# -----------------------------------------------------------------------------

def run_neo4j_checks(domain: str, expectations: dict, results: dict) -> None:
    """Run Neo4j Cypher queries and populate results['neo4j']."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        results["neo4j"] = {"status": "skip", "error": "neo4j driver not installed"}
        return

    config = get_config()
    neo_cfg = config.get("neo4j", {})
    uri = neo_cfg.get("uri", "bolt://localhost:7687")
    user = neo_cfg.get("user", "neo4j")
    password = neo_cfg.get("password", "")

    results["neo4j"] = {
        "status": "ok",
        "uri": uri,
        "domain": domain,
        "documents": [],
        "blocks_by_source": {},
        "node_counts_by_type": {},
        "relationship_counts_by_type": {},
        "provenance_coverage": {},
        "errors": [],
    }
    out = results["neo4j"]

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
        return

    # Lean mode: skip Document/Block queries (they don't exist when graph.lean_mode=True)
    lean_mode = config.get("graph", {}).get("lean_mode", True)

    try:
        with driver.session() as session:
            if not lean_mode:
                # Documents (source_file list)
                r = session.run(
                    "MATCH (d:Document)-[:DOCUMENT_IN_DOMAIN]->(dom:DomainRoot {domain: $domain}) RETURN d.source_file AS src",
                    domain=domain,
                )
                docs = [rec["src"] for rec in r if rec.get("src")]
                out["documents"] = docs
                out["document_count"] = len(docs)

                # Blocks per source_file
                r = session.run(
                    """
                    MATCH (b:Block)-[:NODE_IN_DOMAIN]->(dom:DomainRoot {domain: $domain})
                    RETURN b.source_file AS src, count(*) AS c
                    """,
                    domain=domain,
                )
                for rec in r:
                    src = rec.get("src") or "unknown"
                    out["blocks_by_source"][src] = rec.get("c", 0)
            else:
                out["documents"] = []
                out["document_count"] = 0
                out["blocks_by_source"] = {}
                out["lean_mode_skip"] = "Document/Block queries skipped (graph.lean_mode=True)"

            # Node counts by label (exclude scaffolding)
            node_types = expectations.get("neo4j_node_types_to_check", [])
            for label in node_types:
                try:
                    r = session.run(
                        f"MATCH (n:{label})-[:NODE_IN_DOMAIN]->(dom:DomainRoot {{domain: $domain}}) RETURN count(n) AS c",
                        domain=domain,
                    )
                    rec = r.single()
                    out["node_counts_by_type"][label] = rec["c"] if rec else 0
                except Exception as e:
                    out["node_counts_by_type"][label] = None
                    out["errors"].append(f"{label}: {e}")

            # Relationship counts by type (skip scaffolding types - not present in lean mode)
            SCAFFOLDING_REL_TYPES = {"BLOCK_BELONGS_TO_DOCUMENT", "DOCUMENT_IN_DOMAIN", "BLOCK_MENTIONS_ENTITY", "DIAGRAM_CONTAINS_COMPONENT"}
            rel_types = expectations.get("neo4j_relationship_types_to_check", [])
            rel_types_to_query = [t for t in rel_types if t not in SCAFFOLDING_REL_TYPES]
            for rel_type in SCAFFOLDING_REL_TYPES & set(rel_types):
                out["relationship_counts_by_type"][rel_type] = 0  # Lean mode: scaffolding not present
            for rel_type in rel_types_to_query:
                try:
                    r = session.run(
                        f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS c",
                    )
                    rec = r.single()
                    out["relationship_counts_by_type"][rel_type] = rec["c"] if rec else 0
                except Exception as e:
                    out["relationship_counts_by_type"][rel_type] = None
                    out["errors"].append(f"rel {rel_type}: {e}")

            # Provenance: relationships with source_block_id (semantic rels only)
            try:
                semantic_rel_types = [
                    t for t in rel_types_to_query
                    if t not in ("NODE_IN_DOMAIN",)
                ]
                r = session.run(
                    """
                    MATCH (a)-[r]->(b)
                    WHERE type(r) IN $rel_types
                    WITH r, CASE WHEN r.source_block_id IS NOT NULL AND r.source_block_id <> '' THEN 1 ELSE 0 END AS with_prov
                    RETURN sum(with_prov) AS with_prov, count(*) AS total
                    """,
                    rel_types=semantic_rel_types,
                )
                rec = r.single()
                if rec and rec["total"] and rec["total"] > 0:
                    out["provenance_coverage"] = {
                        "with_source_block_id": rec["with_prov"],
                        "total_rels": rec["total"],
                        "pct": round(100.0 * rec["with_prov"] / rec["total"], 1),
                    }
                else:
                    out["provenance_coverage"] = {"with_source_block_id": 0, "total_rels": 0, "pct": 0}
            except Exception as e:
                out["provenance_coverage"] = {"error": str(e)}

            # Entities per source_file (sample)
            r = session.run(
                """
                MATCH (n)-[:NODE_IN_DOMAIN]->(dom:DomainRoot {domain: $domain})
                WHERE NOT n:Block AND NOT n:Document AND NOT n:DomainRoot
                AND (n.source_file IS NOT NULL AND n.source_file <> 'unknown')
                WITH n.source_file AS src, labels(n) AS lbl
                RETURN src, collect(DISTINCT lbl[0])[0..5] AS sample_labels, count(*) AS c
                ORDER BY c DESC
                LIMIT 20
                """,
                domain=domain,
            )
            out["entities_by_source_sample"] = [
                {"source_file": rec["src"], "sample_labels": rec["sample_labels"], "count": rec["c"]}
                for rec in r
            ]

    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
    finally:
        driver.close()


# -----------------------------------------------------------------------------
# Harness tests (derived checks for planning)
# -----------------------------------------------------------------------------

def run_harness_tests(domain: str, expectations: dict, results: dict) -> None:
    """Run derived harness tests; populate results with checks for planning."""
    # 1. Defect/Incident coverage (from Defect Log)
    if results.get("neo4j", {}).get("status") == "ok":
        n = results["neo4j"]
        results["harness_defect_incident"] = {
            "Defect_nodes": n.get("node_counts_by_type", {}).get("Defect", 0),
            "Incident_nodes": n.get("node_counts_by_type", {}).get("Incident", 0),
        }

    # 2. Weaviate vs Neo4j TestCase gap
    if results.get("weaviate", {}).get("status") == "ok" and results.get("neo4j", {}).get("status") == "ok":
        tc_chunks = results["weaviate"].get("chunks_by_block_category", {}).get("TestCase", 0)
        tc_nodes = results["neo4j"].get("node_counts_by_type", {}).get("TestCase", 0)
        results["harness_testcase_gap"] = {
            "weaviate_chunks": tc_chunks,
            "neo4j_nodes": tc_nodes,
            "gap": tc_chunks - tc_nodes,
            "warning": (tc_chunks - tc_nodes) > 5,
        }

    # 3. Semantic retrieval quality (queries with file hits)
    if results.get("weaviate", {}).get("status") == "ok":
        sq = results["weaviate"].get("semantic_queries", [])
        with_hits = sum(1 for s in sq if s.get("top_k_from_file", 0) > 0)
        results["harness_semantic_quality"] = {
            "queries_run": len(sq),
            "queries_with_file_hits": with_hits,
            "passed": with_hits >= 1,
        }

    # 4. Provenance coverage
    if results.get("neo4j", {}).get("status") == "ok":
        prov = results["neo4j"].get("provenance_coverage", {})
        pct = prov.get("pct", 0)
        results["harness_provenance"] = {
            "pct_with_provenance": pct,
            "passed": pct >= 50,
        }

    # 5. Golden file expectations summary
    if results.get("weaviate", {}).get("status") == "ok":
        fe = results["weaviate"].get("file_expectations", [])
        passed = sum(1 for e in fe if e.get("passed"))
        results["harness_golden_summary"] = {
            "total": len(fe),
            "passed": passed,
            "passed_pct": round(100.0 * passed / len(fe), 1) if fe else 0,
        }


# -----------------------------------------------------------------------------
# Weaviate checks
# -----------------------------------------------------------------------------

def run_weaviate_checks(domain: str, expectations: dict, results: dict) -> None:
    """Run Weaviate queries (count, by source_file, by block_category, nearText per file)."""
    try:
        import weaviate
    except ImportError:
        results["weaviate"] = {"status": "skip", "error": "weaviate-client not installed"}
        return

    config = get_config()
    w_cfg = config.get("weaviate", {})
    url = w_cfg.get("url", "http://localhost:8080")
    class_name = get_weaviate_class(domain)

    results["weaviate"] = {
        "status": "ok",
        "url": url,
        "class": class_name,
        "domain": domain,
        "total_chunks": 0,
        "chunks_by_source_file": {},
        "chunks_by_block_category": {},
        "file_expectations": [],
        "semantic_queries": [],
        "errors": [],
    }
    out = results["weaviate"]

    try:
        client = weaviate.Client(url)
    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))
        return

    try:
        if not client.schema.exists(class_name):
            out["status"] = "error"
            out["errors"].append(f"Class {class_name} does not exist")
            return

        # Total count
        agg = client.query.aggregate(class_name).with_meta_count().do()
        out["total_chunks"] = (
            agg.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
        )

        # Count by source_file (Weaviate doesn't have groupBy in all versions; fetch and aggregate in Python)
        fetch = (
            client.query.get(class_name, ["source_file", "block_category", "kg_node_type", "masked_text"])
            .with_limit(10000)
            .do()
        )
        objects = fetch.get("data", {}).get("Get", {}).get(class_name, [])
        by_source = {}
        by_category = {}
        for obj in objects:
            src = (obj.get("source_file") or "unknown").strip() or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
            cat = (obj.get("block_category") or "Unknown").strip() or "Unknown"
            by_category[cat] = by_category.get(cat, 0) + 1
        out["chunks_by_source_file"] = by_source
        out["chunks_by_block_category"] = by_category

        # File-specific expectations: match source_file substring and check min_chunks
        for spec in expectations.get("golden_files_ingested", []):
            key = spec.get("source_file_contains", spec.get("name", ""))
            count = sum(c for s, c in by_source.items() if key in s)
            out["file_expectations"].append({
                "file": spec.get("name", key),
                "source_file_contains": key,
                "min_chunks": spec.get("min_chunks", 1),
                "actual_chunks": count,
                "passed": count >= spec.get("min_chunks", 1),
            })

        # Semantic queries: class uses vectorizer "none", so we need nearVector with embedded query
        _embedding_fn = None
        try:
            from kbfabric_ingest._common.config import get_app_config
            from custom_ml.embeddings import EmbeddingsClient
            cfg = get_app_config()
            emb_client = EmbeddingsClient(cfg)
            emb_client.load_model()
            _embedding_fn = lambda t: emb_client.encode([t])[0] if emb_client else None
        except Exception:
            pass

        for spec in expectations.get("golden_files_ingested", []):
            questions = spec.get("weaviate_questions", [])
            key = spec.get("source_file_contains", spec.get("name", ""))
            for q in questions[:3]:  # max 3 per file
                try:
                    if _embedding_fn:
                        query_vector = _embedding_fn(q)
                        near = (
                            client.query.get(class_name, ["source_file", "block_category", "masked_text"])
                            .with_near_vector({"vector": query_vector})
                            .with_limit(5)
                            .with_additional(["distance"])
                            .do()
                        )
                    else:
                        # No embedding model: skip vector search, do a simple Get with limit
                        near = (
                            client.query.get(class_name, ["source_file", "block_category", "masked_text"])
                            .with_limit(5)
                            .do()
                        )
                        out["semantic_queries"].append({
                            "question": q[:80] + ("..." if len(q) > 80 else ""),
                            "file_hint": key,
                            "skipped": "embedding_model_not_available",
                        })
                        continue
                    objs = near.get("data", {}).get("Get", {}).get(class_name, [])
                    from_this_file = [o for o in objs if key in (o.get("source_file") or "")]
                    out["semantic_queries"].append({
                        "question": q[:80] + ("..." if len(q) > 80 else ""),
                        "file_hint": key,
                        "top_k_total": len(objs),
                        "top_k_from_file": len(from_this_file),
                        "preview_source": objs[0].get("source_file", "")[:60] if objs else "",
                    })
                except Exception as e:
                    out["semantic_queries"].append({
                        "question": q[:80],
                        "file_hint": key,
                        "error": str(e),
                    })

    except Exception as e:
        out["status"] = "error"
        out["errors"].append(str(e))


# -----------------------------------------------------------------------------
# Direct query mode (--query "..." --items n): Weaviate chunks + Neo4j paths, print top 3
# -----------------------------------------------------------------------------

# Core fields present on all KBChunk classes; optional vision fields may be omitted if missing
WEAVIATE_FIELDS = [
    "domain", "block_category", "kg_node_type", "masked_text", "source_file",
    "page_number", "block_index", "block_type", "metadata",
    "vision_description_masked", "vision_description_raw",
    "extracted_fields", "extracted_knowledge",
    "created_at", "updated_at",
]


def run_direct_query(domain: str, query: str, items: int) -> dict:
    """Run a single query against Weaviate (top-n chunks) and Neo4j (top-n paths). items = number of chunks and paths to fetch; top 3 are printed."""
    result = {"query": query, "domain": domain, "items": items, "weaviate_chunks": [], "neo4j_paths": [], "errors": []}

    # Weaviate: embed query, nearVector, get top items with full properties
    try:
        from kbfabric_ingest.ingest.config import get_app_config
        from custom_ml.embeddings import EmbeddingsClient
        import weaviate
        config = get_config()
        w_cfg = config.get("weaviate", {})
        client = weaviate.Client(w_cfg.get("url", "http://localhost:8080"))
        class_name = get_weaviate_class(domain)
        if not client.schema.exists(class_name):
            result["errors"].append(f"Weaviate class {class_name} does not exist")
            return result
        emb_client = EmbeddingsClient(get_app_config())
        emb_client.load_model()
        query_vector = emb_client.encode([query])[0]
        props = list(WEAVIATE_FIELDS)
        try:
            get_q = (
                client.query.get(class_name, props)
                .with_near_vector({"vector": query_vector})
                .with_limit(items)
                .with_additional(["distance"])
            )
            resp = get_q.do()
        except Exception as schema_err:
            # Fallback: request only core fields in case schema lacks vision/optional fields
            props = ["domain", "block_category", "kg_node_type", "masked_text", "source_file", "page_number", "block_index", "block_type", "metadata"]
            get_q = (
                client.query.get(class_name, props)
                .with_near_vector({"vector": query_vector})
                .with_limit(items)
                .with_additional(["distance"])
            )
            resp = get_q.do()
        objs = resp.get("data", {}).get("Get", {}).get(class_name, [])
        for i, o in enumerate(objs):
            chunk = {"rank": i + 1, "distance": o.get("_additional", {}).get("distance"), "properties": {}}
            for k in props:
                v = o.get(k)
                if v is not None:
                    chunk["properties"][k] = v
            result["weaviate_chunks"].append(chunk)
    except Exception as e:
        result["errors"].append(f"Weaviate: {e}")

    # Neo4j: semantic relationship paths with provenance (like retriever paths)
    try:
        from neo4j import GraphDatabase
        config = get_config()
        neo = config.get("neo4j", {})
        with GraphDatabase.driver(
            neo.get("uri", "bolt://localhost:7687"),
            auth=(neo.get("user", "neo4j"), neo.get("password", "")),
        ) as driver:
            with driver.session() as session:
                # Paths: (a)-[r]->(b) with entity names and provenance; limit by items
                cypher = """
                MATCH (a)-[r]->(b)
                WHERE a.domain = $domain AND b.domain = $domain
                  AND NOT a:Block AND NOT a:Document AND NOT a:DomainRoot
                  AND NOT b:Block AND NOT b:Document AND NOT b:DomainRoot
                  AND NOT type(r) IN ['NODE_IN_DOMAIN', 'DOCUMENT_IN_DOMAIN', 'BLOCK_BELONGS_TO_DOCUMENT']
                RETURN a, r, b
                LIMIT $limit
                """
                r = session.run(cypher, domain=domain, limit=items)
                for i, rec in enumerate(r):
                    a, rel, b = rec["a"], rec["r"], rec["b"]
                    a_labels = list(a.labels) if a else []
                    b_labels = list(b.labels) if b else []
                    rel_type = getattr(rel, "type", None) or (type(rel).__name__ if rel else "REL")
                    def node_props(n):
                        if not n:
                            return {"labels": [], "name": None, "name_display": None, "entity_id": None}
                        return {
                            "labels": list(n.labels),
                            "name": n.get("name") if hasattr(n, "get") else None,
                            "name_display": n.get("name_display") if hasattr(n, "get") else None,
                            "entity_id": n.get("entity_id") if hasattr(n, "get") else None,
                        }
                    path_entry = {
                        "rank": i + 1,
                        "from_node": node_props(a),
                        "relationship": rel_type,
                        "to_node": node_props(b),
                        "source_file": rel.get("source_file") if rel and hasattr(rel, "get") else None,
                        "source_block_id": rel.get("source_block_id") if rel and hasattr(rel, "get") else None,
                    }
                    path_entry["from_node"]["labels"] = a_labels
                    path_entry["to_node"]["labels"] = b_labels
                    result["neo4j_paths"].append(path_entry)
    except Exception as e:
        result["errors"].append(f"Neo4j: {e}")

    return result


def print_direct_query_results(data: dict, top_n: int = 3) -> None:
    """Print top_n Weaviate chunks and top_n Neo4j paths to screen in a clean format."""
    q = data.get("query", "")
    domain = data.get("domain", "")
    print()
    print("=" * 72)
    print("DIRECT QUERY RESULTS")
    print("=" * 72)
    print(f"  Query:  {q}")
    print(f"  Domain: {domain}")
    if data.get("errors"):
        for e in data["errors"]:
            print(f"  Error:  {e}")
    print()

    chunks = data.get("weaviate_chunks", [])[:top_n]
    print("-" * 72)
    print("WEAVIATE (top {} chunks)".format(len(chunks)))
    print("-" * 72)
    for i, c in enumerate(chunks, 1):
        props = c.get("properties", {})
        print()
        print("  --- Chunk #{} (distance: {}) ---".format(i, c.get("distance")))
        print("  source_file:    {}".format(props.get("source_file", "")))
        print("  block_category: {}".format(props.get("block_category", "")))
        print("  kg_node_type:   {}".format(props.get("kg_node_type", "")))
        print("  block_index:    {}".format(props.get("block_index", "")))
        print("  page_number:    {}".format(props.get("page_number", "")))
        text = (props.get("masked_text") or "")[:2000]
        if len(props.get("masked_text") or "") > 2000:
            text += " ... [truncated]"
        print("  masked_text:     {}".format(text))
        if props.get("vision_description_masked"):
            v = (props.get("vision_description_masked") or "")[:500]
            print("  vision_desc:    {}...".format(v[:500]))
        meta = props.get("metadata")
        if meta:
            print("  metadata:       {}".format(json.dumps(meta, default=str)[:400]))
        print()

    paths = data.get("neo4j_paths", [])[:top_n]
    print("-" * 72)
    print("NEO4J (top {} paths)".format(len(paths)))
    print("-" * 72)
    for i, p in enumerate(paths, 1):
        print()
        print("  --- Path #{} ---".format(i))
        fa = p.get("from_node", {})
        tb = p.get("to_node", {})
        print("  From:  {}  {}  (entity_id: {})".format(fa.get("labels"), fa.get("name_display") or fa.get("name"), fa.get("entity_id")))
        print("  Rel:   {}".format(p.get("relationship", "")))
        print("  To:    {}  {}  (entity_id: {})".format(tb.get("labels"), tb.get("name_display") or tb.get("name"), tb.get("entity_id")))
        print("  source_file: {}  source_block_id: {}".format(p.get("source_file"), p.get("source_block_id")))
        print()
    print("=" * 72)


# -----------------------------------------------------------------------------
# Report output
# -----------------------------------------------------------------------------

def write_report(results: dict, output_path: Path) -> None:
    """Write JSON results and a short Markdown summary."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    md_path = output_path.with_suffix(".md")
    lines = [
        "# Post-Ingestion Harness Report",
        f"Generated: {results.get('timestamp', '')}",
        f"Domain: {results.get('domain', '')}",
        "",
        "## Neo4j",
        f"- Status: {results.get('neo4j', {}).get('status', 'N/A')}",
        f"- Documents: {results.get('neo4j', {}).get('document_count', 0)}",
        f"- Blocks by source: {list(results.get('neo4j', {}).get('blocks_by_source', {}).keys())}",
        f"- Provenance coverage: {results.get('neo4j', {}).get('provenance_coverage', {})}",
        "",
        "## Weaviate",
        f"- Status: {results.get('weaviate', {}).get('status', 'N/A')}",
        f"- Total chunks: {results.get('weaviate', {}).get('total_chunks', 0)}",
        "",
        "### File expectations",
    ]
    for fe in results.get("weaviate", {}).get("file_expectations", []):
        lines.append(f"- {fe.get('file', '')}: {fe.get('actual_chunks', 0)} chunks (min {fe.get('min_chunks', 1)}) — {'PASS' if fe.get('passed') else 'FAIL'}")
    lines.append("")
    lines.append("### Semantic queries (sample)")
    for sq in results.get("weaviate", {}).get("semantic_queries", [])[:10]:
        if "error" in sq:
            lines.append(f"- Q: {sq.get('question', '')} — Error: {sq['error'][:60]}")
        else:
            lines.append(f"- Q: {sq.get('question', '')} — top_k_from_file: {sq.get('top_k_from_file', 0)}")
    lines.append("")
    lines.append("### Harness tests (for planning)")
    for key in ["harness_defect_incident", "harness_testcase_gap", "harness_semantic_quality", "harness_provenance", "harness_golden_summary"]:
        val = results.get(key)
        if val is not None:
            lines.append(f"- {key}: {val}")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Post-ingestion test harness (Neo4j + Weaviate)")
    parser.add_argument("--domain", default="corporate_commercial", help="Domain (e.g. corporate_commercial)")
    parser.add_argument("--query", type=str, default=None, help="Direct query string: run retrieval and print top 3 chunks + paths (no full test run)")
    parser.add_argument("--items", type=int, default=5, help="Number of chunks and paths to fetch when using --query (default 5); top 3 printed")
    parser.add_argument("--input-dir", type=str, help="Input dir used for ingestion (for reference only)")
    parser.add_argument("--output", type=str, default="reports/post_ingestion_harness_results.json", help="Output JSON path (full test run only)")
    parser.add_argument("--skip-weaviate", action="store_true", help="Skip Weaviate checks (full test run only)")
    parser.add_argument("--skip-neo4j", action="store_true", help="Skip Neo4j checks (full test run only)")
    args = parser.parse_args()

    # Direct query mode: run retrieval, print top 3, then exit (no full harness)
    if args.query:
        data = run_direct_query(args.domain, args.query.strip(), max(1, args.items))
        print_direct_query_results(data, top_n=3)
        return 0

    # Full test run: run all checks and write report
    expectations = load_expectations()
    if args.domain and expectations.get("domain") != args.domain:
        expectations["domain"] = args.domain

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": args.domain,
        "input_dir": args.input_dir,
        "expectations_file": "config/golden_ingestion_expectations.yaml",
    }

    if not args.skip_neo4j:
        run_neo4j_checks(args.domain, expectations, results)
    else:
        results["neo4j"] = {"status": "skip", "reason": "cli --skip-neo4j"}

    if not args.skip_weaviate:
        run_weaviate_checks(args.domain, expectations, results)
    else:
        results["weaviate"] = {"status": "skip", "reason": "cli --skip-weaviate"}

    run_harness_tests(args.domain, expectations, results)

    out_path = BASE_DIR / args.output
    write_report(results, out_path)
    print(f"Harness complete. Results: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

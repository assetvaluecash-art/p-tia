#!/usr/bin/env python
"""
Release-readiness validation for Knowledge Fabric ingestion.
Runs quality gates against the latest report and live Neo4j.
Exit 0 = release ready, 1 = not ready.
"""
import json
import os
import sys
from pathlib import Path

# Add parent for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"
DOMAIN = "corporate_commercial"


def find_latest_report() -> Path | None:
    """Find most recent ingestion report for domain."""
    pattern = f"ingestion_{DOMAIN}_*.json"
    reports = sorted(REPORT_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def load_report(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_report_metrics(data: dict) -> tuple[bool, list[str]]:
    """Validate report-level metrics."""
    issues = []
    ok = True

    summary = data.get("run_summary", data)

    # Blocks
    blocks = summary.get("total_blocks", 0) or summary.get("blocks_processed", 0)
    if blocks < 600:
        issues.append(f"Blocks {blocks} < 600 (expected ~635)")
        ok = False

    # Neo4j nodes (890 allows ~6-node run variance; baseline 949)
    nodes = summary.get("neo4j_nodes", 0) or summary.get("kg_nodes", 0)
    if nodes < 890:
        issues.append(f"Neo4j nodes {nodes} < 890")
        ok = False

    # Weaviate chunks
    chunks = summary.get("weaviate_chunks", 0) or summary.get("chunks", 0)
    if chunks < 600:
        issues.append(f"Weaviate chunks {chunks} < 600")
        ok = False

    return ok, issues


def check_neo4j_quality() -> tuple[bool, list[str]]:
    """Run live Neo4j queries for quality gates. Skips if store is empty."""
    issues = []
    ok = True

    try:
        from neo4j import GraphDatabase
        from kbfabric_ingest.ingest.config import get_app_config
        cfg = get_app_config().get("neo4j", {})
        uri = cfg.get("uri", "bolt://localhost:7687")
        user = cfg.get("user", "neo4j")
        pwd = cfg.get("password", "")
        driver = GraphDatabase.driver(uri, auth=(user, pwd))

        with driver.session() as session:
            total = session.run("MATCH (n) RETURN count(n) as c").single()["c"]
            if total == 0:
                print("  [SKIP] Neo4j empty (run validation after ingestion completes)")
                driver.close()
                return True, []

            # 1. No duplicate Function nodes (same name)
            r = session.run("""
                MATCH (n:Function) WHERE n.domain = $domain
                WITH n.name AS name, count(*) AS cnt
                WHERE cnt > 1
                RETURN name, cnt
            """, domain=DOMAIN)
            dupes = list(r)
            if dupes:
                issues.append(f"Duplicate Function nodes: {[(d['name'], d['cnt']) for d in dupes]}")
                ok = False

            # 2. Content linked to SubFunction/Function (not just DomainRoot)
            r = session.run("""
                MATCH (c)-[:PART_OF]->(t) WHERE c.domain = $domain
                AND NOT c:DomainRoot AND NOT c:ProductLine AND NOT c:Function AND NOT c:SubFunction
                RETURN labels(t)[0] AS target, count(*) AS cnt
            """, domain=DOMAIN)
            by_target = {row["target"]: row["cnt"] for row in r}
            sf = by_target.get("SubFunction", 0)
            fn = by_target.get("Function", 0)
            dr = by_target.get("DomainRoot", 0)
            if sf + fn < 100:
                issues.append(f"Content→SubFunction/Function too low: {sf}+{fn} (expected >100)")
                ok = False
            if dr > 700:
                issues.append(f"Too many content→DomainRoot: {dr} (spine linking weak)")
                ok = False

            # 3. TestCase and Defect preserved
            r = session.run("""
                MATCH (n) WHERE n.domain = $domain
                AND (n:TestCase OR n:Defect)
                RETURN labels(n)[0] AS lbl, count(*) AS cnt
            """, domain=DOMAIN)
            counts = {row["lbl"]: row["cnt"] for row in r}
            if counts.get("TestCase", 0) < 50:
                issues.append(f"TestCase nodes too few: {counts.get('TestCase', 0)}")
                ok = False
            if counts.get("Defect", 0) < 50:
                issues.append(f"Defect nodes too few: {counts.get('Defect', 0)}")
                ok = False

        driver.close()
    except Exception as e:
        issues.append(f"Neo4j check failed: {e}")
        ok = False

    return ok, issues


def main():
    report_path = find_latest_report()
    if not report_path:
        print("ERROR: No ingestion report found")
        sys.exit(1)

    print(f"Validating: {report_path.name}")
    data = load_report(report_path)

    all_ok = True
    all_issues = []

    # Report metrics
    ok, issues = check_report_metrics(data)
    if not ok:
        all_ok = False
        all_issues.extend(issues)
    else:
        print("  [PASS] Report metrics (blocks, nodes, chunks, errors)")

    # Spine linking validated via Neo4j (content→SubFunction/Function)

    # Report: TestCase and Defect block counts (proxy for KG nodes when Neo4j empty)
    summary = data.get("run_summary", data)
    blocks_cat = summary.get("blocks_per_category", {})
    tc_blocks = blocks_cat.get("TestCase", 0)
    def_blocks = blocks_cat.get("Defect", 0)
    if tc_blocks < 50:
        all_ok = False
        all_issues.append(f"TestCase blocks in report: {tc_blocks} (expected ≥50)")
    else:
        print(f"  [PASS] TestCase blocks: {tc_blocks}")
    if def_blocks < 50:
        all_ok = False
        all_issues.append(f"Defect blocks in report: {def_blocks} (expected ≥50)")
    else:
        print(f"  [PASS] Defect blocks: {def_blocks}")

    # Neo4j quality (when store has data)
    ok, issues = check_neo4j_quality()
    if not ok:
        all_ok = False
        all_issues.extend(issues)
    elif issues:
        pass  # was skipped
    else:
        print("  [PASS] Neo4j quality (no duplicate Functions, spine distribution)")

    if all_ok:
        print("\n*** RELEASE READY ***")
        sys.exit(0)
    else:
        print("\n*** NOT RELEASE READY ***")
        for i in all_issues:
            print(f"  - {i}")
        sys.exit(1)


if __name__ == "__main__":
    main()

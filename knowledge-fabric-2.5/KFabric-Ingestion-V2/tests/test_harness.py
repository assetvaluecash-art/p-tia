"""
Mini Test Harness — thin client over HybridRetriever.

Reads a test-case file, calls retriever.retrieve() for each test case in both
query-only and intent+query modes, and generates three HTML reports:
query-only, intent+query, and a side-by-side comparison.

Usage:
    python tests/test_harness.py --domain corporate_commercial
    python tests/test_harness.py --domain corporate_commercial --tests tests/smoke_tests.txt
    python tests/test_harness.py --domain corporate_commercial --tests tests/golden_tests.txt
"""

import argparse
import os
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kbfabric_ingest.retrieval.hybrid_retriever import HybridRetriever

# ---------------------------------------------------------------------------
# Test-case loading
# ---------------------------------------------------------------------------

def load_test_cases(harness_file: Path) -> list[dict]:
    cases = []
    for line in harness_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        use_case, query = line.split("|", 1)
        cases.append({"use_case": use_case.strip(), "query": query.strip()})
    return cases

# ---------------------------------------------------------------------------
# Run a full pass (query-only or intent)
# ---------------------------------------------------------------------------

def run_pass(
    retriever: HybridRetriever,
    test_cases: list[dict],
    mode: str,
    top_k_chunks: int,
    top_k_paths: int,
    domain: str = "corporate_commercial",
) -> list[dict]:
    """Execute all test cases and return per-case results."""
    intent_mode = mode == "intent"
    results = []

    for i, case in enumerate(test_cases):
        intent = case["use_case"] if intent_mode else "any"
        label = "intent" if intent_mode else "query"
        print(f"\n  [{label}] [{i+1}/{len(test_cases)}] {case['use_case']}: {case['query'][:60]}...")

        result = retriever.retrieve(
            intent=intent,
            query=case["query"],
            domain=domain,
            top_k_semantic=top_k_chunks,
            top_k_graph=top_k_paths,
        )

        results.append({
            "use_case": case["use_case"],
            "query": case["query"],
            "result": result,
        })

    return results

# ═══════════════════════════════════════════════════════════════════════════
# HTML report generation
# ═══════════════════════════════════════════════════════════════════════════

def generate_html_report(results: list[dict], mode: str, report_id: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode_label = "Intent + Query" if mode == "intent" else "Query Only"

    cases_html = []
    for idx, r in enumerate(results, 1):
        res = r["result"]
        meta = res["metadata"]
        intent = escape(r["use_case"])
        query = escape(r["query"])
        pattern_desc = escape(meta.get("pattern", r["use_case"]))

        input_box = f"""
        <div class="input-box">
            <div class="input-row"><span class="input-label">Intent:</span> {intent}</div>
            <div class="input-row"><span class="input-label">Query:</span> {query}</div>
            <div class="input-row"><span class="input-label">Pattern:</span> {pattern_desc}</div>
        </div>"""

        chunks = res.get("reranked_chunks") or res.get("chunks", [])
        paths = res.get("reranked_paths") or res.get("paths", [])
        chunks_html = _render_chunk_table(chunks)
        paths_html = _render_path_table(paths)

        depth_info = f' | Max depth: {meta.get("max_depth", "-")}' if mode == "intent" else ""
        metadata_line = (
            f'Latency: {meta["latency_ms"]:.0f} ms | '
            f'Chunks: {meta["chunks_considered"]} considered &rarr; {len(chunks)} returned | '
            f'Paths: {meta["paths_considered"]} considered &rarr; {len(paths)} returned'
            f'{depth_info}'
        )

        cases_html.append(f"""
        <div class="test-case">
            <div class="case-header">#{idx} &mdash; {intent}: {query}</div>
            <div class="section-title">Input</div>
            {input_box}
            <div class="section-title">Output</div>
            <h3>Top Chunks</h3>
            {chunks_html}
            <h3>Top Paths</h3>
            {paths_html}
            <div class="metadata-bar">{metadata_line}</div>
        </div>""")

    return _wrap_html(
        title=f"Test Harness Report &mdash; {mode_label}",
        subtitle=f"Generated: {ts} | Report ID: {report_id} | Cases: {len(results)}",
        body="".join(cases_html),
    )


def _render_chunk_table(chunks: list[dict]) -> str:
    if not chunks:
        return '<p class="empty">No chunks returned.</p>'
    rows = []
    for i, c in enumerate(chunks, 1):
        text = escape((c.get("text") or "")[:200])
        cat = escape(c.get("category") or c.get("block_category") or "")
        src = escape(c.get("source_file") or c.get("source") or "")
        page = c.get("page_number") if c.get("page_number") is not None else c.get("page")
        page_str = str(page) if page is not None else ""
        score = c.get("score", "")
        if isinstance(score, float):
            score = f"{score:.4f}"
        cls = "row-even" if i % 2 == 0 else "row-odd"
        rows.append(
            f'<tr class="{cls}">'
            f"<td>{i}</td>"
            f"<td>{text}</td>"
            f'<td><span class="cat">{cat}</span></td>'
            f"<td>{src}</td>"
            f"<td>{page_str}</td>"
            f'<td class="score">{score}</td>'
            f"</tr>"
        )
    return (
        '<table class="data-table">'
        "<tr><th>#</th><th>Text (first 200 chars)</th><th>Category</th>"
        "<th>Source File</th><th>Page</th><th>Score</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _render_path_table(paths: list[dict]) -> str:
    if not paths:
        return '<p class="empty">No paths returned.</p>'
    rows = []
    for i, p in enumerate(paths, 1):
        symbolic = escape(p.get("path_text") or p.get("symbolic") or p.get("chain_symbolic") or "")
        natural = escape(p.get("source_evidence") or p.get("natural") or p.get("chain_natural") or "")
        hops = p.get("hops", 1)
        score = p.get("score", "")
        if isinstance(score, float):
            score = f"{score:.4f}"
        src_file = escape(p.get("source_file") or "")
        cls = "row-even" if i % 2 == 0 else "row-odd"
        rows.append(
            f'<tr class="{cls}">'
            f"<td>{i}</td>"
            f'<td><code class="symbolic">{symbolic}</code></td>'
            f"<td>{natural}</td>"
            f"<td>{hops}</td>"
            f"<td>{src_file}</td>"
            f'<td class="score">{score}</td>'
            f"</tr>"
        )
    return (
        '<table class="data-table">'
        "<tr><th>#</th><th>Symbolic Path</th><th>Natural Language Path</th>"
        "<th>Hops</th><th>Source</th><th>Score</th></tr>"
        + "".join(rows)
        + "</table>"
    )

# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------

def generate_comparison_report(
    query_results: list[dict],
    intent_results: list[dict],
    report_id: str,
) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = _compute_summary(query_results, intent_results)
    summary_html = _render_summary(summary)

    case_sections = []
    for qr, ir in zip(query_results, intent_results):
        case_sections.append(_render_comparison_case(qr, ir))

    return _wrap_html(
        title="Test Harness Comparison — Query-Only vs Intent+Query",
        subtitle=f"Generated: {ts} | Report ID: {report_id} | Cases: {len(query_results)}",
        body=summary_html + "\n".join(case_sections),
        extra_css=_COMPARISON_CSS,
    )


def _compute_summary(qr: list[dict], ir: list[dict]) -> dict:
    q_ms = [r["result"]["metadata"]["latency_ms"] for r in qr]
    i_ms = [r["result"]["metadata"]["latency_ms"] for r in ir]

    total_q_chunks = sum(len(r["result"].get("reranked_chunks") or r["result"].get("chunks", [])) for r in qr)
    total_i_chunks = sum(len(r["result"].get("reranked_chunks") or r["result"].get("chunks", [])) for r in ir)
    total_q_paths = sum(len(r["result"].get("reranked_paths") or r["result"].get("paths", [])) for r in qr)
    total_i_paths = sum(len(r["result"].get("reranked_paths") or r["result"].get("paths", [])) for r in ir)

    multihop_count = sum(
        1 for r in ir for p in (r["result"].get("reranked_paths") or r["result"].get("paths", [])) if p.get("hops", 1) > 1
    )

    return {
        "cases": len(qr),
        "avg_q_ms": sum(q_ms) / max(len(q_ms), 1),
        "avg_i_ms": sum(i_ms) / max(len(i_ms), 1),
        "total_q_chunks": total_q_chunks,
        "total_i_chunks": total_i_chunks,
        "total_q_paths": total_q_paths,
        "total_i_paths": total_i_paths,
        "multihop_intent_paths": multihop_count,
    }


def _render_summary(s: dict) -> str:
    return f"""
    <div class="summary-box">
        <h2>Summary Statistics</h2>
        <table class="summary-table">
            <tr><th>Metric</th><th>Query-Only</th><th>Intent+Query</th><th>Delta</th></tr>
            <tr>
                <td>Avg latency</td>
                <td>{s['avg_q_ms']:.0f} ms</td>
                <td>{s['avg_i_ms']:.0f} ms</td>
                <td>{s['avg_i_ms'] - s['avg_q_ms']:+.0f} ms</td>
            </tr>
            <tr>
                <td>Total reranked chunks</td>
                <td>{s['total_q_chunks']}</td>
                <td>{s['total_i_chunks']}</td>
                <td>{s['total_i_chunks'] - s['total_q_chunks']:+d}</td>
            </tr>
            <tr>
                <td>Total reranked paths</td>
                <td>{s['total_q_paths']}</td>
                <td>{s['total_i_paths']}</td>
                <td>{s['total_i_paths'] - s['total_q_paths']:+d}</td>
            </tr>
            <tr>
                <td>Multi-hop paths (intent only)</td>
                <td>0</td>
                <td>{s['multihop_intent_paths']}</td>
                <td>+{s['multihop_intent_paths']}</td>
            </tr>
        </table>
    </div>
    """


def _render_comparison_case(qr: dict, ir: dict) -> str:
    use_case = escape(qr["use_case"])
    query = escape(qr["query"])
    q_meta = qr["result"]["metadata"]
    i_meta = ir["result"]["metadata"]

    q_chunks = qr["result"].get("reranked_chunks") or qr["result"].get("chunks", [])
    i_chunks = ir["result"].get("reranked_chunks") or ir["result"].get("chunks", [])
    q_paths = qr["result"].get("reranked_paths") or qr["result"].get("paths", [])
    i_paths = ir["result"].get("reranked_paths") or ir["result"].get("paths", [])

    chunk_rows = _build_chunk_diff_rows(q_chunks, i_chunks)
    path_rows = _build_path_diff_rows(q_paths, i_paths)

    multihop_rows = ""
    multihop_paths = [p for p in i_paths if p.get("hops", 1) > 1]
    if multihop_paths:
        mh_items = []
        for p in multihop_paths:
            sym = escape(p.get("path_text") or p.get("symbolic") or p.get("chain_symbolic") or "")
            nat = escape(p.get("source_evidence") or p.get("natural") or p.get("chain_natural") or "")
            mh_items.append(f"""<tr>
                <td><code>{sym}</code></td><td>{nat}</td>
                <td>{p.get('hops', '')}</td><td class="score">{p.get('score', '')}</td>
            </tr>""")
        multihop_rows = f"""
        <p class="section-label">Multi-hop paths (intent mode only)</p>
        <table><tr><th>Symbolic</th><th>Natural</th><th>Hops</th><th>Score</th></tr>
        {''.join(mh_items)}</table>"""

    return f"""
    <div class="use-case">
        <h2>{use_case}</h2>
        <p class="query"><strong>Query:</strong> {query}</p>
        <p class="stats">
            Query-only: {q_meta['latency_ms']:.0f} ms, {len(q_chunks)} chunks, {len(q_paths)} paths |
            Intent: {i_meta['latency_ms']:.0f} ms, {len(i_chunks)} chunks, {len(i_paths)} paths |
            Max depth: {i_meta.get('max_depth', '-')}
        </p>
        <h3>Chunk Comparison</h3>
        <table><tr><th>#</th><th>Category</th><th>Text (first 120)</th>
            <th>Q Score</th><th>I Score</th><th>Status</th></tr>
        {''.join(chunk_rows)}</table>
        <h3>Path Comparison</h3>
        <table><tr><th>Symbolic</th><th>Natural</th>
            <th>Q Score</th><th>I Score</th><th>Status</th></tr>
        {''.join(path_rows)}</table>
        {multihop_rows}
    </div>
    """


def _build_chunk_diff_rows(q_chunks: list[dict], i_chunks: list[dict]) -> list[str]:
    q_texts = {(c.get("text") or "")[:80]: c for c in q_chunks}
    i_texts = {(c.get("text") or "")[:80]: c for c in i_chunks}
    all_keys = list(dict.fromkeys(list(q_texts.keys()) + list(i_texts.keys())))

    rows: list[str] = []
    for idx, key in enumerate(all_keys, 1):
        qc = q_texts.get(key)
        ic = i_texts.get(key)
        chunk = qc or ic
        if not chunk:
            continue

        text = escape((chunk.get("text") or "")[:120])
        cat = escape(chunk.get("category") or chunk.get("block_category") or "")
        q_score = f"{qc['score']:.4f}" if qc else ""
        i_score = f"{ic['score']:.4f}" if ic else ""

        if qc and ic:
            status = '<span class="tag-shared">shared</span>'
        elif qc:
            status = '<span class="tag-q-only">query-only</span>'
        else:
            status = '<span class="tag-i-only">intent-only</span>'

        rows.append(f"""<tr>
            <td>{idx}</td><td><span class="cat">{cat}</span></td><td>{text}...</td>
            <td class="score">{q_score}</td><td class="score">{i_score}</td><td>{status}</td>
        </tr>""")
    return rows


def _build_path_diff_rows(q_paths: list[dict], i_paths: list[dict]) -> list[str]:
    q_map = {(p.get("path_text") or p.get("symbolic") or p.get("chain_symbolic") or ""): p for p in q_paths}
    i_map = {(p.get("path_text") or p.get("symbolic") or p.get("chain_symbolic") or ""): p for p in i_paths}
    all_syms = list(dict.fromkeys(list(q_map.keys()) + list(i_map.keys())))

    rows: list[str] = []
    for sym in all_syms:
        qp = q_map.get(sym)
        ip = i_map.get(sym)
        path = qp or ip
        if not path:
            continue

        symbolic = escape(sym)
        natural = escape(path.get("source_evidence") or path.get("natural") or path.get("chain_natural") or "")
        q_score = f"{qp['score']:.4f}" if qp else ""
        i_score = f"{ip['score']:.4f}" if ip else ""

        if qp and ip:
            status = '<span class="tag-shared">shared</span>'
        elif qp:
            status = '<span class="tag-q-only">query-only</span>'
        else:
            status = '<span class="tag-i-only">intent-only</span>'

        rows.append(f"""<tr>
            <td><code>{symbolic}</code></td><td>{natural}</td>
            <td class="score">{q_score}</td><td class="score">{i_score}</td><td>{status}</td>
        </tr>""")
    return rows

# ---------------------------------------------------------------------------
# Shared HTML wrapper + CSS
# ---------------------------------------------------------------------------

_COMPARISON_CSS = """
.summary-box { background: #e8eaf6; padding: 20px; border-radius: 8px; margin: 15px 0; }
.summary-table { max-width: 700px; }
.summary-table th { background: #c5cae9; }
.tag-shared { background: #c8e6c9; color: #2e7d32; padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }
.tag-q-only { background: #ffcdd2; color: #c62828; padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }
.tag-i-only { background: #bbdefb; color: #1565c0; padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }
"""


def _wrap_html(title: str, subtitle: str, body: str, extra_css: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: 'Segoe UI', sans-serif; margin: 30px auto; max-width: 1200px;
       background: #f5f5f5; color: #333; }}
h1 {{ color: #1a237e; border-bottom: 3px solid #1a237e; padding-bottom: 8px; }}
h3 {{ color: #37474f; margin: 14px 0 6px 0; font-size: 0.95em; }}
.meta {{ color: #666; font-size: 0.9em; margin-bottom: 20px; }}

/* --- test case card --- */
.test-case {{ background: #fff; border: 1px solid #ddd; border-radius: 8px;
              margin: 20px 0; box-shadow: 0 1px 4px rgba(0,0,0,0.07); overflow: hidden; }}
.case-header {{ background: #283593; color: #fff; padding: 10px 18px;
                font-size: 1.05em; font-weight: 600; }}
.section-title {{ font-size: 0.8em; font-weight: 700; text-transform: uppercase;
                  letter-spacing: 0.06em; color: #5c6bc0; margin: 14px 18px 4px 18px; }}

/* --- input info box --- */
.input-box {{ background: #f4f5f7; border: 1px solid #e0e0e0; border-radius: 6px;
              margin: 4px 18px 10px 18px; padding: 10px 14px; font-size: 0.88em; }}
.input-row {{ margin: 3px 0; }}
.input-label {{ font-weight: 600; color: #555; min-width: 70px; display: inline-block; }}

/* --- data tables --- */
.data-table {{ width: calc(100% - 36px); margin: 4px 18px 10px 18px;
               border-collapse: collapse; font-size: 0.82em; }}
.data-table th {{ background: #e8eaf6; padding: 7px 8px; text-align: left;
                  border-bottom: 2px solid #c5cae9; white-space: nowrap; }}
.data-table td {{ padding: 6px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
.row-odd {{ background: #fff; }}
.row-even {{ background: #fafafa; }}
.data-table tr:hover {{ background: #f0f4ff; }}
.score {{ font-weight: 600; color: #1b5e20; text-align: right; font-variant-numeric: tabular-nums; }}
.cat {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em;
        background: #e3f2fd; color: #1565c0; }}
code.symbolic {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px;
                 font-size: 0.88em; white-space: pre-wrap; word-break: break-all; }}

/* --- metadata bar --- */
.metadata-bar {{ background: #f9f9f9; border-top: 1px solid #eee;
                  padding: 8px 18px; font-size: 0.8em; color: #888; }}

/* --- comparison report (kept for backwards compat) --- */
.use-case {{ background: #fff; padding: 20px; margin: 15px 0; border-radius: 8px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.query {{ font-size: 1.05em; }}
.stats {{ font-size: 0.85em; color: #888; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.8em; margin: 8px 0; }}
th {{ background: #e8eaf6; padding: 6px; text-align: left; border-bottom: 2px solid #c5cae9; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #eee; vertical-align: top; }}
tr:hover {{ background: #f5f5f5; }}
code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }}
.section-label {{ font-weight: bold; font-size: 0.85em; color: #5c6bc0; margin: 10px 0 4px 0; }}
.empty {{ color: #aaa; font-style: italic; padding: 10px 18px; }}
{extra_css}
</style></head>
<body>
<h1>{title}</h1>
<p class="meta">{subtitle}</p>
{body}
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Mini Test Harness — Thin Client")
    parser.add_argument("--top-k-chunks", type=int, default=5)
    parser.add_argument("--top-k-paths", type=int, default=10)
    parser.add_argument("--domain", default="corporate_commercial")
    parser.add_argument("--tests", "-t", default="tests/harnesstests.txt",
                        help="Path to test-case file (default: tests/harnesstests.txt)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    harness_file = Path(args.tests)
    if not harness_file.exists():
        print(f"ERROR: {harness_file} not found")
        sys.exit(1)

    test_cases = load_test_cases(harness_file)
    print(f"Loaded {len(test_cases)} test cases from {harness_file}")

    print("Initializing HybridRetriever...")
    retriever = HybridRetriever(
        config_path=str(project_root / "config" / "kbfabric.yaml"),
    )

    # --- Pass 1: query-only ---
    print("\n" + "=" * 60)
    print("PASS 1 — Query-Only Mode")
    print("=" * 60)
    query_results = run_pass(retriever, test_cases, "query", args.top_k_chunks, args.top_k_paths, args.domain)

    # --- Pass 2: intent+query ---
    print("\n" + "=" * 60)
    print("PASS 2 — Intent+Query Mode")
    print("=" * 60)
    intent_results = run_pass(retriever, test_cases, "intent", args.top_k_chunks, args.top_k_paths, args.domain)

    retriever.close()

    # --- Write reports ---
    report_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = project_root / "reports"
    reports_dir.mkdir(exist_ok=True)

    report_q = reports_dir / f"test_harness-{report_ts}.html"
    report_i = reports_dir / f"test_harness_intent-{report_ts}.html"
    report_c = reports_dir / f"test_harness_comparison-{report_ts}.html"

    report_q.write_text(generate_html_report(query_results, "query", report_ts), encoding="utf-8")
    report_i.write_text(generate_html_report(intent_results, "intent", report_ts), encoding="utf-8")
    report_c.write_text(generate_comparison_report(query_results, intent_results, report_ts), encoding="utf-8")

    print(f"\nReports written to {reports_dir}/:")
    print(f"  {report_q.name}  (query-only)")
    print(f"  {report_i.name}  (intent+query)")
    print(f"  {report_c.name}  (comparison)")
    print(f"\nTotal cases: {len(test_cases)}")

    avg_q = sum(r["result"]["metadata"]["latency_ms"] for r in query_results) / len(query_results)
    avg_i = sum(r["result"]["metadata"]["latency_ms"] for r in intent_results) / len(intent_results)
    print(f"Avg latency:  query-only {avg_q:.0f} ms  |  intent {avg_i:.0f} ms")


if __name__ == "__main__":
    main()

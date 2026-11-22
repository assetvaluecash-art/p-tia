#!/usr/bin/env python
"""
Wealth Portfolio End-to-End Evaluation Runner

This script runs a comprehensive evaluation of the KnowledgeFabric RAG system
for the wealth_portfolio domain, including:
1. Database reset (Weaviate + Neo4j)
2. Document ingestion
3. Retrieval evaluation using QiBaPulse Evals
4. Detailed HTML report generation

Usage:
    # Run full evaluation (reset + ingest + test)
    python tests/run_wealth_portfolio_eval.py
    
    # Skip ingestion (use existing data)
    python tests/run_wealth_portfolio_eval.py --skip-ingest
    
    # Run specific test by index (0-9)
    python tests/run_wealth_portfolio_eval.py --test-index 0
    
    # Generate report only (from existing results)
    python tests/run_wealth_portfolio_eval.py --report-only

Output:
    - Reports saved to: reports/wealth_portfolio_eval_{timestamp}.html
    - JSON results saved to: reports/wealth_portfolio_eval_{timestamp}.json
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any

import yaml

# Add paths for imports
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BASE_DIR.parent
RETRIEVAL_DIR = PROJECT_ROOT / "kbfabric-retrieval"
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(RETRIEVAL_DIR))  # For qibapulse_evals

REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

GROUND_TRUTH_PATH = SCRIPT_DIR / "ground_truth" / "e2e_ground_truth.yaml"
SAMPLES_DIR = BASE_DIR / "samples" / "wealth_portfolio"


def log(msg: str, level: str = "INFO"):
    """Simple logging with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "[INFO]", "OK": "[OK]", "WARN": "[WARN]", "ERROR": "[ERROR]", "START": "[START]"}.get(level, "")
    print(f"[{timestamp}] {prefix} {msg}", flush=True)


def reset_databases(domain: str) -> dict[str, Any]:
    """Reset Weaviate and Neo4j for the specified domain."""
    log(f"Resetting databases for domain: {domain}", "START")
    
    from kbfabric_ingest.ingest.ingestion_service import IngestionService
    
    service = IngestionService()
    
    # Clear Weaviate
    log("Clearing Weaviate...", "INFO")
    weaviate_result = service.weaviate_writer.clear_domain(domain)
    log(f"Weaviate: {weaviate_result}", "OK")
    
    # Clear Neo4j
    log("Clearing Neo4j...", "INFO")
    neo4j_result = service.kg_writer.clear_domain(domain)
    log(f"Neo4j: {neo4j_result}", "OK")
    
    return {"weaviate": weaviate_result, "neo4j": neo4j_result}


def run_ingestion(domain: str, input_dir: Path) -> dict[str, Any]:
    """Run document ingestion for the specified domain."""
    log(f"Starting ingestion for domain: {domain}", "START")
    log(f"Input directory: {input_dir}", "INFO")
    
    from kbfabric_ingest.ingest.ingestion_service import IngestionService
    
    service = IngestionService()
    
    # Run ingestion
    result = service.run_ingestion(input_dir, domain)
    
    log(f"Ingestion complete: {result.get('total_files', 0)} files, "
        f"{result.get('weaviate_chunks', 0)} chunks", "OK")
    
    return result


def load_ground_truth(domain: str) -> list[dict]:
    """Load ground truth test cases for the domain."""
    log(f"Loading ground truth from: {GROUND_TRUTH_PATH}", "INFO")
    
    with open(GROUND_TRUTH_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    tests = data.get("ground_truth", {}).get(domain, [])
    log(f"Loaded {len(tests)} test cases for {domain}", "OK")
    
    return tests


def run_single_test(
    question: str,
    ideal_contexts: list[str],
    ideal_answer: str,
    test_type: str,
    test_index: int
) -> dict[str, Any]:
    """Run a single retrieval test with QiBaPulse Evals."""
    import httpx
    from qibapulse_evals import EvaluateAndCalculateScores
    from qibapulse_evals.models import GatewayRequest, GatewayResponse, RetrievedContext
    
    log(f"[Test {test_index}] Running: {question[:60]}...", "INFO")
    
    result = {
        "test_index": test_index,
        "question": question,
        "test_type": test_type,
        "ideal_answer": ideal_answer,
        "ideal_contexts": ideal_contexts,
        "retrieved_answer": None,
        "retrieved_chunks": [],
        "retrieved_paths": [],
        "retrieval_time_ms": 0,
        "eval_scores": None,
        "observations": [],
        "status": "pending",
        # Phase 0: Full RAG visibility
        "full_retriever_response": None,  # Complete JSON from retrieval API
        "llm_prompt": None,  # Prompt sent to LLM for answer generation
        "paths_with_evidence": []  # Paths including source evidence
    }
    
    try:
        # Call retrieval API
        start_time = time.time()
        
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                "http://localhost:8002/query",  # Retrieval API runs on port 8002
                json={
                    "query": question,
                    "domain_override": "wealth_portfolio",  # Correct parameter name
                    "top_k_semantic": 5,
                    "top_k_graph": 5,
                    "generate_answer": True  # Request LLM answer
                }
            )
            response.raise_for_status()
            api_result = response.json()
        
        retrieval_time = (time.time() - start_time) * 1000
        result["retrieval_time_ms"] = round(retrieval_time, 2)
        
        # Phase 0: Store full retriever response for debugging/audit
        result["full_retriever_response"] = api_result
        
        # Extract answer and context
        result["retrieved_answer"] = api_result.get("answer", "")
        result["retrieved_chunks"] = [c.get("text", "") for c in api_result.get("chunks", [])]
        result["retrieved_paths"] = [p.get("path_text", "") for p in api_result.get("paths", [])]  # Fixed key name
        
        # Phase 0: Extract paths with evidence for provenance visibility
        paths_with_evidence = []
        for p in api_result.get("paths", []):
            path_info = {
                "path_text": p.get("path_text", ""),
                "score": p.get("score", 0.0),
                "source_evidence": p.get("source_evidence"),
                "source_file": p.get("source_file"),
                "source_page": p.get("source_page"),
                "source_block_id": p.get("source_block_id")
            }
            paths_with_evidence.append(path_info)
        result["paths_with_evidence"] = paths_with_evidence
        
        # Run QiBaPulse Evals
        gateway_request = GatewayRequest(
            query=question,
            domain="wealth_portfolio",
            top_k_semantic=5,
            top_k_graph=5
        )
        
        gateway_response = GatewayResponse(
            answer=result["retrieved_answer"],
            context=RetrievedContext(
                chunks=result["retrieved_chunks"],
                paths=result["retrieved_paths"]
            ),
            model=api_result.get("model", "ollama")
        )
        
        # Evaluate with ground truth
        eval_result = EvaluateAndCalculateScores(
            gateway_request=gateway_request,
            gateway_response=gateway_response,
            ground_truth=ideal_answer
        )
        
        result["eval_scores"] = eval_result.to_dict()
        result["observations"] = [
            {"type": o.type, "text": o.text}
            for o in eval_result.observations
        ]
        result["status"] = "success"
        
        # Log scores
        promptfoo = result["eval_scores"]["promptfoo_scores"]
        ragas = result["eval_scores"]["ragas_scores"]
        log(f"[Test {test_index}] Promptfoo: {promptfoo.get('weighted_total', 0):.2f}, "
            f"RAGAS Faithfulness: {ragas.get('faithfulness', 0) or 0:.2f}", "OK")
        
    except httpx.ConnectError:
        result["status"] = "error"
        result["error"] = "Retrieval API not running. Start with: uvicorn kbfabric_retrieval.api:app --port 8002"
        log(f"[Test {test_index}] ERROR: Retrieval API connection failed", "ERROR")
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        log(f"[Test {test_index}] ERROR: {e}", "ERROR")
    
    return result


def run_all_tests(tests: list[dict], test_indices: list[int] | None = None) -> list[dict]:
    """Run all or selected tests."""
    results = []
    
    if test_indices:
        tests_to_run = [(i, tests[i]) for i in test_indices if i < len(tests)]
    else:
        tests_to_run = list(enumerate(tests))
    
    log(f"Running {len(tests_to_run)} tests...", "START")
    
    for idx, test in tests_to_run:
        result = run_single_test(
            question=test["question"],
            ideal_contexts=test.get("ideal_contexts", []),
            ideal_answer=test.get("ideal_answer", ""),
            test_type=test.get("test_type", "General"),
            test_index=idx
        )
        results.append(result)
        
        # Brief pause between tests
        time.sleep(1)
    
    return results


def calculate_summary(results: list[dict]) -> dict[str, Any]:
    """Calculate summary statistics from test results."""
    successful = [r for r in results if r["status"] == "success"]
    
    if not successful:
        return {
            "total_tests": len(results),
            "successful": 0,
            "failed": len(results),
            "avg_promptfoo": 0,
            "avg_faithfulness": 0,
            "avg_relevancy": 0,
            "avg_retrieval_time_ms": 0
        }
    
    promptfoo_scores = [
        r["eval_scores"]["promptfoo_scores"]["weighted_total"]
        for r in successful if r["eval_scores"]
    ]
    
    faithfulness_scores = [
        r["eval_scores"]["ragas_scores"]["faithfulness"]
        for r in successful 
        if r["eval_scores"] and r["eval_scores"]["ragas_scores"]["faithfulness"] is not None
    ]
    
    relevancy_scores = [
        r["eval_scores"]["ragas_scores"]["answer_relevancy"]
        for r in successful 
        if r["eval_scores"] and r["eval_scores"]["ragas_scores"]["answer_relevancy"] is not None
    ]
    
    retrieval_times = [r["retrieval_time_ms"] for r in successful]
    
    return {
        "total_tests": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "avg_promptfoo": sum(promptfoo_scores) / len(promptfoo_scores) if promptfoo_scores else 0,
        "avg_faithfulness": sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else 0,
        "avg_relevancy": sum(relevancy_scores) / len(relevancy_scores) if relevancy_scores else 0,
        "avg_retrieval_time_ms": sum(retrieval_times) / len(retrieval_times) if retrieval_times else 0,
        "min_promptfoo": min(promptfoo_scores) if promptfoo_scores else 0,
        "max_promptfoo": max(promptfoo_scores) if promptfoo_scores else 0
    }


def generate_action_plan(summary: dict, results: list[dict]) -> list[dict]:
    """Generate action plan based on test results."""
    actions = []
    
    # Overall quality assessment
    if summary["avg_promptfoo"] < 0.6:
        actions.append({
            "priority": 1,
            "category": "Critical",
            "title": "Overall RAG quality below threshold",
            "description": f"Average Promptfoo score ({summary['avg_promptfoo']:.2f}) is below 0.6 threshold.",
            "recommendation": "Review retrieval parameters, consider increasing top_k or adjusting reranking."
        })
    
    if summary["avg_faithfulness"] < 0.7:
        actions.append({
            "priority": 1,
            "category": "Faithfulness",
            "title": "Low faithfulness scores detected",
            "description": "Answers may contain unsupported claims not grounded in context.",
            "recommendation": "Review LLM prompts to enforce source attribution, consider stricter grounding."
        })
    
    # Identify worst performing tests
    successful = [r for r in results if r["status"] == "success" and r["eval_scores"]]
    if successful:
        worst_tests = sorted(
            successful,
            key=lambda x: x["eval_scores"]["promptfoo_scores"]["weighted_total"]
        )[:3]
        
        for test in worst_tests:
            score = test["eval_scores"]["promptfoo_scores"]["weighted_total"]
            if score < 0.6:
                actions.append({
                    "priority": 2,
                    "category": "Test-Specific",
                    "title": f"Low score on '{test['test_type']}' test",
                    "description": f"Test {test['test_index']} scored {score:.2f}",
                    "recommendation": f"Review ingestion quality for: {test['question'][:80]}..."
                })
    
    # Retrieval performance
    if summary["avg_retrieval_time_ms"] > 5000:
        actions.append({
            "priority": 2,
            "category": "Performance",
            "title": "Slow retrieval times",
            "description": f"Average retrieval time ({summary['avg_retrieval_time_ms']:.0f}ms) exceeds 5s threshold.",
            "recommendation": "Consider index optimization, caching, or reducing top_k values."
        })
    
    # Check for errors
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        actions.append({
            "priority": 1,
            "category": "Infrastructure",
            "title": f"{len(errors)} tests failed with errors",
            "description": "Tests failed due to infrastructure or API issues.",
            "recommendation": "Check API connectivity, database status, and error logs."
        })
    
    # Sort by priority
    actions.sort(key=lambda x: x["priority"])
    
    return actions


def generate_html_report(
    results: list[dict],
    summary: dict,
    action_plan: list[dict],
    metadata: dict
) -> str:
    """Generate detailed HTML report."""
    
    # Status color mapping
    def status_color(status: str) -> str:
        return {"success": "#28a745", "error": "#dc3545", "pending": "#6c757d"}.get(status, "#6c757d")
    
    def score_color(score: float) -> str:
        if score >= 0.8:
            return "#28a745"
        elif score >= 0.6:
            return "#ffc107"
        else:
            return "#dc3545"
    
    # Build test result cards
    test_cards = ""
    for r in results:
        status_badge = f'<span style="background:{status_color(r["status"])};color:white;padding:4px 8px;border-radius:4px;font-size:12px">{r["status"].upper()}</span>'
        
        if r["status"] == "success" and r["eval_scores"]:
            promptfoo = r["eval_scores"]["promptfoo_scores"]
            ragas = r["eval_scores"]["ragas_scores"]
            
            scores_html = f"""
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:10px 0">
                <div style="text-align:center;padding:10px;background:#f8f9fa;border-radius:5px">
                    <div style="font-size:24px;font-weight:bold;color:{score_color(promptfoo.get('weighted_total', 0))}">{promptfoo.get('weighted_total', 0):.2f}</div>
                    <div style="font-size:11px;color:#666">Promptfoo</div>
                </div>
                <div style="text-align:center;padding:10px;background:#f8f9fa;border-radius:5px">
                    <div style="font-size:24px;font-weight:bold;color:{score_color(ragas.get('faithfulness') or 0)}">{(ragas.get('faithfulness') or 0):.2f}</div>
                    <div style="font-size:11px;color:#666">Faithfulness</div>
                </div>
                <div style="text-align:center;padding:10px;background:#f8f9fa;border-radius:5px">
                    <div style="font-size:24px;font-weight:bold;color:{score_color(ragas.get('answer_relevancy') or 0)}">{(ragas.get('answer_relevancy') or 0):.2f}</div>
                    <div style="font-size:11px;color:#666">Relevancy</div>
                </div>
                <div style="text-align:center;padding:10px;background:#f8f9fa;border-radius:5px">
                    <div style="font-size:24px;font-weight:bold">{r['retrieval_time_ms']:.0f}ms</div>
                    <div style="font-size:11px;color:#666">Latency</div>
                </div>
            </div>
            """
            
            # Observations
            obs_html = ""
            for obs in r.get("observations", []):
                obs_icon = {"strength": "💪", "warning": "⚠️", "note": "📝", "tip": "💡"}.get(obs["type"], "•")
                obs_html += f'<div style="padding:5px;margin:2px 0;background:#f8f9fa;border-radius:3px;font-size:13px">{obs_icon} {obs["text"]}</div>'
            
            # Build paths with evidence HTML
            paths_evidence_html = ""
            paths_with_evidence = r.get('paths_with_evidence', [])
            for pe in paths_with_evidence[:5]:
                evidence = pe.get('source_evidence', '')
                evidence_preview = evidence[:200] + "..." if evidence and len(evidence) > 200 else (evidence or 'N/A')
                source_info = f"📄 {pe.get('source_file', 'N/A')} | Page: {pe.get('source_page', 'N/A')}" if pe.get('source_file') else ''
                paths_evidence_html += f'''
                <div style="margin:5px 0;padding:8px;background:#f0f0f0;border-radius:4px;border-left:3px solid #a371f7">
                    <div style="font-weight:bold;font-size:12px">{pe.get('path_text', '')}</div>
                    <div style="font-size:11px;color:#666;margin-top:4px">Score: {pe.get('score', 0):.3f}</div>
                    {f'<div style="font-size:11px;font-style:italic;margin-top:4px">📝 Evidence: "{evidence_preview}"</div>' if evidence else ''}
                    {f'<div style="font-size:10px;color:#888;margin-top:2px">{source_info}</div>' if source_info else ''}
                </div>
                '''
            
            # Answer comparison with full RAG visibility
            answer_html = f"""
            <details style="margin:10px 0">
                <summary style="cursor:pointer;font-weight:bold">View Answer & Context</summary>
                <div style="margin:10px 0;padding:10px;background:#e8f4fd;border-radius:5px">
                    <strong>Retrieved Answer:</strong>
                    <p style="margin:5px 0;font-size:13px">{r['retrieved_answer'][:500]}{'...' if len(r['retrieved_answer']) > 500 else ''}</p>
                </div>
                <div style="margin:10px 0;padding:10px;background:#e8fde8;border-radius:5px">
                    <strong>Ideal Answer:</strong>
                    <p style="margin:5px 0;font-size:13px">{r['ideal_answer'][:500]}{'...' if len(r['ideal_answer']) > 500 else ''}</p>
                </div>
                <div style="margin:10px 0;padding:10px;background:#f8f9fa;border-radius:5px">
                    <strong>Retrieved Chunks ({len(r['retrieved_chunks'])}):</strong>
                    <ul style="margin:5px 0;font-size:12px;max-height:200px;overflow-y:auto">
                        {''.join(f'<li style="margin:3px 0">{c[:150]}...</li>' for c in r['retrieved_chunks'][:3])}
                    </ul>
                </div>
                <div style="margin:10px 0;padding:10px;background:#f3e8ff;border-radius:5px">
                    <strong>Graph Paths with Evidence ({len(paths_with_evidence)}):</strong>
                    <div style="max-height:300px;overflow-y:auto">{paths_evidence_html if paths_evidence_html else '<p style="font-size:12px;color:#666">No paths retrieved</p>'}</div>
                </div>
            </details>
            <details style="margin:10px 0">
                <summary style="cursor:pointer;font-weight:bold;color:#58a6ff">🔍 View Full Retriever Response (JSON)</summary>
                <div style="margin:10px 0;padding:10px;background:#1a1a2e;color:#c9d1d9;border-radius:5px;max-height:400px;overflow-y:auto">
                    <pre style="font-size:11px;white-space:pre-wrap;word-break:break-word">{html_escape(str(r.get('full_retriever_response', {})))}</pre>
                </div>
            </details>
            """
        else:
            scores_html = f'<div style="padding:10px;background:#ffebee;border-radius:5px;color:#c62828">{r.get("error", "Unknown error")}</div>'
            obs_html = ""
            answer_html = ""
        
        test_cards += f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:15px;margin:15px 0;background:white">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                <span style="background:#6c757d;color:white;padding:4px 8px;border-radius:4px;font-size:11px">{r['test_type']}</span>
                {status_badge}
            </div>
            <h4 style="margin:0 0 10px 0;color:#1a1a2e">Test {r['test_index']}: {r['question'][:80]}{'...' if len(r['question']) > 80 else ''}</h4>
            {scores_html}
            {obs_html}
            {answer_html}
        </div>
        """
    
    # Build action plan section
    action_html = ""
    for action in action_plan:
        priority_color = {1: "#dc3545", 2: "#ffc107", 3: "#17a2b8"}.get(action["priority"], "#6c757d")
        action_html += f"""
        <div style="border-left:4px solid {priority_color};padding:10px 15px;margin:10px 0;background:#f8f9fa;border-radius:0 5px 5px 0">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <strong>{action['title']}</strong>
                <span style="background:{priority_color};color:white;padding:2px 8px;border-radius:10px;font-size:11px">P{action['priority']}</span>
            </div>
            <p style="margin:5px 0;font-size:13px;color:#666">{action['description']}</p>
            <p style="margin:5px 0;font-size:13px"><strong>Recommendation:</strong> {action['recommendation']}</p>
        </div>
        """
    
    # Overall status
    overall_color = "#28a745" if summary["avg_promptfoo"] >= 0.7 else "#ffc107" if summary["avg_promptfoo"] >= 0.5 else "#dc3545"
    overall_status = "PASSED" if summary["avg_promptfoo"] >= 0.7 else "WARNING" if summary["avg_promptfoo"] >= 0.5 else "NEEDS ATTENTION"
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wealth Portfolio RAG Evaluation Report</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f0f2f5;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 20px;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .summary-card .value {{ font-size: 32px; font-weight: bold; }}
        .summary-card .label {{ font-size: 12px; color: #666; }}
        .section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        .section h2 {{ border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 15px; }}
        @media print {{
            body {{ background: white; }}
            .section {{ box-shadow: none; border: 1px solid #eee; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Wealth Portfolio RAG Evaluation Report</h1>
            <p style="opacity:0.8;margin-top:10px">
                Generated: {metadata['generated_at']} | 
                Domain: {metadata['domain']} | 
                Tests: {summary['total_tests']}
            </p>
        </div>
        
        <div class="summary-grid">
            <div class="summary-card">
                <div class="value" style="color:{overall_color}">{overall_status}</div>
                <div class="label">Overall Status</div>
            </div>
            <div class="summary-card">
                <div class="value" style="color:{score_color(summary['avg_promptfoo'])}">{summary['avg_promptfoo']:.2f}</div>
                <div class="label">Avg Promptfoo Score</div>
            </div>
            <div class="summary-card">
                <div class="value" style="color:{score_color(summary['avg_faithfulness'])}">{summary['avg_faithfulness']:.2f}</div>
                <div class="label">Avg Faithfulness</div>
            </div>
            <div class="summary-card">
                <div class="value" style="color:{score_color(summary['avg_relevancy'])}">{summary['avg_relevancy']:.2f}</div>
                <div class="label">Avg Relevancy</div>
            </div>
            <div class="summary-card">
                <div class="value">{summary['avg_retrieval_time_ms']:.0f}ms</div>
                <div class="label">Avg Latency</div>
            </div>
            <div class="summary-card">
                <div class="value" style="color:#28a745">{summary['successful']}/{summary['total_tests']}</div>
                <div class="label">Tests Passed</div>
            </div>
        </div>
        
        <div class="section">
            <h2>📋 Executive Summary</h2>
            <p>This report evaluates the KnowledgeFabric RAG system against {summary['total_tests']} ground-truth test cases 
            for the <strong>wealth_portfolio</strong> domain. Tests cover Change Impact, Customer Journey, Negative Testing, 
            Control Verification, Diagram Understanding, Glossary Lookup, Incident Analysis, Test Coverage Mapping, 
            PRD Requirement Validation, and State Machine Testing.</p>
            
            <h3 style="margin-top:20px">Key Findings</h3>
            <ul style="margin:10px 0;padding-left:20px">
                <li><strong>Overall Quality:</strong> Average Promptfoo score of {summary['avg_promptfoo']:.2f} 
                    ({'>= 0.7 is good' if summary['avg_promptfoo'] >= 0.7 else 'needs improvement'})</li>
                <li><strong>Faithfulness:</strong> {summary['avg_faithfulness']:.2f} - 
                    {'Answers are well-grounded in context' if summary['avg_faithfulness'] >= 0.7 else 'Some answers may contain unsupported claims'}</li>
                <li><strong>Performance:</strong> Average retrieval latency of {summary['avg_retrieval_time_ms']:.0f}ms</li>
                <li><strong>Coverage:</strong> {summary['successful']} of {summary['total_tests']} tests completed successfully</li>
            </ul>
        </div>
        
        <div class="section">
            <h2>🎯 Action Plan</h2>
            {action_html if action_html else '<p style="color:#28a745">✅ No critical issues identified. System is performing well.</p>'}
        </div>
        
        <div class="section">
            <h2>📝 Detailed Test Results</h2>
            {test_cards}
        </div>
        
        <div class="section">
            <h2>🔧 Additional Tests Available</h2>
            <table style="width:100%;border-collapse:collapse">
                <tr style="background:#f8f9fa">
                    <th style="padding:10px;text-align:left;border-bottom:2px solid #ddd">Test Type</th>
                    <th style="padding:10px;text-align:left;border-bottom:2px solid #ddd">Description</th>
                    <th style="padding:10px;text-align:left;border-bottom:2px solid #ddd">Priority</th>
                </tr>
                <tr>
                    <td style="padding:10px;border-bottom:1px solid #eee">DAST - API Security</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Dynamic security testing of retrieval API endpoints</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">High</td>
                </tr>
                <tr>
                    <td style="padding:10px;border-bottom:1px solid #eee">Load Testing</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Concurrent query performance under load</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Medium</td>
                </tr>
                <tr>
                    <td style="padding:10px;border-bottom:1px solid #eee">Cross-Domain</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Test retrieval isolation between domains</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Medium</td>
                </tr>
                <tr>
                    <td style="padding:10px;border-bottom:1px solid #eee">Adversarial Queries</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">Test robustness against prompt injection</td>
                    <td style="padding:10px;border-bottom:1px solid #eee">High</td>
                </tr>
            </table>
        </div>
        
        <div style="text-align:center;color:#666;font-size:12px;margin-top:30px">
            <p>Generated by KnowledgeFabric QiBaPulse Evals | {metadata['generated_at']}</p>
        </div>
    </div>
</body>
</html>
    """
    
    return html


def main():
    parser = argparse.ArgumentParser(
        description="Wealth Portfolio End-to-End Evaluation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Full evaluation (reset + ingest + test all 10 cases)
    python tests/run_wealth_portfolio_eval.py
    
    # Skip ingestion (use existing data)
    python tests/run_wealth_portfolio_eval.py --skip-ingest
    
    # Run specific test by index
    python tests/run_wealth_portfolio_eval.py --test-index 0
    
    # Run multiple specific tests
    python tests/run_wealth_portfolio_eval.py --test-indices 0 2 5
    
    # Skip database reset
    python tests/run_wealth_portfolio_eval.py --skip-reset
        """
    )
    parser.add_argument("--skip-reset", action="store_true", help="Skip database reset")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip ingestion (use existing data)")
    parser.add_argument("--test-index", type=int, help="Run single test by index (0-9)")
    parser.add_argument("--test-indices", type=int, nargs="+", help="Run multiple tests by indices")
    parser.add_argument("--report-only", action="store_true", help="Generate report from latest results")
    parser.add_argument("--output-dir", type=Path, default=REPORTS_DIR, help="Output directory for reports")
    
    args = parser.parse_args()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    domain = "wealth_portfolio"
    
    print("\n" + "="*70)
    print("  QiBa/Pulse Knowledge Fabric - Wealth Portfolio Evaluation")
    print("="*70 + "\n")
    
    try:
        # Step 1: Reset databases
        if not args.skip_reset and not args.skip_ingest and not args.report_only:
            reset_databases(domain)
        else:
            log("Skipping database reset", "INFO")
        
        # Step 2: Run ingestion
        if not args.skip_ingest and not args.report_only:
            run_ingestion(domain, SAMPLES_DIR)
        else:
            log("Skipping ingestion", "INFO")
        
        # Step 3: Load ground truth
        tests = load_ground_truth(domain)
        
        # Step 4: Determine which tests to run
        if args.test_index is not None:
            test_indices = [args.test_index]
        elif args.test_indices:
            test_indices = args.test_indices
        else:
            test_indices = None  # Run all
        
        # Step 5: Run tests
        log("Starting evaluation tests...", "START")
        results = run_all_tests(tests, test_indices)
        
        # Step 6: Calculate summary
        summary = calculate_summary(results)
        
        # Step 7: Generate action plan
        action_plan = generate_action_plan(summary, results)
        
        # Step 8: Generate reports
        metadata = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "domain": domain,
            "total_tests": len(tests),
            "tests_run": len(results)
        }
        
        # Save JSON results
        json_path = args.output_dir / f"wealth_portfolio_eval_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "metadata": metadata,
                "summary": summary,
                "action_plan": action_plan,
                "results": results
            }, f, indent=2, default=str)
        log(f"JSON results saved: {json_path}", "OK")
        
        # Save HTML report
        html_report = generate_html_report(results, summary, action_plan, metadata)
        html_path = args.output_dir / f"wealth_portfolio_eval_{timestamp}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_report)
        log(f"HTML report saved: {html_path}", "OK")
        
        # Print summary
        print("\n" + "="*70)
        print("  EVALUATION SUMMARY")
        print("="*70)
        print(f"  Tests Run:      {summary['total_tests']}")
        print(f"  Successful:     {summary['successful']}")
        print(f"  Failed:         {summary['failed']}")
        print(f"  Avg Promptfoo:  {summary['avg_promptfoo']:.2f}")
        print(f"  Avg Faithful:   {summary['avg_faithfulness']:.2f}")
        print(f"  Avg Relevancy:  {summary['avg_relevancy']:.2f}")
        print(f"  Avg Latency:    {summary['avg_retrieval_time_ms']:.0f}ms")
        print("="*70)
        print(f"\n  Report: {html_path}")
        print(f"  Data:   {json_path}\n")
        
        return 0
        
    except Exception as e:
        log(f"Fatal error: {e}", "ERROR")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())


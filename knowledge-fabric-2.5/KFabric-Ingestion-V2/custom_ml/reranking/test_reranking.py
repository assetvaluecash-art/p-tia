# -*- coding: utf-8 -*-
"""
Test script for CrossEncoder reranking with HTML report generation.

Usage:
    python -m custom_ml.reranking.test_reranking
    python -m custom_ml.reranking.test_reranking --output report.html
"""

import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from .reranker_client import RerankerClient


def generate_html_report(
    query: str,
    chunks: List[Tuple[int, str, float]],
    paths: List[Tuple[int, str, float]],
    stats: dict,
    output_path: Path
) -> None:
    """Generate an HTML report of the reranking results."""
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CrossEncoder Reranking Test Report</title>
    <style>
        :root {{
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --accent: #58a6ff;
            --success: #3fb950;
            --warning: #d29922;
            --border: #30363d;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 2rem;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        h1 {{
            color: var(--accent);
            border-bottom: 2px solid var(--accent);
            padding-bottom: 0.5rem;
            margin-bottom: 1.5rem;
        }}
        h2 {{
            color: var(--text-primary);
            margin: 1.5rem 0 1rem 0;
            padding-bottom: 0.3rem;
            border-bottom: 1px solid var(--border);
        }}
        .query-box {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            margin: 1rem 0;
        }}
        .query-box strong {{
            color: var(--accent);
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin: 1rem 0;
        }}
        .stat-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            text-align: center;
        }}
        .stat-value {{
            font-size: 1.5rem;
            font-weight: bold;
            color: var(--success);
        }}
        .stat-label {{
            color: var(--text-secondary);
            font-size: 0.875rem;
        }}
        .result-list {{
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }}
        .result-item {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            display: flex;
            align-items: flex-start;
            gap: 1rem;
        }}
        .rank {{
            background: var(--accent);
            color: var(--bg-primary);
            font-weight: bold;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            min-width: 2rem;
            text-align: center;
        }}
        .score {{
            color: var(--success);
            font-weight: bold;
            min-width: 4rem;
        }}
        .content {{
            flex: 1;
            word-break: break-word;
        }}
        .original-index {{
            color: var(--text-secondary);
            font-size: 0.75rem;
        }}
        .section {{
            margin: 2rem 0;
        }}
        .timestamp {{
            color: var(--text-secondary);
            font-size: 0.875rem;
            text-align: right;
            margin-top: 2rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>CrossEncoder Reranking Test Report</h1>
        
        <div class="query-box">
            <strong>Query:</strong> {query}
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">{stats.get('load_time_sec', 0):.2f}s</div>
                <div class="stat-label">Model Load Time</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(chunks)}</div>
                <div class="stat-label">Chunks Reranked</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(paths)}</div>
                <div class="stat-label">Paths Reranked</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{'OK' if stats.get('ready') else 'FAIL'}</div>
                <div class="stat-label">Model Status</div>
            </div>
        </div>
        
        <div class="section">
            <h2>Reranked Chunks (Semantic Content)</h2>
            <div class="result-list">
"""
    
    for rank, (orig_idx, content, score) in enumerate(chunks, 1):
        html += f"""
                <div class="result-item">
                    <div class="rank">#{rank}</div>
                    <div class="score">{score:.4f}</div>
                    <div class="content">
                        {content}
                        <div class="original-index">Original position: #{orig_idx + 1}</div>
                    </div>
                </div>
"""
    
    html += """
            </div>
        </div>
        
        <div class="section">
            <h2>Reranked Paths (Graph Knowledge)</h2>
            <div class="result-list">
"""
    
    for rank, (orig_idx, content, score) in enumerate(paths, 1):
        html += f"""
                <div class="result-item">
                    <div class="rank">#{rank}</div>
                    <div class="score">{score:.4f}</div>
                    <div class="content">
                        {content}
                        <div class="original-index">Original position: #{orig_idx + 1}</div>
                    </div>
                </div>
"""
    
    html += f"""
            </div>
        </div>
        
        <div class="timestamp">
            Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>
"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    
    print(f"HTML report saved to: {output_path}")


def main():
    """Main test entry point."""
    parser = argparse.ArgumentParser(description="CrossEncoder Reranking Test")
    parser.add_argument("--output", "-o", type=str, default="reranking_test_report.html",
                        help="Output HTML report path")
    args = parser.parse_args()
    
    print("=" * 70)
    print("CrossEncoder Reranking Test")
    print("=" * 70)
    print()
    
    # Test query
    query = "What are the ESG compliance requirements for portfolio rebalancing?"
    print(f"Query: {query}")
    print()
    
    # Initialize client
    print("Initializing CrossEncoder client...")
    client = RerankerClient()
    
    # Load model
    print("Loading model...")
    load_start = time.time()
    client.load_model()
    load_time = time.time() - load_start
    print(f"Model loaded in {load_time:.2f}s")
    print()
    
    # Test chunks (simulating semantic search results)
    test_chunks = [
        "The weather today is sunny with a chance of rain in the afternoon.",
        "ESG rebalancing requires compliance with environmental, social, and governance standards.",
        "Portfolio optimization algorithms use mean-variance analysis for asset allocation.",
        "ESG compliance for portfolio management includes carbon footprint assessment and reporting.",
        "The stock market closed higher today on positive earnings reports from tech companies.",
        "Rebalancing triggers when portfolio drift exceeds threshold bands defined in client profiles.",
    ]
    
    # Test paths (simulating graph search results)
    test_paths = [
        "Process 'DriftDetection' triggers Process 'Rebalancing' in wealth_portfolio domain",
        "Control 'ESGComplianceCheck' mitigates RiskType 'ComplianceRisk'",
        "System 'PortfolioManager' depends on System 'AccountService'",
        "Requirement 'REQ-ESG-001' supports Process 'ESGRebalancing'",
        "Process 'TradeExecution' is followed by Process 'Settlement'",
        "TestCase 'TC-REBAL-001' validates Requirement 'REQ-DRIFT-001'",
    ]
    
    # Rerank chunks
    print(f"Reranking {len(test_chunks)} chunks...")
    rerank_start = time.time()
    reranked_chunks = client.rerank(query, test_chunks)
    chunks_time = time.time() - rerank_start
    print(f"Chunks reranked in {chunks_time:.3f}s")
    
    # Rerank paths
    print(f"Reranking {len(test_paths)} paths...")
    rerank_start = time.time()
    reranked_paths = client.rerank(query, test_paths)
    paths_time = time.time() - rerank_start
    print(f"Paths reranked in {paths_time:.3f}s")
    print()
    
    # Display results
    print("-" * 70)
    print("Reranked Chunks")
    print("-" * 70)
    for rank, (orig_idx, content, score) in enumerate(reranked_chunks, 1):
        print(f"  #{rank} [Score: {score:.4f}] (orig #{orig_idx + 1})")
        print(f"      {content[:70]}...")
    
    print()
    print("-" * 70)
    print("Reranked Paths")
    print("-" * 70)
    for rank, (orig_idx, content, score) in enumerate(reranked_paths, 1):
        print(f"  #{rank} [Score: {score:.4f}] (orig #{orig_idx + 1})")
        print(f"      {content}")
    
    # Generate HTML report
    print()
    print("-" * 70)
    print("Generating HTML Report")
    print("-" * 70)
    
    stats = client.get_load_stats()
    output_path = Path(args.output)
    generate_html_report(query, reranked_chunks, reranked_paths, stats, output_path)
    
    # Summary
    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"  Model load time: {load_time:.2f}s")
    print(f"  Chunks rerank time: {chunks_time:.3f}s")
    print(f"  Paths rerank time: {paths_time:.3f}s")
    print(f"  Report saved to: {output_path}")
    print()
    print("TEST PASSED")
    print("=" * 70)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())


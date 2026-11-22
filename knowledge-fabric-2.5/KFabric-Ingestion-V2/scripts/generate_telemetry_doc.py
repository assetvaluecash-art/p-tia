#!/usr/bin/env python
"""
Generate KnowledgeFabric Telemetry Registry Document

This script creates a Word document (.docx) with:
- Telemetry strategy overview
- Complete metrics registry
- Trace spans registry
- Architecture diagrams (referenced)

Usage:
    python scripts/generate_telemetry_doc.py

Output:
    docs/KnowledgeFabric_Telemetry_Registry.docx
"""

import os
import sys
from pathlib import Path
from datetime import datetime

# Try to import python-docx
try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.style import WD_STYLE_TYPE
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False
    print("python-docx not installed. Install with: pip install python-docx")
    print("Creating HTML version instead...")

# Directories
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
DOCS_DIR = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)

# Image paths (relative to cio_dashboard)
CIO_DASHBOARD = BASE_DIR.parent / "cio_dashboard"
IMAGES = {
    "overview": CIO_DASHBOARD / "Knowledge Fabric Overview.png",
    "data_flow": CIO_DASHBOARD / "Knowledge Fabric Data Flow Architecture.png", 
    "three_pillars": CIO_DASHBOARD / "Knowledge Fabric Three Pillars.png",
}

# =============================================================================
# METRICS REGISTRY DATA
# =============================================================================

OTEL_METRICS = [
    # Gen AI Metrics
    {
        "name": "gen_ai_client_operation_duration_seconds",
        "type": "Histogram",
        "labels": ["gen_ai_system", "gen_ai_request_model", "gen_ai_operation_name", "server_address"],
        "convention": "OTel Gen AI",
        "reference": "https://opentelemetry.io/docs/specs/semconv/gen-ai/",
        "purpose": "Duration of LLM operations (IE, domain detection, answer generation)",
        "service": "Both"
    },
    {
        "name": "gen_ai_client_token_usage_total",
        "type": "Counter",
        "labels": ["gen_ai_system", "gen_ai_request_model", "gen_ai_operation_name", "gen_ai_token_type"],
        "convention": "OTel Gen AI",
        "reference": "https://opentelemetry.io/docs/specs/semconv/gen-ai/",
        "purpose": "Token consumption (input/output) for cost tracking",
        "service": "Both"
    },
    # Database Metrics
    {
        "name": "db_client_operation_duration_seconds",
        "type": "Histogram",
        "labels": ["db_system", "db_operation", "db_namespace", "server_address"],
        "convention": "OTel Database",
        "reference": "https://opentelemetry.io/docs/specs/semconv/database/",
        "purpose": "Duration of database operations (Neo4j, Weaviate)",
        "service": "Both"
    },
    {
        "name": "db_client_rows_affected_total",
        "type": "Counter",
        "labels": ["db_system", "db_operation", "db_namespace"],
        "convention": "OTel Database",
        "reference": "https://opentelemetry.io/docs/specs/semconv/database/",
        "purpose": "Count of rows/documents affected by operations",
        "service": "Ingest"
    },
    # Ray Job Metrics
    {
        "name": "ray_job_total",
        "type": "Counter",
        "labels": ["ray_job_status", "kbfabric_domain"],
        "convention": "Custom (Ray-aligned)",
        "reference": "N/A",
        "purpose": "Total Ray jobs by status (submitted, success, error)",
        "service": "Ingest"
    },
    {
        "name": "ray_job_duration_seconds",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom (Ray-aligned)",
        "reference": "N/A",
        "purpose": "Duration of Ray job execution",
        "service": "Ingest"
    },
    {
        "name": "ray_job_active",
        "type": "Gauge",
        "labels": [],
        "convention": "Custom (Ray-aligned)",
        "reference": "N/A",
        "purpose": "Number of currently active Ray jobs",
        "service": "Ingest"
    },
]

CUSTOM_METRICS = [
    # Document Processing
    {
        "name": "kbfabric_document_parse_duration_seconds",
        "type": "Histogram",
        "labels": ["kbfabric_domain", "file_type"],
        "convention": "Custom",
        "purpose": "Duration of document parsing (PDF, DOCX, XLSX)",
        "service": "Ingest"
    },
    {
        "name": "kbfabric_document_blocks_total",
        "type": "Counter",
        "labels": ["kbfabric_domain", "block_type"],
        "convention": "Custom",
        "purpose": "Total document blocks created",
        "service": "Ingest"
    },
    {
        "name": "kbfabric_entities_extracted_total",
        "type": "Counter",
        "labels": ["kbfabric_domain", "entity_type"],
        "convention": "Custom",
        "purpose": "Total entities extracted by IE",
        "service": "Ingest"
    },
    # PII
    {
        "name": "kbfabric_pii_detections_total",
        "type": "Counter",
        "labels": ["kbfabric_domain", "pii_type"],
        "convention": "Custom",
        "purpose": "PII entities detected (compliance tracking)",
        "service": "Ingest"
    },
    {
        "name": "kbfabric_pii_masking_duration_seconds",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom",
        "purpose": "Duration of PII masking operations",
        "service": "Ingest"
    },
    # Retrieval
    {
        "name": "kbfabric_retrieval_queries_total",
        "type": "Counter",
        "labels": ["kbfabric_domain", "status"],
        "convention": "Custom",
        "purpose": "Total retrieval queries by domain",
        "service": "Retrieval"
    },
    {
        "name": "kbfabric_retrieval_query_duration_seconds",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom",
        "purpose": "E2E retrieval query latency",
        "service": "Retrieval"
    },
    {
        "name": "kbfabric_retrieval_contexts_returned",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom",
        "purpose": "Number of contexts returned per query",
        "service": "Retrieval"
    },
    {
        "name": "kbfabric_reranking_duration_seconds",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom",
        "purpose": "Cross-encoder reranking latency",
        "service": "Retrieval"
    },
    {
        "name": "kbfabric_reranking_candidates",
        "type": "Histogram",
        "labels": ["kbfabric_domain"],
        "convention": "Custom",
        "purpose": "Candidates processed by reranker",
        "service": "Retrieval"
    },
]

SPANS_REGISTRY = [
    {
        "name": "kbfabric.ingestion run",
        "parent": "None (root)",
        "attributes": ["domain", "input_dir"],
        "purpose": "Full ingestion job execution"
    },
    {
        "name": "ollama chat {model}",
        "parent": "kbfabric.ingestion run",
        "attributes": ["gen_ai.system", "gen_ai.request.model", "gen_ai.operation.name"],
        "purpose": "LLM call for IE or answer generation"
    },
    {
        "name": "neo4j write",
        "parent": "kbfabric.ingestion run",
        "attributes": ["db.system", "db.operation", "db.namespace"],
        "purpose": "Neo4j write operation"
    },
    {
        "name": "weaviate batch_create",
        "parent": "kbfabric.ingestion run",
        "attributes": ["db.system", "db.operation", "db.namespace"],
        "purpose": "Weaviate batch upsert"
    },
    {
        "name": "kbfabric.retrieval query",
        "parent": "HTTP POST /query",
        "attributes": ["kbfabric.domain", "query"],
        "purpose": "Full retrieval query execution"
    },
    {
        "name": "weaviate nearVector",
        "parent": "kbfabric.retrieval query",
        "attributes": ["db.system", "db.operation", "db.namespace"],
        "purpose": "Semantic vector search"
    },
    {
        "name": "neo4j path_query",
        "parent": "kbfabric.retrieval query",
        "attributes": ["db.system", "db.operation", "db.namespace"],
        "purpose": "Graph path traversal"
    },
]


def create_docx():
    """Create Word document with telemetry registry."""
    doc = Document()
    
    # Title
    title = doc.add_heading('KnowledgeFabric Complete Telemetry Registry', 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # Subtitle
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(128, 128, 128)
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 1: EXECUTIVE SUMMARY
    # ==========================================================================
    doc.add_heading('1. Executive Summary', level=1)
    
    doc.add_paragraph(
        'KnowledgeFabric implements enterprise-grade observability following OpenTelemetry '
        'semantic conventions. This enables APM tool auto-discovery, cross-vendor comparability, '
        'and standardized LLM metrics for AI operations tracking.'
    )
    
    # Key stats table
    stats_table = doc.add_table(rows=4, cols=2)
    stats_table.style = 'Table Grid'
    stats_data = [
        ('Total OTel-Compliant Metrics', str(len(OTEL_METRICS))),
        ('Total Custom Metrics', str(len(CUSTOM_METRICS))),
        ('Total Trace Spans', str(len(SPANS_REGISTRY))),
        ('CNCF Components', 'Prometheus, Jaeger, Perses'),
    ]
    for i, (label, value) in enumerate(stats_data):
        stats_table.rows[i].cells[0].text = label
        stats_table.rows[i].cells[1].text = value
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 2: TELEMETRY STRATEGY
    # ==========================================================================
    doc.add_heading('2. Telemetry Strategy', level=1)
    
    doc.add_heading('2.1 Three Pillars of Observability', level=2)
    
    pillars_table = doc.add_table(rows=4, cols=4)
    pillars_table.style = 'Table Grid'
    pillars_headers = ['Pillar', 'Tool', 'Question Answered', 'Port']
    pillars_data = [
        ('Metrics', 'Prometheus', 'How many? How long on average?', ':9090'),
        ('Traces', 'Jaeger', 'Why was THIS request slow?', ':16686'),
        ('Dashboards', 'Perses', 'Trends over time, alerts', ':8081'),
    ]
    
    # Headers
    for i, header in enumerate(pillars_headers):
        pillars_table.rows[0].cells[i].text = header
    # Data
    for i, row_data in enumerate(pillars_data, 1):
        for j, cell_data in enumerate(row_data):
            pillars_table.rows[i].cells[j].text = cell_data
    
    doc.add_paragraph()
    
    doc.add_heading('2.2 CNCF Stack (100% Apache 2.0)', level=2)
    
    doc.add_paragraph(
        'All observability components are CNCF projects with Apache 2.0 licensing, '
        'ensuring no licensing concerns for enterprise deployment:'
    )
    
    cncf_list = doc.add_paragraph()
    cncf_list.add_run('• Prometheus').bold = True
    cncf_list.add_run(' (Graduated) - Metrics storage and querying\n')
    cncf_list.add_run('• Jaeger').bold = True
    cncf_list.add_run(' (Graduated) - Distributed tracing\n')
    cncf_list.add_run('• Perses').bold = True
    cncf_list.add_run(' (Sandbox) - Dashboards (Grafana alternative)\n')
    cncf_list.add_run('• OpenTelemetry').bold = True
    cncf_list.add_run(' (Incubating) - Instrumentation standards')
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 3: OTEL METRICS REGISTRY
    # ==========================================================================
    doc.add_heading('3. OpenTelemetry Semantic Convention Metrics', level=1)
    
    doc.add_paragraph(
        'These metrics follow OpenTelemetry semantic conventions for automatic APM recognition '
        'and cross-vendor compatibility.'
    )
    
    # OTel Metrics Table
    otel_table = doc.add_table(rows=len(OTEL_METRICS) + 1, cols=5)
    otel_table.style = 'Table Grid'
    
    # Headers
    otel_headers = ['Metric Name', 'Type', 'Labels', 'Convention', 'Purpose']
    for i, header in enumerate(otel_headers):
        otel_table.rows[0].cells[i].text = header
        otel_table.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    
    # Data
    for i, metric in enumerate(OTEL_METRICS, 1):
        otel_table.rows[i].cells[0].text = metric['name']
        otel_table.rows[i].cells[1].text = metric['type']
        otel_table.rows[i].cells[2].text = ', '.join(metric['labels']) if metric['labels'] else '-'
        otel_table.rows[i].cells[3].text = metric['convention']
        otel_table.rows[i].cells[4].text = metric['purpose']
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 4: CUSTOM METRICS REGISTRY
    # ==========================================================================
    doc.add_heading('4. KnowledgeFabric Custom Metrics', level=1)
    
    doc.add_paragraph(
        'Domain-specific metrics for KnowledgeFabric operations where no OTel convention exists.'
    )
    
    # Custom Metrics Table
    custom_table = doc.add_table(rows=len(CUSTOM_METRICS) + 1, cols=5)
    custom_table.style = 'Table Grid'
    
    # Headers
    custom_headers = ['Metric Name', 'Type', 'Labels', 'Service', 'Purpose']
    for i, header in enumerate(custom_headers):
        custom_table.rows[0].cells[i].text = header
        custom_table.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    
    # Data
    for i, metric in enumerate(CUSTOM_METRICS, 1):
        custom_table.rows[i].cells[0].text = metric['name']
        custom_table.rows[i].cells[1].text = metric['type']
        custom_table.rows[i].cells[2].text = ', '.join(metric['labels']) if metric['labels'] else '-'
        custom_table.rows[i].cells[3].text = metric['service']
        custom_table.rows[i].cells[4].text = metric['purpose']
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 5: TRACE SPANS REGISTRY
    # ==========================================================================
    doc.add_heading('5. Trace Spans Registry', level=1)
    
    doc.add_paragraph(
        'Distributed tracing spans exported to Jaeger via OTLP. Spans show parent-child '
        'relationships for debugging request flows.'
    )
    
    # Spans Table
    spans_table = doc.add_table(rows=len(SPANS_REGISTRY) + 1, cols=4)
    spans_table.style = 'Table Grid'
    
    # Headers
    spans_headers = ['Span Name', 'Parent', 'Attributes', 'Purpose']
    for i, header in enumerate(spans_headers):
        spans_table.rows[0].cells[i].text = header
        spans_table.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    
    # Data
    for i, span in enumerate(SPANS_REGISTRY, 1):
        spans_table.rows[i].cells[0].text = span['name']
        spans_table.rows[i].cells[1].text = span['parent']
        spans_table.rows[i].cells[2].text = ', '.join(span['attributes']) if span['attributes'] else '-'
        spans_table.rows[i].cells[3].text = span['purpose']
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 6: ENDPOINTS
    # ==========================================================================
    doc.add_heading('6. Observability Endpoints', level=1)
    
    endpoints_table = doc.add_table(rows=6, cols=3)
    endpoints_table.style = 'Table Grid'
    
    endpoints_headers = ['Endpoint', 'Service', 'Purpose']
    endpoints_data = [
        ('http://localhost:8001/metrics', 'Ingest API', 'Prometheus metrics'),
        ('http://localhost:8002/metrics', 'Retrieval API', 'Prometheus metrics'),
        ('http://localhost:9090', 'Prometheus', 'Metrics UI & PromQL'),
        ('http://localhost:16686', 'Jaeger', 'Traces UI'),
        ('http://localhost:8081', 'Perses', 'Dashboards'),
    ]
    
    # Headers
    for i, header in enumerate(endpoints_headers):
        endpoints_table.rows[0].cells[i].text = header
        endpoints_table.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    
    # Data
    for i, row_data in enumerate(endpoints_data, 1):
        for j, cell_data in enumerate(row_data):
            endpoints_table.rows[i].cells[j].text = cell_data
    
    doc.add_paragraph()
    
    # ==========================================================================
    # SECTION 7: REFERENCES
    # ==========================================================================
    doc.add_heading('7. References', level=1)
    
    refs = doc.add_paragraph()
    refs.add_run('OpenTelemetry Semantic Conventions:\n').bold = True
    refs.add_run('• General: https://opentelemetry.io/docs/specs/semconv/general/\n')
    refs.add_run('• Gen AI: https://opentelemetry.io/docs/specs/semconv/gen-ai/\n')
    refs.add_run('• Database: https://opentelemetry.io/docs/specs/semconv/database/\n')
    refs.add_run('• HTTP: https://opentelemetry.io/docs/specs/semconv/http/\n\n')
    refs.add_run('CNCF Projects:\n').bold = True
    refs.add_run('• Prometheus: https://prometheus.io\n')
    refs.add_run('• Jaeger: https://jaegertracing.io\n')
    refs.add_run('• Perses: https://perses.dev\n')
    
    # Save
    output_path = DOCS_DIR / 'KnowledgeFabric_Telemetry_Registry.docx'
    doc.save(str(output_path))
    print(f"✓ Word document saved to: {output_path}")
    return output_path


def create_html_fallback():
    """Create HTML version if python-docx not available."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>KnowledgeFabric Telemetry Registry</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { color: #1a365d; border-bottom: 2px solid #3182ce; padding-bottom: 10px; }
        h2 { color: #2c5282; margin-top: 30px; }
        table { border-collapse: collapse; width: 100%; margin: 20px 0; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background: #3182ce; color: white; }
        tr:nth-child(even) { background: #f7fafc; }
        .metric-name { font-family: monospace; font-size: 0.9em; }
        .section { margin-bottom: 40px; }
    </style>
</head>
<body>
    <h1>KnowledgeFabric Complete Telemetry Registry</h1>
    <p style="color: #718096;">Generated: """ + datetime.now().strftime("%Y-%m-%d %H:%M") + """</p>
    
    <div class="section">
        <h2>1. Executive Summary</h2>
        <p>KnowledgeFabric implements enterprise-grade observability following OpenTelemetry 
        semantic conventions. This enables APM tool auto-discovery, cross-vendor comparability, 
        and standardized LLM metrics for AI operations tracking.</p>
    </div>
    
    <div class="section">
        <h2>2. OpenTelemetry Semantic Convention Metrics</h2>
        <table>
            <tr><th>Metric Name</th><th>Type</th><th>Labels</th><th>Convention</th><th>Purpose</th></tr>
"""
    
    for m in OTEL_METRICS:
        labels = ', '.join(m['labels']) if m['labels'] else '-'
        html_content += f"""            <tr>
                <td class="metric-name">{m['name']}</td>
                <td>{m['type']}</td>
                <td>{labels}</td>
                <td>{m['convention']}</td>
                <td>{m['purpose']}</td>
            </tr>
"""
    
    html_content += """        </table>
    </div>
    
    <div class="section">
        <h2>3. KnowledgeFabric Custom Metrics</h2>
        <table>
            <tr><th>Metric Name</th><th>Type</th><th>Labels</th><th>Service</th><th>Purpose</th></tr>
"""
    
    for m in CUSTOM_METRICS:
        labels = ', '.join(m['labels']) if m['labels'] else '-'
        html_content += f"""            <tr>
                <td class="metric-name">{m['name']}</td>
                <td>{m['type']}</td>
                <td>{labels}</td>
                <td>{m['service']}</td>
                <td>{m['purpose']}</td>
            </tr>
"""
    
    html_content += """        </table>
    </div>
    
    <div class="section">
        <h2>4. Trace Spans Registry</h2>
        <table>
            <tr><th>Span Name</th><th>Parent</th><th>Attributes</th><th>Purpose</th></tr>
"""
    
    for s in SPANS_REGISTRY:
        attrs = ', '.join(s['attributes']) if s['attributes'] else '-'
        html_content += f"""            <tr>
                <td class="metric-name">{s['name']}</td>
                <td>{s['parent']}</td>
                <td>{attrs}</td>
                <td>{s['purpose']}</td>
            </tr>
"""
    
    html_content += """        </table>
    </div>
    
    <div class="section">
        <h2>5. References</h2>
        <ul>
            <li><a href="https://opentelemetry.io/docs/specs/semconv/gen-ai/">OTel Gen AI Conventions</a></li>
            <li><a href="https://opentelemetry.io/docs/specs/semconv/database/">OTel Database Conventions</a></li>
            <li><a href="https://prometheus.io">Prometheus</a></li>
            <li><a href="https://jaegertracing.io">Jaeger</a></li>
            <li><a href="https://perses.dev">Perses</a></li>
        </ul>
    </div>
</body>
</html>
"""
    
    output_path = DOCS_DIR / 'KnowledgeFabric_Telemetry_Registry.html'
    output_path.write_text(html_content, encoding='utf-8')
    print(f"✓ HTML document saved to: {output_path}")
    print("  (Open in browser and save as Word/PDF)")
    return output_path


def main():
    print("=" * 60)
    print("  KnowledgeFabric Telemetry Registry Generator")
    print("=" * 60)
    
    if HAS_DOCX:
        output = create_docx()
    else:
        output = create_html_fallback()
    
    print(f"\nDocument location: {output}")
    print("\nMetrics Summary:")
    print(f"  - OTel Compliant: {len(OTEL_METRICS)}")
    print(f"  - Custom:         {len(CUSTOM_METRICS)}")
    print(f"  - Trace Spans:    {len(SPANS_REGISTRY)}")


if __name__ == "__main__":
    main()


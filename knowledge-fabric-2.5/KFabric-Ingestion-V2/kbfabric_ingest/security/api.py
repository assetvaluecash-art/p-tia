"""Security scanning API endpoints.

Provides REST API endpoints for on-demand security scanning
and InfoSec compliance reporting.

Usage:
    from kbfabric_ingest.security.api import security_router
    app.include_router(security_router)
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from kbfabric_ingest.security.scanner import SecurityScanner, run_security_scan
from kbfabric_ingest.security.models import SecurityReport, ScanStatus, SeverityLevel
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

security_router = APIRouter(prefix="/security", tags=["Security"])


class ScanRequest(BaseModel):
    """Request to run a security scan."""
    include_raw_data: bool = Field(
        default=False, 
        description="Include raw scan output in response"
    )


class ScanSummaryResponse(BaseModel):
    """Summary of security scan results."""
    report_id: str
    status: str
    risk_level: str
    total_findings: int
    critical: int
    high: int
    medium: int
    low: int
    scans_performed: list[str]
    generated_at: str
    report_url: str


@security_router.get(
    "/report",
    response_model=SecurityReport,
    summary="Run Security Scan and Generate InfoSec Report",
    description="""
Run comprehensive security scans and generate a detailed InfoSec compliance report.

**Scans Performed:**
- **SCA (Software Composition Analysis)**: Checks dependencies for known vulnerabilities using pip-audit
- **Secrets Detection**: Scans code for hardcoded credentials, API keys, and other secrets

**Report Contents:**
- Executive summary for stakeholders
- Detailed vulnerability findings
- Actionable recommendations with priority
- Compliance coverage status
- Additional security tests that can be performed

**Use Cases:**
- On-demand security assessment before releases
- Evidence for security audits and compliance reviews
- Identifying remediation priorities

**Note:** For full SAST (Static Application Security Testing), configure SonarQube at http://localhost:9001
"""
)
async def generate_security_report(
    include_raw: bool = Query(False, description="Include raw scan output")
) -> SecurityReport:
    """
    Generate comprehensive InfoSec compliance report.
    
    This endpoint runs all available security scans and produces
    a detailed report suitable for bank security audits.
    """
    logger.info("Security scan requested via API")
    
    try:
        report = await run_security_scan(include_raw=include_raw)
        
        logger.info(
            "Security scan completed via API",
            report_id=report.report_id,
            status=report.overall_status.value,
            findings=report.total_findings
        )
        
        return report
        
    except Exception as e:
        logger.error("Security scan failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Security scan failed: {str(e)}"
        )


@security_router.get(
    "/report/html",
    response_class=HTMLResponse,
    summary="Generate HTML Security Report",
    description="Generate a formatted HTML report suitable for printing or sharing."
)
async def generate_html_report() -> HTMLResponse:
    """Generate HTML-formatted security report for easy viewing/printing."""
    try:
        report = await run_security_scan(include_raw=False)
        html = _generate_html_report(report)
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error("HTML report generation failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Report generation failed: {str(e)}"
        )


@security_router.get(
    "/report/summary",
    response_model=ScanSummaryResponse,
    summary="Quick Security Summary",
    description="Get a quick summary without full scan details."
)
async def get_security_summary() -> ScanSummaryResponse:
    """Get quick security scan summary."""
    try:
        report = await run_security_scan(include_raw=False)
        
        return ScanSummaryResponse(
            report_id=report.report_id,
            status=report.overall_status.value,
            risk_level=report.overall_risk_level.value,
            total_findings=report.total_findings,
            critical=report.critical_findings,
            high=report.high_findings,
            medium=report.medium_findings,
            low=report.low_findings,
            scans_performed=[s.scan_name for s in report.scans_performed],
            generated_at=report.generated_at.isoformat(),
            report_url="/security/report"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@security_router.get(
    "/coverage",
    summary="Security Coverage Status",
    description="Get status of which security scans are available and configured."
)
async def get_security_coverage() -> dict:
    """Get security scan coverage status."""
    return {
        "available_scans": {
            "sca_dependency_scan": {
                "enabled": True,
                "tool": "pip-audit",
                "description": "Checks Python dependencies for known vulnerabilities"
            },
            "secrets_detection": {
                "enabled": True,
                "tool": "Built-in regex patterns",
                "description": "Detects hardcoded secrets, API keys, credentials"
            },
            "sast_static_analysis": {
                "enabled": False,
                "tool": "SonarQube",
                "description": "Static code analysis for bugs, vulnerabilities, code smells",
                "setup_url": "http://localhost:9001"
            },
            "dast_dynamic_testing": {
                "enabled": False,
                "tool": "OWASP ZAP",
                "description": "Dynamic API security testing",
                "setup_instructions": "docker run -t owasp/zap2docker-stable zap-api-scan.py -t http://localhost:6002/openapi.json"
            },
            "container_scanning": {
                "enabled": False,
                "tool": "Trivy",
                "description": "Docker image vulnerability scanning",
                "setup_instructions": "docker run aquasec/trivy image your-image:tag"
            }
        },
        "compliance_standards": [
            "OWASP Top 10 2021",
            "CWE Top 25",
            "SANS Top 25"
        ],
        "recommendations": [
            "Configure SonarQube for comprehensive SAST coverage",
            "Run DAST scans before production deployment",
            "Implement container scanning in CI/CD pipeline"
        ]
    }


def _generate_html_report(report: SecurityReport) -> str:
    """Generate HTML version of security report."""
    
    # Determine status colors
    status_color = {
        "passed": "#28a745",
        "warning": "#ffc107", 
        "failed": "#dc3545",
        "error": "#dc3545"
    }.get(report.overall_status.value, "#6c757d")
    
    risk_color = {
        "critical": "#dc3545",
        "high": "#fd7e14",
        "medium": "#ffc107",
        "low": "#28a745"
    }.get(report.overall_risk_level.value, "#6c757d")
    
    # Build vulnerability table rows
    vuln_rows = ""
    for scan in report.scans_performed:
        for vuln in scan.vulnerabilities:
            severity_badge = f'<span class="badge badge-{vuln.severity.value}">{vuln.severity.value.upper()}</span>'
            vuln_rows += f"""
            <tr>
                <td>{vuln.package}</td>
                <td>{vuln.installed_version}</td>
                <td>{vuln.vulnerability_id}</td>
                <td>{severity_badge}</td>
                <td>{vuln.fixed_in or 'N/A'}</td>
            </tr>
            """
    
    # Build secrets table rows
    secrets_rows = ""
    for scan in report.scans_performed:
        for secret in scan.secrets:
            severity_badge = f'<span class="badge badge-{secret.severity.value}">{secret.severity.value.upper()}</span>'
            secrets_rows += f"""
            <tr>
                <td>{secret.file_path}</td>
                <td>{secret.line_number}</td>
                <td>{secret.secret_type}</td>
                <td>{severity_badge}</td>
                <td><code>{secret.redacted_match}</code></td>
            </tr>
            """
    
    # Build recommendations list
    recommendations_html = ""
    for rec in report.recommendations:
        recommendations_html += f"""
        <div class="recommendation">
            <h4>P{rec.priority}: {rec.title}</h4>
            <p>{rec.description}</p>
            <strong>Remediation Steps:</strong>
            <ol>
                {"".join(f"<li>{step}</li>" for step in rec.remediation_steps)}
            </ol>
        </div>
        """
    
    # Build additional tests section
    additional_tests_html = ""
    for test in report.additional_tests_available:
        additional_tests_html += f"""
        <tr>
            <td>{test['name']}</td>
            <td>{test['tool']}</td>
            <td>{test['description']}</td>
            <td><span class="badge badge-{test['priority']}">{test['priority'].upper()}</span></td>
        </tr>
        """
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Security Assessment Report - {report.report_id}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 20px;
        }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; }}
        .header .meta {{ opacity: 0.8; font-size: 14px; }}
        .summary-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .card h3 {{ font-size: 14px; color: #666; margin-bottom: 5px; }}
        .card .value {{ font-size: 32px; font-weight: bold; }}
        .status-passed {{ color: #28a745; }}
        .status-warning {{ color: #ffc107; }}
        .status-failed {{ color: #dc3545; }}
        .section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        .section h2 {{
            border-bottom: 2px solid #eee;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            color: white;
        }}
        .badge-critical {{ background: #dc3545; }}
        .badge-high {{ background: #fd7e14; }}
        .badge-medium {{ background: #ffc107; color: #333; }}
        .badge-low {{ background: #28a745; }}
        .badge-info {{ background: #17a2b8; }}
        .executive-summary {{
            background: #f8f9fa;
            padding: 20px;
            border-left: 4px solid {status_color};
            white-space: pre-wrap;
            font-family: inherit;
        }}
        .recommendation {{
            border: 1px solid #eee;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 5px;
        }}
        .recommendation h4 {{ color: #1a1a2e; margin-bottom: 10px; }}
        .coverage-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 10px;
        }}
        .coverage-item {{
            padding: 10px;
            border: 1px solid #eee;
            border-radius: 5px;
        }}
        .coverage-item.enabled {{ border-left: 3px solid #28a745; }}
        .coverage-item.disabled {{ border-left: 3px solid #dc3545; }}
        @media print {{
            body {{ background: white; }}
            .section {{ box-shadow: none; border: 1px solid #eee; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🔒 {report.report_title}</h1>
        <div class="meta">
            Report ID: {report.report_id} | 
            Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} |
            Environment: {report.environment}
        </div>
    </div>
    
    <div class="summary-cards">
        <div class="card">
            <h3>Overall Status</h3>
            <div class="value status-{report.overall_status.value}">{report.overall_status.value.upper()}</div>
        </div>
        <div class="card">
            <h3>Risk Level</h3>
            <div class="value" style="color: {risk_color}">{report.overall_risk_level.value.upper()}</div>
        </div>
        <div class="card">
            <h3>Total Findings</h3>
            <div class="value">{report.total_findings}</div>
        </div>
        <div class="card">
            <h3>Critical/High</h3>
            <div class="value" style="color: #dc3545">{report.critical_findings + report.high_findings}</div>
        </div>
    </div>
    
    <div class="section">
        <h2>Executive Summary</h2>
        <div class="executive-summary">{report.executive_summary}</div>
    </div>
    
    <div class="section">
        <h2>Findings by Severity</h2>
        <table>
            <tr>
                <th>Critical</th>
                <th>High</th>
                <th>Medium</th>
                <th>Low</th>
            </tr>
            <tr>
                <td><span class="badge badge-critical">{report.critical_findings}</span></td>
                <td><span class="badge badge-high">{report.high_findings}</span></td>
                <td><span class="badge badge-medium">{report.medium_findings}</span></td>
                <td><span class="badge badge-low">{report.low_findings}</span></td>
            </tr>
        </table>
    </div>
    
    {"<div class='section'><h2>Dependency Vulnerabilities (SCA)</h2><table><tr><th>Package</th><th>Version</th><th>Vulnerability</th><th>Severity</th><th>Fixed In</th></tr>" + vuln_rows + "</table></div>" if vuln_rows else ""}
    
    {"<div class='section'><h2>Secrets Detected</h2><table><tr><th>File</th><th>Line</th><th>Type</th><th>Severity</th><th>Match</th></tr>" + secrets_rows + "</table></div>" if secrets_rows else ""}
    
    <div class="section">
        <h2>Recommendations</h2>
        {recommendations_html if recommendations_html else "<p>No critical recommendations at this time.</p>"}
    </div>
    
    <div class="section">
        <h2>Security Coverage</h2>
        <div class="coverage-grid">
            {"".join(f'<div class="coverage-item {"enabled" if enabled else "disabled"}"><strong>{name.replace("_", " ").title()}</strong><br/>{"✅ Enabled" if enabled else "❌ Not Configured"}</div>' for name, enabled in report.scans_coverage.items())}
        </div>
    </div>
    
    <div class="section">
        <h2>Additional Security Tests Available</h2>
        <p>The following security tests are recommended for comprehensive coverage:</p>
        <table>
            <tr><th>Test</th><th>Tool</th><th>Description</th><th>Priority</th></tr>
            {additional_tests_html}
        </table>
    </div>
    
    <div class="section" style="text-align: center; color: #666; font-size: 12px;">
        <p>This report was automatically generated by KnowledgeFabric Security Scanner v{report.report_version}</p>
        <p>For questions, contact your security team.</p>
    </div>
</body>
</html>
    """
    
    return html


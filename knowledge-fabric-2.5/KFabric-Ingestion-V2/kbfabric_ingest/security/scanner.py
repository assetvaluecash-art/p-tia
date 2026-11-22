"""Security scanner implementation.

Performs comprehensive security scans including:
- SCA (Software Composition Analysis) using pip-audit
- Secrets detection using regex patterns
- Code quality metrics collection

Usage:
    scanner = SecurityScanner()
    report = await scanner.generate_report()
"""

import asyncio
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kbfabric_ingest.security.models import (
    SecurityReport,
    ScanResult,
    ScanStatus,
    SeverityLevel,
    VulnerabilityFinding,
    SecretFinding,
    SecurityRecommendation,
    ComplianceStatus,
    CodeQualityMetric
)
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

# Secret patterns to detect (regex)
SECRET_PATTERNS = [
    {
        "name": "AWS Access Key",
        "pattern": r"AKIA[0-9A-Z]{16}",
        "severity": SeverityLevel.CRITICAL
    },
    {
        "name": "AWS Secret Key",
        "pattern": r"(?i)aws(.{0,20})?['\"][0-9a-zA-Z/+]{40}['\"]",
        "severity": SeverityLevel.CRITICAL
    },
    {
        "name": "Generic API Key",
        "pattern": r"(?i)(api[_-]?key|apikey)['\"]?\s*[:=]\s*['\"][a-zA-Z0-9_\-]{20,}['\"]",
        "severity": SeverityLevel.HIGH
    },
    {
        "name": "Generic Secret",
        "pattern": r"(?i)(secret|password|passwd|pwd)['\"]?\s*[:=]\s*['\"][^'\"]{8,}['\"]",
        "severity": SeverityLevel.HIGH
    },
    {
        "name": "Private Key",
        "pattern": r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        "severity": SeverityLevel.CRITICAL
    },
    {
        "name": "GitHub Token",
        "pattern": r"ghp_[a-zA-Z0-9]{36}",
        "severity": SeverityLevel.CRITICAL
    },
    {
        "name": "Slack Token",
        "pattern": r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}",
        "severity": SeverityLevel.HIGH
    },
    {
        "name": "JWT Token",
        "pattern": r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*",
        "severity": SeverityLevel.MEDIUM
    },
    {
        "name": "Database Connection String",
        "pattern": r"(?i)(mongodb|postgres|mysql|redis)://[^\s'\"]+",
        "severity": SeverityLevel.HIGH
    },
    {
        "name": "Hardcoded IP with Port",
        "pattern": r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):\d{2,5}\b",
        "severity": SeverityLevel.LOW
    }
]

# Files/directories to exclude from secrets scan
EXCLUDED_PATHS = {
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "*.pyc",
    "*.log",
    "*.egg-info",
    "tests",
    "test_",
    "conftest.py",
    "samples",
    "_deprecated"
}


class SecurityScanner:
    """Comprehensive security scanner for KnowledgeFabric."""
    
    def __init__(self, project_root: Path | None = None):
        """
        Initialize security scanner.
        
        Args:
            project_root: Root directory of the project to scan.
                         Defaults to parent of kbfabric-ingest.
        """
        if project_root:
            self.project_root = Path(project_root)
        else:
            # Default to project root (parent of kbfabric-ingest)
            self.project_root = Path(__file__).parent.parent.parent.parent.parent
        
        self.logger = get_logger(__name__)
    
    async def generate_report(self, include_raw_data: bool = False) -> SecurityReport:
        """
        Generate comprehensive security report.
        
        Args:
            include_raw_data: Include raw scan output in report
            
        Returns:
            Complete SecurityReport with all scan results
        """
        report_id = f"sec-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        
        self.logger.info("Starting security scan", report_id=report_id)
        
        # Run all scans concurrently
        sca_task = asyncio.create_task(self._run_sca_scan())
        secrets_task = asyncio.create_task(self._run_secrets_scan())
        
        sca_result = await sca_task
        secrets_result = await secrets_task
        
        scans = [sca_result, secrets_result]
        
        # Calculate totals
        total_findings = sum(s.findings_count for s in scans)
        critical = sum(s.critical_count for s in scans)
        high = sum(s.high_count for s in scans)
        medium = sum(s.medium_count for s in scans)
        low = sum(s.low_count for s in scans)
        
        # Determine overall status
        if critical > 0:
            overall_status = ScanStatus.FAILED
            overall_risk = SeverityLevel.CRITICAL
        elif high > 0:
            overall_status = ScanStatus.WARNING
            overall_risk = SeverityLevel.HIGH
        elif medium > 0:
            overall_status = ScanStatus.WARNING
            overall_risk = SeverityLevel.MEDIUM
        else:
            overall_status = ScanStatus.PASSED
            overall_risk = SeverityLevel.LOW
        
        # Generate recommendations
        recommendations = self._generate_recommendations(scans)
        
        # Generate executive summary
        executive_summary = self._generate_executive_summary(
            scans, total_findings, critical, high, medium, low
        )
        
        # Build report
        report = SecurityReport(
            report_id=report_id,
            generated_at=datetime.now(timezone.utc),
            overall_status=overall_status,
            overall_risk_level=overall_risk,
            executive_summary=executive_summary,
            scans_performed=scans,
            total_findings=total_findings,
            critical_findings=critical,
            high_findings=high,
            medium_findings=medium,
            low_findings=low,
            recommendations=recommendations,
            scans_coverage={
                "sast_static_analysis": False,  # SonarQube not run via API
                "sca_dependency_scan": sca_result.status != ScanStatus.SKIPPED,
                "secrets_detection": secrets_result.status != ScanStatus.SKIPPED,
                "dast_dynamic_testing": False,
                "container_scanning": False,
                "iac_scanning": False,
                "penetration_testing": False
            }
        )
        
        if include_raw_data:
            report.raw_scan_data = {
                "sca": sca_result.raw_output,
                "secrets": secrets_result.raw_output
            }
        
        self.logger.info(
            "Security scan completed",
            report_id=report_id,
            status=overall_status.value,
            findings=total_findings
        )
        
        return report
    
    async def _run_sca_scan(self) -> ScanResult:
        """Run Software Composition Analysis using pip-audit."""
        started_at = datetime.now(timezone.utc)
        
        vulnerabilities: list[VulnerabilityFinding] = []
        raw_output = ""
        error_message = None
        
        # Find requirements files
        req_files = [
            self.project_root / "kbfabric-ingest" / "requirements.txt",
            self.project_root / "kbfabric-retrieval" / "requirements.txt",
            self.project_root / "requirements.txt"
        ]
        
        for req_file in req_files:
            if not req_file.exists():
                continue
            
            try:
                # Run pip-audit
                result = await asyncio.create_subprocess_exec(
                    "pip-audit",
                    "--requirement", str(req_file),
                    "--format", "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await result.communicate()
                
                raw_output += f"\n--- {req_file.name} ---\n{stdout.decode()}"
                
                if stdout:
                    try:
                        audit_data = json.loads(stdout.decode())
                        for item in audit_data:
                            for vuln in item.get("vulns", []):
                                severity = self._map_severity(vuln.get("id", ""))
                                vulnerabilities.append(VulnerabilityFinding(
                                    package=item.get("name", "unknown"),
                                    installed_version=item.get("version", "unknown"),
                                    vulnerability_id=vuln.get("id", "unknown"),
                                    severity=severity,
                                    title=vuln.get("id", "Unknown vulnerability"),
                                    description=vuln.get("description", ""),
                                    fixed_in=", ".join(vuln.get("fix_versions", [])),
                                    references=vuln.get("aliases", [])
                                ))
                    except json.JSONDecodeError:
                        pass
                        
            except FileNotFoundError:
                error_message = "pip-audit not installed. Install with: pip install pip-audit"
                self.logger.warning("pip-audit not found")
                break
            except Exception as e:
                error_message = str(e)
                self.logger.error("SCA scan error", error=str(e))
        
        completed_at = datetime.now(timezone.utc)
        
        # Count severities
        critical = sum(1 for v in vulnerabilities if v.severity == SeverityLevel.CRITICAL)
        high = sum(1 for v in vulnerabilities if v.severity == SeverityLevel.HIGH)
        medium = sum(1 for v in vulnerabilities if v.severity == SeverityLevel.MEDIUM)
        low = sum(1 for v in vulnerabilities if v.severity == SeverityLevel.LOW)
        
        status = ScanStatus.PASSED
        if critical > 0 or high > 0:
            status = ScanStatus.FAILED
        elif medium > 0:
            status = ScanStatus.WARNING
        elif error_message:
            status = ScanStatus.ERROR
        
        return ScanResult(
            scan_type="sca",
            scan_name="Software Composition Analysis (pip-audit)",
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=(completed_at - started_at).total_seconds(),
            findings_count=len(vulnerabilities),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
            vulnerabilities=vulnerabilities,
            error_message=error_message,
            raw_output=raw_output if raw_output else None
        )
    
    async def _run_secrets_scan(self) -> ScanResult:
        """Run secrets detection scan using regex patterns."""
        started_at = datetime.now(timezone.utc)
        
        secrets: list[SecretFinding] = []
        files_scanned = 0
        
        # Scan Python files
        for py_file in self.project_root.rglob("*.py"):
            # Skip excluded paths
            if any(excl in str(py_file) for excl in EXCLUDED_PATHS):
                continue
            
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                files_scanned += 1
                
                for line_num, line in enumerate(content.split("\n"), 1):
                    for pattern_info in SECRET_PATTERNS:
                        matches = re.finditer(pattern_info["pattern"], line)
                        for match in matches:
                            # Redact the match
                            matched_text = match.group()
                            redacted = matched_text[:4] + "*" * (len(matched_text) - 8) + matched_text[-4:] if len(matched_text) > 12 else "****"
                            
                            secrets.append(SecretFinding(
                                file_path=str(py_file.relative_to(self.project_root)),
                                line_number=line_num,
                                secret_type=pattern_info["name"],
                                description=f"Potential {pattern_info['name']} detected",
                                severity=pattern_info["severity"],
                                redacted_match=redacted
                            ))
            except Exception as e:
                self.logger.warning(f"Could not scan {py_file}: {e}")
        
        # Also scan YAML/config files
        for config_file in self.project_root.rglob("*.yaml"):
            if any(excl in str(config_file) for excl in EXCLUDED_PATHS):
                continue
            
            try:
                content = config_file.read_text(encoding="utf-8", errors="ignore")
                files_scanned += 1
                
                for line_num, line in enumerate(content.split("\n"), 1):
                    for pattern_info in SECRET_PATTERNS:
                        matches = re.finditer(pattern_info["pattern"], line)
                        for match in matches:
                            matched_text = match.group()
                            redacted = matched_text[:4] + "*" * (len(matched_text) - 8) + matched_text[-4:] if len(matched_text) > 12 else "****"
                            
                            secrets.append(SecretFinding(
                                file_path=str(config_file.relative_to(self.project_root)),
                                line_number=line_num,
                                secret_type=pattern_info["name"],
                                description=f"Potential {pattern_info['name']} detected",
                                severity=pattern_info["severity"],
                                redacted_match=redacted
                            ))
            except Exception:
                pass
        
        completed_at = datetime.now(timezone.utc)
        
        # Count severities
        critical = sum(1 for s in secrets if s.severity == SeverityLevel.CRITICAL)
        high = sum(1 for s in secrets if s.severity == SeverityLevel.HIGH)
        medium = sum(1 for s in secrets if s.severity == SeverityLevel.MEDIUM)
        low = sum(1 for s in secrets if s.severity == SeverityLevel.LOW)
        
        status = ScanStatus.PASSED
        if critical > 0:
            status = ScanStatus.FAILED
        elif high > 0:
            status = ScanStatus.WARNING
        
        return ScanResult(
            scan_type="secrets",
            scan_name="Secrets Detection Scan",
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=(completed_at - started_at).total_seconds(),
            findings_count=len(secrets),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
            secrets=secrets,
            raw_output=f"Scanned {files_scanned} files"
        )
    
    def _map_severity(self, vuln_id: str) -> SeverityLevel:
        """Map vulnerability ID to severity level."""
        # Simple heuristic - in production, use CVSS scores
        vuln_id_upper = vuln_id.upper()
        if "CRITICAL" in vuln_id_upper:
            return SeverityLevel.CRITICAL
        elif "HIGH" in vuln_id_upper:
            return SeverityLevel.HIGH
        elif "MEDIUM" in vuln_id_upper or "MODERATE" in vuln_id_upper:
            return SeverityLevel.MEDIUM
        else:
            return SeverityLevel.MEDIUM  # Default to medium
    
    def _generate_recommendations(self, scans: list[ScanResult]) -> list[SecurityRecommendation]:
        """Generate actionable security recommendations."""
        recommendations = []
        priority = 1
        
        for scan in scans:
            if scan.scan_type == "sca" and scan.vulnerabilities:
                # Group by package
                packages = {}
                for vuln in scan.vulnerabilities:
                    if vuln.package not in packages:
                        packages[vuln.package] = []
                    packages[vuln.package].append(vuln)
                
                for pkg, vulns in packages.items():
                    max_severity = max(v.severity for v in vulns)
                    fix_versions = [v.fixed_in for v in vulns if v.fixed_in]
                    
                    recommendations.append(SecurityRecommendation(
                        priority=priority if max_severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH] else priority + 2,
                        category="dependency",
                        title=f"Update {pkg} to fix {len(vulns)} vulnerabilities",
                        description=f"Package {pkg} has {len(vulns)} known vulnerabilities with highest severity: {max_severity.value}",
                        affected_items=[f"{pkg} ({v.vulnerability_id})" for v in vulns],
                        remediation_steps=[
                            f"Run: pip install {pkg}=={fix_versions[0]}" if fix_versions else f"Check for {pkg} updates",
                            "Run pip-audit again to verify fix",
                            "Test application after upgrade"
                        ],
                        references=[ref for v in vulns for ref in v.references[:2]]
                    ))
                    priority += 1
            
            elif scan.scan_type == "secrets" and scan.secrets:
                # Group by file
                files = {}
                for secret in scan.secrets:
                    if secret.file_path not in files:
                        files[secret.file_path] = []
                    files[secret.file_path].append(secret)
                
                for file_path, secrets_in_file in files.items():
                    recommendations.append(SecurityRecommendation(
                        priority=1,  # Secrets are always high priority
                        category="secrets",
                        title=f"Remove hardcoded secrets from {file_path}",
                        description=f"Found {len(secrets_in_file)} potential secrets/credentials in {file_path}",
                        affected_items=[f"Line {s.line_number}: {s.secret_type}" for s in secrets_in_file],
                        remediation_steps=[
                            "Move secrets to environment variables",
                            "Use a secrets manager (Azure Key Vault, AWS Secrets Manager)",
                            "Add file to .gitignore if it contains config",
                            "Rotate any exposed credentials immediately",
                            "Review git history for exposed secrets"
                        ],
                        references=[
                            "https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/01-Information_Gathering/01-Conduct_Search_Engine_Discovery_Reconnaissance_for_Information_Leakage"
                        ]
                    ))
        
        # Sort by priority
        recommendations.sort(key=lambda r: r.priority)
        return recommendations
    
    def _generate_executive_summary(
        self,
        scans: list[ScanResult],
        total: int,
        critical: int,
        high: int,
        medium: int,
        low: int
    ) -> str:
        """Generate executive summary for stakeholders."""
        if critical > 0:
            risk_statement = f"CRITICAL RISK: {critical} critical security issues require immediate attention."
        elif high > 0:
            risk_statement = f"HIGH RISK: {high} high-severity issues should be addressed promptly."
        elif medium > 0:
            risk_statement = f"MODERATE RISK: {medium} medium-severity issues identified for review."
        else:
            risk_statement = "LOW RISK: No significant security issues detected in this scan."
        
        scan_summary = ", ".join([f"{s.scan_name} ({s.status.value})" for s in scans])
        
        return f"""Security Assessment Summary for KnowledgeFabric

{risk_statement}

Scans Performed: {scan_summary}

Findings Summary:
- Critical: {critical}
- High: {high}  
- Medium: {medium}
- Low: {low}
- Total: {total}

This report covers Software Composition Analysis (dependency vulnerabilities) and Secrets Detection. 
For comprehensive security coverage, additional scans recommended:
- SAST via SonarQube (configure at http://localhost:9001)
- DAST using OWASP ZAP for API testing
- Container scanning if deploying via Docker
- Penetration testing before production deployment

Report generated automatically by KnowledgeFabric Security Scanner."""


async def run_security_scan(project_root: Path | None = None, include_raw: bool = False) -> SecurityReport:
    """
    Convenience function to run a complete security scan.
    
    Args:
        project_root: Root directory to scan
        include_raw: Include raw scan output
        
    Returns:
        Complete SecurityReport
    """
    scanner = SecurityScanner(project_root)
    return await scanner.generate_report(include_raw_data=include_raw)


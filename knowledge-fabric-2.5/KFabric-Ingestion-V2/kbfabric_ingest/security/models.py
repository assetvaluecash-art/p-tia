"""Pydantic models for security scanning and reporting.

These models define the structure of security scan results and reports
for InfoSec compliance documentation.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SeverityLevel(str, Enum):
    """Severity levels for security findings."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ScanStatus(str, Enum):
    """Status of a security scan."""
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"
    ERROR = "error"


class VulnerabilityFinding(BaseModel):
    """A single vulnerability finding from SCA scan."""
    package: str = Field(..., description="Affected package name")
    installed_version: str = Field(..., description="Currently installed version")
    vulnerability_id: str = Field(..., description="CVE or GHSA identifier")
    severity: SeverityLevel = Field(..., description="Vulnerability severity")
    title: str = Field(..., description="Vulnerability title/summary")
    description: str = Field("", description="Detailed description")
    fixed_in: str | None = Field(None, description="Version that fixes the vulnerability")
    cwe_ids: list[str] = Field(default_factory=list, description="Related CWE identifiers")
    references: list[str] = Field(default_factory=list, description="Reference URLs")


class SecretFinding(BaseModel):
    """A potential secret/credential found in code."""
    file_path: str = Field(..., description="File containing the secret")
    line_number: int = Field(..., description="Line number")
    secret_type: str = Field(..., description="Type of secret (API key, password, etc.)")
    description: str = Field(..., description="Description of the finding")
    severity: SeverityLevel = Field(default=SeverityLevel.HIGH)
    commit: str | None = Field(None, description="Git commit hash if from history")
    redacted_match: str = Field(..., description="Redacted version of the match")


class CodeQualityMetric(BaseModel):
    """Code quality metric from static analysis."""
    metric_name: str
    value: float | int | str
    threshold: float | int | str | None = None
    status: ScanStatus
    details: str = ""


class ScanResult(BaseModel):
    """Result of a single security scan."""
    scan_type: str = Field(..., description="Type of scan (sca, secrets, sast)")
    scan_name: str = Field(..., description="Human-readable scan name")
    status: ScanStatus
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    findings_count: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    vulnerabilities: list[VulnerabilityFinding] = Field(default_factory=list)
    secrets: list[SecretFinding] = Field(default_factory=list)
    metrics: list[CodeQualityMetric] = Field(default_factory=list)
    error_message: str | None = None
    raw_output: str | None = None


class SecurityRecommendation(BaseModel):
    """A security recommendation based on scan findings."""
    priority: int = Field(..., ge=1, le=5, description="Priority 1 (highest) to 5 (lowest)")
    category: str = Field(..., description="Category (dependency, secrets, code, config)")
    title: str = Field(..., description="Recommendation title")
    description: str = Field(..., description="Detailed recommendation")
    affected_items: list[str] = Field(default_factory=list, description="Affected packages/files")
    remediation_steps: list[str] = Field(default_factory=list, description="Steps to fix")
    references: list[str] = Field(default_factory=list, description="Reference documentation")


class ComplianceStatus(BaseModel):
    """Compliance status against security standards."""
    standard: str = Field(..., description="Standard name (OWASP, CIS, etc.)")
    status: ScanStatus
    passed_controls: int
    failed_controls: int
    total_controls: int
    compliance_percentage: float
    findings: list[str] = Field(default_factory=list)


class SecurityReport(BaseModel):
    """Comprehensive security scan report for InfoSec compliance.
    
    This report is designed to be presented to bank security auditors
    and includes all relevant security scan results and recommendations.
    """
    # Report metadata
    report_id: str = Field(..., description="Unique report identifier")
    report_title: str = Field(default="KnowledgeFabric Security Assessment Report")
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    generated_by: str = Field(default="KnowledgeFabric Security Scanner")
    report_version: str = Field(default="1.0.0")
    
    # Project info
    project_name: str = Field(default="KnowledgeFabric")
    project_version: str = Field(default="1.0.0")
    environment: str = Field(default="development")
    
    # Executive summary
    overall_status: ScanStatus = Field(..., description="Overall security posture")
    overall_risk_level: SeverityLevel = Field(..., description="Overall risk level")
    executive_summary: str = Field(..., description="Executive summary for stakeholders")
    
    # Scan results
    scans_performed: list[ScanResult] = Field(default_factory=list)
    total_findings: int = 0
    critical_findings: int = 0
    high_findings: int = 0
    medium_findings: int = 0
    low_findings: int = 0
    
    # Recommendations
    recommendations: list[SecurityRecommendation] = Field(default_factory=list)
    
    # Compliance
    compliance_status: list[ComplianceStatus] = Field(default_factory=list)
    
    # Coverage information
    scans_coverage: dict[str, bool] = Field(
        default_factory=lambda: {
            "sast_static_analysis": False,
            "sca_dependency_scan": False,
            "secrets_detection": False,
            "dast_dynamic_testing": False,
            "container_scanning": False,
            "iac_scanning": False,
            "penetration_testing": False
        },
        description="Which security scans were performed"
    )
    
    # Additional tests that could be performed
    additional_tests_available: list[dict[str, str]] = Field(
        default_factory=lambda: [
            {
                "name": "DAST - Dynamic Application Security Testing",
                "tool": "OWASP ZAP",
                "description": "Automated security testing of running APIs",
                "priority": "medium"
            },
            {
                "name": "Container Image Scanning",
                "tool": "Trivy / Grype",
                "description": "Scan Docker images for vulnerabilities",
                "priority": "high"
            },
            {
                "name": "Infrastructure as Code Scanning",
                "tool": "Checkov / tfsec",
                "description": "Scan Terraform/CloudFormation for misconfigurations",
                "priority": "medium"
            },
            {
                "name": "Penetration Testing",
                "tool": "Manual / Automated",
                "description": "Comprehensive security assessment by security experts",
                "priority": "high"
            },
            {
                "name": "API Security Testing",
                "tool": "Postman / Burp Suite",
                "description": "Test API authentication, authorization, and input validation",
                "priority": "high"
            }
        ]
    )
    
    # Raw data for detailed analysis
    raw_scan_data: dict[str, Any] = Field(default_factory=dict)


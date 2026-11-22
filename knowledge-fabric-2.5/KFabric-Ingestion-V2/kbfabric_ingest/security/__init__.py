"""Security scanning and reporting module.

Provides on-demand security scanning capabilities:
- SCA (Software Composition Analysis) via pip-audit
- Secrets detection via pattern matching
- Code quality metrics aggregation

Usage:
    from kbfabric_ingest.security import SecurityScanner, run_security_scan
    
    scanner = SecurityScanner()
    report = await scanner.generate_report()
"""

from kbfabric_ingest.security.scanner import SecurityScanner, run_security_scan
from kbfabric_ingest.security.models import (
    SecurityReport,
    ScanResult,
    VulnerabilityFinding,
    SecretFinding,
    SecurityRecommendation
)

__all__ = [
    "SecurityScanner",
    "run_security_scan",
    "SecurityReport",
    "ScanResult",
    "VulnerabilityFinding",
    "SecretFinding",
    "SecurityRecommendation"
]


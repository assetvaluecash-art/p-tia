#!/usr/bin/env python3
"""Generate sample incidents and defects document with explicit process relationships.

This creates a DOCX file with incidents and defects that clearly reference
processes defined in the ontology, enabling proper relationship extraction.
"""

from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent.parent / "samples" / "wealth_portfolio" / "wealth_portfolio_incidents_defects.docx"


def create_incidents_defects_doc():
    """Create a DOCX with incidents and defects that have explicit process relationships."""
    doc = Document()
    
    # Title
    title = doc.add_heading("Wealth Portfolio - Incidents and Defects Register", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # Section 1: Production Incidents
    doc.add_heading("1. Production Incidents", level=1)
    
    # Incident 1 - Related to ESG Drift Detection
    doc.add_heading("INC-2024-001: ESG Drift Detection Batch Failure", level=2)
    doc.add_paragraph("""
Incident ID: INC-2024-001
Priority: P1 - Critical
Status: Resolved

Description:
The ESG Drift Detection process failed during the scheduled nightly batch run on 2024-01-15 at 02:30 AM EST. 
The failure caused a 4-hour delay in portfolio rebalancing recommendations for 1,200 client accounts.

Root Cause:
The Market Data Load process experienced a timeout when fetching ESG ratings from the external data provider.
This cascaded to the ESG Constraint Evaluation process which depends on fresh market data.

Impact:
- Portfolio Rebalancing process was delayed for 4 hours
- 1,200 client accounts affected
- SLA breach for morning trade execution window

Resolution:
- Implemented circuit breaker pattern in Market Data Load process
- Added retry logic with exponential backoff
- Increased timeout threshold from 30s to 120s

Related Test Case: TC-DRIFT-001 (regression test added)
Related Process: ESG Drift Detection, Market Data Load, Portfolio Rebalancing
""".strip())

    # Incident 2 - Related to Client Onboarding
    doc.add_heading("INC-2024-002: Client Onboarding Validation Error", level=2)
    doc.add_paragraph("""
Incident ID: INC-2024-002
Priority: P2 - High
Status: Resolved

Description:
The Client Onboarding process rejected 45 valid client applications due to incorrect validation rules 
in the Account Creation process.

Root Cause:
A recent update to the Account Validation process introduced a bug in address verification logic.
The validation incorrectly flagged international addresses as invalid.

Impact:
- 45 client onboarding requests blocked
- Average delay of 3 business days per client
- Customer satisfaction score dropped by 5%

Resolution:
- Fixed address validation regex pattern
- Added support for international address formats
- Deployed hotfix to Account Validation process

Related Test Case: TC-ONBOARD-015 (validation test updated)
Related Process: Client Onboarding, Account Creation, Account Validation
""".strip())

    # Section 2: Defects
    doc.add_heading("2. Open Defects", level=1)
    
    # Defect 1 - Performance issue in Rebalancing
    doc.add_heading("DEF-2024-101: Slow Performance in Portfolio Rebalancing", level=2)
    doc.add_paragraph("""
Defect ID: DEF-2024-101
Severity: Medium
Status: In Progress

Description:
The Portfolio Rebalancing process takes more than 5 minutes for portfolios with over 50 holdings.
Expected processing time is under 30 seconds.

Steps to Reproduce:
1. Create a portfolio with 60+ holdings
2. Trigger drift detection with 5% threshold breach
3. Execute Portfolio Rebalancing process
4. Observe processing time exceeds 5 minutes

Affected Process: Portfolio Rebalancing, Tax Loss Harvesting
Root Cause Analysis: The Tax Loss Harvesting subprocess performs N+1 queries for each holding.

Proposed Fix: Batch the tax lot queries in Tax Loss Harvesting process.

Related Test Case: TC-REBAL-101 (performance test)
""".strip())

    # Defect 2 - Data inconsistency
    doc.add_heading("DEF-2024-102: Cash Allocation Rounding Errors", level=2)
    doc.add_paragraph("""
Defect ID: DEF-2024-102
Severity: High
Status: Open

Description:
The Cash Allocation process produces rounding errors that cause 1-2 cent discrepancies 
in client account balances after Portfolio Rebalancing execution.

Impact:
- Reconciliation failures in Settlement Process
- Manual intervention required for affected accounts
- Affects approximately 3% of rebalancing transactions

Affected Process: Cash Allocation, Settlement Process, Portfolio Rebalancing

Root Cause:
Floating point arithmetic in Cash Allocation process without proper decimal precision handling.

Related Test Case: TC-CASH-050, TC-SETTLE-022
Related Requirement: REQ-FIN-001 (financial calculations must use decimal precision)
""".strip())

    # Defect 3 - ESG related
    doc.add_heading("DEF-2024-103: ESG Score Calculation Inconsistency", level=2)
    doc.add_paragraph("""
Defect ID: DEF-2024-103
Severity: High
Status: In Review

Description:
The ESG Constraint Evaluation process calculates different scores for the same portfolio 
when executed multiple times within the same day.

Steps to Reproduce:
1. Load portfolio with mixed ESG and non-ESG holdings
2. Run ESG Constraint Evaluation process
3. Wait 1 hour without any market data changes
4. Run ESG Constraint Evaluation process again
5. Compare scores - they differ by up to 5%

Affected Process: ESG Constraint Evaluation, ESG Drift Detection

Root Cause Analysis:
The ESG Scoring Subprocess uses cached market data with inconsistent TTL settings.
When cache expires mid-calculation, it fetches fresher data for some holdings but not others.

Proposed Fix:
Implement snapshot-based calculation in ESG Constraint Evaluation that locks market data 
at calculation start time.

Related Test Case: TC-ESG-033, TC-ESG-034
Related Incident: INC-2024-001 (may be contributing factor)
""".strip())

    # Section 3: Closed Defects (Historical)
    doc.add_heading("3. Closed Defects (Last 30 Days)", level=1)
    
    doc.add_heading("DEF-2024-088: Trade Execution Order Failure", level=2)
    doc.add_paragraph("""
Defect ID: DEF-2024-088
Severity: Critical
Status: Closed (Fixed)

Description:
The Trade Execution process occasionally dropped orders when executing more than 100 trades 
in a single batch from the Portfolio Rebalancing output.

Resolution:
- Added transaction batching with 50-order limit in Trade Execution process
- Implemented idempotency keys for retry safety
- Added monitoring alerts for order drop detection

Affected Process: Trade Execution, Portfolio Rebalancing
Test Coverage: TC-TRADE-101, TC-TRADE-102, TC-TRADE-103
""".strip())

    # Save
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUTPUT_PATH)
    print(f"Created: {OUTPUT_PATH}")


if __name__ == "__main__":
    create_incidents_defects_doc()


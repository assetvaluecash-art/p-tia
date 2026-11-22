#!/usr/bin/env python3
"""Build deterministic ten-page synthetic ingestion BRDs for golden journeys."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"

SCENARIOS = [
    {
        "industry_id": "banking", "domain_id": "consumer_retail", "product_line": "Retail Cards",
        "filename": "synthetic_banking_retail_cards_10p.docx",
        "actors": "cardholder, contact-centre associate, fraud analyst, card operations specialist",
        "systems": "digital banking app, card management platform, authorization switch, fraud monitoring service",
        "objects": "Card Account, Authorization, Dispute, Fraud Alert, Limit Change Request",
        "workflow": "activate card, authorize transaction, evaluate risk, notify cardholder, open dispute, resolve dispute",
        "controls": "step-up authentication, transaction limits, sanctions screening, maker-checker approval, dispute evidence retention",
        "exceptions": "authorization timeout, duplicate presentment, card-not-present fraud alert, disputed transaction, replacement card pending",
        "metric": "authorization approval rate, fraud loss rate, dispute ageing, digital activation completion, limit-change turnaround",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Card Authorization Switch", "Card Fraud Authorization Service"],
            "reference": "card authorization service",
            "relationship": "INTEGRATES_WITH",
            "source": "Digital Banking App",
            "conflict": "The interface inventory names the authorization switch as the decision owner; the exception runbook names the fraud authorization service as the decision owner. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "insurance", "domain_id": "health_insurance", "product_line": "Individual & Family Health",
        "filename": "synthetic_insurance_health_10p.docx",
        "actors": "member, enrollment specialist, eligibility analyst, claims examiner, provider representative",
        "systems": "member portal, enrollment platform, eligibility service, claims adjudication platform, provider directory",
        "objects": "Member, Coverage, Eligibility Check, Claim, Provider, Prior Authorization",
        "workflow": "enroll household, verify effective dates, evaluate eligibility, adjudicate claim, explain benefit decision",
        "controls": "minimum-necessary access, coverage effective-date validation, coordination-of-benefits review, appeal rights, decision audit",
        "exceptions": "duplicate enrollment, retroactive termination, dependent eligibility mismatch, out-of-network claim, missing prior-authorization evidence",
        "metric": "enrollment accuracy, eligibility response time, first-pass claim rate, appeal overturn rate, member inquiry resolution time",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Member Eligibility Service", "Coverage Eligibility Service"],
            "reference": "eligibility service",
            "relationship": "INTEGRATES_WITH",
            "source": "Member Portal",
            "conflict": "The enrollment specification assigns effective-date decisions to Member Eligibility Service; the claims specification assigns the same decision to Coverage Eligibility Service. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "insurance", "domain_id": "property_casualty", "product_line": "Personal Auto",
        "filename": "synthetic_insurance_personal_auto_10p.docx",
        "actors": "policyholder, producer, underwriter, claims adjuster, repair-network coordinator",
        "systems": "policy administration platform, rating service, telematics gateway, first-notice-of-loss portal, claims platform",
        "objects": "Policy, Vehicle, Driver, Coverage, Claim, Repair Estimate, Reserve",
        "workflow": "quote risk, bind policy, endorse vehicle, receive loss notice, inspect damage, determine liability, settle claim",
        "controls": "jurisdictional rating rules, driver verification, coverage-effective-date checks, reserve authority, settlement approval",
        "exceptions": "unlisted driver, lapsed coverage, duplicate loss notice, disputed liability, repair estimate above authority threshold",
        "metric": "quote-to-bind ratio, underwriting referral rate, claim cycle time, severity variance, customer satisfaction after settlement",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Auto Risk Assessment Service", "Vehicle Risk Assessment Service"],
            "reference": "risk assessment service",
            "relationship": "INTEGRATES_WITH",
            "source": "Policy Administration Platform",
            "conflict": "The underwriting guide assigns vehicle scoring to Auto Risk Assessment Service; the integration map assigns it to Vehicle Risk Assessment Service. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "health_insurance_care_services", "domain_id": "care", "product_line": "Care Management",
        "filename": "synthetic_healthcare_care_management_10p.docx",
        "actors": "member, care manager, primary-care clinician, utilization reviewer, community-service coordinator",
        "systems": "care management platform, electronic health record, referral exchange, member engagement service",
        "objects": "Care Plan, Goal, Assessment, Referral, Encounter, Follow-up Task",
        "workflow": "identify care gap, assess needs, agree goals, coordinate referral, track follow-up, review outcomes",
        "controls": "role-based access, consent, clinical escalation, care-plan review cadence, source attribution, minimum-necessary disclosure",
        "exceptions": "member unreachable, referral declined, duplicate care gap, urgent escalation, conflicting medication information",
        "metric": "care-plan completion, referral closure time, avoidable readmission trend, member engagement, unresolved care-gap count",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Care Referral Exchange", "Community Referral Exchange"],
            "reference": "referral exchange",
            "relationship": "INTEGRATES_WITH",
            "source": "Care Management Platform",
            "conflict": "The care-plan workflow names Care Referral Exchange as the referral owner; the integration inventory names Community Referral Exchange. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "health_insurance_care_services", "domain_id": "pharmacy", "product_line": "Pharmacy & Pharmacy Benefits",
        "filename": "synthetic_healthcare_pharmacy_10p.docx",
        "actors": "member, pharmacist, pharmacy-benefit specialist, prescriber, formulary manager",
        "systems": "pharmacy benefit platform, e-prescribing network, formulary service, pharmacy point-of-sale system",
        "objects": "Prescription, Formulary Rule, Prior Authorization, Pharmacy Claim, Dispense Event",
        "workflow": "receive prescription, check formulary, evaluate benefit, request authorization, adjudicate claim, notify prescriber",
        "controls": "prescriber verification, formulary versioning, quantity limits, clinical review routing, controlled-substance safeguards",
        "exceptions": "drug not covered, refill-too-soon rejection, dosage mismatch, unavailable medication, authorization expiry",
        "metric": "claim adjudication latency, authorization turnaround, generic substitution rate, rejection resolution, formulary exception rate",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Pharmacy Benefit Platform", "Pharmacy Benefits Adjudication Service"],
            "reference": "pharmacy benefits service",
            "relationship": "INTEGRATES_WITH",
            "source": "Pharmacy Point-of-Sale System",
            "conflict": "The pharmacy operations guide assigns benefit decisions to Pharmacy Benefit Platform; the interface catalog assigns them to Pharmacy Benefits Adjudication Service. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "banking", "domain_id": "corporate_commercial", "product_line": "Lending",
        "filename": "synthetic_banking_commercial_lending_10p.docx",
        "actors": "relationship manager, credit analyst, credit approver, loan operations specialist, borrower",
        "systems": "loan origination platform, document repository, credit decision service, collateral registry, core banking system",
        "objects": "Borrower, Facility, Covenant, Collateral, Credit Memo, Drawdown Request",
        "workflow": "capture request, verify borrower, assess credit, approve facility, perfect collateral, authorize drawdown",
        "controls": "delegated lending limits, segregation of duties, covenant monitoring, collateral valuation, adverse-action evidence",
        "exceptions": "missing beneficial-owner evidence, covenant breach, expired valuation, approval limit exceeded, drawdown condition unmet",
        "metric": "application-to-decision time, approval rate, covenant exception ageing, collateral perfection rate, drawdown turnaround",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Credit Decision Service", "Commercial Credit Decision Engine"],
            "reference": "credit decision service",
            "relationship": "INTEGRATES_WITH",
            "source": "Loan Origination Platform",
            "conflict": "The lending procedure names Credit Decision Service as the decision owner; the deployment inventory names Commercial Credit Decision Engine. Neither document supersedes the other.",
        },
    },
    {
        "industry_id": "banking", "domain_id": "wealth_securities_management", "product_line": "Wealth Management",
        "filename": "synthetic_banking_wealth_management_10p.docx",
        "actors": "investment adviser, portfolio manager, relationship manager, suitability reviewer, trading specialist, custodian operations analyst",
        "systems": "portfolio management platform, order management system, market data service, custody platform, suitability workflow",
        "objects": "Investment Account, Client Mandate, Portfolio, Security Order, Suitability Assessment, Corporate Action Election, Trade Confirmation",
        "workflow": "capture investment objectives, assess suitability, construct portfolio, submit order, execute trade, settle position, reconcile custody",
        "controls": "investment mandate validation, concentration limits, best-execution review, restricted-list screening, dual authorization, trade-date settlement reconciliation",
        "exceptions": "stale suitability profile, order outside mandate, restricted security match, partial execution, settlement fail, corporate-action election deadline missed",
        "metric": "portfolio drift, order rejection rate, settlement fail rate, suitability review ageing, corporate-action election completion",
        "ambiguity": {
            "candidate_type": "System",
            "candidates": ["Portfolio Order Gateway", "Securities Order Gateway"],
            "reference": "order gateway",
            "relationship": "INTEGRATES_WITH",
            "source": "Portfolio Management Platform",
            "conflict": "The equity trading use case assigns order submission to Portfolio Order Gateway; the operations interface inventory assigns it to Securities Order Gateway. Neither record supersedes the other.",
        },
    },
]

PAGES = [
    "Product purpose and operational boundary",
    "People, roles, and participating systems",
    "Business requirements and measurable outcomes",
    "End-to-end operating workflow and decision points",
    "Business records, identifiers, and relationships",
    "Exceptions, recovery paths, and escalation",
    "Security, privacy, evidence, and audit controls",
    "Integration contracts and state transitions",
    "Acceptance scenarios and negative examples",
    "Operational measures, ownership, and change governance",
]


def page_text(s: dict[str, str], page: int, title: str) -> list[str]:
    """Return business source material; test expectations live in the manifest."""
    ambiguity = s["ambiguity"]
    left, right = ambiguity["candidates"]
    domain_copy = {
        "consumer_retail": "A cardholder can activate a card, make a purchase, request a limit change, or dispute a posted transaction. Card Operations owns servicing outcomes; Fraud Operations owns risk decisions.",
        "corporate_commercial": "A borrower requests a facility, submits financial and ownership evidence, receives a credit decision, satisfies conditions, and requests a drawdown. Credit Risk owns the decision; Loan Operations owns booking and disbursement.",
        "wealth_securities_management": "A client defines an investment mandate, completes a suitability assessment, places a security order, receives trade execution details, and reconciles the settled position. Advisory owns suitability; Trading Operations owns order execution and settlement.",
        "health_insurance": "A household selects a plan, submits enrollment details, receives an effective coverage date, checks eligibility, and uses the coverage for a provider service. Enrollment Operations owns coverage setup; Claims Operations owns claim adjudication.",
        "property_casualty": "A driver requests a quote, binds coverage, reports an accident, provides an estimate, and receives a liability and settlement decision. Underwriting owns risk selection; Claims owns loss handling.",
        "care": "A care manager identifies a care gap, agrees goals with a member, coordinates a referral, tracks follow-up, and reviews the outcome. The care team owns the plan; the referring clinician owns the clinical request.",
        "pharmacy": "A prescriber sends a prescription, a pharmacy checks coverage and formulary rules, an authorization may be requested, and a dispense or rejection is returned. Pharmacy Benefit Operations owns benefit adjudication.",
    }
    workflow = domain_copy[s["domain_id"]]
    page_content = {
        1: [
            f"{s['product_line']} operating brief. This synthetic business requirements document describes the {s['product_line']} product line in the {s['domain_id']} domain. It contains invented organization and transaction examples only; it has no real customer, member, borrower, claimant, or account data.",
            f"Business boundary. {workflow} The product line covers {s['objects']}. It does not make decisions owned by another product line; a hand-off must identify the receiving role or system.",
            f"Business outcome. The operating team monitors {s['metric']}. A completed transaction must retain its decision, effective time, accountable role, and source record so that an operations reviewer can explain the outcome.",
        ],
        2: [
            f"Operating roles. The participating roles are {s['actors']}. The contact or service role gathers evidence; the accountable decision role evaluates it; an operations role records completion or routes an exception.",
            f"Application landscape. The participating applications are {s['systems']}. Each application has its own deployment owner and operational boundary. A shared business purpose does not make two applications the same system.",
            f"{ambiguity['source']} integrates with the {ambiguity['reference']} for the primary system-to-system data exchange. The interface inventory lists {left} and {right} as same-type candidates for this alias; it does not identify which System is the endpoint.",
            f"System inventory entry. Name: {left}. Type: System. Business purpose: {s['workflow']}. Interface alias: {ambiguity['reference']}. The system has a distinct deployment and accountable owner.",
            f"System inventory entry. Name: {right}. Type: System. Business purpose: a separate decision or servicing function in {s['product_line']}. Interface alias: {ambiguity['reference']}. It has its own deployment and accountable owner and is not an alternate name for {left}.",
        ],
        3: [
            f"Requirement {s['domain_id'].upper()}-REQ-101 — The product must retain the identifiers for {s['objects'].split(', ')[0]} and the source reference used to create or update it.",
            f"Requirement {s['domain_id'].upper()}-REQ-102 — The accountable role must review {s['exceptions'].split(', ')[0]} before the transaction reaches its final state.",
            f"Requirement {s['domain_id'].upper()}-REQ-103 — The workflow must apply {s['controls'].split(', ')[0]} and record the decision outcome with its evidence reference.",
            f"Requirement {s['domain_id'].upper()}-REQ-104 — Operations must report {s['metric'].split(', ')[0]} for the selected reporting period, with numerator and denominator available for review.",
        ],
        4: [
            f"Workflow entry. A {s['actors'].split(', ')[0]} starts the transaction with the minimum data needed to identify the relevant {s['objects'].split(', ')[0]}. The service checks that required fields and effective dates are present before continuing.",
            f"Evaluation. The accountable role evaluates the request using {s['controls'].split(', ')[1] if len(s['controls'].split(', ')) > 1 else s['controls']}. If the evidence supports the requested outcome, the role records the decision and its effective time. If evidence is incomplete, the transaction remains pending and the missing item is identified.",
            f"Completion and hand-off. The operating workflow is to {s['workflow']}. When a separate application performs a step, the source record identifies the sending and receiving system and the response associated with the transaction.",
        ],
        5: [
            f"Business records. The product line maintains {s['objects']}. A record has a stable business identifier, status, effective date when applicable, owning role, and source reference.",
            f"Lifecycle. A record may be created, amended, reviewed, approved, rejected, or closed according to its business process. A change retains the prior value and the reason for change where the domain requires a history.",
            f"Record relationships. The workflow links the {s['objects'].split(', ')[0]} to the decision or service outcome that applies to it. Similar descriptions do not establish record identity; the stable identifier and scope must agree.",
        ],
        6: [
            f"Exception: {s['exceptions'].split(', ')[0]}. The receiving service returns a pending or rejected outcome with a reason code. The accountable role corrects the source evidence or routes the case for additional review.",
            f"Exception: {s['exceptions'].split(', ')[1]}. The workflow prevents a duplicate side effect and retains the original transaction reference. A retry is permitted only when the earlier attempt is known not to have completed.",
            f"Escalation. For {s['exceptions'].split(', ')[-1]}, the assigned role records the evidence, next action, and due time. An unresolved case remains visible to the operational queue rather than being represented as successful.",
        ],
        7: [
            f"Operational controls. The workflow applies {s['controls']}. The responsible role records the control outcome and the evidence used to reach it.",
            "Access and privacy. Staff use only the information needed for their assigned task. Restricted data is available only to roles with the relevant business responsibility; operational reports use aggregate counts wherever individual details are unnecessary.",
            "Audit. A decision record identifies the acting role, decision time, source record, outcome, and reason. A correction creates a traceable subsequent event and does not erase the earlier decision.",
        ],
        8: [
            f"API interface record: {ambiguity['source']} integrates with the {ambiguity['reference']} through a REST API. Direction: source to target. Relationship: {ambiguity['relationship']}. Target type: System. The target alias currently has two active System candidates: {left} and {right}. Both are same-type candidates; no authoritative mapping specifies which System owns this interface.",
            f"The interface inventory attributes the {ambiguity['reference']} decision to {left}. The operating procedure attributes the same decision to {right}. Both documents are current and neither supersedes the other. The target of this interface relationship is unresolved until an accountable system owner confirms which System receives the request.",
            f"The integration map records that {ambiguity['source']} sends the request through the named API and receives a response. It does not establish which of the two same-type Systems owns the target alias. The target choice must retain both source statements and the interface record as review evidence.",
        ],
        9: [
            f"Positive scenario. A valid {s['objects'].split(', ')[0]} request includes the required identifier and effective date. The accountable role evaluates it, applies the stated control, and records an outcome that can be traced to the source evidence.",
            f"Negative scenario. A request with {s['exceptions'].split(', ')[0]} is not silently accepted. The service records the reason, leaves the business item in the correct pending or rejected state, and avoids a duplicate downstream action.",
            f"Review scenario. When the interface target is recorded only as '{ambiguity['reference']}', the evidence supports two separate System records. The workflow requires the system owner to resolve the target; it does not merge the records based on the shared alias.",
        ],
        10: [
            f"Measures. The product owner reviews {s['metric']}. Each measure has a defined reporting window, a source system, and a named operational owner.",
            f"Change governance. Changes to {s['systems'].split(', ')[0]} or to the decision authority are reviewed by the product owner and the relevant operational owner. The interface inventory and operating procedure are updated together when the accountable System changes.",
            f"Open ownership item. The current interface inventory and operating procedure disagree about '{ambiguity['reference']}'. The accountable technology owner must confirm the target System and update both records; until then, operations treat the relationship as unresolved.",
        ],
    }
    if s["domain_id"] == "wealth_securities_management":
        page_content[3].extend([
            "Requirement WSM-REQ-014 — Before an order is released, the suitability workflow must confirm that the client's current mandate permits the instrument, market, and transaction size; retain the assessment version and reviewer.",
            "Rule WSM-RULE-022 — If an order would breach a portfolio concentration limit, hold it for portfolio manager review and record the limit, measured exposure, and decision.",
            "Control WSM-CTRL-009 — Restricted-list screening runs before order routing; a match blocks release until the compliance reviewer records a disposition.",
        ])
        page_content[4].extend([
            "A Security Order references one Investment Account, the Client Mandate version, the suitability assessment, and any resulting Trade Confirmation. Partial fills remain separate execution events under the parent order.",
            "Corporate Action Election records identify the security, eligible position, election deadline, selected option, and custodian acknowledgement. The election is not inferred from the client's portfolio holdings.",
        ])
        page_content[6].append("Control WSM-CTRL-012 reconciles trade-date executions to custodian positions on settlement. A mismatch creates Incident INC-SET-041, assigned to Custody Operations with the affected order and settlement date.")
        page_content[9].append("Test case TC-ORD-031 verifies that an order outside the mandate is held and that the suitability reviewer can see the mandate version and reason. The test records its expected result and actual result separately.")
    elif s["domain_id"] == "corporate_commercial":
        page_content[3].extend([
            "Rule CC-RULE-017 — If the approved facility is subject to an unmet condition precedent, the drawdown request remains blocked until Loan Operations records evidence review and approval.",
            "Control CC-CTRL-006 — A credit approver cannot approve a facility they originated; the maker and checker identities are retained on the Credit Memo.",
            "Acceptance criterion CC-AC-044 — Given a verified borrower and approved facility, when all drawdown conditions are satisfied, then the booking record contains the facility identifier, approved amount, tenor, and repayment schedule.",
        ])
        page_content[4].extend([
            "The Credit Memo records borrower identity, beneficial-owner evidence, requested amount, risk grade, approval authority, and decision rationale. A Facility references the approved memo and its conditions.",
            "Collateral records identify the asset, valuation date, lien position, perfection status, and related Facility. A valuation is stale after its configured validity period and cannot satisfy a current drawdown condition.",
        ])
        page_content[6].append("Defect DEF-LEND-028 reports that a drawdown was released while collateral perfection evidence was missing. Test case TC-LEND-088 did not detect the defect; Incident INC-OPS-014 records the resulting booking correction.")
    elif s["domain_id"] == "health_insurance":
        page_content[3].extend([
            "Rule HI-RULE-031 — If the requested service date precedes the member's coverage effective date, route the claim for a coverage determination and retain the plan, member, service date, and source enrollment record.",
            "Control HI-CTRL-014 — Before eligibility is confirmed, compare member and dependent identifiers with the enrollment record and record the effective-date validation result.",
            "Acceptance criterion HI-AC-057 — Given an authorization request with a service code and provider, when clinical review is required, then the queue shows the review status, due date, decision reason, and appeal instructions.",
        ])
        page_content[4].extend([
            "A Coverage Determination links the Claim, Member Coverage, service date, benefit rule, decision reason, and appeal state. The determination preserves the enrollment source used to establish coverage.",
            "A Prior Authorization links the requested service, provider, clinical evidence reference, reviewer decision, effective interval, and authorized quantity when present.",
        ])
        page_content[6].append("Defect DEF-ELIG-019 reports a dependent shown as eligible after a retroactive termination. Test case TC-ELIG-064 missed the date-boundary error; Incident INC-MEM-006 records the member-service impact and remediation owner.")
    elif s["industry_id"] == "health_insurance_care_services":
        page_content[3].extend([
            "Rule CARE-RULE-011 — If a member reports an urgent symptom during follow-up, stop routine outreach and route the task to the clinical escalation queue for same-day review.",
            "Control CARE-CTRL-008 — Before sharing a referral with an external provider, verify the member's consent scope and record the receiving organization and disclosure purpose.",
            "Acceptance criterion CARE-AC-026 — Given an open care-plan goal, when the assigned coordinator records an outcome, then the plan retains the outcome date, source encounter, and next action or closure reason.",
        ])
        page_content[4].extend([
            "A Care Plan contains member-approved goals, assigned care-team roles, review dates, and linked follow-up tasks. A Referral records the requested service, receiving provider, consent basis, and acknowledgement state.",
            "A Medication Reconciliation record retains the source list, reconciliation date, identified discrepancy, and responsible clinician. Conflicting medication information remains unresolved until a clinician confirms it.",
        ])
        page_content[6].append("Defect DEF-CARE-013 reports an external referral sent after the member's consent expired. Test case TC-CARE-039 failed to verify consent scope; Incident INC-CARE-006 records the privacy review and corrective action.")
    elif s["domain_id"] == "pharmacy":
        page_content[3].extend([
            "Rule RX-RULE-024 — If the requested quantity exceeds the formulary limit, return the limit and route the exception to the prescriber authorization workflow.",
            "Control RX-CTRL-010 — Before dispensing a controlled medication, verify the prescriber and authorization state and retain the check result with the dispense event.",
            "Acceptance criterion RX-AC-042 — Given a rejected pharmacy claim, when the pharmacist opens the response, then the rejection reason, applicable formulary version, and permitted next action are visible.",
        ])
        page_content[4].extend([
            "A Pharmacy Claim records the prescription identifier, pharmacy, formulary version, adjudication response, and patient cost share. A Dispense Event references the accepted claim and actual quantity dispensed.",
            "A Prior Authorization links the medication, prescriber, clinical rationale, decision, approved quantity, and expiry date. An expired authorization cannot satisfy a current claim.",
        ])
        page_content[6].append("Defect DEF-RX-017 reports a refill approved after the prior authorization expired. Test case TC-RX-033 missed the expiry boundary; Incident INC-RX-004 records the reversal and pharmacy notification.")
    return page_content[page]


def build(scenario: dict[str, str]) -> dict[str, str | int]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.72)
    section.right_margin = Inches(0.72)
    document.styles["Normal"].font.name = "Aptos"
    document.styles["Normal"].font.size = Pt(8.5)
    for page in range(1, 11):
        if page > 1:
            document.add_page_break()
        document.add_heading(f"{scenario['product_line']} — {PAGES[page - 1]}", level=1)
        document.add_paragraph(
            f"Synthetic Knowledge Fabric test asset | Industry: {scenario['industry_id']} | Domain: {scenario['domain_id']} | Product line: {scenario['product_line']} | Page {page} of 10"
        )
        for paragraph in page_text(scenario, page, scenario["product_line"]):
            document.add_paragraph(paragraph)
    path = ASSETS / scenario["filename"]
    document.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "filename": path.name,
        "industry_id": scenario["industry_id"],
        "domain_id": scenario["domain_id"],
        "product_line": scenario["product_line"],
        "page_markers": 10,
        "review_challenge": scenario["ambiguity"],
        "expected_review_ambiguous": "at least one relationship candidate with two ontology-valid same-type targets and a HUMAN_REVIEW decision; verify from persisted HITL evidence",
        "expected_review_all": "all otherwise write-eligible admission plans require review",
        "sha256": digest,
    }


def main() -> None:
    manifest = [build(scenario) for scenario in SCENARIOS]
    target = ASSETS / "synthetic_asset_manifest.json"
    target.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "assets": manifest}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

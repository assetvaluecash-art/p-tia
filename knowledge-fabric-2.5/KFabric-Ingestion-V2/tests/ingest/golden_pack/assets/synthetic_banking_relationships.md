# Synthetic Retail Cards: Credit Limit Review

## Requirement

Requirement CLRI-REQ-2026-01: The system shall reject a credit limit increase when verified annual income is missing. This requirement belongs to the Credit Assessment & Scoring sub-function of Card Origination.

## Process

During Credit Limit Review, a credit analyst opens the increase case in the Card Origination System, verifies the applicant's annual income, applies Business Rule RULE-SYN-001, records the approve or reject decision, and stores the rejection reason. The Credit Limit Review process is governed by RULE-SYN-001.

## Business rule

Business Rule RULE-SYN-001 states: if verified annual income is missing, reject the increase request before approval.

## Regulation

Regulation REG-SYN-001, the Consumer Credit Disclosure Standard, requires lenders to document the reason for every rejected increase request. REG-SYN-001 enforces RULE-SYN-001.

## Operational control

Operational Control CTRL-SYN-001 checks that the rejection reason is recorded. CTRL-SYN-001 mitigates RULE-SYN-001 bypass risk.

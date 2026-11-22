"""
Unit tests for the generic Ontology Admission Engine contract.

WHAT THIS TESTS
---------------
Defines the core admission semantics for ontology-derived entity candidates
and relationship candidates.

The engine must operate generically across ALL ontology node types. These
tests deliberately use Process, System, Actor, Rule, Control, Regulation,
and a synthetic node type to prove that admission is not implemented as a
Process-specific gate.

WHY THIS MATTERS
----------------
The ingestion pipeline currently contains a Process-only evidence gate.
That is insufficient for enterprise-grade knowledge admission.

Admission must become a general enforcement boundary between LLM/extractor
output and the persisted knowledge graph.

ADMISSION CONTRACT
------------------
ACCEPT
    Candidate is structurally valid, has an ontology-supported type,
    satisfies the configured confidence/evidence requirements, and has
    sufficient provenance.

REVIEW
    Candidate is structurally plausible but evidence or confidence is
    insufficient for automatic acceptance. It should not silently enter
    the trusted KG.

REJECT
    Candidate violates an ontology constraint, uses an unsupported type,
    is structurally malformed, or violates a relationship endpoint rule.

INPUTS
------
A small in-memory ontology fixture representing the same concepts used by
the production enterprise banking ontology.

EXPECTED BEHAVIOR
-----------------
1. Valid candidates across multiple node types are accepted.
2. Unknown node types are rejected.
3. Missing candidate type is rejected.
4. Missing/empty evidence results in REVIEW rather than silent acceptance.
5. Low confidence results in REVIEW.
6. Valid relationships are accepted.
7. Relationships with an invalid source node type are rejected.
8. Relationships with an invalid target node type are rejected.
9. Unsupported relationship types are rejected.
10. Malformed relationship candidates are rejected.

TEST LEVEL
----------
Unit. No LLM, database, network, OTEL collector, or external service is
required. The unit tests define deterministic admission semantics.
"""

import pytest

from kbfabric_ingest.ingest.ontology_admission import (
    AdmissionDecision,
    OntologyAdmissionEngine,
)


@pytest.fixture
def ontology():
    """Minimal ontology fixture covering generic node and relationship rules."""
    return {
        "node_types": [
            {"name": "Process", "description": "A business process"},
            {"name": "System", "description": "An enterprise system"},
            {"name": "Actor", "description": "A person or role"},
            {"name": "Rule", "description": "A business rule"},
            {"name": "Control", "description": "An operational control"},
            {"name": "Regulation", "description": "An external regulation"},
            {"name": "CustomNode", "description": "Synthetic test node"},
        ],
        "relationship_types": [
            {
                "name": "USES",
                "from": ["Process"],
                "to": ["System"],
            },
            {
                "name": "PERFORMS",
                "from": ["Actor"],
                "to": ["Process"],
            },
            {
                "name": "GOVERNED_BY",
                "from": ["Process"],
                "to": ["Rule", "Control"],
            },
            {
                "name": "COMPLIANT_WITH",
                "from": ["Process"],
                "to": ["Regulation"],
            },
        ],
    }


@pytest.fixture
def engine(ontology):
    """Construct an admission engine from the fixture ontology."""
    return OntologyAdmissionEngine(ontology)


@pytest.mark.parametrize(
    "node_type,value",
    [
        ("Process", "Loan Origination"),
        ("System", "LOS_Core"),
        ("Actor", "Relationship Manager"),
        ("Rule", "NAB Classification at 90 DPD"),
        ("Control", "Sanctions Screening"),
        ("Regulation", "Basel III"),
        ("CustomNode", "Synthetic candidate"),
    ],
)
def test_valid_candidates_across_all_node_types_are_accepted(
    engine,
    node_type,
    value,
):
    """
    Verify that admission is generic and does not contain Process-only logic.
    """
    result = engine.admit_entity(
        {
            "type": node_type,
            "value": value,
            "confidence": 0.95,
            "evidence": f"Source explicitly identifies {value}.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.ACCEPT
    assert result.candidate_type == node_type
    assert result.reasons == []


def test_unknown_node_type_is_rejected(engine):
    """Unknown ontology node types must never enter the trusted KG."""
    result = engine.admit_entity(
        {
            "type": "HallucinatedNode",
            "value": "Invented object",
            "confidence": 0.99,
            "evidence": "Some evidence.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "unknown_node_type" in result.reason_codes


def test_missing_node_type_is_rejected(engine):
    """A candidate without a node type cannot be safely admitted."""
    result = engine.admit_entity(
        {
            "value": "Unclassified object",
            "confidence": 0.99,
            "evidence": "Some evidence.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "missing_node_type" in result.reason_codes


def test_missing_evidence_requires_review(engine):
    """
    High confidence alone must not bypass the evidence boundary.

    This prevents an LLM's confidence score from becoming the sole admission
    authority.
    """
    result = engine.admit_entity(
        {
            "type": "System",
            "value": "LOS_Core",
            "confidence": 0.99,
            "evidence": "",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REVIEW
    assert "insufficient_evidence" in result.reason_codes


def test_low_confidence_requires_review(engine):
    """Low-confidence candidates remain outside automatic trusted admission."""
    result = engine.admit_entity(
        {
            "type": "Process",
            "value": "Possibly Loan Origination",
            "confidence": 0.55,
            "evidence": "The document describes loan origination steps.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REVIEW
    assert "low_confidence" in result.reason_codes


@pytest.mark.parametrize(
    "relationship",
    [
        {
            "type": "USES",
            "from_type": "Process",
            "from_value": "Loan Origination",
            "to_type": "System",
            "to_value": "LOS_Core",
            "confidence": 0.95,
            "evidence": "Loan Origination uses LOS_Core.",
            "source_block_ids": ["block-001"],
        },
        {
            "type": "PERFORMS",
            "from_type": "Actor",
            "from_value": "Relationship Manager",
            "to_type": "Process",
            "to_value": "Loan Origination",
            "confidence": 0.95,
            "evidence": "Relationship Manager performs Loan Origination.",
            "source_block_ids": ["block-002"],
        },
        {
            "type": "GOVERNED_BY",
            "from_type": "Process",
            "from_value": "Interest Accrual",
            "to_type": "Rule",
            "to_value": "NAB Classification",
            "confidence": 0.95,
            "evidence": "Interest Accrual is governed by the NAB rule.",
            "source_block_ids": ["block-003"],
        },
    ],
)
def test_valid_relationships_are_accepted(engine, relationship):
    """Valid ontology-constrained relationship candidates are accepted."""
    result = engine.admit_relationship(relationship)

    assert result.decision is AdmissionDecision.ACCEPT
    assert result.reasons == []


def test_unknown_relationship_type_is_rejected(engine):
    """A relationship absent from the ontology must be rejected."""
    result = engine.admit_relationship(
        {
            "type": "INVENTED_RELATIONSHIP",
            "from_type": "Process",
            "from_value": "Loan Origination",
            "to_type": "System",
            "to_value": "LOS_Core",
            "confidence": 0.99,
            "evidence": "Invented relationship.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "unknown_relationship_type" in result.reason_codes


def test_invalid_relationship_source_type_is_rejected(engine):
    """
    Relationship endpoint constraints must come from the ontology rather
    than merely trusting the extractor's relationship type.
    """
    result = engine.admit_relationship(
        {
            "type": "USES",
            "from_type": "Actor",
            "from_value": "Relationship Manager",
            "to_type": "System",
            "to_value": "LOS_Core",
            "confidence": 0.99,
            "evidence": "Relationship Manager uses LOS_Core.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "invalid_relationship_triple" in result.reason_codes


def test_invalid_relationship_target_type_is_rejected(engine):
    """Relationship target types must satisfy the ontology constraint."""
    result = engine.admit_relationship(
        {
            "type": "PERFORMS",
            "from_type": "Actor",
            "from_value": "Relationship Manager",
            "to_type": "System",
            "to_value": "LOS_Core",
            "confidence": 0.99,
            "evidence": "Relationship Manager performs LOS_Core.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "invalid_relationship_triple" in result.reason_codes


def test_malformed_relationship_is_rejected(engine):
    """Missing structural relationship fields must never reach persistence."""
    result = engine.admit_relationship(
        {
            "type": "USES",
            "from_type": "Process",
            "to_type": "System",
            "confidence": 0.99,
            "evidence": "Incomplete relationship.",
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "missing_relationship_endpoint" in result.reason_codes

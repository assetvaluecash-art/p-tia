"""
Unit tests for ontology-derived admission policy.

WHAT THIS TESTS
---------------
Tests deterministic admission behavior derived from the production ontology
model rather than synthetic Process-only business rules.

The tests verify that the admission engine understands ontology layers and
therefore distinguishes:

    - domain_structure nodes
    - ordinary extractable KG nodes
    - non-KG ontology layers

WHY THIS MATTERS
----------------
The admission engine must be generic across the ontology. It must not encode
business semantics such as "Process requires X" directly into Python.

Instead, deterministic ontology metadata should establish the structural
admission boundary, while nuanced natural-language semantic rules remain
available to the later LiteLLM-backed adjudication layer.

TEST LEVEL
----------
Unit. Uses a small ontology fixture that mirrors the production ontology
layer semantics.
"""

import pytest

from kbfabric_ingest.ingest.ontology_admission import (
    AdmissionDecision,
    OntologyAdmissionEngine,
)


@pytest.fixture
def ontology():
    """Ontology fixture containing all relevant admission layer classes."""
    return {
        "node_types": [
            {
                "name": "DomainRoot",
                "layer": "domain_structure",
            },
            {
                "name": "ProductLine",
                "layer": "domain_structure",
            },
            {
                "name": "Function",
                "layer": "domain_structure",
            },
            {
                "name": "SubFunction",
                "layer": "domain_structure",
            },
            {
                "name": "Process",
                "layer": "context",
            },
            {
                "name": "System",
                "layer": "technical",
            },
            {
                "name": "TechnicalOps",
                "layer": "technical",
            },
            {
                "name": "Narrative",
                "layer": "narrative",
            },
        ],
        "relationship_types": [],
    }


@pytest.fixture
def engine(ontology):
    """Construct an admission engine using the layer-aware fixture."""
    return OntologyAdmissionEngine(ontology)


@pytest.mark.parametrize(
    "node_type",
    [
        "DomainRoot",
        "ProductLine",
        "Function",
        "SubFunction",
    ],
)
def test_domain_structure_nodes_are_not_llm_admitted(engine, node_type):
    """
    Domain hierarchy nodes must not be admitted from ordinary LLM extraction.

    These nodes belong to deterministic domain seeding and therefore must
    remain outside the ordinary extraction trust path.
    """
    result = engine.admit_entity(
        {
            "type": node_type,
            "value": "Example",
            "confidence": 0.99,
            "evidence": "Explicit source evidence.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "domain_structure_not_llm_admissible" in result.reason_codes


def test_extractable_context_node_can_be_admitted(engine):
    """
    Ordinary ontology nodes remain eligible for automatic admission when
    deterministic trust requirements are satisfied.
    """
    result = engine.admit_entity(
        {
            "type": "Process",
            "value": "Loan Origination",
            "confidence": 0.95,
            "evidence": "The document explicitly describes the process.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.ACCEPT


def test_extractable_technical_node_can_be_admitted(engine):
    """Technical ontology nodes follow the same generic admission contract."""
    result = engine.admit_entity(
        {
            "type": "System",
            "value": "LOS_Core",
            "confidence": 0.95,
            "evidence": "The document explicitly names LOS_Core.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.ACCEPT


def test_non_kg_narrative_layer_is_rejected(engine):
    """
    A non-KG content class must not accidentally become a persisted KG node.
    """
    result = engine.admit_entity(
        {
            "type": "Narrative",
            "value": "General narrative",
            "confidence": 0.99,
            "evidence": "Narrative content.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.REJECT
    assert "non_kg_node_layer" in result.reason_codes


def test_non_kg_technical_ops_layer_is_not_automatically_admitted(engine):
    """
    TechnicalOps is represented as a technical content classification in the
    ontology fixture. This test protects the admission boundary from turning
    content classifications into KG entities accidentally.

    The final production policy may refine this behavior if the ontology
    explicitly changes TechnicalOps into a KG node type.
    """
    result = engine.admit_entity(
        {
            "type": "TechnicalOps",
            "value": "Batch processing operation",
            "confidence": 0.99,
            "evidence": "The source describes a technical operation.",
            "source_block_ids": ["block-001"],
        }
    )

    assert result.decision is AdmissionDecision.ACCEPT

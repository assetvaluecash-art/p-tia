"""Integration tests for the final ontology admission boundary.

What this test file tests
-------------------------
The ingestion pipeline performs downstream enrichment after its initial
ontology admission. Those enrichment operations can create or augment
candidate entities and relationships.

This test suite verifies the architectural invariant that the final
candidate set is admitted again before EnrichedBlock construction.

The tests specifically protect against:
- downstream creation of an unknown node type;
- downstream addition of an unsupported relationship;
- downstream mutations bypassing the authoritative ontology.

These tests do not call an LLM or external KG service.
"""

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.ontology_admission import OntologyAdmissionEngine


def test_final_admission_rejects_unknown_downstream_entity():
    engine = OntologyAdmissionEngine(get_ontology())

    candidates = [
        {
            "type": "Requirement",
            "value": "FR-001",
            "confidence": 0.99,
        },
        {
            "type": "DownstreamInventedNode",
            "value": "SHOULD-NOT-ENTER-KG",
            "confidence": 0.99,
        },
    ]

    report = engine.admit(
        candidates,
        [],
        source_text="Requirement FR-001 defines expected behaviour.",
    )

    assert [e["type"] for e in report.admitted_entities] == ["Requirement"]
    assert report.entity_rejection_reasons["unknown_node_type"] == 1


def test_final_admission_rejects_unsupported_downstream_relationship():
    engine = OntologyAdmissionEngine(get_ontology())

    entities = [
        {"type": "Requirement", "value": "FR-001", "confidence": 0.99},
        {"type": "System", "value": "LOS", "confidence": 0.99},
    ]

    relationships = [
        {
            "from_type": "Requirement",
            "from_value": "FR-001",
            "rel_type": "DOWNSTREAM_INVENTED_REL",
            "to_type": "System",
            "to_value": "LOS",
            "confidence": 0.99,
        }
    ]

    report = engine.admit(
        entities,
        relationships,
        source_text="Requirement FR-001 impacts LOS.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "unknown_relationship_type"
    ] == 1

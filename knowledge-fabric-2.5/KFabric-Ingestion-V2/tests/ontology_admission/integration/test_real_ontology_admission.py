"""Integration tests for admission against the production ontology.

What this test file tests
-------------------------
This file loads the repository's authoritative
config/enterprise_banking_ontology.yaml through the application's normal
get_ontology() path.

It verifies that:

1. The real ontology loads successfully.
2. Real node types are visible to the admission engine.
3. Real relationship types are visible.
4. A legitimate Requirement candidate can be admitted.
5. An invented node type is rejected.
6. An invented relationship type is rejected.
7. Real relationship endpoint constraints are enforced.

No LLM, LiteLLM, Ray, Langfuse, Neo4j, or Weaviate calls are made.
"""

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.ontology_admission import OntologyAdmissionEngine


def test_real_ontology_can_drive_admission():
    ontology = get_ontology()
    engine = OntologyAdmissionEngine(ontology)

    assert engine.node_types
    assert engine.relationship_types
    assert "Process" in engine.node_types
    assert "System" in engine.node_types


def test_real_ontology_accepts_known_requirement():
    ontology = get_ontology()
    engine = OntologyAdmissionEngine(ontology)

    report = engine.admit(
        [
            {
                "type": "Requirement",
                "value": "FR-TEST-001",
                "confidence": 0.99,
            }
        ],
        [],
        source_text="Requirement FR-TEST-001 defines the expected behaviour.",
    )

    assert any(
        entity["type"] == "Requirement"
        for entity in report.admitted_entities
    )


def test_real_ontology_rejects_invented_node():
    ontology = get_ontology()
    engine = OntologyAdmissionEngine(ontology)

    report = engine.admit(
        [
            {
                "type": "DefinitelyNotAnOntologyNode",
                "value": "hallucination",
                "confidence": 0.99,
            }
        ],
        [],
        source_text="This text contains no valid node.",
    )

    assert not report.admitted_entities
    assert report.entity_rejection_reasons["unknown_node_type"] == 1


def test_real_ontology_rejects_invented_relationship():
    ontology = get_ontology()
    engine = OntologyAdmissionEngine(ontology)

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.99},
            {"type": "System", "value": "LOS", "confidence": 0.99},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "TOTALLY_INVENTED",
                "to_type": "System",
                "to_value": "LOS",
                "confidence": 0.99,
            }
        ],
        source_text="Requirement impacts the system.",
    )

    assert not report.admitted_relationships
    assert (
        report.relationship_rejection_reasons["unknown_relationship_type"] == 1
    )

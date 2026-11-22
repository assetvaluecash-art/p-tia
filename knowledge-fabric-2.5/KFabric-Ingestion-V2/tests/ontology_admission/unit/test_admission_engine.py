"""Detailed unit tests for the ontology-wide admission engine.

What this test file tests
-------------------------
This file tests OntologyAdmissionEngine in isolation.

The engine is the deterministic trust boundary between candidate
information-extraction output and Knowledge Graph candidates.

It verifies:

1. Known ontology node types are admitted.
2. Unknown node types are rejected.
3. Missing entity values are rejected.
4. Entity confidence thresholds are enforced.
5. Evidence gates are applied from ontology configuration.
6. Evidence gates are not hard-coded specifically for Process.
7. Known relationship types with valid endpoints are admitted.
8. Unknown relationship types are rejected.
9. Invalid relationship endpoint types are rejected.
10. Relationships referencing rejected entities are rejected.
11. Relationship confidence thresholds are enforced.
12. Admission metadata is deterministic.

These tests deliberately do not call an LLM, LiteLLM, Langfuse,
Ray, Neo4j, Weaviate, or any external service.
"""

from kbfabric_ingest.ingest.ontology_admission import (
    OntologyAdmissionEngine,
)


def ontology():
    """Return a small deterministic ontology for isolated unit testing."""
    return {
        "node_types": [
            {"name": "Requirement"},
            {
                "name": "Process",
                "evidence_gate": {
                    "enabled": True,
                    "min_evidence_score": 3,
                    "step_keywords": ["step", "then"],
                    "actor_keywords": ["user", "operator", "maker"],
                    "system_keywords": ["system"],
                    "action_keywords": ["process", "execute", "submit"],
                    "outcome_keywords": [
                        "produces",
                        "results in",
                        "approved",
                        "disbursed",
                        "outcome",
                    ],
                    "exclusion_keywords": ["batch", "scheduled job"],
                },
            },
            {
                "name": "System",
                "evidence_gate": {
                    "enabled": True,
                    "min_evidence_score": 1,
                    "system_keywords": ["system", "application"],
                },
            },
            {"name": "Actor"},
        ],
        "relationship_types": [
            {
                "name": "IMPACTS",
                "from": ["Requirement"],
                "to": ["Process", "System"],
            },
            {
                "name": "USES",
                "from": ["Process"],
                "to": ["System"],
            },
        ],
    }


def test_known_entity_type_is_admitted():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Requirement", "value": "FR-001", "confidence": 0.95}],
        [],
        source_text="The requirement defines EMI calculation.",
    )

    assert len(report.admitted_entities) == 1
    assert report.admitted_entities[0]["type"] == "Requirement"


def test_unknown_entity_type_is_rejected():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "MadeUpNode", "value": "X", "confidence": 0.99}],
        [],
        source_text="Some text.",
    )

    assert report.admitted_entities == []
    assert report.entity_rejection_reasons["unknown_node_type"] == 1


def test_low_confidence_entity_is_rejected():
    engine = OntologyAdmissionEngine(
        ontology(),
        entity_confidence_threshold=0.70,
    )

    report = engine.admit(
        [{"type": "Requirement", "value": "FR-001", "confidence": 0.69}],
        [],
        source_text="Requirement FR-001.",
    )

    assert report.admitted_entities == []
    assert (
        report.entity_rejection_reasons[
            "entity_confidence_below_threshold"
        ]
        == 1
    )


def test_evidence_gate_applies_to_process():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Process", "value": "EMI Processing", "confidence": 0.95}],
        [],
        source_text="The product description contains no concrete process evidence.",
    )

    assert report.admitted_entities == []
    assert report.entity_rejection_reasons["node_evidence_gate_failed"] == 1


def test_evidence_gate_is_not_process_specific():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "System", "value": "LOS", "confidence": 0.95}],
        [],
        source_text="The application system integrates with Core Banking.",
    )

    assert len(report.admitted_entities) == 1
    assert report.admitted_entities[0]["type"] == "System"


def test_known_relationship_and_valid_endpoints_are_admitted():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.95},
            {"type": "System", "value": "LOS", "confidence": 0.95},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "IMPACTS",
                "to_type": "System",
                "to_value": "LOS",
                "confidence": 0.95,
            }
        ],
        source_text="The application system implements FR-001.",
    )

    assert len(report.admitted_relationships) == 1


def test_unknown_relationship_is_rejected():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.95},
            {"type": "System", "value": "LOS", "confidence": 0.95},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "HALLUCINATED_REL",
                "to_type": "System",
                "to_value": "LOS",
                "confidence": 0.99,
            }
        ],
        source_text="The application system implements FR-001.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "unknown_relationship_type"
    ] == 1


def test_invalid_relationship_endpoint_type_is_rejected():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.95},
            {"type": "Actor", "value": "RM", "confidence": 0.95},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "IMPACTS",
                "to_type": "Actor",
                "to_value": "RM",
                "confidence": 0.95,
            }
        ],
        source_text="Requirement impacts relationship manager.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "to_endpoint_type_not_allowed"
    ] == 1


def test_relationship_cannot_reference_rejected_entity():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.95},
            {"type": "MadeUpNode", "value": "X", "confidence": 0.99},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "IMPACTS",
                "to_type": "MadeUpNode",
                "to_value": "X",
                "confidence": 0.99,
            }
        ],
        source_text="Requirement impacts X.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "unknown_to_node_type"
    ] == 1


def test_relationship_confidence_is_enforced():
    engine = OntologyAdmissionEngine(
        ontology(),
        relationship_confidence_threshold=0.70,
    )

    report = engine.admit(
        [
            {"type": "Requirement", "value": "FR-001", "confidence": 0.95},
            {"type": "System", "value": "LOS", "confidence": 0.95},
        ],
        [
            {
                "from_type": "Requirement",
                "from_value": "FR-001",
                "rel_type": "IMPACTS",
                "to_type": "System",
                "to_value": "LOS",
                "confidence": 0.69,
            }
        ],
        source_text="The application system implements FR-001.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "relationship_confidence_below_threshold"
    ] == 1


def test_missing_entity_value_is_rejected():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Requirement", "value": "", "confidence": 0.95}],
        [],
        source_text="Requirement exists.",
    )

    assert report.admitted_entities == []
    assert report.entity_rejection_reasons["missing_entity_value"] == 1


def test_admission_metadata_is_added_deterministically():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Requirement", "value": "FR-001", "confidence": 0.95}],
        [],
        source_text="Requirement FR-001.",
        block_category="Requirement",
    )

    metadata = report.admitted_entities[0]["_admission"]

    assert metadata["admitted"] is True
    assert metadata["ontology_node_type"] == "Requirement"
    assert metadata["block_category"] == "Requirement"


_STRONG_PROCESS_TEXT = (
    "First the maker submits the instruction in the system. "
    "Then the checker approves the request. "
    "The workflow results in funds disbursed to the account."
)


def test_process_with_only_two_of_four_keyword_groups_is_rejected():
    """Process min_evidence_score=3 rejects a 2/4 keyword-group hit."""
    engine = OntologyAdmissionEngine(ontology())

    # actor + system only (2/4); no step/action/outcome language beyond that.
    report = engine.admit(
        [{"type": "Process", "value": "Eligibility Check", "confidence": 0.95}],
        [],
        source_text="The user reviews the LOS system screen for eligibility.",
    )

    assert report.admitted_entities == []
    assert report.entity_rejection_reasons["node_evidence_gate_failed"] == 1


def test_process_with_three_of_four_groups_and_structural_signals_is_accepted():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Process", "value": "Loan Disbursement Initiation", "confidence": 0.95}],
        [],
        source_text=_STRONG_PROCESS_TEXT,
    )

    assert len(report.admitted_entities) == 1
    assert report.admitted_entities[0]["type"] == "Process"


def test_process_exclusion_keyword_rejects_even_with_strong_positive_score():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [{"type": "Process", "value": "Emi Generation Batch", "confidence": 0.95}],
        [],
        source_text=(
            "First the maker submits EMI instructions in the system. "
            "Then the checker approves. The batch job results in schedules "
            "produced overnight."
        ),
    )

    assert report.admitted_entities == []
    assert (
        report.entity_rejection_reasons["node_evidence_exclusion_matched"] == 1
    )


def test_process_name_alone_cannot_self_satisfy_evidence_gate():
    engine = OntologyAdmissionEngine(ontology())

    # Name contains generation/classification/booking stems; block is name-only.
    for name in (
        "EMI Schedule Generation",
        "Delinquency Classification",
        "Loan Booking",
    ):
        report = engine.admit(
            [{"type": "Process", "value": name, "confidence": 0.95}],
            [],
            source_text=name,
        )
        assert report.admitted_entities == [], name
        assert report.entity_rejection_reasons.get(
            "node_evidence_gate_failed", 0
        ) >= 1


def test_process_with_actor_system_co_entities_and_strong_block_is_accepted():
    engine = OntologyAdmissionEngine(ontology())

    # Block has step + action + outcome but omits actor/system keywords;
    # co-extracted Actor + System satisfy structural Actor/System signals.
    # Keyword score still needs ≥3: add system keyword via "system" in text
    # while actor comes only from co-entity — use step+system+action (=3).
    report = engine.admit(
        [
            {"type": "Process", "value": "Collateral Validation", "confidence": 0.95},
            {"type": "Actor", "value": "Credit Officer", "confidence": 0.95},
            {"type": "System", "value": "LOS_Core", "confidence": 0.95},
        ],
        [],
        source_text=(
            "First the request is submitted in the core system. "
            "Then validation completes and results in an approved outcome."
        ),
    )

    admitted_types = {e["type"] for e in report.admitted_entities}
    assert "Process" in admitted_types
    assert "Actor" in admitted_types
    assert "System" in admitted_types


def test_process_missing_outcome_signal_is_rejected():
    engine = OntologyAdmissionEngine(ontology())

    # step + actor + system + action (=4) but no outcome keywords.
    report = engine.admit(
        [{"type": "Process", "value": "Limit Review", "confidence": 0.95}],
        [],
        source_text=(
            "First the maker submits the limit request in the system. "
            "Then the checker reviews the workflow steps."
        ),
    )

    assert report.admitted_entities == []
    assert (
        report.entity_rejection_reasons["process_missing_outcome_signal"] == 1
    )


def test_rejected_process_cascades_to_relationship_endpoints():
    engine = OntologyAdmissionEngine(ontology())

    report = engine.admit(
        [
            {"type": "Process", "value": "Loan Booking", "confidence": 0.95},
            {"type": "System", "value": "LOS", "confidence": 0.95},
        ],
        [
            {
                "from_type": "Process",
                "from_value": "Loan Booking",
                "rel_type": "USES",
                "to_type": "System",
                "to_value": "LOS",
                "confidence": 0.95,
            }
        ],
        source_text="Loan Booking uses the application system.",
    )

    assert report.admitted_relationships == []
    assert report.relationship_rejection_reasons[
        "from_endpoint_not_admitted"
    ] == 1

"""Regression coverage for runtime ontology use in legacy text and vision IE helpers."""

from kbfabric_ingest.llm.client import LLMClient


def test_vision_type_and_edge_validation_rejects_non_contract_output():
    """Objective: verify vision output is admitted only after exact runtime type/triple validation.

    This matters because visual extraction can produce plausible but ontology-incompatible labels;
    those must not become KG candidates. It protects the invariant that only runtime ontology node
    types and exact allowed endpoint triples survive the image IE normalization boundary.
    """
    client = object.__new__(LLMClient)
    ontology = {
        "node_types": [{"name": "Process"}, {"name": "Actor"}],
        "relationship_types": [{"name": "PERFORMS", "from": "Actor", "to": "Process"}],
        "vision_ie_type_mapping": {"entity_types": {"Activity": "Process"}},
    }
    entities, entity_warnings = client._validate_entity_types([
        {"type": "Activity", "value": "Claim Review"},
        {"type": "Decision", "value": "Approved?"},
        {"type": "Actor", "value": "Claims Examiner"},
    ], ontology)
    assert [item["type"] for item in entities] == ["Process", "Actor"]
    assert len(entity_warnings) == 1

    relationships, relationship_warnings = client._validate_relationship_types([
        {"from_type": "Actor", "from_value": "Claims Examiner", "rel_type": "PERFORMS",
         "to_type": "Process", "to_value": "Claim Review"},
        {"from_type": "Process", "from_value": "Claim Review", "rel_type": "NEXT_STEP",
         "to_type": "Process", "to_value": "Claim Review"},
    ], ontology, entities)
    assert [item["rel_type"] for item in relationships] == ["PERFORMS"]
    assert len(relationship_warnings) == 1

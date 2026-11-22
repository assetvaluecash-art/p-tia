from kbfabric_ingest.ingest.lane2_two_pass import (
    YamlOntologyProvider,
    build_lane2_ontology,
)


def test_lane2_provider_composes_runtime_contracts_without_losing_triples():
    """Objective: verify every category pack preserves ontology triples and directs Pass 2 to include evidenced primary nodes and edges. This matters because otherwise valid concepts can disappear before mapping and admission. Invariant: the exact frozen triple remains authoritative, supported primary nodes are requested, and every source-evidenced allowed edge is requested without allowing invented edges."""
    customer_ontology = {
        "version": 3,
        "domains": [
            {
                "name": "corporate_commercial",
                "spine": [
                    {
                        "ProductLine": "Trade Finance",
                        "functions": [],
                    }
                ],
            }
        ],
    }

    canonical_model = {
        "block_categories": [
            {
                "name": "Process",
                "description": "Canonical process category",
                "maps_to_node": "Process",
            }
        ],
        "node_types": [
            {
                "name": "Process",
                "layer": "business",
                "description": "Canonical process",
                "name_pattern": "Title Case",
            },
            {
                "name": "System",
                "layer": "technology",
                "description": "Canonical system",
                "name_pattern": "System name",
            },
        ],
        "relationship_types": [
            {
                "name": "USES",
                "from": "Process",
                "to": "System",
                "verb": "uses",
            }
        ],
        "vision_ie_type_mapping": {},
        "path_format": {},
    }

    extraction_policy = {
        "node_types": [
            {
                "name": "Process",
                "description": "Industry-specific process meaning",
                "ie_examples": [
                    {
                        "block_text": "Trade Finance onboarding uses SWIFT",
                        "entities": [
                            {"type": "Process", "value": "Trade Finance onboarding"},
                            {"type": "System", "value": "SWIFT"},
                        ],
                        "relationships": [
                            {
                                "from_type": "Process",
                                "from_value": "Trade Finance onboarding",
                                "rel_type": "USES",
                                "to_type": "System",
                                "to_value": "SWIFT",
                            }
                        ],
                    }
                ],
                "anti_examples": ["A generic policy statement"],
            }
        ],
        "relationship_types": [
            {
                "name": "USES",
                "description": "Industry-specific usage relationship",
                "example": "A process uses a system",
                "anti_example": "A process is governed by a system",
            }
        ],
    }

    runtime = build_lane2_ontology(
        customer_ontology=customer_ontology,
        canonical_model=canonical_model,
        extraction_policy=extraction_policy,
    )

    provider = YamlOntologyProvider(
        runtime,
        canonical_model=canonical_model,
        extraction_policy=extraction_policy,
    )

    prompt = provider.get_prompt("Process")

    assert "Industry-specific process meaning" in prompt.system_prompt
    assert "Trade Finance onboarding uses SWIFT" in prompt.system_prompt
    assert "Process -[USES]-> System" in prompt.system_prompt
    assert prompt.allowed_relationship_triples == [
        ("Process", "USES", "System")
    ]
    assert "MUST include that primary node" in prompt.system_prompt
    assert "Extract every relationship explicitly stated" in prompt.system_prompt
    assert "do not infer a relationship that the source does not support" in prompt.system_prompt

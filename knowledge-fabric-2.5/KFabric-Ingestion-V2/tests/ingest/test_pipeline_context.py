"""Objective: verify canonical categories reach Lane 2 classification.

This matters because the customer runtime ontology intentionally excludes
canonical structural fields, while classification still needs those categories.
Invariant: classification and the prompt provider use the same merged ontology,
and building it does not mutate the customer ontology.
"""

from kbfabric_ingest.ingest.pipeline.context import PipelineDeps


def test_pipeline_dependencies_share_merged_runtime_ontology():
    customer_ontology = {
        "domains": [{"name": "corporate_commercial", "spine": []}],
    }
    canonical_model = {
        "block_categories": [{"name": "Requirement"}],
        "node_types": [{"name": "Requirement"}],
        "relationship_types": [],
    }
    extraction_policy = {
        "node_types": [],
        "relationship_types": [],
    }

    pipeline_dependencies = PipelineDeps.from_config(
        ontology=customer_ontology,
        canonical_model=canonical_model,
        extraction_policy=extraction_policy,
        enable_neo4j_mapping=False,
    )

    assert [
        category["name"]
        for category in pipeline_dependencies.ontology["block_categories"]
    ] == ["Requirement"]
    assert pipeline_dependencies.prompt_provider.supported_categories() == [
        "Requirement"
    ]
    assert "block_categories" not in customer_ontology

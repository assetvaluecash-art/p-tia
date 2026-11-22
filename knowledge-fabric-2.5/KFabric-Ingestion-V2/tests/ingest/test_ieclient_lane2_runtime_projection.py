"""Objective: verify Lane 2 admission projection uses the frozen composed ontology.

This matters because extraction categories and KG node mappings must come from
one customer/domain/product-line/version context. Invariant: a canonical Rule
category mapping survives result projection and is not lost by projecting with
only the customer domain's contextualization definition.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from kbfabric_ingest.ingest import ie_client as ie_client_module
from kbfabric_ingest.ingest.ie_client import IEClient
from kbfabric_ingest.ingest.lane2_two_pass import (
    ClassificationResult,
    ClassifiedNodeType,
    ExtractionResult,
    Lane2TwoPassResult,
    build_lane2_ontology,
)
from kbfabric_ingest.ingest.models import CanonicalBlock
import kbfabric_ingest.ingest.pipeline as pipeline_module


def test_lane2_projection_receives_the_composed_runtime_ontology(monkeypatch):
    """Objective: preserve canonical node mapping when Lane 2 results are projected.

    This protects real KG admission after extraction. Invariant: the IEClient
    passes its composed frozen ontology to the result projector, so a Rule
    classification maps to KG node type Rule.
    """
    customer_definition = {
        "domains": [{"name": "consumer_retail", "spine": []}],
    }
    canonical_model = {
        "block_categories": [
            {"name": "Rule", "maps_to_node": "Rule"},
        ],
        "node_types": [{"name": "Rule"}],
        "relationship_types": [],
    }
    extraction_policy = {"node_types": [], "relationship_types": []}
    runtime_ontology = build_lane2_ontology(
        customer_ontology=customer_definition,
        canonical_model=canonical_model,
        extraction_policy=extraction_policy,
    )
    classification = ClassificationResult(
        category="Rule",
        confidence=0.99,
        categories=[ClassifiedNodeType(category="Rule", confidence=0.99)],
    )
    two_pass_result = Lane2TwoPassResult(
        block_id="block-001",
        source_file="synthetic.txt",
        classification=classification,
        extraction=ExtractionResult(
            classification=classification,
            entities=[{"type": "Rule", "value": "RULE-001"}],
        ),
        pass2_invoked=True,
        success=True,
    )

    client = object.__new__(IEClient)
    client._run_context = SimpleNamespace(
        ontology=SimpleNamespace(
            domain_definition=customer_definition,
            canonical_model=canonical_model,
            extraction_policy=extraction_policy,
        )
    )
    client._runtime_ontology = runtime_ontology
    client._failure_causes = {}
    client.logger = MagicMock()
    client._get_pipeline_deps = lambda: object()

    monkeypatch.setattr(
        ie_client_module,
        "get_app_config",
        lambda: {"ollama": {"text": {"batch_delay_seconds": 0}}},
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_lane2_ie",
        lambda *args, **kwargs: two_pass_result,
    )

    block = CanonicalBlock(
        block_id="block-001",
        source_file="synthetic.txt",
        file_type="txt",
        domain="consumer_retail",
        block_index=0,
        block_type="paragraph",
        text="Business rule RULE-001 applies.",
        metadata={},
    )

    projected, failures = client._batch_lane2_two_pass_ie([block], [block.text])

    assert failures == 0
    assert projected[0]["kg_node_type"] == "Rule"

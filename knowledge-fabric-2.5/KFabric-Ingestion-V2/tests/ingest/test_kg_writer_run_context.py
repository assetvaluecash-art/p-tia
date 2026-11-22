from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kbfabric_ingest.ingest.kg_writer import KGWriter


def test_kg_writer_reads_product_line_from_frozen_run_context():
    """Objective: verify KGWriter reads product-line scope from the frozen run context.

    This matters because spine linking must stay within the product line selected
    at ingestion start. The invariant is that product-line metadata comes from
    OntologyRuntimeContext, never from the ontology definition dictionary.
    """
    writer = KGWriter.__new__(KGWriter)
    writer._run_context = SimpleNamespace(
        ontology=SimpleNamespace(product_line_name="Trade Finance")
    )

    assert writer._run_product_line_name() == "Trade Finance"


def test_kg_writer_requires_configured_run_context_for_product_line():
    """Objective: verify product-line resolution fails closed without run context.

    This matters because spine linking must not guess scope from document content.
    The invariant is that no product line is resolved before run context is set.
    """
    writer = KGWriter.__new__(KGWriter)
    writer._run_context = None

    with pytest.raises(
        RuntimeError,
        match="KGWriter run context has not been configured",
    ):
        writer._run_product_line_name()


def test_kg_writer_composes_customer_canonical_and_policy_contracts():
    """Objective: verify KGWriter uses the frozen composed ontology contract.

    This matters because a domain-only customer definition omits canonical
    node and relationship types, which can silently block graph writes after
    admission. Invariant: KGWriter's runtime contract contains the selected
    customer spine plus canonical relationship triples refined by industry
    policy, all from the same frozen run context.
    """
    writer = KGWriter.__new__(KGWriter)
    writer._run_context = SimpleNamespace(
        ontology=SimpleNamespace(
            domain_definition={
                "version": "customer-v3",
                "domains": [{"name": "consumer_retail", "spine": [{"ProductLine": "Retail Cards", "functions": []}]}],
            },
            canonical_model={
                "node_types": [{"name": "Rule"}],
                "relationship_types": [
                    {"name": "GOVERNED_BY", "from": ["Process"], "to": ["Rule"], "description": "canonical"}
                ],
            },
            extraction_policy={
                "node_types": [],
                "relationship_types": [
                    {"name": "GOVERNED_BY", "description": "banking policy"}
                ],
            },
        )
    )

    ontology = writer._runtime_ontology()

    assert ontology["domains"][0]["spine"][0]["ProductLine"] == "Retail Cards"
    assert ontology["node_types"] == [{"name": "Rule"}]
    assert ontology["relationship_types"] == [
        {"name": "GOVERNED_BY", "from": ["Process"], "to": ["Rule"], "description": "banking policy"}
    ]


def test_neo4j_write_verification_counts_only_the_frozen_run_scope():
    """Existing graph data from other runs/customers cannot inflate this run's report counts."""
    writer = KGWriter.__new__(KGWriter)
    writer.driver = MagicMock()
    writer.logger = MagicMock()
    session = writer.driver.session.return_value.__enter__.return_value
    session.run.side_effect = [
        SimpleNamespace(single=lambda: {"count": 3}),
        SimpleNamespace(single=lambda: {"count": 2}),
    ]

    counts = writer.verify_write(
        domain="cards", customer_id="customer-a", product_line_id="pl-cards",
        ingestion_batch_id="batch-7", expected_nodes=3, expected_relationships=2,
    )

    assert counts == {
        "nodes": 3, "relationships": 2, "verification_status": "VERIFIED"
    }
    assert session.run.call_count == 2
    for call in session.run.call_args_list:
        query = call.args[0]
        params = call.kwargs
        assert "customer_id = $customer_id" in query
        assert "$product_line_id IN coalesce" in query
        assert "$ingestion_batch_id IN coalesce" in query
        assert params == {
            "domain": "cards", "customer_id": "customer-a",
            "product_line_id": "pl-cards", "ingestion_batch_id": "batch-7",
        }


def test_neo4j_verification_unavailable_preserves_writer_counts_without_claiming_readback():
    """A database read-back outage never turns writer-reported writes into zero or verified counts."""
    writer = KGWriter.__new__(KGWriter)
    writer.driver = MagicMock()
    writer.logger = MagicMock()
    writer.driver.session.side_effect = RuntimeError("Neo4j read-back unavailable")

    counts = writer.verify_write(
        domain="cards", customer_id="customer-a", product_line_id="pl-cards",
        ingestion_batch_id="batch-7", expected_nodes=3, expected_relationships=2,
    )

    assert counts == {
        "nodes": 3, "relationships": 2, "verification_status": "UNAVAILABLE"
    }


def test_kg_path_preview_is_filtered_to_customer_product_line_and_batch():
    """The report's relationship samples cannot leak paths from another customer or run."""
    writer = KGWriter.__new__(KGWriter)
    writer.driver = MagicMock()
    writer.logger = MagicMock()
    session = writer.driver.session.return_value.__enter__.return_value
    session.run.return_value = []

    paths = writer.sample_kg_paths(
        domain="cards", customer_id="customer-a", product_line_id="pl-cards",
        ingestion_batch_id="batch-7", limit=4,
    )

    assert paths == []
    query = session.run.call_args.args[0]
    params = session.run.call_args.kwargs
    assert "a.customer_id = $customer_id" in query
    assert "$product_line_id IN coalesce(r.ingestion_product_line_ids, [])" in query
    assert "$ingestion_batch_id IN coalesce(r.ingestion_batch_ids, [])" in query
    assert params["customer_id"] == "customer-a"
    assert params["product_line_id"] == "pl-cards"
    assert params["ingestion_batch_id"] == "batch-7"

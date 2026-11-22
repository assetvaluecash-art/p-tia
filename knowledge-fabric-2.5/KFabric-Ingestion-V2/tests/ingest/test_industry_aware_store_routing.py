from contextlib import contextmanager
from logging import getLogger
from types import SimpleNamespace

import pytest

from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
from kbfabric_ingest.ingest.kg_writer import KGWriter


def test_weaviate_writer_uses_the_frozen_industry_domain_catalog():
    """Industry-specific domain routing must not leak through the banking config cache."""
    writer = WeaviateWriter.__new__(WeaviateWriter)
    writer.domain_to_class = {"consumer_retail": "Consumer_retail"}
    runtime = SimpleNamespace(
        domain_id="health_insurance",
        domain_definition={"domains": [
            {"name": "health_insurance"},
            {"name": "care"},
        ]},
    )
    writer.configure_run_context(SimpleNamespace(ontology=runtime))

    assert writer.class_name_for_domain("health_insurance") == "Health_insurance"
    assert writer.class_name_for_domain("care") == "Care"
    with pytest.raises(ValueError, match="outside the frozen industry ontology"):
        writer.class_name_for_domain("consumer_retail")


def test_customer_root_is_parent_of_domain_root(monkeypatch):
    """Pre-seeding creates CustomerRoot -> DomainRoot -> ProductLine in Neo4j."""
    class Tx:
        def __init__(self):
            self.calls = []

        def run(self, query, **params):
            self.calls.append((query, params))

    class Session:
        def __init__(self):
            self.tx = Tx()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute_write(self, callback):
            return callback(self.tx)

    class Driver:
        def __init__(self):
            self.current = Session()

        @contextmanager
        def session(self):
            yield self.current

        def close(self):
            return None

    writer = KGWriter.__new__(KGWriter)
    writer.logger = getLogger("test.industry_spine")
    writer.driver = Driver()
    writer._run_context = SimpleNamespace(
        tenant_context=SimpleNamespace(customer_id="cust-1", tenant_id="tenant-1", industry_id="banking"),
        ontology=SimpleNamespace(
            customer_ontology_id="ontology-1", customer_ontology_version=3,
            ontology_version="5.1", domain_id="consumer_retail",
        ),
    )
    monkeypatch.setattr(writer, "_runtime_ontology", lambda: {"domains": [{
        "name": "consumer_retail",
        "spine": [{"ProductLine": "Retail Cards", "functions": []}],
    }]})
    monkeypatch.setattr(writer, "_embed_entity_texts", lambda _requests: {})
    monkeypatch.setattr(writer, "_embedding_on_create_clause", lambda *_args: "")
    monkeypatch.setattr(writer, "_embedding_on_match_clause", lambda *_args: "")

    writer.pre_seed_spine("consumer_retail", tenant_id="legacy-tenant-label")

    calls = writer.driver.current.tx.calls
    customer_root = next((params for query, params in calls if "MERGE (customer:CustomerRoot" in query), None)
    domain = next((params for query, params in calls if "MERGE (dom:DomainRoot" in query), None)
    edge_query = next(query for query, _params in calls if "MERGE (dom)-[:PART_OF]->(customer)" in query)
    assert customer_root == {"customer_id": "cust-1", "tenant_id": "tenant-1", "industry_id": "banking"}
    assert domain["industry_id"] == "banking"
    assert "DomainRoot" in edge_query and "CustomerRoot" in edge_query
    assert not any("MERGE (obsolete:IndustryRoot" in query for query, _params in calls)


def test_lane2_candidate_scoring_builds_semantic_query_before_logging():
    """A real Pass 2 entity must reach candidate scoring without a local-variable error."""
    from kbfabric_ingest.ingest.lane2_node_reconciliation import (
        ExistingNode,
        IncomingNode,
        MockSemanticSimilarityProvider,
        score_candidates,
    )

    incoming = IncomingNode(
        temporary_id="temp-1", node_type="Process", name="Card activation",
        description="Activate a newly issued card.", domain="consumer_retail",
    )
    existing = [ExistingNode(entity_id="entity-1", name="Card activation")]

    candidates, provider_name = score_candidates(
        incoming, existing, semantic=MockSemanticSimilarityProvider(),
        query_context="Retail Cards",
    )

    assert provider_name.startswith("MockSemanticSimilarityProvider")
    assert len(candidates) == 1
    assert candidates[0].candidate.entity_id == "entity-1"

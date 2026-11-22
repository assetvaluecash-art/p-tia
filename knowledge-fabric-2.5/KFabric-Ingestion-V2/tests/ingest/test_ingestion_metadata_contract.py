"""Regression coverage for run scope and optional retrieval context metadata."""

from types import SimpleNamespace
from unittest.mock import Mock

from kbfabric_ingest.ingest.ingestion_metadata import (
    build_run_metadata,
    document_id_for_source,
    scoped_block_id,
    scoped_entity_id,
    split_semicolon_values,
)
from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter


def test_run_metadata_carries_frozen_scope_and_optional_document_context():
    """Objective: verify every chunk can inherit immutable run scope plus optional user context.

    This matters because retrieval needs customer-safe, product-line-aware, project-filterable
    evidence. The invariant is that user context is parsed without changing the frozen ontology
    context, while the raw input remains auditable.
    """
    run_context = SimpleNamespace(
        tenant_context=SimpleNamespace(tenant_id="tenant-1", customer_id="customer-1"),
        ontology=SimpleNamespace(
            domain_id="consumer_retail",
            product_line_id="DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
            customer_ontology_id="ontology-1",
            customer_ontology_version=2,
            ontology_version="canonical-v3",
            extraction_policy={"version": "banking-5.0.1"},
        ),
    )

    metadata = build_run_metadata(
        run_context,
        ingestion_batch_id="batch-1",
        project_ids_raw=" PRJ-1;PRJ-2 ; PRJ-1 ",
        context_tags_raw="system=Temenos T24; region=APAC",
        document_nature="technology_artifacts",
    )

    assert metadata == {
        "ingestion_batch_id": "batch-1",
        "tenant_id": "tenant-1",
        "customer_id": "customer-1",
        "domain_id": "consumer_retail",
        "ingestion_product_line_id": "DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
        "customer_ontology_id": "ontology-1",
        "customer_ontology_version": 2,
        "canonical_ontology_version": "canonical-v3",
        "industry_extraction_policy_version": "banking-5.0.1",
        "document_nature": "technology_artifacts",
        "project_ids": ["PRJ-1", "PRJ-2"],
        "context_tags": ["system=Temenos T24", "region=APAC"],
        "raw_project_ids": " PRJ-1;PRJ-2 ; PRJ-1 ",
        "raw_context_tags": "system=Temenos T24; region=APAC",
    }


def test_optional_context_is_empty_when_not_provided():
    """Objective: protect optionality so ingestion works without project or tag inputs.

    This matters for existing ingestion callers and gradual UI rollout. The invariant is that
    omitted optional values produce empty filter lists rather than invented scope or errors.
    """
    assert split_semicolon_values(None) == []
    assert split_semicolon_values(" ;  ; ") == []


def test_document_id_is_stable_and_customer_scoped():
    """Objective: verify source document identity is stable within a customer and distinct across customers.

    This matters for provenance joins from chunks into graph evidence. The invariant is that the
    same source path maps consistently for one customer without conflating another customer's file.
    """
    assert document_id_for_source("customer-1", "docs/requirements.docx") == document_id_for_source(
        "customer-1", "docs/requirements.docx"
    )
    assert document_id_for_source("customer-1", "docs/requirements.docx") != document_id_for_source(
        "customer-2", "docs/requirements.docx"
    )


def test_weaviate_schema_declares_filterable_scope_and_context_fields():
    """Objective: ensure the chunk schema exposes typed fields needed by a future retriever.

    This matters because scope and tags buried in an opaque metadata blob cannot reliably support
    filtering. The invariant is that frozen run scope, project IDs, tags, and provenance are typed
    schema properties.
    """
    writer = WeaviateWriter.__new__(WeaviateWriter)
    writer.config = {}
    properties = {item["name"] for item in writer._get_schema_template()["properties"]}
    assert {
        "tenant_id",
        "customer_id",
        "domain_id",
        "ingestion_product_line_id",
        "customer_ontology_id",
        "customer_ontology_version",
        "canonical_ontology_version",
        "industry_extraction_policy_version",
        "document_id",
        "ingestion_batch_id",
        "source_block_ids",
        "project_ids",
        "context_tags",
        "raw_project_ids",
        "raw_context_tags",
    } <= properties


def test_existing_weaviate_schema_receives_new_contract_properties():
    """Objective: verify existing Weaviate classes gain missing metadata fields additively.

    This matters because deployed classes predate the retrieval metadata contract. The invariant is
    that schema evolution adds only missing properties and does not delete or recreate stored data.
    """
    writer = WeaviateWriter.__new__(WeaviateWriter)
    writer.client = SimpleNamespace(
        schema=SimpleNamespace(
            get=Mock(return_value={"properties": [{"name": "already_present"}]}),
            property=SimpleNamespace(create=Mock()),
        )
    )
    writer.logger = Mock()

    writer._ensure_class_properties(
        "ConsumerRetailBlocks",
        [{"name": "already_present"}, {"name": "project_ids", "dataType": ["text[]"]}],
    )

    writer.client.schema.property.create.assert_called_once_with(
        "ConsumerRetailBlocks", {"name": "project_ids", "dataType": ["text[]"]}
    )


def test_persisted_block_and_entity_ids_are_customer_scoped():
    """Objective: prove identical source blocks and entity names get distinct IDs per customer.

    This matters because Weaviate upserts and Neo4j MERGEs use these IDs as persistence keys.
    The invariant is that same-domain, same-content records remain separate across customers.
    """
    assert scoped_block_id("customer-1", "source-block") != scoped_block_id(
        "customer-2", "source-block"
    )
    assert scoped_entity_id("customer-1", "retail", "Requirement", "card limit") != scoped_entity_id(
        "customer-2", "retail", "Requirement", "card limit"
    )
    assert scoped_entity_id("customer-1", "retail", "Requirement", "card limit") == scoped_entity_id(
        "customer-1", "retail", "Requirement", "card limit"
    )


def test_neo4j_candidate_reads_fail_closed_without_customer_scope():
    """Objective: prevent mapping and endpoint resolution from searching a shared domain graph.

    This matters because customer graphs can contain identically named business concepts.
    The invariant is that candidate retrieval requires one explicit customer and the Cypher
    used for name, broad-pool, and vector lookup all include that exact customer predicate.
    """
    from kbfabric_ingest.ingest.lane2_node_reconciliation import (
        IncomingNode,
        Neo4jCandidateReader,
        _CYPHER_BY_NAME_ALIAS,
        _CYPHER_BY_TYPE_DOMAIN,
        _CYPHER_VECTOR_QUERY,
    )

    reader = Neo4jCandidateReader(driver=None)
    incoming = IncomingNode(temporary_id="test", node_type="Requirement", name="Card limit")
    assert reader.fetch_candidates(incoming, "consumer_retail") == []
    assert reader.read_query_count == 0
    for query in (_CYPHER_BY_NAME_ALIAS, _CYPHER_BY_TYPE_DOMAIN, _CYPHER_VECTOR_QUERY):
        assert "e.customer_id = $customer_id" in query
        assert "e.tenant_id = $tenant_id" in query

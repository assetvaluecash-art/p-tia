"""Contract tests for IE client with mocked LLM responses."""

from unittest.mock import MagicMock

from kbfabric_ingest.ingest.ie_client import IEClient
from kbfabric_ingest.ingest.models import BlockCategory, CanonicalBlock
from kbfabric_ingest.ingest.pii import PIIService
from kbfabric_ingest.llm.client import LLMClient
from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import OntologyRuntimeContext
from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext


def _configure_run_context(ie_client: IEClient, ontology: dict) -> None:
    """Configure the authoritative customer/domain/product-line context for contract tests."""
    runtime = OntologyRuntimeContext(
        customer_id="customer-1",
        tenant_id="tenant-1",
        domain_id="test_domain",
        customer_ontology_id="ontology-1",
        customer_ontology_version=1,
        ontology_id="test-ontology",
        ontology_version="1.0",
        domain_definition=ontology,
        canonical_model={
            key: ontology.get(key, [])
            for key in ("node_types", "relationship_types", "block_categories")
        },
        extraction_policy={"node_types": [], "relationship_types": []},
        product_line_id="DOMAIN:test_domain|PRODUCT_LINE:Test Product",
        product_line_name="Test Product",
    )
    ie_client.configure_run_context(
        IngestionRunContext(
            tenant_context=TenantContext(
                tenant_id="tenant-1",
                customer_id="customer-1",
                customer_name="Test Customer",
                industry_id="test_industry",
            ),
            ontology=runtime,
        )
    )


def test_ie_client_enrich_blocks():
    """Objective: verify IE extracts entities using a frozen run context and mocked LLM.

    This protects the ingestion contract from live-model variability. The IE result
    must retain the expected category, entity type, and embedding dimensions.
    """
    # Create mock services
    pii_service = MagicMock(spec=PIIService)
    pii_service.mask_text.return_value = ("masked text", "masked text", [])
    
    llm_client = MagicMock(spec=LLMClient)
    # Lane 2 now uses the reusable pipeline's llm_callable boundary.
    # Mock ChatLLMClient rather than the retired ie_text_analysis() API.
    chat_client = MagicMock()
    chat_client.call_with_system_user.side_effect = [
        (
            '{"category": "Requirement", "confidence": 0.95, '
            '"reasoning_summary": "The block states a requirement.", '
            '"unmatched": false}',
            {"duration_ms": 1.0, "token_usage": {"prompt_chars": 10, "response_chars": 10}},
        ),
        (
            '{"entities": [{"value": "This is a requirement", '
            '"type": "Requirement", "confidence": 0.95}], '
            '"relationships": []}',
            {"duration_ms": 1.0, "token_usage": {"prompt_chars": 10, "response_chars": 10}},
        ),
    ]
    llm_client._create_chat_client.return_value = chat_client
    llm_client.embed_text.return_value = [[0.1] * 1024]  # Snowflake Arctic embedding
    
    ontology = {
        "node_types": [{"name": "Process"}, {"name": "Requirement"}],
        "relationship_types": [],
        "block_categories": [
            {
                "name": "Requirement",
                "description": "Functional or non-functional requirement statement.",
                "maps_to_node": "Requirement",
            }
        ],
        "block_category_to_node": {"Requirement": "Requirement"},
    }
    
    ie_client = IEClient(pii_service, llm_client)
    _configure_run_context(ie_client, ontology)
    
    # Create test blocks
    test_blocks = [
        CanonicalBlock(
            block_id="test-1",
            source_file="test.docx",
            file_type="docx",
            domain="test_domain",
            page_number=None,
            block_index=0,
            block_type="paragraph",
            text="This is a requirement.",
            image_bytes=None,
            metadata={}
        )
    ]
    
    # Enrich blocks
    enriched = ie_client.enrich_blocks(test_blocks)
    
    # Assertions
    assert len(enriched) == 1, "Should produce one enriched block"
    
    enriched_block = enriched[0]
    assert enriched_block.block_category == BlockCategory.Requirement
    assert enriched_block.kg_node_type == "Requirement"
    assert len(enriched_block.embedding) == 1024, "Snowflake Arctic embedding should be 1024 dimensions"
    assert enriched_block.pii_spans == []
    assert enriched_block.masked_text == "masked text"
    assert len(enriched_block.candidate_entities) == 1
    assert enriched_block.candidate_entities[0]["type"] == "Requirement"


def test_ie_client_image_blocks():
    """Objective: verify image-block IE uses the configured runtime context.

    This protects vision ingestion from bypassing the run's ontology contract.
    Image processing must preserve the expected block classification behavior.
    """
    pii_service = MagicMock(spec=PIIService)
    pii_service.mask_text.return_value = ("text", "text", [])
    llm_client = MagicMock(spec=LLMClient)
    
    llm_client.ie_image_analysis.return_value = [
        {
            "description": "A process diagram showing trade flow",
            "block_category": "Diagram_Process",
            "kg_node_type": "ProcessDiagram",
            "entities": [],
            "relationships": []
        }
    ]
    llm_client.embed_text.return_value = [[0.2] * 1024]
    
    ontology = {
        "block_categories": [],
        "block_category_to_node": {"Diagram_Process": "ProcessDiagram"}
    }
    
    ie_client = IEClient(pii_service, llm_client)
    _configure_run_context(ie_client, ontology)

    # Create image block
    image_block = CanonicalBlock(
        block_id="img-1",
        source_file="test.docx",
        file_type="docx",
        domain="test_domain",
        page_number=None,
        block_index=1,
        block_type="image",
        text="",
        image_bytes=b"fake_image_bytes",
        metadata={}
    )
    
    enriched = ie_client.enrich_blocks([image_block])
    
    assert len(enriched) == 1
    assert enriched[0].block_category == BlockCategory.Diagram
    assert enriched[0].block.text == "A process diagram showing trade flow"

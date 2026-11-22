from unittest.mock import MagicMock
from uuid import uuid4

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRecord,
)
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    EffectiveDomainOntologyResolver,
)
from kbfabric_ingest.ingest.ie_client import IEClient
from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.pii import PIIService
from kbfabric_ingest.llm.client import LLMClient


CUSTOMER_ID = "Pulse AI_Trade Finance Ingestion Test 2_d7802c82-d0c6-4723-96ea-aa930dbc8cb4"
DOMAIN_ID = "corporate_commercial"
PRODUCT_LINE_ID = (
    "DOMAIN:corporate_commercial|PRODUCT_LINE:Trade Finance"
)


class _FakeCustomerOntologyRepository:
    """Return the persisted customer ontology snapshot under test."""

    def __init__(self, record: CustomerOntologyRecord):
        self.record = record

    def get_active(self, customer_id: str):
        assert customer_id == CUSTOMER_ID
        return self.record


def _v3_definition() -> dict:
    return {
        "domains": [
            {
                "name": DOMAIN_ID,
                "spine": [
                    {
                        "ProductLine": "Trade Finance",
                        "functions": [
                            {
                                "Function": "Trade Operations",
                                "subfunctions": [
                                    "Document Presentation & Booking"
                                ],
                            }
                        ],
                    },
                    {
                        "ProductLine": "Payments",
                        "functions": [],
                    },
                ],
                "customer_context": {
                    "spine": {
                        "product_lines": {
                            "Trade Finance": {
                                "configured": True,
                                "applicable": True,
                                "aliases": [],
                            },
                            "Payments": {
                                "configured": True,
                                "applicable": False,
                                "aliases": [],
                            },
                        },
                        "functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "applicable": True,
                                    "aliases": [],
                                },
                            },
                        },
                        "sub_functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "Document Presentation & Booking": {
                                        "applicable": True,
                                        "aliases": [],
                                    },
                                },
                            },
                        },
                    },
                    "additions": {
                        "product_lines": [],
                        "functions": [
                            {
                                "id": "customer-fn-digital-trade-document-processing-v3",
                                "parent_product_line_id": PRODUCT_LINE_ID,
                                "name": "Digital Trade Document Processing",
                                "description": "Customer-specific digital processing capability.",
                                "aliases": [],
                                "applicable": True,
                            }
                        ],
                        "sub_functions": [
                            {
                                "id": "customer-sf-electronic-document-presentation-v3",
                                "parent_function_id": "customer-fn-digital-trade-document-processing-v3",
                                "name": "Electronic Document Presentation",
                                "description": "Customer-specific electronic presentation.",
                                "aliases": [],
                                "applicable": True,
                            }
                        ],
                    },
                },
            }
        ]
    }


def _record() -> CustomerOntologyRecord:
    return CustomerOntologyRecord(
        customer_ontology_id=uuid4(),
        tenant_id="tenant-bank-1",
        customer_id=CUSTOMER_ID,
        ontology_id="enterprise_banking",
        ontology_version="5.0.1",
        customer_ontology_version=3,
        status="ACTIVE",
        definition=_v3_definition(),
        change_type="CONTEXTUALIZATION",
        change_reason="Trade Finance document-processing scope.",
        change_summary={},
        parent_customer_ontology_id=None,
        created_by="test",
        created_at=None,
        confirmed_by="test",
        confirmed_at=None,
        updated_at=None,
        request_id="trade-finance-v3-surgical-20261006",
    )


def test_customer_domain_ontology_reaches_ie_from_active_v3():
    """Verify the active customer/domain ontology is handed unchanged into IE.

    This matters because extraction and admission must share the frozen
    customer/domain contract plus the canonical model and industry policy.
    Invariant: the selected V3 spine, supported node and relationship types,
    and product-line scope all reach the IE admission and prompt boundaries.
    """
    resolver = EffectiveDomainOntologyResolver(
        repository=_FakeCustomerOntologyRepository(_record())
    )

    runtime = resolver.resolve(
        customer_id=CUSTOMER_ID,
        domain_id=DOMAIN_ID,
        product_line_id=PRODUCT_LINE_ID,
        product_line_name="Trade Finance",
    )

    assert runtime.customer_ontology_version == 3
    assert runtime.domain_id == DOMAIN_ID
    assert runtime.product_line_id == PRODUCT_LINE_ID

    runtime_domain = runtime.domain_definition["domains"][0]
    product_lines = runtime_domain["spine"]

    trade_finance = next(
        item for item in product_lines
        if item["ProductLine"] == "Trade Finance"
    )

    assert any(
        fn["Function"] == "Trade Operations"
        for fn in trade_finance["functions"]
    )
    trade_operations = next(
        fn for fn in trade_finance["functions"]
        if fn["Function"] == "Trade Operations"
    )
    assert "Document Presentation & Booking" in trade_operations["subfunctions"]
    assert any(
        fn["Function"] == "Digital Trade Document Processing"
        for fn in trade_finance["functions"]
    )

    assert all(
        item["ProductLine"] != "Payments"
        for item in product_lines
    )
    assert "customer_context" not in runtime_domain

    pii_service = MagicMock(spec=PIIService)
    llm_client = MagicMock(spec=LLMClient)
    chat_client = MagicMock()
    chat_client.call_with_system_user.return_value = (
        '{"category": "Requirement", "confidence": 0.95, '
        '"reasoning_summary": "The block states a requirement.", '
        '"unmatched": false}',
        {"duration_ms": 1.0, "token_usage": {"prompt_chars": 10, "response_chars": 10}},
    )
    llm_client._create_chat_client.return_value = chat_client

    ie_client = IEClient(pii_service, llm_client)

    run_context = IngestionRunContext(
        tenant_context=TenantContext(
            tenant_id="tenant-bank-1",
            customer_id=CUSTOMER_ID,
            customer_name="Pulse AI Trade Finance",
            industry_id="banking",
        ),
        ontology=runtime,
    )
    ie_client.configure_run_context(run_context)

    assert ie_client.ontology_admission is not None
    assert {
        item["name"] for item in runtime.canonical_model["node_types"]
    } <= set(ie_client.ontology_admission.node_types)
    assert {
        item["name"] for item in runtime.canonical_model["relationship_types"]
    } <= set(ie_client.ontology_admission.relationship_types)

    admission = ie_client.ontology_admission.admit(
        [
            {
                "type": "Requirement",
                "value": "Card application requires identity verification",
                "confidence": 0.95,
            }
        ],
        [],
        source_text="Card application requires identity verification.",
        block_category="Requirement",
    )
    assert len(admission.admitted_entities) == 1
    assert admission.entity_rejection_reasons.get("unknown_node_type", 0) == 0

    block = CanonicalBlock(
        block_id="block-1",
        source_file="trade-finance.docx",
        file_type="docx",
        domain=DOMAIN_ID,
        page_number=1,
        block_index=0,
        block_type="paragraph",
        text="Trade document presentation requirement.",
        image_bytes=None,
        metadata={},
    )

    pii_service.mask_text.return_value = (
        block.text,
        block.text,
        [],
    )

    ie_client.enrich_blocks([block], domain=DOMAIN_ID)

    assert chat_client.call_with_system_user.called
    system_prompt = chat_client.call_with_system_user.call_args.kwargs["system_prompt"]
    user_content = chat_client.call_with_system_user.call_args.kwargs["user_content"]

    # Objective: verify the frozen run scope reaches the actual IE prompt.
    # This matters because extraction must honor the customer's selected scope,
    # not infer another product line or silently switch ontology versions.
    # Invariant: domain, product-line ID/name, and ontology version stay fixed.
    prompt = f"{system_prompt}\n{user_content}"
    assert f"Domain: {DOMAIN_ID}" in prompt
    assert f"Selected product line ID: {PRODUCT_LINE_ID}" in prompt
    assert "Selected product line: Trade Finance" in prompt
    assert "Customer ontology version: 3" in prompt

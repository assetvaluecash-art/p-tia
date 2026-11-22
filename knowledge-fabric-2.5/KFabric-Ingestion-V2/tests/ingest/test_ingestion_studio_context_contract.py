"""Contracts for customer-scoped Ingestion Studio setup."""

from types import SimpleNamespace

from kbfabric_ingest.fabric_foundry.customer import repository as customer_repository
from kbfabric_ingest.fabric_foundry.knowledge_contextualization import repository as ontology_repository
from kbfabric_ingest.scheduler import api


def _customer_ontology():
    definition = {
        "domains": [
            {
                "name": "consumer_retail",
                "display_name": "Consumer Retail",
                "spine": [
                    {"ProductLine": "Retail Cards", "functions": []},
                    {"ProductLine": "Personal Loans", "functions": []},
                ],
                "customer_context": {
                    "aliases": ["Retail Banking"],
                    "spine": {
                        "product_lines": {
                            "Retail Cards": {"applicable": True, "aliases": ["Cards"]},
                            "Personal Loans": {"applicable": False, "aliases": []},
                        },
                        "functions": {},
                        "sub_functions": {},
                    },
                    "additions": {
                        "product_lines": [
                            {"id": "customer-pl-1", "name": "Commercial Cards", "aliases": ["Business Cards"], "applicable": True}
                        ]
                    },
                },
            },
            {
                "name": "risk_compliance",
                "display_name": "Risk",
                "spine": [{"ProductLine": "Fraud", "functions": []}],
            },
        ]
    }
    customer = SimpleNamespace(
        customer_id="customer-1", customer_name="Example", tenant_id="tenant-1", industry_id="banking"
    )
    ontology = SimpleNamespace(
        definition=definition, customer_ontology_id="ontology-1", customer_ontology_version=3
    )
    return customer, ontology


def test_ingestion_context_lists_only_configured_customer_domain_and_product_lines(monkeypatch):
    """Objective: verify setup choices come only from the explicitly selected active customer's saved, applicable ontology.

    This matters because the selected business scope controls extraction, admission, and retrieval lineage.
    Invariant: unconfigured or inapplicable industry choices never appear, while saved customer additions and aliases do.
    """
    customer, ontology = _customer_ontology()

    class CustomerRepo:
        def get_by_customer_id(self, *, customer_id):
            assert customer_id == customer.customer_id
            return customer

    class OntologyRepo:
        def get_active(self, customer_id):
            assert customer_id == customer.customer_id
            return ontology

    monkeypatch.setattr(customer_repository, "CustomerRepository", CustomerRepo)
    monkeypatch.setattr(ontology_repository, "CustomerOntologyRepository", OntologyRepo)

    result = api._customer_ingestion_context(customer.customer_id)
    assert result["customer_id"] == "customer-1"
    assert [domain["id"] for domain in result["domains"]] == ["consumer_retail"]
    assert result["domains"][0]["name"] == "Retail Banking"
    assert result["domains"][0]["product_lines"] == [
        {"id": "DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards", "name": "Cards"},
        {"id": "customer-pl-1", "name": "Business Cards"},
    ]


def test_ingestion_studio_openapi_requires_scope_and_documents_shared_lane_contract():
    """Objective: verify OpenAPI tells the UI how to submit domain, product line, lane, and review mode.

    This matters because the studio and ingestion service are separate clients of this API contract.
    Invariant: submission requires explicit configured scope and QE/technology classification, and documents that both classifications share one pipeline.
    """
    schema = api.app.openapi()
    context = schema["paths"]["/ingestion/context"]["get"]
    assert context["summary"] == "Get configured domains and product lines for ingestion"
    assert "explicitly selected ACTIVE customer" in context["description"]
    assert "customer_id" in {item["name"] for item in context["parameters"]}
    assert "/ingestion/customers" in schema["paths"]

    request_schema = schema["components"]["schemas"]["ScheduleJobRequest"]
    assert {"customer_id", "domain", "product_line_id", "document_nature"} <= set(request_schema["required"])
    assert request_schema["properties"]["document_nature"]["description"].find("same ingestion pipeline") >= 0
    assert "hitl_mode" in request_schema["properties"]
    upload_schema_ref = schema["paths"]["/jobs/upload"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    upload_schema_name = upload_schema_ref.rsplit("/", 1)[-1]
    upload_schema = schema["components"]["schemas"][upload_schema_name]
    assert "customer_id" in upload_schema["required"]

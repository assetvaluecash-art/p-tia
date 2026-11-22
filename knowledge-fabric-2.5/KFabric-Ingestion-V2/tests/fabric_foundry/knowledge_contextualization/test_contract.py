from pydantic import ValidationError
import pytest

from kbfabric_ingest.fabric_foundry.knowledge_contextualization.contract import (
    CustomerOntologyContextualizationRequest,
)


def test_set_applicability_contract():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-001",
        changes=[
            {
                "operation": "SET_APPLICABILITY",
                "target": {
                    "type": "PRODUCT_LINE",
                    "canonical_id": "personal_banking",
                },
                "applicable": True,
            }
        ],
        change_reason="Customer context update",
    )

    assert request.customer_id == "CUST-001"
    assert request.domain_id == "consumer_retail"
    assert request.base_customer_ontology_version == 3
    assert request.changes[0].operation == "SET_APPLICABILITY"
    assert request.changes[0].target.canonical_id == "personal_banking"
    assert request.changes[0].applicable is True


def test_set_alias_contract():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-002",
        changes=[
            {
                "operation": "SET_ALIAS",
                "target": {
                    "type": "PRODUCT_LINE",
                    "canonical_id": "personal_banking",
                },
                "alias": "Retail Banking",
            }
        ],
    )

    assert request.domain_id == "consumer_retail"
    assert request.changes[0].operation == "SET_ALIAS"
    assert request.changes[0].alias == "Retail Banking"


def test_add_product_line_contract():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-003",
        changes=[
            {
                "operation": "ADD_PRODUCT_LINE",
                "product_line": {
                    "domain_id": "consumer_retail",
                    "name": "Digital Banking",
                    "description": "Customer-specific digital banking offering",
                    "aliases": ["Digital Bank"],
                },
            }
        ],
    )

    product_line = request.changes[0].product_line

    assert request.domain_id == "consumer_retail"
    assert request.changes[0].operation == "ADD_PRODUCT_LINE"
    assert product_line.domain_id == "consumer_retail"
    assert product_line.name == "Digital Banking"
    assert product_line.aliases == ["Digital Bank"]


def test_multiple_business_intents_can_be_submitted_together():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-004",
        changes=[
            {
                "operation": "SET_APPLICABILITY",
                "target": {
                    "type": "PRODUCT_LINE",
                    "canonical_id": "personal_banking",
                },
                "applicable": True,
            },
            {
                "operation": "SET_ALIAS",
                "target": {
                    "type": "PRODUCT_LINE",
                    "canonical_id": "personal_banking",
                },
                "alias": "Retail Banking",
            },
            {
                "operation": "ADD_PRODUCT_LINE",
                "product_line": {
                    "domain_id": "consumer_retail",
                    "name": "Digital Banking",
                },
            },
        ],
    )

    assert request.domain_id == "consumer_retail"
    assert len(request.changes) == 3


def test_add_function_contract():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-009",
        changes=[
            {
                "operation": "ADD_FUNCTION",
                "function": {
                    "domain_id": "consumer_retail",
                    "parent_product_line_id": "personal_banking",
                    "name": "Digital Account Services",
                    "description": "Customer-specific digital account services",
                    "aliases": ["Digital Accounts"],
                },
            }
        ],
    )

    function = request.changes[0].function

    assert request.domain_id == "consumer_retail"
    assert request.changes[0].operation == "ADD_FUNCTION"
    assert function.parent_product_line_id == "personal_banking"
    assert function.name == "Digital Account Services"
    assert function.aliases == ["Digital Accounts"]


def test_add_subfunction_contract():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-010",
        changes=[
            {
                "operation": "ADD_SUB_FUNCTION",
                "sub_function": {
                    "parent_function_id": "account_management",
                    "name": "Digital Account Opening",
                    "description": "Customer-specific digital account opening",
                    "aliases": ["Digital Onboarding"],
                },
            }
        ],
    )

    sub_function = request.changes[0].sub_function

    assert request.domain_id == "consumer_retail"
    assert request.changes[0].operation == "ADD_SUB_FUNCTION"
    assert sub_function.parent_function_id == "account_management"
    assert sub_function.name == "Digital Account Opening"
    assert sub_function.aliases == ["Digital Onboarding"]


def test_multiple_spine_levels_and_node_type_can_be_submitted_together():
    request = CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id="consumer_retail",
        base_customer_ontology_version=3,
        created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-011",
        changes=[
            {
                "operation": "SET_APPLICABILITY",
                "target": {
                    "type": "PRODUCT_LINE",
                    "canonical_id": "personal_banking",
                },
                "applicable": True,
            },
            {
                "operation": "SET_APPLICABILITY",
                "target": {
                    "type": "FUNCTION",
                    "canonical_id": "account_management",
                },
                "applicable": True,
            },
            {
                "operation": "SET_APPLICABILITY",
                "target": {
                    "type": "SUB_FUNCTION",
                    "canonical_id": "account_opening",
                },
                "applicable": True,
            },
        ],
    )

    assert request.domain_id == "consumer_retail"
    assert len(request.changes) == 3


def test_invalid_base_version_is_rejected():
    with pytest.raises(ValidationError):
        CustomerOntologyContextualizationRequest(
            customer_id="CUST-001",
            domain_id="consumer_retail",
            base_customer_ontology_version=0,
            created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-005",
            changes=[
                {
                    "operation": "SET_APPLICABILITY",
                    "target": {
                        "type": "PRODUCT_LINE",
                        "canonical_id": "personal_banking",
                    },
                    "applicable": True,
                }
            ],
        )


def test_empty_changes_are_rejected():
    with pytest.raises(ValidationError):
        CustomerOntologyContextualizationRequest(
            customer_id="CUST-001",
            domain_id="consumer_retail",
            base_customer_ontology_version=3,
            created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-006",
            changes=[],
        )


def test_unknown_operation_is_rejected():
    with pytest.raises(ValidationError):
        CustomerOntologyContextualizationRequest(
            customer_id="CUST-001",
            domain_id="consumer_retail",
            base_customer_ontology_version=3,
            created_by="SME-USER",
        confirmed_by="SME-USER",
        request_id="REQ-007",
            changes=[
                {
                    "operation": "ACTIVATE",
                    "target": {
                        "type": "PRODUCT_LINE",
                        "canonical_id": "personal_banking",
                    },
                }
            ],
        )

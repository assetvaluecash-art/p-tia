from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kbfabric_ingest.fabric_foundry.knowledge_contextualization.service import (
    CanonicalOntologyArtifactError,
    CanonicalOntologyNotFoundError,
    KnowledgeContextualizationService,
)


def make_customer():
    return SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )


def make_association():
    return SimpleNamespace(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
        is_default=True,
    )


def make_record():
    return SimpleNamespace(
        customer_ontology_id="ontology-version-id",
        customer_id="CUST-001",
        tenant_id="tenant-001",
        ontology_id="banking",
        ontology_version="5.0.1",
        customer_ontology_version=1,
    )


def make_service():
    customer_repository = Mock()
    industry_ontology_repository = Mock()
    customer_ontology_repository = Mock()
    customer_ontology_repository.get_by_request_id.return_value = None
    customer_ontology_repository.get_active.return_value = None

    service = KnowledgeContextualizationService(
        customer_repository=customer_repository,
        industry_ontology_repository=industry_ontology_repository,
        customer_ontology_repository=customer_ontology_repository,
    )

    return (
        service,
        customer_repository,
        industry_ontology_repository,
        customer_ontology_repository,
    )


def test_initialize_customer_ontology_creates_version_one_from_canonical_artifact(
    tmp_path: Path,
):
    service, customer_repository, industry_ontology_repository, customer_ontology_repository = make_service()

    customer_repository.get_by_customer_id.return_value = make_customer()

    industry_ontology_repository.list_for_industry.return_value = [
        make_association()
    ]

    ontology_path = tmp_path / "banking.yaml"
    ontology_path.write_text(
        """
version: 5.0.1
domains:
  - name: consumer_retail
    spine:
      - ProductLine: Personal Banking
        functions:
          - Function: Deposits
            subfunctions:
              - Account Opening
""",
        encoding="utf-8",
    )

    expected_record = make_record()
    customer_ontology_repository.create_customer_ontology.return_value = (
        expected_record
    )

    with patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_definition"
    ) as get_definition, patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_path",
        return_value=ontology_path,
    ):
        get_definition.return_value = SimpleNamespace(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            industry_id="banking",
            status="ACTIVE",
            path="config/enterprise_banking_ontology.yaml",
        )

        result = service.initialize_customer_ontology(
            customer_id="CUST-001",
            created_by="SME-USER",
            change_reason="Initial customer onboarding",
        )

    assert result is expected_record

    create_kwargs = customer_ontology_repository.create_customer_ontology.call_args.kwargs

    assert create_kwargs["customer_id"] == "CUST-001"
    assert create_kwargs["ontology_id"] == "banking"
    assert create_kwargs["ontology_version"] == "5.0.1"
    assert create_kwargs["customer_ontology_version"] == 1
    assert create_kwargs["status"] == "ACTIVE"
    assert create_kwargs["change_type"] == "INITIAL"
    assert create_kwargs["created_by"] == "SME-USER"
    assert create_kwargs["confirmed_by"] == "SME-USER"
    assert create_kwargs["change_reason"] == "Initial customer onboarding"
    assert create_kwargs["definition"]["domains"][0]["spine"][0]["ProductLine"] == (
        "Personal Banking"
    )

    assert create_kwargs["change_summary"] == {
        "operation": "INITIAL",
        "source": {
            "ontology_id": "banking",
            "ontology_name": "Enterprise Banking",
            "ontology_version": "5.0.1",
        },
        "snapshot": {
            "domains": 1,
            "product_lines": 1,
            "functions": 1,
            "subfunctions": 1,
        },
    }


def test_initialize_customer_ontology_is_idempotent_when_active_version_exists():
    service, customer_repository, _, customer_ontology_repository = make_service()
    customer_repository.get_by_customer_id.return_value = make_customer()
    existing = make_record()
    customer_ontology_repository.get_active.return_value = existing

    result = service.initialize_customer_ontology(
        customer_id="CUST-001",
        created_by="SME-USER",
    )

    assert result is existing
    customer_ontology_repository.create_customer_ontology.assert_not_called()


def test_initialize_customer_ontology_rejects_unknown_customer():
    service, customer_repository, _, _ = make_service()

    customer_repository.get_by_customer_id.return_value = None

    with pytest.raises(ValueError, match="Customer not found"):
        service.initialize_customer_ontology(customer_id="UNKNOWN")


def test_initialize_customer_ontology_rejects_missing_active_allocation():
    service, customer_repository, industry_ontology_repository, customer_ontology_repository = make_service()

    customer_repository.get_by_customer_id.return_value = make_customer()
    industry_ontology_repository.list_for_industry.return_value = []

    with pytest.raises(
        CanonicalOntologyNotFoundError,
        match="Expected exactly one active ontology allocation",
    ):
        service.initialize_customer_ontology(customer_id="CUST-001")


def test_initialize_customer_ontology_rejects_missing_artifact(tmp_path: Path):
    service, customer_repository, industry_ontology_repository, customer_ontology_repository = make_service()

    customer_repository.get_by_customer_id.return_value = make_customer()
    industry_ontology_repository.list_for_industry.return_value = [
        make_association()
    ]

    missing_path = tmp_path / "missing.yaml"

    with patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_definition",
        return_value=SimpleNamespace(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            industry_id="banking",
            status="ACTIVE",
            path="config/enterprise_banking_ontology.yaml",
        ),
    ), patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_path",
        return_value=missing_path,
    ):
        with pytest.raises(
            CanonicalOntologyArtifactError,
            match="Canonical ontology artifact not found",
        ):
            service.initialize_customer_ontology(customer_id="CUST-001")


def test_initialize_customer_ontology_rejects_non_mapping_yaml(tmp_path: Path):
    service, customer_repository, industry_ontology_repository, customer_ontology_repository = make_service()

    customer_repository.get_by_customer_id.return_value = make_customer()
    industry_ontology_repository.list_for_industry.return_value = [
        make_association()
    ]

    ontology_path = tmp_path / "invalid.yaml"
    ontology_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_definition",
        return_value=SimpleNamespace(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            industry_id="banking",
            status="ACTIVE",
            path="config/enterprise_banking_ontology.yaml",
        ),
    ), patch(
        "kbfabric_ingest.fabric_foundry.knowledge_contextualization.service.get_ontology_path",
        return_value=ontology_path,
    ):
        with pytest.raises(
            CanonicalOntologyArtifactError,
            match="must be a YAML mapping",
        ):
            service.initialize_customer_ontology(customer_id="CUST-001")


def test_initial_change_summary_counts_customer_ontology_spine():
    definition = {
        "domains": [
            {
                "name": "one",
                "spine": [
                    {
                        "ProductLine": "A",
                        "functions": [
                            {
                                "Function": "F1",
                                "subfunctions": ["S1", "S2"],
                            },
                            {
                                "Function": "F2",
                                "subfunctions": ["S3"],
                            },
                        ],
                    },
                    {
                        "ProductLine": "B",
                        "functions": [],
                    },
                ],
            },
            {
                "name": "two",
                "spine": [],
            },
        ]
    }

    summary = KnowledgeContextualizationService._build_initial_change_summary(
        definition=definition,
        ontology_definition=SimpleNamespace(
            ontology_id="test",
            ontology_name="Test",
            version="1.0.0",
        ),
    )

    assert summary["snapshot"] == {
        "domains": 2,
        "product_lines": 2,
        "functions": 2,
        "subfunctions": 3,
    }
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.contract import (
    CustomerOntologyContextualizationRequest,
    SetApplicabilityChange,
    SetAliasChange,
    SpineTargetRef,
    AddProductLineChange,
    AddFunctionChange,
    AddSubFunctionChange,
    CustomerProductLine,
    CustomerFunction,
    CustomerSubFunction,
)

def make_context_request(changes, *, domain_id="consumer_retail"):
    return CustomerOntologyContextualizationRequest(
        customer_id="CUST-001",
        domain_id=domain_id,
        base_customer_ontology_version=1,
        request_id="req-context-001",
        created_by="SME-USER",
        confirmed_by="SME-USER",
        changes=changes,
        change_reason="Customer domain contextualization",
    )

def make_context_definition():
    return {
        "version": "5.0.1",
        "domains": [
            {
                "name": "consumer_retail",
                "spine": [
                    {
                        "ProductLine": "Personal Banking",
                        "functions": [
                            {
                                "Function": "Deposits",
                                "subfunctions": ["Account Opening"],
                            }
                        ],
                    }
                ],
            },
            {
                "name": "corporate_commercial",
                "spine": [
                    {"ProductLine": "Lending", "functions": []}
                ],
            },
        ],
    }

def test_contextualize_customer_ontology_applies_one_domain_scoped_batch():
    service, customer_repository, _, customer_ontology_repository = make_service()
    customer_repository.get_by_customer_id.return_value = make_customer()
    customer_ontology_repository.get_active.return_value = SimpleNamespace(
        customer_ontology_version=1,
        definition=make_context_definition(),
    )
    expected_record = SimpleNamespace(customer_ontology_version=2)
    customer_ontology_repository.create_next_version.return_value = expected_record

    changes = [
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="PRODUCT_LINE", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking"),
            applicable=True,
        ),
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="FUNCTION", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking|FUNCTION:Deposits"),
            applicable=True,
        ),
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="SUB_FUNCTION", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking|FUNCTION:Deposits|SUB_FUNCTION:Account Opening"),
            applicable=True,
        ),
        SetAliasChange(
            operation="SET_ALIAS",
            target=SpineTargetRef(type="PRODUCT_LINE", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking"),
            alias="Retail Banking",
        ),
    ]

    result = service.contextualize_customer_ontology(
        make_context_request(changes),
                actor_id="test-user-001",
    )

    assert result is expected_record
    kwargs = customer_ontology_repository.create_next_version.call_args.kwargs
    assert kwargs["base_customer_ontology_version"] == 1
    assert kwargs["request_id"] == "req-context-001"
    assert kwargs["change_type"] == "CONTEXTUALIZATION"
    assert kwargs["created_by"] == "test-user-001"

    definition = kwargs["definition"]
    selected = definition["domains"][0]
    untouched = definition["domains"][1]
    context = selected["customer_context"]

    assert context["spine"]["product_lines"]["Personal Banking"] == {
        "applicable": True,
        "aliases": ["Retail Banking"],
    }
    assert context["spine"]["functions"]["Personal Banking"]["Deposits"]["applicable"] is True
    assert context["spine"]["sub_functions"]["Personal Banking"]["Deposits"]["Account Opening"]["applicable"] is True
    assert "customer_context" not in untouched
    customer_ontology_repository.create_next_version.assert_called_once()

def test_contextualize_customer_ontology_supports_customer_additions():
    service, customer_repository, _, customer_ontology_repository = make_service()
    customer_repository.get_by_customer_id.return_value = make_customer()
    customer_ontology_repository.get_active.return_value = SimpleNamespace(
        customer_ontology_version=1,
        definition=make_context_definition(),
    )
    customer_ontology_repository.create_next_version.return_value = SimpleNamespace(
        customer_ontology_version=2
    )

    changes = [
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="PRODUCT_LINE", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking"),
            applicable=True,
        ),
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="FUNCTION", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking|FUNCTION:Deposits"),
            applicable=True,
        ),
        SetApplicabilityChange(
            operation="SET_APPLICABILITY",
            target=SpineTargetRef(type="SUB_FUNCTION", canonical_id="DOMAIN:consumer_retail|PRODUCT_LINE:Personal Banking|FUNCTION:Deposits|SUB_FUNCTION:Account Opening"),
            applicable=True,
        ),
        AddProductLineChange(
            operation="ADD_PRODUCT_LINE",
            product_line=CustomerProductLine(
                id="cust-pl-001",
                domain_id="consumer_retail",
                name="Digital Banking",
                description="Customer-specific digital banking product line",
                aliases=["Digital"],
            ),
        ),
        AddFunctionChange(
            operation="ADD_FUNCTION",
            function=CustomerFunction(
                id="cust-fn-001",
                parent_product_line_id="cust-pl-001",
                name="Digital Onboarding",
                description="Customer-specific onboarding capability",
            ),
        ),
        AddSubFunctionChange(
            operation="ADD_SUB_FUNCTION",
            sub_function=CustomerSubFunction(
                id="cust-sf-001",
                parent_function_id="cust-fn-001",
                name="Digital Account Opening",
                description="Customer-specific digital account opening",
            ),
        ),
    ]

    service.contextualize_customer_ontology(
        make_context_request(changes),
        actor_id="test-user-001",
    )

    additions = customer_ontology_repository.create_next_version.call_args.kwargs[
        "definition"
    ]["domains"][0]["customer_context"]["additions"]

    assert additions["product_lines"][0]["id"] == "cust-pl-001"
    assert additions["product_lines"][0]["name"] == "Digital Banking"
    assert additions["functions"][0]["parent_product_line_id"] == "cust-pl-001"
    assert additions["sub_functions"][0]["parent_function_id"] == "cust-fn-001"

def test_supported_canonical_artifacts_have_domain_spines():
    from kbfabric_ingest.fabric_foundry.ontology.registry import get_ontology_path
    import yaml

    for ontology_id, ontology_version in [
        ("banking", "5.0.1"),
        ("health_insurance_care_services", "1.0.0"),
        ("insurance", "1.0.0"),
    ]:
        path = Path(get_ontology_path(ontology_id, ontology_version))
        assert path.exists(), f"Missing canonical artifact: {path}"
        definition = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert definition["domains"]
        assert all(isinstance(domain["spine"], list) for domain in definition["domains"])
        assert any(domain["spine"] for domain in definition["domains"])

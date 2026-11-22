from unittest.mock import Mock
from uuid import uuid4

import pytest

from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRecord,
)
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    EffectiveDomainOntologyNotFoundError,
    EffectiveDomainOntologyResolver,
    OntologyRuntimeContext,
    compile_customer_runtime_ontology,
)


def _record(definition):
    return CustomerOntologyRecord(
        customer_ontology_id=uuid4(),
        tenant_id="TENANT-1",
        customer_id="CUSTOMER-1",
        ontology_id="banking",
        ontology_version="5.0.1",
        customer_ontology_version=17,
        status="ACTIVE",
        definition=definition,
        change_type="CONTEXTUALIZATION",
        change_reason=None,
        change_summary={},
        parent_customer_ontology_id=None,
        created_by="SYSTEM",
        created_at=None,
        confirmed_by="SYSTEM",
        confirmed_at=None,
        updated_at=None,
        request_id=None,
    )


def test_resolves_selected_spine_per_domain_with_independent_applicability():
    """Verify each customer/domain run gets only its selected applicable spine.

    This matters because one customer can contextualize multiple domains
    differently. The invariant is that product lines, functions, and
    subfunctions from one domain never leak into another domain's runtime
    ontology, even when both share the same Customer Ontology version.
    """
    domain_a = {
        "name": "corporate_commercial",
        "spine": [
            {
                "ProductLine": "Lending",
                "functions": [
                    {
                        "Function": "Loan Origination",
                        "subfunctions": [
                            "Application Intake",
                            "Credit Assessment",
                        ],
                    },
                    {
                        "Function": "Loan Servicing",
                        "subfunctions": [
                            "Payment Processing",
                        ],
                    },
                ],
            },
            {
                "ProductLine": "Trade Finance",
                "functions": [
                    {
                        "Function": "Letter of Credit",
                        "subfunctions": ["Issuance"],
                    }
                ],
            },
        ],
        "customer_context": {
            "spine": {
                "product_lines": {
                    "Lending": {"applicable": True, "aliases": []},
                    "Trade Finance": {"applicable": False, "aliases": []},
                },
                "functions": {
                    "Lending": {
                        "Loan Origination": {"applicable": True, "aliases": []},
                        "Loan Servicing": {"applicable": False, "aliases": []},
                    }
                },
                "sub_functions": {
                    "Lending": {
                        "Loan Origination": {
                            "Application Intake": {"applicable": True, "aliases": []},
                            "Credit Assessment": {"applicable": False, "aliases": []},
                        }
                    }
                },
            }
        },
    }

    domain_b = {
        "name": "wealth_securities_management",
        "spine": [
            {
                "ProductLine": "Wealth Advisory",
                "functions": [
                    {
                        "Function": "Portfolio Management",
                        "subfunctions": [
                            "Portfolio Review",
                            "Tax Optimization",
                        ],
                    },
                    {
                        "Function": "Client Onboarding",
                        "subfunctions": ["KYC"],
                    },
                ],
            },
            {
                "ProductLine": "Securities",
                "functions": [
                    {
                        "Function": "Trading",
                        "subfunctions": ["Order Management"],
                    }
                ],
            },
        ],
        "customer_context": {
            "spine": {
                "product_lines": {
                    "Wealth Advisory": {"applicable": True, "aliases": []},
                    "Securities": {"applicable": False, "aliases": []},
                },
                "functions": {
                    "Wealth Advisory": {
                        "Portfolio Management": {"applicable": True, "aliases": []},
                        "Client Onboarding": {"applicable": False, "aliases": []},
                    }
                },
                "sub_functions": {
                    "Wealth Advisory": {
                        "Portfolio Management": {
                            "Portfolio Review": {"applicable": True, "aliases": []},
                            "Tax Optimization": {"applicable": False, "aliases": []},
                        }
                    }
                },
            }
        },
    }

    record = _record({"domains": [domain_a, domain_b]})

    context_a = OntologyRuntimeContext.from_customer_ontology(
        record,
        domain_id="corporate_commercial",
        product_line_id="Lending",
        product_line_name="Lending",
    )

    context_b = OntologyRuntimeContext.from_customer_ontology(
        record,
        domain_id="wealth_securities_management",
        product_line_id="Wealth Advisory",
        product_line_name="Wealth Advisory",
    )

    runtime_a = next(
        item
        for item in context_a.domain_definition["domains"]
        if item["name"] == "corporate_commercial"
    )
    runtime_b = next(
        item
        for item in context_b.domain_definition["domains"]
        if item["name"] == "wealth_securities_management"
    )

    assert runtime_a["spine"] == [
        {
            "ProductLine": "Lending",
            "functions": [
                {
                    "Function": "Loan Origination",
                    "subfunctions": ["Application Intake"],
                }
            ],
        }
    ]

    assert runtime_b["spine"] == [
        {
            "ProductLine": "Wealth Advisory",
            "functions": [
                {
                    "Function": "Portfolio Management",
                    "subfunctions": ["Portfolio Review"],
                }
            ],
        }
    ]

    assert context_a.product_line_name == "Lending"
    assert context_b.product_line_name == "Wealth Advisory"
    assert context_a.customer_ontology_version == context_b.customer_ontology_version == 17

    # Cross-domain leakage must be impossible.
    assert "Wealth Advisory" not in str(runtime_a["spine"])
    assert "Lending" not in str(runtime_b["spine"])

    # Compilation must not mutate the persisted Customer Ontology definition.
    assert record.definition["domains"][0]["spine"][0]["functions"][0]["subfunctions"] == [
        "Application Intake",
        "Credit Assessment",
    ]


def test_missing_domain_fails():
    """Verify ingestion cannot create runtime context for an unknown domain.

    This matters because the frozen run context must be anchored to an
    authoritative customer/domain ontology. The invariant is that an unresolved
    domain fails closed instead of producing partial or inferred runtime scope.
    """
    with pytest.raises(EffectiveDomainOntologyNotFoundError):
        OntologyRuntimeContext.from_customer_ontology(
            _record({"domains": [{"name": "corporate_commercial"}]}),
            domain_id="wealth_securities_management",
        )


def test_customer_aliases_are_the_only_runtime_spine_names():
    """Verify persisted aliases govern all runtime spine levels and additions.

    This matters because customer terminology is the source contract used by
    extraction, mapping, and graph traversal. Invariant: domain, product-line,
    function, and sub-function aliases replace canonical display names; aliases
    on customer-added product lines/functions/sub-functions are also compiled
    and exposed as the authoritative names.
    """
    canonical_product_line_id = (
        "DOMAIN:corporate_commercial|PRODUCT_LINE:Lending"
    )
    definition = {
        "domains": [
            {
                "name": "corporate_commercial",
                "display_name": "Corporate Commercial",
                "spine": [
                    {
                        "ProductLine": "Lending",
                        "functions": [
                            {
                                "Function": "Loan Origination",
                                "subfunctions": ["Application Intake"],
                            }
                        ],
                    }
                ],
                "customer_context": {
                    "aliases": ["Customer Business Domain"],
                    "spine": {
                        "product_lines": {
                            "Lending": {
                                "configured": True, "applicable": True,
                                "aliases": ["Credit Services"],
                            }
                        },
                        "functions": {
                            "Lending": {
                                "Loan Origination": {
                                    "applicable": True, "aliases": ["Card Origination"],
                                }
                            }
                        },
                        "sub_functions": {
                            "Lending": {
                                "Loan Origination": {
                                    "Application Intake": {
                                        "applicable": True,
                                        "aliases": ["Digital Application Capture"],
                                    }
                                }
                            }
                        },
                    },
                    "additions": {
                        "product_lines": [
                            {
                                "id": "customer-pl-specialty",
                                "name": "Specialty Lending",
                                "aliases": ["Customer Credit Products"],
                                "applicable": True,
                            }
                        ],
                        "functions": [
                            {
                                "id": "customer-fn-digital",
                                "parent_product_line_id": "customer-pl-specialty",
                                "name": "Digital Origination",
                                "aliases": ["Digital Credit Setup"],
                                "applicable": True,
                            }
                        ],
                        "sub_functions": [
                            {
                                "id": "customer-sf-mobile",
                                "parent_function_id": "customer-fn-digital",
                                "name": "Mobile Application",
                                "aliases": ["Mobile Credit Intake"],
                                "applicable": True,
                            }
                        ],
                    },
                },
            }
        ]
    }

    runtime = compile_customer_runtime_ontology(
        definition, domain_id="corporate_commercial"
    )
    runtime_domain = runtime["domains"][0]
    assert runtime_domain["name"] == "corporate_commercial"
    assert runtime_domain["display_name"] == "Customer Business Domain"
    assert runtime_domain["spine"] == [
        {
            "ProductLine": "Credit Services",
            "functions": [
                {
                    "Function": "Card Origination",
                    "subfunctions": ["Digital Application Capture"],
                }
            ],
        },
        {
            "ProductLine": "Customer Credit Products",
            "functions": [
                {
                    "Function": "Digital Credit Setup",
                    "subfunctions": ["Mobile Credit Intake"],
                }
            ],
        },
    ]

    record = _record(definition)
    context = OntologyRuntimeContext.from_customer_ontology(
        record,
        domain_id="corporate_commercial",
        product_line_id=canonical_product_line_id,
        product_line_name="Credit Services",
    )
    assert context.product_line_name == "Credit Services"

    from kbfabric_ingest.ingest.ontology_prompt import build_spine_hierarchy_section

    prompt_spine = build_spine_hierarchy_section(
        runtime, domain="corporate_commercial"
    )
    assert "Customer Business Domain" in prompt_spine
    assert "Credit Services" in prompt_spine
    assert "Card Origination" in prompt_spine
    assert "Digital Application Capture" in prompt_spine
    assert "Customer Credit Products" in prompt_spine
    assert "Digital Credit Setup" in prompt_spine
    assert "Mobile Credit Intake" in prompt_spine
    assert "Corporate Commercial" not in prompt_spine
    assert "Lending" not in prompt_spine
    assert "Loan Origination" not in prompt_spine
    assert "Application Intake" not in prompt_spine


def test_resolves_the_customer_ontology_version_frozen_at_submission():
    """Objective: verify a queued ingestion resolves its submitted ontology snapshot.

    This matters because customer contextualization may publish a newer version while
    a job waits in the scheduler. Invariant: an explicit version lookup uses that
    immutable customer snapshot and does not silently switch to the latest active one.
    """
    definition = {
        "domains": [
            {
                "name": "consumer_retail",
                "display_name": "Consumer Retail",
                "spine": [{"ProductLine": "Retail Cards", "functions": []}],
                "customer_context": {
                    "spine": {
                        "product_lines": {
                            "Retail Cards": {"configured": True, "applicable": True, "aliases": []}
                        },
                        "functions": {},
                        "sub_functions": {},
                    }
                },
            }
        ]
    }
    record = _record(definition)
    repository = Mock()
    repository.get_by_version.return_value = record

    context = EffectiveDomainOntologyResolver(repository).resolve(
        customer_id="CUSTOMER-1",
        domain_id="consumer_retail",
        product_line_id="DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
        customer_ontology_version=17,
    )

    assert context.customer_ontology_version == 17
    assert context.customer_id == "CUSTOMER-1"
    repository.get_by_version.assert_called_once_with("CUSTOMER-1", 17)
    repository.get_active.assert_not_called()

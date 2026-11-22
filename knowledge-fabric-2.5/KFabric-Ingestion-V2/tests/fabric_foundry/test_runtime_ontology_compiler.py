"""Runtime Customer Ontology compiler contract tests."""

from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    compile_customer_runtime_ontology,
)


def test_runtime_ontology_contains_only_applicable_customer_taxonomy():
    """Verify runtime contains only configured+applicable customer concepts.

    This matters because ingestion must obey the customer's persisted taxonomy,
    not expose canonical concepts that the customer did not select. The
    invariant is that non-applicable and unconfigured concepts never enter the
    runtime ontology.
    """
    definition = {
        "domains": [
            {
                "name": "corporate_commercial",
                "spine": [
                    {
                        "ProductLine": "Trade Finance",
                        "functions": [
                            {
                                "Function": "Trade Operations",
                                "subfunctions": [
                                    "Document Presentation & Booking",
                                    "Unused SubFunction",
                                ],
                            },
                            {
                                "Function": "Credit Assessment",
                                "subfunctions": [],
                            },
                        ],
                    }
                ],
                "customer_context": {
                    "spine": {
                        "product_lines": {
                            "Trade Finance": {
                                "configured": True,
                                "applicable": True,
                                "aliases": [],
                            }
                        },
                        "functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "configured": True,
                                    "applicable": True,
                                    "aliases": [],
                                },
                                "Credit Assessment": {
                                    "configured": True,
                                    "applicable": False,
                                    "aliases": ["Credit Evaluation"],
                                },
                            }
                        },
                        "sub_functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "Document Presentation & Booking": {
                                        "configured": True,
                                        "applicable": True,
                                        "aliases": [],
                                    },
                                    "Unused SubFunction": {
                                        "configured": True,
                                        "applicable": False,
                                        "aliases": [],
                                    },
                                }
                            }
                        },
                    },
                    "node_type_applicability": {
                        "Process": True,
                        "Control": False,
                    },
                    "additions": {
                        "product_lines": [],
                        "functions": [],
                        "sub_functions": [],
                    },
                },
            }
        ],
        "node_types": [
            {"name": "Process"},
            {"name": "Control"},
        ],
    }

    runtime = compile_customer_runtime_ontology(
        definition,
        domain_id="corporate_commercial",
    )

    domain = runtime["domains"][0]

    assert domain["name"] == "corporate_commercial"
    assert domain["spine"] == [
        {
            "ProductLine": "Trade Finance",
            "functions": [
                {
                    "Function": "Trade Operations",
                    "subfunctions": ["Document Presentation & Booking"],
                }
            ],
        }
    ]

    # Node types are canonical-model authority, not customer contextualization.
    assert "node_types" not in runtime


def test_runtime_ontology_alias_replaces_canonical_runtime_name():
    """Verify a configured customer alias becomes the runtime taxonomy name.

    This matters because customer contextualization exists to align ingestion
    with the customer's own vocabulary. The invariant is that an applicable
    alias is the runtime name used by extraction and KG resolution.
    """
    definition = {
        "domains": [
            {
                "name": "corporate_commercial",
                "spine": [
                    {
                        "ProductLine": "Lending",
                        "functions": [
                            {
                                "Function": "Credit Assessment",
                                "subfunctions": ["Credit Decision"],
                            }
                        ],
                    }
                ],
                "customer_context": {
                    "spine": {
                        "product_lines": {
                            "Lending": {
                                "configured": True,
                                "applicable": True,
                                "aliases": [],
                            }
                        },
                        "functions": {
                            "Lending": {
                                "Credit Assessment": {
                                    "configured": True,
                                    "applicable": True,
                                    "aliases": ["Credit Evaluation"],
                                }
                            }
                        },
                        "sub_functions": {
                            "Lending": {
                                "Credit Assessment": {
                                    "Credit Decision": {
                                        "configured": True,
                                        "applicable": True,
                                        "aliases": ["Decisioning"],
                                    }
                                }
                            }
                        },
                    },
                    "node_type_applicability": {},
                    "additions": {
                        "product_lines": [],
                        "functions": [],
                        "sub_functions": [],
                    },
                },
            }
        ],
        "node_types": [],
    }

    runtime = compile_customer_runtime_ontology(
        definition,
        domain_id="corporate_commercial",
    )

    assert runtime["domains"][0]["spine"] == [
        {
            "ProductLine": "Lending",
            "functions": [
                {
                    "Function": "Credit Evaluation",
                    "subfunctions": ["Decisioning"],
                }
            ],
        }
    ]


def test_runtime_ontology_includes_customer_added_hierarchy():
    """Verify customer-added concepts are reconstructed into the runtime spine.

    This matters because contextualization can extend any industry's semantic
    bed with customer-specific capabilities. The invariant is that applicable
    additions appear under their persisted parent IDs with the same
    domain/product-line/function/subfunction hierarchy expected by ingestion.
    """
    definition = {
        "domains": [
            {
                "name": "corporate_commercial",
                "spine": [
                    {
                        "ProductLine": "Trade Finance",
                        "functions": [
                            {
                                "Function": "Trade Operations",
                                "subfunctions": [
                                    "Document Presentation & Booking",
                                ],
                            }
                        ],
                    }
                ],
                "customer_context": {
                    "spine": {
                        "product_lines": {
                            "Trade Finance": {
                                "configured": True,
                                "applicable": True,
                                "aliases": [],
                            }
                        },
                        "functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "configured": True,
                                    "applicable": True,
                                    "aliases": [],
                                }
                            }
                        },
                        "sub_functions": {
                            "Trade Finance": {
                                "Trade Operations": {
                                    "Document Presentation & Booking": {
                                        "configured": True,
                                        "applicable": True,
                                        "aliases": [],
                                    }
                                }
                            }
                        },
                    },
                    "node_type_applicability": {
                        "Process": True,
                    },
                    "additions": {
                        "product_lines": [],
                        "functions": [
                            {
                                "id": "customer-fn-digital-trade-document-processing-v3",
                                "parent_product_line_id": (
                                    "DOMAIN:corporate_commercial|"
                                    "PRODUCT_LINE:Trade Finance"
                                ),
                                "name": "Digital Trade Document Processing",
                                "description": "Customer-specific digital processing capability.",
                                "aliases": [],
                                "applicable": True,
                            }
                        ],
                        "sub_functions": [
                            {
                                "id": "customer-sf-electronic-document-presentation-v3",
                                "parent_function_id": (
                                    "customer-fn-digital-trade-document-processing-v3"
                                ),
                                "name": "Electronic Document Presentation",
                                "description": "Customer-specific electronic presentation.",
                                "aliases": [],
                                "applicable": True,
                            }
                        ],
                    },
                },
            }
        ],
        "node_types": [
            {"name": "Process"},
        ],
    }

    runtime = compile_customer_runtime_ontology(
        definition,
        domain_id="corporate_commercial",
    )

    assert runtime["domains"][0]["spine"] == [
        {
            "ProductLine": "Trade Finance",
            "functions": [
                {
                    "Function": "Trade Operations",
                    "subfunctions": ["Document Presentation & Booking"],
                },
                {
                    "Function": "Digital Trade Document Processing",
                    "subfunctions": ["Electronic Document Presentation"],
                },
            ],
        }
    ]

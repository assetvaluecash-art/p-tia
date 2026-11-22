"""Regression tests for the canonical model and per-industry extraction policy contract."""

from copy import deepcopy

from kbfabric_ingest.fabric_foundry.runtime.industry_config import IndustryConfigLoader
from kbfabric_ingest.ingest.lane2_two_pass import (
    YamlOntologyProvider,
    build_lane2_ontology,
    build_pass1_system_prompt,
)


def test_supported_industries_load_complete_policies_against_shared_model():
    """Objective: verify banking, healthcare, and insurance load full policies over the same canonical graph model.

    This matters architecturally because industry differences belong in extraction guidance while
    node identities, relationship types, and valid endpoint triples stay platform-wide. It protects
    the invariant that each supported industry resolves a registered ontology and a complete policy.
    """
    loader = IndustryConfigLoader()
    loaded = [loader.load(industry) for industry in (
        "banking", "health_insurance_care_services", "insurance"
    )]
    canonical = loaded[0].model
    from kbfabric_ingest.fabric_foundry.ontology.registry import get_ontology_path
    from kbfabric_ingest.fabric_foundry.industry.registry import get_industries
    from kbfabric_ingest.fabric_foundry.bootstrap.service import BOOTSTRAP_INDUSTRIES

    industry_definitions = {
        definition.ontology_id: definition
        for industry in get_industries()
        for definition in industry.ontologies
    }
    bootstrap_paths = {item.ontology_id: item.artifact_path for item in BOOTSTRAP_INDUSTRIES}

    for config in loaded:
        expected_path = f"config/industries/{config.industry_id}/ontology.yaml"
        assert config.ontology.keys() == {"version", "domains"}
        assert str(get_ontology_path(config.industry_id)).endswith(expected_path)
        assert industry_definitions[config.industry_id].path == expected_path
        assert bootstrap_paths[config.industry_id] == expected_path
        assert config.model == canonical
        assert config.extraction_policy["industry_id"] == config.industry_id
        assert {item["name"] for item in config.extraction_policy["node_types"]} == {
            item["name"] for item in canonical["node_types"]
        }
        assert {item["name"] for item in config.extraction_policy["relationship_types"]} == {
            item["name"] for item in canonical["relationship_types"]
        }
        assert {item["name"] for item in config.extraction_policy["block_categories"]} == {
            item["name"] for item in canonical["block_categories"]
        }
        for category in config.extraction_policy["block_categories"]:
            assert isinstance(category.get("description"), str)
            assert all(isinstance(value, str) for value in category.get("anti_examples", []))
        for relationship in config.extraction_policy["relationship_types"]:
            assert relationship["name"] in {r["name"] for r in canonical["relationship_types"]}


def test_scoped_few_shots_follow_frozen_domain_and_product_line():
    """Objective: verify scoped IE examples include only examples matching the frozen run scope.

    This matters because industry policies may contain examples for multiple business areas, while
    customer ontology context selects one domain and product line per run. It protects the invariant
    that another domain/product-line example cannot steer extraction for the current ingestion.
    """
    config = IndustryConfigLoader().load("insurance")
    policy = deepcopy(config.extraction_policy)
    process = next(item for item in policy["node_types"] if item["name"] == "Process")
    process["ie_examples"] = [
        {"block_text": "current line marker", "scope": {
            "industry_id": "insurance", "domain_id": "consumer_retail",
            "product_line_id": "PL:current", "product_line_name": "Current Line",
        }, "entities": [], "relationships": []},
        {"block_text": "other line marker", "scope": {
            "industry_id": "insurance", "domain_id": "commercial",
            "product_line_id": "PL:other", "product_line_name": "Other Line",
        }, "entities": [], "relationships": []},
        {"block_text": "industry-wide marker", "entities": [], "relationships": []},
    ]
    ontology = build_lane2_ontology(
        customer_ontology=config.ontology,
        canonical_model=config.model,
        extraction_policy=policy,
    )
    provider = YamlOntologyProvider(
        ontology,
        run_scope={"industry_id": "insurance", "domain_id": "consumer_retail",
                   "product_line_id": "PL:current", "product_line_name": "Current Line"},
    )
    prompt = provider.get_prompt("Process").system_prompt

    assert "current line marker" in prompt
    assert "industry-wide marker" in prompt
    assert "other line marker" not in prompt


def test_industry_examples_are_visible_in_pass1_and_pass2_prompts():
    """Objective: verify industry category examples and anti-examples reach Pass 1 and type examples reach Pass 2.

    This matters because policy files only improve extraction if their guidance reaches the actual
    model requests. It protects the invariant that the industry's extraction policy participates in
    classification and extraction without changing the canonical allowed graph contract.
    """
    config = IndustryConfigLoader().load("health_insurance_care_services")
    policy = deepcopy(config.extraction_policy)
    requirement_category = next(
        item for item in policy["block_categories"] if item["name"] == "Requirement"
    )
    requirement_category["examples"] = ["health policy example sentinel"]
    requirement_category["anti_examples"] = ["health policy exclusion sentinel"]
    requirement_node = next(item for item in policy["node_types"] if item["name"] == "Requirement")
    requirement_node["examples"] = ["Health Requirement Sentinel"]
    ontology = build_lane2_ontology(
        customer_ontology=config.ontology,
        canonical_model=config.model,
        extraction_policy=policy,
    )

    pass1 = build_pass1_system_prompt(ontology)
    pass2 = YamlOntologyProvider(ontology).get_prompt("Requirement").system_prompt
    assert "health policy example sentinel" in pass1
    assert "health policy exclusion sentinel" in pass1
    assert "Health Requirement Sentinel" in pass2


def test_active_ie_templates_are_industry_neutral_and_scope_driven():
    """Objective: verify shared text/vision templates contain no banking-specific contract examples.

    This matters because every industry uses the same extraction implementation and the active
    customer's ontology supplies the business vocabulary. It protects the invariant that prompts do
    not silently bias healthcare or insurance runs toward Banking labels or edge types.
    """
    from kbfabric_ingest.llm.prompts import get_system_prompt

    forbidden = ("Basel III", "SWIFT Gateway", "LOS_Core", "RBI circular", "FINANCIAL SERVICES")
    for operation in ("ie_text", "ie_vision"):
        prompt = get_system_prompt(operation)
        assert all(term.casefold() not in prompt.casefold() for term in forbidden)
    assert "{ontology_context}" in get_system_prompt("ie_text")
    assert "{ontology_context}" in get_system_prompt("ie_vision")


def test_healthcare_and_insurance_effective_prompts_exclude_legacy_banking_examples():
    """Objective: verify runtime policy composition removes stale Banking examples from non-Banking IE prompts.

    This matters because the registered legacy ontology files can contain old sample artifacts even
    when the industry policy is correct. It protects the invariant that the prompt actually sent for
    healthcare and insurance uses their policy and spine, without inherited Banking examples.
    """
    from kbfabric_ingest.ingest.ontology_prompt import build_full_ontology_prompt

    markers = ("Basel III", "SWIFT Gateway", "LOS_Core", "RBI circular", "OFAC SDN")
    for industry in ("health_insurance_care_services", "insurance"):
        config = IndustryConfigLoader().load(industry)
        ontology = build_lane2_ontology(
            customer_ontology=config.ontology,
            canonical_model=config.model,
            extraction_policy=config.extraction_policy,
        )
        prompt = "\n".join((
            build_full_ontology_prompt(ontology),
            build_pass1_system_prompt(ontology),
            YamlOntologyProvider(ontology).get_prompt("Requirement").system_prompt,
        ))
        assert all(marker.casefold() not in prompt.casefold() for marker in markers)

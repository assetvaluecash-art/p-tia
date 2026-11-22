from pathlib import Path

import pytest

from kbfabric_ingest.fabric_foundry.runtime.industry_config import (
    IndustryConfigError,
    IndustryConfigLoader,
)


def test_loads_three_separate_industry_config_contracts():
    """Verify one industry resolves ontology, model, and extraction policy separately.

    This matters architecturally because Fabric Foundry and ingestion must not
    collapse the three configuration responsibilities back into one monolithic
    YAML contract. The invariant is that each required configuration remains a
    distinct mapping loaded from its own file.
    """
    config = IndustryConfigLoader().load("banking")

    assert config.industry_id == "banking"
    assert config.ontology["version"]
    assert config.ontology["domains"]

    assert config.model["version"]
    assert config.model["block_categories"]
    assert config.model["node_types"]
    assert config.model["relationship_types"]
    assert config.model["vision_ie_type_mapping"]
    assert config.model["path_format"]

    assert config.extraction_policy["version"]
    assert config.extraction_policy["node_types"]
    assert config.extraction_policy["relationship_types"]


def test_missing_industry_fails():
    """Verify an unknown industry cannot silently fall back to another config.

    This matters because runtime configuration must be industry-specific and
    deterministic. The invariant is that an unresolved industry raises an
    explicit configuration error rather than loading banking or another
    default configuration.
    """
    with pytest.raises(IndustryConfigError):
        IndustryConfigLoader().load("does_not_exist")


def test_loader_uses_configured_industry_directory(tmp_path: Path):
    """Verify the loader resolves all three files from the selected industry directory.

    This matters because adding industries must require configuration changes,
    not Python branching. The invariant is that ontology, model, and extraction
    policy are loaded from the same selected industry directory.
    """
    industry_dir = tmp_path / "test_industry"
    industry_dir.mkdir()

    (industry_dir / "ontology.yaml").write_text(
        "version: '1.0'\ndomains: []\n",
        encoding="utf-8",
    )
    (industry_dir / "model.yaml").write_text(
        "version: '1.0'\nblock_categories: {}\n",
        encoding="utf-8",
    )
    (industry_dir / "extraction_policy.yaml").write_text(
        "version: '1.0'\nnode_types: {}\nrelationship_types: {}\n",
        encoding="utf-8",
    )

    config = IndustryConfigLoader(base_dir=tmp_path).load("test_industry")

    assert config.industry_id == "test_industry"
    assert config.ontology["version"] == "1.0"
    assert config.model["version"] == "1.0"
    assert config.extraction_policy["version"] == "1.0"

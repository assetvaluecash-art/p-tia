from pathlib import Path

import pytest
import yaml

from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)

ONTOLOGY_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "enterprise_banking_ontology.yaml"
)


@pytest.fixture(scope="module")
def ontology():
    logger.info(
        "Starting ontology structural validation: path=%s",
        ONTOLOGY_PATH,
    )

    assert ONTOLOGY_PATH.exists(), (
        f"Ontology file does not exist: {ONTOLOGY_PATH}"
    )

    with ONTOLOGY_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    assert isinstance(data, dict), (
        "Ontology YAML must load as a mapping."
    )

    logger.info("Ontology YAML parsed successfully.")

    return data


def _named_items(ontology, key):
    return [
        item
        for item in (ontology.get(key) or [])
        if isinstance(item, dict) and item.get("name")
    ]


def test_ontology_yaml_parses(ontology):
    logger.info("Validating ontology YAML parse result.")

    assert ontology.get("node_types") is not None
    assert ontology.get("relationship_types") is not None
    assert ontology.get("block_categories") is not None

    logger.info("Ontology YAML parse validation passed.")


def test_node_types_are_unique_and_expected_count(ontology):
    logger.info("Validating ontology node types.")

    nodes = _named_items(ontology, "node_types")
    names = [item["name"] for item in nodes]

    assert len(names) == 18, (
        f"Expected 18 node types, found {len(names)}."
    )

    assert len(names) == len(set(names)), (
        "Duplicate node type names found."
    )

    logger.info(
        "Node type validation passed: count=%d.",
        len(names),
    )


def test_relationship_types_are_unique_and_expected_count(ontology):
    logger.info("Validating ontology relationship types.")

    relationships = _named_items(
        ontology,
        "relationship_types",
    )
    names = [item["name"] for item in relationships]

    assert len(names) == 17, (
        f"Expected 17 relationship types, found {len(names)}."
    )

    assert len(names) == len(set(names)), (
        "Duplicate relationship type names found."
    )

    logger.info(
        "Relationship type validation passed: count=%d.",
        len(names),
    )


def test_block_categories_are_unique_and_expected_count(ontology):
    logger.info("Validating ontology block categories.")

    categories = _named_items(
        ontology,
        "block_categories",
    )
    names = [item["name"] for item in categories]

    assert len(names) == 17, (
        f"Expected 17 block categories, found {len(names)}."
    )

    assert len(names) == len(set(names)), (
        "Duplicate block category names found."
    )

    logger.info(
        "Block category validation passed: count=%d.",
        len(names),
    )


def test_block_category_node_mappings_are_valid(ontology):
    logger.info(
        "Validating block category maps_to_node values."
    )

    node_names = {
        item["name"]
        for item in _named_items(ontology, "node_types")
    }

    allowed_skip_values = {"_SKIP_KG"}

    errors = []

    for category in _named_items(
        ontology,
        "block_categories",
    ):
        name = category["name"]
        mapping = category.get("maps_to_node")

        if mapping not in node_names and mapping not in allowed_skip_values:
            errors.append(
                f"{name}: invalid maps_to_node={mapping!r}"
            )

    assert not errors, "\n".join(errors)

    logger.info(
        "Block category node mapping validation passed."
    )


def test_relationship_endpoint_types_are_valid(ontology):
    logger.info(
        "Validating relationship endpoint node types."
    )

    node_names = {
        item["name"]
        for item in _named_items(ontology, "node_types")
    }

    errors = []

    for relationship in _named_items(
        ontology,
        "relationship_types",
    ):
        name = relationship["name"]

        allowed_triples = relationship.get("allowed_triples")

        if allowed_triples is not None:
            assert isinstance(allowed_triples, list), (
                f"{name}: allowed_triples must be a list."
            )

            for index, triple in enumerate(allowed_triples):
                assert isinstance(triple, dict), (
                    f"{name}[{index}]: triple must be a mapping."
                )

                from_type = triple.get("from")
                to_type = triple.get("to")

                if from_type not in node_names:
                    errors.append(
                        f"{name}[{index}]: invalid from type "
                        f"{from_type!r}"
                    )

                if to_type not in node_names:
                    errors.append(
                        f"{name}[{index}]: invalid to type "
                        f"{to_type!r}"
                    )

            continue

        from_types = relationship.get("from")
        to_types = relationship.get("to")

        if isinstance(from_types, str):
            from_types = [from_types]

        if isinstance(to_types, str):
            to_types = [to_types]

        assert isinstance(from_types, list), (
            f"{name}: from must be a string or list."
        )

        assert isinstance(to_types, list), (
            f"{name}: to must be a string or list."
        )

        if len(from_types) != len(to_types):
            errors.append(
                f"{name}: from/to endpoint lists have different lengths."
            )

        for from_type in from_types:
            if from_type not in node_names:
                errors.append(
                    f"{name}: invalid from type {from_type!r}"
                )

        for to_type in to_types:
            if to_type not in node_names:
                errors.append(
                    f"{name}: invalid to type {to_type!r}"
                )

    assert not errors, "\n".join(errors)

    logger.info(
        "Relationship endpoint validation passed."
    )


def test_relationship_definitions_have_required_fields(ontology):
    logger.info(
        "Validating relationship definition structure."
    )

    errors = []

    for relationship in _named_items(
        ontology,
        "relationship_types",
    ):
        name = relationship["name"]

        if not relationship.get("description"):
            errors.append(
                f"{name}: missing description."
            )

        if not relationship.get("verb"):
            errors.append(
                f"{name}: missing verb."
            )

        has_allowed_triples = (
            relationship.get("allowed_triples") is not None
        )

        has_legacy_endpoints = (
            relationship.get("from") is not None
            and relationship.get("to") is not None
        )

        if not has_allowed_triples and not has_legacy_endpoints:
            errors.append(
                f"{name}: missing endpoint definition."
            )

    assert not errors, "\n".join(errors)

    logger.info(
        "Relationship definition structure validation passed."
    )

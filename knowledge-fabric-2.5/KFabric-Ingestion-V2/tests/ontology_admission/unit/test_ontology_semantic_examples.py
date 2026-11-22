import re
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
        "Starting semantic ontology example validation: path=%s",
        ONTOLOGY_PATH,
    )

    with ONTOLOGY_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    assert isinstance(data, dict)
    return data


def _named_items(ontology, key):
    return [
        item
        for item in (ontology.get(key) or [])
        if isinstance(item, dict) and item.get("name")
    ]


def _node_types(ontology):
    return {
        item["name"]: item
        for item in _named_items(ontology, "node_types")
    }


def _block_categories(ontology):
    return {
        item["name"]: item
        for item in _named_items(ontology, "block_categories")
    }


def _examples(definition):
    return definition.get("ie_examples") or []


def _example_relationships(example):
    relationships = example.get("relationships") or []

    if isinstance(relationships, dict):
        relationships = [relationships]

    return [
        item
        for item in relationships
        if isinstance(item, dict)
    ]


def _example_entities(example):
    entities = example.get("entities") or []

    if isinstance(entities, dict):
        entities = [entities]

    return [
        item
        for item in entities
        if isinstance(item, dict)
    ]


def _relationship_signature(relationship):
    """
    Normalize the ontology's relationship example representation.

    Current examples use:
      source
      target
      type

    Typed examples may additionally contain:
      from_type
      to_type
    """
    source = str(
        relationship.get("source")
        or relationship.get("from")
        or ""
    ).strip()

    target = str(
        relationship.get("target")
        or relationship.get("to")
        or ""
    ).strip()

    relationship_type = str(
        relationship.get("type")
        or relationship.get("relationship")
        or ""
    ).strip()

    return source, relationship_type, target


def test_positive_examples_have_no_duplicate_relationships(ontology):
    logger.info(
        "Validating semantic examples for duplicate relationships."
    )

    duplicates = []

    for node_type, definition in _node_types(ontology).items():
        for index, example in enumerate(_examples(definition)):
            seen = set()

            for relationship in _example_relationships(example):
                signature = _relationship_signature(relationship)

                # Ignore empty relationship entries rather than
                # treating multiple empty placeholders as duplicates.
                if not any(signature):
                    continue

                if signature in seen:
                    duplicates.append(
                        f"{node_type} example {index}: "
                        f"duplicate relationship {signature!r}"
                    )

                seen.add(signature)

    assert not duplicates, "\n".join(duplicates)

    logger.info(
        "Duplicate relationship validation passed."
    )


def test_positive_example_relationship_endpoints_are_extracted(ontology):
    logger.info(
        "Validating semantic example relationship endpoints."
    )

    errors = []

    for node_type, definition in _node_types(ontology).items():
        for index, example in enumerate(_examples(definition)):
            entity_values = {
                str(
                    entity.get("name")
                    or entity.get("value")
                    or ""
                ).strip()
                for entity in _example_entities(example)
                if entity.get("name") or entity.get("value")
            }

            for relationship in _example_relationships(example):
                source, relationship_type, target = (
                    _relationship_signature(relationship)
                )

                if not any(
                    (source, relationship_type, target)
                ):
                    continue

                if source and source not in entity_values:
                    errors.append(
                        f"{node_type} example {index}: "
                        f"relationship source {source!r} "
                        "is not present in extracted entities."
                    )

                if target and target not in entity_values:
                    errors.append(
                        f"{node_type} example {index}: "
                        f"relationship target {target!r} "
                        "is not present in extracted entities."
                    )

    assert not errors, "\n".join(errors)

    logger.info(
        "Semantic relationship endpoint validation passed."
    )


def test_typed_relationship_examples_match_extracted_entity_types(
    ontology,
):
    logger.info(
        "Validating typed relationship examples."
    )

    errors = []

    for node_type, definition in _node_types(ontology).items():
        for index, example in enumerate(_examples(definition)):
            entity_types_by_name = {}

            for entity in _example_entities(example):
                entity_name = str(
                    entity.get("name")
                    or entity.get("value")
                    or ""
                ).strip()

                entity_type = str(
                    entity.get("type")
                    or ""
                ).strip()

                if entity_name and entity_type:
                    entity_types_by_name[entity_name] = entity_type

            for relationship in _example_relationships(example):
                source, _, target = _relationship_signature(
                    relationship
                )

                declared_source_type = relationship.get(
                    "from_type"
                )
                declared_target_type = relationship.get(
                    "to_type"
                )

                if declared_source_type:
                    actual_source_type = (
                        entity_types_by_name.get(source)
                    )

                    if (
                        actual_source_type
                        and actual_source_type
                        != declared_source_type
                    ):
                        errors.append(
                            f"{node_type} example {index}: "
                            f"{source!r} declared as "
                            f"{declared_source_type!r}, "
                            f"but extracted as "
                            f"{actual_source_type!r}."
                        )

                if declared_target_type:
                    actual_target_type = (
                        entity_types_by_name.get(target)
                    )

                    if (
                        actual_target_type
                        and actual_target_type
                        != declared_target_type
                    ):
                        errors.append(
                            f"{node_type} example {index}: "
                            f"{target!r} declared as "
                            f"{declared_target_type!r}, "
                            f"but extracted as "
                            f"{actual_target_type!r}."
                        )

    assert not errors, "\n".join(errors)

    logger.info(
        "Typed relationship example validation passed."
    )


def test_rule_instructions_teach_condition_and_consequence(ontology):
    logger.info(
        "Validating Rule extraction instructions."
    )

    rule = _node_types(ontology).get("Rule")
    assert rule is not None

    definition_text = " ".join(
        str(rule.get(field, ""))
        for field in (
            "classification",
            "description",
            "definition",
        )
    ).lower()

    assert "condition" in definition_text
    assert "consequence" in definition_text

    examples = _examples(rule)
    assert examples, (
        "Rule must have positive extraction examples."
    )

    conditional_patterns = [
        r"\bif\b",
        r"\bwhen\b",
        r"\bunless\b",
        r"\brequire(?:s|d)?\b",
        r"\bmust\b",
        r"\bshall\b",
    ]

    for index, example in enumerate(examples):
        example_text = " ".join(
            str(value)
            for key, value in example.items()
            if key in {"text", "description", "block_text"}
        ).lower()

        assert any(
            re.search(pattern, example_text)
            for pattern in conditional_patterns
        ), (
            f"Rule example {index} does not demonstrate "
            "conditional or obligation logic."
        )

    logger.info(
        "Rule condition/consequence teaching validation passed."
    )


def test_skip_kg_block_categories_are_explicitly_skipped(ontology):
    logger.info(
        "Validating _SKIP_KG block category semantics."
    )

    categories = _block_categories(ontology)

    skip_categories = [
        name
        for name, definition in categories.items()
        if definition.get("maps_to_node") == "_SKIP_KG"
    ]

    assert skip_categories, (
        "Ontology must explicitly define _SKIP_KG categories."
    )

    logger.info(
        "_SKIP_KG categories validated: %s",
        ", ".join(skip_categories),
    )


def test_kg_producing_block_categories_map_to_known_nodes(ontology):
    logger.info(
        "Validating KG-producing block category mappings."
    )

    node_types = _node_types(ontology)
    categories = _block_categories(ontology)

    errors = []

    for category_name, category in categories.items():
        mapped_node = category.get("maps_to_node")

        if mapped_node == "_SKIP_KG":
            continue

        if mapped_node not in node_types:
            errors.append(
                f"{category_name}: maps_to_node={mapped_node!r} "
                "is not a known node type."
            )

    assert not errors, "\n".join(errors)

    logger.info(
        "KG-producing block category mapping validation passed."
    )


def test_node_examples_are_valid_when_present(ontology):
    logger.info(
        "Validating node IE examples where examples are defined."
    )

    errors = []

    for node_type, definition in _node_types(ontology).items():
        for index, example in enumerate(_examples(definition)):
            if not isinstance(example, dict):
                errors.append(
                    f"{node_type} example {index}: "
                    "example must be a mapping."
                )
                continue

            if not example.get("text") and not example.get(
                "block_text"
            ):
                errors.append(
                    f"{node_type} example {index}: "
                    "missing source example text."
                )

            entities = _example_entities(example)

            if not entities:
                errors.append(
                    f"{node_type} example {index}: "
                    "missing extracted entities."
                )

    assert not errors, "\n".join(errors)

    logger.info(
        "Node IE example validation passed."
    )


def test_semantic_teaching_contract_is_complete(ontology):
    """
    Final semantic guardrail.

    This checks that the ontology contains the instructional fields
    required by the LLM extraction contract. It does not require every
    ontology node to have a positive IE example because some nodes are
    intentionally contextual/spine/reference nodes.
    """
    logger.info(
        "Validating ontology LLM teaching contract."
    )

    errors = []

    spine_nodes_without_ie_examples = {
        "DomainRoot",
        "ProductLine",
        "Function",
        "SubFunction",
    }

    for node_type, definition in _node_types(ontology).items():
        for field in ("description",):
            if not definition.get(field):
                errors.append(
                    f"{node_type}: missing {field!r}."
                )

        if node_type not in spine_nodes_without_ie_examples:
            if not _examples(definition):
                errors.append(
                    f"{node_type}: extractable node type must have "
                    "at least one positive IE example."
                )

    for category_name, definition in _block_categories(
        ontology
    ).items():
        for field in ("description", "maps_to_node"):
            if not definition.get(field):
                errors.append(
                    f"{category_name}: missing {field!r}."
                )

    assert not errors, "\n".join(errors)

    logger.info(
        "Ontology LLM teaching contract validation passed."
    )

"""Tests for ontology and BlockCategory enum validation."""

import pytest

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.models import BlockCategory


def test_blockcategory_enum_matches_ontology():
    """Test that BlockCategory enum matches ontology block_categories."""
    ontology = get_ontology()
    block_category_entries = ontology["block_categories"]
    category_names = [entry["name"] for entry in block_category_entries]
    block_category_to_node = ontology["block_category_to_node"]
    
    # Check count match
    assert len(block_category_entries) == len(BlockCategory), (
        f"BlockCategory enum has {len(BlockCategory)} values, "
        f"but ontology has {len(block_category_entries)} entries"
    )
    
    # Check every enum value is in category_names
    enum_values = {bc.value for bc in BlockCategory}
    missing_in_ontology = enum_values - set(category_names)
    assert not missing_in_ontology, (
        f"BlockCategory enum values not found in ontology: {missing_in_ontology}"
    )
    
    # Check every category_name has a mapping in block_category_to_node
    missing_mappings = set(category_names) - set(block_category_to_node.keys())
    assert not missing_mappings, (
        f"Block categories missing from block_category_to_node: {missing_mappings}"
    )
    
    # Check every enum value has a mapping
    for bc in BlockCategory:
        assert bc.value in block_category_to_node, (
            f"BlockCategory {bc.value} missing from block_category_to_node"
        )


def test_blockcategory_to_node_mapping():
    """Test that block_category_to_node has entries for each BlockCategory."""
    ontology = get_ontology()
    block_category_to_node = ontology["block_category_to_node"]
    
    for bc in BlockCategory:
        assert bc.value in block_category_to_node, (
            f"Missing mapping for {bc.value}"
        )
        assert isinstance(block_category_to_node[bc.value], str), (
            f"Mapping for {bc.value} is not a string"
        )



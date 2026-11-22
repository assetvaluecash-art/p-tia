"""Tests for Requirement description preservation in deterministic IE paths."""

from __future__ import annotations

from kbfabric_ingest.ingest.requirement_entity_fields import (
    description_from_kv_pairs,
    entity_with_optional_fields,
    extract_requirement_description_from_text,
    parse_kv_pairs,
)


FR01_STATEMENT = (
    "The system shall allow bullet repayment configuration for SME Fixed and "
    "Variable loan products during loan creation."
)


def test_parse_kv_pairs_supports_colon_separated_table_rows():
    text = (
        "Requirement ID: FR-01, Requirement Description: "
        "The system shall validate repayment type before booking."
    )
    pairs = parse_kv_pairs(text)
    assert pairs["Requirement ID"] == "FR-01"
    assert "validate repayment type" in pairs["Requirement Description"]


def test_description_from_kv_pairs_reads_requirement_description_column():
    kv = {
        "Requirement ID": "FR-01",
        "Requirement Description": FR01_STATEMENT,
    }
    assert description_from_kv_pairs(kv) == FR01_STATEMENT


def test_extract_requirement_description_from_fr_prefix():
    text = f"FR-01: {FR01_STATEMENT}"
    assert extract_requirement_description_from_text(text, "FR-01") == FR01_STATEMENT


def test_rule_based_shape_populates_description_for_fr_colon_block():
    text = f"FR-01: {FR01_STATEMENT}"
    entity = entity_with_optional_fields(
        "Requirement",
        "FR-01",
        0.90,
        description=extract_requirement_description_from_text(text, "FR-01"),
    )

    assert entity["type"] == "Requirement"
    assert entity["value"] == "FR-01"
    assert entity["description"] == FR01_STATEMENT


def test_structured_table_row_shape_populates_requirement_description():
    text = (
        "Requirement ID: FR-02, Requirement Description: "
        "The system shall apply the new bullet repayment structure at booking."
    )
    kv = parse_kv_pairs(text)
    entity = entity_with_optional_fields(
        "Requirement",
        kv["Requirement ID"],
        0.99,
        description=description_from_kv_pairs(kv),
    )

    assert entity["value"] == "FR-02"
    assert entity["description"] == (
        "The system shall apply the new bullet repayment structure at booking."
    )


def test_id_only_requirement_leaves_description_absent():
    entity = entity_with_optional_fields(
        "Requirement",
        "FR-03",
        0.90,
        description=extract_requirement_description_from_text("FR-03", "FR-03"),
    )

    assert entity["value"] == "FR-03"
    assert "description" not in entity

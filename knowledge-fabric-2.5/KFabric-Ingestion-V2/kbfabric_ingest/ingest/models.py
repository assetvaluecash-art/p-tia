"""Pydantic models for ingestion pipeline.

BlockCategory enum is DYNAMICALLY GENERATED from enterprise_banking_ontology.yaml.
This eliminates the need to manually sync Python code with ontology changes.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.config import get_ontology


# =============================================================================
# DYNAMIC ENUM GENERATION FROM ONTOLOGY (Single Source of Truth)
# =============================================================================
# Load ontology once at module import time
_ontology = get_ontology()
_block_category_entries = _ontology["block_categories"]
_category_names = [entry["name"] for entry in _block_category_entries]

# Build block_category_to_node mapping:
# New format uses maps_to_node on each category; old format uses a separate section
_block_category_to_node = _ontology.get("block_category_to_node", {})
if not _block_category_to_node:
    _block_category_to_node = {}
    for entry in _block_category_entries:
        name = entry.get("name", "")
        maps_to = entry.get("maps_to_node", entry.get("default_node_type"))
        if name and maps_to:
            _block_category_to_node[name] = maps_to

# DYNAMICALLY GENERATE BlockCategory enum from ontology YAML
# This is the single source of truth - no manual Python code maintenance needed
# When you add/remove categories in enterprise_banking_ontology.yaml, they automatically appear here
BlockCategory = Enum(  # type: ignore[misc]
    "BlockCategory",
    {name: name for name in _category_names},
    type=str
)
BlockCategory.__doc__ = """Enumeration of block categories - DYNAMICALLY GENERATED from enterprise_banking_ontology.yaml.

To add a new category:
1. Edit config/enterprise_banking_ontology.yaml - add to block_categories list
2. Add mapping in block_category_to_node section
3. That's it! No Python code changes needed.

Current categories (from ontology):
""" + "\n".join(f"  - {name}" for name in _category_names)


def get_block_category_to_node_mapping() -> dict[str, str]:
    """
    Get the authoritative mapping from BlockCategory to KG node type.
    
    Returns:
        Dictionary mapping block category names to KG node type names
    """
    return _block_category_to_node.copy()


class CanonicalBlock(BaseModel):
    """Canonical representation of a document block."""

    block_id: str  # UUID string
    source_file: str  # relative path from project root
    file_type: str  # "docx", "pdf", "xlsx", "txt"
    domain: str  # derived from top level folder, e.g., "trade_finance"
    page_number: int | None = None
    block_index: int  # sequential index per file
    block_type: str  # "heading", "paragraph", "table_row", "glossary_row", "test_case_row", "incident_row", "defect_row", "image"
    text: str  # raw text from parser
    image_bytes: bytes | None = None
    metadata: dict[str, Any]  # extra hints, table coords, heading level, etc
    # Semantic grouping metadata (optional)
    semantic_unit_id: str | None = None
    source_block_ids: list[str] | None = None
    unit_type: str | None = None
    unit_part_index: int | None = None
    unit_part_total: int | None = None
    # Provenance timestamps for audit trail
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # When block was first parsed


class EnrichedBlock(BaseModel):
    """Enriched block with IE results, embeddings, and dual-mode PII masking."""

    block: CanonicalBlock
    block_category: BlockCategory
    kg_node_type: str | None = None
    spine_node: str = "domain_root"
    candidate_entities: list[dict[str, Any]]  # each dict: {"type": str, "value": str, "confidence": float}
    candidate_relationships: list[dict[str, Any]]  # each dict: {"from_type": str, "from_value": str, "rel_type": str, "to_type": str, "to_value": str, "confidence": float}
    embedding: list[float]  # Embedding vector (dimension depends on model)
    pii_spans: list[dict[str, Any]]  # each dict: {"entity_type": str, "start": int, "end": int, "text": str, "redacted": str, "faker": str}
    
    # Dual-mode PII fields (enterprise strategy)
    original_text_redacted: str  # Text with [REDACTED_TYPE] placeholders for audit logs
    masked_text: str  # Text with faker replacements for retrieval/LLM

    # Diagram/image audit fields (optional; populated for block_type == "image")
    ocr_text_raw: str | None = None
    ocr_text_masked: str | None = None
    vision_description_raw: str | None = None
    vision_description_masked: str | None = None
    diagram_caption_text_raw: str | None = None
    diagram_caption_text_masked: str | None = None
    vision_model: str | None = None
    vision_duration_ms: float | None = None

    # OCR diagnostics (help explain empty OCR results in audits)
    ocr_available: bool | None = None
    ocr_error: str | None = None

    # Vision IE extracted data (optional; populated for block_type == "image")
    extracted_fields: list[dict[str, Any]] | None = None
    extracted_knowledge: list[dict[str, Any]] | None = None
    semantic_chunks: list[dict[str, Any]] | None = None
    vision_api_method: str | None = None  # "chat" or "generate" - tracks KV cache usage



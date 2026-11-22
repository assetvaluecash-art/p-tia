"""Tests for ID-based entity description extraction, persistence identity, and retrieval display."""

from __future__ import annotations

import hashlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field
from typing import Optional

# Stub heavy optional deps before importing retrieval / reconciliation paths
_emb = ModuleType("custom_ml.embeddings")
_emb_client = ModuleType("custom_ml.embeddings.embeddings_client")
_emb_client.EmbeddingsClient = MagicMock
_emb_client.FinBERTEmbeddingsClient = MagicMock
_emb.EmbeddingsClient = MagicMock
_emb.FinBERTEmbeddingsClient = MagicMock
sys.modules["custom_ml"] = ModuleType("custom_ml")
sys.modules["custom_ml.embeddings"] = _emb
sys.modules["custom_ml.embeddings.embeddings_client"] = _emb_client

_rr = ModuleType("custom_ml.reranking")
_rr_client = ModuleType("custom_ml.reranking.reranker_client")
_rr_client.RerankerClient = MagicMock
_rr.RerankerClient = MagicMock
sys.modules["custom_ml.reranking"] = _rr
sys.modules["custom_ml.reranking.reranker_client"] = _rr_client

sys.modules.setdefault("transformers", ModuleType("transformers"))

_telemetry = ModuleType("kbfabric_ingest._common.telemetry")
_telemetry.get_tracer = lambda: MagicMock()
_telemetry.record_neo4j_write = MagicMock()
_telemetry.record_neo4j_node = MagicMock()
_telemetry.record_neo4j_relationship = MagicMock()
_telemetry.record_ie_entity_filtered = MagicMock()
sys.modules.setdefault("kbfabric_ingest._common.telemetry", _telemetry)

from kbfabric_ingest.retrieval.hybrid_retriever import HybridRetriever
from kbfabric_ingest.retrieval.node_display import (
    format_node_for_retrieval,
    ontology_type_descriptions,
)


class _VisionEntityCompat(BaseModel):
    """Mirrors VisionEntity schema for local validation without importing llm.client."""

    type: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    aliases: Optional[list[str]] = None
    description: Optional[str] = None


VisionEntity = _VisionEntityCompat


FR04_STATEMENT = (
    "The system shall auto-generate a repayment schedule (EMI or bullet) upon "
    "loan booking, reflecting selected interest type, tenor, and repayment frequency."
)
FR04_DISPLAY = f"FR-04 : {FR04_STATEMENT}"


# ---------------------------------------------------------------------------
# Display formatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canonical,instance,ontology,expected",
    [
        (
            "FR-04",
            FR04_STATEMENT,
            "Functional or non-functional requirement statement.",
            FR04_DISPLAY,
        ),
        (
            "AC-FR04-01",
            "Given a new loan is booked with bullet repayment, When the schedule is generated, Then the system shall create a single principal payment at maturity with periodic interest-only payments.",
            None,
            "AC-FR04-01 : Given a new loan is booked with bullet repayment, When the schedule is generated, Then the system shall create a single principal payment at maturity with periodic interest-only payments.",
        ),
        (
            "SYN-CL-3015",
            "Interest Accrual not suspended after NAB classification",
            None,
            "SYN-CL-3015 : Interest Accrual not suspended after NAB classification",
        ),
        (
            "INC-LOAN-089",
            "Production incident — bullet repayment not applied to 12 new loans booked after config change.",
            None,
            "INC-LOAN-089 : Production incident — bullet repayment not applied to 12 new loans booked after config change.",
        ),
        (
            "CLOS_DEL_SCN_004_TC_001",
            "Delinquency processing for Long Term Loan",
            None,
            "CLOS_DEL_SCN_004_TC_001 : Delinquency processing for Long Term Loan",
        ),
        (
            "Bullet Repayment Configuration",
            "As a relationship manager, I want to configure bullet repayment schedules for SME loans, so that product setup reflects the agreed terms.",
            None,
            "Bullet Repayment Configuration : As a relationship manager, I want to configure bullet repayment schedules for SME loans, so that product setup reflects the agreed terms.",
        ),
        # No instance description → ontology type fallback
        (
            "FR-04",
            None,
            "Functional or non-functional requirement statement. Core node for coverage and compliance tracing.",
            "FR-04 : Functional or non-functional requirement statement. Core node for coverage and compliance tracing.",
        ),
        # No description at all → bare ID
        ("FR-04", None, None, "FR-04"),
        ("FR-04", "", "", "FR-04"),
    ],
)
def test_format_node_for_retrieval(canonical, instance, ontology, expected):
    assert (
        format_node_for_retrieval(
            canonical,
            instance_description=instance,
            ontology_type_description=ontology,
        )
        == expected
    )


def test_format_prefers_instance_over_ontology():
    display = format_node_for_retrieval(
        "FR-04",
        instance_description=FR04_STATEMENT,
        ontology_type_description="Generic requirement type text",
    )
    assert display == FR04_DISPLAY
    assert "Generic requirement" not in display


def test_format_does_not_duplicate_already_joined_description():
    already = FR04_DISPLAY
    assert (
        format_node_for_retrieval("FR-04", instance_description=already) == already
    )


# ---------------------------------------------------------------------------
# IE schema backward compatibility
# ---------------------------------------------------------------------------


def test_vision_entity_accepts_optional_description():
    ent = VisionEntity(
        type="Requirement",
        value="FR-04",
        confidence=0.95,
        description=FR04_STATEMENT,
    )
    assert ent.value == "FR-04"
    assert ent.description == FR04_STATEMENT


def test_vision_entity_without_description_still_valid():
    ent = VisionEntity(type="Requirement", value="FR-04", confidence=0.95)
    assert ent.description is None
    assert ent.value == "FR-04"


# ---------------------------------------------------------------------------
# Canonical identity / relationship matching
# ---------------------------------------------------------------------------


def _entity_id(domain: str, entity_type: str, value: str) -> str:
    normalized = value.strip().lower()
    return hashlib.sha256(f"{domain}:{entity_type}:{normalized}".encode()).hexdigest()[:16]


def test_canonical_value_unchanged_for_fr04():
    canonical_value = "FR-04"
    retrieval_display = format_node_for_retrieval(
        canonical_value, instance_description=FR04_STATEMENT
    )
    assert canonical_value == "FR-04"
    assert retrieval_display == FR04_DISPLAY


def test_relationship_ids_match_same_entity_hash():
    """AC referencing FR-04 must resolve to the same entity_id as Requirement FR-04."""
    req_id = _entity_id("corporate_commercial", "Requirement", "FR-04")
    ref_id = _entity_id("corporate_commercial", "Requirement", "FR-04")
    assert req_id == ref_id

    # Enriched display string must NOT be used as identity
    bad_id = _entity_id("corporate_commercial", "Requirement", FR04_DISPLAY)
    assert bad_id != req_id


def test_id_only_reference_does_not_invent_description():
    """Reference entities with only an ID leave description empty; display uses stored/fallback later."""
    ref = {"type": "Requirement", "value": "FR-04", "confidence": 0.90}
    assert "description" not in ref or not ref.get("description")
    # Formatter without instance desc does not invent unknown
    assert format_node_for_retrieval("FR-04") == "FR-04"


# ---------------------------------------------------------------------------
# Reconciliation wiring
# ---------------------------------------------------------------------------


def test_entity_to_incoming_wires_description():
    from kbfabric_ingest.ingest.lane2_node_reconciliation import entity_to_incoming
    from kbfabric_ingest.ingest.models import CanonicalBlock

    block = CanonicalBlock(
        block_id="b1",
        domain="corporate_commercial",
        source_file="req.md",
        file_type="txt",
        text=f"FR-04: {FR04_STATEMENT}",
        block_type="paragraph",
        block_index=0,
        metadata={},
    )
    incoming = entity_to_incoming(
        {
            "type": "Requirement",
            "value": "FR-04",
            "description": FR04_STATEMENT,
            "confidence": 0.95,
        },
        block,
    )
    assert incoming.name == "FR-04"
    assert incoming.description == FR04_STATEMENT
    assert "description" not in incoming.properties


def test_entity_to_incoming_without_description():
    from kbfabric_ingest.ingest.lane2_node_reconciliation import entity_to_incoming
    from kbfabric_ingest.ingest.models import CanonicalBlock

    block = CanonicalBlock(
        block_id="b2",
        domain="corporate_commercial",
        source_file="ac.md",
        file_type="txt",
        text="AC-FR04-01 validates FR-04",
        block_type="paragraph",
        block_index=0,
        metadata={},
    )
    incoming = entity_to_incoming(
        {"type": "Requirement", "value": "FR-04", "confidence": 0.90},
        block,
    )
    assert incoming.name == "FR-04"
    assert incoming.description is None


# ---------------------------------------------------------------------------
# Retriever path formatting
# ---------------------------------------------------------------------------


def test_format_path_emits_display_and_keeps_canonical():
    path = {
        "chain_symbolic": '(Requirement:"FR-04")-[:IMPACTS]→(Process:"Loan Booking")',
        "chain_natural": (
            f"{FR04_DISPLAY} impacts Loan Booking"
        ),
        "_decay_score": 0.9,
        "_node_chain": [
            {
                "name": "FR-04",
                "display_name": FR04_DISPLAY,
                "type": "Requirement",
                "description": FR04_STATEMENT,
            },
            {
                "name": "Loan Booking",
                "display_name": "Loan Booking",
                "type": "Process",
                "description": "",
            },
        ],
        "_rel_chain": ["IMPACTS"],
        "_source_block_id": "b1",
        "_source_file": "req.md",
        "_source_page": 1,
    }
    out = HybridRetriever._format_path(path)
    assert out["from_entity"]["name"] == FR04_DISPLAY
    assert out["from_entity"]["canonical_name"] == "FR-04"
    assert out["to_entity"]["canonical_name"] == "Loan Booking"
    assert 'Requirement:"FR-04"' in out["path_text"]
    assert FR04_STATEMENT in out["source_evidence"]


def test_ontology_type_descriptions_loads_requirement():
    ontology = {
        "node_types": [
            {
                "name": "Requirement",
                "description": (
                    "Functional or non-functional requirement statement. "
                    "Core node for coverage and compliance tracing."
                ),
            }
        ]
    }
    mapping = ontology_type_descriptions(ontology)
    assert "Requirement" in mapping
    assert "coverage and compliance" in mapping["Requirement"]

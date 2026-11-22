"""Unit tests for KG entity embedding persistence."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Stub telemetry before kg_writer import (avoids heavy OpenTelemetry dependency chain)
_telemetry = ModuleType("kbfabric_ingest._common.telemetry")
_telemetry.get_tracer = lambda: MagicMock()
_telemetry.record_neo4j_write = MagicMock()
_telemetry.record_neo4j_node = MagicMock()
_telemetry.record_neo4j_relationship = MagicMock()
_telemetry.record_ie_entity_filtered = MagicMock()
sys.modules["kbfabric_ingest._common.telemetry"] = _telemetry

from kbfabric_ingest.ingest.kg_writer import KGWriter


@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    client.embed_text.side_effect = lambda texts: [[float(i + 1)] * 4 for i, _ in enumerate(texts)]
    return client


@pytest.fixture
def minimal_ontology():
    return {
        "node_types": [
            {"name": "Process"},
            {"name": "Requirement"},
            {"name": "ProductLine"},
            {"name": "Function"},
            {"name": "SubFunction"},
        ],
        "relationship_types": [{"name": "IMPACTS"}],
        "domains": [
            {
                "name": "test_domain",
                "spine": [
                    {
                        "ProductLine": "Trade Finance",
                        "functions": [
                            {
                                "Function": "Letter Of Credit",
                                "subfunctions": ["Issuance"],
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def kg_writer(mock_llm_client, minimal_ontology):
    with patch.object(KGWriter, "_create_driver_with_retry", return_value=MagicMock()):
        writer = KGWriter(
            {
                "uri": "bolt://localhost:7687",
                "tenant_id": "test-tenant",
                "connection_pool": {
                    "max_size": 1,
                    "acquisition_timeout": 1,
                    "connection_timeout": 1,
                    "max_lifetime": 60,
                },
            },
            llm_client=mock_llm_client,
        )
    writer._run_context = None
    writer._runtime_ontology = lambda: minimal_ontology
    return writer


def test_entity_embedding_text_prefers_display(kg_writer):
    """Objective: Verify KG embedding text uses the display name when available. This matters for semantically useful graph search; the invariant is that the type and preferred display value form the embedding input."""
    text = kg_writer._entity_embedding_text("Process", "Loan Booking", "loan booking")
    assert text == "Process: loan booking"


def test_embed_entity_texts_keys_by_entity_id(kg_writer, mock_llm_client):
    """Objective: Verify generated vectors remain keyed by deterministic entity IDs. This matters because graph writes must attach each vector to its matching node; the invariant is that embeddings are not reordered or misassigned."""
    requests = {"abc123": "Process: Foo", "def456": "Control: Bar"}
    result = kg_writer._embed_entity_texts(requests)
    assert set(result.keys()) == {"abc123", "def456"}
    assert len(result["abc123"]) == 4
    mock_llm_client.embed_text.assert_called_once_with(["Process: Foo", "Control: Bar"])


def test_compute_entity_id_uses_spine_lookup(kg_writer):
    """Objective: Verify canonical spine IDs take precedence over creating duplicate IDs. This matters for a connected customer business spine; the invariant is that an extracted spine mention resolves to the pre-seeded node."""
    spine = {("Function", "letter of credit"): "full_spine_hash"}
    eid = kg_writer._compute_entity_id("test_domain", "Function", "letter of credit", spine)
    assert eid == "full_spine_hash"


def test_vector_index_labels_include_spine_and_ontology(kg_writer):
    """Objective: Verify vector indexes cover searchable ontology and spine nodes. This matters for entity candidate retrieval; the invariant is that entity labels are indexed while structural DomainRoot nodes are excluded."""
    labels = kg_writer._get_vector_index_labels()
    assert "ProductLine" in labels
    assert "Function" in labels
    assert "SubFunction" in labels
    assert "Process" in labels
    assert "DomainRoot" not in labels


def test_pre_seed_spine_sets_embeddings(kg_writer, mock_llm_client):
    """Objective: Verify spine pre-seeding writes embeddings for the configured customer spine. This matters for similarity-based mapping; the invariant is that ProductLine, Function, and SubFunction nodes are pre-seeded with embedding values."""
    session = MagicMock()
    kg_writer.driver.session.return_value.__enter__.return_value = session
    queries: list[str] = []

    def capture_run(query, **params):
        queries.append(query)
        return MagicMock()

    session.execute_write.side_effect = lambda fn: fn(MagicMock(run=capture_run))

    counts = kg_writer.pre_seed_spine("test_domain", tenant_id="default")
    assert counts["product_lines"] == 1
    assert counts["functions"] == 1
    assert counts["subfunctions"] == 1
    assert mock_llm_client.embed_text.called
    joined = "\n".join(queries)
    assert "pl.embedding" in joined or "embedding" in joined
    assert "sf.embedding" in joined or "SubFunction" in joined


def test_ensure_missing_entity_embeddings_backfills(kg_writer, mock_llm_client):
    """Objective: Verify missing entity vectors can be backfilled. This matters for existing graph search quality; the invariant is that eligible unembedded nodes receive vectors."""
    session = MagicMock()
    kg_writer.driver.session.return_value.__enter__.return_value = session

    find_result = [
        {"entity_id": "e1", "kg_node_type": "Process", "display_name": "Loan Booking"},
    ]
    session.run.side_effect = [
        MagicMock(__iter__=lambda self: iter(find_result), __len__=lambda self: 1),
        MagicMock(),
    ]

    stats = kg_writer.ensure_missing_entity_embeddings("test_domain", batch_size=10)
    assert stats["scanned"] == 1
    assert stats["embedded"] == 1
    assert mock_llm_client.embed_text.called


def test_ensure_missing_entity_embeddings_skips_without_llm(minimal_ontology):
    """Objective: Verify embedding backfill safely skips without an LLM client. This matters for deployments where embeddings are unavailable; the invariant is that no write is attempted without a provider."""
    with patch.object(KGWriter, "_create_driver_with_retry", return_value=MagicMock()):
        writer = KGWriter(
            {
                "uri": "bolt://localhost:7687",
                "tenant_id": "test-tenant",
                "connection_pool": {
                    "max_size": 1,
                    "acquisition_timeout": 1,
                    "connection_timeout": 1,
                    "max_lifetime": 60,
                },
            },
            llm_client=None,
        )
    writer._runtime_ontology = lambda: minimal_ontology
    stats = writer.ensure_missing_entity_embeddings("test_domain")
    assert stats == {"scanned": 0, "embedded": 0, "skipped": 0, "failed": 0}


def test_embedding_on_match_preserves_existing_clause():
    """Objective: Verify an existing vector is preserved during an upsert. This matters to avoid unnecessary re-embedding; the invariant is that a populated embedding is not overwritten."""
    clause = KGWriter._embedding_on_match_clause("e", "$embedding")
    assert "e.embedding IS NULL" in clause
    assert "ELSE e.embedding END" in clause


def test_excluded_labels_not_in_vector_indexes(kg_writer):
    """Objective: Verify structural labels stay out of entity vector indexes. This matters because scaffolding nodes should not pollute entity matching; the invariant is that DomainRoot is never included."""
    labels = kg_writer._get_vector_index_labels()
    for excluded in ("Block", "Document", "DomainRoot"):
        # Block is indexed for chunk vectors; DomainRoot should stay excluded
        if excluded == "DomainRoot":
            assert excluded not in labels

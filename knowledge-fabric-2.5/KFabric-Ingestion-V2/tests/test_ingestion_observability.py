"""Unit tests for structured ingestion observability logging.

These tests are fully self-contained: they use lightweight stubs and never
require Weaviate, Neo4j, Ray, Ollama, or the embedding model. Run with
``SKIP_SERVICE_CHECKS=1`` so the session-level health-check fixture is skipped.
"""

import types

from kbfabric_ingest.utils.ingestion_observability import (
    get_ingestion_logger,
    get_observability_config,
    log_stage_start,
    log_stage_end,
    log_stage_error,
    safe_len,
    redact_for_log,
    redact_uri,
    summarize_blocks,
    summarize_enriched_blocks,
    trace_value_ref,
)


class FakeLogger:
    """Captures structlog-style calls as (level, event, kwargs) tuples."""

    def __init__(self):
        self.events = []

    def info(self, event, **kwargs):
        self.events.append(("info", event, kwargs))

    def warning(self, event, **kwargs):
        self.events.append(("warning", event, kwargs))

    def error(self, event, **kwargs):
        self.events.append(("error", event, kwargs))


# ---------------------------------------------------------------------------
# safe_len / redaction
# ---------------------------------------------------------------------------

def test_safe_len_handles_none_and_unsized():
    assert safe_len(None) == 0
    assert safe_len("abc") == 3
    assert safe_len([1, 2, 3]) == 3
    assert safe_len(123) == 0  # int has no len -> 0, no exception


def test_redact_for_log_never_leaks_raw_text():
    secret = "supersecret-password-12345"
    redacted = redact_for_log(secret)
    assert secret not in redacted
    assert "len=" in redacted
    assert "sha=" in redacted
    # numbers/bools are not sensitive and are kept as-is
    assert redact_for_log(42) == "42"
    assert redact_for_log(True) == "True"
    assert redact_for_log(None) == "<none>"


def test_trace_value_ref_is_stable_and_does_not_expose_raw_value():
    value = "Confidential process name"
    reference = trace_value_ref(value)
    assert reference == trace_value_ref(value)
    assert reference.startswith("sha256:")
    assert value not in reference
    assert trace_value_ref(None) == "sha256:none"


def test_redact_uri_strips_credentials():
    assert redact_uri("bolt://neo4j:s3cr3t@localhost:7687") == "bolt://localhost:7687"
    assert redact_uri("neo4j+s://user:pw@graph.example:7687") == "neo4j+s://graph.example:7687"
    # No credentials -> unchanged
    assert redact_uri("bolt://localhost:7687") == "bolt://localhost:7687"
    # Hostless / malformed -> never raises
    assert redact_uri("") == ""
    assert redact_uri(None) == ""
    # Credentials without scheme
    assert redact_uri("user:pw@host:7687") == "host:7687"


def test_redact_uri_output_never_contains_password():
    out = redact_uri("bolt://admin:hunter2@db:7687")
    assert "hunter2" not in out
    assert "admin" not in out


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------

def test_log_stage_start_and_end_emit_events_with_duration():
    logger = FakeLogger()
    started = log_stage_start(logger, "parse", job_id="ingest-1", domain="d", file_count=2)
    assert isinstance(started, float)
    log_stage_end(logger, "parse", started, job_id="ingest-1", domain="d", block_count=145)

    assert logger.events[0][1] == "ingestion.parse.start"
    start_kwargs = logger.events[0][2]
    assert start_kwargs["stage"] == "parse"
    assert start_kwargs["file_count"] == 2

    assert logger.events[1][1] == "ingestion.parse.end"
    end_kwargs = logger.events[1][2]
    assert end_kwargs["block_count"] == 145
    assert end_kwargs["duration_ms"] is not None
    assert end_kwargs["duration_ms"] >= 0


def test_fully_qualified_event_name_is_used_verbatim():
    logger = FakeLogger()
    started = log_stage_start(logger, "ingestion.parse.file", source_file="a.pdf")
    assert logger.events[0][1] == "ingestion.parse.file"
    log_stage_end(logger, "ingestion.parse.file", started, block_count=5)
    assert logger.events[1][1] == "ingestion.parse.file"


def test_log_stage_error_records_redacted_error_metadata():
    logger = FakeLogger()
    err = ValueError("boom: bolt://neo4j:pw@host")
    log_stage_error(logger, "neo4j.write", err, domain="d")
    level, event, kwargs = logger.events[0]
    assert level == "error"
    assert event == "ingestion.neo4j.write.error"
    assert kwargs["error_type"] == "ValueError"
    assert "boom" in kwargs["error_message"]


# ---------------------------------------------------------------------------
# Block summaries (duck-typed objects)
# ---------------------------------------------------------------------------

def _canonical_like(**kwargs):
    base = dict(
        block_type="paragraph",
        file_type="pdf",
        source_file="doc.pdf",
        text="hello world",
        image_bytes=None,
        semantic_unit_id=None,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def test_summarize_blocks_counts_and_lengths():
    blocks = [
        _canonical_like(block_type="heading", text="Title", semantic_unit_id="u1"),
        _canonical_like(block_type="paragraph", text="hello", semantic_unit_id="u1"),
        _canonical_like(block_type="table_row", text="a|b|c", file_type="xlsx", source_file="s.xlsx"),
        _canonical_like(block_type="image", text="", image_bytes=b"123", semantic_unit_id="u2"),
    ]
    summary = summarize_blocks(blocks)
    assert summary["total"] == 4
    assert summary["by_block_type"]["heading"] == 1
    assert summary["by_block_type"]["table_row"] == 1
    assert summary["by_file_type"]["pdf"] == 3
    assert summary["by_file_type"]["xlsx"] == 1
    assert summary["image_block_count"] == 1
    assert summary["table_block_count"] == 1
    assert summary["semantic_unit_count"] == 2
    assert summary["text_len_min"] == 0
    assert summary["text_len_max"] == 5
    assert summary["text_len_avg"] is not None


def test_summarize_blocks_empty():
    summary = summarize_blocks([])
    assert summary["total"] == 0
    assert summary["text_len_avg"] is None


def _enriched_like(category="Requirement", entities=None, relationships=None,
                   embedding=None, kg_node_type="Requirement", pii_spans=None,
                   image_bytes=None):
    return types.SimpleNamespace(
        block=types.SimpleNamespace(image_bytes=image_bytes),
        block_category=types.SimpleNamespace(value=category),
        kg_node_type=kg_node_type,
        candidate_entities=entities or [],
        candidate_relationships=relationships or [],
        embedding=embedding or [],
        pii_spans=pii_spans or [],
    )


def test_summarize_enriched_blocks_counts_and_dim_no_vector_values():
    blocks = [
        _enriched_like(
            category="Requirement",
            entities=[{"type": "Process", "value": "X"}, {"type": "Control", "value": "Y"}],
            relationships=[{"rel_type": "USES", "from_value": "X", "to_value": "Y"}],
            embedding=[0.1] * 1024,
            pii_spans=[{"entity_type": "PERSON"}],
        ),
        _enriched_like(category="Narrative", entities=[], embedding=[0.2] * 1024, kg_node_type=None),
    ]
    summary = summarize_enriched_blocks(blocks)
    assert summary["total"] == 2
    assert summary["entity_count"] == 2
    assert summary["relationship_count"] == 1
    assert summary["entity_count_by_type"]["Process"] == 1
    assert summary["relationship_count_by_type"]["USES"] == 1
    assert summary["by_block_category"]["Requirement"] == 1
    assert summary["embedding_count"] == 2
    assert summary["embedding_dim"] == 1024
    assert summary["pii_span_count"] == 1
    # Ensure no raw vector values leaked into the summary
    assert 0.1 not in summary.values()
    assert "embedding" not in summary


# ---------------------------------------------------------------------------
# Config + logger factory
# ---------------------------------------------------------------------------

def test_get_observability_config_has_safe_defaults():
    cfg = get_observability_config()
    assert "enabled" in cfg
    assert "log_block_debug" in cfg
    assert "log_llm_payloads_to_server_logs" in cfg
    # Defaults must be safe even if config section is present/absent
    assert isinstance(cfg["log_block_debug"], bool)
    assert isinstance(cfg["log_llm_payloads_to_server_logs"], bool)


def test_get_ingestion_logger_returns_logger():
    logger = get_ingestion_logger("test.observability")
    assert hasattr(logger, "info")


# ---------------------------------------------------------------------------
# LLM call summary logging records metadata but not bodies
# ---------------------------------------------------------------------------

def test_llm_call_logging_records_metadata_not_payloads():
    from kbfabric_ingest.llm.client import LLMClient

    stub = types.SimpleNamespace(logger=FakeLogger(), base_url="http://localhost:11434")
    LLMClient._emit_ingestion_llm_call(
        stub,
        operation="ie_text",
        endpoint="/api/chat",
        model="gpt-oss",
        batch_size=8,
        prompt_chars=1234,
        response_chars=567,
        duration_ms=42.0,
        success=True,
        parse_success=True,
        parsed_items_count=8,
    )
    level, event, kwargs = stub.logger.events[0]
    assert event == "ingestion.llm.text.call"
    assert kwargs["prompt_chars"] == 1234
    assert kwargs["response_chars"] == 567
    assert kwargs["batch_size"] == 8
    assert kwargs["success"] is True
    # Must NOT contain raw prompt/response bodies
    assert "prompt" not in kwargs
    assert "response" not in kwargs
    assert "messages" not in kwargs


def test_llm_vision_call_uses_vision_event_name():
    from kbfabric_ingest.llm.client import LLMClient

    stub = types.SimpleNamespace(logger=FakeLogger(), base_url="http://localhost:11434")
    LLMClient._emit_ingestion_llm_call(
        stub,
        operation="ie_vision",
        endpoint="/api/generate",
        model="qwen-vl",
        batch_size=1,
        prompt_chars=2000,
        response_chars=500,
        duration_ms=120.0,
        success=True,
    )
    assert stub.logger.events[0][1] == "ingestion.llm.vision.call"


# ---------------------------------------------------------------------------
# Embedding batch logging records dim/count but not vector values
# ---------------------------------------------------------------------------

def test_embedding_logging_records_dim_and_count_not_vectors():
    from custom_ml.embeddings.embeddings_client import EmbeddingsClient

    captured = []

    class _StrLogger:
        def info(self, msg):
            captured.append(("info", msg))

        def error(self, msg):
            captured.append(("error", msg))

    stub = types.SimpleNamespace(
        logger=_StrLogger(),
        model_name="snowflake-arctic-embed-l-v2.0",
        _local_model_path="/models/arctic",
        device="cpu",
        batch_size=8,
    )
    EmbeddingsClient._log_embedding_batch(
        stub,
        input_count=98,
        total_input_chars=12345,
        vector_dim=1024,
        duration_ms=88.0,
        success=True,
    )
    assert captured, "embedding batch log should be emitted"
    level, msg = captured[0]
    assert "ingestion.embedding.batch" in msg
    assert "vector_dim=1024" in msg
    assert "input_count=98" in msg
    assert "success=True" in msg
    # No raw vector floats in the message
    assert "0.1" not in msg

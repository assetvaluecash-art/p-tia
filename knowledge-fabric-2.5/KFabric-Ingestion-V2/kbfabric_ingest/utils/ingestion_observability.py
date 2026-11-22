"""Structured, server-visible observability logging for the ingestion pipeline.

This module is a THIN, import-safe layer on top of ``kbfabric_ingest.utils.logger``.
It does NOT replace the existing logging system; it adds stable, structured event
names and key/value fields so each ingestion job produces useful, greppable logs in:

- logs/ingestion_jobs/ingest-{job_id}.log   (Ray worker stdout/stderr tee)
- logs/kbfabric_app.log                      (structlog root logger)

Design constraints:
- No circular imports. ``CanonicalBlock`` / ``EnrichedBlock`` live in
  ``kbfabric_ingest.ingest.models`` but are accessed here via duck-typing only
  (getattr), so this module can be imported from anywhere in the pipeline.
- Server logs must remain SAFE: never log raw document text, prompts, LLM
  responses, PII, secrets, credentials, or full embedding vectors. Only lengths,
  counts, hashes, and redacted descriptors are emitted by the helpers.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from kbfabric_ingest.utils.logger import get_logger

# Default observability behavior when config is missing: enabled but fully
# redacted (no raw block text, no LLM payloads in server logs).
_DEFAULT_OBSERVABILITY = {
    "enabled": True,
    "level": "INFO",
    "log_block_debug": False,
    "log_llm_payloads_to_server_logs": False,
    "max_preview_chars": 120,
}


def get_ingestion_logger(name: str) -> Any:
    """Return a structlog logger bound for ingestion observability.

    Wraps :func:`kbfabric_ingest.utils.logger.get_logger` so events flow through
    stdout (PrintLoggerFactory) into the tee'd job log and through the root
    logger into ``kbfabric_app.log``.
    """
    return get_logger(name)


def get_observability_config() -> dict[str, Any]:
    """Read ``observability.ingestion_logging`` from app config with safe defaults.

    Never raises; if the config loader or section is unavailable, returns the
    safe defaults (enabled, redacted, no block debug, no payloads).
    """
    config = dict(_DEFAULT_OBSERVABILITY)
    try:
        from kbfabric_ingest.ingest.config import get_app_config

        app_config = get_app_config() or {}
        section = (app_config.get("observability") or {}).get("ingestion_logging") or {}
        for key in _DEFAULT_OBSERVABILITY:
            if section.get(key) is not None:
                config[key] = section[key]
    except Exception:
        # Logging configuration must never break ingestion.
        return dict(_DEFAULT_OBSERVABILITY)
    return config


def is_block_debug_enabled() -> bool:
    """True when per-block debug logs are enabled in config."""
    try:
        return bool(get_observability_config().get("log_block_debug", False))
    except Exception:
        return False


def log_llm_payloads_to_server_logs() -> bool:
    """True when full LLM payloads are allowed in server logs (default: False)."""
    try:
        return bool(get_observability_config().get("log_llm_payloads_to_server_logs", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------

def _stage_event(stage: str, suffix: str) -> str:
    """Build the canonical event name for a stage.

    If the caller already passed a fully-qualified event name (e.g.
    ``ingestion.parse.start``), it is used verbatim. Otherwise the short stage
    name (e.g. ``parse``) is expanded to ``ingestion.<stage>.<suffix>``.
    """
    if stage.startswith("ingestion."):
        return stage
    return f"ingestion.{stage}.{suffix}"


def _stage_name(stage: str) -> str:
    """Reduce an event name / stage to a short stage label for the ``stage`` field."""
    name = stage
    if name.startswith("ingestion."):
        name = name[len("ingestion.") :]
    for suffix in (".start", ".end", ".error"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def log_stage_start(logger: Any, stage: str, **fields: Any) -> float:
    """Log a ``ingestion.<stage>.start`` event and return a start timestamp.

    The returned value should be passed to :func:`log_stage_end` as
    ``started_at`` to compute ``duration_ms``.
    """
    event = _stage_event(stage, "start")
    try:
        logger.info(event, stage=_stage_name(stage), **fields)
    except Exception:
        pass
    return time.time()


def log_stage_end(logger: Any, stage: str, started_at: float | None = None, **fields: Any) -> None:
    """Log a ``ingestion.<stage>.end`` event with ``duration_ms`` from ``started_at``."""
    event = _stage_event(stage, "end")
    duration_ms = None
    if started_at is not None:
        try:
            duration_ms = round((time.time() - started_at) * 1000, 2)
        except Exception:
            duration_ms = None
    try:
        logger.info(event, stage=_stage_name(stage), duration_ms=duration_ms, **fields)
    except Exception:
        pass


def log_stage_error(logger: Any, stage: str, error: Any, **fields: Any) -> None:
    """Log a ``ingestion.<stage>.error`` event with redacted error metadata."""
    event = _stage_event(stage, "error")
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
    else:
        error_type = "error"
        error_message = str(error)
    started_at = fields.pop("started_at", None)
    duration_ms = None
    if started_at is not None:
        try:
            duration_ms = round((time.time() - started_at) * 1000, 2)
        except Exception:
            duration_ms = None
    try:
        logger.error(
            event,
            stage=_stage_name(stage),
            error_type=error_type,
            error_message=error_message[:500],
            duration_ms=duration_ms,
            **fields,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

def safe_len(value: Any) -> int:
    """Return ``len(value)`` defensively; 0 for None or unsized values."""
    if value is None:
        return 0
    try:
        return len(value)
    except Exception:
        return 0


def _short_hash(text: str) -> str:
    """Return a short, non-reversible hash of text (for correlation only)."""
    try:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:8]
    except Exception:
        return "00000000"


def redact_for_log(value: Any) -> str:
    """Return a server-safe descriptor of ``value`` that never leaks raw content.

    - Strings -> ``<redacted str len=N sha=XXXXXXXX>`` (length + short hash only)
    - Collections -> ``<redacted list n=N>`` etc.
    - Primitives -> their str() (ints/floats/bools are not sensitive)
    """
    if value is None:
        return "<none>"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f"<redacted str len={len(value)} sha={_short_hash(value)}>"
    if isinstance(value, (bytes, bytearray)):
        return f"<redacted bytes len={safe_len(value)}>"
    if isinstance(value, dict):
        return f"<redacted dict keys={safe_len(value)}>"
    if isinstance(value, (list, tuple, set)):
        return f"<redacted {type(value).__name__} n={safe_len(value)}>"
    return f"<redacted {type(value).__name__}>"


def trace_value_ref(value: Any) -> str:
    """Return a stable, non-reversible reference for trace correlation.

    Entity labels and relationship endpoint values may contain customer data,
    so detailed traces use this digest instead of logging their raw values.
    """
    if value is None:
        return "sha256:none"
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest[:16]}"


def redact_uri(uri: Any) -> str:
    """Strip ``user:password`` credentials from a neo4j/bolt/http URI.

    ``bolt://neo4j:secret@host:7687`` -> ``bolt://host:7687``. Returns a safe
    string even on malformed input; never raises.
    """
    if not uri or not isinstance(uri, str):
        return ""
    try:
        parsed = urlparse(uri)
        if parsed.netloc and "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[1]
            return urlunparse(parsed._replace(netloc=host))
        # ``urlparse`` treats ``user:password@host`` as a URI scheme, so
        # handle userinfo before trusting the parsed scheme.
        if "@" in uri:
            return uri.rsplit("@", 1)[1]
        if parsed.scheme:
            return uri
        return uri
    except Exception:
        # Last-resort: drop anything before an '@' if present.
        if "@" in uri:
            return uri.rsplit("@", 1)[1]
        return "<redacted uri>"


# ---------------------------------------------------------------------------
# Block summaries (duck-typed; no hard import of models to avoid cycles)
# ---------------------------------------------------------------------------

def _bump(counter: dict[str, int], key: Any) -> None:
    label = str(key) if key is not None else "unknown"
    counter[label] = counter.get(label, 0) + 1


def _category_value(category: Any) -> str:
    if category is None:
        return "unknown"
    value = getattr(category, "value", None)
    if value is not None:
        return str(value)
    return str(category)


def summarize_blocks(blocks: Any) -> dict[str, Any]:
    """Aggregate a list of CanonicalBlock-like objects into a safe summary.

    Returns counts and length statistics only; no raw text is included.
    """
    summary: dict[str, Any] = {
        "total": 0,
        "by_block_type": {},
        "by_file_type": {},
        "by_source_file": {},
        "text_len_min": None,
        "text_len_max": None,
        "text_len_avg": None,
        "image_block_count": 0,
        "table_block_count": 0,
        "semantic_unit_count": 0,
    }
    if not blocks:
        return summary

    text_lens: list[int] = []
    semantic_units: set[str] = set()
    for block in blocks:
        if block is None:
            continue
        summary["total"] += 1
        block_type = getattr(block, "block_type", None) or "unknown"
        _bump(summary["by_block_type"], block_type)
        _bump(summary["by_file_type"], getattr(block, "file_type", None))
        _bump(summary["by_source_file"], getattr(block, "source_file", None))

        text_lens.append(safe_len(getattr(block, "text", None)))

        if getattr(block, "image_bytes", None):
            summary["image_block_count"] += 1
        if "table" in str(block_type).lower():
            summary["table_block_count"] += 1

        unit_id = getattr(block, "semantic_unit_id", None)
        if unit_id:
            semantic_units.add(str(unit_id))

    if text_lens:
        summary["text_len_min"] = min(text_lens)
        summary["text_len_max"] = max(text_lens)
        summary["text_len_avg"] = round(sum(text_lens) / len(text_lens), 1)
    summary["semantic_unit_count"] = len(semantic_units)
    return summary


def summarize_enriched_blocks(blocks: Any) -> dict[str, Any]:
    """Aggregate a list of EnrichedBlock-like objects into a safe summary.

    Returns category distributions, entity/relationship counts (total and by
    type), embedding presence/dimension, and PII span counts. No raw text,
    entity values, or vectors are included.
    """
    summary: dict[str, Any] = {
        "total": 0,
        "by_block_category": {},
        "by_kg_node_type": {},
        "entity_count": 0,
        "relationship_count": 0,
        "entity_count_by_type": {},
        "relationship_count_by_type": {},
        "embedding_count": 0,
        "embedding_dim": None,
        "pii_span_count": 0,
        "image_block_count": 0,
    }
    if not blocks:
        return summary

    for enriched in blocks:
        if enriched is None:
            continue
        summary["total"] += 1

        _bump(summary["by_block_category"], _category_value(getattr(enriched, "block_category", None)))
        _bump(summary["by_kg_node_type"], getattr(enriched, "kg_node_type", None))

        entities = getattr(enriched, "candidate_entities", None) or []
        summary["entity_count"] += safe_len(entities)
        for entity in entities:
            if isinstance(entity, dict):
                _bump(summary["entity_count_by_type"], entity.get("type"))

        relationships = getattr(enriched, "candidate_relationships", None) or []
        summary["relationship_count"] += safe_len(relationships)
        for rel in relationships:
            if isinstance(rel, dict):
                _bump(summary["relationship_count_by_type"], rel.get("rel_type"))

        embedding = getattr(enriched, "embedding", None)
        if embedding:
            summary["embedding_count"] += 1
            if summary["embedding_dim"] is None:
                summary["embedding_dim"] = safe_len(embedding)

        summary["pii_span_count"] += safe_len(getattr(enriched, "pii_spans", None))

        inner = getattr(enriched, "block", None)
        if inner is not None and getattr(inner, "image_bytes", None):
            summary["image_block_count"] += 1

    return summary

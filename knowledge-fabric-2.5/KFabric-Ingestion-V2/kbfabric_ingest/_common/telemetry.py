"""OpenTelemetry setup for KnowledgeFabric Ingestion Service.

Provides:
- Distributed tracing with Jaeger export (OTLP)
- Prometheus metrics endpoint
- FastAPI instrumentation
- Metrics following OpenTelemetry Semantic Conventions:
  - Gen AI: https://opentelemetry.io/docs/specs/semconv/gen-ai/
  - Database: https://opentelemetry.io/docs/specs/semconv/database/
  - HTTP: Auto-instrumented via FastAPIInstrumentor

Metric Naming Strategy:
- OTel conventions use dots: gen_ai.client.operation.duration
- Prometheus uses underscores: gen_ai_client_operation_duration
- We emit BOTH for backward compatibility during migration
"""

import os
from typing import Optional

# Ensure .env is loaded before any module-level os.getenv reads below.
from kbfabric_ingest._common import env_config  # noqa: F401

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# =============================================================================
# Service Identification
# =============================================================================
SERVICE_NAME_VALUE = "kbfabric-ingest"
SERVICE_VERSION_VALUE = "1.0.0"

# Environment config — loaded from .env (no in-code defaults)
from kbfabric_ingest._common.env_config import load_dotenv_file, require_env

load_dotenv_file()
OTEL_EXPORTER_OTLP_ENDPOINT = require_env("OTEL_EXPORTER_OTLP_ENDPOINT")
OTEL_ENABLED = require_env("OTEL_ENABLED").lower() == "true"
DEPLOYMENT_ENVIRONMENT = require_env("DEPLOYMENT_ENV")

# =============================================================================
# OTel Semantic Convention Metrics - Gen AI
# Reference: https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/
# =============================================================================

# Duration of generative AI client operations
GEN_AI_CLIENT_OPERATION_DURATION = Histogram(
    "gen_ai_client_operation_duration_seconds",
    "Duration of Generative AI operations (OTel semantic convention)",
    ["gen_ai_system", "gen_ai_request_model", "gen_ai_operation_name", "server_address"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120]
)

# Token usage for generative AI operations
GEN_AI_CLIENT_TOKEN_USAGE = Counter(
    "gen_ai_client_token_usage_total",
    "Number of tokens used in Generative AI operations (OTel semantic convention)",
    ["gen_ai_system", "gen_ai_request_model", "gen_ai_operation_name", "gen_ai_token_type"]
)

# =============================================================================
# OTel Semantic Convention Metrics - Database
# Reference: https://opentelemetry.io/docs/specs/semconv/database/
# =============================================================================

# Duration of database client operations
DB_CLIENT_OPERATION_DURATION = Histogram(
    "db_client_operation_duration_seconds",
    "Duration of database operations (OTel semantic convention)",
    ["db_system", "db_operation", "db_namespace", "server_address"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10]
)

# Rows/documents affected by database operations
DB_CLIENT_ROWS_AFFECTED = Counter(
    "db_client_rows_affected_total",
    "Number of rows/documents affected by database operations",
    ["db_system", "db_operation", "db_namespace"]
)

# =============================================================================
# Ray Job Metrics (Custom, Ray-aligned naming)
# =============================================================================

RAY_JOB_TOTAL = Counter(
    "ray_job_total",
    "Total number of Ray jobs",
    ["ray_job_status", "kbfabric_domain"]
)

RAY_JOB_DURATION = Histogram(
    "ray_job_duration_seconds",
    "Duration of Ray job execution",
    ["kbfabric_domain"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800]
)

RAY_JOB_ACTIVE = Gauge(
    "ray_job_active",
    "Number of currently active Ray jobs"
)

# =============================================================================
# KnowledgeFabric Custom Metrics (No OTel equivalent)
# =============================================================================

# Document processing metrics
KBFABRIC_DOCUMENT_PARSE_DURATION = Histogram(
    "kbfabric_document_parse_duration_seconds",
    "Duration of document parsing",
    ["kbfabric_domain", "file_type"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60]
)

KBFABRIC_DOCUMENT_BLOCKS_TOTAL = Counter(
    "kbfabric_document_blocks_total",
    "Total number of document blocks created",
    ["kbfabric_domain", "block_type"]
)

# Entity extraction metrics
KBFABRIC_ENTITIES_EXTRACTED_TOTAL = Counter(
    "kbfabric_entities_extracted_total",
    "Total number of entities extracted",
    ["kbfabric_domain", "entity_type"]
)

# Entity filtering metrics (IE quality monitoring)
KBFABRIC_IE_ENTITIES_FILTERED_TOTAL = Counter(
    "kbfabric_ie_entities_filtered_total",
    "Total number of entities filtered out during IE validation",
    ["kbfabric_domain", "reason"]  # reason: too_short, too_long, blocked_name, blocked_type, sentence, too_many_words
)

# PII detection metrics (compliance/security)
KBFABRIC_PII_DETECTIONS_TOTAL = Counter(
    "kbfabric_pii_detections_total",
    "Total number of PII entities detected",
    ["kbfabric_domain", "pii_type"]
)

KBFABRIC_PII_MASKING_DURATION = Histogram(
    "kbfabric_pii_masking_duration_seconds",
    "Duration of PII masking operations",
    ["kbfabric_domain"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2]
)

# =============================================================================
# LEGACY Metrics (Backward Compatibility - will be deprecated)
# =============================================================================

# Legacy: Ingestion jobs
INGESTION_JOBS_TOTAL = Counter(
    "kbfabric_ingestion_jobs_total",
    "[LEGACY] Total number of ingestion jobs - use ray_job_total instead",
    ["domain", "status"]
)

INGESTION_JOB_DURATION = Histogram(
    "kbfabric_ingestion_job_duration_seconds",
    "[LEGACY] Duration of ingestion jobs - use ray_job_duration_seconds instead",
    ["domain"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800]
)

ACTIVE_JOBS = Gauge(
    "kbfabric_ingestion_active_jobs",
    "[LEGACY] Active ingestion jobs - use ray_job_active instead"
)

# Legacy: LLM calls
LLM_CALLS_TOTAL = Counter(
    "kbfabric_llm_calls_total",
    "[LEGACY] LLM calls - use gen_ai_client_* metrics instead",
    ["operation", "model", "status"]
)

LLM_CALL_DURATION = Histogram(
    "kbfabric_llm_call_duration_seconds",
    "[LEGACY] LLM call duration - use gen_ai_client_operation_duration_seconds instead",
    ["operation", "model"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60]
)

# Legacy: Weaviate
WEAVIATE_UPSERT_DURATION = Histogram(
    "kbfabric_weaviate_upsert_seconds",
    "[LEGACY] Weaviate upsert - use db_client_operation_duration_seconds instead",
    ["domain", "operation"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10]
)

WEAVIATE_CHUNKS_UPSERTED = Counter(
    "kbfabric_weaviate_chunks_upserted_total",
    "[LEGACY] Weaviate chunks - use db_client_rows_affected_total instead",
    ["domain"]
)

# Legacy: Neo4j
NEO4J_WRITE_DURATION = Histogram(
    "kbfabric_neo4j_write_seconds",
    "[LEGACY] Neo4j write - use db_client_operation_duration_seconds instead",
    ["domain", "operation"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10]
)

NEO4J_NODES_CREATED = Counter(
    "kbfabric_neo4j_nodes_created_total",
    "[LEGACY] Neo4j nodes - use db_client_rows_affected_total instead",
    ["domain", "node_type"]
)

NEO4J_RELATIONSHIPS_CREATED = Counter(
    "kbfabric_neo4j_relationships_created_total",
    "[LEGACY] Neo4j relationships - use db_client_rows_affected_total instead",
    ["domain", "relationship_type"]
)

# Legacy: Document parsing (these are fine, just rename labels)
DOCUMENT_PARSE_DURATION = Histogram(
    "kbfabric_document_parse_seconds",
    "[LEGACY] Document parsing - use kbfabric_document_parse_duration_seconds instead",
    ["domain", "file_type"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60]
)

DOCUMENT_BLOCKS_LEGACY = Counter(
    "kbfabric_doc_blocks_legacy_total",
    "[LEGACY] Document blocks - use kbfabric_document_blocks_total instead",
    ["domain", "block_type"]
)

# Legacy: PII
PII_DETECTIONS_TOTAL = Counter(
    "kbfabric_pii_detections_total_legacy",
    "[LEGACY] PII detections - use kbfabric_pii_detections_total with kbfabric_domain label",
    ["domain", "pii_type"]
)

PII_MASKING_DURATION = Histogram(
    "kbfabric_pii_masking_seconds",
    "[LEGACY] PII masking - use kbfabric_pii_masking_duration_seconds instead",
    ["domain"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2]
)

# Legacy: Embeddings
EMBEDDING_GENERATION_DURATION = Histogram(
    "kbfabric_embedding_generation_seconds",
    "[LEGACY] Embedding generation - use gen_ai_client_operation_duration_seconds instead",
    ["domain", "batch_size"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5]
)

EMBEDDINGS_GENERATED = Counter(
    "kbfabric_embeddings_generated_total",
    "[LEGACY] Embeddings generated",
    ["domain"]
)

# Legacy: Blocks/entities
INGESTION_BLOCKS_PROCESSED = Counter(
    "kbfabric_ingestion_blocks_processed_total",
    "[LEGACY] Blocks processed - use kbfabric_document_blocks_total instead",
    ["domain", "block_type"]
)

INGESTION_ENTITIES_EXTRACTED = Counter(
    "kbfabric_ingestion_entities_extracted_total",
    "[LEGACY] Entities extracted - use kbfabric_entities_extracted_total instead",
    ["domain", "entity_type"]
)


# =============================================================================
# Tracing Setup
# =============================================================================

_tracer: Optional[trace.Tracer] = None


def setup_telemetry() -> None:
    """Initialize OpenTelemetry tracing and metrics."""
    global _tracer
    
    if not OTEL_ENABLED:
        print("[TELEMETRY] OpenTelemetry disabled via OTEL_ENABLED=false")
        return
    
    # Test collector connectivity BEFORE creating exporters
    import socket
    from urllib.parse import urlparse
    
    try:
        parsed = urlparse(OTEL_EXPORTER_OTLP_ENDPOINT)
        host = parsed.hostname or "localhost"
        port = parsed.port or 4317
        
        # Quick connectivity test (1 second timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result != 0:
            print(f"[TELEMETRY] Collector unreachable at {OTEL_EXPORTER_OTLP_ENDPOINT}, skipping telemetry setup")
            return  # Skip telemetry entirely if collector is down
    except Exception as e:
        print(f"[TELEMETRY] Cannot verify collector connectivity: {e}, skipping telemetry setup")
        return  # Skip telemetry if connectivity test fails
    
    # Collector is reachable, proceed with setup
    resource = Resource.create({
        SERVICE_NAME: SERVICE_NAME_VALUE,
        SERVICE_VERSION: SERVICE_VERSION_VALUE,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
    })
    
    tracer_provider = TracerProvider(resource=resource)
    
    try:
        otlp_exporter = OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT)
        tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        print(f"[TELEMETRY] Tracing enabled, exporting to {OTEL_EXPORTER_OTLP_ENDPOINT}")
    except Exception as e:
        print(f"[TELEMETRY] Warning: Could not create OTLP exporter: {e}")
        return  # Don't set up tracer if exporter creation fails
    
    trace.set_tracer_provider(tracer_provider)
    _tracer = trace.get_tracer(SERVICE_NAME_VALUE, SERVICE_VERSION_VALUE)
    
    print(f"[TELEMETRY] OpenTelemetry initialized for {SERVICE_NAME_VALUE}")


def get_tracer() -> trace.Tracer:
    """Get the global tracer instance."""
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer(SERVICE_NAME_VALUE, SERVICE_VERSION_VALUE)
    return _tracer


def instrument_fastapi(app) -> None:
    """Instrument a FastAPI application with OpenTelemetry."""
    # Only instrument if telemetry is enabled AND tracer was successfully set up
    # Check both OTEL_ENABLED flag and that _tracer is not None (meaning setup_telemetry succeeded)
    if not OTEL_ENABLED:
        print("[TELEMETRY] FastAPI instrumentation skipped (OTEL_ENABLED=false)")
        # Un-instrument if already instrumented from a previous run
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        except Exception:
            pass  # Ignore if not instrumented
        return
    
    if _tracer is None:
        print("[TELEMETRY] FastAPI instrumentation skipped (telemetry not available - collector unreachable)")
        # Un-instrument if already instrumented from a previous run
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        except Exception:
            pass  # Ignore if not instrumented
        return
    
    try:
        # Un-instrument first in case it was already instrumented
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        except Exception:
            pass  # Ignore if not instrumented
        # Now instrument with proper telemetry
        FastAPIInstrumentor.instrument_app(app)
        print("[TELEMETRY] FastAPI instrumented")
    except Exception as e:
        print(f"[TELEMETRY] Warning: FastAPI instrumentation failed: {e}")
        # Don't raise - let app continue without instrumentation


def get_metrics_response():
    """Generate Prometheus metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST


# =============================================================================
# Recording Functions - Emit BOTH legacy and OTel metrics
# =============================================================================

def record_ray_job_submitted(domain: str) -> None:
    """Record submission of a Ray job (before it starts running)."""
    RAY_JOB_TOTAL.labels(ray_job_status="submitted", kbfabric_domain=domain).inc()


def record_job_start(domain: str) -> None:
    """Record start of an ingestion job."""
    # OTel
    RAY_JOB_ACTIVE.inc()
    # Legacy
    ACTIVE_JOBS.inc()


def record_job_end(domain: str, status: str, duration_seconds: float) -> None:
    """Record completion of an ingestion job."""
    # OTel
    RAY_JOB_ACTIVE.dec()
    RAY_JOB_TOTAL.labels(ray_job_status=status, kbfabric_domain=domain).inc()
    RAY_JOB_DURATION.labels(kbfabric_domain=domain).observe(duration_seconds)
    # Legacy
    ACTIVE_JOBS.dec()
    INGESTION_JOBS_TOTAL.labels(domain=domain, status=status).inc()
    INGESTION_JOB_DURATION.labels(domain=domain).observe(duration_seconds)


def record_blocks_processed(domain: str, block_type: str, count: int = 1) -> None:
    """Record blocks processed."""
    # OTel
    KBFABRIC_DOCUMENT_BLOCKS_TOTAL.labels(kbfabric_domain=domain, block_type=block_type).inc(count)
    # Legacy
    INGESTION_BLOCKS_PROCESSED.labels(domain=domain, block_type=block_type).inc(count)


def record_entities_extracted(domain: str, entity_type: str, count: int = 1) -> None:
    """Record entities extracted."""
    # OTel
    KBFABRIC_ENTITIES_EXTRACTED_TOTAL.labels(kbfabric_domain=domain, entity_type=entity_type).inc(count)
    # Legacy
    INGESTION_ENTITIES_EXTRACTED.labels(domain=domain, entity_type=entity_type).inc(count)


def record_llm_call(
    operation: str, 
    model: str, 
    status: str, 
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    server_address: str = "localhost:11434"
) -> None:
    """Record an LLM call with OTel Gen AI semantic conventions."""
    # OTel Gen AI metrics
    GEN_AI_CLIENT_OPERATION_DURATION.labels(
        gen_ai_system="ollama",
        gen_ai_request_model=model,
        gen_ai_operation_name=operation,
        server_address=server_address
    ).observe(duration_seconds)
    
    if input_tokens > 0:
        GEN_AI_CLIENT_TOKEN_USAGE.labels(
            gen_ai_system="ollama",
            gen_ai_request_model=model,
            gen_ai_operation_name=operation,
            gen_ai_token_type="input"
        ).inc(input_tokens)
    
    if output_tokens > 0:
        GEN_AI_CLIENT_TOKEN_USAGE.labels(
            gen_ai_system="ollama",
            gen_ai_request_model=model,
            gen_ai_operation_name=operation,
            gen_ai_token_type="output"
        ).inc(output_tokens)
    
    # Legacy
    LLM_CALLS_TOTAL.labels(operation=operation, model=model, status=status).inc()
    LLM_CALL_DURATION.labels(operation=operation, model=model).observe(duration_seconds)


def record_weaviate_upsert(
    domain: str, 
    operation: str, 
    duration_seconds: float, 
    chunk_count: int = 1,
    server_address: str = "localhost:8080"
) -> None:
    """Record a Weaviate upsert operation with OTel Database semantic conventions."""
    # OTel Database metrics
    DB_CLIENT_OPERATION_DURATION.labels(
        db_system="weaviate",
        db_operation=operation,
        db_namespace=domain,
        server_address=server_address
    ).observe(duration_seconds)
    
    DB_CLIENT_ROWS_AFFECTED.labels(
        db_system="weaviate",
        db_operation=operation,
        db_namespace=domain
    ).inc(chunk_count)
    
    # Legacy
    WEAVIATE_UPSERT_DURATION.labels(domain=domain, operation=operation).observe(duration_seconds)
    WEAVIATE_CHUNKS_UPSERTED.labels(domain=domain).inc(chunk_count)


def record_neo4j_write(
    domain: str, 
    operation: str, 
    duration_seconds: float,
    server_address: str = "localhost:7687"
) -> None:
    """Record a Neo4j write operation with OTel Database semantic conventions."""
    # OTel Database metrics
    DB_CLIENT_OPERATION_DURATION.labels(
        db_system="neo4j",
        db_operation=operation,
        db_namespace=domain,
        server_address=server_address
    ).observe(duration_seconds)
    
    # Legacy
    NEO4J_WRITE_DURATION.labels(domain=domain, operation=operation).observe(duration_seconds)


def record_neo4j_node(
    domain: str, 
    node_type: str, 
    count: int = 1
) -> None:
    """Record Neo4j nodes created with OTel Database semantic conventions."""
    # OTel
    DB_CLIENT_ROWS_AFFECTED.labels(
        db_system="neo4j",
        db_operation="create_node",
        db_namespace=domain
    ).inc(count)
    
    # Legacy
    NEO4J_NODES_CREATED.labels(domain=domain, node_type=node_type).inc(count)


def record_neo4j_relationship(
    domain: str, 
    relationship_type: str, 
    count: int = 1
) -> None:
    """Record Neo4j relationships created with OTel Database semantic conventions."""
    # OTel
    DB_CLIENT_ROWS_AFFECTED.labels(
        db_system="neo4j",
        db_operation="create_relationship",
        db_namespace=domain
    ).inc(count)
    
    # Legacy
    NEO4J_RELATIONSHIPS_CREATED.labels(domain=domain, relationship_type=relationship_type).inc(count)


def record_pii_detection(domain: str, pii_type: str, count: int = 1) -> None:
    """Record PII detections."""
    # OTel (custom, no standard equivalent)
    KBFABRIC_PII_DETECTIONS_TOTAL.labels(kbfabric_domain=domain, pii_type=pii_type).inc(count)
    # Legacy
    PII_DETECTIONS_TOTAL.labels(domain=domain, pii_type=pii_type).inc(count)


def record_pii_masking(domain: str, duration_seconds: float) -> None:
    """Record PII masking duration."""
    # OTel (custom)
    KBFABRIC_PII_MASKING_DURATION.labels(kbfabric_domain=domain).observe(duration_seconds)
    # Legacy
    PII_MASKING_DURATION.labels(domain=domain).observe(duration_seconds)


def record_ie_entity_filtered(domain: str, reason: str, count: int = 1) -> None:
    """
    Record entities filtered out during IE validation.
    
    This metric helps monitor IE quality - high filter rates may indicate
    prompt issues or source data quality problems.
    
    Args:
        domain: Domain being processed
        reason: Why the entity was filtered (too_short, too_long, blocked_name, 
                blocked_type, sentence, too_many_words, multi_word)
        count: Number of entities filtered
    """
    KBFABRIC_IE_ENTITIES_FILTERED_TOTAL.labels(kbfabric_domain=domain, reason=reason).inc(count)


def record_document_parse(
    domain: str, 
    file_type: str, 
    duration_seconds: float, 
    block_count: int = 0
) -> None:
    """Record document parsing metrics."""
    # OTel (custom)
    KBFABRIC_DOCUMENT_PARSE_DURATION.labels(kbfabric_domain=domain, file_type=file_type).observe(duration_seconds)
    if block_count > 0:
        KBFABRIC_DOCUMENT_BLOCKS_TOTAL.labels(kbfabric_domain=domain, block_type="parsed").inc(block_count)
    
    # Legacy
    DOCUMENT_PARSE_DURATION.labels(domain=domain, file_type=file_type).observe(duration_seconds)
    if block_count > 0:
        DOCUMENT_BLOCKS_LEGACY.labels(domain=domain, block_type="total").inc(block_count)


def record_embedding_generation(
    domain: str, 
    batch_size: int, 
    duration_seconds: float, 
    count: int = 1,
    model: str = "all-mpnet-base-v2"
) -> None:
    """Record embedding generation metrics with OTel Gen AI semantic conventions."""
    # OTel Gen AI
    GEN_AI_CLIENT_OPERATION_DURATION.labels(
        gen_ai_system="sentence_transformers",
        gen_ai_request_model=model,
        gen_ai_operation_name="embed",
        server_address="local"
    ).observe(duration_seconds)
    
    # Legacy
    batch_label = "1" if batch_size == 1 else "small" if batch_size <= 10 else "large"
    EMBEDDING_GENERATION_DURATION.labels(domain=domain, batch_size=batch_label).observe(duration_seconds)
    EMBEDDINGS_GENERATED.labels(domain=domain).inc(count)

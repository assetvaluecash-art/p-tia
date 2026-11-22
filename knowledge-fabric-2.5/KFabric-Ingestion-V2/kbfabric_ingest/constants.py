"""Centralized constants for KnowledgeFabric.

All magic numbers and configuration defaults should be defined here with
clear documentation. This provides a single source of truth and makes
the codebase more maintainable.

Usage:
    from kbfabric_ingest.constants import LoggingConstants, DatabaseConstants
    
    query_preview = query[:LoggingConstants.QUERY_PREVIEW_LENGTH]
    pool_size = DatabaseConstants.NEO4J_POOL_SIZE
"""


class LoggingConstants:
    """Constants for logging and display formatting.
    
    These values control how data is truncated in logs and UI displays
    to maintain readability while preserving enough context.
    """
    
    # Query/text preview lengths for log messages
    QUERY_PREVIEW_LENGTH = 100        # Chars to show in query log previews
    CONTENT_PREVIEW_LENGTH = 500      # Chars for content previews in logs
    RESPONSE_PREVIEW_LENGTH = 1000    # Chars for LLM response preview
    
    # ID display lengths
    BLOCK_ID_DISPLAY_LENGTH = 12      # Chars for block ID in UI/logs
    ENTITY_ID_DISPLAY_LENGTH = 8      # Chars for entity ID in UI/logs
    
    # Error message truncation
    ERROR_DETAIL_LENGTH = 200         # Max chars for error message display
    
    # Log separator width
    LOG_SEPARATOR_WIDTH = 80          # Width of log separator lines


class RetryConstants:
    """Constants for retry behavior.
    
    These control the exponential backoff retry logic used throughout
    the application for handling transient failures.
    """
    
    DEFAULT_MAX_RETRIES = 3           # Maximum retry attempts
    DEFAULT_BASE_DELAY_SEC = 1.0      # Initial delay between retries
    DEFAULT_MAX_DELAY_SEC = 10.0      # Maximum delay (cap for exponential growth)
    
    # Specific operation retries
    DB_CONNECTION_MAX_RETRIES = 3     # Retries for database connections
    LLM_API_MAX_RETRIES = 3           # Retries for LLM API calls
    HTTP_REQUEST_MAX_RETRIES = 3      # Retries for HTTP requests


class DatabaseConstants:
    """Constants for database operations.
    
    Connection pooling and timeout settings for Neo4j and Weaviate.
    Prefer values from .env (via get_app_config); these constants are
    only for non-config call sites that do not load app config.
    """
    
    # Neo4j connection pool settings
    NEO4J_POOL_SIZE = 50                      # Max connections in pool
    NEO4J_ACQUISITION_TIMEOUT_SEC = 60        # Timeout waiting for connection
    NEO4J_CONNECTION_TIMEOUT_SEC = 30         # Timeout establishing connection
    NEO4J_MAX_LIFETIME_SEC = 3600             # Max connection lifetime (1 hour)
    
    # Weaviate settings
    WEAVIATE_TIMEOUT_SEC = 30                 # Request timeout
    WEAVIATE_QUERY_TIMEOUT_SEC = 10           # Query timeout
    WEAVIATE_INSERT_TIMEOUT_SEC = 30          # Insert timeout


class IngestionConstants:
    """Constants for ingestion pipeline.
    
    Chunking, batching, and processing limits for document ingestion.
    """
    
    # Chunking settings
    DEFAULT_MAX_CHARS_PER_BLOCK = 1500        # Max chars before splitting block
    CONTENT_HASH_LENGTH = 12                  # Chars for content hash in block IDs
    
    # Batching settings
    DEFAULT_LLM_BATCH_SIZE = 8                # Blocks per LLM call
    EMBEDDING_BATCH_SIZE = 16                 # Texts per embedding batch
    
    # Processing limits
    MAX_IMAGE_SIZE_MB = 10                    # Max image size for vision processing
    MAX_ENTITY_NAME_LENGTH = 50               # Max chars for entity names
    MAX_ENTITY_WORDS = 5                      # Max words in entity name


class RetrievalConstants:
    """Constants for retrieval pipeline.
    
    Search, reranking, and answer generation settings.
    """
    
    # Search limits
    DEFAULT_TOP_K_CHUNKS = 15                 # Initial semantic retrieval count
    DEFAULT_TOP_K_PATHS = 15                  # Initial graph retrieval count
    MAX_CHUNKS_FOR_LLM = 8                    # Chunks after reranking
    MAX_PATHS_FOR_LLM = 8                     # Paths after reranking
    
    # Quality thresholds
    RERANK_MIN_SCORE = 0.3                    # Minimum cross-encoder score
    MIN_KEYWORD_OVERLAP = 0.2                 # Minimum query keyword overlap ratio


class SecurityConstants:
    """Constants for security-related operations.
    
    File validation, PII handling, and other security settings.
    """
    
    # File path validation
    MAX_PATH_DEPTH = 10                       # Maximum directory depth allowed
    
    # Supported file extensions for ingestion
    SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".txt", ".md"}


class HealthCheckConstants:
    """Constants for health check endpoints.
    
    Timeout and threshold settings for Kubernetes probes.
    """
    
    # Timeout for dependency checks (seconds)
    DEPENDENCY_CHECK_TIMEOUT_SEC = 5
    
    # Individual service timeouts
    WEAVIATE_HEALTH_TIMEOUT_SEC = 5
    NEO4J_HEALTH_TIMEOUT_SEC = 5
    OLLAMA_HEALTH_TIMEOUT_SEC = 5


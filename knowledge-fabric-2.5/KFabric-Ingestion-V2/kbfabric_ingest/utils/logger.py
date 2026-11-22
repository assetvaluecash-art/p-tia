"""Central logging utility with structlog for KBFabricKB.

Ensures:
- UTF-8 encoding on all file handlers
- Real-time log flushing (no buffering delays)
- Both structured and human-readable formats
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from typing import Any
import structlog
from structlog.types import Processor

# Global flag to ensure logging is configured only once
_logging_configured = False
_llm_logger = None


class FlushingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that flushes and syncs after every log entry.
    
    This ensures logs are written to disk immediately, enabling real-time
    log monitoring during long-running operations.
    """
    
    def __init__(self, *args, **kwargs):
        # Force UTF-8 encoding
        kwargs.setdefault('encoding', 'utf-8')
        super().__init__(*args, **kwargs)
    
    def emit(self, record):
        """Emit a record, then flush and sync to disk."""
        super().emit(record)
        self.flush()
        # Force OS to write to disk (critical for real-time monitoring)
        try:
            if self.stream and hasattr(self.stream, 'fileno'):
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            # Handle cases where stream is closed or doesn't support fsync
            pass


class HumanReadableLLMFormatter(logging.Formatter):
    """Custom formatter for human-readable LLM call logs (truncated for readability)."""
    
    def __init__(self, truncate: bool = True, truncate_length: int = 500):
        """Initialize formatter.
        
        Args:
            truncate: If True, truncate long prompts/responses (default: True)
            truncate_length: Length to truncate to if truncate=True (default: 500)
        """
        super().__init__()
        self.truncate = truncate
        self.truncate_length = truncate_length
    
    def format(self, record: logging.LogRecord) -> str:
        """Format LLM log record as human-readable text."""
        # If message is already a string (from JSON), try to parse it
        if isinstance(record.msg, str):
            try:
                data = json.loads(record.msg)
            except (json.JSONDecodeError, TypeError):
                # Not JSON, return as-is
                return record.msg
        elif isinstance(record.msg, dict):
            data = record.msg
        else:
            return str(record.msg)
        
        event = data.get("event", "LLM call")
        timestamp = data.get("timestamp") or data.get("request_timestamp") or datetime.now().isoformat()
        model = data.get("model_name", "unknown")
        operation = data.get("operation", "unknown")
        duration_ms = data.get("duration_ms")
        
        # Build header
        lines = []
        lines.append("=" * 80)
        lines.append(f"LLM Call: {event}")
        lines.append(f"Time: {timestamp}")
        lines.append(f"Model: {model} | Operation: {operation}")
        
        if duration_ms is not None:
            duration_sec = duration_ms / 1000.0
            lines.append(f"Duration: {duration_ms:.2f}ms ({duration_sec:.2f}s)")
        
        # Add block count if available
        block_count = data.get("block_count")
        if block_count is not None:
            lines.append(f"Blocks: {block_count}")
        
        # Add image count if available (for vision calls)
        image_count = data.get("image_count")
        if image_count is not None:
            lines.append(f"Images: {image_count}")
        
        # Add prompt info
        prompt = data.get("prompt")
        prompt_length = data.get("prompt_length")
        if prompt:
            lines.append("-" * 80)
            lines.append("PROMPT:")
            if prompt_length:
                lines.append(f"[Length: {prompt_length} chars]")
            # Truncate prompt for readability if enabled
            if self.truncate and len(prompt) > self.truncate_length:
                prompt_preview = prompt[:self.truncate_length] + f"... [truncated, full length: {len(prompt)} chars]"
                lines.append(prompt_preview)
            else:
                lines.append(prompt)
        
        # Add response info
        response = data.get("response")
        response_length = data.get("response_length")
        if response:
            lines.append("-" * 80)
            lines.append("RESPONSE:")
            if response_length:
                lines.append(f"[Length: {response_length} chars]")
            # Truncate response for readability if enabled
            if self.truncate and len(response) > self.truncate_length:
                response_preview = response[:self.truncate_length] + f"... [truncated, full length: {len(response)} chars]"
                lines.append(response_preview)
            else:
                lines.append(response)
        
        # Add error if present
        error = data.get("error")
        if error:
            lines.append("-" * 80)
            lines.append(f"ERROR: {error}")
        
        # Add status code if present
        status_code = data.get("status_code")
        if status_code:
            lines.append(f"Status Code: {status_code}")
        
        # Add parsed result count if available
        parsed_count = data.get("parsed_result_count")
        expected_count = data.get("expected_count")
        if parsed_count is not None and expected_count is not None:
            lines.append(f"Parsed: {parsed_count}/{expected_count} results")
        
        # Add request/response timestamps if available
        request_timestamp = data.get("request_timestamp")
        response_timestamp = data.get("response_timestamp")
        if request_timestamp and response_timestamp:
            lines.append(f"Request Time: {request_timestamp}")
            lines.append(f"Response Time: {response_timestamp}")
        
        lines.append("=" * 80)
        lines.append("")
        
        return "\n".join(lines)


class FullLLMFormatter(logging.Formatter):
    """Custom formatter for full LLM call logs (no truncation, complete I/O)."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format LLM log record with full prompt/response (no truncation)."""
        # If message is already a string (from JSON), try to parse it
        if isinstance(record.msg, str):
            try:
                data = json.loads(record.msg)
            except (json.JSONDecodeError, TypeError):
                # Not JSON, return as-is
                return record.msg
        elif isinstance(record.msg, dict):
            data = record.msg
        else:
            return str(record.msg)
        
        event = data.get("event", "LLM call")
        timestamp = data.get("timestamp") or data.get("request_timestamp") or datetime.now().isoformat()
        model = data.get("model_name", "unknown")
        operation = data.get("operation", "unknown")
        duration_ms = data.get("duration_ms")
        call_id = data.get("call_id", "unknown")
        
        # Build header with all metadata
        lines = []
        lines.append("=" * 100)
        lines.append(f"LLM Call ID: {call_id}")
        lines.append(f"Event: {event}")
        lines.append(f"Request Timestamp: {data.get('request_timestamp', timestamp)}")
        lines.append(f"Response Timestamp: {data.get('response_timestamp', 'N/A')}")
        lines.append(f"Model: {model} | Operation: {operation}")
        
        if duration_ms is not None:
            duration_sec = duration_ms / 1000.0
            lines.append(f"Duration: {duration_ms:.2f}ms ({duration_sec:.2f}s)")
        
        # Add all metadata
        block_count = data.get("block_count")
        if block_count is not None:
            lines.append(f"Blocks: {block_count}")
        
        image_count = data.get("image_count")
        if image_count is not None:
            lines.append(f"Images: {image_count}")
            # Log image sizes (not base64 data)
            image_sizes_mb = data.get("image_sizes_mb", [])
            if image_sizes_mb:
                lines.append(f"Image Sizes (MB): {image_sizes_mb}")
        
        status_code = data.get("status_code")
        if status_code:
            lines.append(f"Status Code: {status_code}")
        
        # Full prompt (no truncation)
        prompt = data.get("prompt")
        prompt_length = data.get("prompt_length")
        if prompt:
            lines.append("-" * 100)
            lines.append("FULL PROMPT:")
            lines.append(f"Length: {prompt_length or len(prompt)} chars")
            lines.append("-" * 100)
            lines.append(prompt)
            lines.append("-" * 100)
        
        # Full response (no truncation)
        response = data.get("response")
        response_length = data.get("response_length")
        if response:
            lines.append("FULL RESPONSE:")
            lines.append(f"Length: {response_length or len(response)} chars")
            lines.append("-" * 100)
            lines.append(response)
            lines.append("-" * 100)
        
        # Error if present
        error = data.get("error")
        if error:
            lines.append("ERROR:")
            lines.append(error)
            lines.append("-" * 100)
        
        # Parsed results info
        parsed_count = data.get("parsed_result_count")
        expected_count = data.get("expected_count")
        if parsed_count is not None and expected_count is not None:
            lines.append(f"Parsed Results: {parsed_count}/{expected_count}")
        
        lines.append("=" * 100)
        lines.append("")
        
        return "\n".join(lines)


def configure_logging() -> None:
    """Configure structlog with JSON formatter and file handlers."""
    global _logging_configured
    
    if _logging_configured:
        return
    
    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    # Configure standard logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            logging.StreamHandler(),
        ],
    )
    
    # File handler for general application logs (root logger only)
    # Use FlushingRotatingFileHandler for real-time log output
    app_log_handler = FlushingRotatingFileHandler(
        logs_dir / "kbfabric_app.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    app_log_handler.setLevel(logging.INFO)
    
    # Configure structlog processors
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Add file handler to root logger (only for app logs)
    root_logger = logging.getLogger()
    root_logger.addHandler(app_log_handler)
    
    # Filter HTTP client library noise - these flood logs with request/response details
    # that obscure application-specific events
    http_noise_loggers = [
        "httpx",
        "httpcore",
        "urllib3",
        "urllib3.connectionpool",
        "urllib3.util.retry",
        "requests",
        "requests.packages.urllib3",
        "aiohttp",
        "aiohttp.client",
        "openai",
        "openai._base_client",
        "anthropic",
        "anthropic._base_client",
        "weaviate",
        "weaviate.connect",
        "neo4j",
        "neo4j.pool",
    ]
    for logger_name in http_noise_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    
    _logging_configured = True


def get_logger(name: str) -> Any:
    """
    Get a structlog logger instance.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        structlog logger instance
    """
    configure_logging()
    return structlog.get_logger(name).bind(service_name="KnowledgeFabric")


_llm_text_logger = None
_llm_visual_logger = None
_llm_full_logger = None
_llm_text_run_loggers: dict[str, logging.Logger] = {}
_llm_vision_run_loggers: dict[str, logging.Logger] = {}


def _get_logs_dir() -> Path:
    """
    Resolve the logs directory.

    Default: ./logs
    Override (useful for tests): KBFABRIC_LOG_DIR=/path/to/logs
    """
    env_dir = (os.environ.get("KBFABRIC_LOG_DIR") or "").strip()
    if env_dir:
        return Path(env_dir)
    return Path("logs")


def get_llm_text_logger(name: str) -> logging.Logger:
    """
    Get a dedicated LLM logger for text operations with separate log files.
    
    This logger writes:
    - Human-readable formatted logs to llm_calls_text.log
    - JSONL logs to llm_calls_text.jsonl (for machine parsing)
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Python logging.Logger instance (not structlog)
    """
    global _llm_text_logger
    if _llm_text_logger is not None:
        return _llm_text_logger
    
    configure_logging()
    
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_text")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        # Human-readable handler with real-time flushing
        human_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_text.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        human_handler.setFormatter(HumanReadableLLMFormatter())
        logger.addHandler(human_handler)
        
        # JSONL handler for machine parsing with real-time flushing
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_text.jsonl",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_text_logger = logger
    return _llm_text_logger


def get_llm_visual_logger(name: str) -> logging.Logger:
    """
    Get a dedicated LLM logger for visual operations with separate log files.
    
    This logger writes:
    - Human-readable formatted logs to llm_calls_visual.log
    - JSONL logs to llm_calls_visual.jsonl (for machine parsing)
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Python logging.Logger instance (not structlog)
    """
    global _llm_visual_logger
    if _llm_visual_logger is not None:
        return _llm_visual_logger
    
    configure_logging()
    
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_visual")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        # Human-readable handler with real-time flushing
        human_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_visual.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        human_handler.setFormatter(HumanReadableLLMFormatter())
        logger.addHandler(human_handler)
        
        # JSONL handler for machine parsing with real-time flushing
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_visual.jsonl",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_visual_logger = logger
    return _llm_visual_logger


def get_llm_full_logger(name: str) -> logging.Logger:
    """
    Get a dedicated LLM logger for full I/O logging (no truncation).
    
    This logger writes complete prompts and responses to llm_calls_full.log
    with full timestamps and metadata. Use this for debugging and analysis.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Python logging.Logger instance (not structlog)
    """
    global _llm_full_logger
    if _llm_full_logger is not None:
        return _llm_full_logger
    
    configure_logging()
    
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_full")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        # Full I/O handler (no truncation) with real-time flushing
        full_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_full.log",
            maxBytes=50 * 1024 * 1024,  # 50MB (larger due to full content)
            backupCount=3,
        )
        full_handler.setFormatter(FullLLMFormatter())
        logger.addHandler(full_handler)
        
        # JSONL handler for machine parsing (also full content) with real-time flushing
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_full.jsonl",
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=3,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_full_logger = logger
    return _llm_full_logger


def get_llm_text_run_logger(run_id: str, name: str = __name__) -> logging.Logger:
    """
    Dedicated per-run JSONL logger for TEXT LLM raw I/O.

    Writes: logs/llm_text_<run_id>.jsonl
    """
    if not run_id:
        raise ValueError("run_id is required")

    cached = _llm_text_run_loggers.get(run_id)
    if cached is not None:
        return cached

    configure_logging()

    logs_dir = _get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"llm_text_run.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / f"llm_text_{run_id}.jsonl",
            maxBytes=200 * 1024 * 1024,  # 200MB (raw I/O can be large)
            backupCount=2,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)

    _llm_text_run_loggers[run_id] = logger
    return logger


def get_llm_vision_run_logger(run_id: str, name: str = __name__) -> logging.Logger:
    """
    Dedicated per-run JSONL logger for VISION LLM raw I/O.

    Writes: logs/llm_vision_<run_id>.jsonl
    """
    if not run_id:
        raise ValueError("run_id is required")

    cached = _llm_vision_run_loggers.get(run_id)
    if cached is not None:
        return cached

    configure_logging()

    logs_dir = _get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"llm_vision_run.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / f"llm_vision_{run_id}.jsonl",
            maxBytes=500 * 1024 * 1024,  # 500MB (vision payloads can be huge)
            backupCount=2,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)

    _llm_vision_run_loggers[run_id] = logger
    return logger


def get_llm_logger(name: str) -> logging.Logger:
    """
    Get a dedicated LLM logger with both human-readable and JSONL handlers.
    
    DEPRECATED: Use get_llm_text_logger() or get_llm_visual_logger() instead.
    This is kept for backward compatibility.
    
    This logger writes:
    - Human-readable formatted logs to llm_calls.log
    - JSONL logs to llm_calls.jsonl (for machine parsing)
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Python logging.Logger instance (not structlog)
    """
    global _llm_logger
    if _llm_logger is not None:
        return _llm_logger
    
    configure_logging()
    
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        # Human-readable handler with real-time flushing
        human_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        human_handler.setFormatter(HumanReadableLLMFormatter())
        logger.addHandler(human_handler)
        
        # JSONL handler for machine parsing with real-time flushing
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls.jsonl",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_logger = logger
    return _llm_logger

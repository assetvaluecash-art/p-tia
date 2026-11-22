"""Central logging utility with structlog for KnowledgeFabric Ingestion.

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

# Import BASE_DIR for package-relative log paths
from kbfabric_ingest._common.config import BASE_DIR

# Global flag to ensure logging is configured only once
_logging_configured = False


class FlushingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that flushes and syncs after every log entry."""
    
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('encoding', 'utf-8')
        super().__init__(*args, **kwargs)
    
    def emit(self, record):
        """Emit a record, then flush and sync to disk."""
        super().emit(record)
        self.flush()
        try:
            if self.stream and hasattr(self.stream, 'fileno'):
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            pass


class HumanReadableLLMFormatter(logging.Formatter):
    """Custom formatter for human-readable LLM call logs."""
    
    def __init__(self, truncate: bool = True, truncate_length: int = 500):
        super().__init__()
        self.truncate = truncate
        self.truncate_length = truncate_length
    
    def format(self, record: logging.LogRecord) -> str:
        """Format LLM log record as human-readable text."""
        if isinstance(record.msg, str):
            try:
                data = json.loads(record.msg)
            except (json.JSONDecodeError, TypeError):
                return record.msg
        elif isinstance(record.msg, dict):
            data = record.msg
        else:
            return str(record.msg)
        
        event = data.get("event", "LLM call")
        timestamp = data.get("timestamp") or datetime.now().isoformat()
        model = data.get("model_name", "unknown")
        operation = data.get("operation", "unknown")
        duration_ms = data.get("duration_ms")
        
        lines = []
        lines.append("=" * 80)
        lines.append(f"LLM Call: {event}")
        lines.append(f"Time: {timestamp}")
        lines.append(f"Model: {model} | Operation: {operation}")
        
        if duration_ms is not None:
            lines.append(f"Duration: {duration_ms:.2f}ms")
        
        prompt = data.get("prompt")
        if prompt:
            lines.append("-" * 80)
            lines.append("PROMPT:")
            if self.truncate and len(prompt) > self.truncate_length:
                lines.append(prompt[:self.truncate_length] + "... [truncated]")
            else:
                lines.append(prompt)
        
        response = data.get("response")
        if response:
            lines.append("-" * 80)
            lines.append("RESPONSE:")
            if self.truncate and len(response) > self.truncate_length:
                lines.append(response[:self.truncate_length] + "... [truncated]")
            else:
                lines.append(response)
        
        error = data.get("error")
        if error:
            lines.append("-" * 80)
            lines.append(f"ERROR: {error}")
        
        lines.append("=" * 80)
        lines.append("")
        
        return "\n".join(lines)


def configure_logging() -> None:
    """Configure structlog with JSON formatter and file handlers."""
    global _logging_configured
    
    if _logging_configured:
        return
    
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler()],
    )
    
    app_log_handler = FlushingRotatingFileHandler(
        logs_dir / "kbfabric_ingest.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    app_log_handler.setLevel(logging.INFO)
    
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
    
    root_logger = logging.getLogger()
    root_logger.addHandler(app_log_handler)
    
    # Filter HTTP noise
    for logger_name in ["httpx", "httpcore", "urllib3", "requests", "weaviate", "neo4j"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    
    _logging_configured = True


def get_logger(name: str) -> Any:
    """Get a structlog logger instance."""
    configure_logging()
    return structlog.get_logger(name).bind(service_name="KnowledgeFabric-Ingest")


_llm_text_logger = None
_llm_visual_logger = None
_llm_full_logger = None


def get_llm_text_logger(name: str) -> logging.Logger:
    """Get LLM logger for text operations."""
    global _llm_text_logger
    if _llm_text_logger is not None:
        return _llm_text_logger
    
    configure_logging()
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_text")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        human_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_text.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        human_handler.setFormatter(HumanReadableLLMFormatter())
        logger.addHandler(human_handler)
        
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_text.jsonl",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_text_logger = logger
    return _llm_text_logger


def get_llm_visual_logger(name: str) -> logging.Logger:
    """Get LLM logger for visual operations."""
    global _llm_visual_logger
    if _llm_visual_logger is not None:
        return _llm_visual_logger
    
    configure_logging()
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_visual")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        human_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_visual.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        human_handler.setFormatter(HumanReadableLLMFormatter())
        logger.addHandler(human_handler)
        
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_visual.jsonl",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_visual_logger = logger
    return _llm_visual_logger


def get_llm_full_logger(name: str) -> logging.Logger:
    """Get LLM logger for full I/O logging (no truncation)."""
    global _llm_full_logger
    if _llm_full_logger is not None:
        return _llm_full_logger
    
    configure_logging()
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    logger = logging.getLogger("llm_calls_full")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    if not logger.handlers:
        jsonl_handler = FlushingRotatingFileHandler(
            logs_dir / "llm_calls_full.jsonl",
            maxBytes=50 * 1024 * 1024,
            backupCount=3,
        )
        jsonl_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(jsonl_handler)
    
    _llm_full_logger = logger
    return _llm_full_logger


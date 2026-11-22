"""Conservative classification and limits for automatic ingestion retries."""

from __future__ import annotations

import os
import re
from typing import Any


def classify_failure(error: Any) -> dict[str, Any]:
    """Classify only clearly transient dependency failures as retryable."""
    message = str(error or "")
    normalized = message.lower()
    if any(token in normalized for token in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return {"code": "LLM_TIMEOUT", "retryable": True, "message": message[:1000]}
    if re.search(r"(?:http\s*)?(?:429|502|503|504)\b", normalized) or any(
        token in normalized for token in ("rate limit exceeded", "service unavailable")
    ):
        return {"code": "LLM_SERVICE_UNAVAILABLE", "retryable": True, "message": message[:1000]}
    if any(token in normalized for token in (
        "connection reset", "connection refused", "temporarily unavailable",
        "network is unreachable", "server disconnected",
    )):
        return {"code": "DEPENDENCY_FAILURE", "retryable": True, "message": message[:1000]}
    return {"code": "INGESTION_FAILED", "retryable": False, "message": message[:1000]}


def automatic_retry_enabled() -> bool:
    return os.getenv("INGESTION_AUTO_RETRY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def automatic_retry_max_attempts() -> int:
    """Maximum automatic retries after the initial worker attempt."""
    return max(0, min(5, int(os.getenv("INGESTION_AUTO_RETRY_MAX_ATTEMPTS", "2"))))


def automatic_retry_poll_seconds() -> int:
    return max(2, min(60, int(os.getenv("INGESTION_AUTO_RETRY_POLL_SECONDS", "5"))))

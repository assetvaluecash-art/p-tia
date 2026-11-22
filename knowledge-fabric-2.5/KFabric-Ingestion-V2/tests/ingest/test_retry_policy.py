"""English acceptance checks for conservative bounded automatic retry policy."""

from kbfabric_ingest.ingest.retry_policy import (
    automatic_retry_enabled,
    automatic_retry_max_attempts,
    classify_failure,
)


def test_only_transient_dependency_failures_are_retryable():
    """Timeouts, throttling, and temporary network failures may retry; data errors may not."""
    assert classify_failure("Read timed out") == {
        "code": "LLM_TIMEOUT", "retryable": True, "message": "Read timed out"
    }
    assert classify_failure("HTTP 503 service unavailable")["retryable"] is True
    assert classify_failure("Connection reset by peer")["code"] == "DEPENDENCY_FAILURE"
    assert classify_failure("Invalid ontology relationship")["retryable"] is False


def test_auto_retry_is_opt_in_and_attempt_limit_is_bounded(monkeypatch):
    monkeypatch.delenv("INGESTION_AUTO_RETRY_ENABLED", raising=False)
    monkeypatch.setenv("INGESTION_AUTO_RETRY_MAX_ATTEMPTS", "99")

    assert automatic_retry_enabled() is False
    assert automatic_retry_max_attempts() == 5

    monkeypatch.setenv("INGESTION_AUTO_RETRY_ENABLED", "true")
    monkeypatch.setenv("INGESTION_AUTO_RETRY_MAX_ATTEMPTS", "2")
    assert automatic_retry_enabled() is True
    assert automatic_retry_max_attempts() == 2

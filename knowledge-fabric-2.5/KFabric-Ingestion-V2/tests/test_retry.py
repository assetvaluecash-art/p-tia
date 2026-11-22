"""Unit tests for retry utilities."""

import time
from unittest.mock import Mock, patch

import pytest

from kbfabric_ingest.utils.retry import (
    retry_with_backoff,
    retry_operation,
    RetryExhaustedError
)


class TestRetryWithBackoff:
    """Tests for the retry_with_backoff decorator."""
    
    def test_success_on_first_attempt(self):
        """Function succeeds on first attempt - no retries needed."""
        call_count = 0
        
        @retry_with_backoff(max_retries=3)
        def always_succeeds():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = always_succeeds()
        
        assert result == "success"
        assert call_count == 1
    
    def test_success_after_failures(self):
        """Function succeeds after some failures."""
        call_count = 0
        
        @retry_with_backoff(max_retries=3, base_delay=0.01)  # Fast for testing
        def fails_twice_then_succeeds():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection failed")
            return "success"
        
        result = fails_twice_then_succeeds()
        
        assert result == "success"
        assert call_count == 3
    
    def test_exhausted_retries_raises(self):
        """All retries exhausted - raises the last exception."""
        call_count = 0
        
        @retry_with_backoff(max_retries=3, base_delay=0.01)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"Attempt {call_count} failed")
        
        with pytest.raises(ValueError, match="Attempt 3 failed"):
            always_fails()
        
        assert call_count == 3
    
    def test_specific_exception_types(self):
        """Only retries on specified exception types."""
        call_count = 0
        
        @retry_with_backoff(
            max_retries=3,
            base_delay=0.01,
            exceptions=(ConnectionError,)  # Only retry ConnectionError
        )
        def raises_different_exceptions():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection failed")
            raise TypeError("Type error - should not retry")
        
        with pytest.raises(TypeError):
            raises_different_exceptions()
        
        # Should have tried twice: first attempt (ConnectionError), 
        # second attempt (TypeError - not caught)
        assert call_count == 2
    
    def test_on_retry_callback(self):
        """on_retry callback is called before each retry."""
        call_count = 0
        retry_callbacks = []
        
        def on_retry_callback(exception, attempt):
            retry_callbacks.append((str(exception), attempt))
        
        @retry_with_backoff(
            max_retries=3,
            base_delay=0.01,
            on_retry=on_retry_callback
        )
        def fails_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"Error {call_count}")
            return "success"
        
        result = fails_twice()
        
        assert result == "success"
        assert len(retry_callbacks) == 2
        assert retry_callbacks[0] == ("Error 1", 1)
        assert retry_callbacks[1] == ("Error 2", 2)
    
    def test_exponential_backoff_timing(self):
        """Delays increase exponentially between retries."""
        call_times = []
        
        @retry_with_backoff(max_retries=3, base_delay=0.1, max_delay=1.0)
        def track_timing_and_fail():
            call_times.append(time.time())
            raise RuntimeError("Always fails")
        
        start = time.time()
        
        with pytest.raises(RuntimeError):
            track_timing_and_fail()
        
        assert len(call_times) == 3
        
        # Check delays between calls
        # First retry: ~0.1s delay (base_delay * 2^0)
        # Second retry: ~0.2s delay (base_delay * 2^1)
        delay1 = call_times[1] - call_times[0]
        delay2 = call_times[2] - call_times[1]
        
        # Allow some tolerance for timing
        assert 0.08 <= delay1 <= 0.15, f"First delay was {delay1}"
        assert 0.15 <= delay2 <= 0.30, f"Second delay was {delay2}"
    
    def test_max_delay_cap(self):
        """Delay is capped at max_delay."""
        call_times = []
        
        @retry_with_backoff(max_retries=5, base_delay=0.5, max_delay=0.2)
        def track_timing_and_fail():
            call_times.append(time.time())
            raise RuntimeError("Always fails")
        
        with pytest.raises(RuntimeError):
            track_timing_and_fail()
        
        # All delays should be capped at max_delay (0.2s)
        for i in range(1, len(call_times)):
            delay = call_times[i] - call_times[i-1]
            assert delay <= 0.25, f"Delay {i} was {delay}, should be <= 0.25"
    
    def test_preserves_function_metadata(self):
        """Decorator preserves the original function's metadata."""
        @retry_with_backoff()
        def documented_function():
            """This is a documented function."""
            pass
        
        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is a documented function."


class TestRetryOperation:
    """Tests for the retry_operation function."""
    
    def test_success_on_first_attempt(self):
        """Operation succeeds on first attempt."""
        operation = Mock(return_value="success")
        
        result = retry_operation(operation, max_retries=3)
        
        assert result == "success"
        assert operation.call_count == 1
    
    def test_success_after_failures(self):
        """Operation succeeds after some failures."""
        call_count = 0
        
        def flaky_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Failed")
            return "success"
        
        result = retry_operation(
            flaky_operation,
            max_retries=3,
            base_delay=0.01
        )
        
        assert result == "success"
        assert call_count == 3
    
    def test_exhausted_retries_raises(self):
        """All retries exhausted - raises the last exception."""
        operation = Mock(side_effect=TimeoutError("Timed out"))
        
        with pytest.raises(TimeoutError):
            retry_operation(
                operation,
                max_retries=3,
                base_delay=0.01
            )
        
        assert operation.call_count == 3
    
    def test_operation_name_in_logs(self):
        """Custom operation name is used in logs."""
        with patch('kbfabric_ingest.utils.retry.logger') as mock_logger:
            operation = Mock(side_effect=RuntimeError("Error"))
            
            with pytest.raises(RuntimeError):
                retry_operation(
                    operation,
                    max_retries=2,
                    base_delay=0.01,
                    operation_name="custom_operation"
                )
            
            # Check that the custom name was used in logging
            log_calls = mock_logger.warning.call_args_list
            assert any("custom_operation" in str(call) for call in log_calls)
    
    def test_lambda_operation(self):
        """Works with lambda functions."""
        counter = {"value": 0}
        
        result = retry_operation(
            lambda: (counter.update(value=counter["value"] + 1), "success")[1],
            max_retries=3,
            operation_name="lambda_test"
        )
        
        assert result == "success"
        assert counter["value"] == 1


class TestRetryExhaustedError:
    """Tests for the RetryExhaustedError exception."""
    
    def test_stores_last_exception(self):
        """RetryExhaustedError stores the last exception."""
        original_error = ValueError("Original error")
        error = RetryExhaustedError("Retries exhausted", last_exception=original_error)
        
        assert str(error) == "Retries exhausted"
        assert error.last_exception is original_error
    
    def test_none_last_exception(self):
        """RetryExhaustedError works without a last exception."""
        error = RetryExhaustedError("Retries exhausted")
        
        assert str(error) == "Retries exhausted"
        assert error.last_exception is None


"""Retry utilities with exponential backoff and circuit breaker.

Provides decorators and utilities for retrying operations that may fail
due to transient errors (network issues, database connection timeouts, etc.).

Includes circuit breaker pattern to prevent overwhelming consistently failing services.

Example:
    @retry_with_backoff(max_retries=3, exceptions=(ConnectionError,))
    def connect_to_database():
        return database.connect()

    @circuit_breaker(failure_threshold=5, recovery_timeout=60)
    def call_external_api():
        return requests.get("https://api.example.com")
"""

import threading
import time
from functools import wraps
from typing import Any, Callable, Type, TypeVar

from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

# Type variable for generic return type
T = TypeVar('T')


class RetryExhaustedError(Exception):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, message: str, last_exception: Exception | None = None):
        super().__init__(message)
        self.last_exception = last_exception


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and operation is blocked."""

    def __init__(self, message: str, next_attempt_time: float | None = None):
        super().__init__(message)
        self.next_attempt_time = next_attempt_time


class CircuitBreaker:
    """Circuit breaker implementation to prevent overwhelming failing services.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Service is failing, requests are blocked
    - HALF_OPEN: Testing if service has recovered

    Transitions:
    CLOSED -> OPEN: When failure count exceeds threshold
    OPEN -> HALF_OPEN: After recovery timeout
    HALF_OPEN -> CLOSED: After successful test request
    HALF_OPEN -> OPEN: After failed test request
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0, name: str = "circuit_breaker"):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            name: Identifier for this circuit breaker (for logging)
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name

        # Thread-safe state
        self._lock = threading.Lock()
        self._failure_count = 0
        self._last_failure_time = None
        self._state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

        logger.info(
            "Circuit breaker initialized",
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout
        )

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self._last_failure_time is None:
            return False
        return time.time() - self._last_failure_time >= self.recovery_timeout

    def can_execute(self) -> bool:
        """Check if operation can be executed based on circuit breaker state."""
        with self._lock:
            if self._state == "CLOSED":
                return True
            elif self._state == "OPEN":
                if self._should_attempt_reset():
                    self._state = "HALF_OPEN"
                    logger.info("Circuit breaker transitioning to HALF_OPEN", name=self.name)
                    return True
                else:
                    return False
            elif self._state == "HALF_OPEN":
                return True
            return False

    def record_success(self):
        """Record a successful operation."""
        with self._lock:
            self._failure_count = 0
            if self._state == "HALF_OPEN":
                self._state = "CLOSED"
                logger.info("Circuit breaker transitioning to CLOSED (recovery successful)", name=self.name)

    def record_failure(self):
        """Record a failed operation."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                logger.warning("Circuit breaker transitioning to OPEN (recovery failed)", name=self.name)
            elif self._state == "CLOSED" and self._failure_count >= self.failure_threshold:
                self._state = "OPEN"
                logger.warning(
                    "Circuit breaker transitioning to OPEN (failure threshold exceeded)",
                    name=self.name,
                    failure_count=self._failure_count,
                    threshold=self.failure_threshold
                )

    def get_state(self) -> str:
        """Get current circuit breaker state."""
        with self._lock:
            return self._state

    def get_failure_count(self) -> int:
        """Get current failure count."""
        with self._lock:
            return self._failure_count

    def get_next_attempt_time(self) -> float | None:
        """Get time when next attempt will be allowed (if OPEN)."""
        with self._lock:
            if self._state == "OPEN" and self._last_failure_time is not None:
                return self._last_failure_time + self.recovery_timeout
            return None


# Global circuit breaker registry for different services
_circuit_breakers: dict[str, CircuitBreaker] = {}
_circuit_breakers_lock = threading.Lock()


def get_circuit_breaker(name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0) -> CircuitBreaker:
    """Get or create a circuit breaker for a service."""
    with _circuit_breakers_lock:
        if name not in _circuit_breakers:
            _circuit_breakers[name] = CircuitBreaker(failure_threshold, recovery_timeout, name)
        return _circuit_breakers[name]


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    service_name: str | None = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that applies circuit breaker pattern to prevent overwhelming failing services.

    Args:
        failure_threshold: Consecutive failures before opening circuit
        recovery_timeout: Seconds to wait before testing recovery
        service_name: Identifier for the service (defaults to function name)

    Returns:
        Decorated function with circuit breaker protection

    Example:
        @circuit_breaker(failure_threshold=3, recovery_timeout=30, service_name="weaviate")
        def write_to_weaviate(data):
            return weaviate_client.write(data)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        breaker_name = service_name or f"{func.__module__}.{func.__name__}"

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            breaker = get_circuit_breaker(breaker_name, failure_threshold, recovery_timeout)

            if not breaker.can_execute():
                next_attempt = breaker.get_next_attempt_time()
                wait_time = next_attempt - time.time() if next_attempt else None
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{breaker_name}' is OPEN. "
                    f"Next attempt in {wait_time:.1f}s if wait_time else 'unknown' seconds.",
                    next_attempt_time=next_attempt
                )

            try:
                result = func(*args, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                breaker.record_failure()
                raise

        return wrapper
    return decorator


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[Exception, int], None] | None = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for retrying functions with exponential backoff.
    
    Implements exponential backoff with a maximum of 3 attempts by default,
    ensuring total wait time is bounded (~3 seconds with defaults).
    
    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds between retries (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 10.0)
        exceptions: Tuple of exception types to catch and retry on
        on_retry: Optional callback(exception, attempt) called before each retry
        
    Returns:
        Decorated function that will retry on specified exceptions
        
    Example:
        @retry_with_backoff(max_retries=3, exceptions=(ConnectionError, TimeoutError))
        def fetch_data():
            return requests.get("https://api.example.com/data")
            
    Timeline with defaults (max_retries=3, base_delay=1.0):
        - Attempt 1: Immediate
        - Attempt 2: After 1.0s delay
        - Attempt 3: After 2.0s delay (capped at max_delay)
        Total max wait: ~3 seconds before giving up
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt < max_retries - 1:
                        # Calculate delay with exponential backoff
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        
                        logger.warning(
                            "[RETRY] %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                            func.__name__,
                            attempt + 1,
                            max_retries,
                            str(e),
                            delay
                        )
                        
                        # Call optional retry callback
                        if on_retry:
                            on_retry(e, attempt + 1)
                        
                        time.sleep(delay)
                    else:
                        logger.error(
                            "[RETRY] %s failed after %d attempts: %s",
                            func.__name__,
                            max_retries,
                            str(e)
                        )
            
            # All retries exhausted - raise the last exception
            if last_exception is not None:
                raise last_exception
            
            # This shouldn't happen, but satisfy type checker
            raise RetryExhaustedError(
                f"{func.__name__} failed after {max_retries} attempts"
            )
        
        return wrapper
    return decorator


def retry_operation(
    operation: Callable[..., T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
    operation_name: str | None = None
) -> T:
    """
    Retry an operation with exponential backoff (non-decorator version).
    
    Useful when you can't use a decorator (e.g., lambda functions or
    dynamically created callables).
    
    Args:
        operation: The callable to retry
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds between retries
        max_delay: Maximum delay between retries
        exceptions: Tuple of exception types to catch and retry on
        operation_name: Optional name for logging (defaults to callable name)
        
    Returns:
        Result of the successful operation
        
    Raises:
        The last exception if all retries fail
        
    Example:
        result = retry_operation(
            lambda: database.connect(),
            max_retries=3,
            exceptions=(ConnectionError,),
            operation_name="database_connection"
        )
    """
    name = operation_name or getattr(operation, '__name__', 'operation')
    last_exception: Exception | None = None
    
    for attempt in range(max_retries):
        try:
            return operation()
        except exceptions as e:
            last_exception = e
            
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                
                logger.warning(
                    "[RETRY] %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    name,
                    attempt + 1,
                    max_retries,
                    str(e),
                    delay
                )
                
                time.sleep(delay)
            else:
                logger.error(
                    "[RETRY] %s failed after %d attempts: %s",
                    name,
                    max_retries,
                    str(e)
                )
    
    if last_exception is not None:
        raise last_exception
    
    raise RetryExhaustedError(f"{name} failed after {max_retries} attempts")


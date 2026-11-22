"""Performance timing utilities for KBFabricKB."""

import time
from functools import wraps
from typing import Any, Callable


def timeit(func: Callable) -> Callable:
    """
    Decorator to measure and log execution time of a function.
    
    Usage:
        @timeit
        def my_function():
            # ... code ...
    
    The decorator prints timing information and returns the original result.
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time
            print(f"[TIMING] {func.__name__} completed in {duration:.2f}s ({duration/60:.2f} minutes)", flush=True)
            return result
        except Exception as e:
            duration = time.time() - start_time
            print(f"[TIMING] {func.__name__} failed after {duration:.2f}s ({duration/60:.2f} minutes): {e}", flush=True)
            raise
    return wrapper


class Timer:
    """Context manager for timing code blocks."""
    
    def __init__(self, name: str = "Operation"):
        """
        Initialize timer.
        
        Args:
            name: Name of the operation being timed
        """
        self.name = name
        self.start_time = None
        self.duration = None
    
    def __enter__(self):
        """Start timing."""
        self.start_time = time.time()
        print(f"[TIMER] {self.name} started", flush=True)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timing and print duration."""
        self.duration = time.time() - self.start_time
        status = "completed" if exc_type is None else "failed"
        print(f"[TIMER] {self.name} {status} in {self.duration:.2f}s ({self.duration/60:.2f} minutes)", flush=True)
        return False  # Don't suppress exceptions


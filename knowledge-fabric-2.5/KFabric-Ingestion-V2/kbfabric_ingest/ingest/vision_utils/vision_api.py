"""
Vision API utilities with retry logic and error handling.

Designed for lift-and-shift to kbfabric_ingest/ingest/vision_api.py
"""

import time
import requests
from typing import Optional, Dict, Any


def check_vision_model_health(base_url: str, timeout: int = 5) -> bool:
    """
    Check if vision model API is accessible.
    
    Args:
        base_url: Base URL for Ollama API (e.g., "http://localhost:11434")
        timeout: Timeout in seconds
        
    Returns:
        True if API is accessible, False otherwise
    """
    try:
        response = requests.get(f'{base_url}/api/tags', timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def call_vision_api_with_retry(
    payload: Dict[str, Any],
    base_url: str,
    timeout: int = 300,
    max_retries: int = 3,
    retry_delay: float = 2.0
) -> tuple[Optional[requests.Response], Optional[str]]:
    """
    Call vision API with exponential backoff retry logic.
    
    Args:
        payload: Request payload for /api/generate endpoint
        base_url: Base URL for Ollama API
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts
        retry_delay: Initial delay between retries (exponential backoff)
        
    Returns:
        (response, error_message)
        - response: Response object if successful, None otherwise
        - error_message: Error message if failed, None otherwise
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            print(f"[API] Attempt {attempt + 1}/{max_retries}: Calling {base_url}/api/generate (timeout={timeout}s)...", flush=True)
            response = requests.post(
                f'{base_url}/api/generate',
                json=payload,
                timeout=timeout
            )
            
            print(f"[API] Response received: status={response.status_code}, size={len(response.text)} chars", flush=True)
            
            if response.status_code == 200:
                return response, None
            
            # Non-200 status code
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            print(f"[API] Non-200 response: {last_error}", flush=True)
            
        except requests.exceptions.Timeout as e:
            last_error = f"Timeout after {timeout}s: {e}"
            print(f"[API] Timeout error: {last_error}", flush=True)
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            print(f"[API] Connection error: {last_error}", flush=True)
        except Exception as e:
            last_error = f"Unexpected error: {e}"
            print(f"[API] Unexpected error: {last_error}", flush=True)
        
        # If not last attempt, wait before retry
        if attempt < max_retries - 1:
            wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
            print(f"[API] Retry {attempt + 1}/{max_retries} after {wait_time:.1f}s...", flush=True)
            time.sleep(wait_time)
    
    return None, last_error

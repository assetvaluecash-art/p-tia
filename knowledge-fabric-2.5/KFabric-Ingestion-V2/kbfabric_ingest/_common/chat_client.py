"""
Chat API client with KV cache optimization.

Direct Ollama mode: /api/chat with fallback to /api/generate.
Gateway mode: /ai-assistant/conversation with fallback to /ai-assistant/content.
"""

import json
import time
import random
from typing import Dict, Any, List, Optional, Tuple, Callable, TYPE_CHECKING
from datetime import datetime, timezone
from functools import wraps

import requests
from kbfabric_ingest.utils.logger import get_logger

if TYPE_CHECKING:
    from kbfabric_ingest._common.litellm_gateway_client import ModelRouterGatewayClient

logger = get_logger(__name__)


def retry_with_exponential_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True
):
    """
    Decorator for retrying functions with exponential backoff.

    Handles HTTP 429 (rate limit), 502, 503, 504 errors and connection timeouts.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
                    last_exception = e

                    # Check if this is a retryable error
                    if isinstance(e, requests.exceptions.HTTPError):
                        status_code = e.response.status_code if e.response else None
                        if status_code not in [429, 502, 503, 504]:
                            # Non-retryable HTTP error
                            raise e
                    elif isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                        # Network errors are retryable
                        pass
                    else:
                        # Other request exceptions
                        raise e

                    if attempt == max_retries:
                        # Final attempt failed
                        logger.error(
                            f"Final retry attempt failed for {func.__name__}",
                            attempt=attempt + 1,
                            max_retries=max_retries + 1,
                            error=str(e)
                        )
                        raise e

                    # Calculate delay with exponential backoff and jitter
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    if jitter:
                        delay = delay * (0.5 + random.random() * 0.5)  # Add 50% jitter

                    logger.warning(
                        f"Retryable error in {func.__name__}, attempt {attempt + 1}/{max_retries + 1}",
                        delay=f"{delay:.2f}s",
                        error=str(e)
                    )

                    time.sleep(delay)

            # This should never be reached, but just in case
            raise last_exception

        return wrapper
    return decorator


class ChatLLMClient:
    """
    LLM client optimized for KV cache using /api/chat endpoint.

    Features:
    - System/user message separation for KV cache optimization
    - Automatic fallback to /api/generate for compatibility
    - Performance monitoring and metrics
    - Comprehensive error handling
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        timeout_sec: int = 300,
        run_id: Optional[str] = None,
        gateway_client: Optional["ModelRouterGatewayClient"] = None,
    ):
        """
        Initialize chat client.

        Args:
            base_url: Ollama server URL (e.g., "http://localhost:11434")
            model_name: Model name or logical gateway alias (e.g., "TEXT")
            timeout_sec: Request timeout in seconds
            gateway_client: Optional Model Router gateway client (LiteLLM via Kong)
        """
        self.base_url = base_url.rstrip('/')
        self.model_name = model_name
        self.timeout_sec = timeout_sec
        self.gateway_client = gateway_client
        self.run_id = (run_id or "").strip() or None
        self._run_logger = None
        if self.run_id:
            # Per-run raw I/O logger (JSONL)
            from kbfabric_ingest.utils.logger import get_llm_text_run_logger
            self._run_logger = get_llm_text_run_logger(self.run_id, __name__)

        # HTTP session for keep-alive connections and connection pooling
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Explicit keep-alive headers for persistent connections
        self.session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=300, max=1000'
        })
        self.logger = get_logger(f"{__name__}.{model_name}")

    def call_with_system_user(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.0,
        num_ctx: int = 8192,
        operation: str = "llm_text",
        call_id: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Call LLM with system/user message separation for KV cache optimization.

        Args:
            system_prompt: Static system prompt (gets cached)
            user_content: Dynamic user content (varies per request)
            temperature: Sampling temperature
            num_ctx: Context window size
            **kwargs: Additional parameters

        Returns:
            Tuple of (response_text, metadata_dict)
        """
        from kbfabric_ingest.monitoring.kv_cache_metrics import record_kv_cache_call

        start_time = time.time()
        request_timestamp = datetime.now(timezone.utc).isoformat()

        # Try /api/chat first (optimized)
        try:
            response_text, metadata = self._call_chat_api(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=temperature,
                num_ctx=num_ctx,
                operation=operation,
                call_id=call_id,
                **kwargs
            )

            metadata.update({
                'api_used': 'chat',
                'kv_cache_optimized': True,
                'system_prompt_length': len(system_prompt),
                'user_content_length': len(user_content),
                'total_tokens_estimate': (len(system_prompt) + len(user_content)) // 4,  # Rough estimate
                'success': True
            })

            # Record metrics
            record_kv_cache_call(metadata)

            return response_text, metadata

        except Exception as chat_error:
            self.logger.warning(
                f"Chat API failed, falling back to generate API: {chat_error}",
                error_type=type(chat_error).__name__,
                model=self.model_name
            )

            # Fallback to /api/generate
            combined_prompt = f"{system_prompt}\n\n{user_content}"
            response_text, metadata = self._call_generate_api(
                prompt=combined_prompt,
                temperature=temperature,
                num_ctx=num_ctx,
                operation=operation,
                call_id=call_id,
                **kwargs
            )

            metadata.update({
                'api_used': 'generate',
                'kv_cache_optimized': False,
                'fallback_reason': str(chat_error),
                'combined_prompt_length': len(combined_prompt),
                'total_tokens_estimate': len(combined_prompt) // 4,
                'success': True
            })

            # Record metrics
            record_kv_cache_call(metadata)

            return response_text, metadata

    @retry_with_exponential_backoff(max_retries=5, base_delay=1.0, max_delay=60.0)
    def _call_chat_api(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        num_ctx: int = 8192,
        operation: str = "llm_text",
        call_id: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Call /api/chat endpoint with message format.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature
            num_ctx: Context window size

        Returns:
            Tuple of (response_text, metadata_dict)
        """
        request_timestamp = datetime.now(timezone.utc).isoformat()

        if self.gateway_client is not None:
            return self._call_gateway_conversation(
                messages=messages,
                temperature=temperature,
                operation=operation,
                call_id=call_id,
                request_timestamp=request_timestamp,
            )

        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx
            }
        }

        # Add any additional options from kwargs
        if kwargs:
            payload["options"].update(kwargs)

        start_time = time.time()

        try:
            if self._run_logger is not None:
                self._run_logger.info(json.dumps({
                    "run_id": self.run_id,
                    "call_id": call_id,
                    "timestamp": request_timestamp,
                    "operation": operation,
                    "event": "request",
                    "endpoint": "/api/chat",
                    "payload": payload,
                }, ensure_ascii=False))

            response = self.session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout_sec
            )
            response.raise_for_status()

            duration_ms = (time.time() - start_time) * 1000

            # Parse response
            result = response.json()
            response_text = result.get("message", {}).get("content", "")

            metadata = {
                'endpoint': '/api/chat',
                'status_code': response.status_code,
                'duration_ms': round(duration_ms, 1),
                'request_timestamp': request_timestamp,
                'response_timestamp': datetime.now(timezone.utc).isoformat(),
                'model': self.model_name,
                'temperature': temperature,
                'num_ctx': num_ctx,
                'message_count': len(messages),
                'success': True
            }

            if self._run_logger is not None:
                self._run_logger.info(json.dumps({
                    "run_id": self.run_id,
                    "call_id": call_id,
                    "timestamp": metadata["response_timestamp"],
                    "operation": operation,
                    "event": "response",
                    "endpoint": "/api/chat",
                    "status_code": response.status_code,
                    "raw_response_text": response.text,
                }, ensure_ascii=False))

            self.logger.info(
                "Chat API call successful",
                duration_ms=round(duration_ms, 1),
                response_length=len(response_text),
                model=self.model_name
            )

            return response_text, metadata

        except requests.exceptions.Timeout:
            raise RuntimeError(f"Chat API timeout after {self.timeout_sec}s")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise RuntimeError("Chat API not supported by this Ollama version")
            raise RuntimeError(f"Chat API error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Chat API failed: {str(e)}")

    def _call_gateway_conversation(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        operation: str,
        call_id: Optional[str],
        request_timestamp: str,
    ) -> Tuple[str, Dict[str, Any]]:
        from kbfabric_ingest._common.litellm_gateway_client import GATEWAY_ENDPOINT_CONVERSATION

        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if self._run_logger is not None:
            self._run_logger.info(json.dumps({
                "run_id": self.run_id,
                "call_id": call_id,
                "timestamp": request_timestamp,
                "operation": operation,
                "event": "request",
                "endpoint": GATEWAY_ENDPOINT_CONVERSATION,
                "payload": payload,
            }, ensure_ascii=False))

        response_text, metadata = self.gateway_client.conversation(
            messages=messages,
            model=self.model_name,
            temperature=temperature,
        )
        metadata["request_timestamp"] = request_timestamp
        metadata["response_timestamp"] = datetime.now(timezone.utc).isoformat()

        if self._run_logger is not None:
            self._run_logger.info(json.dumps({
                "run_id": self.run_id,
                "call_id": call_id,
                "timestamp": metadata["response_timestamp"],
                "operation": operation,
                "event": "response",
                "endpoint": GATEWAY_ENDPOINT_CONVERSATION,
                "status_code": metadata.get("status_code"),
                "raw_response_text": response_text,
            }, ensure_ascii=False))

        self.logger.info(
            "Gateway conversation call successful",
            duration_ms=metadata.get("duration_ms"),
            response_length=len(response_text),
            model=self.model_name,
        )
        return response_text, metadata

    @retry_with_exponential_backoff(max_retries=5, base_delay=1.0, max_delay=60.0)
    def _call_generate_api(
        self,
        prompt: str,
        temperature: float = 0.0,
        num_ctx: int = 8192,
        operation: str = "llm_text",
        call_id: Optional[str] = None,
        **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Call /api/generate endpoint (fallback).

        Args:
            prompt: Full prompt text
            temperature: Sampling temperature
            num_ctx: Context window size

        Returns:
            Tuple of (response_text, metadata_dict)
        """
        request_timestamp = datetime.now(timezone.utc).isoformat()

        if self.gateway_client is not None:
            return self._call_gateway_content(
                prompt=prompt,
                temperature=temperature,
                operation=operation,
                call_id=call_id,
                request_timestamp=request_timestamp,
            )

        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx
            }
        }

        # Add any additional options from kwargs
        if kwargs:
            payload["options"].update(kwargs)

        start_time = time.time()

        try:
            if self._run_logger is not None:
                self._run_logger.info(json.dumps({
                    "run_id": self.run_id,
                    "call_id": call_id,
                    "timestamp": request_timestamp,
                    "operation": operation,
                    "event": "request",
                    "endpoint": "/api/generate",
                    "payload": payload,
                }, ensure_ascii=False))

            response = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout_sec
            )
            response.raise_for_status()

            duration_ms = (time.time() - start_time) * 1000

            # Parse response
            result = response.json()
            response_text = result.get("response", "")

            metadata = {
                'endpoint': '/api/generate',
                'status_code': response.status_code,
                'duration_ms': round(duration_ms, 1),
                'request_timestamp': request_timestamp,
                'response_timestamp': datetime.now(timezone.utc).isoformat(),
                'model': self.model_name,
                'temperature': temperature,
                'num_ctx': num_ctx,
                'prompt_length': len(prompt),
                'success': True
            }

            if self._run_logger is not None:
                self._run_logger.info(json.dumps({
                    "run_id": self.run_id,
                    "call_id": call_id,
                    "timestamp": metadata["response_timestamp"],
                    "operation": operation,
                    "event": "response",
                    "endpoint": "/api/generate",
                    "status_code": response.status_code,
                    "raw_response_text": response.text,
                }, ensure_ascii=False))

            self.logger.info(
                "Generate API call successful (fallback)",
                duration_ms=round(duration_ms, 1),
                response_length=len(response_text),
                model=self.model_name
            )

            return response_text, metadata

        except requests.exceptions.Timeout:
            raise RuntimeError(f"Generate API timeout after {self.timeout_sec}s")
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Generate API error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Generate API failed: {str(e)}")

    def _call_gateway_content(
        self,
        prompt: str,
        temperature: float,
        operation: str,
        call_id: Optional[str],
        request_timestamp: str,
    ) -> Tuple[str, Dict[str, Any]]:
        from kbfabric_ingest._common.litellm_gateway_client import GATEWAY_ENDPOINT_CONTENT

        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "temperature": temperature,
            "stream": False,
        }
        if self._run_logger is not None:
            self._run_logger.info(json.dumps({
                "run_id": self.run_id,
                "call_id": call_id,
                "timestamp": request_timestamp,
                "operation": operation,
                "event": "request",
                "endpoint": GATEWAY_ENDPOINT_CONTENT,
                "payload": payload,
            }, ensure_ascii=False))

        response_text, metadata = self.gateway_client.content(
            prompt=prompt,
            model=self.model_name,
            temperature=temperature,
        )
        metadata["request_timestamp"] = request_timestamp
        metadata["response_timestamp"] = datetime.now(timezone.utc).isoformat()

        if self._run_logger is not None:
            self._run_logger.info(json.dumps({
                "run_id": self.run_id,
                "call_id": call_id,
                "timestamp": metadata["response_timestamp"],
                "operation": operation,
                "event": "response",
                "endpoint": GATEWAY_ENDPOINT_CONTENT,
                "status_code": metadata.get("status_code"),
                "raw_response_text": response_text,
            }, ensure_ascii=False))

        self.logger.info(
            "Gateway content call successful",
            duration_ms=metadata.get("duration_ms"),
            response_length=len(response_text),
            model=self.model_name,
        )
        return response_text, metadata

    def health_check(self) -> Dict[str, Any]:
        """
        Check if the model is available and responding.

        Returns:
            Health check results
        """
        try:
            # Quick test with minimal prompt
            _, metadata = self.call_with_system_user(
                system_prompt="You are a helpful assistant.",
                user_content="Hello",
                temperature=0.0
            )

            return {
                'healthy': True,
                'model': self.model_name,
                'api_used': metadata.get('api_used', 'unknown'),
                'response_time_ms': metadata.get('duration_ms', 0),
                'kv_cache_supported': metadata.get('kv_cache_optimized', False)
            }

        except Exception as e:
            return {
                'healthy': False,
                'model': self.model_name,
                'error': str(e),
                'error_type': type(e).__name__
            }

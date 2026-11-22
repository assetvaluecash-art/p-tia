"""LLM client for Ollama API calls.

This module provides a client for interacting with Ollama LLM APIs
for text and vision-based information extraction.

"""

import hashlib
import re

import itertools

import json

import time

import uuid

from datetime import datetime, timezone

from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError

import requests

from kbfabric_ingest.ingest.config import get_app_config

from kbfabric_ingest.ingest.models import BlockCategory

from custom_ml.embeddings import EmbeddingsClient

from kbfabric_ingest.utils.logger import (
    get_logger,
    get_llm_text_logger,
    get_llm_visual_logger,
    get_llm_full_logger,
    get_llm_text_run_logger,
    get_llm_vision_run_logger,
)

logger = get_logger(__name__)

llm_text_logger = get_llm_text_logger(__name__)

llm_visual_logger = get_llm_visual_logger(__name__)

# ============================================================================
# PYDANTIC MODELS FOR VISION LLM RESPONSE VALIDATION
# ============================================================================

class VisionEntity(BaseModel):
    """Entity extracted from vision IE."""
    type: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    aliases: Optional[list[str]] = None
    description: Optional[str] = None

class VisionRelationship(BaseModel):
    """Relationship extracted from vision IE."""
    from_type: str
    from_value: str
    rel_type: str
    to_type: str
    to_value: str
    confidence: float = Field(ge=0.0, le=1.0)

class VisionIEResponse(BaseModel):
    """Strict schema for vision LLM response."""
    description: str
    entities: list[VisionEntity] = Field(default_factory=list)
    relationships: list[VisionRelationship] = Field(default_factory=list)
    content_details: Optional[dict] = Field(default_factory=dict)

# ============================================================================

class LLMClient:

    """Client for LLM operations via Ollama API.

    Features:
    - Thread-safe call ID generation using itertools.count()
    - Text and vision model support
    - Comprehensive logging of all LLM interactions
    """

    # Thread-safe counter for LLM call sequence IDs
    # itertools.count() is thread-safe and doesn't require locks
    _call_counter = itertools.count(1)

    def __init__(self, config: dict[str, Any]):

        """
        Initialize LLM client.

        Args:
            config: Ollama configuration dictionary
        """
        self.config = config

        self.base_url = config["base_url"]
        self.timeout_sec = config["timeout_sec"]
        self.enable_llm_logging = config["enable_llm_logging"]

        from kbfabric_ingest.ingest.config import get_app_config
        from kbfabric_ingest._common.litellm_gateway_client import build_gateway_client

        app_config = get_app_config()
        self.gateway_client = build_gateway_client(
            app_config,
            timeout_sec=self.timeout_sec,
            session=None,
        )
        self.use_gateway = self.gateway_client is not None

        if self.use_gateway:
            mr = app_config["model_router"]
            self.text_model = mr["text_model"]
            self.logger_init_note = "model_router_gateway"
        else:
            self.text_model = config["text_model"]
            self.logger_init_note = "ollama_direct"

        self.vision_model = config["vision_model"]

        # Per-ingestion-run raw I/O logging (files are created once run_id is set)
        self.run_id: str | None = None
        self.llm_text_run_logger = None
        self.llm_vision_run_logger = None

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

        if self.gateway_client is not None:
            self.gateway_client.session = self.session

        # Explicit keep-alive headers for persistent connections
        self.session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=300, max=1000'
        })

        # Token limits from ollama.text / ollama.vision in .env
        # (LLMClient receives the ollama section, not the full app config)
        text_config = config["text"]
        vision_config = config["vision"]
        self.max_context_tokens = max(
            text_config["max_context_tokens"],
            vision_config["max_context_tokens"],
        )
        self.token_buffer_percent = config["token_buffer_percent"]

        self.logger = get_logger(__name__)
        self.llm_text_logger = get_llm_text_logger(__name__)
        self.llm_visual_logger = get_llm_visual_logger(__name__)
        self.llm_full_logger = get_llm_full_logger(__name__)

        # Initialize embeddings client

        self.embeddings_client = EmbeddingsClient(app_config)

        

        # Initialize LLM I/O logging (JSONL format for full audit trail)

        from pathlib import Path

        self.llm_io_log_dir = Path("logs/llm_io")

        self.llm_io_log_dir.mkdir(parents=True, exist_ok=True)

        self.llm_io_log_file = None  # Will be set when domain is known

        self.llm_io_enabled = True  # Can be disabled via config if needed

    def _create_chat_client(self) -> "ChatLLMClient":
        from kbfabric_ingest._common.chat_client import ChatLLMClient

        return ChatLLMClient(
            base_url=self.base_url,
            model_name=self.text_model,
            timeout_sec=self.timeout_sec,
            run_id=self.run_id,
            gateway_client=self.gateway_client,
        )

    def _init_llm_io_log(self, domain: str):

        """Initialize LLM I/O log file for a specific domain and ingestion run."""

        if not self.llm_io_enabled:

            return

        

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        self.llm_io_log_file = self.llm_io_log_dir / f"llm_io_{domain}_{timestamp}.jsonl"

        self.logger.info("LLM I/O logging initialized", log_file=str(self.llm_io_log_file), domain=domain)

    

    def _log_llm_io(

        self,

        operation: str,

        model_name: str,

        domain: str,

        system_prompt: str,

        user_prompt: str,

        response_raw: str,

        response_parsed: Any,

        duration_ms: float,

        success: bool,

        error: str | None = None,

        images_base64_preview: list[str] | None = None

    ):

        """Log LLM I/O to JSONL file for full audit trail."""

        if not self.llm_io_enabled or not self.llm_io_log_file:

            return

        

        try:

            log_entry = {

                "timestamp": datetime.now(timezone.utc).isoformat(),

                "operation": operation,

                "model_name": model_name,

                "domain": domain,

                "system_prompt": system_prompt,

                "user_prompt": user_prompt if user_prompt else "",  # Full prompt (no truncation for audit)

                "images_base64_preview": images_base64_preview or [],

                "response_raw": response_raw if response_raw else "",  # Full response (no truncation for audit)

                "response_parsed": json.dumps(response_parsed, default=str) if response_parsed else "",  # Full parsed response

                "duration_ms": duration_ms,

                "success": success,

                "error": error

            }

            

            with open(self.llm_io_log_file, "a", encoding="utf-8") as f:

                f.write(json.dumps(log_entry) + "\n")

        

        except Exception as e:

            self.logger.warning(f"Failed to write LLM I/O log: {e}")

    

    def check_vision_model_health(self) -> dict[str, Any]:

        """
        Test if the vision model is responding correctly.

        Returns a health check dictionary with status and diagnostics.
        """

        import base64
        import io
        from PIL import Image

        health = {

            "vision_model": self.vision_model,

            "base_url": self.base_url,

            "status": "unknown",

            "error": None,

            "response_time_ms": 0

        }

        

        try:

            # Create a minimal 10x10 PNG test image

            test_img = Image.new('RGB', (10, 10), color='blue')

            img_buffer = io.BytesIO()

            test_img.save(img_buffer, format='PNG')

            img_bytes = img_buffer.getvalue()

            img_b64 = base64.b64encode(img_bytes).decode('utf-8')

            

            # Test vision model with simple prompt

            import time

            start_time = time.time()

            

            response = self.session.post(

                f"{self.base_url}/api/generate",

                json={

                    "model": self.vision_model,

                    "prompt": "What color is this image? Answer in one word.",

                    "images": [img_b64],

                    "stream": False

                },

                timeout=10

            )

            

            elapsed_ms = (time.time() - start_time) * 1000

            health["response_time_ms"] = round(elapsed_ms, 1)

            

            if response.status_code == 200:

                result = response.json()

                response_text = result.get("response", "").lower()

                

                # Check if response is reasonable

                if response_text and len(response_text) > 0:

                    health["status"] = "healthy"

                    health["sample_response"] = response_text[:100]

                else:

                    health["status"] = "degraded"

                    health["error"] = "Vision model returned empty response"

            else:

                health["status"] = "unhealthy"

                health["error"] = f"HTTP {response.status_code}: {response.text[:200]}"

                

        except requests.exceptions.Timeout:

            health["status"] = "unhealthy"

            health["error"] = "Vision model request timed out after 10s"

        except requests.exceptions.ConnectionError as e:

            health["status"] = "unreachable"

            health["error"] = f"Cannot connect to vision model: {str(e)}"

        except Exception as e:

            health["status"] = "error"

            health["error"] = f"{type(e).__name__}: {str(e)}"

        

        # Log health check result

        self.logger.info(

            "Vision model health check",

            **health

        )

        

        return health

    def estimate_tokens(self, text: str) -> int:

        """

        Estimate token count using ~4 chars per token rule + buffer.

        

        This is a conservative estimate; actual tokenization may vary by model.

        For enterprise safety, we overestimate to prevent context overflow.

        

        Args:

            text: Input text to estimate tokens for

            

        Returns:

            Estimated token count with buffer applied

        """

        if not text:

            return 0

        base_estimate = len(text) / 4  # ~4 chars per token average

        buffer_factor = 1 + (self.token_buffer_percent / 100)

        return int(base_estimate * buffer_factor)

    def check_token_limit(self, prompt: str, operation: str = "ie_text") -> tuple[bool, int, str]:

        """

        Check if prompt is within token limits.

        

        Args:

            prompt: Full prompt text

            operation: Operation name for logging

            

        Returns:

            Tuple of (is_safe, estimated_tokens, warning_message)

        """

        estimated = self.estimate_tokens(prompt)

        threshold_80 = int(self.max_context_tokens * 0.8)

        

        if estimated > self.max_context_tokens:

            msg = f"[TOKEN WARNING] {operation}: {estimated} tokens exceeds limit {self.max_context_tokens}"

            self.logger.warning(msg)

            return (False, estimated, msg)

        elif estimated > threshold_80:

            msg = f"[TOKEN INFO] {operation}: {estimated} tokens ({int(estimated/self.max_context_tokens*100)}% of limit)"

            self.logger.info(msg)

            return (True, estimated, msg)

        

        return (True, estimated, "")

    def _get_call_id(self) -> str:

        """Generate a unique, thread-safe call ID for this LLM request.

        

        Uses itertools.count() which is inherently thread-safe, avoiding

        race conditions when multiple threads make concurrent LLM calls.

        

        Returns:

            Unique call ID in format: call-NNNNNN-XXXXXXXX

        """

        seq = next(LLMClient._call_counter)

        return f"call-{seq:06d}-{uuid.uuid4().hex[:8]}"

    def _log_llm_event(self, call_id: str, event: str, operation: str = "", **data: Any) -> None:

        """Write an LLM event to appropriate log file (text or visual) as structured data.

        

        Also writes to full logger for complete I/O logging.

        """

        # Skip logging if disabled in configuration
        if not self.enable_llm_logging:
            return

        try:

            timestamp = datetime.now(timezone.utc).isoformat()

            # Extract operation from data if not provided as parameter

            op = operation or data.get("operation", "")

            log_entry = {

                "call_id": call_id,

                "event": event,

                "timestamp": timestamp,

                "operation": op,

                **data

            }

            # Determine which logger to use based on operation

            op_l = (op or "").lower()
            if op in ("ie_image", "ie_vision") or ("image" in op_l) or ("vision" in op_l):

                target_logger = self.llm_visual_logger

            else:

                target_logger = self.llm_text_logger

            

            # Log to regular logger (truncated)

            target_logger.info(json.dumps(log_entry, ensure_ascii=False))

            

            # Also log to full logger (no truncation) - only for events with prompt/response

            if "prompt" in data or "response" in data or event in ("LLM call starting", "LLM call response received", "LLM call complete"):

                self.llm_full_logger.info(json.dumps(log_entry, ensure_ascii=False))

        except Exception as log_err:  # pragma: no cover - logging should never raise

            logger.warning("Failed to write LLM log entry", error=str(log_err))

    def set_run_id(self, run_id: str) -> None:
        """
        Set ingestion run id used for per-run raw I/O logging.

        This should be the SAME timestamp used by the generated report filenames,
        so logs and reports are trivially correlatable.
        """
        rid = (run_id or "").strip()
        if not rid:
            return
        self.run_id = rid
        self.llm_text_run_logger = get_llm_text_run_logger(rid, __name__)
        self.llm_vision_run_logger = get_llm_vision_run_logger(rid, __name__)

    def _log_run_io(self, *, call_id: str, operation: str, event: str, **data: Any) -> None:
        """
        Write raw LLM I/O logs to per-run JSONL files only.

        Intentionally does NOT write to the existing llm_calls_*.jsonl logs to avoid
        bloating normal logs with raw prompts/responses.
        """
        if not self.run_id:
            return
        try:
            entry: dict[str, Any] = {
                "run_id": self.run_id,
                "call_id": call_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "operation": operation,
                "event": event,
                **data,
            }
            op_l = (operation or "").lower()
            is_vision = ("vision" in op_l) or ("image" in op_l)
            target = self.llm_vision_run_logger if is_vision else self.llm_text_run_logger
            if target is not None:
                target.info(json.dumps(entry, ensure_ascii=False))
        except Exception as e:  # pragma: no cover
            logger.warning("Failed to write per-run LLM I/O log", error=str(e))

    def _log_llm_call_summary(

        self,

        call_id: str,

        model_name: str,

        operation: str,

        duration_ms: float,

        prompt_length: int,

        response_length: int | None = None,

        block_count: int | None = None,

        result_count: int | None = None,

        error: str | None = None,

        status_code: int | None = None

    ) -> None:

        """

        Log a concise summary of an LLM call for quick scanning.

        """

        summary_data = {

            "model_name": model_name,

            "operation": operation,

            "duration_ms": duration_ms,

            "prompt_length": prompt_length,

        }

        

        if response_length is not None:

            summary_data["response_length"] = response_length

        if block_count is not None:

            summary_data["block_count"] = block_count

        if result_count is not None:

            summary_data["result_count"] = result_count

        if error:

            summary_data["error"] = error

        if status_code:

            summary_data["status_code"] = status_code

        

        self._log_llm_event(call_id, "LLM call summary", **summary_data)

    def _emit_ingestion_llm_call(
        self,
        *,
        operation: str,
        endpoint: str,
        model: str,
        batch_size: int | None = None,
        prompt_chars: int | None = None,
        response_chars: int | None = None,
        duration_ms: float | None = None,
        success: bool = True,
        status_code: int | None = None,
        parse_success: bool | None = None,
        parsed_items_count: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        error: Any | None = None,
    ) -> None:
        """Emit ONE structured, server-safe summary per LLM API call.

        Never logs raw prompts or responses (only character counts). Also records
        Prometheus/OTel telemetry. Both paths are wrapped so logging/metrics can
        never break ingestion.
        """
        op_l = (operation or "").lower()
        is_vision = ("vision" in op_l) or ("image" in op_l)
        event = "ingestion.llm.vision.call" if is_vision else "ingestion.llm.text.call"

        fields: dict[str, Any] = {
            "model": model,
            "endpoint": endpoint,
            "success": bool(success),
        }
        if batch_size is not None:
            fields["batch_size"] = batch_size
        if prompt_chars is not None:
            fields["prompt_chars"] = prompt_chars
        if response_chars is not None:
            fields["response_chars"] = response_chars
        if duration_ms is not None:
            fields["duration_ms"] = round(duration_ms, 2)
        if status_code is not None:
            fields["status_code"] = status_code
        if parse_success is not None:
            fields["parse_success"] = parse_success
        if parsed_items_count is not None:
            fields["parsed_items_count"] = parsed_items_count
        if temperature is not None:
            fields["temperature"] = temperature
        if timeout is not None:
            fields["timeout"] = timeout
        if error is not None:
            fields["error_type"] = type(error).__name__ if isinstance(error, BaseException) else "error"
            fields["error_message"] = str(error)[:500]

        try:
            self.logger.info(event, **fields)
        except Exception:
            pass

        try:
            from kbfabric_ingest._common.telemetry import record_llm_call

            server_addr = (self.base_url or "").replace("http://", "").replace("https://", "")
            record_llm_call(
                operation=operation,
                model=model,
                status="success" if success else "error",
                duration_seconds=(duration_ms or 0) / 1000.0,
                server_address=server_addr or "localhost:11434",
            )
        except Exception:
            pass

    def ie_text_analysis(self, blocks: list[tuple[str, str]], ontology: dict[str, Any]) -> list[dict[str, Any]]:

        """

        Perform information extraction on text blocks with KV cache optimization.

        Uses full ontology context (descriptions, constraints, examples) from

        frozen runtime ontology for ontology-driven IE without industry hardcoding.

        Args:

            blocks: List of (masked_text, domain) tuples

        Returns:

            List of IE result dictionaries

        """

        if not blocks:
            return []

        # Always use optimized version with KV cache (configurable thresholds)
        return self._ie_text_analysis_optimized(blocks, ontology)

    def _ie_text_analysis_optimized(self, blocks: list[tuple[str, str]], ontology: dict[str, Any]) -> list[dict[str, Any]]:

        """Hybrid API strategy: Chat-only, Chat+fallback, or Generate-only based on batch size."""
        import json
        import time
        from datetime import datetime, timezone
        import requests

        try:
            from kbfabric_ingest.llm.prompts import get_system_prompt, build_ie_user_content
        except ImportError as e:
            raise RuntimeError(f"Failed to import prompt functions: {e}")

        from kbfabric_ingest._common.chat_client import ChatLLMClient

        if not blocks:

            return []

        batch_size = len(blocks)

        print(f"[LLM][ie_text] Processing batch of {batch_size} blocks (KV cache optimized)...", flush=True)

        # HYBRID API STRATEGY: Choose optimal API based on batch size (configurable)
        # Read thresholds from config (defaults maintain backward compatibility)
        text_config = self.config.get("ollama", {}).get("text", {})
        single_block_threshold = text_config.get("single_block_threshold", 1)
        chat_fallback_threshold = text_config.get("chat_fallback_threshold", 8)

        if batch_size <= single_block_threshold:
            # Single block: Chat API only (fastest, most reliable for simple cases)
            return self._call_chat_only(blocks, ontology)
        elif batch_size <= chat_fallback_threshold:
            # Small batches: Chat API with intelligent fallback (performance + reliability)
            return self._call_chat_with_fallback(blocks, ontology)
        else:
            # Large batches: Generate API directly (most reliable for complex JSON)
            return self._call_generate_only(blocks, ontology)

    def _call_chat_only(self, blocks: list[tuple[str, str]], ontology: dict[str, Any]) -> list[dict[str, Any]]:
        """Chat API only - no fallback (for single blocks where chat works reliably)."""
        return self._call_chat_api_generic(blocks, ontology, allow_fallback=False)

    def _call_chat_with_fallback(self, blocks: list[tuple[str, str]], ontology: dict[str, Any], allow_fallback: bool = True) -> list[dict[str, Any]]:
        """Chat API with optional fallback to generate (for small batches)."""
        return self._call_chat_api_generic(blocks, ontology, allow_fallback=allow_fallback)

    def _call_generate_only(self, blocks: list[tuple[str, str]], ontology: dict[str, Any]) -> list[dict[str, Any]]:
        """Generate API directly (for large batches where reliability matters most)."""
        return self._call_generate_api_generic(blocks, ontology)

    def _call_chat_api_generic(self, blocks: list[tuple[str, str]], ontology: dict[str, Any], allow_fallback: bool = True) -> list[dict[str, Any]]:
        """Generic chat API implementation with optional fallback."""
        try:
            from kbfabric_ingest.llm.prompts import get_system_prompt, build_ie_user_content
        except ImportError as e:
            raise RuntimeError(f"Failed to import prompt functions: {e}")

        from kbfabric_ingest._common.chat_client import ChatLLMClient

        batch_size = len(blocks)
        first_domain = blocks[0][1] if blocks else None
        print(f"[LLM][ie_text] Processing batch of {batch_size} blocks (Chat API{' with fallback' if allow_fallback else ' only'})...", flush=True)

        from kbfabric_ingest.ingest.ontology_prompt import (
            build_full_ontology_prompt, get_ie_examples_from_ontology,
            format_ie_examples_for_prompt, CLASSIFICATION_DECISION_TREE,
        )

        first_domain = blocks[0][1] if blocks else None
        ontology_context = build_full_ontology_prompt(ontology, domain=first_domain)
        category_mapping = {
            str(item.get("name")): str(item.get("maps_to_node") or item.get("default_node_type") or "")
            for item in ontology.get("block_categories", []) or []
            if isinstance(item, dict) and item.get("name")
        }

        examples = get_ie_examples_from_ontology(ontology)
        formatted_examples = format_ie_examples_for_prompt(examples)

        system_prompt = get_system_prompt('ie_text').format(
            ontology_context=ontology_context,
            category_mapping=category_mapping,
            classification_tree=CLASSIFICATION_DECISION_TREE,
            formatted_examples=formatted_examples,
        )

        user_content = build_ie_user_content(blocks, first_domain)

        # Get configuration
        from kbfabric_ingest.ingest.config import get_app_config

        config = get_app_config()
        ollama_config = config.get("ollama", {})
        temp_config = ollama_config.get("temperature", {})
        ie_temp = temp_config.get("ie_text", 0.0)
        # Use text processing configuration
        text_config = ollama_config.get("text", {})
        num_ctx = text_config.get("num_ctx", 32768)

        # Create chat client
        chat_client = self._create_chat_client()

        # Make optimized call
        start_time = time.time()
        request_timestamp = datetime.now(timezone.utc).isoformat()
        call_id = self._get_call_id()

        self._log_llm_event(
            call_id,
            f"LLM call starting (Chat API{' with fallback' if allow_fallback else ' only'})",
            operation="ie_text",
            model_name=self.text_model,
            system_prompt_length=len(system_prompt),
            user_content_length=len(user_content),
            block_count=batch_size,
            request_timestamp=request_timestamp
        )

        if allow_fallback:
            # Use ChatLLMClient with fallback
            response_text, metadata = chat_client.call_with_system_user(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=ie_temp,
                num_ctx=num_ctx,
                operation="ie_text",
                call_id=call_id
            )
        else:
            # Direct chat call without fallback
            try:
                response_text, metadata = chat_client._call_chat_api(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    temperature=ie_temp,
                    num_ctx=num_ctx,
                    operation="ie_text",
                    call_id=call_id
                )
                metadata.update({
                    'api_used': 'chat',
                    'kv_cache_optimized': True,
                    'system_prompt_length': len(system_prompt),
                    'user_content_length': len(user_content),
                    'total_tokens_estimate': (len(system_prompt) + len(user_content)) // 4,
                    'success': True
                })
            except Exception as e:
                # For chat-only mode, we don't fallback - just re-raise
                raise RuntimeError(f"Chat API failed and fallback disabled: {e}")

        duration_ms = metadata.get('duration_ms', 0)

        # Extract JSON from response
        try:
            ie_results = self._extract_json_from_response(response_text, operation="ie_text")
            if not ie_results:
                raise ValueError("No JSON found in response")
        except Exception as e:
            error_msg = f"Failed to extract JSON from LLM response: {e}"
            self._log_llm_event(
                call_id,
                "JSON extraction failed",
                operation="ie_text",
                model_name=self.text_model,
                response_length=len(response_text),
                error=error_msg
            )
            # Fallback: return default empty results to avoid failing entire job
            default_result = {
                "block_category": "Narrative",
                "kg_node_type": "NarrativeText",
                "entities": [],
                "relationships": []
            }
            self.logger.warning(
                "IE JSON extraction failed - using default empty results",
                operation="ie_text",
                model_name=self.text_model,
                batch_size=len(blocks),
                error=error_msg
            )
            self._emit_ingestion_llm_call(
                operation="ie_text",
                endpoint=metadata.get("endpoint", "/api/chat"),
                model=self.text_model,
                batch_size=batch_size,
                prompt_chars=len(system_prompt) + len(user_content),
                response_chars=len(response_text),
                duration_ms=duration_ms,
                success=True,
                parse_success=False,
                parsed_items_count=0,
                temperature=ie_temp,
                timeout=self.timeout_sec,
            )
            return [default_result] * len(blocks)

        # Validate results - be fault-tolerant for partial LLM failures
        if len(ie_results) < len(blocks):
            self.logger.warning(
                f"Partial IE batch failure: expected {len(blocks)} results, got {len(ie_results)}",
                operation="ie_text",
                batch_size=len(blocks),
                results_count=len(ie_results),
                missing_blocks=len(blocks) - len(ie_results)
            )

            self._log_llm_event(
                call_id,
                "Partial IE batch success - some blocks failed extraction",
                operation="ie_text",
                model_name=self.text_model,
                expected_count=batch_size,
                actual_count=len(ie_results),
                missing_blocks=len(blocks) - len(ie_results)
            )

            # Continue with successful results - failed blocks will be handled upstream

        elif len(ie_results) > len(blocks):
            self.logger.warning(
                f"Extra IE results: expected {len(blocks)} results, got {len(ie_results)}",
                operation="ie_text",
                batch_size=len(blocks),
                results_count=len(ie_results)
            )

            # Truncate extra results to match batch size
            ie_results = ie_results[:len(blocks)]

            self._log_llm_event(
                call_id,
                "IE batch success - truncated extra results",
                operation="ie_text",
                model_name=self.text_model,
                expected_count=batch_size,
                actual_count=len(ie_results),
                truncated_results=len(ie_results) - len(blocks)
            )

        self._log_llm_event(
            call_id,
            "LLM call completed successfully (Chat API)",
            operation="ie_text",
            model_name=self.text_model,
            result_count=len(ie_results),
            duration_ms=duration_ms,
            api_used=metadata.get('api_used', 'chat'),
            kv_cache_optimized=metadata.get('kv_cache_optimized', allow_fallback)
        )

        self._emit_ingestion_llm_call(
            operation="ie_text",
            endpoint=metadata.get("endpoint", "/api/chat"),
            model=self.text_model,
            batch_size=batch_size,
            prompt_chars=len(system_prompt) + len(user_content),
            response_chars=len(response_text),
            duration_ms=duration_ms,
            success=True,
            status_code=metadata.get('status_code'),
            parse_success=True,
            parsed_items_count=len(ie_results),
            temperature=ie_temp,
            timeout=self.timeout_sec,
        )

        return ie_results

    def _call_generate_api_generic(self, blocks: list[tuple[str, str]], ontology: dict[str, Any]) -> list[dict[str, Any]]:
        """Generate API directly for maximum reliability."""
        try:
            from kbfabric_ingest.llm.prompts import get_system_prompt, build_ie_user_content
        except ImportError as e:
            raise RuntimeError(f"Failed to import prompt functions: {e}")

        batch_size = len(blocks)
        first_domain = blocks[0][1] if blocks else None
        print(f"[LLM][ie_text] Processing batch of {batch_size} blocks (Generate API only)...", flush=True)

        from kbfabric_ingest.ingest.ontology_prompt import (
            build_full_ontology_prompt, get_ie_examples_from_ontology,
            format_ie_examples_for_prompt, CLASSIFICATION_DECISION_TREE,
        )

        first_domain = blocks[0][1] if blocks else None
        ontology_context = build_full_ontology_prompt(ontology, domain=first_domain)
        category_mapping = {
            str(item.get("name")): str(item.get("maps_to_node") or item.get("default_node_type") or "")
            for item in ontology.get("block_categories", []) or []
            if isinstance(item, dict) and item.get("name")
        }

        examples = get_ie_examples_from_ontology(ontology)
        formatted_examples = format_ie_examples_for_prompt(examples)

        system_prompt = get_system_prompt('ie_text').format(
            ontology_context=ontology_context,
            category_mapping=category_mapping,
            classification_tree=CLASSIFICATION_DECISION_TREE,
            formatted_examples=formatted_examples,
        )

        user_content = build_ie_user_content(blocks, first_domain)

        combined_prompt = f"{system_prompt}\n\n{user_content}"

        # Get configuration
        from kbfabric_ingest.ingest.config import get_app_config

        config = get_app_config()
        ollama_config = config.get("ollama", {})
        temp_config = ollama_config.get("temperature", {})
        ie_temp = temp_config.get("ie_text", 0.0)
        # Use text processing configuration (not deprecated ollama.num_ctx)
        text_config = ollama_config.get("text", {})
        num_ctx = text_config.get("num_ctx", 32768)

        start_time = time.time()
        request_timestamp = datetime.now(timezone.utc).isoformat()
        call_id = self._get_call_id()

        self._log_llm_event(
            call_id,
            "LLM call starting (Generate API only)",
            operation="ie_text",
            model_name=self.text_model,
            prompt_length=len(combined_prompt),
            block_count=batch_size,
            request_timestamp=request_timestamp
        )

        # Direct generate API call (or Model Router /ai-assistant/content)
        if self.gateway_client is not None:
            from kbfabric_ingest._common.litellm_gateway_client import GATEWAY_ENDPOINT_CONTENT

            try:
                response_text, gateway_meta = self.gateway_client.content(
                    prompt=combined_prompt,
                    model=self.text_model,
                    temperature=ie_temp,
                )
                duration_ms = gateway_meta.get("duration_ms", (time.time() - start_time) * 1000)
                endpoint = GATEWAY_ENDPOINT_CONTENT
                status_code = gateway_meta.get("status_code")
            except Exception as e:
                self._emit_ingestion_llm_call(
                    operation="ie_text",
                    endpoint=GATEWAY_ENDPOINT_CONTENT,
                    model=self.text_model,
                    batch_size=batch_size,
                    prompt_chars=len(combined_prompt),
                    duration_ms=(time.time() - start_time) * 1000,
                    success=False,
                    temperature=ie_temp,
                    timeout=self.timeout_sec,
                    error=e,
                )
                raise RuntimeError(f"Gateway content API failed: {e}") from e
        else:
            payload = {
                "model": self.text_model,
                "prompt": combined_prompt,
                "stream": False,
                "options": {
                    "temperature": ie_temp,
                    "num_ctx": num_ctx
                }
            }

            try:
                import requests
                response = self.session.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                    timeout=self.timeout_sec
                )
                response.raise_for_status()

                duration_ms = (time.time() - start_time) * 1000

                result = response.json()
                response_text = result.get("response", "")
                endpoint = "/api/generate"
                status_code = response.status_code
            except requests.exceptions.Timeout as e:
                self._emit_ingestion_llm_call(
                    operation="ie_text", endpoint="/api/generate", model=self.text_model,
                    batch_size=batch_size, prompt_chars=len(combined_prompt),
                    duration_ms=(time.time() - start_time) * 1000, success=False,
                    temperature=ie_temp, timeout=self.timeout_sec, error=e,
                )
                raise RuntimeError(f"Generate API timeout after {self.timeout_sec}s")
            except requests.exceptions.HTTPError as e:
                self._emit_ingestion_llm_call(
                    operation="ie_text", endpoint="/api/generate", model=self.text_model,
                    batch_size=batch_size, prompt_chars=len(combined_prompt),
                    duration_ms=(time.time() - start_time) * 1000, success=False,
                    status_code=getattr(getattr(e, "response", None), "status_code", None),
                    temperature=ie_temp, timeout=self.timeout_sec, error=e,
                )
                raise RuntimeError(f"Generate API error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                self._emit_ingestion_llm_call(
                    operation="ie_text", endpoint="/api/generate", model=self.text_model,
                    batch_size=batch_size, prompt_chars=len(combined_prompt),
                    duration_ms=(time.time() - start_time) * 1000, success=False,
                    temperature=ie_temp, timeout=self.timeout_sec, error=e,
                )
                raise RuntimeError(f"Generate API failed: {e}")

        try:
            ie_results = self._extract_json_from_response(response_text, operation="ie_text")
            if not ie_results:
                raise ValueError("No JSON found in response")
        except Exception as e:
            error_msg = f"Failed to extract JSON from LLM response: {e}"
            self._log_llm_event(
                call_id,
                "JSON extraction failed",
                operation="ie_text",
                model_name=self.text_model,
                response_length=len(response_text),
                error=error_msg
            )
            raise RuntimeError(error_msg)

        # For generate API, we expect exact matches (no padding needed as it's more reliable)
        if len(ie_results) != len(blocks):
            error_msg = f"Generate API: Expected {len(blocks)} results, got {len(ie_results)}"
            self._log_llm_event(
                call_id,
                "Result count mismatch",
                operation="ie_text",
                model_name=self.text_model,
                expected_count=len(blocks),
                actual_count=len(ie_results),
                error=error_msg
            )
            raise RuntimeError(error_msg)

        self._log_llm_event(
            call_id,
            "LLM call completed successfully (Generate API)",
            operation="ie_text",
            model_name=self.text_model,
            result_count=len(ie_results),
            duration_ms=round(duration_ms, 1),
            api_used='generate',
            kv_cache_optimized=False
        )

        self._emit_ingestion_llm_call(
            operation="ie_text",
            endpoint=endpoint,
            model=self.text_model,
            batch_size=batch_size,
            prompt_chars=len(combined_prompt),
            response_chars=len(response_text),
            duration_ms=duration_ms,
            success=True,
            status_code=status_code,
            parse_success=True,
            parsed_items_count=len(ie_results),
            temperature=ie_temp,
            timeout=self.timeout_sec,
        )

        return ie_results

    def _calculate_performance_gain(self, actual_duration_ms: float, total_chars: int) -> float:

        """Estimate performance gain from KV cache (rough calculation)."""

        # Rough estimate: without KV cache, we'd process all tokens each time

        # With KV cache, system prompt tokens are cached

        estimated_full_tokens = total_chars // 4

        # Assume system prompt is ~70% of total (conservative estimate)

        system_tokens = int(estimated_full_tokens * 0.7)

        # Rough estimate: 10ms per 1000 tokens without cache, 2ms with cache

        estimated_without_cache = (estimated_full_tokens / 1000) * 10

        estimated_with_cache = ((estimated_full_tokens - system_tokens) / 1000) * 10 + (system_tokens / 1000) * 2

        if estimated_without_cache > 0:

            gain = (estimated_without_cache - estimated_with_cache) / estimated_without_cache * 100

            return round(gain, 1)

        return 0.0

        # LEGACY METHOD REMOVED - always use optimized version

        raise NotImplementedError('Legacy mode has been removed. Use optimized version.')

    def _extract_json_array(self, text: str) -> list[dict] | None:

        """Extract JSON array [...] from text."""

        json_start = text.find("[")

        json_end = text.rfind("]") + 1

        if json_start >= 0 and json_end > json_start:

            try:

                return json.loads(text[json_start:json_end])

            except json.JSONDecodeError:

                pass

        return None

    

    def _extract_single_json_from_response(self, response_text: str, operation: str = "ie_vision") -> dict | None:
        """
        Extract single JSON object from vision LLM response.

        Vision models return a single JSON object (not array) for individual images.
        """
        import json
        import re

        if not response_text or not response_text.strip():
            return None

        # Clean the response
        response_text = response_text.strip()

        # Strategy 1: Parse entire response as JSON (preferred for JSON-only prompts)
        try:
            result = json.loads(response_text)
            # Defensive handling if model accidentally returns an array
            if isinstance(result, list) and len(result) > 0:
                return result[0]
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Strategy 2: Find JSON in markdown code blocks (legacy prompts)
        json_pattern = r'```(?:json)?\s*\n?([\{\[].*?[\}\]])\s*\n?```'
        match = re.search(json_pattern, response_text, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                result = json.loads(match.group(1).strip())
                # If we got an array, extract the first element (defensive handling)
                if isinstance(result, list) and len(result) > 0:
                    return result[0]
                return result
            except json.JSONDecodeError:
                pass

        # Strategy 3: Try a best-effort "outermost braces" extraction
        # (prevents accidentally parsing inner objects like {"type": "..."} from within the response)
        lbrace = response_text.find("{")
        rbrace = response_text.rfind("}")
        if lbrace != -1 and rbrace != -1 and rbrace > lbrace:
            candidate = response_text[lbrace : rbrace + 1].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        # Strategy 4: Look for JSON-like content and clean it up
        # Remove common prefixes/suffixes that LLMs add
        cleaned = re.sub(r'^(Here is|Here\'s|The|Analysis:|Result:)\s*', '', response_text, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*(Please|Note|This).*?$', '', cleaned, flags=re.IGNORECASE | re.MULTILINE)

        try:
            return json.loads(cleaned.strip())
        except json.JSONDecodeError:
            return None

    def _extract_json_from_response(self, response_text: str, operation: str = "ie") -> list[dict] | None:

        """

        Extract JSON from LLM response that may contain markdown code blocks or extra text.

        

        Tries multiple strategies:

        1. Find JSON in ```json code blocks

        2. Find raw JSON array [...]

        3. Parse entire response as JSON

        

        Args:

            response_text: Raw LLM response text

            operation: Operation name for logging

            

        Returns:

            Parsed JSON array or None if extraction fails

        """

        if not response_text or not response_text.strip():

            return None

        

        strategies = [

            ("markdown_code_block", lambda text: self._extract_from_markdown(text)),

            ("json_array_search", lambda text: self._extract_json_array(text)),

            ("direct_parse", lambda text: json.loads(text))

        ]

        

        for strategy_name, strategy_fn in strategies:

            try:

                result = strategy_fn(response_text)

                if result and isinstance(result, list):

                    self.logger.info(

                        f"JSON extraction successful using {strategy_name}",

                        operation=operation,

                        strategy=strategy_name,

                        result_count=len(result)

                    )

                    return result

            except (json.JSONDecodeError, AttributeError, TypeError) as e:

                # Log failed strategy but continue to next one

                self.logger.debug(

                    f"JSON extraction strategy {strategy_name} failed",

                    operation=operation,

                    strategy=strategy_name,

                    error=str(e)[:100]

                )

                continue

        

        # All strategies failed

        self.logger.warning(

            "All JSON extraction strategies failed",

            operation=operation,

            response_preview=response_text[:500]

        )

        return None

    def ie_image_analysis(
        self, items: list[dict[str, Any]], *, ontology: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:

        """

        Perform information extraction on images with KV cache optimization.

        Args:

            items: List of dicts with keys: image_bytes, domain, context_text

        Returns:

            List of IE result dictionaries

        """

        if not items:
            return []
        if ontology is None:
            raise RuntimeError("Vision IE requires the frozen runtime ontology")

        # Vision calls use the same authoritative ontology as text extraction.
        return self._ie_image_analysis_optimized(items, ontology=ontology)

    def _post_process_vision_ie(
        self, llm_result: dict, domain: str, ontology: dict[str, Any]
    ) -> dict:
        """
        Post-process LLM vision IE result to ensure all required fields exist.
        
        Response flow:
        - description (string) + content_details (fields, knowledge_items, diagram_type) -> semantic chunks (Weaviate)
        - entities (array) + relationships (array) -> validated KG nodes/edges (Neo4j)
        
        Validates against the frozen customer + canonical + industry runtime ontology.
        """
        # Extract from simplified structure
        description = llm_result.get("description", "")
        entities = llm_result.get("entities", [])
        relationships = llm_result.get("relationships", [])
        if not isinstance(entities, list):
            entities = []
        if not isinstance(relationships, list):
            relationships = []
        entities = [e for e in entities if isinstance(e, dict)]
        relationships = [r for r in relationships if isinstance(r, dict)]
        content_details = llm_result.get("content_details", {})
        
        # Flatten content_details
        extracted_fields = content_details.get("fields", [])
        extracted_knowledge = content_details.get("knowledge_items", [])
        diagram_type = content_details.get("diagram_type", "process_flow")
        
        # Split compound Actor values the Vision LM sometimes returns as a single string,
        # e.g. "Relationship Manager / Branch Staff" or "Approver, Credit Team".
        # Each logical role should be its own graph node so Neo4j edges are meaningful.
        _compound_sep = re.compile(r"\s*/\s*|\s*,\s*|\s+and\s+", re.IGNORECASE)
        expanded: list[dict] = []
        for _e in entities:
            if _e.get("type") == "Actor":
                _parts = [p.strip() for p in _compound_sep.split(_e.get("value", "")) if p.strip()]
                if len(_parts) > 1:
                    for _p in _parts:
                        expanded.append({**_e, "value": _p})
                    print(
                        f"[LLM][vision_post_process] Split compound Actor "
                        f'"{_e.get("value")}" → {_parts}',
                        flush=True,
                    )
                else:
                    expanded.append(_e)
            else:
                expanded.append(_e)
        entities = expanded

        # ENHANCED: Extract entities and relationships from table rows
        if diagram_type == "table" and extracted_fields:
            table_entities, table_relationships = self._extract_from_table_rows(extracted_fields, entities, relationships)
            entities.extend(table_entities)
            relationships.extend(table_relationships)
        
        # Infer missing metadata from diagram_type
        block_category = self._infer_block_category(diagram_type, ontology)
        category = next(
            (item for item in ontology.get("block_categories", []) or []
             if isinstance(item, dict) and item.get("name") == block_category),
            {},
        )
        kg_node_type = category.get("maps_to_node", "_SKIP_KG")
        
        # CREATE SEMANTIC CHUNKS from description + content_details (-> Weaviate)
        # description + fields + knowledge_items become searchable chunks
        semantic_chunks = []
        
        # 1. Always create description chunk (primary semantic representation)
        if description:
            semantic_chunks.append({
                "chunk_type": "image_description",
                "text": description,
                "entities_referenced": [e.get("value", "") for e in entities if e.get("value")],
                "confidence": 0.95,
                "business_impact": "medium"
            })
        
        # 2. Aggregate knowledge items into higher-level narrative chunks
        if extracted_knowledge:
            chunk_text, chunk_type, entity_refs = self._aggregate_knowledge_items(
                extracted_knowledge, entities, diagram_type
            )
            if chunk_text:
                semantic_chunks.append({
                    "chunk_type": chunk_type,
                    "text": chunk_text,
                    "entities_referenced": entity_refs,
                    "confidence": 0.90,
                    "business_impact": "high"
                })
        
        # 3. For forms with many fields, create a structured field summary chunk
        if extracted_fields and len(extracted_fields) >= 10:
            field_summary = self._create_field_summary_chunk(extracted_fields, domain)
            if field_summary:
                semantic_chunks.append(field_summary)

        # 4. workflow_narrative — a single cohesive story chunk for process flow diagrams.
        # Combines the LLM description with the complete entity inventory (steps, gateways, actors)
        # so a retrieval query like "describe the BB loan end-to-end process" gets one rich chunk
        # rather than having to assemble across multiple narrow chunks.
        # Stored separately from image_description so both remain independently retrievable.
        if diagram_type == "process_flow" and description:
            process_step_vals = [e.get("value", "") for e in entities if e.get("type") == "Process" and e.get("value")]
            decision_vals     = [e.get("value", "") for e in entities if e.get("type") == "Decision" and e.get("value")]
            actor_vals        = [e.get("value", "") for e in entities if e.get("type") == "Actor"    and e.get("value")]

            narrative_parts = [description]
            if process_step_vals:
                narrative_parts.append(
                    f"Activity steps ({len(process_step_vals)}): {', '.join(process_step_vals)}."
                )
            if decision_vals:
                narrative_parts.append(
                    f"Gateway/decision points: {', '.join(decision_vals)}."
                )
            if actor_vals:
                narrative_parts.append(
                    f"Process actors/roles: {', '.join(actor_vals)}."
                )

            workflow_narrative_text = " ".join(narrative_parts)
            semantic_chunks.append({
                "chunk_type": "workflow_narrative",
                "text": workflow_narrative_text,
                "entities_referenced": [e.get("value", "") for e in entities if e.get("value")],
                "confidence": 0.92,
                "business_impact": "high",
            })
            print(
                f"[LLM][vision_post_process] workflow_narrative created: "
                f"{len(workflow_narrative_text)} chars, "
                f"{len(process_step_vals)} steps, {len(decision_vals)} gateways, {len(actor_vals)} actors",
                flush=True,
            )

        # Validate entities and relationships against vision ontology (non-blocking: log failures, keep data)
        validation_warnings = []
        try:
            entities, entity_warnings = self._validate_entity_types(entities, ontology)
            relationships, rel_warnings = self._validate_relationship_types(relationships, ontology, entities)
            validation_warnings = entity_warnings + rel_warnings
            for w in validation_warnings:
                self.logger.warning("Vision IE validation", message=w)
                print(f"[LLM][vision_post_process] Validation: {w}", flush=True)
        except Exception as val_err:
            self.logger.warning("Vision IE validation failed (keeping unvalidated data)", error=str(val_err), exc_info=True)
            print(f"[LLM][vision_post_process] Validation failed (keeping data): {val_err}", flush=True)
            validation_warnings.append(f"Validation error: {val_err}")
        
        # Return complete structure
        result = {
            "description": description,
            "block_category": block_category,
            "kg_node_type": kg_node_type,
            "entities": entities,
            "relationships": relationships,
            "extracted_fields": extracted_fields,
            "extracted_knowledge": extracted_knowledge,
            "semantic_chunks": semantic_chunks,
            "api_method": llm_result.get("api_method", "generate"),
            "vision_model_used": llm_result.get("vision_model_used"),
        }
        if validation_warnings:
            result["validation_warnings"] = validation_warnings
        return result
    
    def _infer_block_category(self, diagram_type: str, ontology: dict[str, Any]) -> str:
        """Map visual format to an available category without assuming a domain type."""
        categories = [
            item.get("name") for item in ontology.get("block_categories", []) or []
            if isinstance(item, dict) and item.get("name")
        ]
        preferred = "FormCapture" if diagram_type == "form" else "Diagram"
        if preferred in categories:
            return preferred
        return "Narrative" if "Narrative" in categories else (categories[0] if categories else "")

    def _aggregate_knowledge_items(self, knowledge_items: list, entities: list, diagram_type: str) -> tuple[str, str, list]:
        """
        Aggregate knowledge items into a higher-level narrative chunk.
        
        Returns: (chunk_text, chunk_type, entity_references)
        """
        if not knowledge_items:
            return "", "business_process", []
        
        # Group knowledge items by type
        process_steps = [k for k in knowledge_items if k.get("type") == "process_step"]
        business_rules = [k for k in knowledge_items if k.get("type") == "business_rule"]
        regulatory_reqs = [k for k in knowledge_items if k.get("type") == "regulatory_requirement"]
        
        # Determine chunk type based on content
        if len(process_steps) >= 3:
            chunk_type = "process_flow"
            # Create narrative from process steps — up to 20 steps (covers tiled images)
            step_texts = [k.get("content", "") for k in process_steps[:20]]
            chunk_text = "Process flow: " + " → ".join(step_texts)
        elif len(regulatory_reqs) >= 2:
            chunk_type = "regulatory_requirement"
            req_texts = [k.get("content", "") for k in regulatory_reqs]
            chunk_text = "Regulatory requirements: " + "; ".join(req_texts)
        elif len(business_rules) >= 2:
            chunk_type = "business_process"
            rule_texts = [k.get("content", "") for k in business_rules]
            chunk_text = "Business rules: " + "; ".join(rule_texts)
        else:
            # Generic aggregation
            chunk_type = "business_process"
            all_texts = [k.get("content", "") for k in knowledge_items[:5]]
            chunk_text = "; ".join(all_texts)
        
        # Collect all entity references from knowledge items
        entity_refs = []
        for k in knowledge_items:
            refs = k.get("entities_referenced", [])
            entity_refs.extend(refs)
        entity_refs = list(set(entity_refs))  # Deduplicate
        
        return chunk_text, chunk_type, entity_refs
    
    def _create_field_summary_chunk(self, fields: list, domain: str) -> dict:
        """Create a semantic chunk summarizing form fields."""
        if not fields:
            return {}
        
        # Group fields by type
        field_types = {}
        for f in fields:
            ftype = f.get("field_type", "text")
            if ftype not in field_types:
                field_types[ftype] = []
            field_types[ftype].append(f.get("field_name", ""))
        
        # Create summary text
        summary_parts = [f"Form contains {len(fields)} fields"]
        for ftype, fnames in field_types.items():
            summary_parts.append(f"{len(fnames)} {ftype} fields: {', '.join(fnames[:5])}")
        
        return {
            "chunk_type": "business_process",
            "text": "; ".join(summary_parts),
            "entities_referenced": [],
            "confidence": 0.85,
            "business_impact": "medium"
        }
    
    def _validate_entity_types(self, entities: list, ontology: dict) -> tuple[list, list[str]]:
        """Map explicitly configured vision aliases and reject unsupported entity types."""
        valid_types = {
            item["name"] for item in ontology.get("node_types", []) or []
            if isinstance(item, dict) and item.get("name")
        }
        aliases = (ontology.get("vision_ie_type_mapping") or {}).get("entity_types") or {}
        validated = []
        warnings = []
        for entity in entities:
            entity_type = str(entity.get("type") or "")
            mapped_type = aliases.get(entity_type, entity_type)
            if mapped_type in valid_types:
                entity["type"] = mapped_type
                validated.append(entity)
            else:
                warnings.append(
                    f"Rejected unsupported entity type {entity_type!r}: {entity.get('value', '')}"
                )
        return validated, warnings

    def _fuzzy_map_entity_type(self, entity_type: str, valid_types: set) -> str:
        """Attempt to map an invalid entity type to a valid one using fuzzy matching."""
        entity_type_lower = entity_type.lower()
        
        # Common mappings
        mappings = {
            "party": "Party",
            "organization": "Party",
            "instrument": "InstrumentDefinition",
            "document": "InstrumentDefinition",
            "lc": "InstrumentDefinition",
            "process": "Process",
            "step": "ProcessStep",
            "system": "System",
            "component": "Component"
        }
        
        # Check direct mapping
        for key, value in mappings.items():
            if key in entity_type_lower and value in valid_types:
                return value
        
        # Check if any valid type is substring
        for vtype in valid_types:
            if vtype.lower() in entity_type_lower or entity_type_lower in vtype.lower():
                return vtype
        
        return ""
    
    def _validate_relationship_types(self, relationships: list, ontology: dict, entities: list) -> tuple[list, list[str]]:
        """Map configured aliases and retain only exact ontology-allowed endpoint triples."""
        aliases = (ontology.get("vision_ie_type_mapping") or {}).get("relationship_types") or {}
        allowed: set[tuple[str, str, str]] = set()
        for item in ontology.get("relationship_types", []) or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            triples = item.get("allowed_triples") or []
            if triples:
                for triple in triples:
                    if isinstance(triple, dict):
                        allowed.add((str(triple.get("from") or ""), name, str(triple.get("to") or "")))
            elif item.get("from") and item.get("to"):
                allowed.add((str(item["from"]), name, str(item["to"])))
        entity_types = {
            str(entity.get("value", "")).strip().casefold(): str(entity.get("type", ""))
            for entity in entities if entity.get("value")
        }
        entity_types.update({
            str(entity.get("value", "")).strip().casefold(): str(entity.get("type", ""))
            for entity in entities if entity.get("value")
        })
        entity_values = {
            str(entity.get("value", "")).strip().casefold(): str(entity.get("value", ""))
            for entity in entities if entity.get("value")
        }
        validated = []
        warnings = []
        for rel in relationships:
            rel_type = aliases.get(str(rel.get("rel_type") or ""), str(rel.get("rel_type") or ""))
            from_value = str(rel.get("from_value") or "")
            to_value = str(rel.get("to_value") or "")
            from_key, to_key = from_value.strip().casefold(), to_value.strip().casefold()
            from_type = str(rel.get("from_type") or entity_types.get(from_key) or "")
            to_type = str(rel.get("to_type") or entity_types.get(to_key) or "")
            # Apply configured endpoint aliases before testing the exact triple.
            entity_aliases = (ontology.get("vision_ie_type_mapping") or {}).get("entity_types") or {}
            from_type = entity_aliases.get(from_type, from_type)
            to_type = entity_aliases.get(to_type, to_type)
            rel["rel_type"] = rel_type
            rel["from_type"] = from_type
            rel["to_type"] = to_type
            if from_key in entity_values:
                rel["from_value"] = entity_values[from_key]
            if to_key in entity_values:
                rel["to_value"] = entity_values[to_key]
            if (from_type, rel_type, to_type) in allowed and from_key in entity_values and to_key in entity_values:
                validated.append(rel)
            else:
                warnings.append(
                    f"Rejected unsupported relationship triple {from_type}-[{rel_type}]->{to_type}: "
                    f"{from_value!r} -> {to_value!r}"
                )
        return validated, warnings

    def _fuzzy_map_relationship_type(self, rel_type: str, valid_types: set) -> str:
        """Attempt to map an invalid relationship type to a valid one."""
        rel_type_lower = rel_type.lower()
        
        # Common mappings
        mappings = {
            "next": "NEXT_STEP",
            "flows": "FLOWS_TO",
            "involves": "PARTY_INVOLVED",
            "uses": "USES_INSTRUMENT",
            "handles": "PROCESS_HANDLES",
            "depends": "DEPENDS_ON",
            "triggers": "TRIGGERS"
        }
        
        # Check direct mapping
        for key, value in mappings.items():
            if key in rel_type_lower and value in valid_types:
                return value
        
        # Check if any valid type is substring
        for vtype in valid_types:
            if vtype.lower() in rel_type_lower or rel_type_lower in vtype.lower():
                return vtype
        
        return ""
    
    def _extract_from_table_rows(self, extracted_fields: list, existing_entities: list, existing_relationships: list) -> tuple[list, list]:
        """
        Extract entities and relationships from table rows (Actor-Comment-Control tables).
        
        Args:
            extracted_fields: List of field dicts from vision IE (may include table_row fields)
            existing_entities: Already extracted entities
            existing_relationships: Already extracted relationships
        
        Returns:
            Tuple of (additional_entities, additional_relationships)
        """
        additional_entities = []
        additional_relationships = []
        existing_entity_values = {e.get("value", "").upper() for e in existing_entities}
        existing_rel_set = {(r.get("rel_type"), r.get("from_value", ""), r.get("to_value", "")) for r in existing_relationships}
        
        # Process table rows
        for field in extracted_fields:
            if not isinstance(field, dict):
                continue
            
            field_type = field.get("field_type", "")
            if field_type != "table_row":
                continue
            
            # Extract table_columns mapping
            table_columns = field.get("table_columns", {})
            if not isinstance(table_columns, dict):
                continue
            
            # Extract Actor
            actor_value = table_columns.get("Actor", "").strip()
            if actor_value and actor_value.upper() not in existing_entity_values:
                # Normalize actor abbreviations
                actor_normalized = self._normalize_actor_name(actor_value)
                if actor_normalized:
                    additional_entities.append({
                        "type": "Actor",
                        "value": actor_normalized,
                        "confidence": 0.95
                    })
                    existing_entity_values.add(actor_normalized.upper())
            
            # Extract Control as Activity (vision ontology has Activity, not Control)
            control_value = table_columns.get("Controls", "").strip()
            if control_value and control_value.upper() not in existing_entity_values:
                # Extract control name (first sentence or key phrase)
                control_name = self._extract_control_name(control_value)
                if control_name:
                    additional_entities.append({
                        "type": "Activity",
                        "value": control_name,
                        "confidence": 0.90
                    })
                    existing_entity_values.add(control_name.upper())
            
            # Extract Process/Activity from Comments
            comments_value = table_columns.get("Comments", "").strip()
            if comments_value:
                # Extract process/activity mentions
                process_mentions = self._extract_process_mentions(comments_value)
                for process_name in process_mentions:
                    if process_name.upper() not in existing_entity_values:
                        additional_entities.append({
                            "type": "Process",
                            "value": process_name,
                            "confidence": 0.85
                        })
                        existing_entity_values.add(process_name.upper())
                        
                        # Create ACTED_BY relationship if Actor exists
                        if actor_value:
                            actor_normalized = self._normalize_actor_name(actor_value)
                            if actor_normalized:
                                rel_key = ("ACTED_BY", process_name, actor_normalized)
                                if rel_key not in existing_rel_set:
                                    additional_relationships.append({
                                        "from_type": "Process",
                                        "from_value": process_name,
                                        "rel_type": "ACTED_BY",
                                        "to_type": "Actor",
                                        "to_value": actor_normalized,
                                        "confidence": 0.85
                                    })
                                    existing_rel_set.add(rel_key)
                        
                        # Create ACTED_BY relationship if Control (Activity) exists (vision ontology: Activity ACTED_BY Actor)
                        if control_value and actor_value:
                            control_name = self._extract_control_name(control_value)
                            actor_normalized = self._normalize_actor_name(actor_value)
                            if control_name and actor_normalized:
                                rel_key = ("ACTED_BY", control_name, actor_normalized)
                                if rel_key not in existing_rel_set:
                                    additional_relationships.append({
                                        "from_type": "Activity",
                                        "from_value": control_name,
                                        "rel_type": "ACTED_BY",
                                        "to_type": "Actor",
                                        "to_value": actor_normalized,
                                        "confidence": 0.80
                                    })
                                    existing_rel_set.add(rel_key)
        
        return additional_entities, additional_relationships
    
    def _normalize_actor_name(self, actor_value: str) -> str:
        """Normalize actor abbreviations to full names."""
        actor_upper = actor_value.upper().strip()
        mapping = {
            "RM": "Relationship Manager",
            "TR": "Trader",
            "AVQ": "Avaloq",
            "BO": "Back Office",
            "CO": "Compliance Office"
        }
        return mapping.get(actor_upper, actor_value.strip())
    
    def _extract_control_name(self, control_text: str) -> str:
        """Extract control name from control text (first key phrase or report name)."""
        # Look for report names in quotes or specific patterns
        import re
        # Pattern: 'All Stex Trx grouped by Status & Asset for RM'
        quote_match = re.search(r"'([^']+)'", control_text)
        if quote_match:
            return quote_match.group(1).strip()
        
        # Pattern: "uses 'Report Name'"
        uses_match = re.search(r"uses\s+['\"]([^'\"]+)['\"]", control_text, re.IGNORECASE)
        if uses_match:
            return uses_match.group(1).strip()
        
        # First sentence up to 50 chars
        first_sentence = control_text.split('.')[0].strip()
        if len(first_sentence) <= 50:
            return first_sentence
        
        # First 50 chars
        return control_text[:50].strip()
    
    def _extract_process_mentions(self, comments_text: str) -> list[str]:
        """Extract process/activity mentions from comments text."""
        import re
        processes = []
        
        # Common process patterns
        process_patterns = [
            r"(\w+\s+(?:order|trade|execution|verification|settlement|approval))",
            r"(order\s+\w+)",
            r"(trade\s+\w+)",
            r"(\w+\s+process)",
            r"(\w+\s+workflow)",
        ]
        
        for pattern in process_patterns:
            matches = re.findall(pattern, comments_text, re.IGNORECASE)
            for match in matches:
                process_name = match.strip()
                if len(process_name) <= 50:  # Reasonable length
                    processes.append(process_name)
        
        # If no patterns found, try to extract key activities
        if not processes:
            # Look for action verbs + objects
            action_patterns = [
                r"(fills?\s+in\s+\w+)",
                r"(puts?\s+\w+\s+on\s+hold)",
                r"(selects?\s+\w+)",
                r"(verifies?\s+\w+)",
                r"(rejects?\s+\w+)",
            ]
            for pattern in action_patterns:
                matches = re.findall(pattern, comments_text, re.IGNORECASE)
                for match in matches:
                    activity_name = match.strip().title()
                    if len(activity_name) <= 50:
                        processes.append(activity_name)
        
        return list(set(processes))  # Deduplicate
    
    def _ie_image_analysis_optimized(
        self, items: list[dict[str, Any]], *, ontology: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Optimized vision IE using /api/generate endpoint for multimodal input.
        Supports Qwen2-VL model with full ontology context and OCR enhancement.
        """
        import json
        import time
        from datetime import datetime, timezone
        import requests

        try:
            from kbfabric_ingest.llm.prompts import get_system_prompt
        except ImportError as e:
            raise RuntimeError(f"Failed to import prompt functions: {e}")

        from kbfabric_ingest.ingest.ontology_prompt import build_full_ontology_prompt

        if not items:
            return []

        # Smoke test: minimal payload (matches working curl) to verify API before full IE
        first_with_image = next((i for i in items if i.get("image_bytes") and len(i.get("image_bytes", b"")) >= 1024), None)
        if first_with_image:
            try:
                b64 = self._encode_image_to_base64(first_with_image["image_bytes"])
                r = self.session.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.vision_model, "prompt": "Describe this image.", "images": [b64], "stream": False},
                    timeout=30
                )
                if r.status_code == 200:
                    print(f"[LLM][ie_vision] Smoke test OK (200) - proceeding with vision IE", flush=True)
                else:
                    print(f"[LLM][ie_vision] Smoke test returned {r.status_code} - API may have issues", flush=True)
            except Exception as e:
                print(f"[LLM][ie_vision] Smoke test failed: {e}", flush=True)

        print(f"[LLM][ie_vision] Processing {len(items)} images with vision ontology...", flush=True)

        # Get minimal vision ontology context (6 node types, 5 rel types vs 31/30 in full ontology)
        ontology_context = build_full_ontology_prompt(ontology)
        # Vision prompt budget (approx 4 chars per token; keep buffer)
        app_config = get_app_config()
        vision_cfg = app_config.get("ollama", {}).get("vision", {})
        max_tokens = vision_cfg.get("max_context_tokens", 131072)
        max_prompt_chars = int(max_tokens * 4 * (1 - (self.token_buffer_percent / 100.0)))
        # Conservative hard cap to prevent prompt-too-long errors
        max_prompt_chars = min(max_prompt_chars, 20000)

        results = []

        for item_idx, item in enumerate(items):
            try:
                call_id = self._get_call_id()

                # Extract item data
                image_bytes = item.get("image_bytes")
                domain = item.get("domain", "general")
                context_text = item.get("context_text", "")

                print(f"[LLM][ie_vision] Processing item {item_idx + 1}: domain={domain}, image_size={len(image_bytes) if image_bytes else 0} bytes, context_len={len(context_text)}", flush=True)

                if not image_bytes:
                    print(f"[LLM][ie_vision] Skipping item {item_idx + 1}: no image data", flush=True)
                    results.append({
                        "description": "No image data available",
                        "block_category": "Diagram",
                        "kg_node_type": "None",
                        "entities": [],
                        "relationships": [],
                        "extracted_fields": [],
                        "extracted_knowledge": [],
                        "semantic_chunks": [],
                        "vision_model_used": None
                    })
                    continue

                if len(image_bytes) < 1024:
                    print(f"[LLM][ie_vision] Skipping item {item_idx + 1}: image too small ({len(image_bytes)} bytes < 1KB, likely invalid)", flush=True)
                    results.append({
                        "description": "Image too small or invalid (< 1KB)",
                        "block_category": "Diagram",
                        "kg_node_type": "None",
                        "entities": [],
                        "relationships": [],
                        "extracted_fields": [],
                        "extracted_knowledge": [],
                        "semantic_chunks": [],
                        "vision_model_used": None
                    })
                    continue

                # Trim context to stay within prompt budget
                from kbfabric_ingest.llm.prompts import build_vision_user_content
                system_prompt_template = get_system_prompt('ie_vision')
                ontology_context_trim = ontology_context
                context_text_trim = context_text
                system_prompt = system_prompt_template.format(
                    domain=domain,
                    ontology_context=ontology_context_trim
                )
                user_content = build_vision_user_content([{
                    "domain": domain,
                    "context_text": context_text_trim
                }])
                if len(system_prompt) + len(user_content) > max_prompt_chars:
                    overflow = (len(system_prompt) + len(user_content)) - max_prompt_chars
                    # Trim context_text first
                    if context_text_trim:
                        context_text_trim = context_text_trim[:max(0, len(context_text_trim) - overflow)]
                    # Rebuild prompts after context trim
                    system_prompt = system_prompt_template.format(
                        domain=domain,
                        ontology_context=ontology_context_trim
                    )
                    user_content = build_vision_user_content([{
                        "domain": domain,
                        "context_text": context_text_trim
                    }])
                    if len(system_prompt) + len(user_content) > max_prompt_chars:
                        overflow = (len(system_prompt) + len(user_content)) - max_prompt_chars
                        if ontology_context_trim:
                            ontology_context_trim = ontology_context_trim[:max(0, len(ontology_context_trim) - overflow)]
                        system_prompt = system_prompt_template.format(
                            domain=domain,
                            ontology_context=ontology_context_trim
                        )
                        user_content = build_vision_user_content([{
                            "domain": domain,
                            "context_text": context_text_trim
                        }])
                # Enforce strict JSON output (no prose)
                system_prompt = system_prompt + "\nReturn ONLY a single JSON object. No prose."

                # Resize image to vision encoder's native patch budget before encoding.
                # Oversized images are silently downsampled internally by the vision encoder,
                # making text in small boxes unreadable. Resizing here gives the model the
                # image at its optimal resolution (no internal downsampling needed).
                vision_max_px = vision_cfg.get("max_image_pixels", 2_890_138)
                image_bytes = self._resize_image_for_vision(image_bytes, max_pixels=vision_max_px)

                # Prepare multimodal request
                base64_image = self._encode_image_to_base64(image_bytes)

                # Check image size and warn for very large images
                image_size_kb = len(image_bytes) / 1024
                if image_size_kb > 200:  # > 200KB
                    print(f"[LLM][ie_vision] WARNING: Large image ({image_size_kb:.1f}KB) - may timeout", flush=True)
                    self.logger.warning("Large image detected", size_kb=image_size_kb, item_index=item_idx + 1)
                
                # Log token usage (estimate: 1 token ≈ 4 chars for text, images consume fixed tokens)
                prompt_chars = len(system_prompt) + len(user_content)
                prompt_tokens_est = prompt_chars // 4
                # Gemma 3 12B uses ~1024 tokens per image (estimate based on standard vision transformers)
                image_tokens_est = 1024
                total_tokens_est = prompt_tokens_est + image_tokens_est
                context_utilization = (total_tokens_est / max_tokens) * 100
                print(f"[LLM][ie_vision] Token usage estimate: prompt={prompt_tokens_est} ({prompt_chars} chars), image={image_tokens_est}, total={total_tokens_est}/{max_tokens} ({context_utilization:.1f}%)", flush=True)
                self.logger.info(
                    "Vision IE token usage",
                    item_index=item_idx + 1,
                    prompt_chars=prompt_chars,
                    prompt_tokens_est=prompt_tokens_est,
                    image_tokens_est=image_tokens_est,
                    total_tokens_est=total_tokens_est,
                    max_tokens=max_tokens,
                    context_utilization_pct=round(context_utilization, 1)
                )
                
                # Call vision API with multimodal data (uses /api/generate only)
                print(f"[LLM][ie_vision] Calling vision API for item {item_idx + 1}...", flush=True)
                vision_model_used = self.vision_model
                try:
                    response, api_method, vision_model_used = self._call_generate_api_multimodal(
                        prompt=user_content,
                        system_prompt=system_prompt,
                        images=[base64_image],
                        model_name=self.vision_model,
                        call_id=call_id,
                        operation="ie_vision"
                    )
                except Exception as e:
                    err_lower = str(e).lower()
                    if "prompt too long" in err_lower or "limit is" in err_lower:
                        # Retry with a minimal prompt (no full ontology)
                        reduced_context = context_text_trim[:500]
                        system_prompt = (
                            "You are a vision information extraction engine. "
                            "Return ONLY a single JSON object with keys: "
                            "description, block_category, entities, relationships, "
                            "extracted_fields, extracted_knowledge, semantic_chunks."
                        )
                        user_content = build_vision_user_content([{
                            "domain": domain,
                            "context_text": reduced_context
                        }])
                        try:
                            response, api_method, vision_model_used = self._call_generate_api_multimodal(
                                prompt=user_content,
                                system_prompt=system_prompt,
                                images=[base64_image],
                                model_name=self.vision_model,
                                call_id=call_id,
                                operation="ie_vision"
                            )
                        except Exception:
                            self.logger.error(
                                "Vision prompt too long even after reduction",
                                domain=domain,
                                item_index=item_idx + 1
                            )
                            response = ""
                            api_method = "generate"
                    elif "500" in str(e):
                        # Fallback to vision_llm_fallback on 500 (same Ollama endpoint)
                        fallback_model = vision_cfg.get("vision_llm_fallback")
                        if fallback_model:
                            print(f"[LLM][ie_vision] Primary model returned 500, retrying with fallback: {fallback_model}", flush=True)
                            try:
                                response, api_method, vision_model_used = self._call_generate_api_multimodal(
                                    prompt=user_content,
                                    system_prompt=system_prompt,
                                    images=[base64_image],
                                    model_name=fallback_model,
                                    call_id=call_id,
                                    operation="ie_vision"
                                )
                                print(f"[LLM][ie_vision] Fallback model succeeded: {vision_model_used}", flush=True)
                            except Exception as fallback_err:
                                print(f"[LLM][ie_vision] Fallback model also failed: {fallback_err}", flush=True)
                                raise
                        else:
                            raise
                    else:
                        raise

                print(f"[LLM][ie_vision] Vision API response for item {item_idx + 1}: {len(response)} chars", flush=True)

                # Extract and validate JSON response
                ie_result = self._extract_single_json_from_response(response)
                if not ie_result:
                    print(f"[LLM][ie_vision] Failed to extract JSON from item {item_idx + 1}", flush=True)
                    print(f"[LLM][ie_vision] Raw response preview: {response[:200]}...", flush=True)
                    ie_result = {
                        "description": "Vision model response parsing failed - using fallback result",
                        "block_category": "Diagram",
                        "kg_node_type": "None",
                        "entities": [],
                        "relationships": [],
                        "api_method": api_method,
                        "vision_model_used": vision_model_used
                    }
                else:
                    # Validate with Pydantic schema
                    try:
                        validated = VisionIEResponse(**ie_result)
                        # Convert back to dict with validated data
                        ie_result = validated.model_dump()
                        ie_result["api_method"] = api_method
                        ie_result["vision_model_used"] = vision_model_used
                        print(f"[LLM][ie_vision] Pydantic validation passed for item {item_idx + 1}", flush=True)
                    except ValidationError as ve:
                        print(f"[LLM][ie_vision] Pydantic validation failed for item {item_idx + 1}: {ve}", flush=True)
                        self.logger.warning("Vision response validation failed", item_index=item_idx + 1, errors=str(ve))
                        # Keep original ie_result but log the issue
                        ie_result["api_method"] = api_method
                        ie_result["vision_model_used"] = vision_model_used
                        ie_result["validation_errors"] = str(ve)
                
                # Post-process the LLM response to ensure all required fields exist
                ie_result = self._post_process_vision_ie(ie_result, domain, ontology)

                # Ensure the response has a description field for ImageIEResult validation
                if "description" not in ie_result:
                    # Create a description from the extracted information
                    entities = ie_result.get("entities", [])
                    relationships = ie_result.get("relationships", [])
                    block_category = ie_result.get("block_category", "Narrative")

                    description_parts = []
                    if block_category != "Narrative":
                        description_parts.append(f"Diagram category: {block_category}")

                    if entities:
                        description_parts.append(f"Contains {len(entities)} entities")

                    if relationships:
                        description_parts.append(f"Contains {len(relationships)} relationships")

                    if not description_parts:
                        description_parts.append("Visual diagram content extracted")

                    ie_result["description"] = ". ".join(description_parts)

                results.append(ie_result)
                print(f"[LLM][ie_vision] Processed item {item_idx + 1}/{len(items)}: {len(ie_result.get('entities', []))} entities, {len(ie_result.get('relationships', []))} relationships", flush=True)

            except Exception as e:
                self.logger.error(
                    f"CRITICAL ERROR processing item {item_idx + 1}: {type(e).__name__}: {e}",
                    item_index=item_idx + 1,
                    total_items=len(items),
                    exc_info=True
                )
                print(f"[LLM][ie_vision] CRITICAL ERROR processing item {item_idx + 1}: {type(e).__name__}: {e}", flush=True)
                error_result = {
                    "description": f"Vision processing failed: {type(e).__name__}",
                    "block_category": "Diagram",
                    "kg_node_type": "None",
                    "entities": [],
                    "relationships": [],
                    "extracted_fields": [],
                    "extracted_knowledge": [],
                    "semantic_chunks": [],
                    "api_method": "error",  # API method unknown due to error
                    "vision_model_used": None,
                    "error": str(e),
                    "error_type": type(e).__name__
                }
                results.append(error_result)

        print(f"[LLM][ie_vision] Completed processing {len(results)}/{len(items)} images", flush=True)
        return results

    def _encode_image_to_base64(self, image_bytes: bytes) -> str:
        """Encode image bytes to base64 string for API calls."""
        import base64
        return base64.b64encode(image_bytes).decode('utf-8')

    def _resize_image_for_vision(self, image_bytes: bytes, max_pixels: int = 2_890_138) -> bytes:
        """
        Resize image so it fits within the vision encoder's native patch budget.

        Default max_pixels = 3,211,264 × 0.9 = 2,890,138 (~2.9M), matching
        Qwen3-VL's 16,384-patch limit (16384 × 14² px) with 10% headroom so
        Ollama's internal preprocessor has room to align patch boundaries.

        Images already within budget are returned unchanged (no-op).
        LANCZOS is used for downscaling because it preserves text/edge sharpness
        better than bilinear or bicubic — critical for process-diagram labels.

        For model switching, override via ollama.vision.max_image_pixels in kbfabric.yaml:
          Qwen3-VL 235B : 2890138  (~2.9M — default)
          Gemma3 12B    :  722534  (~0.72M)
        """
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        total_pixels = w * h

        if total_pixels <= max_pixels:
            return image_bytes  # already within budget — no-op

        scale = (max_pixels / total_pixels) ** 0.5
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        print(
            f"[LLM][ie_vision] Resized image for vision encoder: "
            f"({w}×{h}) {total_pixels / 1e6:.2f}M → ({new_w}×{new_h}) "
            f"{new_w * new_h / 1e6:.2f}M pixels (budget={max_pixels / 1e6:.2f}M)",
            flush=True
        )

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    def _call_generate_api_multimodal(
        self,
        prompt: str,
        system_prompt: str,
        images: list[str],
        model_name: str,
        call_id: str | None = None,
        operation: str = "ie_vision",
    ) -> tuple[str, str, str]:
        """
        Call vision API using /api/generate endpoint only (proven to work in standalone tests).
        Returns (response_text, api_method, model_used) so caller knows which model succeeded.
        
        Args:
            prompt: User prompt text
            system_prompt: System prompt text
            images: List of base64-encoded image strings
            model_name: Vision model name
        """
        import base64
        
        # Get configuration
        from kbfabric_ingest.ingest.config import get_app_config

        config = get_app_config()
        ollama_config = config.get("ollama", {})
        temp_config = ollama_config.get("temperature", {})
        ie_vision_temp = temp_config.get("ie_vision", 0.0)
        # Use vision processing configuration
        vision_config = ollama_config.get("vision", {})
        num_ctx = vision_config.get("num_ctx", 131072)
        # Use configurable timeout (default to ollama.timeout_sec or 300s)
        base_timeout = vision_config.get("timeout_sec", ollama_config.get("timeout_sec", 300))
        
        # Compute image sizes/hashes once (also used for raw I/O logging)
        image_infos: list[dict[str, Any]] = []
        total_image_bytes = 0
        for img_b64 in images or []:
            try:
                img_bytes = base64.b64decode(img_b64)
            except Exception:
                img_bytes = b""
            total_image_bytes += len(img_bytes)
            image_infos.append({
                "sha256": hashlib.sha256(img_bytes).hexdigest() if img_bytes else None,
                "size_bytes": len(img_bytes),
                "b64_len": len(img_b64) if img_b64 is not None else 0,
            })

        # Increase timeout for large images (estimate: 1KB = 1 second processing time)
        image_size_kb = (total_image_bytes / 1024) if images else 0
        vision_timeout = max(base_timeout, int(image_size_kb * 1.5))  # At least 1.5s per KB
        if vision_timeout > base_timeout:
            print(f"[LLM] Increased vision timeout to {vision_timeout}s for {image_size_kb:.1f}KB image", flush=True)

        start_time = time.time()

        # Combine system and user prompts (as done in standalone test)
        combined_prompt = f"{system_prompt}\n\n{prompt}"
        combined_prompt_len = len(combined_prompt)

        # Use /api/generate format (no format:"json" - can cause 500; options with 32k ctx)
        generate_payload = {
            "model": model_name,
            "prompt": combined_prompt,
            "images": images,
            "stream": False,
            "options": {
                "temperature": ie_vision_temp,
                "num_ctx": num_ctx
            }
        }

        try:
            if call_id is None:
                call_id = self._get_call_id()

            include_images_b64 = bool((ollama_config.get("logging", {}) or {}).get("llm_io_include_images_base64", False))
            payload_for_log = dict(generate_payload)
            if not include_images_b64:
                payload_for_log.pop("images", None)

            self._log_run_io(
                call_id=call_id,
                operation=operation,
                event="request",
                endpoint="/api/generate",
                model_name=model_name,
                timeout_sec=vision_timeout,
                prompt_length_chars=combined_prompt_len,
                max_context_tokens=num_ctx,
                images=image_infos,
                payload=payload_for_log,
            )

            response = self.session.post(
                f"{self.base_url}/api/generate",
                json=generate_payload,
                timeout=vision_timeout
            )

            if response.status_code == 200:
                result = response.json()
                duration_ms = (time.time() - start_time) * 1000
                print(f"[LLM] Vision API (/api/generate) success: {duration_ms:.1f}ms", flush=True)
                self._log_run_io(
                    call_id=call_id,
                    operation=operation,
                    event="response",
                    endpoint="/api/generate",
                    model_name=model_name,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 1),
                    raw_response_text=response.text,
                )
                self._emit_ingestion_llm_call(
                    operation=operation,
                    endpoint="/api/generate",
                    model=model_name,
                    batch_size=len(images or []),
                    prompt_chars=combined_prompt_len,
                    response_chars=len(response.text or ""),
                    duration_ms=duration_ms,
                    success=True,
                    status_code=response.status_code,
                    temperature=ie_vision_temp,
                    timeout=vision_timeout,
                )
                return result.get("response", ""), "generate", model_name
            else:
                raise Exception(f"API call failed: {response.status_code} - {response.text}")

        except Exception as e:
            print(f"[LLM] Vision API call failed: {e}", flush=True)
            self._log_run_io(
                call_id=call_id or self._get_call_id(),
                operation=operation,
                event="error",
                endpoint="/api/generate",
                model_name=model_name,
                error=str(e),
            )
            self._emit_ingestion_llm_call(
                operation=operation,
                endpoint="/api/generate",
                model=model_name,
                batch_size=len(images or []),
                prompt_chars=combined_prompt_len,
                duration_ms=(time.time() - start_time) * 1000,
                success=False,
                status_code=getattr(locals().get("response"), "status_code", None),
                temperature=ie_vision_temp,
                timeout=vision_timeout,
                error=e,
            )
            raise

    def embed_text(self, texts: list[str]) -> list[list[float]]:

        """

        Generate embeddings for texts using the configured offline model.

        

        Args:

            texts: List of text strings

            

        Returns:

            List of embedding vectors (768-dim each)

        """

        if not texts:

            return []

        

        print(f"[LLM][embed_text] Calling embedding encoder for {len(texts)} texts...", flush=True)

        result = self.embeddings_client.encode(texts)

        print(f"[LLM][embed_text] Embedding encoding complete: {len(result)} vectors", flush=True)

        return result


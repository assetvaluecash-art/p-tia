"""LLM client for Ollama API calls."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

from kbfabric_ingest._common.config import get_app_config
from kbfabric_ingest._common.telemetry import get_tracer, record_llm_call
from kbfabric_ingest.ingest.models import BlockCategory, get_block_category_to_node_mapping
from custom_ml.embeddings import EmbeddingsClient
from kbfabric_ingest._common.logger import get_logger, get_llm_text_logger, get_llm_visual_logger, get_llm_full_logger

logger = get_logger(__name__)
llm_text_logger = get_llm_text_logger(__name__)
llm_visual_logger = get_llm_visual_logger(__name__)

# Global counter for LLM call sequence IDs
_call_sequence = 0


class LLMClient:
    """Client for LLM operations via Ollama API."""

    def __init__(self, config: dict[str, Any]):
        """
        Initialize LLM client.
        
        Args:
            config: Ollama configuration dictionary
        """
        self.config = config
        self.base_url = config["base_url"]
        self.text_model = config["text_model"]
        self.vision_model = config["vision_model"]
        self.timeout_sec = config["timeout_sec"]

        # Token limits from ollama.text / ollama.vision (.env); client receives ollama section
        text_cfg = config["text"]
        vision_cfg = config["vision"]
        self.max_context_tokens = max(
            text_cfg["max_context_tokens"],
            vision_cfg["max_context_tokens"],
        )
        self.token_buffer_percent = config["token_buffer_percent"]
        
        self.logger = get_logger(__name__)
        self.llm_text_logger = get_llm_text_logger(__name__)
        self.llm_visual_logger = get_llm_visual_logger(__name__)
        self.llm_full_logger = get_llm_full_logger(__name__)
        
        # Initialize embeddings client
        app_config = get_app_config()
        self.embeddings_client = EmbeddingsClient(app_config)

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
        """Generate a unique call ID for this LLM request."""
        global _call_sequence
        _call_sequence += 1
        return f"call-{_call_sequence:06d}-{uuid.uuid4().hex[:8]}"

    def _log_llm_event(self, call_id: str, event: str, operation: str = "", **data: Any) -> None:
        """Write an LLM event to appropriate log file (text or visual) as structured data.
        
        Also writes to full logger for complete I/O logging.
        """
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
            if op == "ie_image" or "image" in op.lower():
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

    def ie_text_analysis(self, blocks: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """
        Perform information extraction on text blocks.
        
        Uses full ontology context (descriptions, constraints, examples) from
        enterprise_banking_ontology.yaml for ontology-driven IE without hardcoding.
        
        Args:
            blocks: List of (masked_text, domain) tuples
            
        Returns:
            List of IE result dictionaries
        """
        if not blocks:
            return []
        
        batch_size = len(blocks)
        print(f"[LLM][ie_text] Processing batch of {batch_size} blocks...", flush=True)
        
        # Get full ontology for building rich prompt
        from kbfabric_ingest._common.config import get_ontology
        from kbfabric_ingest.ingest.ontology_prompt import build_full_ontology_prompt, get_allowed_category_names, get_allowed_node_types, get_allowed_relationship_types
        
        ontology = get_ontology()
        
        # Build full ontology context section (descriptions, constraints, examples)
        ontology_context = build_full_ontology_prompt(ontology)
        
        # Get allowed values for validation
        allowed_categories = get_allowed_category_names(ontology)
        allowed_node_types = get_allowed_node_types(ontology)
        allowed_rel_types = get_allowed_relationship_types(ontology)
        
        # Get category to node mapping
        category_mapping = get_block_category_to_node_mapping()
        
        # Get few-shot examples from config (domain-specific or defaults)
        # This enables easy domain onboarding via config instead of code changes
        from kbfabric_ingest._common.config import get_ie_examples
        
        # Get domain from first block if available (all blocks in batch should be same domain)
        first_domain = blocks[0][1] if blocks else ProcessDiagram
        examples = get_ie_examples(first_domain)
        
        # Fallback to hardcoded defaults if config has no examples
        if not examples:
            examples = [
                {
                    "block_text": "Faster Payments: A near real time payment scheme for small value transfers",
                    "block_category": "Reference",
                    "kg_node_type": "Reference",
                    "entities": [{"type": "Term", "value": "Faster Payments", "confidence": 0.99}],
                    "relationships": []
                },
                {
                    "block_text": "The system must process payment requests within 2 seconds.",
                    "block_category": "Requirement",
                    "kg_node_type": "Requirement",
                    "entities": [{"type": "System", "value": "payment system", "confidence": 0.9}],
                    "relationships": []
                }
            ]
        
        examples_json = json.dumps(examples, indent=2)
        
        prompt = f"""You are an information extraction system for enterprise knowledge bases.

{ontology_context}

TASK: For each text block, classify it and extract entities/relationships using ONLY the ontology above.

=== ENTITY EXTRACTION SCOPE - READ CAREFULLY ===

WHAT TO EXTRACT:
- Named business concepts that are DISCRETE and IDENTIFIABLE
- Processes with clear names (e.g., "DriftDetection", "Rebalancing")
- Systems mentioned by name (e.g., "PostgreSQL", "AcctSvc")
- Test cases with specific IDs (e.g., "TC-REBAL-001")
- Requirements with IDs (e.g., "REQ-001")

WHAT NOT TO EXTRACT:
- Flowchart box labels (e.g., "Check band", "Build proposal") - these are diagram elements, not named processes
- State machine states (e.g., "Rejected", "HappyPath") - these are states, not processes
- Decision tree branches (e.g., "AggressiveProfile", "ConservativeProfile") - these are data values
- Range endpoints (e.g., from "TC-001 through TC-010" do NOT extract TC-001 and TC-010 separately)
- PII field names (e.g., "SSN", "Email", "IPAddress") - these are field names, not business data entities
- Architecture layer names (e.g., "ClientLayer", "ServiceMesh") - use specific component names instead

GUIDANCE FOR DIAGRAMS:
- A state machine diagram describes ONE Process entity (extract the machine name only)
- A flowchart describes ONE Process with steps (extract the process name, not each box)
- "TC-001 through TC-010" means a test suite - extract nothing or extract as one entity, NOT 10 separate entities

=== ENTITY NAME RULES ===
- Entity names must be 1-5 words MAXIMUM
- Use CamelCase or short phrases (e.g., "DriftDetection", "RebalancingEngine")
- If you cannot identify a clear short name, SKIP the entity
- When in doubt, extract FEWER entities, not more

=== PROCESS vs DATAENTITY CLASSIFICATION (CRITICAL) ===
RULE: Use the "Can you DO this?" test to decide if something is a Process:
- Process = An ACTION or WORKFLOW you can DO (gerund/verb-form: Rebalancing, Detection, Monitoring)
- DataEntity = A VALUE, CATEGORY, or CLASSIFICATION you HAVE (not an action)

EXAMPLES:
- "Rebalancing" - Can you DO rebalancing? YES → Process ✓
- "DriftDetection" - Can you DO drift detection? YES → Process ✓
- "Aggressive" - Can you DO aggressive? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Conservative" - Can you DO conservative? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Moderate" - Can you DO moderate? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Monthly" - Can you DO monthly? NO → NOT a Process (it's a time period → DataEntity)

Adjectives and modifiers (Aggressive, Conservative, High, Low, Daily, Weekly) are NEVER Processes.

OUTPUT FORMAT - For each block, provide a JSON object with:
- block_category: MUST be one of the allowed categories (EXACT name, case-sensitive)
- kg_node_type: the corresponding KG node type (use mapping below)
- entities: list of {{"type": str, "value": str, "confidence": float}} objects
  - "type" MUST be from NODE TYPES list above (EXACT name, case-sensitive)
  - "value" MUST be 1-5 words (short identifiers, not sentences!)
- relationships: list of {{"from_type": str, "from_value": str, "rel_type": str, "to_type": str, "to_value": str, "confidence": float}} objects
  - rel_type MUST be from RELATIONSHIP TYPES list above
  - Respect from/to type constraints
  - If no relationship matches allowed types, return empty array []

Category to Node Type Mapping:
{json.dumps(category_mapping, indent=2)}

Examples:
{examples_json}

Return a JSON array with one object per input block. Be strict with JSON formatting.

Input blocks:
"""
        for idx, (text, domain) in enumerate(blocks):
            prompt += f"\nBlock {idx + 1} (domain: {domain}):\n{text}\n"
        
        prompt += "\n\nReturn only the JSON array, no other text."
        
        # Token safety check before LLM call
        is_safe, estimated_tokens, token_msg = self.check_token_limit(prompt, "ie_text")
        if not is_safe:
            self.logger.error(f"Batch too large: {estimated_tokens} tokens. Split batch into smaller chunks.")
            # For safety, we'll still proceed but log the warning prominently
            print(f"[LLM][ie_text] ⚠️  WARNING: {token_msg}", flush=True)
        elif token_msg:
            print(f"[LLM][ie_text] {token_msg}", flush=True)
        
        # Call Ollama API with retry logic
        start_time = time.time()
        request_timestamp = datetime.now(timezone.utc).isoformat()
        call_id = self._get_call_id()
        
        # Log full prompt before sending
        self._log_llm_event(
            call_id,
            "LLM call starting",
            operation="ie_text",
            model_name=self.text_model,
            prompt=prompt,
            prompt_length=len(prompt),
            block_count=len(blocks),
            request_timestamp=request_timestamp
        )
        print(f"[LLM][ie_text][{call_id}] Sending request to {self.base_url} (model={self.text_model})", flush=True)
        
        # Retry logic for the request
        max_retries = 3
        backoff_factor = 2
        response = ProcessDiagram
        
        for attempt in range(max_retries):
            try:
                # Start progress monitoring thread for long requests
                import threading
                progress_stop = threading.Event()
                progress_thread = ProcessDiagram
                
                def show_progress():
                    """Show progress updates every 5 seconds during LLM call."""
                    elapsed = 0
                    while not progress_stop.is_set():
                        time.sleep(5)
                        elapsed += 5
                        if not progress_stop.is_set():
                            print(f"[LLM][ie_text][{call_id}] Still processing... (elapsed: {elapsed}s)", flush=True)
                
                # Start progress thread for requests longer than 10 seconds
                if self.timeout_sec > 10:
                    progress_thread = threading.Thread(target=show_progress, daemon=True)
                    progress_thread.start()
                
                try:
                    # Get temperature from config (default 0 for IE)
                    from kbfabric_ingest._common.config import get_app_config
                    config = get_app_config()
                    ollama_config = config.get("ollama", {})
                    temp_config = ollama_config.get("temperature", {})
                    ie_temp = temp_config.get("ie_text", 0.0)
                    num_ctx = ollama_config.get("num_ctx", 8192)
                    
                    response = requests.post(
                        f"{self.base_url}/api/generate",
                        json={
                            "model": self.text_model,
                            "prompt": prompt,
                            "stream": False,
                            "format": "json",
                            "options": {
                                "temperature": ie_temp,  # From config (default 0 for deterministic IE)
                                "seed": 42,  # Fixed seed for reproducibility
                                "num_ctx": num_ctx  # Context window from config
                            }
                        },
                        timeout=self.timeout_sec
                    )
                    response.raise_for_status()
                    break  # Success, exit retry loop
                finally:
                    # Stop progress thread
                    if progress_thread:
                        progress_stop.set()
                        progress_thread.join(timeout=1)
            except requests.Timeout as e:
                if attempt < max_retries - 1:
                    wait_time = backoff_factor ** attempt
                    print(f"[RETRY] ie_text timeout (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s...", flush=True)
                    logger.warning(f"LLM call timeout, retrying", operation="ie_text", attempt=attempt+1, wait_sec=wait_time)
                    time.sleep(wait_time)
                    continue
                raise
            except requests.exceptions.HTTPError as e:
                # Check if it's a 500 error
                if hasattr(e, 'response') and e.response.status_code == 500 and attempt < max_retries - 1:
                    wait_time = backoff_factor ** attempt
                    print(f"[RETRY] ie_text 500 error (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s...", flush=True)
                    logger.warning(f"LLM call 500 error, retrying", operation="ie_text", attempt=attempt+1, wait_sec=wait_time)
                    time.sleep(wait_time)
                    continue
                raise
            except requests.exceptions.ConnectionError as e:
                # Retry connection errors (server may be temporarily unavailable)
                # Connection errors are often transient: server restart, network hiccup,
                # connection pool exhaustion. Use exponential backoff consistent with
                # timeout/500 error handling.
                if attempt < max_retries - 1:
                    wait_time = backoff_factor ** attempt  # 1s, 2s, 4s
                    print(f"[RETRY] ie_text connection error (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s...", flush=True)
                    logger.warning(
                        f"LLM call connection error, retrying",
                        operation="ie_text",
                        attempt=attempt+1,
                        wait_sec=wait_time,
                        error_type=type(e).__name__,
                        error_msg=str(e)[:200]  # Truncate long error messages
                    )
                    time.sleep(wait_time)
                    continue
                # After max retries, log final failure and raise
                logger.error(
                    f"LLM call connection error exhausted retries",
                    operation="ie_text",
                    max_retries=max_retries,
                    error_type=type(e).__name__,
                    error_msg=str(e)[:200]
                )
                raise
            except requests.exceptions.RequestException as e:
                # Don't retry on other request exceptions
                raise
        
        if response is ProcessDiagram:
            raise RuntimeError("Failed to get response after retries")
        
        try:
            
            response_time = time.time()
            response_timestamp = datetime.now(timezone.utc).isoformat()
            duration_ms = (response_time - start_time) * 1000
            
            result = response.json()
            response_text = result.get("response", "")
            
            # Log full response
            self._log_llm_event(
                call_id,
                "LLM call response received",
                operation="ie_text",
                model_name=self.text_model,
                response=response_text,
                response_length=len(response_text),
                response_timestamp=response_timestamp,
                duration_ms=duration_ms,
                status_code=response.status_code
            )
            print(f"[LLM][ie_text][{call_id}] Response received (status={response.status_code}, duration={duration_ms:.0f}ms)", flush=True)
            
            # Parse JSON response
            try:
                # Try to extract JSON from response
                json_start = response_text.find("[")
                json_end = response_text.rfind("]") + 1
                if json_start >= 0 and json_end > json_start:
                    json_text = response_text[json_start:json_end]
                    ie_results = json.loads(json_text)
                else:
                    # Fallback: try parsing entire response
                    ie_results = json.loads(response_text)
                
                self._log_llm_event(
                    call_id,
                    "LLM call JSON parsed successfully",
                    operation="ie_text",
                    model_name=self.text_model,
                    parsed_result_count=len(ie_results),
                    expected_count=len(blocks)
                )
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse LLM JSON response: {str(e)}"
                self.logger.error("Failed to parse LLM JSON response", error=str(e), response_preview=response_text[:500])
                self._log_llm_event(
                    call_id,
                    "LLM call JSON parse failed",
                    operation="ie_text",
                    model_name=self.text_model,
                    error=error_msg,
                    response_preview=response_text[:1000],
                    duration_ms=duration_ms
                )
                print(f"[LLM][ie_text] JSON parse failed: {error_msg}", flush=True)
                # Return default structure
                ie_results = [
                    {
                        "block_category": "Narrative",
                        "kg_node_type": "NarrativeText",
                        "entities": [],
                        "relationships": []
                    }
                    for _ in blocks
                ]
            
            # Log successful call summary
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.text_model,
                operation="ie_text",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                response_length=len(response_text),
                block_count=len(blocks),
                result_count=len(ie_results),
                status_code=response.status_code
            )
            
            # Extract token counts from Ollama response (OTel Gen AI convention)
            input_tokens = result.get("prompt_eval_count", 0)
            output_tokens = result.get("eval_count", 0)
            
            # Record telemetry metric with token counts
            record_llm_call(
                operation="ie_text", 
                model=self.text_model, 
                status="success", 
                duration_seconds=duration_ms / 1000,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            # Also log full prompt/response for detailed analysis
            self._log_llm_event(
                call_id,
                "LLM call complete",
                operation="ie_text",
                model_name=self.text_model,
                prompt=prompt,
                response=response_text,
                request_timestamp=request_timestamp,
                response_timestamp=response_timestamp,
                duration_ms=duration_ms,
                block_count=len(blocks),
                result_count=len(ie_results)
            )
            
            # Ensure we have one result per block
            if len(ie_results) != len(blocks):
                self.logger.warning(
                    "IE result count mismatch",
                    expected=len(blocks),
                    got=len(ie_results)
                )
                # Pad or truncate
                if len(ie_results) < len(blocks):
                    default_result = {
                        "block_category": "Narrative",
                        "kg_node_type": "NarrativeText",
                        "entities": [],
                        "relationships": []
                    }
                    ie_results.extend([default_result] * (len(blocks) - len(ie_results)))
                else:
                    ie_results = ie_results[:len(blocks)]
            
            return ie_results
            
        except requests.Timeout as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"Timeout after {self.timeout_sec}s: {str(e)}"
            self.logger.exception("LLM text IE call timed out", model_name=self.text_model, operation="ie_text", duration_ms=duration_ms, timeout_sec=self.timeout_sec)
            self._log_llm_event(
                call_id,
                "LLM call timeout",
                operation="ie_text",
                model_name=self.text_model,
                error=error_msg,
                duration_ms=duration_ms,
                timeout_sec=self.timeout_sec,
                prompt=prompt,
                prompt_length=len(prompt),
                block_count=len(blocks),
                request_timestamp=request_timestamp
            )
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.text_model,
                operation="ie_text",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                block_count=len(blocks),
                error=error_msg
            )
            print(f"[LLM][ie_text][{call_id}] TIMEOUT after {self.timeout_sec}s", flush=True)
            
            # Record telemetry metric for timeout
            record_llm_call(
                operation="ie_text", 
                model=self.text_model, 
                status="timeout", 
                duration_seconds=duration_ms / 1000,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            # Return default structure
            return [
                {
                    "block_category": "Narrative",
                    "kg_node_type": "NarrativeText",
                    "entities": [],
                    "relationships": []
                }
                for _ in blocks
            ]
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"Exception: {str(e)}"
            self.logger.exception("LLM text IE call failed", model_name=self.text_model, operation="ie_text", duration_ms=duration_ms, error=str(e))
            self._log_llm_event(
                call_id,
                "LLM call failed",
                operation="ie_text",
                model_name=self.text_model,
                error=error_msg,
                duration_ms=duration_ms,
                prompt=prompt,
                prompt_length=len(prompt),
                block_count=len(blocks),
                request_timestamp=request_timestamp,
                exception_type=type(e).__name__
            )
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.text_model,
                operation="ie_text",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                block_count=len(blocks),
                error=error_msg
            )
            print(f"[LLM][ie_text][{call_id}] ERROR: {error_msg}", flush=True)
            
            # Record telemetry metric for error
            record_llm_call(
                operation="ie_text", 
                model=self.text_model, 
                status="error", 
                duration_seconds=duration_ms / 1000,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            # Return default structure
            return [
                {
                    "block_category": "Narrative",
                    "kg_node_type": "NarrativeText",
                    "entities": [],
                    "relationships": []
                }
                for _ in blocks
            ]

    def ie_image_analysis(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Perform information extraction on images.
        
        Args:
            items: List of dicts with keys: image_bytes, domain, context_text
            
        Returns:
            List of IE result dictionaries
        """
        if not items:
            return []
        
        print(f"[LLM][ie_image] Preparing prompt for {len(items)} images", flush=True)
        
        # Build prompt - simplified for diagrams only
        category_mapping = get_block_category_to_node_mapping()
        
        prompt = f"""You are an information extraction system for enterprise knowledge bases.

Task: For each image, interpret it as a process or architecture diagram within the given domain, and extract information.

Block categories for images: Diagram, Diagram_Architecture

For each image, provide a JSON object with:
- description: short text description of the image content
- block_category: either "Diagram" (for process flows, workflows) or "Diagram_Architecture" (for system architecture, component diagrams)
- kg_node_type: "ProcessDiagram" if block_category is Diagram, or "ArchitectureDiagram" if block_category is Diagram_Architecture
- entities: list of {{"type": str, "value": str, "confidence": float}} objects extracted from the diagram (e.g., processes, systems, components)
- relationships: list of {{"from_type": str, "from_value": str, "rel_type": str, "to_type": str, "to_value": str, "confidence": float}} objects representing connections in the diagram

Return a JSON array with one object per input image. Be strict with JSON formatting.

Input images:
"""
        for idx, item in enumerate(items):
            domain = item.get("domain", "unknown")
            context = item.get("context_text", "")
            ocr_text = item.get("ocr_text", "")
            
            prompt += f"\nImage {idx + 1} (domain: {domain}"
            if context:
                prompt += f", context: {context}"
            if ocr_text:
                # Include OCR-extracted text to help LLM understand diagram labels
                prompt += f")\nOCR Text Found in Diagram: {ocr_text[:500]}"  # Limit to 500 chars
            else:
                prompt += ")"
            prompt += "\n"
        
        prompt += "\n\nReturn only the JSON array, no other text."
        
        # Call Ollama vision API
        start_time = time.time()
        request_timestamp = datetime.now(timezone.utc).isoformat()
        call_id = self._get_call_id()
        
        try:
            # For vision models, we need to send images as base64
            import base64
            
            # Build request with images and calculate sizes
            images_data = []
            valid_images_count = 0
            image_sizes_bytes = []
            for item in items:
                image_bytes = item.get("image_bytes")
                if image_bytes and len(image_bytes) > 0:
                    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                    images_data.append(image_b64)
                    image_sizes_bytes.append(len(image_bytes))  # Original size, not base64
                    valid_images_count += 1
                else:
                    images_data.append("")
                    image_sizes_bytes.append(0)
                    self.logger.warning(
                        "Image item has no or empty image_bytes",
                        item_index=len(images_data) - 1,
                        domain=item.get("domain", "unknown")
                    )
            
            # Calculate image sizes in MB for logging (original bytes, not base64)
            image_sizes_mb = [round(size / (1024 * 1024), 2) for size in image_sizes_bytes]
            total_image_size_mb = sum(image_sizes_bytes) / (1024 * 1024)
            
            # Log image preparation with size information
            self.logger.info(
                "Preparing vision API call",
                total_items=len(items),
                valid_images=valid_images_count,
                model=self.vision_model,
                total_image_size_mb=round(total_image_size_mb, 2),
                max_image_size_mb=round(max(image_sizes_bytes) / (1024 * 1024), 2) if image_sizes_bytes else 0
            )
            
            # Log full prompt before sending (with image size metadata)
            self._log_llm_event(
                call_id,
                "LLM call starting",
                operation="ie_image",
                model_name=self.vision_model,
                prompt=prompt,
                prompt_length=len(prompt),
                image_count=len(items),
                image_sizes_mb=image_sizes_mb,
                total_image_size_mb=round(total_image_size_mb, 2),
                valid_images=valid_images_count,
                request_timestamp=request_timestamp
            )
            print(f"[LLM][ie_image][{call_id}] Sending request to {self.base_url} (model={self.vision_model})", flush=True)
            
            # Check vision model health before calling
            from kbfabric_ingest.llm.vision_health_check import verify_vision_model_before_call
            
            model_available = verify_vision_model_before_call(
                base_url=self.base_url,
                model_name=self.vision_model,
                timeout=5,
                raise_on_missing=False
            )
            
            if not model_available:
                # Model not available - return default results
                error_msg = f"Vision model '{self.vision_model}' is not available. Skipping vision analysis."
                self.logger.warning(error_msg)
                print(f"[LLM][ie_image] WARNING: {error_msg}", flush=True)
                self._log_llm_event(
                    call_id,
                    "LLM call skipped - model not available",
                    operation="ie_image",
                    model_name=self.vision_model,
                    error=error_msg,
                    request_timestamp=request_timestamp
                )
                return [
                    {
                        "description": "Image description unavailable - vision model not loaded",
                        "block_category": "Diagram",
                        "kg_node_type": "ProcessDiagram",
                        "entities": [],
                        "relationships": []
                    }
                    for _ in items
                ]
            
            # Try /api/generate first (standard Ollama format)
            # If that fails with 500, try /api/chat (some vision models use this)
            response = None
            last_error = None
            max_endpoint_retries = 2
            max_request_retries = 3
            backoff_factor = 2
            
            # Start progress monitoring thread for long vision requests
            import threading
            vision_progress_stop = threading.Event()
            vision_progress_thread = None
            
            def show_vision_progress():
                """Show progress updates every 5 seconds during vision LLM call."""
                elapsed = 0
                while not vision_progress_stop.is_set():
                    time.sleep(5)
                    elapsed += 5
                    if not vision_progress_stop.is_set():
                        print(f"[LLM][ie_image][{call_id}] Still processing vision... (elapsed: {elapsed}s)", flush=True)
            
            # Start progress thread for requests longer than 10 seconds
            if self.timeout_sec > 10:
                vision_progress_thread = threading.Thread(target=show_vision_progress, daemon=True)
                vision_progress_thread.start()
            
            try:
                # Get vision IE temperature and num_ctx from config
                from kbfabric_ingest._common.config import get_app_config
                config = get_app_config()
                ollama_config = config.get("ollama", {})
                temp_config = ollama_config.get("temperature", {})
                ie_vision_temp = temp_config.get("ie_vision", 0.0)
                num_ctx = ollama_config.get("num_ctx", 8192)
                
                for endpoint_attempt, endpoint in enumerate(["/api/generate", "/api/chat"], 1):
                    for request_attempt in range(max_request_retries):
                        try:
                            if endpoint == "/api/generate":
                                # Standard format: images array with base64 strings
                                request_payload = {
                                    "model": self.vision_model,
                                    "prompt": prompt,
                                    "images": images_data,
                                    "stream": False,
                                    "format": "json",
                                    "options": {
                                        "temperature": ie_vision_temp,
                                        "num_ctx": num_ctx
                                    }
                                }
                            else:
                                # Chat format: messages array with image_url
                                messages = [{
                                    "role": "user",
                                    "content": prompt
                                }]
                                # Add images to content if chat format supports it
                                # Some models expect images in content array
                                request_payload = {
                                    "model": self.vision_model,
                                    "messages": messages,
                                    "stream": False,
                                    "format": "json",
                                    "options": {
                                        "temperature": ie_vision_temp,
                                        "num_ctx": num_ctx
                                    }
                                }
                                # Try adding images to first message content if supported
                                if hasattr(self, '_try_chat_with_images'):
                                    # For now, stick with /api/generate format
                                    continue
                            
                            response = requests.post(
                                f"{self.base_url}{endpoint}",
                                json=request_payload,
                                timeout=self.timeout_sec
                            )
                            
                            if response.status_code == 200:
                                break  # Success, exit both loops
                            elif response.status_code == 500:
                                if request_attempt < max_request_retries - 1:
                                    # Retry same endpoint
                                    wait_time = backoff_factor ** request_attempt
                                    print(f"[RETRY] ie_image 500 error on {endpoint} (attempt {request_attempt+1}/{max_request_retries}), retrying in {wait_time}s...", flush=True)
                                    logger.warning(f"LLM vision call 500 error, retrying", endpoint=endpoint, attempt=request_attempt+1, wait_sec=wait_time)
                                    time.sleep(wait_time)
                                    continue
                                elif endpoint_attempt < max_endpoint_retries:
                                    # Try alternative endpoint
                                    error_detail = response.text[:500] if response.text else "No error details"
                                    self.logger.warning(
                                        f"Ollama {endpoint} returned 500, trying alternative endpoint",
                                        status_code=response.status_code,
                                        model=self.vision_model,
                                        error_detail=error_detail[:200]
                                    )
                                    print(f"[LLM][ie_image] WARNING: {endpoint} returned 500, trying alternative...", flush=True)
                                    last_error = f"{endpoint} returned 500: {error_detail[:200]}"
                                    break  # Break to try next endpoint
                                else:
                                    response.raise_for_status()
                            else:
                                # Other error - don't retry
                                response.raise_for_status()
                                
                        except requests.Timeout as e:
                            if request_attempt < max_request_retries - 1:
                                wait_time = backoff_factor ** request_attempt
                                print(f"[RETRY] ie_image timeout on {endpoint} (attempt {request_attempt+1}/{max_request_retries}), retrying in {wait_time}s...", flush=True)
                                logger.warning(f"LLM vision call timeout, retrying", endpoint=endpoint, attempt=request_attempt+1, wait_sec=wait_time)
                                time.sleep(wait_time)
                                continue
                            elif endpoint_attempt < max_endpoint_retries:
                                last_error = str(e)
                                self.logger.warning(f"Request to {endpoint} timed out, trying alternative", error=str(e))
                                break  # Break to try next endpoint
                            else:
                                raise
                        except requests.exceptions.ConnectionError as e:
                            # Retry connection errors (server may be temporarily unavailable)
                            # Connection errors are often transient: server restart, network hiccup,
                            # connection pool exhaustion. Use exponential backoff consistent with
                            # timeout/500 error handling.
                            if request_attempt < max_request_retries - 1:
                                wait_time = backoff_factor ** request_attempt  # 1s, 2s, 4s
                                print(f"[RETRY] ie_image connection error on {endpoint} (attempt {request_attempt+1}/{max_request_retries}), retrying in {wait_time}s...", flush=True)
                                logger.warning(
                                    f"LLM vision call connection error, retrying",
                                    endpoint=endpoint,
                                    attempt=request_attempt+1,
                                    wait_sec=wait_time,
                                    error_type=type(e).__name__,
                                    error_msg=str(e)[:200]
                                )
                                time.sleep(wait_time)
                                continue
                            elif endpoint_attempt < max_endpoint_retries:
                                # Try alternative endpoint
                                last_error = str(e)
                                self.logger.warning(f"Request to {endpoint} connection error, trying alternative", error=str(e))
                                break  # Break to try next endpoint
                            else:
                                # After max retries, log final failure and raise
                                logger.error(
                                    f"LLM vision call connection error exhausted retries",
                                    endpoint=endpoint,
                                    max_retries=max_request_retries,
                                    error_type=type(e).__name__,
                                    error_msg=str(e)[:200]
                                )
                                raise
                        except requests.exceptions.RequestException as e:
                            if endpoint_attempt < max_endpoint_retries and request_attempt == max_request_retries - 1:
                                last_error = str(e)
                                self.logger.warning(f"Request to {endpoint} failed, trying alternative", error=str(e))
                                break  # Break to try next endpoint
                            else:
                                raise
                    
                    # Check if we got a successful response (from inner try block)
                    if response and response.status_code == 200:
                        break  # Success, exit endpoint loop
            finally:
                # Stop progress thread
                if vision_progress_thread:
                    vision_progress_stop.set()
                    vision_progress_thread.join(timeout=1)
            
            # Enhanced error handling
            if response is None or response.status_code != 200:
                error_detail = response.text[:1000] if response and response.text else (last_error or "No error details")
                self.logger.error(
                    "Ollama vision API returned error",
                    status_code=response.status_code if response else None,
                    model=self.vision_model,
                    error_detail=error_detail,
                    response_headers=dict(response.headers) if response else {}
                )
                print(f"[LLM][ie_image] ERROR: Ollama returned {response.status_code if response else 'None'}: {error_detail[:200]}", flush=True)
                if response:
                    response.raise_for_status()
                else:
                    raise RuntimeError(f"Vision API call failed: {last_error}")
            
            response_time = time.time()
            response_timestamp = datetime.now(timezone.utc).isoformat()
            duration_ms = (response_time - start_time) * 1000
            
            result = response.json()
            response_text = result.get("response", "")
            
            # Log full response
            self._log_llm_event(
                call_id,
                "LLM call response received",
                operation="ie_image",
                model_name=self.vision_model,
                response=response_text,
                response_length=len(response_text),
                response_timestamp=response_timestamp,
                duration_ms=duration_ms,
                status_code=response.status_code
            )
            print(f"[LLM][ie_image][{call_id}] Response received (status={response.status_code}, duration={duration_ms:.0f}ms)", flush=True)
            
            # Parse JSON response
            try:
                json_start = response_text.find("[")
                json_end = response_text.rfind("]") + 1
                if json_start >= 0 and json_end > json_start:
                    json_text = response_text[json_start:json_end]
                    ie_results = json.loads(json_text)
                else:
                    ie_results = json.loads(response_text)
                
                self._log_llm_event(
                    call_id,
                    "LLM call JSON parsed successfully",
                    operation="ie_image",
                    model_name=self.vision_model,
                    parsed_result_count=len(ie_results),
                    expected_count=len(items)
                )
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse LLM vision JSON response: {str(e)}"
                self.logger.error("Failed to parse LLM vision JSON response", error=str(e), response_preview=response_text[:500])
                self._log_llm_event(
                    call_id,
                    "LLM call JSON parse failed",
                    operation="ie_image",
                    model_name=self.vision_model,
                    error=error_msg,
                    response_preview=response_text[:1000],
                    duration_ms=duration_ms
                )
                print(f"[LLM][ie_image] JSON parse failed: {error_msg}", flush=True)
                ie_results = [
                    {
                        "description": "Image description unavailable",
                        "block_category": "Diagram",
                        "kg_node_type": "ProcessDiagram",
                        "entities": [],
                        "relationships": []
                    }
                    for _ in items
                ]
            
            # Log successful call summary
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.vision_model,
                operation="ie_image",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                response_length=len(response_text),
                block_count=len(items),  # Using block_count for consistency
                result_count=len(ie_results),
                status_code=response.status_code
            )
            
            # Extract token counts from Ollama response (OTel Gen AI convention)
            input_tokens = result.get("prompt_eval_count", 0)
            output_tokens = result.get("eval_count", 0)
            
            # Record telemetry metric with token counts
            record_llm_call(
                operation="ie_image", 
                model=self.vision_model, 
                status="success", 
                duration_seconds=duration_ms / 1000,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            # Also log full prompt/response for detailed analysis
            self._log_llm_event(
                call_id,
                "LLM call complete",
                operation="ie_image",
                model_name=self.vision_model,
                prompt=prompt,
                response=response_text,
                request_timestamp=request_timestamp,
                response_timestamp=response_timestamp,
                duration_ms=duration_ms,
                image_count=len(items),
                result_count=len(ie_results)
            )
            
            # Ensure we have one result per item
            if len(ie_results) != len(items):
                self.logger.warning(
                    "IE vision result count mismatch",
                    expected=len(items),
                    got=len(ie_results)
                )
                if len(ie_results) < len(items):
                    default_result = {
                        "description": "Image description unavailable",
                        "block_category": "Diagram",
                        "kg_node_type": "ProcessDiagram",
                        "entities": [],
                        "relationships": []
                    }
                    ie_results.extend([default_result] * (len(items) - len(ie_results)))
                else:
                    ie_results = ie_results[:len(items)]
            
            return ie_results
            
        except requests.Timeout as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"Timeout after {self.timeout_sec}s: {str(e)}"
            self.logger.exception("LLM vision IE call timed out", model_name=self.vision_model, operation="ie_image", duration_ms=duration_ms, timeout_sec=self.timeout_sec)
            self._log_llm_event(
                call_id,
                "LLM call timeout",
                operation="ie_image",
                model_name=self.vision_model,
                error=error_msg,
                duration_ms=duration_ms,
                timeout_sec=self.timeout_sec,
                prompt=prompt,
                prompt_length=len(prompt),
                image_count=len(items),
                request_timestamp=request_timestamp
            )
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.vision_model,
                operation="ie_image",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                block_count=len(items),
                error=error_msg
            )
            print(f"[LLM][ie_image][{call_id}] TIMEOUT after {self.timeout_sec}s", flush=True)
            
            # Record telemetry metric for timeout
            record_llm_call(
                operation="ie_image", 
                model=self.vision_model, 
                status="timeout", 
                duration_seconds=duration_ms / 1000,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            return [
                {
                    "description": "Image description unavailable",
                    "block_category": "Diagram",
                    "kg_node_type": "ProcessDiagram",
                    "entities": [],
                    "relationships": []
                }
                for _ in items
            ]
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"Exception: {str(e)}"
            self.logger.exception("LLM vision IE call failed", model_name=self.vision_model, operation="ie_image", duration_ms=duration_ms, error=str(e))
            self._log_llm_event(
                call_id,
                "LLM call failed",
                operation="ie_image",
                model_name=self.vision_model,
                error=error_msg,
                duration_ms=duration_ms,
                prompt=prompt,
                prompt_length=len(prompt),
                image_count=len(items),
                request_timestamp=request_timestamp,
                exception_type=type(e).__name__
            )
            self._log_llm_call_summary(
                call_id=call_id,
                model_name=self.vision_model,
                operation="ie_image",
                duration_ms=duration_ms,
                prompt_length=len(prompt),
                block_count=len(items),
                error=error_msg
            )
            print(f"[LLM][ie_image][{call_id}] ERROR: {error_msg}", flush=True)
            
            # Record telemetry metric for error
            record_llm_call(
                operation="ie_image", 
                model=self.vision_model, 
                status="error", 
                duration_seconds=duration_ms / 1000,
                server_address=self.base_url.replace("http://", "").replace("https://", "")
            )
            
            return [
                {
                    "description": "Image description unavailable",
                    "block_category": "Diagram",
                    "kg_node_type": "ProcessDiagram",
                    "entities": [],
                    "relationships": []
                }
                for _ in items
            ]

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



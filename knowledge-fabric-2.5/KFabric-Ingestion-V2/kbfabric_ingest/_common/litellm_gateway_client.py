"""HTTP client for QiBa LiteLLM Model Router (Kong public gateway).

Routes text LLM calls through:
  POST /ai-assistant/conversation  (multi-turn / system+user messages)
  POST /ai-assistant/content       (single prompt)

Vision/multimodal IE continues to use direct Ollama when gateway is enabled
(images are not supported on the public content endpoint).
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

GATEWAY_ENDPOINT_CONVERSATION = "/ai-assistant/conversation"
GATEWAY_ENDPOINT_CONTENT = "/ai-assistant/content"


def extract_gateway_response_text(data: Any) -> str:
    """Parse Model Router / Kong response into assistant text."""
    if isinstance(data, dict):
        response = data.get("response")
        if response:
            return str(response)
        text = data.get("text")
        if text:
            return str(text)
        choices = data.get("choices", [])
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content:
                return str(content)
    return str(data) if data is not None else ""


def gateway_config_from_app(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return gateway settings when model_router is enabled and fully configured."""
    mr = config.get("model_router") or {}
    if not mr.get("enabled"):
        return None

    chat_url = (mr.get("chat_url") or "").strip()
    generate_url = (mr.get("generate_url") or "").strip()
    auth_token = (mr.get("auth_token") or "").strip()
    if not chat_url or not generate_url or not auth_token:
        logger.warning(
            "model_router.enabled is true but chat_url, generate_url, or auth_token is missing"
        )
        return None

    ollama_text = (config.get("ollama") or {}).get("text") or {}
    default_max_tokens = int(
        mr.get("max_tokens") or ollama_text.get("max_context_tokens") or 4000
    )

    return {
        "chat_url": chat_url,
        "generate_url": generate_url,
        "auth_token": auth_token,
        "text_model": (mr.get("text_model") or "TEXT").strip() or "TEXT",
        "max_tokens": default_max_tokens,
        "max_retries": int(mr.get("max_retries", 5)),
    }


def is_gateway_enabled(config: dict[str, Any]) -> bool:
    return gateway_config_from_app(config) is not None


class ModelRouterGatewayClient:
    """Sync HTTP client for LiteLLM Model Router public endpoints."""

    def __init__(
        self,
        gateway_config: dict[str, Any],
        timeout_sec: int = 300,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.chat_url = gateway_config["chat_url"]
        self.generate_url = gateway_config["generate_url"]
        self.auth_token = gateway_config["auth_token"]
        self.default_model = gateway_config.get("text_model", "TEXT")
        self.default_max_tokens = int(gateway_config.get("max_tokens", 4000))
        self.max_retries = int(gateway_config.get("max_retries", 5))
        self.timeout_sec = timeout_sec
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }

    def _post_with_retries(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        endpoint_label: str,
    ) -> Tuple[dict[str, Any], int, float]:
        delay = 1.0
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            start = time.time()
            try:
                response = self.session.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout_sec,
                )
                duration_ms = (time.time() - start) * 1000

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay
                    logger.warning(
                        "Gateway %s rate limited (429), retrying in %.1fs (attempt %d)",
                        endpoint_label,
                        wait,
                        attempt,
                    )
                    time.sleep(wait)
                    delay = min(delay * 2, 30.0)
                    continue

                if response.status_code >= 400:
                    body_preview = (response.text or "")[:512]
                    raise RuntimeError(
                        f"Gateway {endpoint_label} HTTP {response.status_code}: {body_preview}"
                    )

                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    body_preview = (response.text or "")[:512]
                    raise RuntimeError(
                        f"Gateway {endpoint_label} returned non-JSON body: {body_preview}"
                    ) from exc

                return data, response.status_code, duration_ms

            except requests.exceptions.Timeout as exc:
                last_error = exc
                logger.warning(
                    "Gateway %s timeout on attempt %d/%d",
                    endpoint_label,
                    attempt,
                    self.max_retries,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in (429, 502, 503, 504):
                    raise RuntimeError(f"Gateway {endpoint_label} failed: {exc}") from exc
                logger.warning(
                    "Gateway %s request error on attempt %d/%d: %s",
                    endpoint_label,
                    attempt,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries:
                sleep_for = delay * (0.5 + random.random() * 0.5)
                time.sleep(sleep_for)
                delay = min(delay * 2, 30.0)

        raise RuntimeError(
            f"Gateway {endpoint_label} failed after {self.max_retries} attempts: {last_error}"
        )

    def conversation(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> Tuple[str, dict[str, Any]]:
        """POST /ai-assistant/conversation."""
        payload = {
            "messages": messages,
            "model": model or self.default_model,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "stream": stream,
        }
        data, status_code, duration_ms = self._post_with_retries(
            self.chat_url,
            payload,
            endpoint_label=GATEWAY_ENDPOINT_CONVERSATION,
        )
        response_text = extract_gateway_response_text(data)
        metadata = {
            "endpoint": GATEWAY_ENDPOINT_CONVERSATION,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 1),
            "model": payload["model"],
            "temperature": temperature,
            "max_tokens": payload["max_tokens"],
            "message_count": len(messages),
            "request_id": data.get("request_id") if isinstance(data, dict) else None,
            "provider": data.get("provider") if isinstance(data, dict) else None,
            "gateway": True,
            "success": True,
        }
        return response_text, metadata

    def content(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> Tuple[str, dict[str, Any]]:
        """POST /ai-assistant/content."""
        payload = {
            "prompt": prompt,
            "model": model or self.default_model,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "stream": stream,
        }
        data, status_code, duration_ms = self._post_with_retries(
            self.generate_url,
            payload,
            endpoint_label=GATEWAY_ENDPOINT_CONTENT,
        )
        response_text = extract_gateway_response_text(data)
        metadata = {
            "endpoint": GATEWAY_ENDPOINT_CONTENT,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 1),
            "model": payload["model"],
            "temperature": temperature,
            "max_tokens": payload["max_tokens"],
            "prompt_length": len(prompt),
            "request_id": data.get("request_id") if isinstance(data, dict) else None,
            "provider": data.get("provider") if isinstance(data, dict) else None,
            "gateway": True,
            "success": True,
        }
        return response_text, metadata

    def health_check(self, timeout_sec: Optional[int] = None) -> bool:
        """Lightweight gateway probe via conversation endpoint."""
        try:
            _, _ = self.conversation(
                messages=[{"role": "user", "content": "ping"}],
                temperature=0.0,
                max_tokens=16,
            )
            return True
        except Exception as exc:
            logger.error("Model Router gateway health check failed: %s", exc)
            return False


def build_gateway_client(
    config: dict[str, Any],
    timeout_sec: int = 300,
    session: Optional[requests.Session] = None,
) -> Optional[ModelRouterGatewayClient]:
    """Create a gateway client from app config, or None when disabled."""
    gateway_cfg = gateway_config_from_app(config)
    if not gateway_cfg:
        return None
    return ModelRouterGatewayClient(gateway_cfg, timeout_sec=timeout_sec, session=session)

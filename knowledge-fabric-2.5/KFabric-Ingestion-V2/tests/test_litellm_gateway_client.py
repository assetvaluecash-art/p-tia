"""Tests for LiteLLM Model Router gateway client."""

from kbfabric_ingest._common.litellm_gateway_client import (
    GATEWAY_ENDPOINT_CONTENT,
    GATEWAY_ENDPOINT_CONVERSATION,
    ModelRouterGatewayClient,
    extract_gateway_response_text,
    gateway_config_from_app,
    is_gateway_enabled,
)


def test_extract_gateway_response_text_prefers_response_field():
    data = {"response": "hello", "model": "TEXT", "request_id": "abc"}
    assert extract_gateway_response_text(data) == "hello"


def test_extract_gateway_response_text_openai_shape():
    data = {"choices": [{"message": {"content": "world"}}]}
    assert extract_gateway_response_text(data) == "world"


def test_gateway_config_from_app_disabled():
    assert gateway_config_from_app({"model_router": {"enabled": False}}) is None
    assert not is_gateway_enabled({"model_router": {"enabled": False}})


def test_gateway_config_from_app_enabled_requires_urls_and_token():
    cfg = {
        "model_router": {
            "enabled": True,
            "chat_url": "https://gw/ai-assistant/conversation",
            "generate_url": "https://gw/ai-assistant/content",
            "auth_token": "tok",
            "text_model": "TEXT",
        },
        "ollama": {"text": {"max_context_tokens": 8192}},
    }
    resolved = gateway_config_from_app(cfg)
    assert resolved is not None
    assert resolved["chat_url"].endswith("/conversation")
    assert resolved["text_model"] == "TEXT"
    assert resolved["max_tokens"] == 8192
    assert is_gateway_enabled(cfg)


def test_gateway_conversation_and_content(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"response": '{"ok": true}', "request_id": "r1", "provider": "ollama_cloud"}

        text = '{"response": "{\\"ok\\": true}"}'

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return _Resp()

    gateway = ModelRouterGatewayClient(
        {
            "chat_url": "https://gw/ai-assistant/conversation",
            "generate_url": "https://gw/ai-assistant/content",
            "auth_token": "secret",
            "text_model": "TEXT",
            "max_tokens": 1000,
            "max_retries": 1,
        },
        timeout_sec=30,
    )
    monkeypatch.setattr(gateway.session, "post", _fake_post)

    text, meta = gateway.conversation(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
    )
    assert text == '{"ok": true}'
    assert meta["endpoint"] == GATEWAY_ENDPOINT_CONVERSATION
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert calls[0]["json"]["model"] == "TEXT"
    assert calls[0]["json"]["stream"] is False

    text2, meta2 = gateway.content(prompt="hello", temperature=0.1)
    assert text2 == '{"ok": true}'
    assert meta2["endpoint"] == GATEWAY_ENDPOINT_CONTENT
    assert calls[1]["url"].endswith("/content")
    assert calls[1]["json"]["prompt"] == "hello"

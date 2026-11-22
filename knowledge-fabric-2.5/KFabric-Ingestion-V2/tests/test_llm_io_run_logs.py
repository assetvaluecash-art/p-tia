import base64
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class _DummyResponse:
    status_code: int
    _json: dict[str, Any]
    text: str

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


def _read_jsonl(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def test_llm_run_logs_write_text_and_vision(monkeypatch, tmp_path):
    # Ensure per-run logs go into temp folder
    monkeypatch.setenv("KBFABRIC_LOG_DIR", str(tmp_path))

    run_id = "20990101_010101"

    # --- TEXT: ChatLLMClient logs request+response ---
    from kbfabric_ingest._common.chat_client import ChatLLMClient

    chat_client = ChatLLMClient(base_url="http://fake-ollama", model_name="text-model", timeout_sec=1, run_id=run_id)

    def _fake_chat_post(url, json=None, timeout=None, **kwargs):
        assert url.endswith("/api/chat")
        assert isinstance(json, dict)
        assert json.get("model") == "text-model"
        assert json.get("stream") is False
        return _DummyResponse(
            status_code=200,
            _json={"message": {"content": "{\"ok\": true}"}},
            text='{"message":{"content":"{\\"ok\\": true}"}}',
        )

    monkeypatch.setattr(chat_client.session, "post", _fake_chat_post)

    response_text, metadata = chat_client._call_chat_api(
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        temperature=0.0,
        num_ctx=128,
        operation="ie_text",
        call_id="call_text_1",
    )
    assert response_text
    assert metadata.get("endpoint") == "/api/chat"

    text_log_path = tmp_path / f"llm_text_{run_id}.jsonl"
    assert text_log_path.exists()
    text_events = _read_jsonl(text_log_path)
    assert len(text_events) >= 2
    assert any(e.get("event") == "request" and e.get("endpoint") == "/api/chat" for e in text_events)
    assert any(e.get("event") == "response" and "raw_response_text" in e for e in text_events)

    # --- VISION: LLMClient logs request+response (payload without base64 images by default) ---
    from kbfabric_ingest.llm.client import LLMClient

    # Keep config loading deterministic inside _call_generate_api_multimodal
    monkeypatch.setattr(
        "kbfabric_ingest.ingest.config.get_app_config",
        lambda: {
            "ollama": {
                "temperature": {"ie_vision": 0.0},
                "vision": {"num_ctx": 4096, "timeout_sec": 5},
                "logging": {"llm_io_include_images_base64": False},
            }
        },
    )

    llm = LLMClient({"base_url": "http://fake-ollama", "vision_model": "vision-model", "text_model": "text-model", "timeout_sec": 1})
    llm.set_run_id(run_id)

    def _fake_generate_post(url, json=None, timeout=None, **kwargs):
        assert url.endswith("/api/generate")
        assert isinstance(json, dict)
        assert json.get("model") == "vision-model"
        assert json.get("stream") is False
        # images MUST be present in the actual request payload to Ollama
        assert "images" in json and isinstance(json["images"], list) and len(json["images"]) == 1
        return _DummyResponse(
            status_code=200,
            _json={"response": "{\"vision\": true}"},
            text='{"response":"{\\"vision\\": true}"}',
        )

    monkeypatch.setattr(llm.session, "post", _fake_generate_post)

    img_bytes = b"\x89PNG\r\n\x1a\nfakepngbytes"
    b64_img = base64.b64encode(img_bytes).decode("utf-8")

    response_text, api_used = llm._call_generate_api_multimodal(
        prompt="USER",
        system_prompt="SYSTEM",
        images=[b64_img],
        model_name="vision-model",
        call_id="call_vis_1",
        operation="ie_vision",
    )
    assert response_text
    assert api_used == "generate"

    vision_log_path = tmp_path / f"llm_vision_{run_id}.jsonl"
    assert vision_log_path.exists()
    vis_events = _read_jsonl(vision_log_path)
    assert any(e.get("event") == "request" and e.get("endpoint") == "/api/generate" for e in vis_events)
    req = next(e for e in vis_events if e.get("event") == "request")
    assert req.get("call_id") == "call_vis_1"
    assert isinstance(req.get("images"), list) and req["images"] and req["images"][0].get("sha256")
    # Base64 should be omitted from the logged payload by default
    assert isinstance(req.get("payload"), dict)
    assert "images" not in req["payload"]
    assert any(e.get("event") == "response" and "raw_response_text" in e for e in vis_events)


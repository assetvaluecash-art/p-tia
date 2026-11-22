"""Durable, integrity-checked JSON checkpoints for resumable ingestion stages."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def write_checkpoint_artifact(path: Path, *, stage_id: str, payload: dict[str, Any]) -> str:
    """Atomically write a JSON stage artifact and return its SHA-256 digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"schema_version": 1, "stage_id": stage_id, "payload": payload},
                     sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return digest


def read_checkpoint_artifact(path: Path, expected_sha256: str) -> tuple[str, dict[str, Any]]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Checkpoint artifact integrity check failed")
    envelope = json.loads(raw)
    if envelope.get("schema_version") != 1 or not isinstance(envelope.get("payload"), dict):
        raise ValueError("Unsupported or malformed checkpoint artifact")
    return str(envelope["stage_id"]), envelope["payload"]

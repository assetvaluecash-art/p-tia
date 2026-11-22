# -*- coding: utf-8 -*-
"""
Unified embeddings client for QiBa Knowledge Fabric (offline-only).

ENTERPRISE OFFLINE-ONLY:
- Forces HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE before importing transformers
- Uses local_files_only=True for tokenizer + model loads
- Resolves to an explicit local model directory (no HF fallback)

Supports retrieval-trained encoder models stored locally (enterprise offline).
"""

import os
import sys
import time
import threading
import logging
from pathlib import Path
from typing import List, Optional

import numpy as np
import torch
import torch.nn.functional as F

# CRITICAL: Set offline mode BEFORE importing transformers
# This prevents any attempt to connect to HuggingFace Hub
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from transformers import AutoTokenizer, AutoModel


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with standard formatting."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


class EmbeddingsClient:
    """
    Unified embeddings client for enterprise offline environments.
    
    Features:
    - Singleton pattern for memory efficiency
    - Thread-safe model loading
    - Local-only model loading (no network calls)
    - Batch encoding support
    - Progress tracking and timing
    
    Usage:
        client = EmbeddingsClient(config)
        client.load_model()
        vectors = client.encode(["text1", "text2"])
    """

    _instance: Optional["EmbeddingsClient"] = None
    _lock = threading.Lock()

    def __new__(cls, config: Optional[dict] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, config: Optional[dict] = None):
        if self._initialized:
            return
        
        config = config or {}
        emb_cfg = config.get("embeddings", {})
        
        # Model configuration
        # Default to the retrieval-trained model used by the system configs.
        self.model_name = emb_cfg.get("model_name", "sentence-transformers/all-mpnet-base-v2")
        # Enterprise offline: require explicit local model directory (env wins over YAML)
        self._configured_local_model_path = emb_cfg.get("local_model_path")
        self.device = emb_cfg.get("device", "cuda" if torch.cuda.is_available() else "cpu")
        self.batch_size = emb_cfg.get("batch_size", 16)
        self.max_length = emb_cfg.get("max_length", 512)
        self._configured_dimension = emb_cfg.get("dimension", 1024)  # Arctic default: 1024
        
        # Local model path resolution (NO HF fallback)
        self._local_model_path = self._resolve_local_model_path()
        
        self.logger = get_logger(__name__)
        
        # Threading and readiness attributes
        self._ready_event = threading.Event()
        self._ready = False
        self._load_start_time: Optional[float] = None
        self._load_end_time: Optional[float] = None
        self._load_error: Optional[BaseException] = None
        
        # Model components (loaded lazily)
        self.tokenizer = None
        self.model = None
        
        self._initialized = True
        
        self.logger.info(
            f"[Embeddings] Client initialized - model_name={self.model_name}, device={self.device}, "
            f"batch_size={self.batch_size}, max_length={self.max_length}"
        )
        
        # Validate chunk size vs token limit (startup warning)
        ingestion_cfg = config.get("ingestion", {})
        char_limit = ingestion_cfg.get("max_chars_per_block", 1000)
        worst_case_tokens = char_limit / 2.5  # Acronym-heavy estimate
        if worst_case_tokens > self.max_length:
            self.logger.warning(
                f"[Embeddings] CONFIG RISK: max_chars_per_block={char_limit} may exceed "
                f"{self.max_length} tokens with acronym-heavy text. "
                f"Recommended max: {int(self.max_length * 2.5)}"
            )

    def _resolve_local_model_path(self) -> Optional[Path]:
        """
        Resolve local model path in order of priority (enterprise offline):
        1) KBFABRIC_EMBEDDINGS_MODEL_DIR env var (preferred)
        2) embeddings.local_model_path (YAML)
        3) Bundled model in custom_ml/embeddings/models/<model_dirname>/ (process convenience)

        IMPORTANT: There is intentionally NO HuggingFace cache / model-id fallback.
        """
        candidates: list[Path] = []
        
        env_dir = (os.environ.get("KBFABRIC_EMBEDDINGS_MODEL_DIR") or "").strip()
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        
        cfg_dir = (self._configured_local_model_path or "").strip() if isinstance(self._configured_local_model_path, str) else ""
        if cfg_dir:
            candidates.append(Path(cfg_dir).expanduser())
        
        module_dir = Path(__file__).parent
        # Prefer a bundled folder matching the configured local_model_path basename or model_name tail.
        model_dirname = None
        try:
            if isinstance(self._configured_local_model_path, str) and self._configured_local_model_path.strip():
                model_dirname = Path(self._configured_local_model_path.strip()).name
        except Exception:
            model_dirname = None
        if not model_dirname:
            # e.g., "sentence-transformers/all-mpnet-base-v2" -> "all-mpnet-base-v2"
            model_dirname = (str(self.model_name).split("/")[-1] or "model").strip() or "model"
        candidates.append(module_dir / "models" / model_dirname)
        
        for p in candidates:
            try:
                if p.exists() and self._check_model_files_at(p):
                    return p
            except Exception:
                # ignore invalid candidate
                continue

        return None

    def _check_model_files_at(self, model_dir: Path) -> bool:
        """Check if required model files are present in the given directory."""
        required_files = [
            "config.json",
        ]
        
        tokenizer_files = ["tokenizer.json", "vocab.txt", "tokenizer_config.json"]
        model_files = ["model.safetensors", "pytorch_model.bin"]
        
        # Check config
        for f in required_files:
            if not (model_dir / f).exists():
                return False
        
        # Check at least one tokenizer file
        has_tokenizer = any((model_dir / f).exists() for f in tokenizer_files)
        
        # Check at least one model file
        has_model = any((model_dir / f).exists() for f in model_files)
        
        return has_tokenizer and has_model

    def _require_local_model_path(self) -> Path:
        """Return resolved model dir or raise a clear enterprise-grade error."""
        if self._local_model_path and self._check_model_files_at(self._local_model_path):
            return self._local_model_path

        env_dir = (os.environ.get("KBFABRIC_EMBEDDINGS_MODEL_DIR") or "").strip() or None
        cfg_dir = (self._configured_local_model_path or "").strip() if isinstance(self._configured_local_model_path, str) else None
        bundled_parent = str((Path(__file__).parent / "models").resolve())

        raise RuntimeError(
            "[Embeddings] Local model directory not found or incomplete. "
            "Enterprise mode forbids HuggingFace cache/model-id fallback.\n"
            f"- Env KBFABRIC_EMBEDDINGS_MODEL_DIR: {env_dir}\n"
            f"- Config embeddings.local_model_path: {cfg_dir}\n"
            f"- Bundled fallback base dir: {bundled_parent}\n"
            "Required: config.json + (tokenizer.json|vocab.txt|tokenizer_config.json) + (model.safetensors|pytorch_model.bin)"
        )

    def load_model(self) -> None:
        """
        Load embedding model with progress tracking and timing.
        
        This method is thread-safe and will only load the model once.
        Uses local_files_only=True to prevent any network calls.
        """
        with self._lock:
            # Reset previous load error (allows explicit retry)
            self._load_error = None

            if self._ready:
                self.logger.info("[Embeddings] Model already loaded, skipping")
                return
            
            if self.tokenizer is not None and self.model is not None:
                self.logger.info("[Embeddings] Model components already exist, marking as ready")
                self._ready_event.set()
                self._ready = True
                return
            
            self.logger.info("[Embeddings] Starting model load (OFFLINE MODE)")
            self._load_start_time = time.time()
            model_dir = self._require_local_model_path()
            model_source = str(model_dir)
            self.logger.info(f"[Embeddings] Using local model dir: {model_source}")
            
            try:
                # Step 1: Initialize tokenizer
                step_start = time.time()
                self.logger.info("[Embeddings] Step 1/3 - Initializing tokenizer...")
                
                self.tokenizer = AutoTokenizer.from_pretrained(
                    model_source,
                    local_files_only=True
                )
                
                step_elapsed = time.time() - step_start
                self.logger.info(f"[Embeddings] Step 1/3 - Tokenizer initialized ({step_elapsed:.1f}s)")
                
                # Step 2: Load model weights
                step_start = time.time()
                self.logger.info("[Embeddings] Step 2/3 - Loading model weights...")

                # IMPORTANT: Avoid Transformers safetensors auto-conversion paths that can touch HF metadata.
                # Load safetensors only if the file is present locally; otherwise load pytorch_model.bin directly.
                has_safetensors = (model_dir / "model.safetensors").exists()
                self.model = AutoModel.from_pretrained(
                    model_source,
                    use_safetensors=has_safetensors,
                    local_files_only=True
                )
                
                step_elapsed = time.time() - step_start
                self.logger.info(f"[Embeddings] Step 2/3 - Model weights loaded ({step_elapsed:.1f}s)")
                
                # Step 3: Move model to device
                step_start = time.time()
                self.logger.info(f"[Embeddings] Step 3/3 - Moving model to device ({self.device})...")
                
                self.model = self.model.to(self.device)
                self.model.eval()
                
                step_elapsed = time.time() - step_start
                self.logger.info(f"[Embeddings] Step 3/3 - Model moved to device ({step_elapsed:.1f}s)")
                
                # Mark as ready
                self._ready_event.set()
                self._ready = True
                self._load_end_time = time.time()
                total_time = self._load_end_time - self._load_start_time
                
                self.logger.info(f"[Embeddings] Model loaded successfully in {total_time:.2f}s")
                
            except Exception as e:
                # Store and surface the error immediately (prevents 600s wait hang)
                self._load_error = e
                self._ready_event.set()
                error_msg = f"[Embeddings] Model loading failed: {str(e)}"
                self.logger.error(error_msg)
                raise RuntimeError(
                    f"[Embeddings] Failed to load model from local dir: {model_source}. "
                    f"Set KBFABRIC_EMBEDDINGS_MODEL_DIR or embeddings.local_model_path. Error: {e}"
                ) from e

    def wait_until_ready(self, timeout: int = 600) -> None:
        """Wait for model to be ready with timeout."""
        if self._ready_event.is_set():
            if self._load_error is not None:
                raise RuntimeError(f"[Embeddings] Model load failed: {self._load_error}") from self._load_error
            return
        
        if self.tokenizer is not None and self.model is not None:
            self._ready_event.set()
            self._ready = True
            return
        
        if self._load_start_time is None:
            self.auto_load()
        
        start_wait = time.time()
        while not self._ready_event.is_set():
            waited = time.time() - start_wait
            if waited >= timeout:
                break
            self._ready_event.wait(min(5.0, timeout - waited))
        
        if self._load_error is not None:
            raise RuntimeError(f"[Embeddings] Model load failed: {self._load_error}") from self._load_error

        if not self._ready_event.is_set():
            raise RuntimeError(f"[Embeddings] Wait timeout ({timeout}s) - model still not ready")

    def auto_load(self) -> None:
        """Trigger model loading asynchronously in background thread."""
        if not self._ready_event.is_set():
            threading.Thread(target=self.load_model, daemon=True).start()

    def get_load_stats(self) -> dict:
        """Get loading statistics for diagnostics."""
        return {
            "ready": self._ready,
            "device": self.device,
            "model_path": str(self._local_model_path) if self._local_model_path else None,
            "load_error": str(self._load_error) if self._load_error else None,
            "load_time_sec": (
                self._load_end_time - self._load_start_time if self._ready else None
            )
        }

    def encode(self, texts: List[str]) -> List[List[float]]:
        """
        Batch encode list of texts into embedding vectors.
        
        Args:
            texts: List of text strings to encode
            
        Returns:
            List of embedding vectors (dimension depends on model)
        """
        if not texts:
            return []
        
        self.wait_until_ready()
        
        all_vectors = []
        total_batches = (len(texts) + self.batch_size - 1) // self.batch_size
        encode_start_time = time.time()
        input_count = len(texts)
        try:
            total_input_chars = sum(len(t) for t in texts if isinstance(t, str))
        except Exception:
            total_input_chars = 0

        try:
            for i in range(0, len(texts), self.batch_size):
                batch_num = (i // self.batch_size) + 1
                chunk = texts[i : i + self.batch_size]

                # Detect truncation for observability (before the actual encoding)
                for idx, text in enumerate(chunk):
                    raw_len = len(self.tokenizer.encode(text, add_special_tokens=True))
                    if raw_len > self.max_length:
                        self.logger.warning(
                            f"[Embeddings] TRUNCATION: {raw_len} → {self.max_length} tokens "
                            f"(lost {raw_len - self.max_length}). Text preview: {text[:80]}..."
                        )

                tokens = self.tokenizer(
                    chunk,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt"
                ).to(self.device)

                with torch.no_grad():
                    outputs = self.model(**tokens)
                    # Proper mean pooling with attention mask (excludes padding), then L2 normalize.
                    token_embeddings = outputs.last_hidden_state  # [B, T, H]
                    attn = tokens.get("attention_mask")
                    if attn is None:
                        embeddings = token_embeddings.mean(dim=1)
                    else:
                        mask = attn.unsqueeze(-1).expand(token_embeddings.size()).float()
                        summed = torch.sum(token_embeddings * mask, dim=1)
                        counts = torch.clamp(mask.sum(dim=1), min=1e-9)
                        embeddings = summed / counts
                    embeddings = F.normalize(embeddings, p=2, dim=1)

                all_vectors.extend(embeddings.cpu().numpy().tolist())

                if batch_num % 10 == 0 or batch_num == total_batches:
                    elapsed = time.time() - encode_start_time
                    self.logger.debug(
                        f"[Embeddings] Encoded batch {batch_num}/{total_batches} ({elapsed:.1f}s elapsed)"
                    )
        except Exception as e:
            self._log_embedding_batch(
                input_count=input_count,
                total_input_chars=total_input_chars,
                vector_dim=None,
                duration_ms=(time.time() - encode_start_time) * 1000,
                success=False,
                error=e,
            )
            raise

        vector_dim = len(all_vectors[0]) if all_vectors else None
        self._log_embedding_batch(
            input_count=input_count,
            total_input_chars=total_input_chars,
            vector_dim=vector_dim,
            duration_ms=(time.time() - encode_start_time) * 1000,
            success=True,
        )
        return all_vectors

    def _log_embedding_batch(
        self,
        *,
        input_count: int,
        total_input_chars: int,
        vector_dim: Optional[int],
        duration_ms: float,
        success: bool,
        error: Optional[BaseException] = None,
    ) -> None:
        """Emit a server-safe ``ingestion.embedding.batch`` summary + telemetry.

        Never logs vector values. Safe: logging/metrics must not break ingestion.
        """
        model_path = str(self._local_model_path) if self._local_model_path else None
        fields = (
            f"event=ingestion.embedding.batch model_name={self.model_name} "
            f"model_path={model_path} device={self.device} batch_size={self.batch_size} "
            f"input_count={input_count} total_input_chars={total_input_chars} "
            f"vector_dim={vector_dim} duration_ms={round(duration_ms, 2)} success={success}"
        )
        if error is not None:
            fields += f" error_type={type(error).__name__} error_message={str(error)[:200]}"
        try:
            if success:
                self.logger.info(f"ingestion.embedding.batch {fields}")
            else:
                self.logger.error(f"ingestion.embedding.batch {fields}")
        except Exception:
            pass

        try:
            from kbfabric_ingest._common.telemetry import record_embedding_generation

            record_embedding_generation(
                domain="ingestion",
                batch_size=self.batch_size,
                duration_seconds=(duration_ms or 0) / 1000.0,
                count=input_count,
                model=str(self.model_name),
            )
        except Exception:
            pass

    def encode_single(self, text: str) -> List[float]:
        """Encode a single text string."""
        vectors = self.encode([text])
        return vectors[0] if vectors else []

    @property
    def embedding_dimension(self) -> int:
        """Return embedding dimension (from model config when available)."""
        # Priority 1: Model's actual config (runtime)
        if self.model is not None and getattr(self.model, "config", None) is not None:
            hs = getattr(self.model.config, "hidden_size", None)
            if isinstance(hs, int) and hs > 0:
                return hs
        # Priority 2: Configured dimension (YAML)
        if hasattr(self, "_configured_dimension") and isinstance(self._configured_dimension, int):
            return self._configured_dimension
        # Priority 3: Fallback to Arctic default (1024-dim)
        return 1024


# Alias for backward compatibility
FinBERTEmbeddingsClient = EmbeddingsClient


def test_embeddings():
    """Quick test of embeddings functionality."""
    print("=" * 60)
    print("FinBERT Embeddings Client Test")
    print("=" * 60)
    
    client = EmbeddingsClient()
    client.load_model()
    
    test_texts = [
        "The portfolio shows strong ESG compliance metrics.",
        "Risk assessment for derivative instruments requires careful analysis.",
        "Letter of Credit issuance follows UCP 600 guidelines."
    ]
    
    print(f"\nEncoding {len(test_texts)} test texts...")
    vectors = client.encode(test_texts)
    
    print(f"\nResults:")
    for i, (text, vec) in enumerate(zip(test_texts, vectors)):
        print(f"  [{i+1}] Text: {text[:50]}...")
        print(f"      Dimension: {len(vec)}, First 5: {[round(v, 4) for v in vec[:5]]}")
    
    # Test similarity
    vec1 = np.array(vectors[0])
    vec2 = np.array(vectors[1])
    similarity = np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))
    print(f"\nCosine similarity between text 1 and 2: {similarity:.4f}")
    
    stats = client.get_load_stats()
    print(f"\nLoad stats: {stats}")
    print("\n" + "=" * 60)
    print("TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    test_embeddings()


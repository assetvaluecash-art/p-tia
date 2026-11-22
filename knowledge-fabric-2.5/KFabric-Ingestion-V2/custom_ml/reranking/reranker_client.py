# -*- coding: utf-8 -*-
"""
CrossEncoder Reranker Client for QiBa Knowledge Fabric.

ENTERPRISE OFFLINE-ONLY: This client is designed for air-gapped environments.
It enforces HF_HUB_OFFLINE=1 to prevent any network calls to HuggingFace.

Model files must be pre-downloaded and placed in a local models/ folder.
Use download_models.py or enterprise packaging to set up the local model cache.
"""

import os
import sys
import time
import threading
import logging
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

# CRITICAL: Set offline mode BEFORE importing sentence_transformers
# This prevents any attempt to connect to HuggingFace Hub
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(Path(__file__).parent / "models")

from sentence_transformers import CrossEncoder


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


class RerankerClient:
    """
    CrossEncoder reranker for enterprise offline environments.
    
    Uses MS-MARCO MiniLM model for efficient query-document relevance scoring.
    This model is ~80MB and provides fast, accurate reranking.
    
    Features:
    - Singleton pattern for memory efficiency
    - Thread-safe model loading
    - Local-only model loading (no network calls)
    - Batch prediction support
    - Score normalization
    
    Usage:
        client = RerankerClient()
        client.load_model()
        scores = client.predict([("query", "doc1"), ("query", "doc2")])
    """

    _instance: Optional["RerankerClient"] = None
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
        rerank_cfg = config.get("reranking", {})
        
        # Model configuration
        self.model_name = rerank_cfg.get(
            "model_name",
            "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        self.max_length = rerank_cfg.get("max_length", 512)
        self._configured_local_model_path = rerank_cfg.get("local_model_path")
        
        # Local model path resolution (NO HF fallback)
        self._local_model_path = self._resolve_local_model_path()
        
        self.logger = get_logger(__name__)
        
        # Threading and readiness attributes
        self._ready_event = threading.Event()
        self._ready = False
        self._load_start_time: Optional[float] = None
        self._load_end_time: Optional[float] = None
        
        # Model (loaded lazily)
        self.model: Optional[CrossEncoder] = None
        
        self._initialized = True
        
        self.logger.info(f"[CrossEncoder] Client initialized - max_length={self.max_length}")

    def _resolve_local_model_path(self) -> Optional[Path]:
        """
        Resolve local model path in order of priority (enterprise offline):
        1) KBFABRIC_RERANKER_MODEL_DIR env var (preferred)
        2) reranking.local_model_path (YAML)
        3) Bundled model in custom_ml/reranking/models/<model_dirname>/ (process convenience)

        IMPORTANT: There is intentionally NO HuggingFace cache / model-id fallback.
        """
        candidates: list[Path] = []

        env_dir = (os.environ.get("KBFABRIC_RERANKER_MODEL_DIR") or "").strip()
        if env_dir:
            candidates.append(Path(env_dir).expanduser())

        cfg_dir = (self._configured_local_model_path or "").strip() if isinstance(self._configured_local_model_path, str) else ""
        if cfg_dir:
            candidates.append(Path(cfg_dir).expanduser())

        module_dir = Path(__file__).parent
        model_dirname = None
        try:
            if isinstance(self._configured_local_model_path, str) and self._configured_local_model_path.strip():
                model_dirname = Path(self._configured_local_model_path.strip()).name
        except Exception:
            model_dirname = None
        if not model_dirname:
            model_dirname = (str(self.model_name).split("/")[-1] or "model").strip() or "model"
        candidates.append(module_dir / "models" / model_dirname)

        for p in candidates:
            try:
                if p.exists() and self._check_model_files_at(p):
                    return p
            except Exception:
                continue

        return None

    def _check_model_files_at(self, model_dir: Path) -> bool:
        """Check if required model files are present in the given directory."""
        required_files = ["config.json"]
        model_files = ["model.safetensors", "pytorch_model.bin"]

        for f in required_files:
            if not (model_dir / f).exists():
                return False

        has_model = any((model_dir / f).exists() for f in model_files)
        return has_model

    def load_model(self) -> None:
        """
        Load CrossEncoder model with progress tracking.
        
        This method is thread-safe and will only load the model once.
        Uses local model path to prevent any network calls.
        """
        with self._lock:
            if self._ready:
                self.logger.info("[CrossEncoder] Model already loaded, skipping")
                return
            
            if self.model is not None:
                self.logger.info("[CrossEncoder] Model already exists, marking as ready")
                self._ready_event.set()
                self._ready = True
                return
            
            self.logger.info("[CrossEncoder] Starting model load (OFFLINE MODE)")
            print("[CrossEncoder] Starting model load (OFFLINE MODE)...", flush=True)
            self._load_start_time = time.time()
            
            # Determine model source (local-only)
            if self._local_model_path and self._check_model_files_at(self._local_model_path):
                model_source = str(self._local_model_path)
                self.logger.info(f"[CrossEncoder] Using local model dir: {model_source}")
                print(f"[CrossEncoder] Using local model dir: {model_source}", flush=True)
            else:
                env_dir = (os.environ.get("KBFABRIC_RERANKER_MODEL_DIR") or "").strip() or None
                cfg_dir = (self._configured_local_model_path or "").strip() if isinstance(self._configured_local_model_path, str) else None
                bundled_parent = str((Path(__file__).parent / "models").resolve())
                raise RuntimeError(
                    "[CrossEncoder] Local model directory not found or incomplete. "
                    "Enterprise mode forbids HuggingFace cache/model-id fallback.\n"
                    f"- Env KBFABRIC_RERANKER_MODEL_DIR: {env_dir}\n"
                    f"- Config reranking.local_model_path: {cfg_dir}\n"
                    f"- Bundled fallback base dir: {bundled_parent}\n"
                    "Required: config.json + (model.safetensors|pytorch_model.bin)"
                )
            
            try:
                # Load the CrossEncoder
                print("[CrossEncoder] Loading model...", flush=True)
                self.model = CrossEncoder(model_source, max_length=self.max_length)
                
                # Mark as ready
                self._ready_event.set()
                self._ready = True
                self._load_end_time = time.time()
                total_time = self._load_end_time - self._load_start_time
                
                self.logger.info(f"[CrossEncoder] Model loaded successfully in {total_time:.2f}s")
                print(f"[CrossEncoder] Model loaded successfully in {total_time:.2f}s", flush=True)
                
            except Exception as e:
                error_msg = f"[CrossEncoder] Model loading failed: {str(e)}"
                self.logger.error(error_msg)
                print(f"[CrossEncoder] ERROR: {error_msg}", flush=True)
                raise RuntimeError(
                    f"Failed to load CrossEncoder model. Ensure model files exist at "
                    f"{self._local_model_path or '~/.cache/'}. "
                    f"Run 'python -m custom_ml.download_models' to set up. Error: {e}"
                )

    def wait_until_ready(self, timeout: int = 300) -> None:
        """Wait for model to be ready with timeout."""
        if self._ready_event.is_set():
            return
        
        if self.model is not None:
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
        
        if not self._ready_event.is_set():
            raise RuntimeError(f"[CrossEncoder] Wait timeout ({timeout}s) - model still not ready")

    def auto_load(self) -> None:
        """Trigger model loading asynchronously in background thread."""
        if not self._ready_event.is_set():
            threading.Thread(target=self.load_model, daemon=True).start()

    def get_load_stats(self) -> dict:
        """Get loading statistics for diagnostics."""
        return {
            "ready": self._ready,
            "model_path": str(self._local_model_path) if self._local_model_path else None,
            "load_time_sec": (
                self._load_end_time - self._load_start_time if self._ready else None
            )
        }

    def predict(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """
        Predict relevance scores for query-document pairs.
        
        Args:
            pairs: List of (query, document) tuples
            
        Returns:
            List of relevance scores (higher = more relevant)
        """
        if not pairs:
            return []
        
        self.wait_until_ready()
        
        scores = self.model.predict(pairs)
        return [float(s) for s in scores]

    def predict_normalized(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """
        Predict and normalize relevance scores to [0, 1] range.
        
        Args:
            pairs: List of (query, document) tuples
            
        Returns:
            List of normalized relevance scores
        """
        scores = self.predict(pairs)
        
        if not scores:
            return []
        
        s_min, s_max = min(scores), max(scores)
        if s_max > s_min:
            return [(s - s_min) / (s_max - s_min) for s in scores]
        else:
            return [0.5] * len(scores)

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None
    ) -> List[Tuple[int, str, float]]:
        """
        Rerank documents by relevance to query.
        
        Args:
            query: Query string
            documents: List of document texts
            top_k: Optional limit on results (default: all)
            
        Returns:
            List of (original_index, document, score) tuples, sorted by score descending
        """
        if not documents:
            return []
        
        pairs = [(query, doc) for doc in documents]
        scores = self.predict_normalized(pairs)
        
        # Create (index, doc, score) tuples and sort
        results = [(i, doc, score) for i, (doc, score) in enumerate(zip(documents, scores))]
        results.sort(key=lambda x: x[2], reverse=True)
        
        if top_k is not None:
            results = results[:top_k]
        
        return results


# Alias for backward compatibility
CrossEncoderReranker = RerankerClient


def test_reranking():
    """Quick test of reranking functionality."""
    print("=" * 60)
    print("CrossEncoder Reranker Client Test")
    print("=" * 60)
    
    client = RerankerClient()
    client.load_model()
    
    query = "What are the ESG compliance requirements for portfolio rebalancing?"
    
    documents = [
        "The weather today is sunny with a chance of rain.",
        "ESG rebalancing requires compliance with environmental, social, and governance standards.",
        "Portfolio optimization algorithms use mean-variance analysis.",
        "ESG compliance for portfolio management includes carbon footprint assessment.",
        "The stock market closed higher today on positive earnings reports.",
        "Rebalancing triggers when drift exceeds threshold bands defined in client profiles."
    ]
    
    print(f"\nQuery: {query}")
    print(f"\nReranking {len(documents)} documents...")
    
    results = client.rerank(query, documents)
    
    print(f"\nReranked Results:")
    for rank, (orig_idx, doc, score) in enumerate(results, 1):
        print(f"  #{rank} [Score: {score:.4f}] (orig #{orig_idx+1})")
        print(f"      {doc[:70]}...")
    
    stats = client.get_load_stats()
    print(f"\nLoad stats: {stats}")
    print("\n" + "=" * 60)
    print("TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    test_reranking()


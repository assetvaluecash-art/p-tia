# -*- coding: utf-8 -*-
"""
Model Download/Setup Script for QiBa Knowledge Fabric.

This script sets up the local model cache for offline operation.
It copies model files from the HuggingFace cache to the bundled model folders.

When running from Windows, it checks BOTH:
  - Windows cache: %USERPROFILE%\\.cache\\huggingface
  - WSL cache: \\\\wsl$\\Ubuntu\\home\\<user>\\.cache\\huggingface

Usage:
    Use the wrapper scripts for your platform:
    
    Windows:  .\\custom_ml\\download_and_setup.bat
    Linux:    ./custom_ml/download_and_setup.sh

    Or run directly:
    
    Windows:  set HF_CACHE_PATH=%USERPROFILE%\\.cache\\huggingface
              set WSL_HF_CACHE_PATH=\\\\wsl$\\Ubuntu\\home\\ashwinp\\.cache\\huggingface
              python -m custom_ml.download_models
    
    Linux:    export HF_CACHE_PATH=$HOME/.cache/huggingface
              python -m custom_ml.download_models

Environment variables:
    HF_CACHE_PATH: Primary HuggingFace cache location
    WSL_HF_CACHE_PATH: WSL HuggingFace cache (Windows only, optional)
    WSL_TORCH_CACHE_PATH: WSL Torch cache for sentence_transformers (Windows only)
"""

import os
import sys
import shutil
import platform
from pathlib import Path
from typing import Optional, List


# ============================================================================
# Configuration
# ============================================================================

def is_windows() -> bool:
    """Check if running on Windows."""
    return platform.system() == "Windows"


def get_hf_cache_paths() -> List[Path]:
    """
    Get list of HuggingFace cache paths to check.
    
    When on Windows, checks both Windows cache and WSL cache.
    The wrapper scripts set the appropriate environment variables.
    """
    paths = []
    
    # Primary cache from environment (Windows user cache)
    if os.environ.get("HF_CACHE_PATH"):
        win_path = Path(os.environ["HF_CACHE_PATH"])
        if win_path.exists():
            paths.append(win_path)
            print(f"  [INFO] Windows HF cache: {win_path}")
        else:
            print(f"  [INFO] Windows HF cache not found: {win_path}")
    
    # WSL cache (Windows only) - check if accessible
    if os.environ.get("WSL_HF_CACHE_PATH"):
        wsl_path = Path(os.environ["WSL_HF_CACHE_PATH"])
        try:
            if wsl_path.exists():
                paths.append(wsl_path)
                print(f"  [INFO] WSL HF cache: {wsl_path}")
            else:
                print(f"  [INFO] WSL HF cache not accessible: {wsl_path}")
        except Exception as e:
            print(f"  [WARN] Error checking WSL cache: {e}")
    
    # Platform defaults if no env vars set
    if not paths:
        default_cache = Path.home() / ".cache" / "huggingface"
        if default_cache.exists():
            paths.append(default_cache)
        print(f"  [INFO] Using default cache: {default_cache}")
    
    return paths


def get_torch_cache_paths() -> List[Path]:
    """
    Get list of Torch cache paths to check for sentence_transformers.
    
    CrossEncoder models from sentence_transformers are cached here.
    """
    paths = []
    
    # WSL torch cache (Windows only)
    if os.environ.get("WSL_TORCH_CACHE_PATH"):
        wsl_path = Path(os.environ["WSL_TORCH_CACHE_PATH"]) / "sentence_transformers"
        try:
            if wsl_path.exists():
                paths.append(wsl_path)
                print(f"  [INFO] WSL Torch cache: {wsl_path}")
        except Exception as e:
            print(f"  [WARN] Error checking WSL torch cache: {e}")
    
    # Windows/Linux default
    default_cache = Path.home() / ".cache" / "torch" / "sentence_transformers"
    if default_cache.exists():
        paths.append(default_cache)
        print(f"  [INFO] Torch cache: {default_cache}")
    
    return paths


def find_finbert_model(hf_caches: List[Path]) -> Optional[Path]:
    """Find the FinBERT model in any of the cache locations."""
    
    for hf_cache in hf_caches:
        print(f"  [DEBUG] Searching in: {hf_cache}")
        
        # Hub cache location (newer transformers)
        hub_path = hf_cache / "hub" / "models--ProsusAI--finbert"
        if hub_path.exists():
            print(f"  [DEBUG] Checking hub cache: {hub_path}")
            snapshots = hub_path / "snapshots"
            if snapshots.exists():
                result = _find_best_snapshot(snapshots)
                if result:
                    return result
        
        # Transformers cache location (older transformers - flat structure)
        transformers_path = hf_cache / "transformers" / "models--ProsusAI--finbert"
        if transformers_path.exists():
            print(f"  [DEBUG] Checking transformers cache: {transformers_path}")
            # Check for direct model files (flat structure)
            config_file = transformers_path / "config.json"
            if config_file.exists():
                print(f"  [DEBUG] Found model with config.json: {transformers_path}")
                return transformers_path
            # Also check snapshots subfolder
            snapshots = transformers_path / "snapshots"
            if snapshots.exists():
                result = _find_best_snapshot(snapshots)
                if result:
                    return result
    
    return None


def _find_best_snapshot(snapshots_dir: Path) -> Optional[Path]:
    """Find the best snapshot directory (newest with config.json)."""
    try:
        snapshot_dirs = [d for d in snapshots_dir.iterdir() if d.is_dir()]
        # Sort by modification time (newest first)
        snapshot_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for snapshot in snapshot_dirs:
            config_file = snapshot / "config.json"
            if config_file.exists():
                print(f"  [DEBUG] Found snapshot with config.json: {snapshot}")
                return snapshot
    except Exception as e:
        print(f"  [WARN] Error scanning snapshots: {e}")
    return None


def find_crossencoder_model(hf_caches: List[Path], torch_caches: List[Path]) -> Optional[Path]:
    """Find the CrossEncoder model in any of the cache locations."""
    
    model_folder = "cross-encoder_ms-marco-MiniLM-L-6-v2"
    
    # Check sentence_transformers cache (in torch folder)
    for torch_cache in torch_caches:
        st_path = torch_cache / model_folder
        print(f"  [DEBUG] Checking sentence_transformers cache: {st_path}")
        if st_path.exists():
            config_file = st_path / "config.json"
            if config_file.exists():
                print(f"  [DEBUG] Found model with config.json: {st_path}")
                return st_path
    
    # Check HuggingFace hub cache
    for hf_cache in hf_caches:
        hub_path = hf_cache / "hub" / "models--cross-encoder--ms-marco-MiniLM-L-6-v2"
        print(f"  [DEBUG] Checking hub cache: {hub_path}")
        if hub_path.exists():
            snapshots = hub_path / "snapshots"
            if snapshots.exists():
                result = _find_best_snapshot(snapshots)
                if result:
                    return result
    
    return None


def copy_model_files(src_dir: Path, dest_dir: Path, model_name: str) -> bool:
    """Copy model files from source to destination."""
    if not src_dir.exists():
        print(f"  ERROR: Source directory does not exist: {src_dir}")
        return False
    
    # Create destination directory
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    # Essential files to copy
    essential_files = [
        "config.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "vocab.txt",
        "special_tokens_map.json",
        "model.safetensors",
        "pytorch_model.bin",
    ]
    
    copied_count = 0
    total_size = 0
    
    # Copy essential files
    for filename in essential_files:
        src_file = src_dir / filename
        if src_file.exists():
            dest_file = dest_dir / filename
            try:
                shutil.copy2(src_file, dest_file)
                file_size = src_file.stat().st_size
                total_size += file_size
                copied_count += 1
                print(f"    Copied: {filename} ({file_size / 1024 / 1024:.2f} MB)")
            except Exception as e:
                print(f"    ERROR copying {filename}: {e}")
    
    # Also copy any other .json or .txt files that might be needed
    try:
        for src_file in src_dir.glob("*.json"):
            if src_file.name not in essential_files:
                dest_file = dest_dir / src_file.name
                if not dest_file.exists():
                    shutil.copy2(src_file, dest_file)
                    copied_count += 1
                    print(f"    Copied: {src_file.name}")
        
        for src_file in src_dir.glob("*.txt"):
            if src_file.name not in essential_files:
                dest_file = dest_dir / src_file.name
                if not dest_file.exists():
                    shutil.copy2(src_file, dest_file)
                    copied_count += 1
                    print(f"    Copied: {src_file.name}")
    except Exception as e:
        print(f"    WARN: Error copying additional files: {e}")
    
    print(f"  Total: {copied_count} files, {total_size / 1024 / 1024:.2f} MB")
    return copied_count > 0


def verify_model(model_dir: Path, model_name: str) -> bool:
    """Verify that a model has all required files."""
    required_files = ["config.json"]
    model_files = ["model.safetensors", "pytorch_model.bin"]
    
    if not model_dir.exists():
        print(f"  MISSING: {model_dir} does not exist")
        return False
    
    # Check required files
    for f in required_files:
        if not (model_dir / f).exists():
            print(f"  MISSING: {f}")
            return False
    
    # Check at least one model file
    has_model = any((model_dir / f).exists() for f in model_files)
    if not has_model:
        print(f"  MISSING: No model weights file found (need model.safetensors or pytorch_model.bin)")
        return False
    
    print(f"  OK: {model_name} is properly set up")
    return True


def main():
    """Main entry point for model setup."""
    print("=" * 70)
    print("QiBa Knowledge Fabric - Model Setup Script")
    print("=" * 70)
    print()
    
    # Platform detection
    print(f"Platform: {platform.system()}")
    print()
    
    # Determine target paths
    script_dir = Path(__file__).parent
    embeddings_models_dir = script_dir / "embeddings" / "models" / "finbert"
    reranking_models_dir = script_dir / "reranking" / "models" / "cross-encoder-ms-marco"
    
    print(f"Script directory: {script_dir}")
    print(f"FinBERT target: {embeddings_models_dir}")
    print(f"CrossEncoder target: {reranking_models_dir}")
    print()
    
    # Get all cache paths to check
    print("Discovering cache locations...")
    hf_caches = get_hf_cache_paths()
    torch_caches = get_torch_cache_paths()
    print()
    
    if not hf_caches:
        print("  [WARN] No HuggingFace cache paths found!")
    
    # Track success
    finbert_ok = False
    crossencoder_ok = False
    
    # ===== FinBERT Setup =====
    print("-" * 70)
    print("1. FinBERT (ProsusAI/finbert)")
    print("-" * 70)
    
    # Check if already set up
    if verify_model(embeddings_models_dir, "FinBERT"):
        finbert_ok = True
    else:
        # Find source in any cache
        finbert_src = find_finbert_model(hf_caches)
        if finbert_src:
            print(f"  Found in cache: {finbert_src}")
            print("  Copying to bundled location...")
            if copy_model_files(finbert_src, embeddings_models_dir, "FinBERT"):
                finbert_ok = verify_model(embeddings_models_dir, "FinBERT")
        else:
            print("  NOT FOUND in any cache!")
            print()
            print("  Run the download script for your platform:")
            print("    Windows: .\\custom_ml\\download_and_setup.bat")
            print("    Linux:   ./custom_ml/download_and_setup.sh")
    
    print()
    
    # ===== CrossEncoder Setup =====
    print("-" * 70)
    print("2. CrossEncoder (cross-encoder/ms-marco-MiniLM-L-6-v2)")
    print("-" * 70)
    
    # Check if already set up
    if verify_model(reranking_models_dir, "CrossEncoder"):
        crossencoder_ok = True
    else:
        # Find source in any cache
        crossencoder_src = find_crossencoder_model(hf_caches, torch_caches)
        if crossencoder_src:
            print(f"  Found in cache: {crossencoder_src}")
            print("  Copying to bundled location...")
            if copy_model_files(crossencoder_src, reranking_models_dir, "CrossEncoder"):
                crossencoder_ok = verify_model(reranking_models_dir, "CrossEncoder")
        else:
            print("  NOT FOUND in any cache!")
            print()
            print("  Run the download script for your platform:")
            print("    Windows: .\\custom_ml\\download_and_setup.bat")
            print("    Linux:   ./custom_ml/download_and_setup.sh")
    
    print()
    
    # ===== Summary =====
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    
    status = []
    if finbert_ok:
        status.append("FinBERT: OK")
    else:
        status.append("FinBERT: NEEDS SETUP")
    
    if crossencoder_ok:
        status.append("CrossEncoder: OK")
    else:
        status.append("CrossEncoder: NEEDS SETUP")
    
    for s in status:
        print(f"  {s}")
    
    print()
    
    if finbert_ok and crossencoder_ok:
        print("All models are set up and ready for offline use!")
        print()
        print("Next steps - copy to subprojects:")
        print("  1. Copy custom_ml/embeddings/models/finbert/ to kbfabric-ingest/custom_ml/embeddings/models/")
        print("  2. Copy custom_ml/embeddings/models/finbert/ to kbfabric-retrieval/custom_ml/embeddings/models/")
        print("  3. Copy custom_ml/reranking/models/cross-encoder-ms-marco/ to kbfabric-retrieval/custom_ml/reranking/models/")
        print()
        print("Test with (from WSL):")
        print("  python -m custom_ml.embeddings.test_embeddings \"your query\"")
        print("  python -m custom_ml.reranking.test_reranking")
        return 0
    else:
        print("Some models need to be downloaded.")
        print()
        print("Run the download script for your platform:")
        print("  Windows: .\\custom_ml\\download_and_setup.bat")
        print("  Linux:   ./custom_ml/download_and_setup.sh")
        return 1


if __name__ == "__main__":
    sys.exit(main())

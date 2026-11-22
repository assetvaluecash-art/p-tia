#!/bin/bash
# ============================================================================
# QiBa Knowledge Fabric - all-mpnet-base-v2 Download and Bundle Script (Linux/WSL)
# ============================================================================
#
# Downloads the Apache-2.0 embedding model:
#   sentence-transformers/all-mpnet-base-v2
# and bundles it into:
#   kbfabric-ingest/custom_ml/embeddings/models/all-mpnet-base-v2
#
# Usage:
#   ./custom_ml/download_and_setup_mpnet.sh
#
# Optional (if your venv is not auto-detected):
#   VENV_ACTIVATE=/path/to/.venv/bin/activate ./custom_ml/download_and_setup_mpnet.sh
#
# ============================================================================

set -e  # Exit on error

echo "======================================================================"
echo "QiBa Knowledge Fabric - all-mpnet-base-v2 Download and Bundle"
echo "======================================================================"
echo ""

# Change to the kbfabric-ingest directory (parent of custom_ml)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
echo "Working directory: $(pwd)"
echo ""

# HuggingFace cache path for Linux/WSL
export HF_CACHE_PATH="$HOME/.cache/huggingface"
export HF_HUB_DISABLE_TELEMETRY=1
echo "HuggingFace cache: $HF_CACHE_PATH"
echo ""

# Optional: activate venv if present (override with VENV_ACTIVATE)
if [ -n "${VENV_ACTIVATE:-}" ] && [ -f "$VENV_ACTIVATE" ]; then
  echo "Activating venv: $VENV_ACTIVATE"
  # shellcheck disable=SC1090
  source "$VENV_ACTIVATE"
elif [ -f "./.venv/bin/activate" ]; then
  echo "Activating venv: $(pwd)/.venv/bin/activate"
  # shellcheck disable=SC1091
  source "./.venv/bin/activate"
elif [ -f "../.venv/bin/activate" ]; then
  echo "Activating venv: $(cd .. && pwd)/.venv/bin/activate"
  # shellcheck disable=SC1091
  source "../.venv/bin/activate"
else
  echo "No venv auto-activated; using system python3"
fi

echo ""

MODEL_ID="sentence-transformers/all-mpnet-base-v2"
DEST_DIR="$(pwd)/custom_ml/embeddings/models/all-mpnet-base-v2"

echo "Model: $MODEL_ID"
echo "Destination: $DEST_DIR"
echo ""

if [ -f "$DEST_DIR/modules.json" ]; then
  echo "Found existing bundled model. modules.json present. Skipping download."
  exit 0
fi

echo "----------------------------------------------------------------------"
echo "Downloading and bundling model (this can take a few minutes)..."
echo "----------------------------------------------------------------------"
echo ""

python3 -c "from sentence_transformers import SentenceTransformer; import os; mid='$MODEL_ID'; dest='$DEST_DIR'; print(f'[INFO] Loading: {mid}'); m=SentenceTransformer(mid); os.makedirs(dest, exist_ok=True); print(f'[INFO] Saving to: {dest}'); m.save(dest); print('[INFO] Done.')"

echo ""
echo "======================================================================"
echo "Done!"
echo "======================================================================"
echo ""


#!/bin/bash
# ============================================================================
# QiBa Knowledge Fabric - Model Download and Setup Script (Linux/WSL)
# ============================================================================
#
# This script downloads ML models and sets them up for offline use.
# Run this from the kbfabric root directory.
#
# Usage:
#     ./custom_ml/download_and_setup.sh
#
# Or from WSL accessing Windows path:
#     /mnt/c/data/qiba_pulse_knowledge_fabric/kbfabric/custom_ml/download_and_setup.sh
#
# ============================================================================

set -e  # Exit on error

echo "======================================================================"
echo "QiBa Knowledge Fabric - Model Download and Setup"
echo "======================================================================"
echo ""

# Change to the kbfabric directory (parent of custom_ml)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
echo "Working directory: $(pwd)"
echo ""

# Set the HuggingFace cache path for Linux/WSL
export HF_CACHE_PATH="$HOME/.cache/huggingface"
echo "HuggingFace cache: $HF_CACHE_PATH"
echo ""

# ============================================================================
# Step 1: Download FinBERT
# ============================================================================
echo "----------------------------------------------------------------------"
echo "[1/3] Downloading FinBERT (ProsusAI/finbert)..."
echo "----------------------------------------------------------------------"
echo ""

python3 -c "
from transformers import AutoModel, AutoTokenizer
print('Downloading FinBERT model...')
AutoModel.from_pretrained('ProsusAI/finbert')
print('Downloading FinBERT tokenizer...')
AutoTokenizer.from_pretrained('ProsusAI/finbert')
print('FinBERT download complete!')
"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Failed to download FinBERT. Check your internet connection."
    exit 1
fi
echo ""

# ============================================================================
# Step 2: Download CrossEncoder
# ============================================================================
echo "----------------------------------------------------------------------"
echo "[2/3] Downloading CrossEncoder (ms-marco-MiniLM-L-6-v2)..."
echo "----------------------------------------------------------------------"
echo ""

python3 -c "
from sentence_transformers import CrossEncoder
print('Downloading CrossEncoder model...')
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
print('CrossEncoder download complete!')
"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Failed to download CrossEncoder. Check your internet connection."
    exit 1
fi
echo ""

# ============================================================================
# Step 3: Setup bundled models
# ============================================================================
echo "----------------------------------------------------------------------"
echo "[3/3] Setting up bundled models..."
echo "----------------------------------------------------------------------"
echo ""

python3 -m custom_ml.download_models

if [ $? -ne 0 ]; then
    echo ""
    echo "WARNING: Model setup had issues. Check output above."
fi

echo ""
echo "======================================================================"
echo "Done!"
echo "======================================================================"
echo ""


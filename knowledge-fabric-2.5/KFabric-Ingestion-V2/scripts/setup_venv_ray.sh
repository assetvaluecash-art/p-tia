#!/bin/bash
# Create .venv_ray for Ray job workers (Python 3.11, offline from pip_deps)
# Run from project root.
# Usage: ./scripts/setup_venv_ray.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

REQUIREMENTS="ray_job_requirements.txt"
PIP_DEPS="pip_deps"
VENV_DIR=".venv_ray"

if [ ! -f "$REQUIREMENTS" ]; then
    echo "[ERROR] $REQUIREMENTS not found in $PROJECT_DIR"
    exit 1
fi

if [ ! -d "$PIP_DEPS" ]; then
    echo "[ERROR] $PIP_DEPS not found. Create it with:"
    echo "  pip download -r $REQUIREMENTS -d $PIP_DEPS"
    exit 1
fi

if [ -d "$VENV_DIR" ]; then
    echo "[INFO] $VENV_DIR exists. Recreating..."
    rm -rf "$VENV_DIR"
fi

echo "[INFO] Creating $VENV_DIR with Python 3.11..."
python3.11 -m venv "$VENV_DIR"

echo "[INFO] Installing from $PIP_DEPS (offline)..."
"$VENV_DIR/bin/pip" install --no-index --find-links "./$PIP_DEPS" -r "$REQUIREMENTS"

echo "[INFO] Verifying..."
"$VENV_DIR/bin/python" -c "import ray; print('Ray:', ray.__version__)"
"$VENV_DIR/bin/python" -c "import torch; print('PyTorch:', torch.__version__)"

echo "[OK] .venv_ray ready. Ray workers will use: $PROJECT_DIR/$VENV_DIR/bin/python"

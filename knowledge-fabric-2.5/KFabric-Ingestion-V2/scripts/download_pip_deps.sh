#!/bin/bash
# Download pip_deps for offline Ray worker install.
# Uses PyTorch CPU index first (torch+cpu ~188MB) - else PyPI returns full CUDA torch (~915MB).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

REQ_FILE="ray_job_requirements.lock"
if [ ! -f "$REQ_FILE" ]; then
  REQ_FILE="ray_job_requirements.txt"
  echo "[WARN] ray_job_requirements.lock not found. Using .txt (may backtrack)."
  echo "[INFO] Run ./scripts/compile_ray_requirements.sh once to create a lock file."
fi

echo "[INFO] Downloading pip_deps (PyTorch CPU + PyPI) from $REQ_FILE ..."

pip download --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host download.pytorch.org --trusted-host github.com --trusted-host release-assets.githubusercontent.com --trusted-host raw.githubusercontent.com -r "$REQ_FILE" -d ./pip_deps

echo "[OK] pip_deps ready. Run docker exec to create .venv_ray inside the Ray container."

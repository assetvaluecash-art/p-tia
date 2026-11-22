#!/bin/bash
# Compile ray_job_requirements.txt to a fully pinned lock file.
# Run once when changing dependencies; eliminates pip backtracking.
# Requires: pip-tools (pip install pip-tools)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v pip-compile &>/dev/null; then
  echo "[ERROR] pip-compile not found. Install: pip install pip-tools"
  exit 1
fi

echo "[INFO] Compiling ray_job_requirements.txt -> ray_job_requirements.lock"
echo "[INFO] PyTorch CPU index first (else pip downloads 915MB CUDA torch)"
echo "[INFO] This may take 5-15 minutes on first run..."

pip-compile \
  --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple \
  --trusted-host pypi.org \
  --trusted-host files.pythonhosted.org \
  --trusted-host download.pytorch.org \
  --trusted-host github.com \
  --trusted-host release-assets.githubusercontent.com \
  --trusted-host raw.githubusercontent.com \
  --output-file ray_job_requirements.lock \
  ray_job_requirements.txt

echo "[OK] ray_job_requirements.lock created. Run ./scripts/download_pip_deps.sh"

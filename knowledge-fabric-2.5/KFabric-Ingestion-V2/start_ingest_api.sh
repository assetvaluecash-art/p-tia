#!/bin/bash
set -euo pipefail

# KnowledgeFabric Ingestion API - PRODUCTION Startup Script
# For customer deployment. Use start_ingest_api_dev.sh for local development.
# Usage: ./start_ingest_api.sh [--tmux] [--host <IP>] [--port <PORT>]

USE_TMUX=false
HOST="0.0.0.0"
PORT="6002"
while [ $# -gt 0 ]; do
    case "$1" in
        --tmux)
            USE_TMUX=true
            ;;
        --host)
            shift
            HOST="${1:-}"
            ;;
        --port)
            shift
            PORT="${1:-}"
            ;;
        *)
            echo "[ERROR] Unknown argument: $1"
            echo "Usage: ./start_ingest_api.sh [--tmux] [--host <IP>] [--port <PORT>]"
            exit 1
            ;;
    esac
    shift
done

# Validate port (1..65535)
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "[ERROR] Invalid port: $PORT (must be 1..65535)"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Load environment from repo-root ``env`` (non-hidden). See env.example.
if [ -f "$REPO_ROOT/env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_ROOT/env"
    set +a
fi

PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
[ ! -x "$PYTHON_BIN" ] && PYTHON_BIN="$(command -v python3 || command -v python)"

echo "=========================================="
echo "Pulse Knowledge Fabric - Ingestion API (PROD)"
echo "=========================================="
echo "Project: $PROJECT_DIR"
echo "Port: $PORT"
echo "Docs: http://$HOST:$PORT/docs"
echo "=========================================="

# Activate virtual environment if needed
if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ ! -d ".venv" ]; then
        echo "Creating virtual environment..."
        python3.11 -m venv .venv
    fi
    echo "Activating virtual environment and installing dependencies..."
    source .venv/bin/activate
else
    echo "Using existing virtual environment: $VIRTUAL_ENV"
fi
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host github.com --trusted-host release-assets.githubusercontent.com --trusted-host raw.githubusercontent.com --upgrade pip
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host github.com --trusted-host release-assets.githubusercontent.com --trusted-host raw.githubusercontent.com -r requirements.txt 

# PROD paths - auto-detect from script location (works for kbfabric-ingest, kbfabric-ingestv2, etc.)
export PYTHONPATH="$PROJECT_DIR"
export RAY_DASHBOARD_URL="${RAY_DASHBOARD_URL:-http://localhost:8266}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8080}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
export KBFABRIC_EMBEDDINGS_MODEL_DIR="${KBFABRIC_EMBEDDINGS_MODEL_DIR:-$PROJECT_DIR/custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0}"
export KBFABRIC_INGEST_PERSIST_DIR="${KBFABRIC_INGEST_PERSIST_DIR:-$PROJECT_DIR}"
export RAY_WORKER_DIR="${RAY_WORKER_DIR:-/app/kbfabric-ingest}"
export RAY_RUNTIME_ENV_TEMPORARY_REFERENCE_EXPIRATION_S="${RAY_RUNTIME_ENV_TEMPORARY_REFERENCE_EXPIRATION_S:-1800}"

echo "Environment variables (PROD):"
echo "  RAY_DASHBOARD_URL: $RAY_DASHBOARD_URL"
echo "  OLLAMA_BASE_URL: $OLLAMA_BASE_URL"
echo "  WEAVIATE_URL: $WEAVIATE_URL"
echo "  NEO4J_URI: $NEO4J_URI"
echo "  KBFABRIC_EMBEDDINGS_MODEL_DIR: $KBFABRIC_EMBEDDINGS_MODEL_DIR"
echo "  KBFABRIC_INGEST_PERSIST_DIR: $KBFABRIC_INGEST_PERSIST_DIR"
echo "  RAY_WORKER_DIR: $RAY_WORKER_DIR"
echo ""

if [ "$USE_TMUX" = true ]; then
    echo "Starting ingestion API service in tmux background session..."
    tmux new-session -d -s "kbfabric-ingest-api" "cd $PROJECT_DIR && $PYTHON_BIN -m uvicorn kbfabric_ingest.scheduler.api:app --host $HOST --port $PORT"
    echo "Service started in tmux session: kbfabric-ingest-api"
    echo "Attach with: tmux attach-session -t kbfabric-ingest-api"
    echo "Detach with: Ctrl+B, D"
else
    echo "Starting ingestion API service in background..."
    nohup "$PYTHON_BIN" -m uvicorn kbfabric_ingest.scheduler.api:app \
        --host "$HOST" --port "$PORT" \
        > ingest_api.log 2>&1 &
    echo "Service started in background (PID: $!)"
    echo "Logs: tail -f ingest_api.log"
fi

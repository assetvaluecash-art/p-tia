#!/bin/bash
set -euo pipefail

# KnowledgeFabric Ingestion API - DEV Startup Script
# For local/WSL development. Use start_ingest_api.sh for production.
# Usage: ./start_ingest_api_dev.sh [--tmux] [--host <IP>] [--port <PORT>] [--no-ray]
#
# With --no-ray: skip Ray container check/start (use when Ray runs elsewhere or you only need API).

USE_TMUX=false
HOST="0.0.0.0"
PORT="6002"
SKIP_RAY=false
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
        --no-ray)
            SKIP_RAY=true
            ;;
        *)
            echo "[ERROR] Unknown argument: $1"
            echo "Usage: ./start_ingest_api_dev.sh [--tmux] [--host <IP>] [--port <PORT>] [--no-ray]"
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

# WSL 1 + Docker Desktop: ensure docker client can reach daemon
export DOCKER_HOST="${DOCKER_HOST:-tcp://localhost:2375}"

# Local Ray uses port 6001 (avoids conflict with SSH tunnel to server on 8266)
RAY_DASHBOARD_PORT="${RAY_DASHBOARD_PORT:-6001}"

# Ensure Ray container is running (for Docker-based local dev)
# Ray: background (docker -d). API: foreground below.
RAY_CONTAINER_PATH="/app/kbfabric-ingest"
if [ "$SKIP_RAY" = false ]; then
    if ! docker ps -q -f name=ray-core 2>/dev/null | grep -q .; then
        echo "[DEV] Ray container not running. Starting ray-core (background)..."
        VOLUME_PATH="$(wslpath -w "$(pwd)" 2>/dev/null || pwd)"
        docker run -d \
            --name ray-core \
            --shm-size=6g \
            -p "${RAY_DASHBOARD_PORT}:8266" \
            -p 10001:10001 \
            -v "${VOLUME_PATH}:/app/kbfabric-ingest" \
            ray-ingest:2.9.3-py311-cpu \
            ray start --head --dashboard-host=0.0.0.0 --dashboard-port=8266 --block
        echo "[DEV] Ray container started. Dashboard: http://localhost:${RAY_DASHBOARD_PORT}"
    else
        echo "[DEV] Ray container already running."
    fi
    export KBFABRIC_INGEST_PERSIST_DIR="${KBFABRIC_INGEST_PERSIST_DIR:-$PROJECT_DIR}"
    export RAY_WORKER_DIR="${RAY_WORKER_DIR:-$RAY_CONTAINER_PATH}"
    export KBFABRIC_EMBEDDINGS_MODEL_DIR="${KBFABRIC_EMBEDDINGS_MODEL_DIR:-$PROJECT_DIR/custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0}"
else
    export KBFABRIC_INGEST_PERSIST_DIR="${KBFABRIC_INGEST_PERSIST_DIR:-$PROJECT_DIR}"
    export KBFABRIC_EMBEDDINGS_MODEL_DIR="${KBFABRIC_EMBEDDINGS_MODEL_DIR:-$PROJECT_DIR/custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0}"
fi

echo "=========================================="
echo "Pulse Knowledge Fabric - Ingestion API (DEV)"
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

# DEV paths (Ray on 6001 locally; override with RAY_DASHBOARD_URL if needed)
# Service URLs: Use host.docker.internal so Ray worker (in container) can reach host's Docker services.
# Ollama: Hardcoded to remote VPN host (not local).
export PYTHONPATH="$PROJECT_DIR"
export RAY_DASHBOARD_URL="${RAY_DASHBOARD_URL:-http://localhost:${RAY_DASHBOARD_PORT}}"
export WEAVIATE_URL="${WEAVIATE_URL:-http://host.docker.internal:8080}"
export NEO4J_URI="${NEO4J_URI:-bolt://host.docker.internal:7687}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-$OLLAMA_URL}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://host.docker.internal:4317}"
export RAY_RUNTIME_ENV_TEMPORARY_REFERENCE_EXPIRATION_S="${RAY_RUNTIME_ENV_TEMPORARY_REFERENCE_EXPIRATION_S:-1800}"

echo "Environment variables (DEV):"
echo "  RAY_DASHBOARD_URL: $RAY_DASHBOARD_URL"
echo "  WEAVIATE_URL: $WEAVIATE_URL"
echo "  NEO4J_URI: $NEO4J_URI"
echo "  OLLAMA_URL: $OLLAMA_URL"
echo "  OTEL_EXPORTER_OTLP_ENDPOINT: $OTEL_EXPORTER_OTLP_ENDPOINT"
echo "  KBFABRIC_EMBEDDINGS_MODEL_DIR: $KBFABRIC_EMBEDDINGS_MODEL_DIR"
echo "  KBFABRIC_INGEST_PERSIST_DIR: $KBFABRIC_INGEST_PERSIST_DIR"
echo ""

if [ "$USE_TMUX" = true ]; then
    echo "Starting ingestion API service in tmux background session..."
    tmux new-session -d -s "kbfabric-ingest-api" "cd $PROJECT_DIR && $PYTHON_BIN -m uvicorn kbfabric_ingest.scheduler.api:app --host $HOST --port $PORT"
    echo "Service started in tmux session: kbfabric-ingest-api"
    echo "Attach with: tmux attach-session -t kbfabric-ingest-api"
    echo "Detach with: Ctrl+B, D"
else
    echo "Starting ingestion API service in foreground..."
    echo "Press Ctrl+C to stop the service"
    echo ""
    "$PYTHON_BIN" -m uvicorn kbfabric_ingest.scheduler.api:app --host "$HOST" --port "$PORT"
fi

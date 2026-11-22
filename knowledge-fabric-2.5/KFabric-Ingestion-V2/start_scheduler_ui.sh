#!/bin/bash
set -euo pipefail

# KnowledgeFabric Scheduler UI Startup Script
# Starts the Streamlit scheduler UI on port 6003
# Usage: ./start_scheduler_ui.sh [--tmux] [--host <IP>] [--port <PORT>]

USE_TMUX=false
HOST="0.0.0.0"
PORT="6003"
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
            echo "Usage: ./start_scheduler_ui.sh [--tmux] [--host <IP>] [--port <PORT>]"
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

# Load environment from repo-root ``env`` (non-hidden). See env.example.
if [ -f "$REPO_ROOT/env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_ROOT/env"
    set +a
fi

echo "=========================================="
echo "QiBa/Pulse Knowledge Fabric - Scheduler UI"
echo "=========================================="
echo "Project: $PROJECT_DIR"
echo "Port: $PORT"
echo "URL: http://$HOST:$PORT"
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

# Set environment variables
export PYTHONPATH="$PROJECT_DIR"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8080}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"

if [ "$USE_TMUX" = true ]; then
    echo "Starting scheduler UI in tmux background session..."
    tmux new-session -d -s "kbfabric-scheduler-ui" "streamlit run kbfabric_ingest/frontend/scheduler_ui.py --server.port $PORT --server.address $HOST --server.headless true"
    echo "UI started in tmux session: kbfabric-scheduler-ui"
    echo "Attach with: tmux attach-session -t kbfabric-scheduler-ui"
    echo "Detach with: Ctrl+B, D"
else
    echo "Starting scheduler UI in foreground..."
    echo "Press Ctrl+C to stop the service"
    echo ""
    # Start the scheduler UI
    streamlit run kbfabric_ingest/frontend/scheduler_ui.py --server.port "$PORT" --server.address "$HOST" --server.headless true
fi
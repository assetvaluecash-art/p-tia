#!/bin/bash
set -euo pipefail

# KnowledgeFabric Ray Cluster Startup Script
# Starts Ray cluster with dashboard on port 6001 and Redis on port 6379
# Usage: ./start_ray_cluster.sh [--tmux] [--host <IP>] [--port <PORT>]

USE_TMUX=false
HOST="0.0.0.0"
PORT="6001"
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
            echo "Usage: ./start_ray_cluster.sh [--tmux] [--host <IP>] [--port <PORT>]"
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

echo "=========================================="
echo "QiBa/Pulse Knowledge Fabric - Ray Cluster"
echo "=========================================="
echo "Dashboard: http://localhost:$PORT"
echo "Jobs API: http://localhost:6001/api/jobs"
echo "Redis Port: 6379"
echo "Workers: 4 CPUs, 12GB Memory"
echo "=========================================="

# Activate virtual environment if needed
if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ ! -d ".venv" ]; then
        echo "Creating virtual environment..."
        python3.11 -m venv .venv
    fi
    echo "Activating virtual environment..."
    source .venv/bin/activate
else
    echo "Using existing virtual environment: $VIRTUAL_ENV"
fi

# Set environment variables for Ray workers
export KBFABRIC_EMBEDDINGS_MODEL_DIR="${KBFABRIC_EMBEDDINGS_MODEL_DIR:-/mnt/c/data/qiba_pulse_knowledge_fabric/kbfabric/kbfabric-ingest/custom_ml/embeddings/models/snowflake-arctic-embed-l-v2.0}"

echo "Ray environment variables:"
echo "  KBFABRIC_EMBEDDINGS_MODEL_DIR: $KBFABRIC_EMBEDDINGS_MODEL_DIR"

if [ "$USE_TMUX" = true ]; then
    echo "Starting Ray cluster in tmux background session..."
    tmux new-session -d -s "kbfabric-ray-cluster" "bash -lc 'export KBFABRIC_EMBEDDINGS_MODEL_DIR=\"$KBFABRIC_EMBEDDINGS_MODEL_DIR\"; ray stop --force >/dev/null 2>&1 || true; pkill -9 -f \"ray::\" 2>/dev/null || true; pkill -9 -f \"raylet\" 2>/dev/null || true; pkill -9 -f \"ray/dashboard\" 2>/dev/null || true; rm -rf /tmp/ray/session_*/sockets/* 2>/dev/null || true; sleep 3; ray start --head --port 6379 --dashboard-host 0.0.0.0 --dashboard-port $PORT --num-cpus=4 --memory=12000000000; echo \"Ray cluster started\"; while true; do sleep 1; done'"
    echo "Session: kbfabric-ray-cluster"
    echo "Attach with: tmux attach-session -t kbfabric-ray-cluster"
    echo "Detach with: Ctrl+B, D"
else
    # Stop any existing cluster and clean up zombie processes
    echo "Stopping any existing Ray clusters..."
    ray stop --force >/dev/null 2>&1 || true
    
    # Kill any zombie Ray processes that didn't stop
    echo "Cleaning up any remaining Ray processes..."
    pkill -9 -f 'ray::' 2>/dev/null || true
    pkill -9 -f 'raylet' 2>/dev/null || true
    pkill -9 -f 'ray/dashboard' 2>/dev/null || true
    pkill -9 -f 'dashboard_agent' 2>/dev/null || true
    
    # Clean up stale temp files
    rm -rf /tmp/ray/session_*/sockets/* 2>/dev/null || true
    
    echo "Waiting for cleanup..."
    sleep 3

    # Start new cluster with worker resources
    echo "Starting Ray cluster with 4 CPUs and 12GB memory..."
    ray start --head --port 6379 --dashboard-host 0.0.0.0 --dashboard-port "$PORT" \
        --num-cpus=4 --memory=12000000000

    echo ""
    echo "Ray cluster started successfully!"
    echo "Dashboard: http://localhost:$PORT"
    echo "Workers: 4 CPUs, 12GB Memory"
    echo ""
    echo "To use Ray CLI, set: export RAY_ADDRESS='http://127.0.0.1:$PORT'"
    echo "Press Ctrl+C to stop the cluster"
    echo ""
    # Keep running and handle graceful shutdown
    trap 'echo ""; echo "Stopping Ray cluster..."; ray stop; exit 0' INT
    while true; do sleep 1; done
fi
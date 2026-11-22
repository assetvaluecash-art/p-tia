#!/usr/bin/env python3
"""Launch the Knowledge Fabric Retriever API on port 6004.

Usage (from kbfabric-ingest/):
    PYTHONPATH=. .venv/bin/python3 scripts/start_retriever.py

Swagger UI:  http://localhost:6004/docs
Health:      http://localhost:6004/health
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "kbfabric_ingest.retrieval.retriever_api:app",
        host="0.0.0.0",
        port=6004,
        log_level="info",
        reload=False,
    )

"""
FastAPI application exposing the HybridRetriever as ``POST /query``.

Launch via ``scripts/start_retriever.py`` or directly:

    PYTHONPATH=. .venv/bin/uvicorn kbfabric_ingest.retrieval.retriever_api:app \
        --host 0.0.0.0 --port 6004

Swagger UI available at http://localhost:6004/docs
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from kbfabric_ingest.retrieval.hybrid_retriever import HybridRetriever

logger = logging.getLogger("retriever_api")

# ---------------------------------------------------------------------------
# Singleton retriever (expensive to init — loaded once on startup)
# ---------------------------------------------------------------------------

_retriever: HybridRetriever | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _retriever
    logger.info("Loading HybridRetriever …")
    _retriever = HybridRetriever()
    logger.info("HybridRetriever ready.")
    yield
    logger.info("Shutting down HybridRetriever …")
    if _retriever:
        _retriever.close()
        _retriever = None


# ---------------------------------------------------------------------------
# Pydantic models — request / response
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    intent: str = Field("any", description="Traversal pattern / use-case name (e.g. change_impact, test_coverage, any)")
    query: str = Field(..., description="Natural-language query")
    domain: str = Field("corporate_commercial", description="Domain to search")
    top_k_semantic: int = Field(10, ge=1, le=50, description="Max semantic chunks")
    top_k_graph: int = Field(10, ge=1, le=50, description="Max graph paths")

    model_config = {"json_schema_extra": {"examples": [{
        "intent": "change_impact",
        "query": "What does user story JIRA-4521 impact?",
        "domain": "corporate_commercial",
        "top_k_semantic": 10,
        "top_k_graph": 10,
    }]}}


class DetectedDomain(BaseModel):
    domain: str
    confidence: float


class ChunkItem(BaseModel):
    text: str = ""
    source_file: str = ""
    category: str = ""
    score: float = 0
    weaviate_uuid: str = ""
    block_id: str = ""
    page_number: int | None = None
    block_index: int | None = None
    block_type: str = ""
    kg_node_type: str = ""
    domain: str = ""
    extracted_fields: str | None = None
    extracted_knowledge: str | None = None


class EntityRef(BaseModel):
    name: str = ""
    type: str = ""


class PathItem(BaseModel):
    path_text: str = ""
    score: float = 0
    from_entity: EntityRef = Field(default_factory=EntityRef)
    relationship_type: str = ""
    to_entity: EntityRef = Field(default_factory=EntityRef)
    source_block_id: str = ""
    source_file: str = ""
    source_page: int | None = None
    source_evidence: str = ""


class RetrievalMetadata(BaseModel):
    intent: str = ""
    pattern: str = ""
    max_depth: int = 2
    latency_ms: float = 0
    chunks_considered: int = 0
    paths_considered: int = 0
    errors: list[str] = []


class QueryResponse(BaseModel):
    query: str
    domain: str
    detected_domains: list[DetectedDomain] = []
    chunks: list[ChunkItem] = []
    reranked_chunks: list[ChunkItem] = []
    paths: list[PathItem] = []
    reranked_paths: list[PathItem] = []
    metadata: RetrievalMetadata | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Knowledge Fabric Retriever",
    version="1.0",
    description="Hybrid semantic + graph retrieval for AI-native QE agents.",
    lifespan=_lifespan,
)


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    """Execute a hybrid retrieval query."""
    raw: dict[str, Any] = _retriever.retrieve(
        intent=req.intent,
        query=req.query,
        domain=req.domain,
        top_k_semantic=req.top_k_semantic,
        top_k_graph=req.top_k_graph,
    )

    meta_raw = raw.get("metadata", {})

    return QueryResponse(
        query=raw["query"],
        domain=raw["domain"],
        detected_domains=[DetectedDomain(**d) for d in raw.get("detected_domains", [])],
        chunks=[ChunkItem(**c) for c in raw.get("chunks", [])],
        reranked_chunks=[ChunkItem(**c) for c in raw.get("reranked_chunks", [])],
        paths=[PathItem(**_coerce_path(p)) for p in raw.get("paths", [])],
        reranked_paths=[PathItem(**_coerce_path(p)) for p in raw.get("reranked_paths", [])],
        metadata=RetrievalMetadata(**meta_raw) if meta_raw else None,
    )


def _coerce_path(p: dict) -> dict:
    """Ensure nested entity dicts become EntityRef-compatible."""
    out = dict(p)
    for key in ("from_entity", "to_entity"):
        val = out.get(key)
        if isinstance(val, dict):
            out[key] = EntityRef(**val)
    return out


@app.get("/health")
def health():
    return {"status": "ok", "retriever_loaded": _retriever is not None}

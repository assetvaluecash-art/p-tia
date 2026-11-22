"""Data models for hybrid retrieval results."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticChunk:
    """Semantic search result from Weaviate."""
    uuid: str
    masked_text: str
    source_file: str
    block_category: str
    kg_node_type: str
    score: float
    domain: str
    source: str = "weaviate"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphResult:
    """Graph search result from Neo4j (legacy node-based)."""
    node_id: int
    node_type: str
    node_properties: dict[str, Any]
    relationships: list[dict[str, Any]]
    path_to_root: list[dict[str, Any]]
    relevance_score: float
    domain: str
    source: str = "neo4j"
    chunk_id: str | None = None  # If this node references a Weaviate chunk


@dataclass
class GraphPath:
    """A relationship path from graph traversal - the true graph value.
    
    Format: Node: {name} of Type: {type} - Relationship: {rel_type} - Node: {name} of Type: {type}
    
    Provenance fields enable complete audit trail:
    - entity_id hashes link to Neo4j nodes
    - chunk_ids link to Weaviate chunks (for Block nodes)
    - timestamps track when entities were created/updated
    """
    path_text: str  # Human-readable path string
    from_entity: dict[str, str]  # {name: str, type: str}
    relationship_type: str
    to_entity: dict[str, str]  # {name: str, type: str}
    relevance_score: float  # Based on seed embedding match
    domain: str
    source: str = "neo4j"
    # Provenance fields for traceability
    from_entity_id: str | None = None     # Neo4j entity_id hash
    to_entity_id: str | None = None       # Neo4j entity_id hash
    relationship_id: str | None = None    # Relationship rel_id hash (if available)
    # Weaviate chunk_id links for complete audit trail (AI governance)
    source_chunk_ids: list[str] = field(default_factory=list)  # Weaviate UUIDs of related Block nodes
    # Timestamps for provenance (ISO 8601 strings)
    from_entity_created_at: str | None = None
    to_entity_created_at: str | None = None
    relationship_created_at: str | None = None
    
    @staticmethod
    def format_path(from_name: str, from_type: str, rel_type: str, to_name: str, to_type: str) -> str:
        """Format a path as human-readable string."""
        return f"Node: {from_name} of Type: {from_type} - Relationship: {rel_type} - Node: {to_name} of Type: {to_type}"


@dataclass
class FusedResult:
    """Fused result combining semantic and graph results."""
    content: str
    sources: list[str]  # ["weaviate"], ["neo4j"], or ["weaviate", "neo4j"]
    semantic_score: float | None = None
    graph_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_id: str | None = None
    node_id: int | None = None


@dataclass
class RerankedResult:
    """Result after cross-encoder reranking."""
    fused_result: FusedResult
    cross_encoder_score: float
    rank: int


@dataclass
class FinalResult:
    """Final result after LLM relevance scoring."""
    reranked_result: RerankedResult
    llm_relevance_score: float
    final_score: float
    rank: int


@dataclass
class HybridRetrievalResult:
    """Complete hybrid retrieval result (internal processing)."""
    query: str
    detected_domain: str
    semantic_chunks: list[SemanticChunk]
    graph_results: list[GraphResult]
    fused_results: list[FusedResult]
    reranked_results: list[RerankedResult]
    final_results: list[FinalResult]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankedChunk:
    """A semantic chunk after cross-encoder reranking with provenance.
    
    Provenance fields provide explainability for QE agents:
    - Where exactly did this chunk come from?
    - What are its unique identifiers for traceability?
    """
    text: str
    source_file: str
    category: str
    score: float
    rank: int
    # Provenance fields (always populated for explainability)
    weaviate_uuid: str | None = None      # Weaviate object UUID
    block_id: str | None = None           # Deterministic block ID (UUID5)
    page_number: int | None = None        # Source page in document
    block_index: int | None = None        # Block position in document
    block_type: str | None = None         # heading, paragraph, table_row, etc.
    kg_node_type: str | None = None       # KG node type label
    
    def format_provenance(self) -> str:
        """Format provenance for display in results."""
        parts = [f"📄 Source: {self.source_file}"]
        if self.page_number:
            parts.append(f"Page: {self.page_number}")
        if self.block_index is not None:
            parts.append(f"Block: {self.block_index}")
        parts.append(f"\n  🏷️ Category: {self.category}")
        if self.block_type:
            parts.append(f"Type: {self.block_type}")
        if self.weaviate_uuid or self.block_id:
            ids = []
            if self.weaviate_uuid:
                ids.append(f"Weaviate: {self.weaviate_uuid[:12]}...")
            if self.block_id:
                ids.append(f"Block: {self.block_id[:16]}...")
            parts.append(f"\n  🔑 IDs: {' | '.join(ids)}")
        return " | ".join(parts)


@dataclass
class RerankedPath:
    """A graph path after cross-encoder reranking with provenance.
    
    Provenance fields track Neo4j node origins and Weaviate chunk links for traceability.
    """
    path_text: str
    from_entity: dict[str, str]
    relationship_type: str
    to_entity: dict[str, str]
    score: float
    rank: int
    # Provenance fields (always populated for explainability)
    from_entity_id: str | None = None     # Neo4j entity_id hash
    to_entity_id: str | None = None       # Neo4j entity_id hash
    relationship_id: str | None = None    # Relationship rel_id hash
    # Weaviate chunk_id links for complete audit trail (AI governance)
    source_chunk_ids: list[str] = field(default_factory=list)  # Weaviate UUIDs of related Block nodes
    # Timestamps for provenance (ISO 8601 strings)
    from_entity_created_at: str | None = None
    to_entity_created_at: str | None = None
    relationship_created_at: str | None = None
    
    def format_provenance(self) -> str:
        """Format provenance for display in results."""
        parts = [f"🔗 {self.path_text}"]
        if self.from_entity_id or self.to_entity_id:
            ids = []
            if self.from_entity_id:
                ids.append(f"From: {self.from_entity_id[:8]}...")
            if self.to_entity_id:
                ids.append(f"To: {self.to_entity_id[:8]}...")
            if self.relationship_id:
                ids.append(f"Rel: {self.relationship_id[:8]}...")
            parts.append(f"\n  🔑 Neo4j IDs: {' | '.join(ids)}")
        if self.source_chunk_ids:
            chunk_ids_short = [cid[:12] + "..." for cid in self.source_chunk_ids[:3]]
            parts.append(f"\n  📦 Weaviate Chunks: {' | '.join(chunk_ids_short)}")
        if self.from_entity_created_at or self.to_entity_created_at:
            parts.append(f"\n  📅 Created: From={self.from_entity_created_at or 'N/A'}, To={self.to_entity_created_at or 'N/A'}")
        return "\n".join(parts)


@dataclass
class HybridRetrievalResponse:
    """Agent-friendly response with labeled sections.
    
    Provides clear separation between content knowledge (chunks)
    and structural knowledge (paths) for QE agents.
    """
    query: str
    domain: str
    chunks: list[RerankedChunk]  # Content knowledge from semantic search
    paths: list[RerankedPath]    # Structural knowledge from graph search
    answer: str
    metadata: dict[str, Any] = field(default_factory=dict)


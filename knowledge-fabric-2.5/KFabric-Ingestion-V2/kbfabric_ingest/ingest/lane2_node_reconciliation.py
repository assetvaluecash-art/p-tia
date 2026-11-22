"""Lane 2 Pass 3 — node reconciliation prototype (read-only Neo4j + live LLM).

Opt-in only. Not wired into production service.py / kg_writer.

Flow (per mock CanonicalBlock):
  Pass 1 classify (REAL LLM) → Pass 2 extract (REAL LLM)
  → for each extracted entity: correction patterns → Neo4j READ candidates
  → lexical + semantic fusion → Pass 3 LLM adjudication → decision policy
  → (HITL / correction-pattern store available but opt-in; demo leaves them off)

Safety:
  - Neo4j: MATCH/RETURN (and optional read-only vector query) only
  - No CREATE/MERGE/SET/DELETE/REMOVE/DROP
  - No Weaviate reads or writes required
  - graph_write_performed / weaviate_write_performed / neo4j_write_attempted
    are always False (recommendations only)
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.lane2_two_pass import (
    LLMCallable,
    Lane2TwoPassResult,
    YamlOntologyProvider,
    _parse_json_object,
    process_lane2_block,
)
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

PASS3_PROMPT_ID = "lane2_pass3_adjudicate"
PASS3_PROMPT_VERSION = "v1"

# ---------------------------------------------------------------------------
# Thresholds / fusion
# ---------------------------------------------------------------------------

LEXICAL_WEIGHT = 0.3
SEMANTIC_WEIGHT = 0.7
TOP_K = 3
MIN_FUSION_SCORE = 0.45
# RRF and the cross-encoder rank candidates; the normalized lexical/semantic
# fusion score is compared to the environment-configured policy threshold.
AMBIGUITY_MARGIN = 0.05
CORRECTION_PATTERN_MIN_CONFIDENCE = 0.85

_RULE = "=" * 72
_SUB = "-" * 72

# Allowlisted Cypher (MATCH / RETURN / optional read-only vector query only)
_CYPHER_BY_NAME_ALIAS = """
MATCH (e)
WHERE e.domain = $domain
  AND e.customer_id = $customer_id
  AND e.tenant_id = $tenant_id
  AND e.name IS NOT NULL
  AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
  AND (
    $node_type IS NULL
    OR e.kg_node_type = $node_type
    OR $node_type IN labels(e)
  )
  AND (
    toLower(e.name) CONTAINS toLower($name_fragment)
    OR toLower(coalesce(e.name_display, '')) CONTAINS toLower($name_fragment)
    OR ANY(a IN coalesce(e.aliases, []) WHERE toLower(toString(a)) CONTAINS toLower($name_fragment))
  )
RETURN e.entity_id AS entity_id,
       e.name AS name,
       coalesce(e.name_display, e.name) AS name_display,
       e.aliases AS aliases,
       coalesce(e.kg_node_type, labels(e)[0]) AS kg_node_type,
       labels(e) AS labels,
       e.embedding AS embedding,
       coalesce(e.description, e.summary, '') AS description,
       coalesce(e.ingestion_product_line_ids,
                CASE WHEN e.product_line_id IS NULL THEN [] ELSE [e.product_line_id] END) AS product_line_ids
ORDER BY CASE WHEN e.product_line_id = $product_line_id
                   OR $product_line_id IN coalesce(e.ingestion_product_line_ids, [])
              THEN 1 ELSE 0 END DESC
LIMIT $limit
""".strip()

_CYPHER_BY_TYPE_DOMAIN = """
MATCH (e)
WHERE e.domain = $domain
  AND e.customer_id = $customer_id
  AND e.tenant_id = $tenant_id
  AND e.name IS NOT NULL
  AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
  AND (
    $node_type IS NULL
    OR e.kg_node_type = $node_type
    OR $node_type IN labels(e)
  )
RETURN e.entity_id AS entity_id,
       e.name AS name,
       coalesce(e.name_display, e.name) AS name_display,
       e.aliases AS aliases,
       coalesce(e.kg_node_type, labels(e)[0]) AS kg_node_type,
       labels(e) AS labels,
       e.embedding AS embedding,
       coalesce(e.description, e.summary, '') AS description,
       CASE WHEN size(coalesce(e.ingestion_product_line_ids, [])) > 0
            THEN e.ingestion_product_line_ids
            ELSE CASE WHEN e.product_line_id IS NULL THEN [] ELSE [e.product_line_id] END END AS product_line_ids
ORDER BY CASE WHEN e.product_line_id = $product_line_id
                   OR $product_line_id IN coalesce(e.ingestion_product_line_ids, [])
              THEN 1 ELSE 0 END DESC,
         coalesce(e.pagerank, 0) DESC
LIMIT $limit
""".strip()

_CYPHER_VECTOR_QUERY = """
CALL db.index.vector.queryNodes($index_name, $limit, $query_vector)
YIELD node AS e, score
WHERE e.domain = $domain
  AND e.customer_id = $customer_id
  AND e.tenant_id = $tenant_id
  AND e.name IS NOT NULL
  AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
  AND (
    $node_type IS NULL
    OR e.kg_node_type = $node_type
    OR $node_type IN labels(e)
  )
RETURN e.entity_id AS entity_id,
       e.name AS name,
       coalesce(e.name_display, e.name) AS name_display,
       e.aliases AS aliases,
       coalesce(e.kg_node_type, labels(e)[0]) AS kg_node_type,
       labels(e) AS labels,
       e.embedding AS embedding,
       coalesce(e.description, e.summary, '') AS description,
       CASE WHEN size(coalesce(e.ingestion_product_line_ids, [])) > 0
            THEN e.ingestion_product_line_ids
            ELSE CASE WHEN e.product_line_id IS NULL THEN [] ELSE [e.product_line_id] END END AS product_line_ids,
       score AS vector_score
""".strip()

_ALLOWLISTED_QUERIES: frozenset[str] = frozenset(
    {
        _CYPHER_BY_NAME_ALIAS,
        _CYPHER_BY_TYPE_DOMAIN,
        _CYPHER_VECTOR_QUERY,
    }
)

_FORBIDDEN_WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b"
    r"|CALL\s*\{[^}]*\bWRITE\b"
    r"|CALL\s+dbms\.(?!components\b)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Enums + models
# ---------------------------------------------------------------------------


class AdjudicationDecision(str, Enum):
    SAME = "SAME"
    DIFFERENT = "DIFFERENT"
    AMBIGUOUS = "AMBIGUOUS"


class ReconciliationRecommendation(str, Enum):
    REUSE_EXISTING = "REUSE_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    SEND_TO_REVIEW = "SEND_TO_REVIEW"
    # Delta / shared-mapping extensions (aliases align with MappingDecision)
    CREATE_DELTA_RELATIONSHIP = "CREATE_DELTA_RELATIONSHIP"
    CREATE_DELTA_NODE_AND_RELATIONSHIP = "CREATE_DELTA_NODE_AND_RELATIONSHIP"
    NO_CHANGE = "NO_CHANGE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"  # synonym of SEND_TO_REVIEW for shared utility


class ReasonCode(str, Enum):
    CORRECTION_PATTERN_HIT = "CORRECTION_PATTERN_HIT"
    HIGH_FUSION_LLM_SAME = "HIGH_FUSION_LLM_SAME"
    LLM_SAME_VALID_CANDIDATE = "LLM_SAME_VALID_CANDIDATE"
    NO_CANDIDATES = "NO_CANDIDATES"
    LOW_FUSION_LLM_DIFFERENT = "LOW_FUSION_LLM_DIFFERENT"
    LLM_DIFFERENT = "LLM_DIFFERENT"
    NEAR_TIE = "NEAR_TIE"
    TYPE_CONFLICT = "TYPE_CONFLICT"
    INVALID_CANDIDATE_ID = "INVALID_CANDIDATE_ID"
    FUSION_LLM_CONFLICT = "FUSION_LLM_CONFLICT"
    LLM_AMBIGUOUS = "LLM_AMBIGUOUS"
    LLM_ERROR = "LLM_ERROR"
    BELOW_MIN_FUSION = "BELOW_MIN_FUSION"
    POLICY_DEFAULT_REVIEW = "POLICY_DEFAULT_REVIEW"


class ReviewerAction(str, Enum):
    APPROVE_REUSE = "APPROVE_REUSE"
    REASSIGN = "REASSIGN"
    EDIT_AND_REUSE = "EDIT_AND_REUSE"
    APPROVE_AS_NEW = "APPROVE_AS_NEW"
    SKIP = "SKIP"


class IncomingNode(BaseModel):
    """Pass-2-shaped extracted entity + provenance."""

    temporary_id: str
    node_type: str
    name: str
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    source_file: str | None = None
    block_id: str | None = None
    domain: str | None = None
    extraction_confidence: float = 0.0


class ExistingNode(BaseModel):
    """Mapped from a Neo4j READ row."""

    entity_id: str
    name: str
    name_display: str | None = None
    aliases: list[str] = Field(default_factory=list)
    kg_node_type: str | None = None
    labels: list[str] = Field(default_factory=list)
    description: str | None = None
    embedding: list[float] | None = None
    vector_score: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    product_line_ids: list[str] = Field(default_factory=list)


class CandidateMatch(BaseModel):
    candidate: ExistingNode
    lexical_score: float
    semantic_score: float
    fusion_score: float
    rank: int
    rrf_score: float = 0.0
    cross_encoder_score: float | None = None
    graph_neighborhood: list[dict[str, Any]] = Field(default_factory=list)


class LLMAdjudicationResult(BaseModel):
    decision: AdjudicationDecision | None = None
    selected_candidate_id: str | None = None
    confidence: float = 0.0
    explanation_summary: str = ""
    raw_response: str | None = None
    duration_ms: float | None = None
    parse_error: str | None = None


class CorrectionPattern(BaseModel):
    pattern_id: str
    incoming_name_normalized: str
    incoming_node_type: str
    domain: str | None = None
    action: ReviewerAction
    selected_entity_id: str | None = None
    edited_name: str | None = None
    confidence: float = 1.0
    usage_count: int = 0
    notes: str = ""


class ReviewQueueItem(BaseModel):
    review_id: str
    incoming: IncomingNode
    candidates: list[CandidateMatch] = Field(default_factory=list)
    adjudication: LLMAdjudicationResult | None = None
    reason_code: ReasonCode
    status: str = "pending"  # pending | resolved
    reviewer_action: ReviewerAction | None = None
    resolved_entity_id: str | None = None
    notes: str = ""


class ReconciliationResult(BaseModel):
    incoming: IncomingNode
    recommendation: ReconciliationRecommendation
    reason_code: ReasonCode
    selected_entity_id: str | None = None
    candidates: list[CandidateMatch] = Field(default_factory=list)
    adjudication: LLMAdjudicationResult | None = None
    correction_pattern_hit: bool = False
    correction_pattern_id: str | None = None
    review_item_id: str | None = None
    neo4j_read_executed: bool = False
    candidate_fetch_count: int = 0
    semantic_provider: str | None = None
    graph_write_performed: bool = False
    weaviate_write_performed: bool = False
    neo4j_write_attempted: bool = False


class Lane2ReconcileRunResult(BaseModel):
    """Pass1 + Pass2 + Pass3 wrap for one CanonicalBlock."""

    block_id: str
    source_file: str
    domain: str | None = None
    two_pass: Lane2TwoPassResult
    reconciliations: list[ReconciliationResult] = Field(default_factory=list)
    review_queue_size: int = 0
    total_duration_ms: float = 0.0
    graph_write_performed: bool = False
    weaviate_write_performed: bool = False
    neo4j_write_attempted: bool = False
    success: bool = False


# ---------------------------------------------------------------------------
# Review helpers (console)
# ---------------------------------------------------------------------------


def _review_print(enabled: bool, *lines: str) -> None:
    if not enabled:
        return
    for line in lines:
        print(line, flush=True)


def _review_section(enabled: bool, title: str) -> None:
    _review_print(enabled, "", _RULE, title, _RULE)


# ---------------------------------------------------------------------------
# Normalization / similarity (local; KGWriter normalize is REFERENCE ONLY)
# ---------------------------------------------------------------------------


def normalize_entity_value(value: str) -> str:
    """Mirror of KGWriter._normalize_entity_value — do not call writers."""
    if not value:
        return ""
    normalized = value.strip().lower()
    normalized = re.sub(r"^(the|a|an)\s+", "", normalized)
    normalized = re.sub(r"\s+(the|a|an)$", "", normalized)
    normalized = re.sub(r'[.,;:!?"\(\)\[\]{}]', "", normalized)
    return " ".join(normalized.split())


def lexical_similarity(a: str, b: str) -> float:
    na = normalize_entity_value(a)
    nb = normalize_entity_value(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return float(SequenceMatcher(None, na, nb).ratio())


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / (math.sqrt(na) * math.sqrt(nb)))


def fusion_score(lexical: float, semantic: float) -> float:
    return float(LEXICAL_WEIGHT * lexical + SEMANTIC_WEIGHT * semantic)


def get_auto_resolution_confidence_threshold() -> float:
    """Read the shared node/endpoint cutoff from environment-built config."""
    from kbfabric_ingest.ingest.config import get_app_config

    return float(
        get_app_config()["ingestion"]["entity_mapping"][
            "auto_resolution_confidence_threshold"
        ]
    )


def confidence_allows_auto_resolution(
    confidence: float,
    *,
    threshold: float | None = None,
) -> bool:
    """Apply the shared strict, environment-configured auto-resolution cutoff."""
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return False
    if threshold is None:
        threshold = get_auto_resolution_confidence_threshold()
    try:
        cutoff = float(threshold)
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and math.isfinite(cutoff) and score > cutoff


# ---------------------------------------------------------------------------
# Semantic similarity providers
# ---------------------------------------------------------------------------


@runtime_checkable
class SemanticSimilarityProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def is_mock(self) -> bool: ...

    def embed(self, text: str) -> list[float] | None: ...


class RealEmbeddingProvider:
    """Embed via LLMClient.embed_text / embeddings client. Does not store vectors."""

    def __init__(self, embed_fn: Callable[[list[str]], list[list[float]]]):
        self._embed_fn = embed_fn

    @property
    def name(self) -> str:
        return "RealEmbeddingProvider"

    @property
    def is_mock(self) -> bool:
        return False

    def embed(self, text: str) -> list[float] | None:
        try:
            vectors = self._embed_fn([text])
            if vectors and vectors[0]:
                return [float(x) for x in vectors[0]]
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "lane2.pass3.embed_failed",
                error=str(exc),
                provider=self.name,
            )
        return None


class MockSemanticSimilarityProvider:
    """Degraded mode only — hash-bucket pseudo-embedding. Prefer real embeds."""

    def __init__(self, dims: int = 32):
        self._dims = dims

    @property
    def name(self) -> str:
        return "MockSemanticSimilarityProvider"

    @property
    def is_mock(self) -> bool:
        return True

    def embed(self, text: str) -> list[float] | None:
        # Deterministic bag-of-char pseudo vector for offline demos
        vec = [0.0] * self._dims
        normed = normalize_entity_value(text)
        if not normed:
            return vec
        for i, ch in enumerate(normed):
            vec[i % self._dims] += (ord(ch) % 31) / 31.0
        mag = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / mag for v in vec]


# ---------------------------------------------------------------------------
# Neo4j read-only candidate reader
# ---------------------------------------------------------------------------


def assert_read_only_cypher(query: str) -> None:
    """Reject any non-allowlisted or write-shaped Cypher."""
    cleaned = query.strip()
    if cleaned not in _ALLOWLISTED_QUERIES:
        raise ValueError("Neo4jCandidateReader: query not in allowlist")
    if _FORBIDDEN_WRITE_RE.search(cleaned):
        raise ValueError("Neo4jCandidateReader: forbidden write keyword in query")
    # Soft structural check
    upper = cleaned.upper()
    if "MATCH" not in upper and "CALL DB.INDEX.VECTOR.QUERYNODES" not in upper:
        raise ValueError("Neo4jCandidateReader: query must be MATCH or vector query")
    if any(tok in upper for tok in (" CREATE ", " MERGE ", " SET ", " DELETE ")):
        raise ValueError("Neo4jCandidateReader: write token detected")


def _row_to_existing(record: Any) -> ExistingNode | None:
    data = dict(record)
    eid = data.get("entity_id")
    name = data.get("name")
    if not eid or not name:
        return None
    aliases_raw = data.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases = [aliases_raw]
    elif isinstance(aliases_raw, list):
        aliases = [str(a) for a in aliases_raw if a is not None]
    else:
        aliases = []
    emb = data.get("embedding")
    embedding: list[float] | None = None
    if emb is not None:
        try:
            embedding = [float(x) for x in list(emb)]
        except (TypeError, ValueError):
            embedding = None
    labels = data.get("labels") or []
    if not isinstance(labels, list):
        labels = [str(labels)]
    vector_score = data.get("vector_score")
    return ExistingNode(
        entity_id=str(eid),
        name=str(name),
        name_display=str(data["name_display"]) if data.get("name_display") else None,
        aliases=aliases,
        kg_node_type=str(data["kg_node_type"]) if data.get("kg_node_type") else None,
        labels=[str(x) for x in labels],
        description=str(data["description"]) if data.get("description") else None,
        embedding=embedding,
        vector_score=float(vector_score) if vector_score is not None else None,
        raw={k: v for k, v in data.items() if k != "embedding"},
        product_line_ids=[str(value) for value in (data.get("product_line_ids") or []) if value],
    )


class Neo4jCandidateReader:
    """READ-only Neo4j candidate fetch. Never opens write transactions."""

    def __init__(
        self,
        driver: Any,
        *,
        vector_index_name: str | None = None,
        default_limit: int = 25,
        customer_id: str | None = None,
        tenant_id: str | None = None,
        product_line_id: str | None = None,
    ):
        self._driver = driver
        self._vector_index_name = vector_index_name
        self.customer_id = customer_id
        self.tenant_id = tenant_id
        self.product_line_id = product_line_id
        self._default_limit = default_limit
        self.read_query_count = 0
        self.last_queries: list[str] = []
        self.write_attempted = False  # always remains False

    def _run_read(self, query: str, params: dict[str, Any]) -> list[ExistingNode]:
        assert_read_only_cypher(query)
        self.last_queries.append(query)
        self.read_query_count += 1
        nodes: list[ExistingNode] = []
        # Prefer explicit READ access mode when driver supports it
        session_kwargs: dict[str, Any] = {}
        try:
            from neo4j import READ_ACCESS

            session_kwargs["default_access_mode"] = READ_ACCESS
        except Exception:  # noqa: BLE001
            pass

        with self._driver.session(**session_kwargs) as session:
            # Prefer execute_read when available
            def _work(tx: Any) -> list[ExistingNode]:
                result = tx.run(query, **params)
                out: list[ExistingNode] = []
                for record in result:
                    node = _row_to_existing(record)
                    if node is not None:
                        out.append(node)
                return out

            if hasattr(session, "execute_read"):
                nodes = session.execute_read(_work)
            else:
                result = session.run(query, **params)
                for record in result:
                    node = _row_to_existing(record)
                    if node is not None:
                        nodes.append(node)
        return nodes

    def fetch_candidates(
        self,
        incoming: IncomingNode,
        domain: str,
        *,
        limit: int | None = None,
        query_embedding: list[float] | None = None,
        use_vector_index: bool = False,
    ) -> list[ExistingNode]:
        """Fetch Neo4j candidates (READ). Dedupes by entity_id."""
        if not self.customer_id or not self.tenant_id:
            logger.warning("lane2.candidate_read.rejected_missing_customer_scope", domain=domain)
            return []
        lim = limit or self._default_limit
        raw_name = (incoming.name or "").strip()
        # CONTAINS fragments: full name (truncated) + longest significant token
        fragments: list[str] = []
        if raw_name:
            fragments.append(raw_name[:48])
            tokens = [
                t
                for t in re.split(r"\W+", normalize_entity_value(raw_name))
                if len(t) >= 4
            ]
            if tokens:
                longest = max(tokens, key=len)
                if longest not in {f.lower() for f in fragments}:
                    fragments.append(longest)
        node_type = incoming.node_type or None
        by_id: dict[str, ExistingNode] = {}

        # 1) name / alias / display match (per fragment)
        for name_fragment in fragments:
            for node in self._run_read(
                _CYPHER_BY_NAME_ALIAS,
                {
                    "domain": domain,
                    "customer_id": self.customer_id,
                    "tenant_id": self.tenant_id,
                    "product_line_id": self.product_line_id or "",
                    "node_type": node_type,
                    "name_fragment": name_fragment,
                    "limit": lim,
                },
            ):
                by_id[node.entity_id] = node

        # 2) broader type/domain pool for fusion ranking
        for node in self._run_read(
            _CYPHER_BY_TYPE_DOMAIN,
            {
                "domain": domain,
                "customer_id": self.customer_id,
                "tenant_id": self.tenant_id,
                "product_line_id": self.product_line_id or "",
                "node_type": node_type,
                "limit": lim,
            },
        ):
            by_id.setdefault(node.entity_id, node)

        # 3) optional vector index (still READ-only CALL)
        if (
            use_vector_index
            and self._vector_index_name
            and query_embedding
        ):
            try:
                for node in self._run_read(
                    _CYPHER_VECTOR_QUERY,
                    {
                        "index_name": self._vector_index_name,
                        "limit": lim,
                        "query_vector": query_embedding,
                        "domain": domain,
                        "customer_id": self.customer_id,
                        "tenant_id": self.tenant_id,
                        "product_line_id": self.product_line_id or "",
                        "node_type": node_type,
                    },
                ):
                    existing = by_id.get(node.entity_id)
                    if existing is None:
                        by_id[node.entity_id] = node
                    elif node.vector_score is not None and existing.vector_score is None:
                        existing.vector_score = node.vector_score
            except Exception as exc:  # noqa: BLE001 — vector index may not exist
                logger.info(
                    "lane2.pass3.vector_query_skipped",
                    error=str(exc),
                    index=self._vector_index_name,
                )

        return list(by_id.values())

    def fetch_candidate_neighborhoods(
        self, entity_ids: list[str], *, domain: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Read bounded, same-customer/domain graph context for reranking candidates."""
        if not entity_ids or not self.customer_id or not self.tenant_id:
            return {}
        query = """
        MATCH (e)-[r]-(n)
        WHERE e.entity_id IN $entity_ids
          AND e.customer_id = $customer_id AND e.tenant_id = $tenant_id AND e.domain = $domain
          AND n.customer_id = $customer_id AND n.tenant_id = $tenant_id AND n.domain = $domain
          AND NOT n:Block AND NOT n:Document AND NOT n:DomainRoot
        RETURN e.entity_id AS entity_id, type(r) AS rel_type,
               coalesce(n.kg_node_type, labels(n)[0]) AS other_type,
               coalesce(n.name_display, n.name) AS other_value,
               CASE WHEN startNode(r) = e THEN 'outgoing' ELSE 'incoming' END AS direction
        LIMIT $row_limit
        """.strip()
        result: dict[str, list[dict[str, Any]]] = {str(eid): [] for eid in entity_ids}
        try:
            from neo4j import READ_ACCESS
            with self._driver.session(default_access_mode=READ_ACCESS) as session:
                rows = session.execute_read(lambda tx: [dict(row) for row in tx.run(
                    query, entity_ids=entity_ids, customer_id=self.customer_id,
                    tenant_id=self.tenant_id, domain=domain,
                    row_limit=max(40, len(entity_ids) * 12),
                )])
            for row in rows:
                result.setdefault(str(row["entity_id"]), []).append(row)
        except Exception as exc:  # noqa: BLE001
            logger.info("lane2.candidate_neighborhoods.read_failed", error=str(exc))
        return result


def make_neo4j_driver_from_config(app_config: dict[str, Any] | None = None) -> Any:
    """Build Neo4j driver from app config (same pattern as other scripts)."""
    from neo4j import GraphDatabase

    if app_config is None:
        from kbfabric_ingest.ingest.config import get_app_config

        app_config = get_app_config()
    neo = app_config.get("neo4j", {}) or {}
    uri = neo.get("uri", "bolt://localhost:7687")
    user = neo.get("user", "neo4j")
    password = neo.get("password", "")
    pool = neo.get("connection_pool") or {}
    kwargs: dict[str, Any] = {}
    if pool.get("connection_timeout") is not None:
        try:
            kwargs["connection_timeout"] = float(pool["connection_timeout"])
        except (TypeError, ValueError):
            pass
    return GraphDatabase.driver(uri, auth=(user, password), **kwargs)


# ---------------------------------------------------------------------------
# In-memory HITL + correction patterns
# ---------------------------------------------------------------------------


class InMemoryCorrectionStore:
    def __init__(self) -> None:
        self._patterns: list[CorrectionPattern] = []

    def all(self) -> list[CorrectionPattern]:
        return list(self._patterns)

    def find_hit(
        self,
        incoming: IncomingNode,
        *,
        min_confidence: float = CORRECTION_PATTERN_MIN_CONFIDENCE,
    ) -> CorrectionPattern | None:
        key = normalize_entity_value(incoming.name)
        ntype = incoming.node_type
        domain = incoming.domain
        best: CorrectionPattern | None = None
        best_score = 0.0
        for p in self._patterns:
            if p.incoming_node_type != ntype:
                continue
            if domain and p.domain and p.domain != domain:
                continue
            if p.confidence < min_confidence:
                continue
            score = lexical_similarity(key, p.incoming_name_normalized)
            # Exact or near-exact name match preferred
            if key == p.incoming_name_normalized:
                score = 1.0
            if score >= 0.92 and score > best_score:
                best = p
                best_score = score
        return best

    def record(
        self,
        incoming: IncomingNode,
        action: ReviewerAction,
        *,
        selected_entity_id: str | None = None,
        edited_name: str | None = None,
        confidence: float = 1.0,
        notes: str = "",
    ) -> CorrectionPattern:
        pattern = CorrectionPattern(
            pattern_id=f"cp-{uuid.uuid4().hex[:12]}",
            incoming_name_normalized=normalize_entity_value(incoming.name),
            incoming_node_type=incoming.node_type,
            domain=incoming.domain,
            action=action,
            selected_entity_id=selected_entity_id,
            edited_name=edited_name,
            confidence=confidence,
            usage_count=0,
            notes=notes,
        )
        self._patterns.append(pattern)
        return pattern

    def increment(self, pattern: CorrectionPattern) -> None:
        pattern.usage_count += 1


class InMemoryReviewQueue:
    def __init__(self) -> None:
        self._items: list[ReviewQueueItem] = []

    def enqueue(self, item: ReviewQueueItem) -> ReviewQueueItem:
        self._items.append(item)
        return item

    def pending(self) -> list[ReviewQueueItem]:
        return [i for i in self._items if i.status == "pending"]

    def all(self) -> list[ReviewQueueItem]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


def simulate_reviewer(
    item: ReviewQueueItem,
    *,
    action: ReviewerAction | None = None,
    reassign_entity_id: str | None = None,
    edited_name: str | None = None,
    correction_store: InMemoryCorrectionStore | None = None,
) -> ReviewQueueItem:
    """In-memory reviewer simulation (no real HITL UI/API)."""
    if action is None:
        # Default heuristic: reuse top candidate if present, else approve as new
        if item.candidates:
            action = ReviewerAction.APPROVE_REUSE
            reassign_entity_id = item.candidates[0].candidate.entity_id
        else:
            action = ReviewerAction.APPROVE_AS_NEW

    item.reviewer_action = action
    item.status = "resolved"
    if action in (ReviewerAction.APPROVE_REUSE, ReviewerAction.REASSIGN, ReviewerAction.EDIT_AND_REUSE):
        eid = reassign_entity_id
        if eid is None and item.candidates:
            eid = item.candidates[0].candidate.entity_id
        item.resolved_entity_id = eid
    elif action == ReviewerAction.APPROVE_AS_NEW:
        item.resolved_entity_id = None

    if correction_store is not None and action != ReviewerAction.SKIP:
        correction_store.record(
            item.incoming,
            action,
            selected_entity_id=item.resolved_entity_id,
            edited_name=edited_name,
            confidence=1.0,
            notes=f"simulated review {item.review_id}",
        )
    return item


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------


def score_candidates(
    incoming: IncomingNode,
    existing: list[ExistingNode],
    *,
    semantic: SemanticSimilarityProvider,
    top_k: int = TOP_K,
    min_fusion: float = MIN_FUSION_SCORE,
    review_io: bool = False,
    query_context: str = "",
) -> tuple[list[CandidateMatch], str]:
    provider_name = semantic.name + ("(mock)" if semantic.is_mock else "")
    incoming_norm = normalize_entity_value(incoming.name)

    _review_section(review_io, "LEXICAL SIMILARITY (SequenceMatcher.ratio)")
    _review_print(
        review_io,
        "algorithm=difflib.SequenceMatcher.ratio (Gestalt / Ratcliff–Obershelp)",
        f"incoming_name={incoming.name!r}",
        f"incoming_normalized={incoming_norm!r}",
        f"candidates_to_score={len(existing)}",
    )
    logger.info(
        "lane2.pass3.lexical.start",
        incoming_name=incoming.name,
        incoming_normalized=incoming_norm,
        candidate_count=len(existing),
        algorithm="SequenceMatcher.ratio",
    )

    # Pre-compute lexical so we can log before embedding
    lexical_by_id: dict[str, tuple[float, str, list[tuple[str, float]]]] = {}
    for node in existing:
        field_scores: list[tuple[str, float]] = []
        names: list[tuple[str, str]] = [("name", node.name)]
        if node.name_display:
            names.append(("name_display", node.name_display))
        for alias in node.aliases or []:
            names.append(("alias", str(alias)))
        for field, raw in names:
            score = lexical_similarity(incoming.name, raw)
            field_scores.append((f"{field}:{raw}", score))
        best_field, best_lex = max(field_scores, key=lambda t: t[1], default=("", 0.0))
        lexical_by_id[node.entity_id] = (best_lex, best_field, field_scores)
        _review_print(
            review_io,
            _SUB,
            f"candidate={node.name!r} id={node.entity_id[:16]}…",
        )
        for label, score in field_scores:
            marker = " ← best" if label == best_field else ""
            _review_print(
                review_io,
                f"  lex vs {label!r} → {score:.4f}{marker}",
            )
        _review_print(review_io, f"  lexical_score={best_lex:.4f} (max over name/display/aliases)")
        logger.info(
            "lane2.pass3.lexical.candidate",
            incoming_name=incoming.name,
            candidate_name=node.name,
            entity_id=node.entity_id,
            lexical_score=round(best_lex, 6),
            best_field=best_field,
            field_scores={k: round(v, 6) for k, v in field_scores},
        )

    _review_section(review_io, "SEMANTIC SIMILARITY (embedding cosine)")
    semantic_query = "\n".join(part for part in [
        incoming.node_type, incoming.name, incoming.description or "", query_context,
    ] if part)
    _review_print(
        review_io,
        f"provider={provider_name}",
        "algorithm=cosine_similarity(incoming_embedding, candidate_embedding)",
        f"embedding_query_chars={len(semantic_query)} (name + description + source context)",
    )
    # Pseudo-embeddings are useful for demos but are not calibrated evidence
    # for an automatic identity decision.
    incoming_emb = None if semantic.is_mock else semantic.embed(semantic_query)
    if incoming_emb:
        _review_print(
            review_io,
            f"incoming_embedding: ok dims={len(incoming_emb)} "
            f"(first3={[round(x, 4) for x in incoming_emb[:3]]})",
        )
    else:
        _review_print(review_io, "incoming_embedding: FAILED / None → semantic scores may be 0")
    logger.info(
        "lane2.pass3.semantic.start",
        incoming_name=incoming.name,
        provider=provider_name,
        embed_ok=bool(incoming_emb),
        embed_dims=len(incoming_emb) if incoming_emb else 0,
        algorithm="cosine_similarity",
    )

    scored: list[CandidateMatch] = []
    for node in existing:
        lex, _, _ = lexical_by_id.get(node.entity_id, (0.0, "", []))

        if incoming_emb and node.embedding:
            sem = max(0.0, min(1.0, cosine_similarity(incoming_emb, node.embedding)))
            sem_source = "neo4j_stored_embedding"
        elif node.vector_score is not None:
            # Neo4j vector query already scored — clamp to 0..1-ish
            sem = max(0.0, min(1.0, float(node.vector_score)))
            sem_source = "neo4j_vector_index_score"
        else:
            sem = 0.0
            if not incoming_emb:
                sem_source = "no_incoming_embedding"
            elif not node.embedding:
                sem_source = "no_candidate_embedding"
            else:
                sem_source = "unavailable"

        fus = fusion_score(lex, sem)
        _review_print(
            review_io,
            _SUB,
            f"candidate={node.name!r} id={node.entity_id[:16]}…",
            f"  semantic_source={sem_source}",
            f"  candidate_has_stored_embedding={'yes' if node.embedding else 'no'}"
            + (f" dims={len(node.embedding)}" if node.embedding else ""),
            f"  semantic_score={sem:.4f}",
            f"  fusion = {LEXICAL_WEIGHT}*lex + {SEMANTIC_WEIGHT}*sem "
            f"= {LEXICAL_WEIGHT}*{lex:.4f} + {SEMANTIC_WEIGHT}*{sem:.4f} = {fus:.4f}",
        )
        logger.info(
            "lane2.pass3.semantic.candidate",
            incoming_name=incoming.name,
            candidate_name=node.name,
            entity_id=node.entity_id,
            semantic_score=round(sem, 6),
            semantic_source=sem_source,
            lexical_score=round(lex, 6),
            fusion_score=round(fus, 6),
            has_stored_embedding=bool(node.embedding),
        )
        scored.append(
            CandidateMatch(
                candidate=node,
                lexical_score=round(lex, 6),
                semantic_score=round(sem, 6),
                fusion_score=round(fus, 6),
                rank=0,
            )
        )

    from custom_ml.reranking.candidate_ranking import reciprocal_rank_fusion

    lexical_rank = sorted(scored, key=lambda c: c.lexical_score, reverse=True)
    semantic_rank = sorted(scored, key=lambda c: c.semantic_score, reverse=True)
    rankings = [
        [c.candidate.entity_id for c in lexical_rank],
        [c.candidate.entity_id for c in semantic_rank],
    ]
    selected_product_line = str(incoming.properties.get("product_line_id") or "")
    if selected_product_line:
        product_line_rank = sorted(
            scored,
            key=lambda c: selected_product_line in c.candidate.product_line_ids,
            reverse=True,
        )
        rankings.append([c.candidate.entity_id for c in product_line_rank])
    rrf_scores = reciprocal_rank_fusion(rankings)
    for candidate in scored:
        candidate.rrf_score = round(rrf_scores.get(candidate.candidate.entity_id, 0.0), 8)
    scored.sort(key=lambda c: (c.rrf_score, c.fusion_score), reverse=True)
    filtered = [c for c in scored if c.fusion_score >= min_fusion][:top_k]
    # If everything below min_fusion, still keep top_k for LLM context (policy may CREATE/REVIEW)
    if not filtered and scored:
        filtered = scored[:top_k]
    for i, c in enumerate(filtered, start=1):
        c.rank = i

    _review_section(review_io, "FUSION RANKING (top_k)")
    _review_print(
        review_io,
        "ordering=RRF(lexical_rank, semantic_rank); fusion_score remains a separate policy signal",
        f"min_fusion={min_fusion} top_k={top_k} kept={len(filtered)} of {len(scored)} scored",
    )
    for c in filtered:
        _review_print(
            review_io,
            f"  rank={c.rank} rrf={c.rrf_score:.5f} fusion={c.fusion_score:.4f} "
            f"lex={c.lexical_score:.4f} sem={c.semantic_score:.4f} "
            f"→ {c.candidate.name} ({c.candidate.entity_id[:16]}…)",
        )
    logger.info(
        "lane2.pass3.fusion.ranked",
        incoming_name=incoming.name,
        provider=provider_name,
        scored_count=len(scored),
        kept_count=len(filtered),
        top_fusion=filtered[0].fusion_score if filtered else None,
    )
    return filtered, provider_name


# ---------------------------------------------------------------------------
# Pass 3 LLM adjudication
# ---------------------------------------------------------------------------


def build_pass3_system_prompt() -> str:
    return "\n".join(
        [
            "You are a knowledge-graph node reconciliation adjudicator.",
            "Decide whether an incoming extracted entity refers to the SAME existing graph node,",
            "a DIFFERENT (novel) node, or is AMBIGUOUS among candidates.",
            "Do NOT invent new entity ids. Do NOT write chain-of-thought.",
            "",
            "SCORING SIGNALS (provided per candidate — you MUST use them):",
            "  lexical_score: string similarity of names/aliases (0..1)",
            "  semantic_score: embedding cosine / vector similarity (0..1)",
            "  fusion_score: weighted blend of lexical + semantic (0..1)",
            "Prefer SAME when fusion_score is high and names/types agree.",
            "Prefer DIFFERENT when fusion_score is low or types conflict.",
            "Prefer AMBIGUOUS when top candidates are close (near-tie) or evidence conflicts.",
            "",
            "OUTPUT: Return a SINGLE JSON object with keys:",
            '  decision: "SAME" | "DIFFERENT" | "AMBIGUOUS"',
            "  selected_candidate_id: entity_id string when decision is SAME, else null",
            "  confidence: float 0..1",
            "  explanation_summary: one short sentence citing the key scores (no chain-of-thought)",
            "Return ONLY JSON.",
        ]
    )


def build_pass3_user_content(
    incoming: IncomingNode,
    candidates: list[CandidateMatch],
) -> str:
    cand_payload = []
    for c in candidates:
        cand_payload.append(
            {
                "entity_id": c.candidate.entity_id,
                "name": c.candidate.name,
                "name_display": c.candidate.name_display,
                "aliases": c.candidate.aliases,
                "kg_node_type": c.candidate.kg_node_type,
                "description": (c.candidate.description or "")[:200],
                "lexical_score": c.lexical_score,
                "semantic_score": c.semantic_score,
                "fusion_score": c.fusion_score,
                "rank": c.rank,
            }
        )
    payload = {
        "incoming": {
            "temporary_id": incoming.temporary_id,
            "node_type": incoming.node_type,
            "name": incoming.name,
            "description": incoming.description,
            "domain": incoming.domain,
            "extraction_confidence": incoming.extraction_confidence,
            "source_file": incoming.source_file,
            "block_id": incoming.block_id,
        },
        "candidates": cand_payload,
        "thresholds": {
            "AUTO_RESOLUTION_CONFIDENCE_THRESHOLD": get_auto_resolution_confidence_threshold(),
            "LLM_CONFIDENCE_THRESHOLD": get_auto_resolution_confidence_threshold(),
            "AMBIGUITY_MARGIN": AMBIGUITY_MARGIN,
            "MIN_FUSION_SCORE": MIN_FUSION_SCORE,
        },
    }
    return json.dumps(payload, indent=2)


def adjudicate_with_llm(
    incoming: IncomingNode,
    candidates: list[CandidateMatch],
    *,
    llm_callable: LLMCallable,
    review_io: bool = False,
    source_context: str = "",
    graph_neighborhoods: dict[str, list[dict[str, Any]]] | None = None,
) -> LLMAdjudicationResult:
    system_prompt = build_pass3_system_prompt()
    user_content = build_pass3_user_content(incoming, candidates)
    user_content += "\n\nContext evidence (source block and scoped graph neighborhoods):\n"
    user_content += json.dumps({
        "source_context": source_context[:5000],
        "candidate_neighborhoods": graph_neighborhoods or {},
    }, ensure_ascii=False)
    score_rows = [
        {
            "rank": c.rank,
            "entity_id": c.candidate.entity_id,
            "name": c.candidate.name,
            "kg_node_type": c.candidate.kg_node_type,
            "lexical_score": c.lexical_score,
            "semantic_score": c.semantic_score,
            "fusion_score": c.fusion_score,
        }
        for c in candidates
    ]
    logger.info(
        "lane2.pass3.adjudicate.input",
        log_kind="input",
        block_id=incoming.block_id,
        incoming_name=incoming.name,
        incoming_node_type=incoming.node_type,
        domain=incoming.domain,
        candidate_count=len(candidates),
        candidate_scores=score_rows,
        scores_passed_to_llm=True,
        system_prompt_chars=len(system_prompt),
        user_content_chars=len(user_content),
        user_content_preview=user_content[:800],
    )
    print(
        f"[EntityMapping][INPUT] adjudicate "
        f"name={incoming.name!r} type={incoming.node_type} "
        f"candidates={len(candidates)} scores_to_llm=True",
        flush=True,
    )
    for row in score_rows:
        print(
            f"[EntityMapping][SCORES] rank={row['rank']} "
            f"lex={row['lexical_score']:.4f} sem={row['semantic_score']:.4f} "
            f"fusion={row['fusion_score']:.4f} "
            f"→ {row['name']!r} ({row['entity_id'][:16]}…)",
            flush=True,
        )
    _review_section(
        review_io,
        f"PASS 3 — ADJUDICATE  |  incoming={incoming.name!r}  type={incoming.node_type}",
    )
    _review_print(
        review_io,
        f"system_prompt ({len(system_prompt)} chars):",
        system_prompt,
        "",
        f"user_content ({len(user_content)} chars):",
        user_content,
        _SUB,
        "Calling LLM for Pass 3 adjudicate...",
        _SUB,
    )
    try:
        raw, metadata = llm_callable(system_prompt, user_content)
    except Exception as exc:  # noqa: BLE001
        result = LLMAdjudicationResult(
            decision=None,
            confidence=0.0,
            explanation_summary="LLM call failed",
            parse_error=f"llm_error: {exc}",
        )
        logger.info(
            "lane2.pass3.adjudicate.output",
            log_kind="output",
            block_id=incoming.block_id,
            success=False,
            error=str(exc),
            candidate_scores=score_rows,
        )
        print(f"[EntityMapping][OUTPUT] adjudicate ERROR: {exc}", flush=True)
        _review_print(review_io, f"OUTPUT ← Pass 3 ERROR: {exc}")
        return result

    duration_ms = metadata.get("duration_ms") if isinstance(metadata, dict) else None
    parsed = _parse_json_object(raw or "")
    if parsed is None:
        result = LLMAdjudicationResult(
            decision=None,
            confidence=0.0,
            explanation_summary="Malformed LLM response",
            raw_response=raw,
            duration_ms=duration_ms,
            parse_error="malformed_json",
        )
        logger.info(
            "lane2.pass3.adjudicate.output",
            log_kind="output",
            block_id=incoming.block_id,
            success=False,
            parse_error="malformed_json",
            llm_duration_ms=duration_ms,
            raw_response_preview=(raw or "")[:400],
            candidate_scores=score_rows,
        )
        print("[EntityMapping][OUTPUT] adjudicate malformed_json", flush=True)
        _review_print(review_io, f"OUTPUT ← Pass 3 malformed_json raw={raw!r}")
        return result

    raw_decision = str(parsed.get("decision") or "").strip().upper()
    decision: AdjudicationDecision | None = None
    parse_error: str | None = None
    try:
        decision = AdjudicationDecision(raw_decision)
    except ValueError:
        parse_error = f"invalid_decision:{raw_decision}"

    conf = float(parsed.get("confidence") or 0.0)
    conf = max(0.0, min(1.0, conf))
    selected = parsed.get("selected_candidate_id")
    if selected is not None:
        selected = str(selected)
    explanation = str(parsed.get("explanation_summary") or "")[:240]

    # Logic: whether selected id is among scored candidates
    selected_in_candidates = bool(
        selected and any(c.candidate.entity_id == selected for c in candidates)
    )
    selected_scores = next(
        (r for r in score_rows if r["entity_id"] == selected),
        None,
    )
    logger.info(
        "lane2.pass3.adjudicate.logic",
        log_kind="logic",
        block_id=incoming.block_id,
        raw_decision=raw_decision,
        selected_candidate_id=selected,
        selected_in_candidates=selected_in_candidates,
        selected_candidate_scores=selected_scores,
        llm_confidence=conf,
        decision=decision.value if decision else None,
    )
    print(
        f"[EntityMapping][LOGIC] decision={raw_decision} "
        f"selected={selected} in_candidates={selected_in_candidates} "
        f"llm_conf={conf:.3f} selected_scores={selected_scores}",
        flush=True,
    )

    result = LLMAdjudicationResult(
        decision=decision,
        selected_candidate_id=selected,
        confidence=conf,
        explanation_summary=explanation,
        raw_response=raw,
        duration_ms=duration_ms,
        parse_error=parse_error,
    )
    logger.info(
        "lane2.pass3.adjudicate.output",
        log_kind="output",
        block_id=incoming.block_id,
        success=parse_error is None and decision is not None,
        decision=result.decision.value if result.decision else None,
        selected_candidate_id=result.selected_candidate_id,
        confidence=result.confidence,
        explanation_summary=result.explanation_summary,
        parse_error=result.parse_error,
        llm_duration_ms=duration_ms,
        candidate_scores=score_rows,
        raw_response_preview=(raw or "")[:400],
    )
    print(
        f"[EntityMapping][OUTPUT] adjudicate "
        f"decision={result.decision} selected={result.selected_candidate_id} "
        f"conf={result.confidence:.3f} "
        f"explain={result.explanation_summary!r}",
        flush=True,
    )
    _review_print(
        review_io,
        "",
        _SUB,
        f"OUTPUT ← Pass 3 adjudicate"
        + (f"  ({duration_ms:.1f} ms)" if duration_ms is not None else ""),
        _SUB,
        f"raw_response ({len(raw or '')} chars):",
        raw or "(empty)",
        "",
        "parsed summary:",
        f"  decision={result.decision}",
        f"  selected_candidate_id={result.selected_candidate_id}",
        f"  confidence={result.confidence}",
        f"  explanation_summary={result.explanation_summary}",
        f"  parse_error={result.parse_error}",
        _SUB,
    )
    return result


# ---------------------------------------------------------------------------
# Deterministic decision policy
# ---------------------------------------------------------------------------


def apply_decision_policy(
    incoming: IncomingNode,
    candidates: list[CandidateMatch],
    adjudication: LLMAdjudicationResult | None,
) -> tuple[ReconciliationRecommendation, ReasonCode, str | None]:
    """Map scores + LLM adjudication → REUSE | CREATE | REVIEW."""
    high_fusion_threshold = get_auto_resolution_confidence_threshold()

    if adjudication is None or adjudication.parse_error or adjudication.decision is None:
        return (
            ReconciliationRecommendation.SEND_TO_REVIEW,
            ReasonCode.LLM_ERROR,
            None,
        )

    decision = adjudication.decision
    selected_id = adjudication.selected_candidate_id
    cand_ids = {c.candidate.entity_id for c in candidates}
    top = candidates[0] if candidates else None
    top_fusion = top.fusion_score if top else 0.0

    # Type conflict: LLM SAME to a candidate with conflicting kg_node_type
    if decision == AdjudicationDecision.SAME and selected_id:
        match = next((c for c in candidates if c.candidate.entity_id == selected_id), None)
        if match is None:
            return (
                ReconciliationRecommendation.SEND_TO_REVIEW,
                ReasonCode.INVALID_CANDIDATE_ID,
                None,
            )
        cand_type = match.candidate.kg_node_type
        if (
            cand_type
            and incoming.node_type
            and cand_type != incoming.node_type
            and incoming.node_type not in (match.candidate.labels or [])
        ):
            return (
                ReconciliationRecommendation.SEND_TO_REVIEW,
                ReasonCode.TYPE_CONFLICT,
                selected_id,
            )

    if decision == AdjudicationDecision.AMBIGUOUS:
        return (
            ReconciliationRecommendation.SEND_TO_REVIEW,
            ReasonCode.LLM_AMBIGUOUS,
            selected_id if selected_id in cand_ids else None,
        )

    # Strong fusion vs LLM DIFFERENT → conflict → REVIEW
    if (
        decision == AdjudicationDecision.DIFFERENT
        and top is not None
        and top_fusion > high_fusion_threshold
    ):
        return (
            ReconciliationRecommendation.SEND_TO_REVIEW,
            ReasonCode.FUSION_LLM_CONFLICT,
            None,
        )

    # LLM SAME + high confidence + valid id
    if decision == AdjudicationDecision.SAME:
        if not selected_id or selected_id not in cand_ids:
            return (
                ReconciliationRecommendation.SEND_TO_REVIEW,
                ReasonCode.INVALID_CANDIDATE_ID,
                None,
            )
        match = next(c for c in candidates if c.candidate.entity_id == selected_id)
        if (
            match.fusion_score > high_fusion_threshold
            and adjudication.confidence >= high_fusion_threshold
        ):
            return (
                ReconciliationRecommendation.REUSE_EXISTING,
                ReasonCode.HIGH_FUSION_LLM_SAME,
                selected_id,
            )
        if adjudication.confidence >= high_fusion_threshold and match.fusion_score >= MIN_FUSION_SCORE:
            return (
                ReconciliationRecommendation.REUSE_EXISTING,
                ReasonCode.LLM_SAME_VALID_CANDIDATE,
                selected_id,
            )
        return (
            ReconciliationRecommendation.SEND_TO_REVIEW,
            ReasonCode.POLICY_DEFAULT_REVIEW,
            selected_id,
        )

    # DIFFERENT
    if decision == AdjudicationDecision.DIFFERENT:
        if not candidates:
            return (
                ReconciliationRecommendation.CREATE_NEW,
                ReasonCode.NO_CANDIDATES,
                None,
            )
        if top_fusion < MIN_FUSION_SCORE:
            return (
                ReconciliationRecommendation.CREATE_NEW,
                ReasonCode.LOW_FUSION_LLM_DIFFERENT,
                None,
            )
        if adjudication.confidence >= high_fusion_threshold and top_fusion <= high_fusion_threshold:
            return (
                ReconciliationRecommendation.CREATE_NEW,
                ReasonCode.LLM_DIFFERENT,
                None,
            )
        return (
            ReconciliationRecommendation.SEND_TO_REVIEW,
            ReasonCode.BELOW_MIN_FUSION
            if top_fusion < MIN_FUSION_SCORE
            else ReasonCode.POLICY_DEFAULT_REVIEW,
            None,
        )

    return (
        ReconciliationRecommendation.SEND_TO_REVIEW,
        ReasonCode.POLICY_DEFAULT_REVIEW,
        None,
    )


# ---------------------------------------------------------------------------
# Entity → IncomingNode
# ---------------------------------------------------------------------------


def entity_to_incoming(
    entity: dict[str, Any],
    block: CanonicalBlock,
    *,
    index: int = 0,
) -> IncomingNode:
    raw_desc = entity.get("description")
    description = raw_desc.strip() if isinstance(raw_desc, str) and raw_desc.strip() else None
    return IncomingNode(
        temporary_id=f"{block.block_id}:ent:{index}:{uuid.uuid4().hex[:8]}",
        node_type=str(entity.get("type") or "Unknown"),
        name=str(entity.get("value") or "").strip(),
        description=description,
        properties={k: v for k, v in entity.items() if k not in ("type", "value", "confidence", "description")},
        source_file=block.source_file,
        block_id=block.block_id,
        domain=block.domain,
        extraction_confidence=float(entity.get("confidence") or 0.0),
    )


# ---------------------------------------------------------------------------
# Per-entity reconcile + block orchestrator
# ---------------------------------------------------------------------------


def reconcile_incoming_node(
    incoming: IncomingNode,
    *,
    candidate_reader: Neo4jCandidateReader,
    llm_callable: LLMCallable,
    semantic: SemanticSimilarityProvider,
    correction_store: InMemoryCorrectionStore | None = None,
    review_queue: InMemoryReviewQueue | None = None,
    domain: str | None = None,
    top_k: int = TOP_K,
    min_fusion: float = MIN_FUSION_SCORE,
    use_vector_index: bool = False,
    review_io: bool = False,
    skip_llm_if_correction_hit: bool = True,
    reranker: Any | None = None,
    source_context: str = "",
) -> ReconciliationResult:
    """Pass 3 for one IncomingNode. Never writes to Neo4j / Weaviate."""
    domain = domain or incoming.domain or ""
    store = correction_store or InMemoryCorrectionStore()
    queue = review_queue

    _review_section(
        review_io,
        f"PASS 3 — RECONCILE ENTITY  |  {incoming.name!r}  ({incoming.node_type})",
    )

    # C) Correction-pattern check
    hit = store.find_hit(incoming)
    if hit is not None and skip_llm_if_correction_hit:
        store.increment(hit)
        if hit.action in (
            ReviewerAction.APPROVE_REUSE,
            ReviewerAction.REASSIGN,
            ReviewerAction.EDIT_AND_REUSE,
        ):
            rec = ReconciliationRecommendation.REUSE_EXISTING
            selected = hit.selected_entity_id
        elif hit.action == ReviewerAction.APPROVE_AS_NEW:
            rec = ReconciliationRecommendation.CREATE_NEW
            selected = None
        else:
            rec = ReconciliationRecommendation.SEND_TO_REVIEW
            selected = None
        result = ReconciliationResult(
            incoming=incoming,
            recommendation=rec,
            reason_code=ReasonCode.CORRECTION_PATTERN_HIT,
            selected_entity_id=selected,
            candidates=[],
            adjudication=None,
            correction_pattern_hit=True,
            correction_pattern_id=hit.pattern_id,
            neo4j_read_executed=False,
            candidate_fetch_count=0,
            semantic_provider=None,
            graph_write_performed=False,
            weaviate_write_performed=False,
            neo4j_write_attempted=False,
        )
        _review_print(
            review_io,
            f"correction_pattern_hit id={hit.pattern_id} action={hit.action} "
            f"usage_count={hit.usage_count} → {rec.value}",
            "skipped Neo4j semantic ranking extras + LLM adjudication",
            "write flags: graph=False weaviate=False neo4j_write_attempted=False",
        )
        return result

    # D) Neo4j READ
    retrieval_query = "\n".join(part for part in [
        incoming.node_type, incoming.name, incoming.description or "", source_context,
    ] if part)
    query_emb = semantic.embed(retrieval_query) if use_vector_index else None
    existing = candidate_reader.fetch_candidates(
        incoming,
        domain,
        query_embedding=query_emb,
        use_vector_index=use_vector_index,
    )
    _review_print(
        review_io,
        f"Neo4j READ candidates: {len(existing)} "
        f"(queries_run={candidate_reader.read_query_count} write_attempted={candidate_reader.write_attempted})",
    )
    for n in existing[:10]:
        _review_print(
            review_io,
            f"  - {n.kg_node_type}:{n.name} id={n.entity_id[:16]}… "
            f"aliases={n.aliases[:3]} emb={'yes' if n.embedding else 'no'}",
        )

    # E–G) lexical + semantic + fusion (step logs printed inside score_candidates)
    candidates, provider_name = score_candidates(
        incoming,
        existing,
        semantic=semantic,
        top_k=max(top_k * 3, top_k),
        min_fusion=min_fusion,
        review_io=review_io,
        query_context=source_context,
    )
    graph_neighborhoods: dict[str, list[dict[str, Any]]] = {}
    if candidates and hasattr(candidate_reader, "fetch_candidate_neighborhoods"):
        graph_neighborhoods = candidate_reader.fetch_candidate_neighborhoods(
            [c.candidate.entity_id for c in candidates], domain=domain
        )
    # Cross-encoder sees source block evidence and a bounded graph neighborhood.
    # Its raw logits only reorder the RRF shortlist; they are not admission scores.
    if reranker is not None and candidates:
        from custom_ml.reranking.candidate_ranking import cross_encoder_scores

        query_text = "\n".join(part for part in [
            f"Incoming {incoming.node_type}: {incoming.name}",
            incoming.description or "",
            source_context[:5000],
        ] if part)
        ce_scores = cross_encoder_scores(
            query=query_text,
            candidates=[c.candidate for c in candidates],
            reranker=reranker,
            neighborhoods=graph_neighborhoods,
        )
        if ce_scores:
            for candidate in candidates:
                candidate.cross_encoder_score = ce_scores.get(candidate.candidate.entity_id)
            candidates.sort(
                key=lambda c: (c.cross_encoder_score is not None,
                               c.cross_encoder_score if c.cross_encoder_score is not None else c.rrf_score),
                reverse=True,
            )
        logger.info(
            "lane2.pass3.reranking.completed", block_id=incoming.block_id,
            shortlist_count=len(ce_scores), reranked=bool(ce_scores),
            context_chars=len(query_text), graph_context_candidate_count=len(graph_neighborhoods),
        )
    candidates = candidates[:top_k]
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank
    _review_print(review_io, f"semantic_provider={provider_name}  top_k={len(candidates)}")

    # H) LLM adjudication
    adjudication = adjudicate_with_llm(
        incoming,
        candidates,
        llm_callable=llm_callable,
        review_io=review_io,
        source_context=source_context,
        graph_neighborhoods=graph_neighborhoods,
    )

    # I) Policy
    recommendation, reason, selected = apply_decision_policy(
        incoming, candidates, adjudication
    )

    review_item_id: str | None = None
    if recommendation == ReconciliationRecommendation.SEND_TO_REVIEW and queue is not None:
        item = ReviewQueueItem(
            review_id=f"rv-{uuid.uuid4().hex[:12]}",
            incoming=incoming,
            candidates=candidates,
            adjudication=adjudication,
            reason_code=reason,
        )
        queue.enqueue(item)
        review_item_id = item.review_id
        _review_print(review_io, f"enqueued HITL review_id={review_item_id} reason={reason.value}")
    elif recommendation == ReconciliationRecommendation.SEND_TO_REVIEW:
        _review_print(
            review_io,
            f"SEND_TO_REVIEW reason={reason.value} (HITL queue disabled — not enqueued)",
        )

    result = ReconciliationResult(
        incoming=incoming,
        recommendation=recommendation,
        reason_code=reason,
        selected_entity_id=selected,
        candidates=candidates,
        adjudication=adjudication,
        correction_pattern_hit=False,
        review_item_id=review_item_id,
        neo4j_read_executed=True,
        candidate_fetch_count=len(existing),
        semantic_provider=provider_name,
        graph_write_performed=False,
        weaviate_write_performed=False,
        neo4j_write_attempted=False,
    )
    assert result.graph_write_performed is False
    assert result.weaviate_write_performed is False
    assert result.neo4j_write_attempted is False
    assert candidate_reader.write_attempted is False

    _review_print(
        review_io,
        f"recommendation={recommendation.value} reason_code={reason.value} "
        f"selected_entity_id={selected}",
        "write flags: graph=False weaviate=False neo4j_write_attempted=False",
    )
    logger.info(
        "lane2.pass3.reconcile",
        temporary_id=incoming.temporary_id,
        name=incoming.name,
        node_type=incoming.node_type,
        recommendation=recommendation.value,
        reason_code=reason.value,
        candidate_count=len(candidates),
        neo4j_fetch_count=len(existing),
        correction_pattern_hit=False,
        graph_write_performed=False,
        weaviate_write_performed=False,
        neo4j_write_attempted=False,
    )
    return result


def process_lane2_block_with_reconciliation(
    block: CanonicalBlock,
    *,
    llm_callable: LLMCallable,
    candidate_reader: Neo4jCandidateReader,
    semantic: SemanticSimilarityProvider,
    prompt_provider: Any | None = None,
    ontology: dict[str, Any] | None = None,
    correction_store: InMemoryCorrectionStore | None = None,
    review_queue: InMemoryReviewQueue | None = None,
    top_k: int = TOP_K,
    min_fusion: float = MIN_FUSION_SCORE,
    use_vector_index: bool = False,
    review_io: bool = False,
    auto_simulate_reviews: bool = False,
) -> Lane2ReconcileRunResult:
    """Pass 1 + Pass 2 + Pass 3 for one block. No graph / Weaviate writes."""
    start = time.perf_counter()
    if prompt_provider is None:
        prompt_provider = YamlOntologyProvider(ontology)

    store = correction_store  # None ⇒ no correction-pattern lookup / HITL persistence
    queue = review_queue  # None ⇒ do not enqueue SEND_TO_REVIEW items (HITL off)

    two_pass = process_lane2_block(
        block,
        llm_callable=llm_callable,
        prompt_provider=prompt_provider,
        ontology=ontology,
        review_io=review_io,
    )

    reconciliations: list[ReconciliationResult] = []
    entities = (
        list(two_pass.extraction.entities)
        if two_pass.extraction is not None
        else []
    )
    for idx, ent in enumerate(entities):
        name = str(ent.get("value") or "").strip()
        if not name:
            continue
        incoming = entity_to_incoming(ent, block, index=idx)
        rec = reconcile_incoming_node(
            incoming,
            candidate_reader=candidate_reader,
            llm_callable=llm_callable,
            semantic=semantic,
            correction_store=store,
            review_queue=queue,
            domain=block.domain,
            top_k=top_k,
            min_fusion=min_fusion,
            use_vector_index=use_vector_index,
            review_io=review_io,
        )
        reconciliations.append(rec)

    if auto_simulate_reviews and queue is not None and store is not None:
        for item in list(queue.pending()):
            simulate_reviewer(item, correction_store=store)

    total_ms = (time.perf_counter() - start) * 1000.0
    result = Lane2ReconcileRunResult(
        block_id=block.block_id,
        source_file=block.source_file,
        domain=block.domain,
        two_pass=two_pass,
        reconciliations=reconciliations,
        review_queue_size=len(queue) if queue is not None else 0,
        total_duration_ms=total_ms,
        graph_write_performed=False,
        weaviate_write_performed=False,
        neo4j_write_attempted=False,
        success=two_pass.success,
    )
    _review_section(
        review_io,
        f"RECONCILE DONE  |  {block.block_id}  |  entities={len(reconciliations)}  |  {total_ms:.1f} ms",
    )
    _review_print(
        review_io,
        f"review_queue_size={len(queue) if queue is not None else 0} "
        f"(HITL={'on' if queue is not None else 'off'})",
        "graph_write_performed=False weaviate_write_performed=False neo4j_write_attempted=False",
    )
    return result

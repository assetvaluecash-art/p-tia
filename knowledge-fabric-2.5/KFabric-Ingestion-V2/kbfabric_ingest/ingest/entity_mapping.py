"""Shared existing-node mapping utility for Lane 1, Lane 2, and Lane 3.

Builds on ``lane2_node_reconciliation`` candidate retrieval, lexical/semantic
fusion, and decision policy. Adds:

* MappingDecision statuses required for delta graph updates
* Relationship-context compatibility scoring
* Ontology-driven parameterized Cypher builders for candidate reads / delta writes
* A lane-agnostic ``map_extracted_entity`` entry point

Graph writes remain opt-in via ``apply_delta_plan``; Pass 3 stay read-only by default.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.ontology_admission import compile_relationship_triples
from kbfabric_ingest.ingest.lane2_node_reconciliation import (
    MIN_FUSION_SCORE,
    TOP_K,
    AdjudicationDecision,
    CandidateMatch,
    ExistingNode,
    IncomingNode,
    LLMAdjudicationResult,
    Neo4jCandidateReader,
    ReasonCode,
    ReconciliationRecommendation,
    ReconciliationResult,
    SemanticSimilarityProvider,
    apply_decision_policy,
    confidence_allows_auto_resolution,
    entity_to_incoming,
    lexical_similarity,
    reconcile_incoming_node,
    score_candidates,
)
from kbfabric_ingest.ingest.lane2_two_pass import get_ontology_neighborhood
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.relationship_endpoint_resolution import (
    ContextResolutionResult,
    resolve_context_relationship,
)
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MappingDecision(str, Enum):
    """Delta-oriented mapping decisions consumed by writers / later taxonomy stages."""

    REUSE_EXISTING = "REUSE_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    CREATE_DELTA_RELATIONSHIP = "CREATE_DELTA_RELATIONSHIP"
    CREATE_DELTA_NODE_AND_RELATIONSHIP = "CREATE_DELTA_NODE_AND_RELATIONSHIP"
    NO_CHANGE = "NO_CHANGE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


# Alias for callers that still use Pass 3 vocabulary
REQUIRES_REVIEW = MappingDecision.REQUIRES_REVIEW


def recommendation_to_mapping_decision(
    recommendation: ReconciliationRecommendation | str,
) -> MappingDecision:
    value = recommendation.value if isinstance(recommendation, Enum) else str(recommendation)
    if value == ReconciliationRecommendation.SEND_TO_REVIEW.value or value == "REQUIRES_REVIEW":
        return MappingDecision.REQUIRES_REVIEW
    if value == ReconciliationRecommendation.REUSE_EXISTING.value:
        return MappingDecision.REUSE_EXISTING
    if value == ReconciliationRecommendation.CREATE_NEW.value:
        return MappingDecision.CREATE_NEW
    try:
        return MappingDecision(value)
    except ValueError:
        return MappingDecision.REQUIRES_REVIEW


class ProposedRelationship(BaseModel):
    direction: str  # incoming | outgoing
    rel_type: str
    other_type: str
    other_value: str
    confidence: float = 0.0


class MappingScoreBreakdown(BaseModel):
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    fusion_score: float = 0.0
    type_match_score: float = 0.0
    relationship_compatibility_score: float = 0.0
    combined_confidence: float = 0.0
    signals: list[str] = Field(default_factory=list)


class EntityMappingResult(BaseModel):
    """Stable mapping result for all lanes and later blueprint/taxonomy stages."""

    lane: str
    incoming: IncomingNode
    decision: MappingDecision
    reason_code: str
    selected_entity_id: str | None = None
    created_entity_id: str | None = None
    candidates: list[CandidateMatch] = Field(default_factory=list)
    score_breakdown: MappingScoreBreakdown = Field(default_factory=MappingScoreBreakdown)
    proposed_incoming: list[ProposedRelationship] = Field(default_factory=list)
    proposed_outgoing: list[ProposedRelationship] = Field(default_factory=list)
    ontology_constraints: dict[str, Any] = Field(default_factory=dict)
    delta_relationships: list[dict[str, Any]] = Field(default_factory=list)
    delta_nodes: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    context_resolutions: list[ContextResolutionResult] = Field(default_factory=list)
    reconciliation: ReconciliationResult | None = None
    cypher_plan: list[dict[str, Any]] = Field(default_factory=list)






















def _safe_label(label: str) -> str:
    if not label or not _SAFE_IDENT_RE.match(label):
        raise ValueError(f"Unsafe Neo4j label: {label!r}")
    return label


def build_candidate_retrieval_cypher(
    *,
    node_type: str | None,
    use_name_fragment: bool = True,
) -> str:
    """Parameterized candidate retrieval Cypher driven by node type constraint.

    Values (tenant_id, customer_id, domain, name_fragment, node_type, limit) MUST be passed as params.
    """
    type_clause = ""
    if node_type:
        _safe_label(node_type)
        type_clause = (
            "AND ($node_type IS NULL OR e.kg_node_type = $node_type "
            "OR $node_type IN labels(e))"
        )
    name_clause = ""
    if use_name_fragment:
        name_clause = """
  AND (
    toLower(e.name) CONTAINS toLower($name_fragment)
    OR toLower(coalesce(e.name_display, '')) CONTAINS toLower($name_fragment)
    OR ANY(a IN coalesce(e.aliases, []) WHERE toLower(toString(a)) CONTAINS toLower($name_fragment))
  )"""
    return f"""
MATCH (e)
WHERE e.domain = $domain
  AND e.customer_id = $customer_id
  AND e.tenant_id = $tenant_id
  AND e.name IS NOT NULL
  AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
  {type_clause}
  {name_clause}
RETURN e.entity_id AS entity_id,
       e.name AS name,
       coalesce(e.name_display, e.name) AS name_display,
       e.aliases AS aliases,
       coalesce(e.kg_node_type, labels(e)[0]) AS kg_node_type,
       labels(e) AS labels,
       e.embedding AS embedding,
       coalesce(e.description, e.summary, '') AS description
LIMIT $limit
""".strip()


def build_neighbor_context_cypher() -> str:
    """Read existing relationship neighborhood for a candidate entity_id (params only)."""
    return """
MATCH (e {entity_id: $entity_id})
OPTIONAL MATCH (e)-[r]->(out)
WHERE NOT out:Block AND NOT out:Document AND NOT out:DomainRoot
OPTIONAL MATCH (inn)-[rin]->(e)
WHERE NOT inn:Block AND NOT inn:Document AND NOT inn:DomainRoot
RETURN e.entity_id AS entity_id,
       collect(DISTINCT {
         direction: 'outgoing',
         rel_type: type(r),
         other_type: coalesce(out.kg_node_type, labels(out)[0]),
         other_value: out.name,
         other_id: out.entity_id
       }) AS outgoing,
       collect(DISTINCT {
         direction: 'incoming',
         rel_type: type(rin),
         other_type: coalesce(inn.kg_node_type, labels(inn)[0]),
         other_value: inn.name,
         other_id: inn.entity_id
       }) AS incoming
""".strip()


def build_reuse_node_provenance_cypher(*, node_type: str) -> dict[str, Any]:
    """MERGE existing node by entity_id and append source-block provenance."""
    label = _safe_label(node_type)
    query = f"""
MERGE (e:{label} {{entity_id: $entity_id}})
ON MATCH SET
  e.source_blocks = reduce(s = coalesce(e.source_blocks, []), x IN $source_block_ids |
      CASE WHEN x IN s THEN s ELSE s + x END),
  e.source_files = CASE WHEN NOT $source_file IN coalesce(e.source_files, [])
      THEN coalesce(e.source_files, []) + $source_file ELSE e.source_files END,
  e.updated_at = datetime()
ON CREATE SET
  e.name = $name,
  e.name_display = $name,
  e.domain = $domain,
  e.kg_node_type = $node_type,
  e.source_block_id = $source_block_id,
  e.source_blocks = $source_block_ids,
  e.source_file = $source_file,
  e.source_files = [$source_file],
  e.created_at = datetime(),
  e.updated_at = datetime()
RETURN e.entity_id AS entity_id
""".strip()
    return {"query": query, "purpose": "reuse_or_create_with_provenance"}


def build_delta_relationship_cypher(
    *,
    from_type: str,
    to_type: str,
    rel_type: str,
) -> dict[str, Any]:
    """Parameterized MERGE for a missing ontology relationship."""
    fl = _safe_label(from_type)
    tl = _safe_label(to_type)
    if not _SAFE_IDENT_RE.match(rel_type):
        raise ValueError(f"Unsafe relationship type: {rel_type!r}")
    query = f"""
MATCH (a:{fl} {{entity_id: $from_entity_id}})
MATCH (b:{tl} {{entity_id: $to_entity_id}})
MERGE (a)-[r:{rel_type}]->(b)
ON CREATE SET
  r.source_block_id = $source_block_id,
  r.source_block_ids = $source_block_ids,
  r.created_at = datetime()
ON MATCH SET
  r.source_block_ids = reduce(s = coalesce(r.source_block_ids, []), x IN $source_block_ids |
      CASE WHEN x IN s THEN s ELSE s + x END),
  r.updated_at = datetime()
RETURN type(r) AS rel_type, a.entity_id AS from_id, b.entity_id AS to_id
""".strip()
    return {"query": query, "purpose": "delta_relationship", "rel_type": rel_type}


def relationships_from_extraction(
    entity: dict[str, Any],
    relationships: list[dict[str, Any]] | None,
) -> tuple[list[ProposedRelationship], list[ProposedRelationship]]:
    """Split proposed rels into incoming/outgoing relative to the entity value/type."""
    name = str(entity.get("value") or "").strip().lower()
    etype = str(entity.get("type") or "").strip().lower()
    incoming: list[ProposedRelationship] = []
    outgoing: list[ProposedRelationship] = []
    for rel in relationships or []:
        if not isinstance(rel, dict):
            continue
        ft = str(rel.get("from_type") or "")
        fv = str(rel.get("from_value") or "")
        tt = str(rel.get("to_type") or "")
        tv = str(rel.get("to_value") or "")
        rt = str(rel.get("rel_type") or "")
        conf = float(rel.get("confidence") or 0.0)
        if not rt:
            continue
        if fv.strip().lower() == name and ft.strip().lower() == etype:
            outgoing.append(
                ProposedRelationship(
                    direction="outgoing",
                    rel_type=rt,
                    other_type=tt,
                    other_value=tv,
                    confidence=conf,
                )
            )
        elif tv.strip().lower() == name and tt.strip().lower() == etype:
            incoming.append(
                ProposedRelationship(
                    direction="incoming",
                    rel_type=rt,
                    other_type=ft,
                    other_value=fv,
                    confidence=conf,
                )
            )
    return incoming, outgoing


def score_relationship_compatibility(
    proposed_incoming: list[ProposedRelationship],
    proposed_outgoing: list[ProposedRelationship],
    existing_incoming: list[dict[str, Any]] | None,
    existing_outgoing: list[dict[str, Any]] | None,
    ontology_constraints: dict[str, Any] | None = None,
) -> float:
    """Score how well proposed relationships fit candidate graph + ontology.

    Returns 0..1. Empty proposals → 1.0 (no conflict). Ontology-invalid proposals
    reduce the score even when the candidate has no neighborhood yet.
    """
    constraints = ontology_constraints or {}
    allowed_rels = set(constraints.get("relationship_types") or [])
    allowed_triples = {
        tuple(triple)
        for triple in (constraints.get("allowed_relationship_triples") or [])
        if isinstance(triple, (list, tuple)) and len(triple) == 3
    }
    node_type = str(constraints.get("node_type") or "").strip()

    proposals = list(proposed_incoming) + list(proposed_outgoing)
    if not proposals:
        return 1.0

    valid = 0
    for p in proposals:
        if allowed_triples:
            if p.direction == "incoming":
                triple = (p.other_type, p.rel_type, node_type)
            else:
                triple = (node_type, p.rel_type, p.other_type)

            if triple not in allowed_triples:
                continue
        elif allowed_rels and p.rel_type not in allowed_rels:
            continue

        valid += 1

    ontology_score = valid / len(proposals)

    existing = list(existing_incoming or []) + list(existing_outgoing or [])
    if not existing:
        return round(ontology_score, 6)

    def _key(rel_type: str, other_type: str, other_value: str) -> tuple[str, str, str]:
        return (
            rel_type.strip().lower(),
            other_type.strip().lower(),
            other_value.strip().lower(),
        )

    existing_keys = {
        _key(
            str(e.get("rel_type") or ""),
            str(e.get("other_type") or ""),
            str(e.get("other_value") or ""),
        )
        for e in existing
        if e.get("rel_type")
    }
    overlap = 0
    for p in proposals:
        key = _key(p.rel_type, p.other_type, p.other_value)
        if key in existing_keys:
            overlap += 1
            continue
        # Soft lexical match on other_value against same rel_type+other_type
        for ek in existing_keys:
            if ek[0] == key[0] and ek[1] == key[1] and lexical_similarity(ek[2], key[2]) >= 0.85:
                overlap += 1
                break
    graph_score = overlap / len(proposals) if proposals else 1.0
    # Prefer ontology validity; boost when graph already has compatible edges
    return round(0.6 * ontology_score + 0.4 * graph_score, 6)


def type_match_score(incoming_type: str, candidate_type: str | None) -> float:
    if not incoming_type or not candidate_type:
        return 0.0
    if incoming_type == candidate_type:
        return 1.0
    if incoming_type.lower() == candidate_type.lower():
        return 0.95
    return 0.0


def combine_mapping_confidence(
    *,
    fusion: float,
    type_score: float,
    rel_score: float,
) -> MappingScoreBreakdown:
    # Node and relationship resolution share one policy confidence: the
    # normalized lexical/semantic fusion score. Type is a hard eligibility
    # gate; graph-neighborhood and relationship context inform reranking.
    combined = fusion
    signals = [
        f"lexical_semantic_fusion={fusion:.4f}",
        f"type_match_eligibility={type_score:.4f}",
        f"relationship_compatibility_context={rel_score:.4f}",
    ]
    return MappingScoreBreakdown(
        fusion_score=round(fusion, 6),
        type_match_score=round(type_score, 6),
        relationship_compatibility_score=round(rel_score, 6),
        combined_confidence=round(min(1.0, max(0.0, combined)), 6),
        signals=signals,
    )


def fetch_candidate_neighborhood(
    candidate_reader: Neo4jCandidateReader | None,
    entity_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Best-effort READ of candidate relationship context."""
    if candidate_reader is None or not entity_id:
        return [], []
    session_factory = getattr(candidate_reader, "_session", None)
    run = getattr(candidate_reader, "run_read", None)
    query = build_neighbor_context_cypher()
    try:
        if callable(run):
            rows = run(query, {"entity_id": entity_id})
        elif callable(session_factory):
            with session_factory() as session:
                rows = [dict(r) for r in session.run(query, entity_id=entity_id)]
        else:
            driver = getattr(candidate_reader, "driver", None) or getattr(
                candidate_reader, "_driver", None
            )
            if driver is None:
                return [], []
            with driver.session() as session:
                rows = [dict(r) for r in session.run(query, entity_id=entity_id)]
        if not rows:
            return [], []
        row = rows[0] if isinstance(rows[0], dict) else dict(rows[0])
        incoming = [x for x in (row.get("incoming") or []) if isinstance(x, dict) and x.get("rel_type")]
        outgoing = [x for x in (row.get("outgoing") or []) if isinstance(x, dict) and x.get("rel_type")]
        return incoming, outgoing
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "entity_mapping.neighborhood_read_failed",
            entity_id=entity_id,
            error=str(exc),
        )
        return [], []


def enrich_candidates_with_context(
    incoming: IncomingNode,
    candidates: list[CandidateMatch],
    *,
    proposed_incoming: list[ProposedRelationship],
    proposed_outgoing: list[ProposedRelationship],
    ontology_constraints: dict[str, Any],
    candidate_reader: Neo4jCandidateReader | None = None,
) -> list[tuple[CandidateMatch, MappingScoreBreakdown]]:
    """Re-rank candidates with type + relationship context on top of fusion."""
    ranked: list[tuple[CandidateMatch, MappingScoreBreakdown]] = []
    for cand in candidates:
        existing_in = [
            {
                "rel_type": edge.get("rel_type"),
                "other_type": edge.get("other_type"),
                "other_value": edge.get("other_value"),
            }
            for edge in cand.graph_neighborhood
            if edge.get("direction") == "incoming"
        ]
        existing_out = [
            {
                "rel_type": edge.get("rel_type"),
                "other_type": edge.get("other_type"),
                "other_value": edge.get("other_value"),
            }
            for edge in cand.graph_neighborhood
            if edge.get("direction") == "outgoing"
        ]
        rel_score = score_relationship_compatibility(
            proposed_incoming,
            proposed_outgoing,
            existing_in,
            existing_out,
            ontology_constraints,
        )
        t_score = type_match_score(incoming.node_type, cand.candidate.kg_node_type)
        breakdown = combine_mapping_confidence(
            fusion=cand.fusion_score,
            type_score=t_score,
            rel_score=rel_score,
        )
        breakdown.lexical_score = cand.lexical_score
        breakdown.semantic_score = cand.semantic_score
        ranked.append((cand, breakdown))
    ranked.sort(key=lambda t: t[1].combined_confidence, reverse=True)
    return ranked


def decide_delta_actions(
    *,
    base_decision: MappingDecision,
    selected_entity_id: str | None,
    proposed_incoming: list[ProposedRelationship],
    proposed_outgoing: list[ProposedRelationship],
    existing_incoming: list[dict[str, Any]] | None,
    existing_outgoing: list[dict[str, Any]] | None,
    related_entity_exists: dict[str, bool] | None = None,
) -> tuple[MappingDecision, list[dict[str, Any]], list[dict[str, Any]]]:
    """Refine REUSE into delta relationship / related-node actions."""
    related_entity_exists = related_entity_exists or {}
    delta_rels: list[dict[str, Any]] = []
    delta_nodes: list[dict[str, Any]] = []

    if base_decision in (MappingDecision.REQUIRES_REVIEW, MappingDecision.CREATE_NEW):
        return base_decision, delta_rels, delta_nodes

    if base_decision != MappingDecision.REUSE_EXISTING or not selected_entity_id:
        return base_decision, delta_rels, delta_nodes

    def _exists(rel: ProposedRelationship) -> bool:
        pool = existing_incoming if rel.direction == "incoming" else existing_outgoing
        for e in pool or []:
            if (
                str(e.get("rel_type") or "").lower() == rel.rel_type.lower()
                and str(e.get("other_type") or "").lower() == rel.other_type.lower()
                and lexical_similarity(str(e.get("other_value") or ""), rel.other_value) >= 0.9
            ):
                return True
        return False

    missing_rels = []
    for rel in list(proposed_incoming) + list(proposed_outgoing):
        if _exists(rel):
            continue
        other_key = f"{rel.other_type}:{rel.other_value}".lower()
        if related_entity_exists.get(other_key) is False:
            delta_nodes.append(
                {
                    "node_type": rel.other_type,
                    "name": rel.other_value,
                    "reason": "related_entity_missing",
                }
            )
            delta_rels.append(
                {
                    "rel_type": rel.rel_type,
                    "direction": rel.direction,
                    "other_type": rel.other_type,
                    "other_value": rel.other_value,
                    "primary_entity_id": selected_entity_id,
                }
            )
            missing_rels.append(rel)
        else:
            delta_rels.append(
                {
                    "rel_type": rel.rel_type,
                    "direction": rel.direction,
                    "other_type": rel.other_type,
                    "other_value": rel.other_value,
                    "primary_entity_id": selected_entity_id,
                }
            )
            missing_rels.append(rel)

    if not missing_rels and not delta_nodes:
        return MappingDecision.NO_CHANGE, [], []
    if delta_nodes:
        return MappingDecision.CREATE_DELTA_NODE_AND_RELATIONSHIP, delta_nodes, delta_rels
    return MappingDecision.CREATE_DELTA_RELATIONSHIP, delta_nodes, delta_rels


def build_cypher_plan_for_mapping(result: EntityMappingResult) -> list[dict[str, Any]]:
    """Generate parameterized Cypher steps for the mapping decision (no execution)."""
    plan: list[dict[str, Any]] = []
    node_type = result.incoming.node_type
    provenance = result.provenance
    source_block_ids = provenance.get("source_block_ids") or (
        [provenance["source_block_id"]] if provenance.get("source_block_id") else []
    )
    params_base = {
        "domain": result.incoming.domain,
        "node_type": node_type,
        "name": result.incoming.name,
        "source_file": provenance.get("source_document") or result.incoming.source_file,
        "source_block_id": provenance.get("source_block_id") or result.incoming.block_id,
        "source_block_ids": source_block_ids,
    }

    if result.decision == MappingDecision.NO_CHANGE:
        plan.append(
            {
                **build_reuse_node_provenance_cypher(node_type=node_type),
                "params": {
                    **params_base,
                    "entity_id": result.selected_entity_id,
                },
                "note": "attach provenance only",
            }
        )
        return plan

    if result.decision in (
        MappingDecision.REUSE_EXISTING,
        MappingDecision.CREATE_DELTA_RELATIONSHIP,
        MappingDecision.CREATE_DELTA_NODE_AND_RELATIONSHIP,
    ):
        plan.append(
            {
                **build_reuse_node_provenance_cypher(node_type=node_type),
                "params": {
                    **params_base,
                    "entity_id": result.selected_entity_id,
                },
            }
        )

    if result.decision == MappingDecision.CREATE_NEW:
        plan.append(
            {
                **build_reuse_node_provenance_cypher(node_type=node_type),
                "params": {
                    **params_base,
                    "entity_id": result.created_entity_id or "$compute_entity_id",
                },
                "note": "CREATE_NEW — entity_id from KGWriter hash when writing",
            }
        )

    for node in result.delta_nodes:
        try:
            plan.append(
                {
                    **build_reuse_node_provenance_cypher(node_type=str(node["node_type"])),
                    "params": {
                        **params_base,
                        "node_type": node["node_type"],
                        "name": node["name"],
                        "entity_id": "$compute_entity_id",
                    },
                    "note": "delta related node",
                }
            )
        except ValueError:
            continue

    for rel in result.delta_relationships:
        try:
            if rel.get("direction") == "incoming":
                spec = build_delta_relationship_cypher(
                    from_type=str(rel["other_type"]),
                    to_type=node_type,
                    rel_type=str(rel["rel_type"]),
                )
                params = {
                    **params_base,
                    "from_entity_id": "$resolve_other",
                    "to_entity_id": result.selected_entity_id,
                }
            else:
                spec = build_delta_relationship_cypher(
                    from_type=node_type,
                    to_type=str(rel["other_type"]),
                    rel_type=str(rel["rel_type"]),
                )
                params = {
                    **params_base,
                    "from_entity_id": result.selected_entity_id,
                    "to_entity_id": "$resolve_other",
                }
            plan.append({**spec, "params": params})
        except ValueError:
            continue

    return plan


def map_extracted_entity(
    entity: dict[str, Any],
    *,
    lane: str,
    node_type: str | None = None,
    relationships: list[dict[str, Any]] | None = None,
    ontology: dict[str, Any] | None = None,
    block: CanonicalBlock | None = None,
    provenance: dict[str, Any] | None = None,
    candidate_reader: Neo4jCandidateReader | None = None,
    semantic: SemanticSimilarityProvider | None = None,
    llm_callable: Callable[..., Any] | None = None,
    existing_candidates: list[ExistingNode] | None = None,
    related_entity_exists: dict[str, bool] | None = None,
    top_k: int = TOP_K,
    min_fusion: float = MIN_FUSION_SCORE,
    high_fusion: float | None = None,
    index: int = 0,
    use_llm_adjudication: bool = False,
    batch_candidates: list[ExistingNode] | None = None,
    reranker: Any | None = None,
    context_evidence_reader: Any | None = None,
    source_context: str = "",
) -> EntityMappingResult:
    """Lane-agnostic existing-node mapping.

    Prefer live Neo4j + semantic providers when provided; otherwise score the
    supplied ``existing_candidates`` offline (unit tests / demos).

    Candidate identity decisions use deterministic lexical/semantic scoring.
    Source context and scoped graph neighborhoods inform retrieval and
    reranking; LLM adjudication is disabled by default.
    """
    etype = node_type or str(entity.get("type") or "Unknown")
    name = str(entity.get("value") or "").strip()
    neighborhood = get_ontology_neighborhood(ontology, etype)
    proposed_in, proposed_out = relationships_from_extraction(entity, relationships)

    if block is not None:
        incoming = entity_to_incoming({**entity, "type": etype, "value": name}, block, index=index)
    else:
        incoming = IncomingNode(
            temporary_id=f"{lane}:ent:{index}",
            node_type=etype,
            name=name,
            domain=(provenance or {}).get("domain"),
            source_file=(provenance or {}).get("source_document"),
            block_id=(provenance or {}).get("source_block_id"),
            extraction_confidence=float(entity.get("confidence") or 0.0),
        )

    prov = dict(provenance or {})
    if block is not None:
        prov.setdefault("source_block_id", block.block_id)
        prov.setdefault("source_document", block.source_file)
        prov.setdefault("source_block_ids", block.source_block_ids or [block.block_id])
        prov.setdefault("domain", block.domain)
    prov.setdefault("lane", lane)
    prov.setdefault("classified_node_type", etype)
    incoming.properties.setdefault(
        "product_line_id",
        prov.get("product_line_id") or prov.get("ingestion_product_line_id")
        or getattr(candidate_reader, "product_line_id", None),
    )

    weaviate_document_context: list[str] = []
    if context_evidence_reader is not None and block is not None:
        try:
            weaviate_document_context = context_evidence_reader.fetch_document_context(
                domain=incoming.domain or "",
                customer_id=str(prov.get("customer_id") or getattr(candidate_reader, "customer_id", "") or ""),
                tenant_id=str(prov.get("tenant_id") or getattr(candidate_reader, "tenant_id", "") or ""),
                source_file=str(block.source_file or ""),
                current_block_id=block.block_id,
                product_line_id=(prov.get("product_line_id") or prov.get("ingestion_product_line_id")
                                 or getattr(candidate_reader, "product_line_id", None)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("entity_mapping.document_context_unavailable", block_id=block.block_id, error=str(exc))

    source_context = "\n".join(part for part in [
        source_context[:5000],
        "Prior same-document chunks: " + "\n".join(weaviate_document_context) if weaviate_document_context else "",
        str(entity.get("description") or ""),
        "Proposed relationships: " + "; ".join(
            f"{r.direction} {r.rel_type} {r.other_type} {r.other_value}"
            for r in [*proposed_in, *proposed_out]
        ),
    ] if part)

    # The matching/review decision is statistical. LLM calls remain in Pass 1
    # and Pass 2 extraction; they do not adjudicate candidate identity here.
    scores_to_llm = False
    logger.info(
        "entity_mapping.input",
        log_kind="input",
        lane=lane,
        block_id=incoming.block_id,
        node_type=etype,
        name=name[:80],
        proposed_incoming_count=len(proposed_in),
        proposed_outgoing_count=len(proposed_out),
        proposed_incoming=[p.model_dump() for p in proposed_in[:8]],
        proposed_outgoing=[p.model_dump() for p in proposed_out[:8]],
        ontology_incoming=neighborhood.get("incoming_node_types", [])[:12],
        ontology_outgoing=neighborhood.get("outgoing_node_types", [])[:12],
        use_llm_adjudication=use_llm_adjudication,
        scores_will_pass_to_llm=scores_to_llm,
        has_candidate_reader=candidate_reader is not None,
        has_semantic=semantic is not None,
        has_llm_callable=llm_callable is not None,
    )
    print(
        f"[EntityMapping][INPUT] lane={lane} block={incoming.block_id} "
        f"type={etype} name={name!r} scores_to_llm={scores_to_llm}",
        flush=True,
    )

    reconciliation: ReconciliationResult | None = None
    candidates: list[CandidateMatch] = []
    breakdown = MappingScoreBreakdown()
    base_decision = MappingDecision.CREATE_NEW
    selected: str | None = None
    reason = ReasonCode.NO_CANDIDATES.value

    use_full_reconcile = scores_to_llm

    if use_full_reconcile:
        logger.info(
            "entity_mapping.logic",
            log_kind="logic",
            lane=lane,
            block_id=incoming.block_id,
            path="llm_adjudication_with_scores",
            decision="fetch_score_then_pass_lex_sem_fusion_to_llm",
        )
        print(
            "[EntityMapping][LOGIC] path=llm_adjudication "
            "(lexical+semantic+fusion scores passed to LLM)",
            flush=True,
        )
        reconciliation = reconcile_incoming_node(
            incoming,
            candidate_reader=candidate_reader,
            llm_callable=llm_callable,
            semantic=semantic,
            domain=incoming.domain,
            top_k=top_k,
            min_fusion=min_fusion,
            reranker=reranker,
            source_context=source_context,
        )
        candidates = list(reconciliation.candidates)
        base_decision = recommendation_to_mapping_decision(reconciliation.recommendation)
        selected = reconciliation.selected_entity_id
        reason = reconciliation.reason_code.value
        if candidates:
            top = candidates[0]
            breakdown = MappingScoreBreakdown(
                lexical_score=top.lexical_score,
                semantic_score=top.semantic_score,
                fusion_score=top.fusion_score,
                combined_confidence=top.fusion_score,
                signals=[
                    f"lexical={top.lexical_score:.4f}",
                    f"semantic={top.semantic_score:.4f}",
                    f"fusion={top.fusion_score:.4f}",
                    (
                        "llm_decision="
                        + (
                            reconciliation.adjudication.decision.value
                            if reconciliation.adjudication and reconciliation.adjudication.decision
                            else "None"
                        )
                    ),
                ],
            )
        logger.info(
            "entity_mapping.scores",
            log_kind="scores",
            lane=lane,
            block_id=incoming.block_id,
            scores_passed_to_llm=True,
            candidate_scores=[
                {
                    "rank": c.rank,
                    "entity_id": c.candidate.entity_id,
                    "name": c.candidate.name,
                    "lexical_score": c.lexical_score,
                    "semantic_score": c.semantic_score,
                    "fusion_score": c.fusion_score,
                    "rrf_score": c.rrf_score,
                    "cross_encoder_score": c.cross_encoder_score,
                }
                for c in candidates
            ],
            adjudication_decision=(
                reconciliation.adjudication.decision.value
                if reconciliation.adjudication and reconciliation.adjudication.decision
                else None
            ),
            adjudication_confidence=(
                reconciliation.adjudication.confidence if reconciliation.adjudication else None
            ),
            adjudication_explanation=(
                reconciliation.adjudication.explanation_summary
                if reconciliation.adjudication
                else None
            ),
        )
    else:
        # Fusion-only / offline path: Neo4j candidates optional; no LLM adjudication
        from kbfabric_ingest.ingest.lane2_node_reconciliation import (
            MockSemanticSimilarityProvider,
        )

        logger.info(
            "entity_mapping.logic",
            log_kind="logic",
            lane=lane,
            block_id=incoming.block_id,
            path="fusion_only_no_llm",
            reason=(
                "llm_adjudication_disabled"
                if not use_llm_adjudication
                else "missing_reader_or_semantic_or_llm"
            ),
            decision="score_locally_without_passing_to_llm",
        )
        print(
            "[EntityMapping][LOGIC] path=fusion_only "
            "(scores computed locally; NOT passed to LLM)",
            flush=True,
        )

        sem = semantic or MockSemanticSimilarityProvider()
        existing = list(existing_candidates or [])
        if candidate_reader is not None and not existing:
            try:
                query_emb = sem.embed(incoming.name) if hasattr(sem, "embed") else None
                existing = candidate_reader.fetch_candidates(
                    incoming,
                    incoming.domain or "",
                    query_embedding=query_emb,
                    use_vector_index=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "entity_mapping.candidate_fetch_failed",
                    lane=lane,
                    block_id=incoming.block_id,
                    error=str(exc),
                )
                existing = []

        candidates, _provider = score_candidates(
            incoming,
            existing,
            semantic=sem,
            top_k=max(top_k * 3, top_k),
            min_fusion=min_fusion,
            query_context=source_context,
        )
        graph_neighborhoods = (
            candidate_reader.fetch_candidate_neighborhoods(
                [c.candidate.entity_id for c in candidates], domain=incoming.domain or ""
            )
            if candidates and candidate_reader is not None
            and hasattr(candidate_reader, "fetch_candidate_neighborhoods")
            else {}
        )
        for candidate in candidates:
            candidate.graph_neighborhood = list(
                graph_neighborhoods.get(candidate.candidate.entity_id, [])
            )
        if reranker is not None and candidates:
            from custom_ml.reranking.candidate_ranking import cross_encoder_scores

            ce_scores = cross_encoder_scores(
                query=f"{etype}: {name}\n{source_context[:5000]}",
                candidates=[c.candidate for c in candidates],
                reranker=reranker,
                neighborhoods=graph_neighborhoods,
            )
            for candidate in candidates:
                candidate.cross_encoder_score = ce_scores.get(candidate.candidate.entity_id)
            logger.info(
                "entity_mapping.reranking.completed", block_id=incoming.block_id,
                shortlist_count=len(ce_scores), reranked=bool(ce_scores),
                graph_context_candidate_count=len(graph_neighborhoods),
            )
        candidates = candidates[:top_k]
        enriched = enrich_candidates_with_context(
            incoming,
            candidates,
            proposed_incoming=proposed_in,
            proposed_outgoing=proposed_out,
            ontology_constraints=neighborhood,
            candidate_reader=candidate_reader,
        )
        if enriched:
            if any(c.cross_encoder_score is not None for c, _ in enriched):
                enriched.sort(
                    key=lambda pair: (
                        pair[0].cross_encoder_score is not None,
                        pair[0].cross_encoder_score if pair[0].cross_encoder_score is not None
                        else pair[1].combined_confidence,
                    ),
                    reverse=True,
                )
            best_cand, breakdown = enriched[0]
            breakdown.lexical_score = best_cand.lexical_score
            breakdown.semantic_score = best_cand.semantic_score
            candidates = [c for c, _ in enriched]
            logger.info(
                "entity_mapping.scores",
                log_kind="scores",
                lane=lane,
                block_id=incoming.block_id,
                scores_passed_to_llm=False,
                candidate_scores=[
                    {
                        "rank": c.rank,
                        "entity_id": c.candidate.entity_id,
                        "name": c.candidate.name,
                        "lexical_score": c.lexical_score,
                        "semantic_score": c.semantic_score,
                        "fusion_score": c.fusion_score,
                        "combined_confidence": bd.combined_confidence,
                        "type_match": bd.type_match_score,
                        "rel_compat": bd.relationship_compatibility_score,
                    }
                    for (c, bd) in enriched
                ],
            )
            for c, bd in enriched:
                print(
                    f"[EntityMapping][SCORES] rank={c.rank} "
                    f"lex={c.lexical_score:.4f} sem={c.semantic_score:.4f} "
                    f"fusion={c.fusion_score:.4f} combined={bd.combined_confidence:.4f} "
                    f"→ {c.candidate.name!r}",
                    flush=True,
                )
            if (
                confidence_allows_auto_resolution(breakdown.combined_confidence)
                and breakdown.type_match_score >= 0.95
            ):
                base_decision = MappingDecision.REUSE_EXISTING
                selected = best_cand.candidate.entity_id
                reason = "statistical_match_above_auto_resolution_threshold"
            else:
                base_decision = MappingDecision.REQUIRES_REVIEW
                selected = None
                reason = "statistical_match_at_or_below_auto_resolution_threshold"
        else:
            base_decision = MappingDecision.REQUIRES_REVIEW
            selected = None
            reason = "no_eligible_candidate_requires_review"
            breakdown = MappingScoreBreakdown(signals=["no_candidates"])
            logger.info(
                "entity_mapping.scores",
                log_kind="scores",
                lane=lane,
                block_id=incoming.block_id,
                candidate_scores=[],
                scores_passed_to_llm=False,
            )
    # Relationship context refinement when we have a selected reuse candidate
    existing_in: list[dict[str, Any]] = []
    existing_out: list[dict[str, Any]] = []
    if selected and candidates:
        top = next((c for c in candidates if c.candidate.entity_id == selected), candidates[0])
        existing_in = [
            {
                "rel_type": edge.get("rel_type"),
                "other_type": edge.get("other_type"),
                "other_value": edge.get("other_value"),
            }
            for edge in top.graph_neighborhood
            if edge.get("direction") == "incoming"
        ]
        existing_out = [
            {
                "rel_type": edge.get("rel_type"),
                "other_type": edge.get("other_type"),
                "other_value": edge.get("other_value"),
            }
            for edge in top.graph_neighborhood
            if edge.get("direction") == "outgoing"
        ]
        rel_score = score_relationship_compatibility(
            proposed_in, proposed_out, existing_in, existing_out, neighborhood
        )
        t_score = type_match_score(etype, top.candidate.kg_node_type)
        breakdown = combine_mapping_confidence(
            fusion=top.fusion_score,
            type_score=t_score,
            rel_score=rel_score,
        )
        breakdown.lexical_score = top.lexical_score
        breakdown.semantic_score = top.semantic_score

    decision, delta_nodes, delta_rels = decide_delta_actions(
        base_decision=base_decision,
        selected_entity_id=selected,
        proposed_incoming=proposed_in,
        proposed_outgoing=proposed_out,
        existing_incoming=existing_in,
        existing_outgoing=existing_out,
        related_entity_exists=related_entity_exists,
    )

    # Resolve relationship intent independently of whether the primary entity
    # already exists. Context Resolution is read-only: it never writes to Neo4j.
    #
    # Existing primary entity:
    #     resolve only relationships that are new graph deltas.
    #
    # New primary entity:
    #     resolve the extracted relationships now, using the temporary source
    #     identity. The eventual KG writer binds that temporary identity to the
    #     created entity ID.
    #
    # Primary entity awaiting reconciliation review:
    #     defer relationship resolution until the primary entity decision exists.
    context_resolutions: list[ContextResolutionResult] = []

    if selected:
        context_relationships = list(delta_rels)
        source_entity_id = selected
    elif base_decision in {
        MappingDecision.CREATE_NEW,
        MappingDecision.REQUIRES_REVIEW,
    }:
        # Resolve evidence for review-bound source entities too. This does not
        # authorize a write; it gives HITL the exact ontology-valid triplets
        # that must remain withheld until the source identity is decided.
        context_relationships = [
            {
                "rel_type": rel.rel_type,
                "direction": rel.direction,
                "other_type": rel.other_type,
                "other_value": rel.other_value,
                "primary_entity_id": incoming.temporary_id,
            }
            for rel in [*proposed_in, *proposed_out]
        ]
        source_entity_id = (
            selected
            if selected and base_decision == MappingDecision.REQUIRES_REVIEW
            else incoming.temporary_id
        )
    else:
        context_relationships = []
        source_entity_id = incoming.temporary_id

    logger.info(
        "context_resolution.input",
        log_kind="input",
        lane=lane,
        block_id=incoming.block_id,
        source_entity_id=source_entity_id,
        primary_mapping_decision=base_decision.value,
        relationship_count=len(context_relationships),
        relationship_source=(
            "delta_relationships"
            if selected
            else "proposed_relationships_for_create_new"
            if base_decision == MappingDecision.CREATE_NEW
            else "deferred_primary_review"
        ),
    )

    context_semantic = semantic
    if context_semantic is None and candidate_reader is not None:
        try:
            from kbfabric_ingest.ingest.lane2_node_reconciliation import (
                MockSemanticSimilarityProvider,
            )
            context_semantic = MockSemanticSimilarityProvider()
        except Exception:  # noqa: BLE001
            context_semantic = None

    for delta_rel in context_relationships:
        direction = str(delta_rel.get("direction") or "")
        rel_type = str(delta_rel.get("rel_type") or "")
        target_type = str(
            delta_rel.get("other_type")
            or delta_rel.get("target_type")
            or ""
        )
        target_value = str(
            delta_rel.get("other_value")
            or delta_rel.get("target_value")
            or ""
        ).strip()

        if not direction or not rel_type or not target_type or not target_value:
            logger.info(
                "context_resolution.skipped",
                log_kind="logic",
                lane=lane,
                block_id=incoming.block_id,
                reason="incomplete_delta_relationship",
                relationship=delta_rel,
            )
            continue

        resolution = resolve_context_relationship(
            source_type=etype,
            source_name=name,
            source_entity_id=source_entity_id,
            direction=direction,
            rel_type=rel_type,
            target_type=target_type,
            target_value=target_value,
            domain=incoming.domain or "",
            ontology=ontology,
            candidate_reader=candidate_reader,
            batch_candidates=batch_candidates,
            semantic=context_semantic,
            reranker=reranker,
            source_context=source_context,
            llm_callable=llm_callable,
            use_llm_adjudication=use_llm_adjudication,

            source_description=str(
                entity.get("description")
                or entity.get("text")
                or ""
            ) or None,
            block=block,
            provenance=prov,
            top_k=top_k,
            min_fusion=min_fusion,
            high_fusion=high_fusion,
        )
        context_resolutions.append(resolution)

    logger.info(
        "context_resolution.batch.completed",
        log_kind="output",
        lane=lane,
        block_id=incoming.block_id,
        source_entity_id=source_entity_id,
        relationship_count=len(context_relationships),
        resolution_count=len(context_resolutions),
        decisions=[r.decision for r in context_resolutions],
    )

    result = EntityMappingResult(
        lane=lane,
        incoming=incoming,
        decision=decision,
        reason_code=reason,
        selected_entity_id=selected,
        created_entity_id=None if selected else None,
        candidates=candidates,
        score_breakdown=breakdown,
        proposed_incoming=proposed_in,
        proposed_outgoing=proposed_out,
        ontology_constraints=neighborhood,
        delta_relationships=delta_rels,
        delta_nodes=delta_nodes,
        provenance=prov,
        context_resolutions=context_resolutions,
        reconciliation=reconciliation,
    )
    result.cypher_plan = build_cypher_plan_for_mapping(result)

    logger.info(
        "entity_mapping.output",
        log_kind="output",
        lane=lane,
        block_id=incoming.block_id,
        node_type=etype,
        name=name[:80],
        candidate_count=len(candidates),
        lexical_score=breakdown.lexical_score,
        semantic_score=breakdown.semantic_score,
        fusion_score=breakdown.fusion_score,
        relationship_compatibility_score=breakdown.relationship_compatibility_score,
        type_match_score=breakdown.type_match_score,
        final_confidence=breakdown.combined_confidence,
        score_signals=breakdown.signals,
        mapping_decision=decision.value,
        reason_code=reason,
        reused_node_id=selected,
        newly_created_node_id=result.created_entity_id,
        delta_relationship_count=len(delta_rels),
        delta_node_count=len(delta_nodes),
        scores_passed_to_llm=scores_to_llm,
        cypher_plan_steps=len(result.cypher_plan),
    )
    print(
        f"[EntityMapping][OUTPUT] decision={decision.value} reason={reason} "
        f"lex={breakdown.lexical_score:.4f} sem={breakdown.semantic_score:.4f} "
        f"fusion={breakdown.fusion_score:.4f} conf={breakdown.combined_confidence:.4f} "
        f"reused={selected} deltas_rel={len(delta_rels)} deltas_node={len(delta_nodes)}",
        flush=True,
    )
    return result


def annotate_entities_with_mapping(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    lane: str,
    block: CanonicalBlock,
    ontology: dict[str, Any] | None = None,
    candidate_reader: Neo4jCandidateReader | None = None,
    semantic: SemanticSimilarityProvider | None = None,
    llm_callable: Callable[..., Any] | None = None,
    use_llm_adjudication: bool = False,
    provenance: dict[str, Any] | None = None,
    batch_candidates: list[ExistingNode] | None = None,
    reranker: Any | None = None,
    context_evidence_reader: Any | None = None,
    source_context: str = "",
) -> tuple[list[dict[str, Any]], list[EntityMappingResult]]:
    """Annotate extracted entities with mapping decisions for KGWriter.

    Matching ambiguity is resolved statistically; this setting remains only
    for API compatibility. Candidate identity decisions never call an LLM.
    """
    use_llm_adjudication = False
    logger.info(
        "entity_mapping.annotate.input",
        log_kind="input",
        lane=lane,
        block_id=block.block_id,
        entity_count=len(entities or []),
        relationship_count=len(relationships or []),
        use_llm_adjudication=use_llm_adjudication,
        scores_will_pass_to_llm=use_llm_adjudication and llm_callable is not None,
    )
    print(
        f"[EntityMapping][INPUT] annotate lane={lane} block={block.block_id} "
        f"entities={len(entities or [])} rels={len(relationships or [])} "
        f"llm_adjudication={use_llm_adjudication}",
        flush=True,
    )

    annotated: list[dict[str, Any]] = []
    results: list[EntityMappingResult] = []
    present_keys = {
        f"{e.get('type')}:{e.get('value')}".lower()
        for e in (entities or [])
        if isinstance(e, dict) and e.get("value")
    }

    for idx, ent in enumerate(entities or []):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("value") or "").strip()
        if not name:
            annotated.append(dict(ent))
            continue

        related_exists: dict[str, bool] = {}
        for rel in relationships or []:
            if not isinstance(rel, dict):
                continue
            for side_t, side_v in (
                (rel.get("from_type"), rel.get("from_value")),
                (rel.get("to_type"), rel.get("to_value")),
            ):
                if not side_v:
                    continue
                key = f"{side_t}:{side_v}".lower()
                # Known in this extraction batch; Neo4j existence unknown → treat as maybe-exists
                related_exists[key] = key in present_keys

        try:
            mapped = map_extracted_entity(
                ent,
                lane=lane,
                relationships=relationships,
                ontology=ontology,
                block=block,
                candidate_reader=candidate_reader,
                semantic=semantic,
                llm_callable=llm_callable,
                related_entity_exists=related_exists,
                index=idx,
                use_llm_adjudication=use_llm_adjudication,
                provenance=provenance,
                batch_candidates=batch_candidates,
                reranker=reranker,
                context_evidence_reader=context_evidence_reader,
                source_context=source_context,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the whole block
            logger.info(
                "entity_mapping.annotate_failed",
                lane=lane,
                block_id=block.block_id,
                entity_type=ent.get("type"),
                error=str(exc),
            )
            out = dict(ent)
            out["lane"] = lane
            out["mapping_decision"] = MappingDecision.CREATE_NEW.value
            out["mapping_reason_code"] = "MAPPING_ERROR"
            out["mapping_error"] = str(exc)[:200]
            annotated.append(out)
            continue

        results.append(mapped)
        out = dict(ent)
        out["lane"] = lane
        out["mapping_decision"] = mapped.decision.value
        out["mapping_reason_code"] = mapped.reason_code
        out["mapping_confidence"] = mapped.score_breakdown.combined_confidence
        out["lexical_score"] = mapped.score_breakdown.lexical_score
        out["semantic_score"] = mapped.score_breakdown.semantic_score
        out["relationship_compatibility_score"] = (
            mapped.score_breakdown.relationship_compatibility_score
        )
        if mapped.selected_entity_id:
            out["selected_entity_id"] = mapped.selected_entity_id
        if mapped.decision == MappingDecision.REQUIRES_REVIEW:
            # Do not block ingestion; writer creates via hash id
            out["mapping_requires_review"] = True
        if mapped.delta_nodes:
            out["mapping_delta_nodes"] = mapped.delta_nodes
        if mapped.delta_relationships:
            out["mapping_delta_relationships"] = mapped.delta_relationships
        annotated.append(out)

    logger.info(
        "entity_mapping.annotate.output",
        log_kind="output",
        lane=lane,
        block_id=block.block_id,
        entity_count=len(annotated),
        mapping_count=len(results),
        decisions=[r.decision.value for r in results],
        lexical_scores=[r.score_breakdown.lexical_score for r in results],
        semantic_scores=[r.score_breakdown.semantic_score for r in results],
        fusion_scores=[r.score_breakdown.fusion_score for r in results],
        scores_passed_to_llm=use_llm_adjudication and llm_callable is not None,
    )
    print(
        f"[EntityMapping][OUTPUT] annotate lane={lane} block={block.block_id} "
        f"decisions={[r.decision.value for r in results]}",
        flush=True,
    )
    return annotated, results


def map_entities_from_ie(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    lane: str,
    block: CanonicalBlock | None = None,
    ontology: dict[str, Any],
    **kwargs: Any,
) -> list[EntityMappingResult]:
    """Map every extracted entity with the shared utility."""
    results: list[EntityMappingResult] = []
    for idx, ent in enumerate(entities or []):
        if not isinstance(ent, dict) or not str(ent.get("value") or "").strip():
            continue
        results.append(
            map_extracted_entity(
                ent,
                lane=lane,
                relationships=relationships,
                ontology=ontology,
                block=block,
                index=idx,
                **kwargs,
            )
        )
    return results

# Re-export Pass 3 primitives so lanes import from one place
__all__ = [
    "MappingDecision",
    "EntityMappingResult",
    "MappingScoreBreakdown",
    "ProposedRelationship",
    "map_extracted_entity",
    "map_entities_from_ie",
    "annotate_entities_with_mapping",
    "score_relationship_compatibility",
    "build_candidate_retrieval_cypher",
    "build_delta_relationship_cypher",
    "build_reuse_node_provenance_cypher",
    "build_cypher_plan_for_mapping",
    "recommendation_to_mapping_decision",
    "ReconciliationRecommendation",
    "Neo4jCandidateReader",
    "score_candidates",
    "lexical_similarity",
    "apply_decision_policy",
    "AdjudicationDecision",
    "LLMAdjudicationResult",
]

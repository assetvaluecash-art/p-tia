"""Context resolution for ontology-valid relationship endpoints.

This module resolves extracted relationship references to existing KG entities.
It performs retrieval/ranking/decision only; it does not write to Neo4j.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from kbfabric_ingest.utils.logger import get_logger

from .lane2_node_reconciliation import (
    MIN_FUSION_SCORE,
    CandidateMatch,
    ExistingNode,
    IncomingNode,
    Neo4jCandidateReader,
    SemanticSimilarityProvider,
    cosine_similarity,
    confidence_allows_auto_resolution,
    fusion_score,
    get_auto_resolution_confidence_threshold,
    lexical_similarity,
)
from .lane2_two_pass import CanonicalBlock
from .ontology_admission import compile_relationship_triples

logger = get_logger(__name__)

def build_context_resolution_query(*, source_type: str, source_name: str, rel_type: str, target_type: str, target_value: str, source_description: str | None = None) -> str:
    """Build the semantic query used to resolve a relationship endpoint."""
    parts = [source_type, source_name, rel_type, target_type, target_value]
    if source_description:
        parts.append(source_description)
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip())


class ContextResolutionCandidate(BaseModel):
    entity_id: str
    node_type: str
    name: str
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    fusion_score: float = 0.0
    rank: int = 0
    semantic_source: str = ""
    rrf_score: float = 0.0
    cross_encoder_score: float | None = None
    graph_neighborhood: list[dict[str, Any]] = Field(default_factory=list)

class ContextResolutionResult(BaseModel):
    direction: str
    rel_type: str
    source_entity_id: str
    target_type: str
    target_value: str
    selected_entity_id: str | None = None
    decision: str
    confidence: float = 0.0
    candidates: list[ContextResolutionCandidate] = Field(default_factory=list)
    reason_code: str = ""
    justification: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)

def rank_context_resolution_candidates(
    *,
    target_value: str,
    candidates: list[ExistingNode],
    semantic: SemanticSimilarityProvider,
    query_text: str,
    query_embedding: list[float] | None = None,
    top_k: int = 5,
    min_fusion: float = MIN_FUSION_SCORE,
    reranker: Any | None = None,
    graph_neighborhoods: dict[str, list[dict[str, Any]]] | None = None,
    product_line_id: str | None = None,
) -> list[ContextResolutionCandidate]:
    """Rank relationship targets using the existing lexical+semantic fusion policy."""
    if query_embedding is None:
        query_embedding = semantic.embed(query_text)
    ranked: list[ContextResolutionCandidate] = []

    for candidate in candidates:
        lexical = max(
            [
                lexical_similarity(target_value, candidate.name),
                lexical_similarity(target_value, candidate.name_display or ""),
                *[
                    lexical_similarity(target_value, str(alias))
                    for alias in candidate.aliases or []
                ],
            ],
            default=0.0,
        )

        if query_embedding and candidate.embedding:
            semantic_score = max(0.0, min(1.0, cosine_similarity(query_embedding, candidate.embedding)))
            semantic_source = "neo4j_stored_embedding"
        elif candidate.vector_score is not None:
            semantic_score = max(0.0, min(1.0, float(candidate.vector_score)))
            semantic_source = "neo4j_vector_index_score"
        else:
            semantic_score = 0.0
            semantic_source = (
                "mock_semantic_provider_not_eligible_for_auto_resolution"
                if semantic.is_mock
                else "unavailable"
            )

        fusion = fusion_score(lexical, semantic_score)

        ranked.append(
            ContextResolutionCandidate(
                entity_id=candidate.entity_id,
                node_type=candidate.kg_node_type or "",
                name=candidate.name,
                lexical_score=round(lexical, 6),
                semantic_score=round(semantic_score, 6),
                fusion_score=round(fusion, 6),
                rank=0,
                semantic_source=semantic_source,
                graph_neighborhood=list((graph_neighborhoods or {}).get(candidate.entity_id, [])),
            )
        )

    from custom_ml.reranking.candidate_ranking import (
        cross_encoder_scores, reciprocal_rank_fusion,
    )

    lexical_rank = sorted(ranked, key=lambda c: c.lexical_score, reverse=True)
    semantic_rank = sorted(ranked, key=lambda c: c.semantic_score, reverse=True)
    rankings = [
        [c.entity_id for c in lexical_rank], [c.entity_id for c in semantic_rank]
    ]
    if product_line_id:
        by_id = {candidate.entity_id: candidate for candidate in candidates}
        product_line_rank = sorted(
            ranked,
            key=lambda item: product_line_id in by_id[item.entity_id].product_line_ids,
            reverse=True,
        )
        rankings.append([candidate.entity_id for candidate in product_line_rank])
    rrf_scores = reciprocal_rank_fusion(rankings)
    for candidate in ranked:
        candidate.rrf_score = round(rrf_scores.get(candidate.entity_id, 0.0), 8)
    ranked.sort(key=lambda c: (c.rrf_score, c.fusion_score), reverse=True)
    shortlist = ranked[:max(top_k * 3, top_k)]
    if reranker is not None and shortlist:
        by_id = {candidate.entity_id: candidate for candidate in candidates}
        logits = cross_encoder_scores(
            query=query_text,
            candidates=[by_id[c.entity_id] for c in shortlist if c.entity_id in by_id],
            reranker=reranker,
            neighborhoods=graph_neighborhoods,
        )
        for candidate in shortlist:
            candidate.cross_encoder_score = logits.get(candidate.entity_id)
        if logits:
            shortlist.sort(
                key=lambda c: (c.cross_encoder_score is not None,
                               c.cross_encoder_score if c.cross_encoder_score is not None else c.rrf_score),
                reverse=True,
            )
    filtered = [c for c in shortlist if c.fusion_score >= min_fusion][:top_k]
    if not filtered and ranked:
        filtered = ranked[:top_k]

    for rank, candidate in enumerate(filtered, start=1):
        candidate.rank = rank

    logger.info(
        "context_resolution.ranking.completed",
        target_value=target_value,
        candidate_count=len(candidates),
        retained_count=len(filtered),
        top_fusion_score=filtered[0].fusion_score if filtered else 0.0,
        top_rrf_score=filtered[0].rrf_score if filtered else 0.0,
        top_cross_encoder_score=filtered[0].cross_encoder_score if filtered else None,
        top_k=top_k,
        min_fusion=min_fusion,
    )

    return filtered

def build_context_resolution_justification(
    *,
    decision: str,
    reason_code: str,
    source_entity_id: str,
    rel_type: str,
    target_type: str,
    target_value: str,
    candidates: list[ContextResolutionCandidate],
    confidence_threshold: float,
) -> str:
    """Build a deterministic human-readable explanation from resolution evidence."""
    if not candidates:
        return (
            f'No eligible {target_type} candidate was found for '
            f'"{target_value}" in the context of '
            f'{source_entity_id} [{rel_type}] {target_type}. '
            f'Confidence is 0.000, at or below the {confidence_threshold:.3f} '
            'auto-resolution threshold; '
            'the endpoint requires HITL review.'
        )

    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    gap = top.fusion_score - second.fusion_score if second is not None else 1.0

    if decision == "RESOLVED":
        return (
            f'Candidate "{top.name}" ({top.entity_id}) was selected for '
            f'"{target_value}" because it is an ontology-valid {target_type} '
            f'target for {source_entity_id} [{rel_type}] {target_type}; its '
            f'lexical/semantic confidence {top.fusion_score:.3f} is above '
            f'{confidence_threshold:.3f}.'
        )

    if decision == "UNRESOLVED_REFERENCE":
        return (
            f'No candidate was sufficiently supported for "{target_value}" '
            f'as a {target_type} target of {source_entity_id} '
            f'[{rel_type}] {target_type}. The strongest candidate, '
            f'"{top.name}" ({top.entity_id}), scored '
            f'{top.fusion_score:.3f}, below the required minimum confidence.'
        )

    return (
        f'The reference "{target_value}" has eligible {target_type} '
        f'candidates, but its top lexical/semantic confidence is '
        f'{top.fusion_score:.3f}, at or below the '
        f'{confidence_threshold:.3f} threshold. '
        f'The top candidate is "{top.name}" ({top.entity_id}). '
        f'Human confirmation is required.'
    )

def decide_context_resolution(
    *,
    direction: str,
    rel_type: str,
    source_entity_id: str,
    target_type: str,
    target_value: str,
    candidates: list[ContextResolutionCandidate],
    block: CanonicalBlock | None = None,
    provenance: dict[str, Any] | None = None,
    min_fusion: float = MIN_FUSION_SCORE,
    high_fusion: float | None = None,
    ambiguity_margin: float = 0.05,
) -> ContextResolutionResult:
    """Apply deterministic relationship-target resolution policy to ranked candidates."""
    resolution_provenance = build_context_resolution_provenance(
        block=block,
        provenance=provenance,
    )
    confidence_threshold = get_auto_resolution_confidence_threshold()

    if not candidates:
        reason_code = "no_eligible_candidates_below_auto_resolution_threshold"
        justification = (
            f'No eligible {target_type} candidate was found for "{target_value}" '
            f'in the context of {source_entity_id} [{rel_type}] {target_type}. '
            f'The endpoint is sent to HITL because its confidence is at or '
            f'below the configured threshold {confidence_threshold:.3f}.'
        )
        logger.info(
            "context_resolution.decision.review_required",
            source_entity_id=source_entity_id,
            rel_type=rel_type,
            target_type=target_type,
            target_value=target_value,
            decision="RELATIONSHIP_REVIEW_REQUIRED",
            reason_code=reason_code,
            provenance=resolution_provenance,
        )
        return ContextResolutionResult(
            direction=direction,
            rel_type=rel_type,
            source_entity_id=source_entity_id,
            target_type=target_type,
            target_value=target_value,
            decision="RELATIONSHIP_REVIEW_REQUIRED",
            confidence=0.0,
            candidates=[],
            reason_code=reason_code,
            justification=justification,
            provenance=resolution_provenance,
        )

    top = candidates[0]
    if confidence_allows_auto_resolution(top.fusion_score):
        decision = "RESOLVED"
        reason_code = "statistical_match_above_auto_resolution_threshold"
        selected_entity_id = top.entity_id
    else:
        decision = "RELATIONSHIP_REVIEW_REQUIRED"
        reason_code = "statistical_match_at_or_below_auto_resolution_threshold"
        selected_entity_id = None

    justification = build_context_resolution_justification(
        decision=decision,
        reason_code=reason_code,
        source_entity_id=source_entity_id,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        candidates=candidates,
        confidence_threshold=confidence_threshold,
    )

    logger.info(
        "context_resolution.decision.completed",
        source_entity_id=source_entity_id,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        decision=decision,
        reason_code=reason_code,
        top_fusion_score=round(top.fusion_score, 6),
        candidate_gap=(
            round(top.fusion_score - candidates[1].fusion_score, 6)
            if len(candidates) > 1 else None
        ),
        candidate_count=len(candidates),
        justification=justification,
    )

    return ContextResolutionResult(
        direction=direction,
        rel_type=rel_type,
        source_entity_id=source_entity_id,
        target_type=target_type,
        target_value=target_value,
        selected_entity_id=selected_entity_id,
        decision=decision,
        confidence=round(top.fusion_score, 6),
        candidates=candidates,
        reason_code=reason_code,
        justification=justification,
        provenance=resolution_provenance,
    )

def resolve_context_relationship(
    *,
    source_type: str,
    source_name: str,
    source_entity_id: str,
    direction: str,
    rel_type: str,
    target_type: str,
    target_value: str,
    domain: str,
    ontology: dict[str, Any],
    candidate_reader: Neo4jCandidateReader | None,
    batch_candidates: list[ExistingNode] | None = None,
    semantic: SemanticSimilarityProvider | None = None,
    source_description: str | None = None,
    block: CanonicalBlock | None = None,
    provenance: dict[str, Any] | None = None,
    top_k: int = 5,
    min_fusion: float = MIN_FUSION_SCORE,
    high_fusion: float | None = None,
    ambiguity_margin: float = 0.05,
    reranker: Any | None = None,
    llm_callable: Callable[..., Any] | None = None,
    use_llm_adjudication: bool = False,
    source_context: str = "",
) -> ContextResolutionResult:
    """Resolve one extracted relationship endpoint without performing graph writes."""
    resolution_provenance = build_context_resolution_provenance(
        block=block,
        provenance=provenance,
    )

    if not get_context_resolution_target_type(
        ontology=ontology,
        source_type=source_type,
        rel_type=rel_type,
        target_type=target_type,
        direction=direction,
    ):
        justification = (
            f'The relationship {source_type} [{rel_type}] {target_type} '
            f'is not allowed by the authoritative ontology, so the reference '
            f'"{target_value}" cannot be resolved as this relationship.'
        )
        logger.info(
            "context_resolution.rejected",
            source_entity_id=source_entity_id,
            rel_type=rel_type,
            target_type=target_type,
            target_value=target_value,
            reason_code="invalid_relationship_triple",
        )
        return ContextResolutionResult(
            direction=direction,
            rel_type=rel_type,
            source_entity_id=source_entity_id,
            target_type=target_type,
            target_value=target_value,
            decision="REJECTED",
            confidence=0.0,
            candidates=[],
            reason_code="invalid_relationship_triple",
            justification=justification,
            provenance=resolution_provenance,
        )

    target = IncomingNode(
        temporary_id=f"context-target:{source_entity_id}:{rel_type}:{target_value}",
        node_type=target_type,
        name=target_value,
        description=None,
        domain=domain,
    )

    query_text = build_context_resolution_query(
        source_type=source_type,
        source_name=source_name,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        source_description=source_description,
    )
    contextual_query = "\n".join(part for part in [
        query_text, source_context[:5000], str(source_description or ""),
    ] if part)
    query_embedding = (
        semantic.embed(contextual_query)
        if semantic is not None and not semantic.is_mock
        else None
    )

    candidates = (
        candidate_reader.fetch_candidates(
            target,
            domain,
            limit=max(top_k * 4, 20),
            query_embedding=query_embedding,
            use_vector_index=query_embedding is not None,
        )
        if candidate_reader is not None
        else []
    )
    # Include ontology-compatible entities extracted elsewhere in this batch.
    # Prefer persisted candidates with the same canonical identity, and dedupe
    # by entity ID so one node cannot create artificial ambiguity.
    by_id = {candidate.entity_id: candidate for candidate in candidates}
    persisted_names = {
        str(candidate.name_display or candidate.name).strip().casefold()
        for candidate in candidates
    }
    for candidate in batch_candidates or []:
        candidate_type = candidate.kg_node_type or (candidate.labels[0] if candidate.labels else None)
        if candidate_type != target_type or candidate.entity_id in by_id:
            continue
        candidate_names = {
            str(value).strip().casefold()
            for value in [candidate.name, candidate.name_display, *(candidate.aliases or [])]
            if value
        }
        if candidate_names & persisted_names:
            continue
        by_id[candidate.entity_id] = candidate
    candidates = list(by_id.values())
    graph_neighborhoods = (
        candidate_reader.fetch_candidate_neighborhoods(
            [candidate.entity_id for candidate in candidates], domain=domain
        )
        if candidate_reader is not None and candidates
        and hasattr(candidate_reader, "fetch_candidate_neighborhoods")
        else {}
    )
    ranked = (
        rank_context_resolution_candidates(
            target_value=target_value,
            candidates=candidates,
            semantic=semantic,
            query_text=contextual_query,
            query_embedding=query_embedding,
            top_k=top_k,
            min_fusion=min_fusion,
            reranker=reranker,
            graph_neighborhoods=graph_neighborhoods,
            product_line_id=getattr(candidate_reader, "product_line_id", None),
        )
        if candidates and semantic is not None
        else []
    )

    result = decide_context_resolution(
        direction=direction,
        rel_type=rel_type,
        source_entity_id=source_entity_id,
        target_type=target_type,
        target_value=target_value,
        candidates=ranked,
        block=block,
        provenance=resolution_provenance,
        min_fusion=min_fusion,
        high_fusion=high_fusion,
        ambiguity_margin=ambiguity_margin,
    )

    logger.info(
        "context_resolution.completed",
        source_entity_id=source_entity_id,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        decision=result.decision,
        selected_entity_id=result.selected_entity_id,
        confidence=result.confidence,
        reason_code=result.reason_code,
    )
    return result

def retrieve_context_resolution_candidates(
    *,
    source_type: str,
    source_name: str,
    source_entity_id: str,
    rel_type: str,
    target_type: str,
    target_value: str,
    domain: str,
    ontology: dict[str, Any],
    candidate_reader: Neo4jCandidateReader | None,
    batch_candidates: list[ExistingNode] | None = None,
    semantic: SemanticSimilarityProvider | None = None,
    source_description: str | None = None,
    limit: int = 10,
) -> list[ExistingNode]:
    """Retrieve ontology-legal target candidates for one extracted relationship."""
    if not get_context_resolution_target_type(
        ontology=ontology,
        source_type=source_type,
        rel_type=rel_type,
        target_type=target_type,
    ):
        logger.info(
            "context_resolution.retrieval.rejected",
            source_type=source_type,
            source_entity_id=source_entity_id,
            rel_type=rel_type,
            target_type=target_type,
            reason="invalid_relationship_triple",
        )
        return []

    target = IncomingNode(
        temporary_id=f"context-target:{source_entity_id}:{rel_type}:{target_value}",
        node_type=target_type,
        name=target_value,
        description=None,
        domain=domain,
    )

    query_text = build_context_resolution_query(
        source_type=source_type,
        source_name=source_name,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        source_description=source_description,
    )

    query_embedding = semantic.embed(query_text) if semantic is not None else None

    candidates = candidate_reader.fetch_candidates(
        target,
        domain,
        limit=limit,
        query_embedding=query_embedding,
        use_vector_index=query_embedding is not None,
    )

    if candidates and semantic is not None:
        rank_context_resolution_candidates(
            target_value=target_value,
            candidates=candidates,
            semantic=semantic,
            query_text=query_text,
            query_embedding=query_embedding,
            top_k=limit,
        )

    logger.info(
        "context_resolution.retrieval.completed",
        source_type=source_type,
        source_entity_id=source_entity_id,
        source_name=source_name,
        rel_type=rel_type,
        target_type=target_type,
        target_value=target_value,
        candidate_count=len(candidates),
        embedding_used=query_embedding is not None,
    )

    return candidates

def build_context_resolution_provenance(
    *,
    block: CanonicalBlock | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build resolution provenance from the existing block/extraction contracts."""
    result = dict(provenance or {})
    if block is not None:
        result.setdefault("source_block_id", block.block_id)
        result.setdefault("source_block_ids", block.source_block_ids or [block.block_id])
        result.setdefault("source_document", block.source_file)
        result.setdefault("source_file", block.source_file)
        result.setdefault("file_type", block.file_type)
        result.setdefault("domain", block.domain)
        result.setdefault("page_number", block.page_number)
        result.setdefault("block_index", block.block_index)
        result.setdefault("block_type", block.block_type)
        result.setdefault("source_text", block.text)
        result.setdefault("metadata", dict(block.metadata or {}))
        result.setdefault("semantic_unit_id", block.semantic_unit_id)
        result.setdefault("unit_type", block.unit_type)
        result.setdefault("unit_part_index", block.unit_part_index)
        result.setdefault("unit_part_total", block.unit_part_total)
        result.setdefault("created_at", block.created_at.isoformat())
    return result

def get_context_resolution_target_type(
    *,
    ontology: dict[str, Any],
    source_type: str,
    rel_type: str,
    target_type: str,
    direction: str = "outgoing",
) -> bool:
    """Return whether the endpoint pair forms an allowed directed triple."""
    relationship_defs = {
        str(rt.get("name")): rt
        for rt in ontology.get("relationship_types", []) or []
        if isinstance(rt, dict) and rt.get("name")
    }
    allowed_triples = compile_relationship_triples(relationship_defs)
    if direction.strip().lower() == "incoming":
        triple = (target_type, rel_type, source_type)
    else:
        triple = (source_type, rel_type, target_type)
    return triple in allowed_triples

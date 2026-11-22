"""Shared RRF shortlist and offline cross-encoder reranking for KG candidates."""

from __future__ import annotations

from typing import Any, Iterable
import logging

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rankings: Iterable[Iterable[str]], *, k: int = 60
) -> dict[str, float]:
    """Fuse ordered candidate IDs without treating unrelated raw scores as calibrated."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, candidate_id in enumerate(ranking, start=1):
            key = str(candidate_id)
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def candidate_document(candidate: Any, neighborhood: Iterable[dict[str, Any]] = ()) -> str:
    """Render candidate identity, description, aliases and graph neighborhood for reranking."""
    get = candidate.get if isinstance(candidate, dict) else lambda key, default=None: getattr(candidate, key, default)
    name = get("name_display") or get("name") or ""
    fields = [f"Type: {get('kg_node_type') or get('node_type') or ''}", f"Name: {name}"]
    description = get("description")
    if description:
        fields.append(f"Description: {description}")
    aliases = get("aliases") or []
    if aliases:
        fields.append("Aliases: " + "; ".join(map(str, aliases[:12])))
    edges = []
    for edge in neighborhood:
        direction = str(edge.get("direction") or "")
        other_type = str(edge.get("other_type") or "")
        other_name = str(edge.get("other_value") or "")
        rel_type = str(edge.get("rel_type") or "")
        if rel_type and other_name:
            edges.append(f"{direction} {rel_type} {other_type} {other_name}")
    if edges:
        fields.append("Graph neighborhood: " + "; ".join(edges[:20]))
    return "\n".join(fields)


def cross_encoder_scores(
    *, query: str, candidates: list[Any], reranker: Any | None,
    neighborhoods: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, float]:
    """Score candidate evidence with raw model logits; never normalize per candidate set."""
    if reranker is None or not candidates:
        return {}
    neighborhoods = neighborhoods or {}
    pairs = []
    ids = []
    for candidate in candidates:
        get = candidate.get if isinstance(candidate, dict) else lambda key, default=None: getattr(candidate, key, default)
        candidate_id = str(get("entity_id") or "")
        if not candidate_id:
            continue
        pairs.append((query, candidate_document(candidate, neighborhoods.get(candidate_id, []))))
        ids.append(candidate_id)
    if not pairs:
        return {}
    try:
        raw_scores = reranker.predict(pairs)
        if len(raw_scores) != len(ids):
            return {}
        return {candidate_id: float(score) for candidate_id, score in zip(ids, raw_scores)}
    except Exception as exc:  # noqa: BLE001 — keep ingestion alive, but make degradation visible
        logger.info("candidate_reranking.unavailable", extra={"error": str(exc)})
        return {}

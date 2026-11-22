"""Offline candidate-ranking evaluation helpers; this module is not used by ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    text: str
    lexical_rank: int | None
    semantic_rank: int | None
    node_type: str | None = None


def eligible_candidates(
    candidates: Sequence[Candidate], *, required_node_type: str | None
) -> list[Candidate]:
    """Apply a hard endpoint-type gate before ranking candidates."""
    if required_node_type is None:
        return list(candidates)
    return [candidate for candidate in candidates if candidate.node_type == required_node_type]


def reciprocal_rank_fusion(
    candidates: Sequence[Candidate], *, rank_constant: int = 60
) -> list[Candidate]:
    """Return the supplied candidate pool ordered by reciprocal-rank fusion."""
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")

    def score(candidate: Candidate) -> float:
        return sum(
            1.0 / (rank_constant + rank)
            for rank in (candidate.lexical_rank, candidate.semantic_rank)
            if rank is not None and rank > 0
        )

    return sorted(candidates, key=lambda item: (-score(item), item.candidate_id))


def ranking_metrics(
    ranked_ids: Sequence[str], relevant_id: str | None
) -> dict[str, float | bool]:
    """Compute exact top-1, reciprocal rank, and no-match false-link metrics."""
    if relevant_id is None:
        return {
            "top1_correct": not ranked_ids,
            "reciprocal_rank": 0.0,
            "false_link": bool(ranked_ids),
        }
    try:
        rank = ranked_ids.index(relevant_id) + 1
    except ValueError:
        rank = 0
    return {
        "top1_correct": rank == 1,
        "reciprocal_rank": 1.0 / rank if rank else 0.0,
        "false_link": False,
    }

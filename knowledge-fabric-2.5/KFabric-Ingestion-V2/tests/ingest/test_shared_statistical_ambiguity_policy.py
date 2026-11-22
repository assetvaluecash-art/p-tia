"""Objective: keep node and relationship HITL decisions on one statistical gate."""

import pytest

from kbfabric_ingest._common.env_config import _as_confidence_threshold
from kbfabric_ingest.ingest.entity_mapping import (
    MappingDecision,
    combine_mapping_confidence,
    map_extracted_entity,
)
from kbfabric_ingest.ingest.lane2_node_reconciliation import (
    confidence_allows_auto_resolution,
    ExistingNode,
    get_auto_resolution_confidence_threshold,
)
from kbfabric_ingest.ingest.relationship_endpoint_resolution import (
    ContextResolutionCandidate,
    decide_context_resolution,
)


def test_shared_auto_resolution_threshold_is_strictly_greater_than_point_eight():
    """Objective: define behavior exactly at and around the agreed boundary."""
    assert confidence_allows_auto_resolution(0.800001)
    assert not confidence_allows_auto_resolution(0.8)
    assert not confidence_allows_auto_resolution(0.799999)
    assert not confidence_allows_auto_resolution(float("nan"))


def test_auto_resolution_threshold_is_read_from_environment_config():
    """Objective: keep the runtime cutoff parameterized outside source code."""
    assert get_auto_resolution_confidence_threshold() == 0.8
    assert confidence_allows_auto_resolution(0.76, threshold=0.75)
    assert not confidence_allows_auto_resolution(0.75, threshold=0.75)


def test_environment_threshold_rejects_non_finite_or_out_of_range_values():
    """Objective: prevent invalid confidence cutoffs entering runtime config."""
    assert _as_confidence_threshold("0") == 0.0
    assert _as_confidence_threshold("1") == 1.0
    for invalid in ("-0.01", "1.01", "nan", "inf"):
        with pytest.raises(ValueError):
            _as_confidence_threshold(invalid)


def test_node_confidence_uses_the_same_lexical_semantic_fusion_signal():
    """Objective: prevent node-only type/context weights changing the cutoff."""
    score = combine_mapping_confidence(
        fusion=0.79,
        type_score=1.0,
        rel_score=1.0,
    )
    assert score.combined_confidence == score.fusion_score == 0.79
    assert not confidence_allows_auto_resolution(score.combined_confidence)


def test_relationship_endpoint_uses_the_same_strict_threshold():
    """Objective: send endpoint scores <= 0.8 to HITL and resolve scores > 0.8."""
    def decide(score: float):
        candidate = ContextResolutionCandidate(
            entity_id="candidate-1",
            node_type="System",
            name="Card Authorization Service",
            lexical_score=score,
            semantic_score=score,
            fusion_score=score,
            rank=1,
        )
        return decide_context_resolution(
            direction="outgoing",
            rel_type="INTEGRATES_WITH",
            source_entity_id="source-1",
            target_type="System",
            target_value="Card Authorization Service",
            candidates=[candidate],
        )

    review = decide(0.8)
    assert review.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    assert review.confidence == 0.8

    resolved = decide(0.800001)
    assert resolved.decision == "RESOLVED"
    assert resolved.selected_entity_id == "candidate-1"


def test_missing_relationship_candidates_are_reviewable_with_zero_confidence():
    """Objective: make missing endpoint candidates a HITL case, not silent deferral."""
    result = decide_context_resolution(
        direction="outgoing",
        rel_type="INTEGRATES_WITH",
        source_entity_id="source-1",
        target_type="System",
        target_value="Unmatched System",
        candidates=[],
    )
    assert result.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    assert result.confidence == 0.0
    assert result.reason_code == "no_eligible_candidates_below_auto_resolution_threshold"


class _FixedSemantic:
    name = "fixed-test"
    is_mock = False

    def embed(self, _text):
        return [1.0, 0.0]


class _ScopedCandidateReader:
    product_line_id = "pl-retail-cards"

    def fetch_candidate_neighborhoods(self, entity_ids, *, domain):
        assert entity_ids == ["system-1"]
        assert domain == "consumer_retail"
        return {"system-1": [{
            "direction": "outgoing",
            "rel_type": "INTEGRATES_WITH",
            "other_type": "System",
            "other_value": "Card Network Adapter",
        }]}


def test_node_above_threshold_uses_statistics_and_persists_neighbor_evidence():
    """Objective: resolve strong node matches without LLM and keep graph evidence."""
    def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("node matching must not call an LLM")

    result = map_extracted_entity(
        {"type": "System", "value": "Card Authorization Service"},
        lane="lane2",
        relationships=[],
        ontology={},
        candidate_reader=_ScopedCandidateReader(),
        semantic=_FixedSemantic(),
        llm_callable=forbidden_llm,
        existing_candidates=[ExistingNode(
            entity_id="system-1",
            name="Card Authorization Service",
            kg_node_type="System",
            labels=["System"],
            embedding=[1.0, 0.0],
        )],
        provenance={"domain": "consumer_retail"},
        use_llm_adjudication=True,
    )

    assert result.decision == MappingDecision.NO_CHANGE
    assert result.selected_entity_id == "system-1"
    assert result.score_breakdown.combined_confidence > 0.8
    assert result.candidates[0].graph_neighborhood[0]["other_value"] == "Card Network Adapter"

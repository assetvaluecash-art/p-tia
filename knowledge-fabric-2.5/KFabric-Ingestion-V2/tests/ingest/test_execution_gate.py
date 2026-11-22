from kbfabric_ingest.ingest.execution_gate import (
    ExecutionOutcome,
    HITLMode,
    evaluate_execution_gate,
)
from kbfabric_ingest.ingest.entity_mapping import (
    EntityMappingResult,
    MappingDecision,
)
from kbfabric_ingest.ingest.lane2_node_reconciliation import IncomingNode
from kbfabric_ingest.ingest.lane2_node_reconciliation import CandidateMatch, ExistingNode
from kbfabric_ingest.ingest.relationship_endpoint_resolution import ContextResolutionResult
from kbfabric_ingest.ingest.relationship_endpoint_resolution import ContextResolutionCandidate


def make_mapping(
    decision=MappingDecision.CREATE_NEW,
    resolutions=None,
):
    incoming = IncomingNode(
        temporary_id="tmp-001",
        node_type="Process",
        name="Loan Application",
        description="Loan application process",
        properties={},
        source_file="test.pdf",
        block_id="block-001",
        domain="Lending",
        extraction_confidence=0.95,
    )

    return EntityMappingResult(
        lane="test",
        incoming=incoming,
        decision=decision,
        reason_code="test",
        context_resolutions=resolutions or [],
        provenance={"source_document": "test.pdf"},
    )


def make_resolution(
    decision,
    reason_code="",
):
    return ContextResolutionResult(
        direction="outgoing",
        rel_type="GOVERNED_BY",
        source_entity_id="tmp-001",
        target_type="Rule",
        target_value="Loan Approval Rule",
        decision=decision,
        confidence=0.90,
        reason_code=reason_code,
        justification="test justification",
        provenance={"source_block_id": "block-001"},
    )


def test_invalid_ontology_relationship_is_always_rejected():
    mapping = make_mapping(
        resolutions=[
            make_resolution(
                "REJECTED",
                "invalid_relationship_triple",
            )
        ]
    )

    for mode in HITLMode:
        result = evaluate_execution_gate(mapping, hitl_mode=mode)
        assert result.outcome == ExecutionOutcome.REJECTED


def test_primary_entity_review_requires_hitl():
    mapping = make_mapping(MappingDecision.REQUIRES_REVIEW)

    for mode in HITLMode:
        result = evaluate_execution_gate(mapping, hitl_mode=mode)
        assert result.outcome == ExecutionOutcome.HITL_REQUIRED


def test_auto_write_selects_top_ranked_same_type_primary_candidate():
    mapping = make_mapping(MappingDecision.REQUIRES_REVIEW)
    mapping.candidates = [
        CandidateMatch(
            candidate=ExistingNode(
                entity_id="entity-top", name="Loan Application", kg_node_type="Process"
            ),
            lexical_score=0.8, semantic_score=0.9, fusion_score=0.88, rank=1,
        ),
        CandidateMatch(
            candidate=ExistingNode(
                entity_id="entity-second", name="Loan Origination", kg_node_type="Process"
            ),
            lexical_score=0.7, semantic_score=0.8, fusion_score=0.78, rank=2,
        ),
    ]

    result = evaluate_execution_gate(mapping, hitl_mode=HITLMode.AUTO_WRITE)

    assert result.outcome == ExecutionOutcome.WRITE_READY
    assert result.reason_code == "auto_write_top_ranked_entity_candidate"
    assert result.selected_entity_id == "entity-top"
    assert result.mapping_decision == MappingDecision.REUSE_EXISTING.value
    assert result.provenance["auto_write_decision"]["original_mapping_decision"] == "REQUIRES_REVIEW"
    assert result.provenance["auto_write_decision"]["candidate_count"] == 2

    from kbfabric_ingest.ingest.execution_plan import (
        ExecutionAction,
        build_execution_plan,
    )

    plan = build_execution_plan(mapping=mapping, gate=result, plan_id="auto-write-test")
    assert plan.action == ExecutionAction.WRITE_NOW
    assert plan.selected_entity_id == "entity-top"
    assert plan.entity_operations[0].operation == "REUSE"
    assert plan.provenance["auto_write_decision"]["selected_entity_id"] == "entity-top"
    assert plan.execution_metadata["gate_reason_code"] == "auto_write_top_ranked_entity_candidate"


def test_auto_write_fails_closed_when_primary_candidate_type_does_not_match():
    mapping = make_mapping(MappingDecision.REQUIRES_REVIEW)
    mapping.candidates = [
        CandidateMatch(
            candidate=ExistingNode(
                entity_id="wrong-type", name="Loan Application", kg_node_type="System"
            ),
            lexical_score=0.9, semantic_score=0.9, fusion_score=0.9, rank=1,
        )
    ]

    result = evaluate_execution_gate(mapping, hitl_mode=HITLMode.AUTO_WRITE)

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED
    assert result.reason_code == "primary_entity_review_required"


def test_auto_write_selects_top_ranked_relationship_candidate_with_audit_evidence():
    resolution = make_resolution("RELATIONSHIP_REVIEW_REQUIRED")
    resolution.candidates = [
        ContextResolutionCandidate(
            entity_id="target-top", node_type="Rule", name="Loan Approval Rule",
            fusion_score=0.82, rank=1,
        ),
        ContextResolutionCandidate(
            entity_id="target-second", node_type="Rule", name="Credit Policy",
            fusion_score=0.76, rank=2,
        ),
    ]
    mapping = make_mapping(resolutions=[resolution])

    result = evaluate_execution_gate(mapping, hitl_mode=HITLMode.AUTO_WRITE)

    assert result.outcome == ExecutionOutcome.WRITE_READY
    assert result.reason_code == "auto_write_top_ranked_relationship_candidates"
    assert result.context_resolutions[0].decision == "RESOLVED"
    assert result.context_resolutions[0].selected_entity_id == "target-top"
    assert result.context_resolutions[0].provenance["auto_write_decision"]["candidate_count"] == 2


def test_auto_write_does_not_silently_drop_relationship_without_eligible_candidate():
    mapping = make_mapping(resolutions=[make_resolution("RELATIONSHIP_REVIEW_REQUIRED")])

    result = evaluate_execution_gate(mapping, hitl_mode=HITLMode.AUTO_WRITE)

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED
    assert result.reason_code == "auto_write_relationship_candidate_not_eligible"


def test_clear_mapping_is_write_ready_in_auto_write():
    mapping = make_mapping()

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.AUTO_WRITE,
    )

    assert result.outcome == ExecutionOutcome.WRITE_READY


def test_clear_mapping_requires_review_under_review_all():
    mapping = make_mapping()

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.REVIEW_ALL,
    )

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED


def test_ambiguous_relationship_requires_hitl_under_review_ambiguous():
    mapping = make_mapping(
        resolutions=[
            make_resolution("RELATIONSHIP_REVIEW_REQUIRED")
        ]
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.REVIEW_AMBIGUOUS,
    )

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED


def test_ambiguous_relationship_without_candidate_fails_closed_in_auto_write():
    mapping = make_mapping(
        resolutions=[
            make_resolution("RELATIONSHIP_REVIEW_REQUIRED")
        ]
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.AUTO_WRITE,
    )

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED
    assert result.reason_code == "auto_write_relationship_candidate_not_eligible"


def test_unresolved_reference_is_writable_in_auto_write():
    mapping = make_mapping(
        resolutions=[
            make_resolution("UNRESOLVED_REFERENCE")
        ]
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.AUTO_WRITE,
    )

    assert result.outcome == ExecutionOutcome.UNRESOLVED_WRITE


def test_unresolved_reference_is_writable_under_review_ambiguous():
    mapping = make_mapping(
        resolutions=[
            make_resolution("UNRESOLVED_REFERENCE")
        ]
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.REVIEW_AMBIGUOUS,
    )

    assert result.outcome == ExecutionOutcome.UNRESOLVED_WRITE


def test_unresolved_reference_requires_review_under_review_all():
    mapping = make_mapping(
        resolutions=[
            make_resolution("UNRESOLVED_REFERENCE")
        ]
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=HITLMode.REVIEW_ALL,
    )

    assert result.outcome == ExecutionOutcome.HITL_REQUIRED


def test_run_level_hitl_mode_overrides_service_default(monkeypatch):
    """Objective: verify the ingestion request's review policy wins over the
    process default. This matters because each run is configured in Ingestion
    Studio and its decision path must remain stable if service config changes.
    Invariant: an explicit valid run policy is used unchanged; absent a run
    choice, the configured service default remains the fallback.
    """
    from kbfabric_ingest.ingest import execution_gate

    monkeypatch.setattr(
        execution_gate,
        "get_configured_hitl_mode",
        lambda: HITLMode.REVIEW_AMBIGUOUS,
    )

    assert execution_gate.resolve_hitl_mode("review_all") == HITLMode.REVIEW_ALL
    assert execution_gate.resolve_hitl_mode(None) == HITLMode.REVIEW_AMBIGUOUS


def test_run_level_hitl_mode_rejects_unknown_policy():
    """Objective: reject unsupported review policy values before planning.

    This protects the persisted run policy contract from typos or unsupported
    UI values; invalid policy input must never silently fall back to a mode.
    """
    import pytest
    from kbfabric_ingest.ingest.execution_gate import resolve_hitl_mode

    with pytest.raises(ValueError):
        resolve_hitl_mode("review_sometimes")

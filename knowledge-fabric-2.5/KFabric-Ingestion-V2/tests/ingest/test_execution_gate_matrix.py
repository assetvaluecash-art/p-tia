import logging
from itertools import product

import pytest

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
from kbfabric_ingest.ingest.relationship_endpoint_resolution import ContextResolutionResult


logger = logging.getLogger(__name__)


def make_mapping(mapping_decision, resolutions):
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
        lane="execution_gate_matrix",
        incoming=incoming,
        decision=mapping_decision,
        reason_code="synthetic_matrix",
        context_resolutions=resolutions,
        provenance={"source_document": "test.pdf"},
    )


def make_resolution(state, index):
    reason_codes = {
        "RESOLVED": "high_confidence_clear_candidate",
        "UNRESOLVED_REFERENCE": "no_eligible_candidates",
        "RELATIONSHIP_REVIEW_REQUIRED": "ambiguous_or_insufficient_context",
        "REJECTED": "invalid_relationship_triple",
    }

    return ContextResolutionResult(
        direction="outgoing",
        rel_type="GOVERNED_BY",
        source_entity_id="tmp-001",
        target_type="Rule",
        target_value=f"Synthetic Rule {index}",
        selected_entity_id=(
            f"rule-{index}"
            if state == "RESOLVED"
            else None
        ),
        decision=state,
        confidence={
            "RESOLVED": 0.95,
            "UNRESOLVED_REFERENCE": 0.0,
            "RELATIONSHIP_REVIEW_REQUIRED": 0.60,
            "REJECTED": 0.0,
        }[state],
        reason_code=reason_codes[state],
        justification=f"Synthetic {state}",
        provenance={"source_block_id": "block-001"},
    )


CONTEXT_INTENTS = {
    "NONE": [],
    "RESOLVED": [make_resolution("RESOLVED", 1)],
    "UNRESOLVED": [make_resolution("UNRESOLVED_REFERENCE", 1)],
    "AMBIGUOUS": [make_resolution("RELATIONSHIP_REVIEW_REQUIRED", 1)],
    "INVALID": [make_resolution("REJECTED", 1)],
    "RESOLVED+UNRESOLVED": [
        make_resolution("RESOLVED", 1),
        make_resolution("UNRESOLVED_REFERENCE", 2),
    ],
    "RESOLVED+AMBIGUOUS": [
        make_resolution("RESOLVED", 1),
        make_resolution("RELATIONSHIP_REVIEW_REQUIRED", 2),
    ],
    "UNRESOLVED+AMBIGUOUS": [
        make_resolution("UNRESOLVED_REFERENCE", 1),
        make_resolution("RELATIONSHIP_REVIEW_REQUIRED", 2),
    ],
    "RESOLVED+UNRESOLVED+AMBIGUOUS": [
        make_resolution("RESOLVED", 1),
        make_resolution("UNRESOLVED_REFERENCE", 2),
        make_resolution("RELATIONSHIP_REVIEW_REQUIRED", 3),
    ],
    "RESOLVED+INVALID": [
        make_resolution("RESOLVED", 1),
        make_resolution("REJECTED", 2),
    ],
}


@pytest.mark.parametrize(
    "mapping_decision,context_name,hitl_mode",
    product(
        [
            MappingDecision.CREATE_NEW,
            MappingDecision.REUSE_EXISTING,
            MappingDecision.REQUIRES_REVIEW,
        ],
        CONTEXT_INTENTS.keys(),
        list(HITLMode),
    ),
    ids=lambda value: getattr(value, "value", str(value)),
)
def test_execution_gate_contract_matrix(
    mapping_decision,
    context_name,
    hitl_mode,
):
    mapping = make_mapping(
        mapping_decision,
        CONTEXT_INTENTS[context_name],
    )

    result = evaluate_execution_gate(
        mapping,
        hitl_mode=hitl_mode,
    )

    assert result.outcome in set(ExecutionOutcome)
    assert result.mapping_decision == mapping_decision.value
    assert result.reason_code
    assert result.justification

    # Ontology-invalid relationships are terminal and cannot be
    # converted into a HITL decision by any mapping state or HITL mode.
    if context_name in {"INVALID", "RESOLVED+INVALID"}:
        assert result.outcome == ExecutionOutcome.REJECTED
        assert result.reason_code == "invalid_relationship_triple"

    # Primary entity review applies only when no ontology-invalid
    # relationship has already caused terminal rejection.
    elif mapping_decision == MappingDecision.REQUIRES_REVIEW:
        assert result.outcome == ExecutionOutcome.HITL_REQUIRED
        assert result.reason_code == "primary_entity_review_required"

    logger.info(
        "execution_gate.matrix.case.completed",
        extra={
            "mapping_decision": mapping_decision.value,
            "context_intent": context_name,
            "hitl_mode": hitl_mode.value,
            "outcome": result.outcome.value,
            "reason_code": result.reason_code,
        },
    )


def test_execution_gate_matrix_has_expected_case_count():
    expected = 3 * len(CONTEXT_INTENTS) * len(HITLMode)

    assert expected == 90

    logger.info(
        "execution_gate.matrix.completed",
        extra={
            "mapping_decisions": 3,
            "context_intents": len(CONTEXT_INTENTS),
            "hitl_modes": len(HITLMode),
            "total_cases": expected,
        },
    )

import logging

import pytest
from pydantic import ValidationError

from kbfabric_ingest.ingest.execution_plan import (
    EntityOperation,
    ExecutionAction,
    ExecutionPlan,
    RelationshipOperation,
    UnresolvedReferenceOperation,
)


logger = logging.getLogger(__name__)


def make_plan(action: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-001",
        action=action,
        entity_operations=[
            EntityOperation(
                operation="CREATE",
                entity_type="Process",
                temporary_id="tmp-process-1",
                name="Loan Application",
                properties={"confidence": 0.94},
            )
        ],
        relationship_operations=[
            RelationshipOperation(
                operation="CREATE",
                rel_type="PART_OF",
                source_temporary_id="tmp-process-1",
                target_entity_id="function-001",
                target_type="Function",
                target_value="Loan Origination",
            )
        ],
        unresolved_reference_operations=[],
        mapping_decision="CREATE_NEW",
        selected_entity_id=None,
        provenance={
            "source_block_id": "block-001",
            "source_document": "loan-process.pdf",
        },
        justification="Create Process Loan Application and link it to "
        "the resolved Function Loan Origination.",
        ontology_version="test-ontology-v1",
        retrieval_version="test-retrieval-v1",
        ranking_version="test-ranking-v1",
    )


@pytest.mark.parametrize(
    "action",
    list(ExecutionAction),
)
def test_execution_plan_supports_all_actions(action):
    plan = make_plan(action)

    assert plan.action == action
    assert plan.plan_id == "plan-001"
    assert plan.entity_operations
    assert plan.relationship_operations
    assert plan.justification
    assert plan.provenance

    logger.info(
        "execution_plan.contract.action.completed",
        extra={
            "plan_id": plan.plan_id,
            "action": plan.action.value,
        },
    )


def test_execution_plan_can_carry_unresolved_reference():
    plan = make_plan(ExecutionAction.WRITE_UNRESOLVED)

    plan.unresolved_reference_operations.append(
        UnresolvedReferenceOperation(
            operation="CREATE_UNRESOLVED_REFERENCE",
            rel_type="GOVERNED_BY",
            source_temporary_id="tmp-process-1",
            target_type="Rule",
            target_value="Unknown repayment rule",
            reason_code="no_eligible_candidates",
        )
    )

    assert len(plan.unresolved_reference_operations) == 1
    assert (
        plan.unresolved_reference_operations[0].reason_code
        == "no_eligible_candidates"
    )

    logger.info(
        "execution_plan.contract.unresolved_reference.completed",
        extra={
            "plan_id": plan.plan_id,
            "operation_count": len(plan.unresolved_reference_operations),
        },
    )


def test_execution_plan_is_serializable():
    plan = make_plan(ExecutionAction.QUEUE_FOR_REVIEW)

    payload = plan.model_dump(mode="json")

    assert payload["action"] == "QUEUE_FOR_REVIEW"
    assert payload["plan_id"] == "plan-001"
    assert payload["provenance"]["source_block_id"] == "block-001"
    assert payload["ontology_version"] == "test-ontology-v1"

    logger.info(
        "execution_plan.contract.serialization.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_execution_plan_requires_mapping_decision_and_justification():
    with pytest.raises(ValidationError):
        ExecutionPlan(
            plan_id="plan-001",
            action=ExecutionAction.WRITE_NOW,
        )

    logger.info(
        "execution_plan.contract.required_fields.completed",
        extra={"validated": True},
    )


def test_execution_plan_does_not_require_a_selected_entity():
    plan = make_plan(ExecutionAction.WRITE_UNRESOLVED)

    assert plan.selected_entity_id is None

    logger.info(
        "execution_plan.contract.unresolved_without_entity.completed",
        extra={"plan_id": plan.plan_id},
    )


from types import SimpleNamespace

from kbfabric_ingest.ingest.execution_plan import build_execution_plan


def make_gate(outcome, *, context_resolutions=None):
    return SimpleNamespace(
        outcome=outcome,
        mapping_decision="CREATE_NEW",
        reason_code="test",
        justification="Deterministic test justification.",
        selected_entity_id="entity-001",
        source_temporary_id="tmp-001",
        context_resolutions=context_resolutions or [],
        provenance={"source_block_id": "block-001"},
    )


def make_mapping():
    return SimpleNamespace(
        decision="CREATE_NEW",
        incoming=SimpleNamespace(
            node_type="Process",
            name="Loan Application",
            properties={"domain": "Lending"},
        ),
        provenance={"source_document": "loan-process.pdf"},
    )


def make_resolution(decision, **kwargs):
    values = {
        "direction": "outgoing",
        "rel_type": "GOVERNED_BY",
        "source_entity_id": "entity-001",
        "target_type": "Rule",
        "target_value": "Repayment Rule",
        "selected_entity_id": None,
        "decision": decision,
        "confidence": 0.91,
        "reason_code": "test",
        "justification": "Deterministic test evidence.",
        "provenance": {"source_block_id": "block-001"},
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("outcome", "expected_action"),
    [
        ("WRITE_READY", ExecutionAction.WRITE_NOW),
        ("WRITE_UNRESOLVED", ExecutionAction.WRITE_UNRESOLVED),
        ("HITL_REQUIRED", ExecutionAction.QUEUE_FOR_REVIEW),
        ("REJECTED", ExecutionAction.REJECT),
    ],
)
def test_build_execution_plan_maps_gate_outcome(outcome, expected_action):
    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(outcome),
        plan_id=f"plan-{outcome.lower()}",
        ontology_version="ontology-v1",
        retrieval_version="retrieval-v1",
        ranking_version="ranking-v1",
    )

    assert plan.action == expected_action
    assert plan.plan_id == f"plan-{outcome.lower()}"
    assert plan.execution_metadata["execution_outcome"] == outcome

    logger.info(
        "execution_plan.builder.outcome.completed",
        extra={
            "plan_id": plan.plan_id,
            "outcome": outcome,
            "action": expected_action.value,
        },
    )


def test_build_execution_plan_resolved_context_becomes_relationship_operation():
    resolution = make_resolution(
        "RESOLVED",
        selected_entity_id="rule-001",
    )

    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(
            "WRITE_READY",
            context_resolutions=[resolution],
        ),
        plan_id="plan-resolved",
    )

    assert len(plan.relationship_operations) == 1

    operation = plan.relationship_operations[0]
    assert operation.rel_type == "GOVERNED_BY"
    assert operation.target_entity_id == "rule-001"
    assert operation.target_type == "Rule"
    assert operation.target_value == "Repayment Rule"

    logger.info(
        "execution_plan.builder.resolved_relationship.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_build_execution_plan_unresolved_context_becomes_unresolved_operation():
    resolution = make_resolution(
        "UNRESOLVED_REFERENCE",
        selected_entity_id=None,
        reason_code="no_eligible_candidates",
    )

    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(
            "WRITE_UNRESOLVED",
            context_resolutions=[resolution],
        ),
        plan_id="plan-unresolved",
    )

    assert len(plan.unresolved_reference_operations) == 1
    assert not plan.relationship_operations

    operation = plan.unresolved_reference_operations[0]
    assert operation.rel_type == "GOVERNED_BY"
    assert operation.target_type == "Rule"
    assert operation.target_value == "Repayment Rule"
    assert operation.reason_code == "no_eligible_candidates"

    logger.info(
        "execution_plan.builder.unresolved_reference.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_build_execution_plan_preserves_review_context_for_hitl():
    resolution = make_resolution(
        "RELATIONSHIP_REVIEW_REQUIRED",
        selected_entity_id=None,
        reason_code="ambiguous_or_insufficient_context",
    )

    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(
            "HITL_REQUIRED",
            context_resolutions=[resolution],
        ),
        plan_id="plan-review",
    )

    assert plan.action == ExecutionAction.QUEUE_FOR_REVIEW
    assert len(plan.context_resolutions) == 1
    assert (
        plan.context_resolutions[0]["reason_code"]
        == "ambiguous_or_insufficient_context"
    )
    assert not plan.relationship_operations

    logger.info(
        "execution_plan.builder.hitl_context.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_build_execution_plan_preserves_terminal_rejection():
    resolution = make_resolution(
        "REJECTED",
        selected_entity_id=None,
        reason_code="invalid_relationship_triple",
    )

    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(
            "REJECTED",
            context_resolutions=[resolution],
        ),
        plan_id="plan-rejected",
    )

    assert plan.action == ExecutionAction.REJECT
    assert not plan.relationship_operations
    assert not plan.unresolved_reference_operations
    assert plan.context_resolutions[0]["reason_code"] == (
        "invalid_relationship_triple"
    )

    logger.info(
        "execution_plan.builder.rejection.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_build_execution_plan_never_requires_re_resolution():
    resolution = make_resolution(
        "RESOLVED",
        selected_entity_id="rule-001",
    )

    plan = build_execution_plan(
        mapping=make_mapping(),
        gate=make_gate(
            "WRITE_READY",
            context_resolutions=[resolution],
        ),
        plan_id="plan-snapshot",
    )

    assert plan.context_resolutions[0]["selected_entity_id"] == "rule-001"
    assert plan.relationship_operations[0].target_entity_id == "rule-001"
    assert plan.provenance["source_block_id"] == "block-001"
    assert plan.provenance["source_document"] == "loan-process.pdf"

    logger.info(
        "execution_plan.builder.snapshot.completed",
        extra={"plan_id": plan.plan_id},
    )


def test_build_execution_plan_rejects_unknown_gate_outcome():
    with pytest.raises(ValueError, match="Unsupported execution outcome"):
        build_execution_plan(
            mapping=make_mapping(),
            gate=make_gate("UNKNOWN_OUTCOME"),
            plan_id="plan-invalid",
        )

    logger.info(
        "execution_plan.builder.invalid_outcome.completed",
        extra={"validated": True},
    )

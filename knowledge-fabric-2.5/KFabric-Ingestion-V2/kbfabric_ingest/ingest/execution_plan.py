"""Execution plan contract for the ingestion write boundary.

ExecutionPlan is the immutable hand-off between decisioning and execution.
It contains enough information for approved plans to execute without
re-running extraction, mapping, context resolution, or retrieval.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionAction(str, Enum):
    WRITE_NOW = "WRITE_NOW"
    QUEUE_FOR_REVIEW = "QUEUE_FOR_REVIEW"
    WRITE_UNRESOLVED = "WRITE_UNRESOLVED"
    REJECT = "REJECT"


class EntityOperation(BaseModel):
    operation: str
    entity_type: str
    entity_id: str | None = None
    temporary_id: str | None = None
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class RelationshipOperation(BaseModel):
    operation: str
    rel_type: str
    source_entity_id: str | None = None
    source_temporary_id: str | None = None
    target_entity_id: str | None = None
    target_temporary_id: str | None = None
    target_type: str
    target_value: str
    properties: dict[str, Any] = Field(default_factory=dict)


class UnresolvedReferenceOperation(BaseModel):
    operation: str
    rel_type: str
    source_entity_id: str | None = None
    source_temporary_id: str | None = None
    target_type: str
    target_value: str
    reason_code: str
    properties: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    """Deterministic executable decision produced before KG writing."""

    plan_id: str
    action: ExecutionAction

    entity_operations: list[EntityOperation] = Field(default_factory=list)
    relationship_operations: list[RelationshipOperation] = Field(
        default_factory=list
    )
    unresolved_reference_operations: list[UnresolvedReferenceOperation] = Field(
        default_factory=list
    )

    mapping_decision: str
    selected_entity_id: str | None = None

    # Snapshot of the context decisions presented to execution/HITL.
    # Keeping this on the plan means approval does not require re-resolution.
    context_resolutions: list[dict[str, Any]] = Field(default_factory=list)

    provenance: dict[str, Any] = Field(default_factory=dict)
    justification: str

    ontology_version: str | None = None
    retrieval_version: str | None = None
    ranking_version: str | None = None

    execution_metadata: dict[str, Any] = Field(default_factory=dict)


def build_execution_plan(
    *,
    mapping: Any,
    gate: Any,
    plan_id: str,
    ontology_version: str | None = None,
    retrieval_version: str | None = None,
    ranking_version: str | None = None,
) -> ExecutionPlan:
    """Build the immutable execution hand-off from mapping and gate results.

    This function performs no retrieval, reconciliation, ontology inference,
    Neo4j access, or LLM calls. Approved plans must be executable without
    repeating upstream decisioning.
    """
    from kbfabric_ingest.ingest.execution_gate import ExecutionOutcome

    outcome = gate.outcome
    if isinstance(outcome, ExecutionOutcome):
        outcome_value = outcome.value
    else:
        outcome_value = str(outcome)

    action_by_outcome = {
        "WRITE_READY": ExecutionAction.WRITE_NOW,
        "WRITE_UNRESOLVED": ExecutionAction.WRITE_UNRESOLVED,
        "HITL_REQUIRED": ExecutionAction.QUEUE_FOR_REVIEW,
        "REJECTED": ExecutionAction.REJECT,
    }

    try:
        action = action_by_outcome[outcome_value]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported execution outcome: {outcome_value}"
        ) from exc

    entity_operations: list[EntityOperation] = []
    relationship_operations: list[RelationshipOperation] = []
    unresolved_operations: list[UnresolvedReferenceOperation] = []

    selected_entity_id = getattr(gate, "selected_entity_id", None)
    source_temporary_id = getattr(gate, "source_temporary_id", None)

    # The mapped entity itself is executable only when the mapping decision
    # provides a concrete write target or a new entity.
    mapping_decision = getattr(mapping, "decision", None)
    mapping_decision_value = (
        mapping_decision.value
        if hasattr(mapping_decision, "value")
        else str(mapping_decision or "")
    )

    incoming = getattr(mapping, "incoming", None)
    entity_type = getattr(incoming, "node_type", None)
    entity_name = getattr(incoming, "name", None)

    if entity_type and entity_name and action != ExecutionAction.REJECT:
        if selected_entity_id:
            entity_operations.append(
                EntityOperation(
                    operation="REUSE",
                    entity_type=entity_type,
                    entity_id=selected_entity_id,
                    name=entity_name,
                )
            )
        elif mapping_decision_value in {"CREATE_NEW", "CREATE_DELTA_NODE_AND_RELATIONSHIP"}:
            entity_operations.append(
                EntityOperation(
                    operation="CREATE",
                    entity_type=entity_type,
                    temporary_id=source_temporary_id,
                    name=entity_name,
                    properties=getattr(incoming, "properties", {}) or {},
                )
            )

    context_resolutions = []
    for resolution in getattr(gate, "context_resolutions", []) or []:
        if hasattr(resolution, "model_dump"):
            snapshot = resolution.model_dump(mode="json")
        elif isinstance(resolution, dict):
            snapshot = dict(resolution)
        else:
            snapshot = dict(vars(resolution))
        context_resolutions.append(snapshot)

        decision = snapshot.get("decision")
        if decision == "REJECTED":
            # The gate should already have made this terminal. Never turn it
            # into a reviewable operation here.
            continue

        if decision == "RESOLVED" and snapshot.get("selected_entity_id"):
            relationship_operations.append(
                RelationshipOperation(
                    operation="CREATE",
                    rel_type=snapshot["rel_type"],
                    source_entity_id=(
                        snapshot.get("source_entity_id")
                        or selected_entity_id
                    ),
                    source_temporary_id=source_temporary_id,
                    target_entity_id=snapshot["selected_entity_id"],
                    target_type=snapshot["target_type"],
                    target_value=snapshot["target_value"],
                    properties={
                        "confidence": snapshot.get("confidence", 0.0),
                        "reason_code": snapshot.get("reason_code", ""),
                    },
                )
            )
        elif decision == "UNRESOLVED_REFERENCE":
            unresolved_operations.append(
                UnresolvedReferenceOperation(
                    operation="CREATE_UNRESOLVED_REFERENCE",
                    rel_type=snapshot["rel_type"],
                    source_entity_id=(
                        snapshot.get("source_entity_id")
                        or selected_entity_id
                    ),
                    source_temporary_id=source_temporary_id,
                    target_type=snapshot["target_type"],
                    target_value=snapshot["target_value"],
                    reason_code=snapshot.get(
                        "reason_code",
                        "unresolved_reference",
                    ),
                    properties={
                        "confidence": snapshot.get("confidence", 0.0),
                    },
                )
            )

    provenance = dict(getattr(gate, "provenance", {}) or {})
    mapping_provenance = getattr(mapping, "provenance", None)
    if isinstance(mapping_provenance, dict):
        provenance = {**mapping_provenance, **provenance}

    justification = getattr(gate, "justification", "") or (
        f"Execution action {action.value} for mapping decision "
        f"{mapping_decision_value}."
    )

    plan = ExecutionPlan(
        plan_id=plan_id,
        action=action,
        entity_operations=entity_operations,
        relationship_operations=relationship_operations,
        unresolved_reference_operations=unresolved_operations,
        mapping_decision=mapping_decision_value,
        selected_entity_id=selected_entity_id,
        context_resolutions=context_resolutions,
        provenance=provenance,
        justification=justification,
        ontology_version=ontology_version,
        retrieval_version=retrieval_version,
        ranking_version=ranking_version,
        execution_metadata={
            "execution_outcome": outcome_value,
            "source_temporary_id": source_temporary_id,
            "gate_reason_code": getattr(gate, "reason_code", None),
            "gate_justification": getattr(gate, "justification", None),
        },
    )

    import logging

    logger = logging.getLogger(__name__)
    logger.info(
        "execution_plan.built",
        extra={
            "plan_id": plan.plan_id,
            "action": plan.action.value,
            "mapping_decision": plan.mapping_decision,
            "entity_operation_count": len(plan.entity_operations),
            "relationship_operation_count": len(plan.relationship_operations),
            "unresolved_reference_operation_count": len(
                plan.unresolved_reference_operations
            ),
            "context_resolution_count": len(plan.context_resolutions),
        },
    )

    return plan

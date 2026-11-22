"""Contract tests for post-pipeline HITL review endpoints."""

from fastapi import FastAPI
import pytest

from kbfabric_ingest.ingest.execution_plan import ExecutionAction, ExecutionPlan
from kbfabric_ingest.ingest.hitl_api import (
    HITLDecisionRequest,
    HITLEndpointChoice,
    ReviewAction,
    _build_reviewed_plan,
    hitl_router,
)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-1",
        action=ExecutionAction.QUEUE_FOR_REVIEW,
        mapping_decision="REQUIRES_REVIEW",
        justification="candidate ambiguity",
        execution_metadata={"source_temporary_id": "temp-1"},
    )


def test_openapi_exposes_scoped_hitl_requests_responses_and_action_docs():
    """Objective: verify the published API schema documents scope, payloads, and write behavior.

    This matters because Ingestion Studio and the downstream HITL UI consume
    these contracts independently. Invariant: OpenAPI exposes batch/domain/
    product-line scope, typed response models, and the synchronous Neo4j write lifecycle plus retry contract.
    """
    app = FastAPI()
    app.include_router(hitl_router)
    schema = app.openapi()

    list_operation = schema["paths"]["/hitl/reviews"]["get"]
    assert list_operation["summary"] == "List HITL decisions for one ingestion scope"
    assert {p["name"] for p in list_operation["parameters"]} >= {
        "ingestion_batch_id", "domain_id", "product_line_id"
    }
    decision_operation = schema["paths"]["/hitl/reviews/{review_id}/decision"]["post"]
    assert "execute synchronously in Neo4j" in decision_operation["description"]
    assert "/hitl/reviews/{review_id}/execute" in schema["paths"]
    assert "HITLDecisionRequest" in schema["components"]["schemas"]
    assert "HITLDecisionResponse" in schema["components"]["schemas"]
    assert "HITLExecutionResponse" in schema["components"]["schemas"]


def test_node_create_new_builds_plan_from_stored_extraction():
    """Objective: verify Create New uses the persisted extraction contract.

    It matters because reviewer approval must not invent node fields or rely on
    the original source document being reprocessed. Invariant: the resulting
    plan carries the original node type, name, properties, and temporary ID.
    """
    plan = _plan()
    item = {
        "queue_type": "NODE_ADMISSION",
        "execution_plan_json": plan.model_dump(mode="json"),
        "mapping_context_json": {
            "incoming_entity": {
                "node_type": "Process", "name": "Underwriting",
                "temporary_id": "temp-1", "properties": {"description": "Credit assessment"},
            }
        },
    }

    result = _build_reviewed_plan(
        item, HITLDecisionRequest(action=ReviewAction.APPROVE_AS_NEW)
    )

    assert result is not None
    assert result.entity_operations[0].operation == "CREATE"
    assert result.entity_operations[0].entity_type == "Process"
    assert result.entity_operations[0].name == "Underwriting"
    assert result.entity_operations[0].properties == {"description": "Credit assessment"}
    assert result.entity_operations[0].temporary_id == "temp-1"


def test_node_reuse_accepts_only_persisted_candidate_of_expected_type():
    """Objective: verify HITL reuse cannot select an invented or wrong-type node.

    The UI's displayed node alternatives are authoritative for correction input;
    this protects the frozen ontology endpoint-type invariant at the API boundary.
    """
    item = {
        "queue_type": "NODE_ADMISSION",
        "execution_plan_json": _plan().model_dump(mode="json"),
        "mapping_context_json": {
            "incoming_entity": {"node_type": "Process", "name": "Underwriting"},
            "candidate_scores": [{"candidate": {"entity_id": "node-1", "kg_node_type": "Process", "name": "Underwriting Process"}}],
        },
    }
    request = HITLDecisionRequest(action=ReviewAction.APPROVE_REUSE, selected_entity_id="node-1")

    result = _build_reviewed_plan(item, request)
    assert result is not None
    assert result.entity_operations[0].operation == "REUSE"
    assert result.entity_operations[0].entity_id == "node-1"

    with pytest.raises(ValueError, match="persisted candidate"):
        _build_reviewed_plan(
            item,
            HITLDecisionRequest(action=ReviewAction.APPROVE_REUSE, selected_entity_id="invented-node"),
        )


def test_node_reuse_with_alias_is_explicit_and_carries_customer_domain_provenance():
    """Objective: verify a reviewer can explicitly approve a customer-domain alias.

    This matters because confirmed customer terminology should improve future
    matching without silently renaming the canonical KG node. Invariant: only
    a persisted same-type candidate can be reused; the extracted alias and its
    source/run scope are preserved in the reviewed plan for the write executor.
    """
    item = {
        "review_id": "review-1",
        "ingestion_batch_id": "batch-1",
        "queue_type": "NODE_ADMISSION",
        "execution_plan_json": _plan().model_dump(mode="json"),
        "ontology_context_json": {
            "customer_id": "customer-1", "tenant_id": "tenant-1",
            "domain_id": "consumer_retail", "product_line_id": "pl-cards",
        },
        "document_context_json": {"document_id": "doc-1"},
        "source_context_json": {"block_id": "block-1"},
        "provenance_json": {"source_document_id": "doc-1", "source_block_id": "block-1"},
        "mapping_context_json": {
            "incoming_entity": {"node_type": "Process", "name": "Short-Term Credit"},
            "candidate_scores": [{"candidate": {
                "entity_id": "node-1", "kg_node_type": "Process", "name": "Credit Underwriting",
            }}],
        },
    }

    result = _build_reviewed_plan(
        item,
        HITLDecisionRequest(
            action=ReviewAction.APPROVE_REUSE_WITH_ALIAS,
            selected_entity_id="node-1",
        ),
    )

    assert result is not None
    operation = result.entity_operations[0]
    assert operation.operation == "REUSE"
    assert operation.properties == {
        "aliases": ["Short-Term Credit"], "alias_scope": "CUSTOMER_DOMAIN",
    }
    assert result.execution_metadata["approved_alias_provenance"] == {
        "alias": "Short-Term Credit", "customer_id": "customer-1",
        "tenant_id": "tenant-1", "domain_id": "consumer_retail",
        "product_line_id": "pl-cards", "ingestion_batch_id": "batch-1",
        "source_document_id": "doc-1", "source_block_id": "block-1",
        "review_id": "review-1",
    }


def test_alias_approval_rejects_same_as_canonical_name():
    """Objective: prevent an identical canonical name from being stored as its own alias.

    This keeps the customer taxonomy useful for future matching. Invariant: an
    alias approval only stores a distinct alternate name.
    """
    item = {
        "queue_type": "NODE_ADMISSION",
        "execution_plan_json": _plan().model_dump(mode="json"),
        "mapping_context_json": {
            "incoming_entity": {"node_type": "Process", "name": "Underwriting"},
            "candidate_scores": [{"candidate": {
                "entity_id": "node-1", "kg_node_type": "Process", "name": "Underwriting",
            }}],
        },
    }
    with pytest.raises(ValueError, match="distinct from the selected node name"):
        _build_reviewed_plan(
            item,
            HITLDecisionRequest(
                action=ReviewAction.APPROVE_REUSE_WITH_ALIAS,
                selected_entity_id="node-1",
            ),
        )


def test_relationship_endpoint_selection_must_match_stored_candidate_and_type():
    """Objective: verify corrected triplet endpoints come only from ranked, typed candidates.

    This matters because users can choose among ranked alternatives but must not
    create an arbitrary edge endpoint. Invariant: selected ID and required node
    type both match the persisted context-resolution candidate set.
    """
    plan = _plan()
    plan.context_resolutions = [{
        "direction": "outgoing", "rel_type": "PERFORMS", "source_entity_id": "process-1",
        "target_type": "SubFunction", "target_value": "Short-Term Credit",
        "decision": "RELATIONSHIP_REVIEW_REQUIRED", "candidates": [
            {"entity_id": "sub-1", "node_type": "SubFunction", "name": "Short-Term Credit", "fusion_score": 0.81},
        ],
    }]
    item = {"queue_type": "CONTEXT_RESOLUTION", "execution_plan_json": plan.model_dump(mode="json")}
    choice = HITLEndpointChoice(
        rel_type="PERFORMS", direction="outgoing", target_type="SubFunction",
        target_value="Short-Term Credit", entity_id="sub-1",
    )

    result = _build_reviewed_plan(
        item,
        HITLDecisionRequest(action=ReviewAction.APPROVE_REUSE, endpoint_choices=[choice]),
    )
    assert result is not None
    assert result.context_resolutions[0]["selected_entity_id"] == "sub-1"
    assert result.relationship_operations[0].target_entity_id == "sub-1"

    with pytest.raises(ValueError, match="ontology-type-compatible"):
        _build_reviewed_plan(
            item,
            HITLDecisionRequest(
                action=ReviewAction.APPROVE_REUSE,
                endpoint_choices=[choice.model_copy(update={"entity_id": "invented-sub"})],
            ),
        )

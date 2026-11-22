"""Post-pipeline HITL review contracts for Ingestion Studio clients."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from kbfabric_ingest.ingest.execution_plan import ExecutionAction, ExecutionPlan
from kbfabric_ingest.ingest.repository.hitl_review_repository import HITLReviewRepository
from kbfabric_ingest.ingest.hitl_write_executor import HITLPlanWriter
from kbfabric_ingest.security.auth import require_user_id


hitl_router = APIRouter(prefix="/hitl", tags=["HITL Review"])


class ReviewStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReviewQueueType(str, Enum):
    NODE_ADMISSION = "NODE_ADMISSION"
    CONTEXT_RESOLUTION = "CONTEXT_RESOLUTION"
    UNRESOLVED_TARGET = "UNRESOLVED_TARGET"


class ReviewAction(str, Enum):
    """Supported reviewer outcomes for node and relationship queue items."""

    APPROVE_REUSE = "APPROVE_REUSE"
    APPROVE_REUSE_WITH_ALIAS = "APPROVE_REUSE_WITH_ALIAS"
    APPROVE_AS_NEW = "APPROVE_AS_NEW"
    REJECT = "REJECT"
    LEAVE_UNRESOLVED = "LEAVE_UNRESOLVED"


class HITLReviewItem(BaseModel):
    """One review task and its source, decision, candidate, and run evidence."""

    model_config = ConfigDict(extra="ignore")

    review_id: str
    ingestion_batch_id: str
    plan_id: str
    queue_type: ReviewQueueType
    status: ReviewStatus
    priority: str
    reason_code: str
    admission_behavior: str
    short_summary: str | None = None
    decision_question: str | None = None
    source_temporary_id: str | None = None
    source_context_json: dict[str, Any] = Field(default_factory=dict)
    document_context_json: dict[str, Any] = Field(default_factory=dict)
    ontology_context_json: dict[str, Any] = Field(default_factory=dict)
    mapping_context_json: dict[str, Any] = Field(default_factory=dict)
    context_resolution_json: list[dict[str, Any]] = Field(default_factory=list)
    relationship_evidence_json: list[dict[str, Any]] = Field(default_factory=list)
    provenance_json: dict[str, Any] = Field(default_factory=dict)
    review_payload_json: dict[str, Any] = Field(default_factory=dict)
    execution_plan_json: dict[str, Any]
    resolved_plan_json: dict[str, Any] | None = None
    reviewer_action: str | None = None
    reviewer_id: str | None = None
    reviewer_notes: str | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    write_status: str = "NOT_STARTED"
    write_result_json: dict[str, Any] = Field(default_factory=dict)
    write_error: str | None = None
    write_attempts: int = 0


class HITLReviewListResponse(BaseModel):
    """Paginated review queue for one ingestion batch and ontology scope."""

    items: list[HITLReviewItem]
    total: int
    limit: int
    offset: int
    scope: dict[str, str]


class HITLEndpointChoice(BaseModel):
    """Select one already-ranked, ontology-type-compatible relationship target."""

    rel_type: str = Field(description="Canonical relationship type from the review item.")
    direction: Literal["incoming", "outgoing"] = Field(description="Endpoint direction relative to the extracted node.")
    target_type: str = Field(description="Required canonical node type.")
    target_value: str = Field(description="Extracted endpoint value being resolved.")
    entity_id: str = Field(description="Candidate ID from the persisted ranked candidate list.")


class HITLDecisionRequest(BaseModel):
    """Reviewer action; selections must reference candidates persisted with the item."""

    action: ReviewAction = Field(
        description=(
            "Choose APPROVE_AS_NEW or APPROVE_REUSE for a node; use "
            "APPROVE_REUSE_WITH_ALIAS to add the extracted name to customer-domain "
            "taxonomy; use APPROVE_REUSE with endpoint_choices for a triplet; "
            "REJECT declines admission; LEAVE_UNRESOLVED leaves the item pending."
        )
    )
    notes: str | None = Field(default=None, max_length=4000)
    selected_entity_id: str | None = Field(
        default=None,
        description=(
            "Existing persisted candidate ID for node reuse actions, including "
            "APPROVE_REUSE_WITH_ALIAS. Alias approval explicitly adds the extracted "
            "name as a customer-domain alias in the approved plan."
        ),
    )
    endpoint_choices: list[HITLEndpointChoice] = Field(
        default_factory=list,
        description="Relationship target selections, each constrained to the item's persisted ranked candidates.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"action": "APPROVE_AS_NEW", "notes": "Evidence supports a distinct process."},
                {"action": "APPROVE_REUSE_WITH_ALIAS", "selected_entity_id": "existing-node-id", "notes": "Approve the extracted name as a customer-domain alias."},
                {"action": "LEAVE_UNRESOLVED", "notes": "Wait for another source document."},
            ]
        }
    )


class HITLDecisionResponse(BaseModel):
    """Acknowledges persisted reviewer action and the synchronous KG write outcome."""

    review_id: str
    status: ReviewStatus
    reviewer_action: ReviewAction
    reviewer_id: str
    reviewer_notes: str | None = None
    updated_at: datetime
    resolved_at: datetime | None = None
    event_id: str
    execution_status: str = Field(description="Neo4j write lifecycle: PENDING, IN_PROGRESS, SUCCEEDED, FAILED, or NOT_REQUIRED.")
    write_result: dict[str, int] = Field(default_factory=dict)
    write_error: str | None = None
    write_attempts: int = 0


class HITLExecutionResponse(BaseModel):
    """Write outcome for an approved plan, including retry attempts."""

    review_id: str
    execution_status: str
    write_result: dict[str, int] = Field(default_factory=dict)
    write_error: str | None = None
    write_attempts: int = 0


class HITLReviewEvent(BaseModel):
    """Append-only audit event for a reviewer action."""

    event_id: str
    review_id: str
    event_type: str
    action: ReviewAction
    status_before: ReviewStatus
    status_after: ReviewStatus
    reviewer_id: str
    reviewer_notes: str | None = None
    resolved_plan_json: dict[str, Any] | None = None
    event_details_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class HITLReviewHistoryResponse(BaseModel):
    review_id: str
    events: list[HITLReviewEvent]


def _build_reviewed_plan(
    item: dict[str, Any], request: HITLDecisionRequest
) -> ExecutionPlan | None:
    """Build a corrected plan only from candidates stored with this review item."""
    mapping_context = item.get("mapping_context_json") or {}
    candidates = mapping_context.get("candidate_scores") or []
    queue_type = item.get("queue_type")

    if queue_type == ReviewQueueType.NODE_ADMISSION.value:
        if request.action in {
            ReviewAction.APPROVE_REUSE,
            ReviewAction.APPROVE_REUSE_WITH_ALIAS,
        }:
            if not request.selected_entity_id:
                raise ValueError(f"{request.action.value} requires a persisted entity candidate selection")
            if request.endpoint_choices:
                raise ValueError("Node admission actions cannot include relationship endpoint choices")
            candidate_match = next(
                (candidate for candidate in candidates
                 if str(candidate.get("candidate", {}).get("entity_id") or candidate.get("entity_id")) == request.selected_entity_id),
                None,
            )
            incoming = mapping_context.get("incoming_entity") or {}
            expected_type = incoming.get("node_type")
            persisted_candidate = (candidate_match or {}).get("candidate", candidate_match or {})
            if candidate_match is None or persisted_candidate.get("kg_node_type") != expected_type:
                raise ValueError("Selected node is not an eligible persisted candidate of the required type")
            plan = ExecutionPlan.model_validate(item["execution_plan_json"])
            from kbfabric_ingest.ingest.execution_plan import EntityOperation
            reuse_properties: dict[str, Any] = {}
            if request.action == ReviewAction.APPROVE_REUSE_WITH_ALIAS:
                extracted_name = str(incoming.get("name") or "").strip()
                canonical_name = str(persisted_candidate.get("name") or "").strip()
                if not extracted_name or extracted_name.casefold() == canonical_name.casefold():
                    raise ValueError("Alias approval requires an extracted name distinct from the selected node name")
                reuse_properties = {
                    "aliases": [extracted_name],
                    "alias_scope": "CUSTOMER_DOMAIN",
                }
                plan.execution_metadata["approved_alias_provenance"] = {
                    "alias": extracted_name,
                    "customer_id": (item.get("ontology_context_json") or {}).get("customer_id"),
                    "tenant_id": (item.get("ontology_context_json") or {}).get("tenant_id"),
                    "domain_id": (item.get("ontology_context_json") or {}).get("domain_id"),
                    "product_line_id": (item.get("ontology_context_json") or {}).get("product_line_id"),
                    "ingestion_batch_id": item.get("ingestion_batch_id"),
                    "source_document_id": (item.get("document_context_json") or {}).get("document_id"),
                    "source_block_id": (item.get("source_context_json") or {}).get("block_id"),
                    "review_id": item.get("review_id"),
                }
            plan.entity_operations = [EntityOperation(
                operation="REUSE",
                entity_type=expected_type,
                entity_id=request.selected_entity_id,
                temporary_id=plan.execution_metadata.get("source_temporary_id"),
                name=str(incoming.get("name") or persisted_candidate.get("name") or ""),
                properties=reuse_properties,
            )]
            plan.selected_entity_id = request.selected_entity_id
            plan.mapping_decision = "REUSE_EXISTING"
            plan.action = ExecutionAction.WRITE_NOW
            return plan
        if request.action == ReviewAction.APPROVE_AS_NEW:
            if request.selected_entity_id or request.endpoint_choices:
                raise ValueError("APPROVE_AS_NEW cannot include existing candidate selections")
            plan = ExecutionPlan.model_validate(item["execution_plan_json"])
            incoming = mapping_context.get("incoming_entity") or {}
            if not incoming.get("node_type") or not incoming.get("name"):
                raise ValueError("Review item lacks the extracted entity contract needed for Create New")
            from kbfabric_ingest.ingest.execution_plan import EntityOperation
            plan.entity_operations = [EntityOperation(
                operation="CREATE",
                entity_type=incoming["node_type"],
                temporary_id=plan.execution_metadata.get("source_temporary_id"),
                name=incoming["name"],
                properties=incoming.get("properties") or {},
            )]
            plan.selected_entity_id = None
            plan.mapping_decision = "CREATE_NEW"
            plan.action = ExecutionAction.WRITE_NOW
            return plan
        if request.selected_entity_id or request.endpoint_choices:
            raise ValueError("This node action does not accept candidate selections")
        return None

    if request.action not in {ReviewAction.APPROVE_REUSE, ReviewAction.REJECT, ReviewAction.LEAVE_UNRESOLVED}:
        raise ValueError("This action is not valid for a relationship review item")
    if request.selected_entity_id:
        raise ValueError("Relationship review accepts endpoint choices, not node selections")
    if request.action == ReviewAction.APPROVE_REUSE and not request.endpoint_choices:
        raise ValueError("APPROVE_REUSE on a relationship review requires at least one endpoint choice")
    if request.endpoint_choices:
        if request.action != ReviewAction.APPROVE_REUSE:
            raise ValueError("Endpoint choices must be submitted with APPROVE_REUSE")
        plan = ExecutionPlan.model_validate(item["execution_plan_json"])
        selected_keys: set[tuple[str, str, str, str]] = set()
        for choice in request.endpoint_choices:
            key = (choice.rel_type, choice.direction.casefold(), choice.target_type, choice.target_value.casefold())
            if key in selected_keys:
                raise ValueError("Duplicate endpoint choice for the same relationship endpoint")
            selected_keys.add(key)
            resolution_index = next(
                (index for index, resolution in enumerate(plan.context_resolutions)
                 if resolution.get("rel_type") == choice.rel_type
                 and resolution.get("direction", "").casefold() == choice.direction.casefold()
                 and resolution.get("target_type") == choice.target_type
                 and resolution.get("target_value", "").casefold() == choice.target_value.casefold()),
                None,
            )
            if resolution_index is None:
                raise ValueError("Endpoint choice does not identify a relationship in this review item")
            resolution = plan.context_resolutions[resolution_index]
            candidate = next(
                (candidate for candidate in resolution.get("candidates", [])
                 if candidate.get("entity_id") == choice.entity_id
                 and candidate.get("node_type") == choice.target_type),
                None,
            )
            if candidate is None:
                raise ValueError("Selected endpoint is not an ontology-type-compatible persisted candidate")
            resolution.update(
                decision="RESOLVED",
                selected_entity_id=choice.entity_id,
                confidence=float(candidate.get("cross_encoder_score") or candidate.get("fusion_score") or 0.0),
                reason_code="reviewer_selected_ranked_candidate",
                justification="Reviewer selected an existing ontology-valid ranked endpoint candidate.",
            )
            from kbfabric_ingest.ingest.execution_plan import RelationshipOperation
            relationship_op = RelationshipOperation(
                operation="CREATE",
                rel_type=choice.rel_type,
                source_entity_id=resolution.get("source_entity_id") or plan.selected_entity_id,
                source_temporary_id=plan.execution_metadata.get("source_temporary_id"),
                target_entity_id=choice.entity_id,
                target_type=choice.target_type,
                target_value=choice.target_value,
                properties={"confidence": resolution["confidence"], "reason_code": resolution["reason_code"]},
            )
            plan.relationship_operations = [
                operation for operation in plan.relationship_operations
                if not (operation.rel_type == choice.rel_type
                        and operation.target_type == choice.target_type
                        and operation.target_value.casefold() == choice.target_value.casefold())
            ] + [relationship_op]
        plan.action = ExecutionAction.WRITE_NOW
        return plan

    return None


def _scope_parameters(
    ingestion_batch_id: str,
    domain_id: str,
    product_line_id: str,
) -> dict[str, str]:
    return {
        "ingestion_batch_id": ingestion_batch_id,
        "domain_id": domain_id,
        "product_line_id": product_line_id,
    }


_SCOPE_DOC = """
Every request must include the ingestion batch ID, domain ID, and product-line ID.
The service returns an item only when all three match the persisted frozen run scope.
`Authorization: Bearer <token>` is required; the authenticated user ID is recorded
for reviewer actions. Approved plans execute synchronously and expose persisted write
status, counts, and errors. User-to-project entitlement lookup remains a separate
integration.
"""


@hitl_router.get(
    "/reviews",
    response_model=HITLReviewListResponse,
    summary="List HITL decisions for one ingestion scope",
    description=(
        "Post-pipeline review queue. Filters by the required batch, domain, and "
        "product-line scope, then optionally by status and decision queue type. "
        + _SCOPE_DOC
    ),
    responses={401: {"description": "Authorization header or user identity is missing."}},
)
def list_hitl_reviews(
    ingestion_batch_id: str = Query(..., min_length=1, description="Ingestion job/batch ID."),
    domain_id: str = Query(..., min_length=1, description="Frozen domain ID for the run."),
    product_line_id: str = Query(..., min_length=1, description="Frozen product-line ID for the run."),
    status: ReviewStatus | None = Query(None, description="Filter by pending or terminal review status."),
    queue_type: ReviewQueueType | None = Query(None, description="Filter node admission, context resolution, or unresolved target items."),
    limit: int = Query(50, ge=1, le=200, description="Maximum items to return."),
    offset: int = Query(0, ge=0, description="Number of matching items to skip."),
    reviewer_id: str = Depends(require_user_id),
) -> HITLReviewListResponse:
    del reviewer_id  # Authentication is required; list operations do not assign ownership.
    try:
        items, total = HITLReviewRepository().list_scoped(
            **_scope_parameters(ingestion_batch_id, domain_id, product_line_id),
            status=status.value if status else None,
            queue_type=queue_type.value if queue_type else None,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="HITL review store is unavailable") from exc
    return HITLReviewListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        scope={"ingestion_batch_id": ingestion_batch_id, "domain_id": domain_id, "product_line_id": product_line_id},
    )


@hitl_router.get(
    "/reviews/{review_id}",
    response_model=HITLReviewItem,
    summary="Get one HITL item with full evidence",
    description=(
        "Returns masked source-block evidence, document identity, ontology snapshot, "
        "mapping scores, ranked relationship candidates, execution plan, and run provenance. "
        + _SCOPE_DOC
    ),
    responses={401: {"description": "Authorization required."}, 404: {"description": "No item exists in the supplied scope."}},
)
def get_hitl_review(
    review_id: str,
    ingestion_batch_id: str = Query(..., min_length=1),
    domain_id: str = Query(..., min_length=1),
    product_line_id: str = Query(..., min_length=1),
    reviewer_id: str = Depends(require_user_id),
) -> HITLReviewItem:
    del reviewer_id
    item = HITLReviewRepository().get_scoped(
        review_id=review_id,
        **_scope_parameters(ingestion_batch_id, domain_id, product_line_id),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="HITL review item not found in supplied scope")
    return HITLReviewItem.model_validate(item)


@hitl_router.get(
    "/reviews/{review_id}/history",
    response_model=HITLReviewHistoryResponse,
    summary="Read reviewer action history",
    description="Returns append-only actions for a review item within its required run scope. " + _SCOPE_DOC,
    responses={401: {"description": "Authorization required."}, 404: {"description": "No item exists in the supplied scope."}},
)
def get_hitl_review_history(
    review_id: str,
    ingestion_batch_id: str = Query(..., min_length=1),
    domain_id: str = Query(..., min_length=1),
    product_line_id: str = Query(..., min_length=1),
    reviewer_id: str = Depends(require_user_id),
) -> HITLReviewHistoryResponse:
    del reviewer_id
    repo = HITLReviewRepository()
    scope = _scope_parameters(ingestion_batch_id, domain_id, product_line_id)
    if repo.get_scoped(review_id=review_id, **scope) is None:
        raise HTTPException(status_code=404, detail="HITL review item not found in supplied scope")
    events = repo.list_history(review_id=review_id, **scope)
    return HITLReviewHistoryResponse(review_id=review_id, events=events)


@hitl_router.post(
    "/reviews/{review_id}/decision",
    response_model=HITLDecisionResponse,
    summary="Record a reviewer decision",
    description=(
        "Persists the reviewer action and an append-only audit event atomically. "
        "Approved plans execute synchronously in Neo4j after the review decision is committed. "
        "The response reports write status and counts; failures are persisted and can be retried. "
        "Node reuse requires a persisted candidate ID; relationship approval requires ranked endpoint selections. "
        "LEAVE_UNRESOLVED records a non-terminal action and leaves the item pending. "
        + _SCOPE_DOC
    ),
    responses={
        400: {"description": "Action or resolved-plan contract is invalid."},
        401: {"description": "Authorization required."},
        404: {"description": "No pending item exists in the supplied scope."},
    },
)
def record_hitl_decision(
    review_id: str,
    request: HITLDecisionRequest,
    ingestion_batch_id: str = Query(..., min_length=1),
    domain_id: str = Query(..., min_length=1),
    product_line_id: str = Query(..., min_length=1),
    reviewer_id: str = Depends(require_user_id),
) -> HITLDecisionResponse:
    repo = HITLReviewRepository()
    scope = _scope_parameters(ingestion_batch_id, domain_id, product_line_id)
    item = repo.get_scoped(review_id=review_id, **scope)
    if item is None:
        raise HTTPException(status_code=404, detail="HITL review item not found in supplied scope")
    try:
        resolved_plan = _build_reviewed_plan(item, request)
        result = repo.record_decision(
            review_id=review_id,
            **scope,
            reviewer_action=request.action.value,
            reviewer_id=reviewer_id,
            reviewer_notes=request.notes,
            resolved_plan=resolved_plan,
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Could not persist HITL decision") from exc
    if resolved_plan is not None:
        result.update(_execute_and_record(repo, item, resolved_plan, scope))
    return HITLDecisionResponse.model_validate(result)


def _execute_and_record(
    repo: HITLReviewRepository,
    item: dict[str, Any],
    plan: ExecutionPlan,
    scope: dict[str, str],
) -> dict[str, Any]:
    """Claim, execute, and persist one idempotent reviewed-plan write."""
    if not repo.claim_write(review_id=item["review_id"], **scope):
        current = repo.get_scoped(review_id=item["review_id"], **scope) or {}
        return {
            "execution_status": current.get("write_status", "IN_PROGRESS"),
            "write_result": current.get("write_result_json") or {},
            "write_error": current.get("write_error"),
            "write_attempts": current.get("write_attempts", 0),
        }
    writer = None
    try:
        writer = HITLPlanWriter()
        write_result = writer.execute(plan, item)
        repo.complete_write(review_id=item["review_id"], **scope, result=write_result)
        status, error = "SUCCEEDED", None
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:4000]
        repo.complete_write(review_id=item["review_id"], **scope, error=error)
        write_result, status = {}, "FAILED"
    finally:
        if writer is not None:
            writer.close()
    current = repo.get_scoped(review_id=item["review_id"], **scope) or {}
    return {
        "execution_status": status,
        "write_result": write_result,
        "write_error": error,
        "write_attempts": current.get("write_attempts", 0),
    }


@hitl_router.post(
    "/reviews/{review_id}/execute",
    response_model=HITLExecutionResponse,
    summary="Retry a failed HITL-approved KG write",
    description=(
        "Retries a persisted approved plan only when its last write failed or is still pending. "
        "Neo4j operations use deterministic identities and MERGE semantics. " + _SCOPE_DOC
    ),
    responses={400: {"description": "The item is not approved or is not retryable."}, 401: {"description": "Authorization required."}, 404: {"description": "No item exists in the supplied scope."}},
)
def retry_hitl_write(
    review_id: str,
    ingestion_batch_id: str = Query(..., min_length=1),
    domain_id: str = Query(..., min_length=1),
    product_line_id: str = Query(..., min_length=1),
    reviewer_id: str = Depends(require_user_id),
) -> HITLExecutionResponse:
    del reviewer_id
    repo = HITLReviewRepository()
    scope = _scope_parameters(ingestion_batch_id, domain_id, product_line_id)
    item = repo.get_scoped(review_id=review_id, **scope)
    if item is None:
        raise HTTPException(status_code=404, detail="HITL review item not found in supplied scope")
    if item.get("status") != "APPROVED" or not item.get("resolved_plan_json"):
        raise HTTPException(status_code=400, detail="Only approved items with a persisted execution plan can be executed")
    if item.get("write_status") not in {"PENDING", "FAILED", "IN_PROGRESS"}:
        raise HTTPException(status_code=400, detail=f"Write is not retryable from state {item.get('write_status')}")
    plan = ExecutionPlan.model_validate(item["resolved_plan_json"])
    result = _execute_and_record(repo, item, plan, scope)
    return HITLExecutionResponse(review_id=review_id, **result)

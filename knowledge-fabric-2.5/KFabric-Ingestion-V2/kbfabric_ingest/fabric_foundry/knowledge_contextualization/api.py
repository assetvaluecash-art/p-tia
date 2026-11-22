from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
from kbfabric_ingest.fabric_foundry.industry.ontology_repository import IndustryOntologyRepository
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.contract import CustomerOntologyContextualizationRequest
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRecord,
    CustomerOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.service import KnowledgeContextualizationError, KnowledgeContextualizationService
from kbfabric_ingest.security.auth import require_user_id
from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


customer_ontology_router = APIRouter(
    prefix="/customers",
    tags=["Customer Knowledge Contextualization"],
)

customer_ontology_repository = CustomerOntologyRepository()
customer_ontology_service = KnowledgeContextualizationService(
    customer_repository=CustomerRepository(),
    industry_ontology_repository=IndustryOntologyRepository(),
    customer_ontology_repository=customer_ontology_repository,
)


class CustomerOntologyResponse(BaseModel):
    """
    Current Customer Ontology snapshot.

    UI usage:
    - Call this when opening the Customer Context Overlay.
    - The returned customer_ontology_version becomes the base version
      for a subsequent contextualization save.
    """

    customer_ontology_id: UUID = Field(
        ...,
        description="Unique identifier of this Customer Ontology snapshot.",
    )
    customer_id: str = Field(
        ...,
        description="External customer identifier.",
    )
    tenant_id: str = Field(
        ...,
        description="Internal tenant isolation identifier.",
    )
    ontology_id: str = Field(
        ...,
        description="Canonical ontology identifier used as the source.",
    )
    ontology_version: str = Field(
        ...,
        description="Canonical ontology version used as the source.",
    )
    customer_ontology_version: int = Field(
        ...,
        description="Immutable customer-specific ontology version.",
    )
    status: str = Field(
        ...,
        description="Customer Ontology status. The current version is ACTIVE.",
    )
    definition: dict = Field(
        ...,
        description=(
            "Customer Ontology definition containing the canonical ontology "
            "plus customer-specific contextualization."
        ),
    )
    change_type: str = Field(
        ...,
        description="Type of change represented by this version.",
    )
    change_reason: str | None = Field(
        None,
        description="Reason supplied when this version was created.",
    )
    change_summary: dict = Field(
        ...,
        description="Structured summary of changes represented by this version.",
    )
    parent_customer_ontology_id: UUID | None = Field(
        None,
        description="Previous Customer Ontology snapshot from which this version was created.",
    )
    created_by: str = Field(
        ...,
        description="Actor that created this Customer Ontology version.",
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when this Customer Ontology version was created.",
    )
    confirmed_by: str | None = Field(
        None,
        description="Actor that confirmed this version, when applicable.",
    )
    confirmed_at: datetime | None = Field(
        None,
        description="Timestamp when this version was confirmed, when applicable.",
    )
    updated_at: datetime = Field(
        ...,
        description="Timestamp when this record was last updated.",
    )


class ContextualizationDomainSummary(BaseModel):
    """Domain entry shown on the Customer Knowledge Context landing page."""

    id: str = Field(..., description="Stable canonical domain identifier.")
    name: str = Field(..., description="Human-readable domain name.")
    description: str | None = Field(None, description="Canonical domain description.")
    configured: bool = Field(..., description="Whether this domain has persisted customer contextualization.")


class ContextualizationSubFunctionItem(BaseModel):
    """Customer contextualization state for a canonical or persisted SubFunction/C."""

    id: str = Field(..., description="Stable canonical or customer-added identifier.")
    name: str = Field(..., description="SubFunction/C name.")
    applicable: bool = Field(..., description="Whether this SubFunction/C is applicable within this domain. When configured=true, true means the customer explicitly selected it and false means the customer explicitly marked it not applicable. When configured=false, false is only the default value and does not represent a saved decision.")
    alias: str | None = Field(None, description="Customer-provided alias, when configured.")
    configured: bool = Field(..., description="Whether the customer has explicitly saved an applicability decision for this item in this domain. false means the item is not yet decided and may be presented as Skip for now.")
    locked: bool = Field(..., description="Whether the UI should treat this saved decision as read-only in the current customer ontology version. A locked item is persisted; a future ontology version may contain a new decision.")


class ContextualizationFunctionItem(BaseModel):
    """Customer contextualization state for a canonical or persisted Function/BCG."""

    id: str = Field(..., description="Stable canonical or customer-added identifier.")
    name: str = Field(..., description="Function/BCG name.")
    description: str | None = Field(None, description="Canonical or customer-added description.")
    applicable: bool = Field(..., description="Whether this Function/BCG is applicable within this domain. When configured=true, true means the customer explicitly selected it and false means the customer explicitly marked it not applicable. When configured=false, false is only the default value and does not represent a saved decision.")
    alias: str | None = Field(None, description="Customer-provided alias, when configured.")
    configured: bool = Field(..., description="Whether the customer has explicitly saved an applicability decision for this item in this domain. false means the item is not yet decided and may be presented as Skip for now.")
    locked: bool = Field(..., description="Whether the UI should treat this saved decision as read-only in the current customer ontology version. A locked item is persisted; a future ontology version may contain a new decision.")
    sub_functions: list[ContextualizationSubFunctionItem] = Field(default_factory=list, description="Nested SubFunction/C items.")


class ContextualizationProductLineItem(BaseModel):
    """Customer contextualization state for a canonical or persisted Product Line."""

    id: str = Field(..., description="Stable canonical or customer-added identifier.")
    name: str = Field(..., description="Product Line name.")
    description: str | None = Field(None, description="Canonical or customer-added description.")
    applicable: bool = Field(..., description="Whether this Product Line is applicable within this domain. When configured=true, true means the customer explicitly selected it and false means the customer explicitly marked it not applicable. When configured=false, false is only the default value and does not represent a saved decision.")
    alias: str | None = Field(None, description="Customer-provided alias, when configured.")
    configured: bool = Field(..., description="Whether the customer has explicitly saved an applicability decision for this item in this domain. false means the item is not yet decided and may be presented as Skip for now.")
    locked: bool = Field(..., description="Whether the UI should treat this saved decision as read-only in the current customer ontology version. A locked item is persisted; a future ontology version may contain a new decision.")
    functions: list[ContextualizationFunctionItem] = Field(default_factory=list, description="Nested Function/BCG items.")


class ContextualizationProductLineAddition(BaseModel):
    """Persisted customer-added Product Line."""

    id: str = Field(..., description="Stable customer-added Product Line identifier.")
    name: str = Field(..., description="Customer-added Product Line name.")
    description: str | None = Field(None, description="Customer-added Product Line description.")
    aliases: list[str] = Field(default_factory=list, description="Customer-provided aliases.")
    applicable: bool = Field(..., description="Customer-added Product Lines are automatically applicable because adding the item is itself the applicability decision.")
    configured: bool = Field(True, description="Customer-added items returned here are persisted.")
    locked: bool = Field(True, description="Customer-added items are immutable after save.")


class ContextualizationFunctionAddition(BaseModel):
    """Persisted customer-added Function/BCG."""

    id: str = Field(..., description="Stable customer-added Function/BCG identifier.")
    parent_product_line_id: str = Field(..., description="Identifier of the Product Line containing this Function/BCG.")
    name: str = Field(..., description="Customer-added Function/BCG name.")
    description: str | None = Field(None, description="Customer-added Function/BCG description.")
    aliases: list[str] = Field(default_factory=list, description="Customer-provided aliases.")
    applicable: bool = Field(..., description="Customer-added Functions/BCGs are automatically applicable because adding the item is itself the applicability decision.")
    configured: bool = Field(True, description="Customer-added items returned here are persisted.")
    locked: bool = Field(True, description="Customer-added items are immutable after save.")


class ContextualizationSubFunctionAddition(BaseModel):
    """Persisted customer-added SubFunction/C."""

    id: str = Field(..., description="Stable customer-added SubFunction/C identifier.")
    parent_function_id: str = Field(..., description="Identifier of the Function/BCG containing this SubFunction/C.")
    name: str = Field(..., description="Customer-added SubFunction/C name.")
    description: str | None = Field(None, description="Customer-added SubFunction/C description.")
    aliases: list[str] = Field(default_factory=list, description="Customer-provided aliases.")
    applicable: bool = Field(..., description="Customer-added SubFunctions/C are automatically applicable because adding the item is itself the applicability decision.")
    configured: bool = Field(True, description="Customer-added items returned here are persisted.")
    locked: bool = Field(True, description="Customer-added items are immutable after save.")


class ContextualizationAdditions(BaseModel):
    """Persisted customer-created hierarchy additions for a domain."""

    product_lines: list[ContextualizationProductLineAddition] = Field(default_factory=list)
    functions: list[ContextualizationFunctionAddition] = Field(default_factory=list)
    sub_functions: list[ContextualizationSubFunctionAddition] = Field(default_factory=list)


class ContextualizationOperations(BaseModel):
    """Operations the UI may perform in the selected domain."""

    can_add_product_line: bool = Field(True, description="Whether a new Product Line may be added to this domain.")
    can_add_function: bool = Field(True, description="Whether a new Function/BCG may be added under an applicable Product Line.")
    can_add_sub_function: bool = Field(True, description="Whether a new SubFunction/C may be added under an applicable Function/BCG.")


class ContextualizationPersistedState(BaseModel):
    """Persisted customer state for the selected domain."""

    product_lines: list[ContextualizationProductLineItem] = Field(default_factory=list)
    additions: ContextualizationAdditions = Field(default_factory=ContextualizationAdditions)


class ContextualizationCanonicalDelta(BaseModel):
    """Remaining canonical items that have not yet been configured."""

    product_lines: list[ContextualizationProductLineItem] = Field(default_factory=list)


class ContextualizationDomainResponse(BaseModel):
    """Domain-scoped state used by the Customer Knowledge Context UI."""

    id: str = Field(..., description="Stable canonical domain identifier.")
    name: str = Field(..., description="Human-readable domain name.")
    description: str | None = Field(None, description="Canonical domain description.")


class CustomerContextualizationDomainResponse(BaseModel):
    """Complete domain-scoped contextualization state for the UI."""

    customer_id: str = Field(..., description="External customer identifier.")
    customer_name: str = Field(..., description="Customer display name.")
    industry_id: str = Field(..., description="Customer industry identifier.")
    industry_name: str = Field(..., description="Customer industry display name.")
    ontology_id: str = Field(..., description="Canonical ontology identifier allocated to the customer industry.")
    ontology_version: str = Field(..., description="Canonical ontology version allocated to the customer industry.")
    customer_ontology_version: int | None = Field(None, description="Current persisted Customer Ontology version, if contextualization has been saved.")
    domain: ContextualizationDomainResponse = Field(..., description="Selected canonical domain.")
    persisted: ContextualizationPersistedState = Field(..., description="Customer decisions already persisted for this domain. Persisted items include both explicitly applicable and explicitly not-applicable decisions; configured=true identifies these saved decisions.")
    canonical_delta: ContextualizationCanonicalDelta = Field(..., description="Canonical items for this domain that do not yet have a persisted customer decision. These are the items available for further contextualization. An item here must not be interpreted as not applicable.")
    operations: ContextualizationOperations = Field(..., description="Operations that remain available for this domain.")


class CustomerContextualizationPageResponse(BaseModel):
    """Customer-level contextualization landing-page response."""

    customer_id: str = Field(..., description="External customer identifier.")
    customer_name: str = Field(..., description="Customer display name.")
    industry_id: str = Field(..., description="Customer industry identifier.")
    industry_name: str = Field(..., description="Customer industry display name.")
    ontology_id: str = Field(..., description="Canonical ontology identifier allocated to the customer industry.")
    ontology_version: str = Field(..., description="Canonical ontology version allocated to the customer industry.")
    customer_ontology_version: int | None = Field(None, description="Current persisted Customer Ontology version, if contextualization has been saved.")
    domains: list[ContextualizationDomainSummary] = Field(default_factory=list, description="Canonical domains available for contextualization.")


class CustomerOntologyVersionSummary(BaseModel):
    """
    Version-history entry for the Customer Ontology.

    UI usage:
    - Render these entries in the Version History view.
    - Use customer_ontology_id to retrieve a specific version later.
    """

    customer_ontology_id: UUID = Field(
        ...,
        description="Unique identifier of this Customer Ontology version.",
    )
    customer_ontology_version: int = Field(
        ...,
        description="Immutable customer-specific ontology version number.",
    )
    status: str = Field(
        ...,
        description="Version status. Only the current version is ACTIVE.",
    )
    change_type: str = Field(
        ...,
        description="Type of change represented by this version.",
    )
    change_reason: str | None = Field(
        None,
        description="Reason supplied when this version was created.",
    )
    change_summary: dict = Field(
        ...,
        description="Structured summary of changes represented by this version.",
    )
    parent_customer_ontology_id: UUID | None = Field(
        None,
        description="Previous Customer Ontology version in the version chain.",
    )
    created_by: str = Field(
        ...,
        description="Actor that created this version.",
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when this version was created.",
    )
    confirmed_by: str | None = Field(
        None,
        description="Actor that confirmed this version, when applicable.",
    )
    confirmed_at: datetime | None = Field(
        None,
        description="Timestamp when this version was confirmed, when applicable.",
    )


class CustomerOntologyVersionDetailResponse(BaseModel):
    """Detailed Customer Ontology version information for Version History View Details."""
    customer_id: str = Field(..., description="External customer identifier.")
    customer_ontology_version: int = Field(..., description="Version being inspected.")
    status: str = Field(..., description="Lifecycle status of this Customer Ontology version.")
    change_type: str = Field(..., description="Business change category for this version.")
    change_reason: str | None = Field(None, description="Optional reason supplied when the version was created.")
    change_summary: dict[str, Any] = Field(..., description="Compact summary for the version detail header.")
    parent_customer_ontology_id: UUID | None = Field(None, description="Previous Customer Ontology version in the version chain.")
    created_by: str = Field(..., description="Actor that created this version.")
    created_at: datetime = Field(..., description="Timestamp when this version was created.")
    confirmed_by: str | None = Field(None, description="Actor that confirmed this version, when applicable.")
    confirmed_at: datetime | None = Field(None, description="Timestamp when this version was confirmed, when applicable.")
    details_available: bool = Field(..., description="Whether detailed submitted changes are available for this version.")
    changes: list[dict[str, Any]] = Field(default_factory=list, description="Detailed contextualization changes submitted when this version was created.")


class CustomerOntologyHistoryResponse(BaseModel):
    """
    Complete Customer Ontology version history.

    UI usage:
    - Call when opening Version History.
    - Versions are returned newest-first.
    """

    customer_id: str = Field(
        ...,
        description="External customer identifier.",
    )
    current_customer_ontology_version: int | None = Field(
        None,
        description="Currently ACTIVE Customer Ontology version, if one exists.",
    )
    versions: list[CustomerOntologyVersionSummary] = Field(
        ...,
        description="Customer Ontology versions ordered newest-first.",
    )


def _to_response(record: CustomerOntologyRecord) -> CustomerOntologyResponse:
    return CustomerOntologyResponse(
        customer_ontology_id=record.customer_ontology_id,
        customer_id=record.customer_id,
        tenant_id=record.tenant_id,
        ontology_id=record.ontology_id,
        ontology_version=record.ontology_version,
        customer_ontology_version=record.customer_ontology_version,
        status=record.status,
        definition=record.definition,
        change_type=record.change_type,
        change_reason=record.change_reason,
        change_summary=record.change_summary,
        parent_customer_ontology_id=record.parent_customer_ontology_id,
        created_by=record.created_by,
        created_at=record.created_at,
        confirmed_by=record.confirmed_by,
        confirmed_at=record.confirmed_at,
        updated_at=record.updated_at,
    )


def _to_history_entry(
    record: CustomerOntologyRecord,
) -> CustomerOntologyVersionSummary:
    compact_summary = dict(record.change_summary or {})
    compact_summary.pop("changes", None)
    return CustomerOntologyVersionSummary(
        customer_ontology_id=record.customer_ontology_id,
        customer_ontology_version=record.customer_ontology_version,
        status=record.status,
        change_type=record.change_type,
        change_reason=record.change_reason,
        change_summary=compact_summary,
        parent_customer_ontology_id=record.parent_customer_ontology_id,
        created_by=record.created_by,
        created_at=record.created_at,
        confirmed_by=record.confirmed_by,
        confirmed_at=record.confirmed_at,
    )


@customer_ontology_router.post(
    "/{customer_id}/ontology/{domain_id}/contextualize",
    response_model=CustomerOntologyResponse,
    summary="Save Customer Ontology contextualization",
    description=(
        "Saves one complete domain-scoped Customer Ontology contextualization batch "
        "for the authenticated customer. The request must use the current "
        "customer_ontology_version returned by the Customer Ontology GET endpoint "
        "as base_customer_ontology_version. On success, exactly one new immutable "
        "Customer Ontology version is created and returned, and the previous ACTIVE "
        "version becomes INACTIVE. The authenticated actor is resolved from the "
        "Authorization header and is recorded as the audit actor; audit identity "
        "must not be supplied by the UI request body. request_id provides idempotent "
        "request handling so a replay does not create another version. The UI success "
        "message should be: Knowledge context saved successfully. Your customer "
        "knowledge context has been updated to version {customer_ontology_version}."
    ),
    responses={
        200: {
            "description": (
                "Contextualization saved successfully. Returns the newly created "
                "ACTIVE Customer Ontology version."
            ),
        },
        400: {
            "description": (
                "The request is invalid, including a customer_id mismatch, unknown "
                "domain, invalid contextualization target, or stale base version."
            ),
        },
        401: {
            "description": (
                "Authentication is required. The authenticated actor is resolved "
                "from the Authorization header."
            ),
        },
        409: {
            "description": (
                "The request could not be committed because of a resource or "
                "persistence conflict."
            ),
        },
    },
)
def contextualize_customer_ontology(
    request: CustomerOntologyContextualizationRequest,
    customer_id: str = Path(
        ...,
        min_length=1,
        description="External customer identifier.",
    ),
    domain_id: str = Path(
        ...,
        min_length=1,
        description="Canonical domain being contextualized.",
    ),
    actor_id: str = Depends(require_user_id),
) -> CustomerOntologyResponse:
    if request.customer_id != customer_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_REQUEST",
                "message": "Path customer_id must match request customer_id.",
            },
        )

    if request.domain_id != domain_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_REQUEST",
                "message": "Path domain_id must match request domain_id.",
                "details": {
                    "customer_id": customer_id,
                    "domain_id": domain_id,
                },
            },
        )

    try:
        record = customer_ontology_service.contextualize_customer_ontology(
            request,
            actor_id=actor_id,
        )
    except KnowledgeContextualizationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONTEXTUALIZATION_INVALID",
                "message": str(exc),
                "details": {"customer_id": customer_id, "domain_id": request.domain_id},
            },
        ) from exc
    except Exception as exc:
        logger.exception(
            "Customer Ontology contextualization failed: customer_id=%s domain_id=%s",
            customer_id,
            request.domain_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESOURCE_CONFLICT",
                "message": str(exc),
            },
        ) from exc

    return _to_response(record)


@customer_ontology_router.get(
    "/{customer_id}/ontology/contextualization/{domain_id}",
    response_model=CustomerContextualizationDomainResponse,
    summary="Get Customer Knowledge Context for a domain",
    description=(
        "Returns the selected domain Customer Knowledge Context state. "
        "The response separates persisted customer configuration from the "
        "remaining canonical delta. Persisted canonical items and customer "
        "additions are locked after save. Remaining canonical items remain "
        "available for contextualization. Product Lines, Functions/BCGs, "
        "and SubFunctions/Cs can continue to be added under valid parents. "
        "Node-type applicability is returned separately. This endpoint is "
        "read-only and does not create or modify a Customer Ontology version."
    ),
    responses={
        404: {
            "description": "Customer or canonical domain was not found.",
        },
    },
)
def get_customer_contextualization_domain(
    customer_id: str = Path(..., min_length=1, description="External customer identifier."),
    domain_id: str = Path(..., min_length=1, description="Stable canonical domain identifier."),
) -> CustomerContextualizationDomainResponse:
    try:
        result = customer_ontology_service.get_contextualization_domain(
            customer_id=customer_id,
            domain_id=domain_id,
        )
    except KnowledgeContextualizationError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CONTEXTUALIZATION_NOT_FOUND",
                "message": str(exc),
                "details": {
                    "customer_id": customer_id,
                    "domain_id": domain_id,
                },
            },
        ) from exc

    return {
        "customer_id": result["customer_id"],
        "customer_name": result["customer_name"],
        "industry_id": result["industry_id"],
        "industry_name": result["industry_name"],
        "ontology_id": result["ontology_id"],
        "ontology_version": result["ontology_version"],
        "customer_ontology_version": result["customer_ontology_version"],
        "domain": result["domain"],
        "persisted": result["domain"]["persisted"],
        "canonical_delta": result["domain"]["canonical_delta"],
        "operations": result["domain"]["operations"],
    }

@customer_ontology_router.get(
    "/{customer_id}/ontology/contextualization",
    response_model=CustomerContextualizationPageResponse,
    summary="Get Customer Knowledge Context domains",
    description=(
        "Returns the canonical domains available for Customer Knowledge Context "
        "configuration. Customer and industry identity are returned as context "
        "and are not UI inputs. The UI should display the domains first and "
        "then call the domain-scoped endpoint after the user selects a domain. "
        "The response is read-only and does not create or modify a Customer "
        "Ontology version."
    ),
    responses={
        404: {
            "description": "Customer was not found or its canonical ontology could not be resolved.",
        },
    },
)
def get_customer_contextualization_page(
    customer_id: str = Path(..., min_length=1, description="External customer identifier."),
) -> CustomerContextualizationPageResponse:
    try:
        return customer_ontology_service.get_contextualization_page(customer_id=customer_id)
    except KnowledgeContextualizationError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CONTEXTUALIZATION_NOT_FOUND",
                "message": str(exc),
                "details": {"customer_id": customer_id},
            },
        ) from exc

@customer_ontology_router.get(
    "/{customer_id}/ontology",
    response_model=CustomerOntologyResponse,
    summary="Get the current Customer Ontology",
    description=(
        "Returns the ACTIVE Customer Ontology snapshot for a customer. "
        "The UI should call this when opening the Customer Context Overlay. "
        "The returned customer_ontology_version is the version the UI should "
        "use as base_customer_ontology_version when submitting contextualization changes. "
        "This endpoint is read-only and does not create or modify a version."
    ),
    responses={
        404: {
            "description": "Customer Ontology was not found.",
            "content": {
                "application/json": {
                    "example": {
                        "code": "RESOURCE_NOT_FOUND",
                        "message": "Customer Ontology not found.",
                        "details": {"customer_id": "CUST-001"},
                    }
                }
            },
        },
    },
)
def get_current_customer_ontology(
    customer_id: str = Path(
        ...,
        min_length=1,
        description="External customer identifier.",
        examples=["CUST-001"],
    ),
) -> CustomerOntologyResponse:
    record = customer_ontology_repository.get_active(customer_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": "Customer Ontology not found.",
                "details": {"customer_id": customer_id},
            },
        )

    return _to_response(record)


@customer_ontology_router.get(
    "/{customer_id}/ontology/versions",
    response_model=CustomerOntologyHistoryResponse,
    summary="Get Customer Ontology version history",
    description=(
        "Returns all persisted Customer Ontology versions for a customer, "
        "ordered newest-first. The UI should call this for Version History. "
        "Each entry identifies the version, status, change type, change summary, "
        "reason, creator, confirmation information, and parent version. "
        "This endpoint is read-only."
    ),
    responses={
        404: {
            "description": "No Customer Ontology versions exist for the customer.",
            "content": {
                "application/json": {
                    "example": {
                        "code": "RESOURCE_NOT_FOUND",
                        "message": "Customer Ontology not found.",
                        "details": {"customer_id": "CUST-001"},
                    }
                }
            },
        },
    },
)
def get_customer_ontology_history(
    customer_id: str = Path(
        ...,
        min_length=1,
        description="External customer identifier.",
        examples=["CUST-001"],
    ),
) -> CustomerOntologyHistoryResponse:
    records = customer_ontology_repository.list_history(customer_id)

    if not records:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": "Customer Ontology not found.",
                "details": {"customer_id": customer_id},
            },
        )

    current_version = next(
        (
            record.customer_ontology_version
            for record in records
            if record.status == "ACTIVE"
        ),
        None,
    )

    return CustomerOntologyHistoryResponse(
        customer_id=customer_id,
        current_customer_ontology_version=current_version,
        versions=[_to_history_entry(record) for record in records],
    )

@customer_ontology_router.get(
    "/{customer_id}/ontology/versions/{customer_ontology_version}",
    response_model=CustomerOntologyVersionDetailResponse,
    summary="Get detailed Customer Ontology version",
    description=(
        "Returns the detailed submitted changes for a specific Customer Ontology version. "
        "The UI should call this from the View Details action in Version History. "
        "The response contains semantic change data for rendering operation-specific details. "
        "For versions created before detailed change persistence was introduced, "
        "details_available is false and changes is empty."
    ),
    responses={
        404: {
            "description": "Customer Ontology version was not found.",
            "content": {
                "application/json": {
                    "example": {
                        "code": "RESOURCE_NOT_FOUND",
                        "message": "Customer Ontology version not found.",
                        "details": {
                            "customer_id": "CUST-001",
                            "customer_ontology_version": 2,
                        },
                    }
                }
            },
        },
        400: {
            "description": "Detailed change information is not available for this version.",
        },
    },
)
def get_customer_ontology_version_detail(
    customer_id: str = Path(
        ...,
        min_length=1,
        description="External customer identifier.",
        examples=["CUST-001"],
    ),
    customer_ontology_version: int = Path(
        ...,
        ge=1,
        description="Customer Ontology version to inspect.",
        examples=[1],
    ),
) -> CustomerOntologyVersionDetailResponse:
    records = customer_ontology_repository.list_history(customer_id)
    record = next(
        (
            item
            for item in records
            if item.customer_ontology_version == customer_ontology_version
        ),
        None,
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": "Customer Ontology version not found.",
                "details": {
                    "customer_id": customer_id,
                    "customer_ontology_version": customer_ontology_version,
                },
            },
        )

    summary = dict(record.change_summary or {})
    changes = summary.pop("changes", [])
    compact_summary = dict(summary)

    return CustomerOntologyVersionDetailResponse(
        customer_id=customer_id,
        customer_ontology_version=record.customer_ontology_version,
        status=record.status,
        change_type=record.change_type,
        change_reason=record.change_reason,
        change_summary=compact_summary,
        parent_customer_ontology_id=record.parent_customer_ontology_id,
        created_by=record.created_by,
        created_at=record.created_at,
        confirmed_by=record.confirmed_by,
        confirmed_at=record.confirmed_at,
        details_available=bool(changes),
        changes=changes,
    )

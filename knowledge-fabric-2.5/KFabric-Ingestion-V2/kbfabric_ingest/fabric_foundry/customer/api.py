from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kbfabric_ingest.fabric_foundry.customer.repository import (
    CustomerAlreadyExistsError,
    CustomerIndustryNotFoundError,
    CustomerRepository,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.service import (
    KnowledgeContextualizationService,
)
from kbfabric_ingest.security.auth import require_user_id
from kbfabric_ingest.utils.logger import get_logger


customer_router = APIRouter(
    prefix="/customers",
    tags=["Fabric Foundry - Industry & Customer Onboarding"],
)

logger = get_logger(__name__)


class CustomerOnboardingRequest(BaseModel):
    """Customer onboarding request."""

    customer_name: str = Field(
        ...,
        min_length=1,
        description="Customer display name. Fabric Foundry uses this value when constructing the generated customer_id.",
    )
    industry_id: str = Field(
        ...,
        min_length=1,
        description="Supported active industry identifier. Use an industry_id returned by GET /fabric-foundry/industries.",
    )


class CustomerOnboardingResponse(BaseModel):
    """Customer onboarding response."""

    tenant_id: str = Field(
        description="Backend-generated unique tenant UUID.",
    )
    customer_id: str = Field(
        description="Backend-generated customer display/service identifier in the format Pulse AI_<customer_name>_<tenant_id>.",
    )
    customer_name: str = Field(
        description="Customer display name supplied during onboarding.",
    )
    industry_id: str = Field(
        description="Industry identifier associated with the customer.",
    )
    industry_name: str = Field(
        description="Human-readable industry name resolved from the Fabric Foundry industry registry.",
    )
    status: str = Field(
        description="Customer lifecycle status. Successful onboarding creates the customer as ACTIVE.",
    )


@customer_router.post(
    "",
    response_model=CustomerOnboardingResponse,
    status_code=201,
    summary="Onboard a customer",
    description=(
        "Creates a new customer workspace for a supported active industry. "
        "The UI supplies only customer_name and industry_id. "
        "Fabric Foundry generates tenant_id as a UUID and derives customer_id "
        "as Pulse AI_<customer_name>_<tenant_id>. "
        "On successful onboarding the customer is created with status ACTIVE. "
        "Potential UI success message: Welcome to Pulse AI Fabric Foundry, "
        "{customer_name}. Your {industry_name} workspace has been created "
        "successfully. Next, let us configure your knowledge context. "
        "This welcome message is UI guidance only and is not an API response field."
    ),
    responses={
        201: {
            "description": "Customer workspace created successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
                        "customer_id": "Pulse AI_Acme Bank_550e8400-e29b-41d4-a716-446655440000",
                        "customer_name": "Acme Bank",
                        "industry_id": "banking",
                        "industry_name": "Enterprise Banking",
                        "status": "ACTIVE",
                    }
                }
            },
        },
        409: {
            "description": "Customer persistence conflict.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Customer already exists: Pulse AI_Acme Bank_550e8400-e29b-41d4-a716-446655440000"
                    }
                }
            },
        },
        422: {
            "description": "Request validation failed or the requested industry is not active.",
            "content": {
                "application/json": {
                    "examples": {
                        "inactive_or_unknown_industry": {
                            "summary": "Industry is not active",
                            "value": {
                                "detail": "Active industry not found: unknown"
                            },
                        },
                        "request_validation": {
                            "summary": "Required request field is missing or empty",
                            "value": {
                                "detail": [
                                    {
                                        "type": "missing",
                                        "loc": ["body", "customer_name"],
                                        "msg": "Field required",
                                        "input": None,
                                    }
                                ]
                            },
                        },
                    }
                }
            },
        },
        500: {
            "description": "Customer onboarding failed due to an unexpected persistence or industry-resolution error.",
            "content": {
                "application/json": {
                    "examples": {
                        "onboarding_failed": {
                            "summary": "Unexpected onboarding failure",
                            "value": {
                                "detail": "Customer onboarding failed"
                            },
                        },
                        "industry_resolution_failed": {
                            "summary": "Industry could not be resolved after creation",
                            "value": {
                                "detail": "Customer industry could not be resolved"
                            },
                        },
                    }
                }
            },
        },
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "customer_name": "Acme Bank",
                        "industry_id": "banking",
                    }
                }
            }
        }
    },
)
async def onboard_customer(
    request: CustomerOnboardingRequest,
    actor_id: str = Depends(require_user_id),
) -> CustomerOnboardingResponse:
    """Create a new customer workspace and generate its Fabric Foundry customer and tenant identities."""

    repository = CustomerRepository()

    logger.info(
        "customer.api.onboarding.start",
        customer_name=request.customer_name,
        industry_id=request.industry_id,
        actor_id=actor_id,
    )

    try:
        context = repository.create_customer(
            customer_name=request.customer_name,
            industry_id=request.industry_id,
            created_by=actor_id,
        )
        KnowledgeContextualizationService(
            customer_repository=repository,
        ).initialize_customer_ontology(
            customer_id=context.customer_id,
            created_by=actor_id,
            change_reason="Initialized during customer onboarding",
        )
    except CustomerAlreadyExistsError as exc:
        logger.info(
            "customer.api.onboarding.conflict",
            error=str(exc),
        )
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except CustomerIndustryNotFoundError as exc:
        logger.info(
            "customer.api.onboarding.invalid_industry",
            industry_id=request.industry_id,
        )
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "customer.api.onboarding.failed",
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="Customer onboarding failed",
        ) from exc

    logger.info(
        "customer.api.onboarding.completed",
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        industry_id=context.industry_id,
        actor_id=actor_id,
    )

    # Resolve the display name from the Fabric Foundry industry registry.
    industry = repository.industry_repository.get_industry(
        industry_id=context.industry_id
    )

    if industry is None:
        logger.error(
            "customer.api.onboarding.industry_resolution_failed",
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            industry_id=context.industry_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Customer industry could not be resolved",
        )

    return CustomerOnboardingResponse(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        customer_name=context.customer_name,
        industry_id=context.industry_id,
        industry_name=industry.industry_name,
        status="ACTIVE",
    )

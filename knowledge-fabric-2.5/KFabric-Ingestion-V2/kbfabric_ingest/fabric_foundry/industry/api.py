from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRepository
from kbfabric_ingest.fabric_foundry.ontology.repository import OntologyRepository
from kbfabric_ingest.utils.logger import get_logger


industry_router = APIRouter(
    tags=["Fabric Foundry - Industry & Customer Onboarding"],
)

logger = get_logger(__name__)

industry_repository = IndustryRepository()
ontology_repository = OntologyRepository()
industry_ontology_repository = IndustryOntologyRepository()


class OntologySummary(BaseModel):
    """Active ontology metadata associated with an industry."""

    ontology_id: str = Field(
        description="Stable identifier of the ontology."
    )
    ontology_name: str = Field(
        description="Human-readable name of the ontology."
    )
    version: str = Field(
        description="Version of the ontology associated with the industry."
    )
    status: str = Field(
        description="Lifecycle status of the ontology. Only ACTIVE ontologies are returned by this endpoint."
    )


class IndustrySummary(BaseModel):
    """Industry option exposed to the Fabric Foundry UI."""

    industry_id: str = Field(
        description="Stable backend identifier for the industry. Use this value when calling industry-specific APIs."
    )
    industry_name: str = Field(
        description="Human-readable industry name displayed in the UI."
    )
    status: str = Field(
        description="Industry lifecycle status. This endpoint returns active industries only."
    )


class IndustryOntologyResponse(BaseModel):
    """Active ontology metadata available for a selected industry."""

    industry_id: str = Field(
        description="Identifier of the requested industry."
    )
    industry_name: str = Field(
        description="Human-readable name of the requested industry."
    )
    ontologies: list[OntologySummary] = Field(
        description="Active ontologies associated with the industry."
    )


@industry_router.get(
    "/industries",
    response_model=list[IndustrySummary],
    summary="List available industries",
    description=(
        "Returns all active industries available to Fabric Foundry. "
        "The UI uses this endpoint to populate the Industry selection at bootstrap. "
        "No request body or query parameters are required. "
        "The returned industry_id is the identifier to use with subsequent "
        "industry-specific APIs."
    ),
    response_description="List of active industries available to Fabric Foundry.",
    responses={
        200: {
            "description": "Active industries returned successfully.",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "industry_id": "banking",
                            "industry_name": "Banking",
                            "status": "ACTIVE",
                        },
                        {
                            "industry_id": "health_insurance_care_services",
                            "industry_name": "Health Insurance & Care Services",
                            "status": "ACTIVE",
                        },
                        {
                            "industry_id": "insurance",
                            "industry_name": "Insurance & Retirement Services",
                            "status": "ACTIVE",
                        },
                    ]
                }
            },
        }
    },
)
async def list_industries() -> list[IndustrySummary]:
    logger.info("fabric_foundry.industries.list.start")

    records = industry_repository.list_industries(active_only=True)

    industries = [
        IndustrySummary(
            industry_id=industry.industry_id,
            industry_name=industry.industry_name,
            status=industry.status,
        )
        for industry in records
    ]

    logger.info(
        "fabric_foundry.industries.list.completed",
        count=len(industries),
    )

    return industries


@industry_router.get(
    "/industries/{industry_id}/ontologies",
    response_model=IndustryOntologyResponse,
    summary="List active ontologies for an industry",
    description=(
        "Returns the active ontology metadata associated with the requested industry. "
        "The industry_id should be taken from GET /fabric-foundry/industries. "
        "This endpoint returns ontology metadata only; it does not return the "
        "Domain, Product Line, Function, SubFunction, or Node Type hierarchy. "
        "No request body or query parameters are required."
    ),
    response_description="Active ontology metadata associated with the selected industry.",
    responses={
        200: {
            "description": "Active ontology metadata returned successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "industry_id": "banking",
                        "industry_name": "Banking",
                        "ontologies": [
                            {
                                "ontology_id": "banking",
                                "ontology_name": "Enterprise Banking",
                                "version": "5.0.1",
                                "status": "ACTIVE",
                            }
                        ],
                    }
                }
            },
        },
        404: {
            "description": "Industry does not exist or is not ACTIVE.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Industry not found: banking"
                    }
                }
            },
        },
    },
)
async def list_industry_ontologies(
    industry_id: str = Path(
        description="Industry identifier returned by GET /fabric-foundry/industries."
    ),
) -> IndustryOntologyResponse:
    logger.info(
        "fabric_foundry.industry_ontologies.list.start",
        industry_id=industry_id,
    )

    industry = industry_repository.get_industry(
        industry_id=industry_id,
    )

    if industry is None or industry.status != "ACTIVE":
        logger.info(
            "fabric_foundry.industry_ontologies.list.not_found",
            industry_id=industry_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Industry not found: {industry_id}",
        )

    associations = industry_ontology_repository.list_for_industry(
        industry_id=industry.industry_id,
        active_only=True,
    )

    ontologies: list[OntologySummary] = []

    for association in associations:
        ontology = ontology_repository.get_ontology(
            ontology_id=association.ontology_id,
            version=association.ontology_version,
        )

        if ontology is None or ontology.status != "ACTIVE":
            continue

        ontologies.append(
            OntologySummary(
                ontology_id=ontology.ontology_id,
                ontology_name=ontology.ontology_name,
                version=ontology.version,
                status=ontology.status,
            )
        )

    logger.info(
        "fabric_foundry.industry_ontologies.list.completed",
        industry_id=industry.industry_id,
        count=len(ontologies),
    )

    return IndustryOntologyResponse(
        industry_id=industry.industry_id,
        industry_name=industry.industry_name,
        ontologies=ontologies,
    )

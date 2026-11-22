from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OntologyDefinition:
    ontology_id: str
    ontology_name: str
    version: str
    status: str
    path: str


@dataclass(frozen=True)
class IndustryDefinition:
    industry_id: str
    industry_name: str
    status: str
    ontologies: tuple[OntologyDefinition, ...]


INDUSTRIES: tuple[IndustryDefinition, ...] = (
    IndustryDefinition(
        industry_id="banking",
        industry_name="Banking",
        status="ACTIVE",
        ontologies=(
            OntologyDefinition(
                ontology_id="banking",
                ontology_name="Enterprise Banking",
                version="5.0.1",
                status="ACTIVE",
                path="config/industries/banking/ontology.yaml",
            ),
        ),
    ),

    IndustryDefinition(
        industry_id="health_insurance_care_services",
        industry_name="Health Insurance & Care Services",
        status="ACTIVE",
        ontologies=(
            OntologyDefinition(
                ontology_id="health_insurance_care_services",
                ontology_name="Health Insurance & Care Services",
                version="1.0.0",
                status="ACTIVE",
                path="config/industries/health_insurance_care_services/ontology.yaml",
            ),
        ),
    ),
    IndustryDefinition(
        industry_id="insurance",
        industry_name="Insurance & Retirement Services",
        status="ACTIVE",
        ontologies=(
            OntologyDefinition(
                ontology_id="insurance",
                ontology_name="Insurance & Retirement Services",
                version="1.0.0",
                status="ACTIVE",
                path="config/industries/insurance/ontology.yaml",
            ),
        ),
    ),
)


def get_industries() -> list[IndustryDefinition]:
    return list(INDUSTRIES)


def get_industry(industry_id: str) -> IndustryDefinition | None:
    normalized = industry_id.strip().lower()

    for industry in INDUSTRIES:
        if industry.industry_id == normalized:
            return industry

    return None

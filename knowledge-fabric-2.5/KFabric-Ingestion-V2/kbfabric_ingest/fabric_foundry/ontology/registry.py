from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kbfabric_ingest.utils.logger import get_logger


BASE_DIR = Path(__file__).resolve().parents[3]

logger = get_logger(__name__)


@dataclass(frozen=True)
class OntologyDefinition:
    ontology_id: str
    ontology_name: str
    version: str
    industry_id: str
    status: str
    path: str


ONTOLOGIES: tuple[OntologyDefinition, ...] = (
    OntologyDefinition(
        ontology_id="banking",
        ontology_name="Enterprise Banking",
        version="5.0.1",
        industry_id="banking",
        status="ACTIVE",
        path="config/industries/banking/ontology.yaml",
    ),
    OntologyDefinition(
        ontology_id="health_insurance_care_services",
        ontology_name="Health Insurance & Care Services",
        version="1.0.0",
        industry_id="health_insurance_care_services",
        status="ACTIVE",
        path="config/industries/health_insurance_care_services/ontology.yaml",
    ),
    OntologyDefinition(
        ontology_id="insurance",
        ontology_name="Insurance & Retirement Services",
        version="1.0.0",
        industry_id="insurance",
        status="ACTIVE",
        path="config/industries/insurance/ontology.yaml",
    ),
)


def get_ontology_definitions() -> list[OntologyDefinition]:
    """Return all registered ontology definitions."""
    logger.info(
        "fabric_foundry.ontology_registry.loaded",
        ontology_count=len(ONTOLOGIES),
        ontology_ids=[ontology.ontology_id for ontology in ONTOLOGIES],
    )
    return list(ONTOLOGIES)


def get_ontology_definition(
    ontology_id: str,
    version: str | None = None,
) -> OntologyDefinition | None:
    """
    Resolve an ontology definition by ontology ID and optional version.

    The registry stores metadata and the canonical configuration path.
    It does not load or mutate the ontology YAML.
    """
    normalized_ontology_id = ontology_id.strip().lower()

    for ontology in ONTOLOGIES:
        if ontology.ontology_id != normalized_ontology_id:
            continue

        if version is not None and ontology.version != version.strip():
            continue

        return ontology

    return None


def get_ontology_path(
    ontology_id: str,
    version: str | None = None,
) -> Path | None:
    """Resolve the filesystem path for a registered ontology."""
    definition = get_ontology_definition(
        ontology_id=ontology_id,
        version=version,
    )

    if definition is None:
        return None

    return BASE_DIR / definition.path

from __future__ import annotations

from dataclasses import dataclass

from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRepository
from kbfabric_ingest.fabric_foundry.industry.ontology_schema import IndustryOntologySchema
from kbfabric_ingest.fabric_foundry.industry.schema import IndustrySchema
from kbfabric_ingest.fabric_foundry.customer.schema import CustomerSchema
from kbfabric_ingest.fabric_foundry.ontology.repository import OntologyRepository
from kbfabric_ingest.fabric_foundry.ontology.schema import OntologySchema
from kbfabric_ingest.ingest.repository.hitl_review_schema import HITLReviewSchema
from kbfabric_ingest.ingest.repository.ingestion_run_schema import IngestionRunSchema
from kbfabric_ingest.utils.logger import get_logger


@dataclass(frozen=True)
class BootstrapIndustry:
    industry_id: str
    industry_name: str
    ontology_id: str
    ontology_name: str
    ontology_version: str
    artifact_path: str


BOOTSTRAP_INDUSTRIES = (
    BootstrapIndustry(
        industry_id="banking",
        industry_name="Banking",
        ontology_id="banking",
        ontology_name="Enterprise Banking",
        ontology_version="5.0.1",
        artifact_path="config/industries/banking/ontology.yaml",
    ),
    BootstrapIndustry(
        industry_id="health_insurance_care_services",
        industry_name="Health Insurance & Care Services",
        ontology_id="health_insurance_care_services",
        ontology_name="Health Insurance & Care Services",
        ontology_version="1.0.0",
        artifact_path="config/industries/health_insurance_care_services/ontology.yaml",
    ),
    BootstrapIndustry(
        industry_id="insurance",
        industry_name="Insurance & Retirement Services",
        ontology_id="insurance",
        ontology_name="Insurance & Retirement Services",
        ontology_version="1.0.0",
        artifact_path="config/industries/insurance/ontology.yaml",
    ),
)


class FabricFoundryBootstrap:
    """Initialize the persisted Fabric Foundry control plane."""

    def __init__(self) -> None:
        self.logger = get_logger(__name__)
        self.industry_schema = IndustrySchema()
        self.industry_ontology_schema = IndustryOntologySchema()
        self.ontology_schema = OntologySchema()
        self.customer_schema = CustomerSchema()
        self.hitl_review_schema = HITLReviewSchema()
        self.ingestion_run_schema = IngestionRunSchema()
        self.industry_repository = IndustryRepository()
        self.ontology_repository = OntologyRepository()
        self.association_repository = IndustryOntologyRepository()

    def run(self) -> None:
        self.logger.info("fabric_foundry.bootstrap.start")

        self._ensure_schema()
        self.hitl_review_schema.ensure()
        self.ingestion_run_schema.ensure()
        self._seed_industries()
        self._seed_ontologies()
        self._seed_associations()
        self._ensure_customer_schema()

        self.logger.info(
            "fabric_foundry.bootstrap.completed",
            industry_count=len(BOOTSTRAP_INDUSTRIES),
        )

    def _ensure_schema(self) -> None:
        self.logger.info("fabric_foundry.bootstrap.schema.start")

        # Industry must exist before industry_ontology because of its FK.
        self.industry_schema.ensure()
        self.ontology_schema.ensure()

        self._ensure_industry_ontology_schema()

        self.logger.info("fabric_foundry.bootstrap.schema.completed")

    def _ensure_industry_ontology_schema(self) -> None:
        self.industry_ontology_schema.ensure()

    def _ensure_customer_schema(self) -> None:
        self.logger.info("fabric_foundry.bootstrap.customer_schema.start")
        self.customer_schema.ensure()
        self.logger.info("fabric_foundry.bootstrap.customer_schema.completed")

    def _seed_industries(self) -> None:
        for definition in BOOTSTRAP_INDUSTRIES:
            existing = self.industry_repository.get_industry(
                industry_id=definition.industry_id
            )

            if existing is not None:
                self.logger.info(
                    "fabric_foundry.bootstrap.industry.exists",
                    industry_id=definition.industry_id,
                )
                continue

            self.industry_repository.create_industry(
                industry_id=definition.industry_id,
                industry_name=definition.industry_name,
            )

            self.logger.info(
                "fabric_foundry.bootstrap.industry.created",
                industry_id=definition.industry_id,
            )

    def _seed_ontologies(self) -> None:
        for definition in BOOTSTRAP_INDUSTRIES:
            existing = self.ontology_repository.get_ontology(
                ontology_id=definition.ontology_id,
                version=definition.ontology_version,
            )

            if existing is not None:
                self.logger.info(
                    "fabric_foundry.bootstrap.ontology.exists",
                    ontology_id=definition.ontology_id,
                    version=definition.ontology_version,
                )
                continue

            self.ontology_repository.create_ontology(
                ontology_id=definition.ontology_id,
                ontology_name=definition.ontology_name,
                version=definition.ontology_version,
                artifact_path=definition.artifact_path,
            )

            self.logger.info(
                "fabric_foundry.bootstrap.ontology.created",
                ontology_id=definition.ontology_id,
                version=definition.ontology_version,
            )

    def _seed_associations(self) -> None:
        for definition in BOOTSTRAP_INDUSTRIES:
            existing = self.association_repository.get_association(
                industry_id=definition.industry_id,
                ontology_id=definition.ontology_id,
                ontology_version=definition.ontology_version,
            )

            if existing is not None:
                self.logger.info(
                    "fabric_foundry.bootstrap.association.exists",
                    industry_id=definition.industry_id,
                    ontology_id=definition.ontology_id,
                    ontology_version=definition.ontology_version,
                )
                continue

            self.association_repository.associate(
                industry_id=definition.industry_id,
                ontology_id=definition.ontology_id,
                ontology_version=definition.ontology_version,
                is_default=True,
            )

            self.logger.info(
                "fabric_foundry.bootstrap.association.created",
                industry_id=definition.industry_id,
                ontology_id=definition.ontology_id,
                ontology_version=definition.ontology_version,
            )

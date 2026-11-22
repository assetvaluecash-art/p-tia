from fastapi import APIRouter

from kbfabric_ingest.fabric_foundry.customer.api import customer_router
from kbfabric_ingest.fabric_foundry.industry.api import industry_router
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.api import customer_ontology_router

fabric_foundry_router = APIRouter(
    prefix="/fabric-foundry",
    tags=["Fabric Foundry"],
)

fabric_foundry_router.include_router(customer_router)
fabric_foundry_router.include_router(industry_router)
fabric_foundry_router.include_router(customer_ontology_router)

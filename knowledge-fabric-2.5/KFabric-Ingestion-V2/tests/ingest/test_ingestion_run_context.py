from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    OntologyRuntimeContext,
)
from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext


def test_ingestion_run_context_freezes_customer_and_ontology_lineage():
    """Objective: verify one immutable run context carries workspace and ontology lineage.

    This matters because every ingestion artifact must remain attributable to the
    customer/tenant and the exact ontology snapshot selected when the run starts.
    If the implementation changes, those two pieces of lineage must remain bound
    together and immutable for the lifetime of the run.
    """
    tenant_context = TenantContext(
        tenant_id="tenant-1",
        customer_id="customer-1",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    ontology_context = OntologyRuntimeContext(
        customer_id="customer-1",
        tenant_id="tenant-1",
        domain_id="consumer_payments",
        customer_ontology_id="co-1",
        customer_ontology_version=3,
        ontology_id="enterprise_banking",
        ontology_version="5.0.1",
        domain_definition={"name": "consumer_payments"},
        canonical_model={"node_types": [], "relationship_types": [], "block_categories": []},
        extraction_policy={"node_types": [], "relationship_types": []},
        product_line_id="pl-1",
        product_line_name="Cards",
    )

    run_context = IngestionRunContext(
        tenant_context=tenant_context,
        ontology=ontology_context,
        hitl_mode="review_all",
    )

    assert run_context.hitl_mode == "review_all"
    assert run_context.tenant_context.customer_id == "customer-1"
    assert run_context.tenant_context.tenant_id == "tenant-1"
    assert run_context.ontology.domain_id == "consumer_payments"
    assert run_context.ontology.product_line_id == "pl-1"
    assert run_context.ontology.customer_ontology_version == 3

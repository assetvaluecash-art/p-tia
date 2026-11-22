from __future__ import annotations

from dataclasses import dataclass

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    OntologyRuntimeContext,
)


@dataclass(frozen=True)
class IngestionRunContext:
    """Immutable customer and ontology context frozen for one ingestion run."""

    tenant_context: TenantContext
    ontology: OntologyRuntimeContext
    hitl_mode: str = "review_ambiguous"

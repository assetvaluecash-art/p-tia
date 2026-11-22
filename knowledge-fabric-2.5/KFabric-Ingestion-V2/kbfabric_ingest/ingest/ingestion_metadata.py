"""Normalization helpers for optional ingestion context and provenance."""

from __future__ import annotations

from typing import Any
import hashlib
import uuid


def split_semicolon_values(raw_value: str | None) -> list[str]:
    """Split optional semicolon-delimited user context, preserving first spelling."""
    if not raw_value:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw_value.split(";"):
        value = item.strip()
        key = value.casefold()
        if value and key not in seen:
            values.append(value)
            seen.add(key)
    return values


def build_run_metadata(
    run_context: Any,
    *,
    ingestion_batch_id: str,
    project_ids_raw: str | None = None,
    context_tags_raw: str | None = None,
    document_nature: str | None = None,
) -> dict[str, Any]:
    """Build the flat, store-friendly provenance contract for one frozen run."""
    tenant = run_context.tenant_context
    ontology = run_context.ontology
    policy_version = (ontology.extraction_policy or {}).get("version")
    return {
        "ingestion_batch_id": ingestion_batch_id,
        "tenant_id": tenant.tenant_id,
        "customer_id": tenant.customer_id,
        "domain_id": ontology.domain_id,
        "ingestion_product_line_id": ontology.product_line_id,
        "customer_ontology_id": str(ontology.customer_ontology_id),
        "customer_ontology_version": ontology.customer_ontology_version,
        "canonical_ontology_version": ontology.ontology_version,
        "industry_extraction_policy_version": str(policy_version) if policy_version is not None else None,
        "document_nature": document_nature,
        "project_ids": split_semicolon_values(project_ids_raw),
        "context_tags": split_semicolon_values(context_tags_raw),
        "raw_project_ids": project_ids_raw,
        "raw_context_tags": context_tags_raw,
    }


def document_id_for_source(customer_id: str | None, source_file: str) -> str | None:
    """Return a stable document identity without treating file text as an ID."""
    if not customer_id or not source_file:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"customer:{customer_id}:source:{source_file}"))


def customer_scope_id(customer_id: str | None, tenant_id: str | None = None) -> str:
    """Return the stable graph scope, requiring customer scope when available."""
    customer = str(customer_id).strip() if customer_id else None
    tenant = str(tenant_id).strip() if tenant_id else None
    if not customer and not tenant:
        raise ValueError("A customer_id or tenant_id is required for persisted identity")
    if customer and tenant:
        return f"tenant:{tenant}|customer:{customer}"
    return f"customer:{customer}" if customer else f"tenant:{tenant}"


def scoped_entity_id(
    scope_id: str,
    domain: str,
    entity_type: str,
    normalized_value: str,
) -> str:
    """Build deterministic entity IDs that cannot collide across customers."""
    identity = ":".join((scope_id, domain, entity_type, normalized_value))
    return hashlib.sha256(identity.encode()).hexdigest()


def scoped_block_id(scope_id: str, block_id: str) -> str:
    """Build a customer-scoped UUID for a stored block while retaining source block ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"customer:{scope_id}:block:{block_id}"))

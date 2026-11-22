"""Idempotent Neo4j executor for persisted HITL-approved plans."""

from __future__ import annotations

import json
import re
from typing import Any

from neo4j import GraphDatabase

from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.ingest.execution_plan import ExecutionPlan
from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_entity_id
from kbfabric_ingest.ingest.kg_writer import KGWriter


_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")


class HITLPlanWriter:
    """Apply an already-reviewed plan in one idempotent Neo4j transaction.

    This writer accepts only the persisted plan and provenance context; it never
    accepts arbitrary Cypher, labels, relationship types, or endpoint IDs from a
    client request.
    """

    def __init__(self, driver: Any | None = None) -> None:
        self._owns_driver = driver is None
        if driver is None:
            config = get_app_config()["neo4j"]
            driver = GraphDatabase.driver(
                config["uri"],
                auth=(config.get("user") or config.get("username"), config["password"]),
            )
        self.driver = driver

    @staticmethod
    def _safe_label(value: str, field: str) -> str:
        if not _LABEL.fullmatch(value):
            raise ValueError(f"Invalid persisted {field}: {value!r}")
        return value

    @staticmethod
    def _append(values: Any, value: str) -> list[str]:
        result = list(values or [])
        if value and value not in result:
            result.append(value)
        return result

    def execute(self, plan: ExecutionPlan, item: dict[str, Any]) -> dict[str, int]:
        """Write approved nodes/edges with deterministic identities and scope checks."""
        scope = item.get("ontology_context_json") or {}
        document = item.get("document_context_json") or {}
        source = item.get("source_context_json") or {}
        mapping = item.get("mapping_context_json") or {}
        customer_id = str(scope.get("customer_id") or "").strip()
        tenant_id = str(scope.get("tenant_id") or "").strip()
        domain_id = str(scope.get("domain_id") or "").strip()
        industry_id = str(scope.get("industry_id") or "").strip()
        product_line_id = str(scope.get("product_line_id") or "").strip()
        batch_id = str(item.get("ingestion_batch_id") or "").strip()
        if not all((customer_id, tenant_id, domain_id, product_line_id, batch_id)):
            raise ValueError("Review item is missing frozen customer, tenant, domain, product-line, or batch scope")

        incoming = mapping.get("incoming_entity") or {}
        primary_type = str(incoming.get("node_type") or "").strip()
        persisted_candidates = mapping.get("candidate_scores") or []
        primary_name = str(incoming.get("name") or "").strip()
        if not primary_type or not primary_name:
            raise ValueError("Review item lacks the persisted primary entity contract")
        self._safe_label(primary_type, "node type")
        resolutions = list(plan.context_resolutions or [])
        allowed_rels = set((mapping.get("ontology_constraints") or {}).get("relationship_types") or [])
        allowed_triples = {
            tuple(x) for x in ((mapping.get("ontology_constraints") or {}).get("allowed_relationship_triples") or [])
            if isinstance(x, (list, tuple)) and len(x) == 3
        }

        scope_id = customer_scope_id(customer_id, tenant_id)
        temp_ids: dict[str, str] = {}
        node_specs: list[tuple[Any, str]] = []
        for operation in plan.entity_operations:
            node_type = self._safe_label(operation.entity_type, "node type")
            if node_type != primary_type:
                raise ValueError("Reviewed plan node type differs from the persisted extracted node type")
            if operation.operation == "CREATE":
                if operation.name.strip().casefold() != primary_name.casefold():
                    raise ValueError("Create operation name differs from the persisted extracted entity")
                display_name = KGWriter._normalize_to_camelcase(operation.name, node_type)
                normalized = KGWriter._normalize_entity_value(display_name)
                entity_id = scoped_entity_id(scope_id, domain_id, node_type, normalized)
                if operation.temporary_id:
                    temp_ids[operation.temporary_id] = entity_id
            elif operation.operation == "REUSE":
                entity_id = str(operation.entity_id or "")
                candidate = next((
                    entry.get("candidate", entry)
                    for entry in persisted_candidates
                    if str((entry.get("candidate", entry) or {}).get("entity_id") or "") == entity_id
                ), None)
                if not entity_id or candidate is None or candidate.get("kg_node_type") != node_type:
                    raise ValueError("Reuse target is not a persisted candidate of the required ontology type")
            else:
                raise ValueError(f"Unsupported entity operation: {operation.operation}")
            node_specs.append((operation, entity_id))

        relationships: list[tuple[Any, str, str, str, str]] = []

        def resolve_endpoint(entity_id: str | None, temporary_id: str | None) -> str | None:
            # A pre-write relationship snapshot may retain the incoming
            # temporary ID in entity_id as well as temporary_id. Once its
            # node operation has been materialized, use the deterministic
            # scoped ID for either representation.
            if temporary_id and temporary_id in temp_ids:
                return temp_ids[temporary_id]
            if entity_id and entity_id in temp_ids:
                return temp_ids[entity_id]
            return entity_id

        for operation in plan.relationship_operations:
            rel_type = self._safe_label(operation.rel_type, "relationship type")
            resolution = next((r for r in resolutions if
                r.get("rel_type") == operation.rel_type and
                r.get("target_type") == operation.target_type and
                str(r.get("target_value", "")).casefold() == operation.target_value.casefold()), None)
            if resolution is None:
                raise ValueError("Relationship operation has no matching persisted context-resolution evidence")
            selected = str(resolution.get("selected_entity_id") or "")
            candidate = next((c for c in (resolution.get("candidates") or [])
                              if str(c.get("entity_id") or "") == selected
                              and c.get("node_type") == resolution.get("target_type")), None)
            if not selected or candidate is None:
                raise ValueError("Relationship endpoint is not a persisted candidate of the required ontology type")
            if selected not in {operation.source_entity_id, operation.target_entity_id}:
                raise ValueError("Relationship endpoint differs from persisted reviewer selection")
            direction = str(resolution.get("direction") or "outgoing").casefold()
            if direction == "incoming":
                source_type, target_type = str(operation.target_type), primary_type
            else:
                source_type, target_type = primary_type, str(operation.target_type)
            self._safe_label(source_type, "relationship source type")
            self._safe_label(target_type, "relationship target type")
            triple = (source_type, rel_type, target_type)
            if allowed_triples and triple not in allowed_triples:
                raise ValueError(f"Relationship violates the persisted ontology triple contract: {triple}")
            if allowed_rels and rel_type not in allowed_rels:
                raise ValueError(f"Relationship type violates the persisted ontology contract: {rel_type}")
            source_id = resolve_endpoint(
                operation.source_entity_id, operation.source_temporary_id
            )
            target_id = resolve_endpoint(
                operation.target_entity_id, operation.target_temporary_id
            )
            if direction == "incoming":
                source_id, target_id = target_id, source_id
                if not source_id:
                    source_id = selected
                if not target_id:
                    target_id = plan.selected_entity_id or next(iter(temp_ids.values()), None)
            if not source_id or not target_id:
                raise ValueError("Relationship operation lacks resolved source or target identity")
            relationships.append((operation, source_id, target_id, source_type, target_type))

        doc_id = document.get("document_id")
        source_block_ids = source.get("source_block_ids") or [source.get("block_id")]
        source_block_ids = [str(value) for value in source_block_ids if value]
        source_file = str(document.get("source_file") or source.get("source_file") or "")
        product_lines = [product_line_id]
        base_props = {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "domain": domain_id,
            "domain_id": domain_id,
            "industry_id": industry_id,
            "ingestion_batch_ids": [batch_id],
            "ingestion_product_line_ids": product_lines,
            "source_document_ids": [doc_id] if doc_id else [],
            "source_blocks": source_block_ids,
            "source_files": [source_file] if source_file else [],
            "source_pages": [source.get("page_number")] if source.get("page_number") is not None else [],
            "industry_extraction_policy_versions": (
                [scope.get("industry_extraction_policy_version")]
                if scope.get("industry_extraction_policy_version") else []
            ),
            "customer_ontology_ids": [scope.get("customer_ontology_id")] if scope.get("customer_ontology_id") else [],
            "customer_ontology_versions": [scope.get("customer_ontology_version")] if scope.get("customer_ontology_version") is not None else [],
            "canonical_ontology_versions": [scope.get("canonical_ontology_version")] if scope.get("canonical_ontology_version") else [],
            "project_ids": document.get("project_ids") or [],
            "context_tags": document.get("context_tags") or [],
        }

        def write_tx(tx: Any) -> dict[str, int]:
            counts = {"nodes": 0, "relationships": 0, "aliases": 0}
            if industry_id:
                tx.run("""
                    MERGE (customer:CustomerRoot {customer_id: $customer_id, tenant_id: $tenant_id})
                    ON CREATE SET customer.name = $customer_id,
                                  customer.kg_node_type = 'CustomerRoot', customer.layer = 'customer_structure',
                                  customer.created_at = datetime()
                    SET customer.industry_id = $industry_id,
                        customer.kg_node_type = 'CustomerRoot'
                    MERGE (domain:DomainRoot {customer_id: $customer_id, domain: $domain_id})
                    ON CREATE SET domain.tenant_id = $tenant_id, domain.domain_id = $domain_id,
                                  domain.industry_id = $industry_id, domain.kg_node_type = 'DomainRoot',
                                  domain.layer = 'domain_structure', domain.created_at = datetime()
                    MERGE (domain)-[:PART_OF]->(customer)
                    MERGE (pl:ProductLine {customer_id: $customer_id, product_line_id: $product_line_id})
                    ON CREATE SET pl.tenant_id = $tenant_id, pl.domain = $domain_id,
                                  pl.domain_id = $domain_id, pl.name = $product_line_name,
                                  pl.kg_node_type = 'ProductLine', pl.layer = 'domain_structure',
                                  pl.created_at = datetime()
                    MERGE (pl)-[:PART_OF]->(domain)
                    MERGE (pl)-[:NODE_IN_DOMAIN]->(domain)
                """, customer_id=customer_id, tenant_id=tenant_id, industry_id=industry_id,
                    domain_id=domain_id, product_line_id=product_line_id,
                    product_line_name=str(document.get("product_line_name") or product_line_id))
                tx.run("""
                    MATCH (customer:CustomerRoot {customer_id: $customer_id, tenant_id: $tenant_id})
                    MATCH (obsolete:IndustryRoot {customer_id: $customer_id, tenant_id: $tenant_id, industry_id: $industry_id})
                    OPTIONAL MATCH (domain:DomainRoot)-[:PART_OF]->(obsolete)
                    WITH customer, obsolete, collect(domain) AS domains
                    FOREACH (domain IN domains | MERGE (domain)-[:PART_OF]->(customer))
                    DETACH DELETE obsolete
                """, customer_id=customer_id, tenant_id=tenant_id, industry_id=industry_id)
            for operation, entity_id in node_specs:
                label = self._safe_label(operation.entity_type, "node type")
                params = {**base_props, "entity_id": entity_id, "node_type": label}
                if operation.operation == "CREATE":
                    params.update({
                        "name": operation.name,
                        "name_display": operation.name,
                        "description": str(operation.properties.get("description") or ""),
                    })
                    tx.run(f"""
                        MERGE (n:`{label}` {{entity_id: $entity_id}})
                        ON CREATE SET n.name = $name, n.name_display = $name_display,
                                      n.description = CASE WHEN $description = '' THEN null ELSE $description END,
                                      n.kg_node_type = $node_type, n.created_at = datetime()
                        SET n += $base_props, n.updated_at = datetime()
                    """, entity_id=entity_id, name=operation.name, name_display=operation.name,
                        description=params["description"], node_type=label,
                        base_props={k: v for k, v in params.items() if k not in {"name", "name_display", "description", "node_type"}})
                else:
                    exists = tx.run(f"""
                        MATCH (n:`{label}` {{entity_id: $entity_id}})
                        WHERE n.customer_id = $customer_id AND n.tenant_id = $tenant_id
                          AND n.domain_id = $domain_id AND n.kg_node_type = $node_type
                        RETURN n.entity_id AS entity_id
                    """, entity_id=entity_id, customer_id=customer_id, tenant_id=tenant_id,
                        domain_id=domain_id, node_type=label).single()
                    if not exists:
                        raise ValueError("Selected reuse candidate no longer exists in this customer/domain scope")
                if industry_id and label not in {"CustomerRoot", "DomainRoot", "ProductLine", "Function", "SubFunction"}:
                    tx.run(f"""
                        MATCH (n:`{label}` {{entity_id: $entity_id}})
                        MATCH (pl:ProductLine {{customer_id: $customer_id, product_line_id: $product_line_id}})
                        WHERE n.customer_id = $customer_id AND n.tenant_id = $tenant_id
                          AND n.domain_id = $domain_id AND pl.domain_id = $domain_id
                        MERGE (n)-[:PART_OF]->(pl)
                        MERGE (n)-[:NODE_IN_DOMAIN]->(:DomainRoot {{customer_id: $customer_id, domain: $domain_id}})
                    """, entity_id=entity_id, customer_id=customer_id, tenant_id=tenant_id,
                        domain_id=domain_id, product_line_id=product_line_id)
                if operation.properties.get("aliases"):
                    alias_value = str(operation.properties["aliases"][0]).strip()
                    if not alias_value:
                        raise ValueError("Approved alias cannot be blank")
                    alias_provenance = plan.execution_metadata.get("approved_alias_provenance") or {}
                    stored = tx.run(f"""
                        MATCH (n:`{label}` {{entity_id: $entity_id}})
                        WHERE n.customer_id = $customer_id AND n.tenant_id = $tenant_id AND n.domain_id = $domain_id
                        RETURN n.alias_provenance_json AS alias_provenance_json
                    """, entity_id=entity_id, customer_id=customer_id,
                        tenant_id=tenant_id, domain_id=domain_id).single()
                    if not stored:
                        raise ValueError("Alias target no longer exists in the customer/domain scope")
                    try:
                        prior = json.loads(stored.get("alias_provenance_json") or "[]")
                    except (TypeError, ValueError):
                        prior = []
                    if not isinstance(prior, list):
                        prior = []
                    provenance_key = (alias_value.casefold(), str(alias_provenance.get("review_id") or ""))
                    if not any((str(record.get("alias") or "").casefold(), str(record.get("review_id") or "")) == provenance_key for record in prior if isinstance(record, dict)):
                        prior.append(alias_provenance)
                    tx.run(f"""
                        MATCH (n:`{label}` {{entity_id: $entity_id}})
                        WHERE n.customer_id = $customer_id AND n.tenant_id = $tenant_id AND n.domain_id = $domain_id
                        SET n.aliases = CASE WHEN $alias IN coalesce(n.aliases, []) THEN coalesce(n.aliases, []) ELSE coalesce(n.aliases, []) + $alias END,
                            n.alias_scope = 'CUSTOMER_DOMAIN', n.alias_provenance_json = $provenance_json,
                            n.updated_at = datetime()
                    """, entity_id=entity_id, customer_id=customer_id, tenant_id=tenant_id,
                        domain_id=domain_id, alias=alias_value,
                        provenance_json=json.dumps(prior, sort_keys=True))
                    counts["aliases"] += 1
                counts["nodes"] += 1
            for operation, source_id, target_id, source_type, target_type in relationships:
                rel_type = self._safe_label(operation.rel_type, "relationship type")
                source_label = self._safe_label(source_type, "relationship source type")
                target_label = self._safe_label(target_type, "relationship target type")
                result = tx.run(f"""
                    MATCH (s:`{source_label}` {{entity_id: $source_id}}), (t:`{target_label}` {{entity_id: $target_id}})
                    WHERE s.customer_id = $customer_id AND s.tenant_id = $tenant_id AND s.domain_id = $domain_id
                      AND t.customer_id = $customer_id AND t.tenant_id = $tenant_id AND t.domain_id = $domain_id
                      AND s.kg_node_type = $source_type AND t.kg_node_type = $target_type
                    MERGE (s)-[r:`{rel_type}`]->(t)
                    SET r.customer_id = $customer_id, r.tenant_id = $tenant_id, r.domain_id = $domain_id,
                        r.ingestion_batch_ids = CASE WHEN $batch_id IN coalesce(r.ingestion_batch_ids, []) THEN coalesce(r.ingestion_batch_ids, []) ELSE coalesce(r.ingestion_batch_ids, []) + $batch_id END,
                        r.ingestion_product_line_ids = CASE WHEN $product_line_id IN coalesce(r.ingestion_product_line_ids, []) THEN coalesce(r.ingestion_product_line_ids, []) ELSE coalesce(r.ingestion_product_line_ids, []) + $product_line_id END,
                        r.source_document_ids = CASE WHEN $document_id IS NULL OR $document_id IN coalesce(r.source_document_ids, []) THEN coalesce(r.source_document_ids, []) ELSE coalesce(r.source_document_ids, []) + $document_id END,
                        r.source_blocks = reduce(s = coalesce(r.source_blocks, []), x IN $source_blocks | CASE WHEN x IN s THEN s ELSE s + x END),
                        r.source_files = CASE WHEN $source_file IS NULL OR $source_file IN coalesce(r.source_files, []) THEN coalesce(r.source_files, []) ELSE coalesce(r.source_files, []) + $source_file END,
                        r.customer_ontology_ids = CASE WHEN $customer_ontology_id IS NULL OR $customer_ontology_id IN coalesce(r.customer_ontology_ids, []) THEN coalesce(r.customer_ontology_ids, []) ELSE coalesce(r.customer_ontology_ids, []) + $customer_ontology_id END,
                        r.customer_ontology_versions = CASE WHEN $customer_ontology_version IS NULL OR $customer_ontology_version IN coalesce(r.customer_ontology_versions, []) THEN coalesce(r.customer_ontology_versions, []) ELSE coalesce(r.customer_ontology_versions, []) + $customer_ontology_version END,
                        r.canonical_ontology_versions = CASE WHEN $canonical_ontology_version IS NULL OR $canonical_ontology_version IN coalesce(r.canonical_ontology_versions, []) THEN coalesce(r.canonical_ontology_versions, []) ELSE coalesce(r.canonical_ontology_versions, []) + $canonical_ontology_version END,
                        r.industry_extraction_policy_versions = CASE WHEN $policy_version IS NULL OR $policy_version IN coalesce(r.industry_extraction_policy_versions, []) THEN coalesce(r.industry_extraction_policy_versions, []) ELSE coalesce(r.industry_extraction_policy_versions, []) + $policy_version END,
                        r.project_ids = reduce(s = coalesce(r.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                        r.context_tags = reduce(s = coalesce(r.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END),
                        r.updated_at = datetime()
                    RETURN elementId(r) AS relationship_id
                """, source_id=source_id, target_id=target_id, customer_id=customer_id,
                    tenant_id=tenant_id, domain_id=domain_id, source_type=source_type,
                    target_type=target_type, batch_id=batch_id, product_line_id=product_line_id,
                    document_id=doc_id, source_blocks=source_block_ids, source_file=source_file or None,
                    customer_ontology_id=scope.get("customer_ontology_id"),
                    customer_ontology_version=scope.get("customer_ontology_version"),
                    canonical_ontology_version=scope.get("canonical_ontology_version"),
                    policy_version=scope.get("industry_extraction_policy_version"),
                    project_ids=document.get("project_ids") or [], context_tags=document.get("context_tags") or []).single()
                if not result:
                    raise ValueError("Relationship endpoints are missing or outside the frozen customer/domain scope")
                counts["relationships"] += 1
            return counts

        with self.driver.session() as session:
            return session.execute_write(write_tx)

    def close(self) -> None:
        if self._owns_driver:
            self.driver.close()

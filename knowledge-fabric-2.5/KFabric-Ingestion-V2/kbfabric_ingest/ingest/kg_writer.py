"""Neo4j knowledge graph writer.

This module handles writing enriched blocks to Neo4j with retry logic
and connection pooling for reliability and performance.
"""

import hashlib
import json
import re
import time
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from kbfabric_ingest._common.telemetry import get_tracer, record_neo4j_write, record_neo4j_node, record_neo4j_relationship, record_ie_entity_filtered
from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.ingest.models import EnrichedBlock
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import log_stage_start, log_stage_end, log_stage_error, redact_uri
from kbfabric_ingest.utils.retry import retry_with_backoff, circuit_breaker
from kbfabric_ingest.constants import DatabaseConstants, RetryConstants

logger = get_logger(__name__)


class KGWriter:
    """Writer for storing EnrichedBlocks as Neo4j nodes and relationships.
    
    Features:
    - Connection pooling for performance
    - Retry with exponential backoff for reliability
    - Configurable pool settings via kbfabric.yaml
    """

    def __init__(self, config: dict[str, Any], llm_client=None):
        """
        Initialize KG writer with connection pooling and retry support.
        
        Args:
            config: Neo4j configuration dictionary. Supports optional 'connection_pool'
                    section for pool tuning:
                    - max_size: Maximum connections (default: 50)
                    - acquisition_timeout: Timeout waiting for connection (default: 60s)
                    - connection_timeout: Timeout establishing connection (default: 30s)
                    - max_lifetime: Max connection lifetime in seconds (default: 3600)
            ontology: Ontology dictionary
            llm_client: Optional LLM client for generating entity embeddings
        """
        self.config = config
        self.llm_client = llm_client  # For generating entity embeddings
        self._run_context = None
        self.logger = get_logger(__name__)

        self.uri = config["uri"]
        self.database = config.get("database", "neo4j")
        self.user = config.get("user") or config.get("username")
        self.password = config.get("password")

        import logging
        logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

        pool_config = config["connection_pool"]
        self._pool_config = {
            "max_size": pool_config["max_size"],
            "acquisition_timeout": pool_config["acquisition_timeout"],
            "connection_timeout": pool_config["connection_timeout"],
            "max_lifetime": pool_config["max_lifetime"],
        }

        app_config = get_app_config()
        self.lean_mode = app_config["graph"]["lean_mode"]
        self.logger.info("KG Writer initialized", lean_mode=self.lean_mode)

        self.driver = self._create_driver_with_retry()

    def _runtime_ontology(self) -> dict[str, Any]:
        if self._run_context is None:
            raise RuntimeError("KGWriter run context has not been configured")
        from kbfabric_ingest.ingest.lane2_two_pass import build_lane2_ontology

        runtime = self._run_context.ontology
        return build_lane2_ontology(
            customer_ontology=runtime.domain_definition,
            canonical_model=runtime.canonical_model,
            extraction_policy=runtime.extraction_policy,
        )

    def _run_product_line_name(self) -> str | None:
        if self._run_context is None:
            raise RuntimeError("KGWriter run context has not been configured")
        return self._run_context.ontology.product_line_name

    def configure_run_context(self, run_context) -> None:
        """Configure the immutable authoritative context for one ingestion run."""
        from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext

        if not isinstance(run_context, IngestionRunContext):
            raise TypeError("run_context must be an IngestionRunContext")

        self._run_context = run_context
        self.logger.info(
            "kg_writer.run_context.configured",
            tenant_id=run_context.tenant_context.tenant_id,
            customer_id=run_context.tenant_context.customer_id,
            domain_id=run_context.ontology.domain_id,
            product_line_id=run_context.ontology.product_line_id,
            customer_ontology_version=run_context.ontology.customer_ontology_version,
        )



    @retry_with_backoff(
        max_retries=RetryConstants.DB_CONNECTION_MAX_RETRIES,
        base_delay=RetryConstants.DEFAULT_BASE_DELAY_SEC,
        max_delay=RetryConstants.DEFAULT_MAX_DELAY_SEC,
        exceptions=(ServiceUnavailable, ConnectionError, OSError)
    )
    def _create_driver_with_retry(self):
        """
        Create Neo4j driver with retry logic and connection pooling.
        
        Uses exponential backoff (3 attempts, ~3s total max wait) for
        transient connection failures.
        
        Returns:
            Neo4j driver instance with connection pool
        """
        self.logger.info(
            "Creating Neo4j driver with connection pool",
            uri=self.uri,
            pool_size=self._pool_config["max_size"],
            acquisition_timeout=self._pool_config["acquisition_timeout"]
        )
        
        return GraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password),
            max_connection_pool_size=self._pool_config["max_size"],
            connection_acquisition_timeout=self._pool_config["acquisition_timeout"],
            connection_timeout=self._pool_config["connection_timeout"],
            max_connection_lifetime=self._pool_config["max_lifetime"],
        )

    def clear_domain(self, domain: str) -> dict[str, any]:
        """
        Clear all nodes and relationships for a specific domain from Neo4j.
        
        Uses the existing authenticated driver connection.
        
        Args:
            domain: Domain name (e.g., "wealth_portfolio")
            
        Returns:
            dict with status and count of deleted items
        """
        try:
            with self.driver.session() as session:
                # Delete all nodes and relationships where domain matches
                result = session.run(
                    """
                    MATCH (n {domain: $domain})
                    DETACH DELETE n
                    RETURN count(n) as deleted
                    """,
                    domain=domain
                )
                deleted = result.single()["deleted"]
                self.logger.info("Cleared Neo4j nodes", domain=domain, deleted=deleted)
                return {"status": "cleared", "deleted": deleted}
        except Exception as e:
            self.logger.error("Failed to clear Neo4j nodes", domain=domain, error=str(e))
            return {"status": "error", "error": str(e)}

    def clear_product_line(
        self,
        *,
        customer_id: str,
        tenant_id: str,
        domain_id: str,
        product_line_id: str,
    ) -> dict[str, int | str]:
        """Remove one ingestion scope while preserving customer ontology spine and cross-line graph data."""
        scope = (customer_id, tenant_id, domain_id, product_line_id)
        if not all(isinstance(value, str) and value.strip() for value in scope):
            raise ValueError("Product-line cleanup requires customer, tenant, domain, and product-line IDs")

        params = {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "domain_id": domain_id,
            "product_line_id": product_line_id,
            "protected_labels": ["DomainRoot", "ProductLine", "Function", "SubFunction"],
        }
        counts: dict[str, int | str] = {"status": "cleared"}
        with self.driver.session() as session:
            # Blocks belong to one ingestion scope. Detaching removes only their evidence edges.
            row = session.run(
                """
                MATCH (b:Block)
                WHERE b.customer_id = $customer_id AND b.tenant_id = $tenant_id
                  AND b.domain_id = $domain_id
                  AND b.ingestion_product_line_id = $product_line_id
                WITH collect(b) AS blocks
                UNWIND blocks AS block
                DETACH DELETE block
                RETURN count(*) AS deleted
                """,
                **params,
            ).single()
            counts["blocks_deleted"] = int(row["deleted"] if row else 0)

            # Remove only relationships whose recorded ingestion scope includes this PL.
            row = session.run(
                """
                MATCH ()-[r]->()
                WHERE r.customer_id = $customer_id AND r.tenant_id = $tenant_id
                  AND r.domain_id = $domain_id
                  AND $product_line_id IN coalesce(r.ingestion_product_line_ids, [])
                  AND size(coalesce(r.ingestion_product_line_ids, [])) = 1
                DELETE r
                RETURN count(*) AS deleted
                """,
                **params,
            ).single()
            counts["relationships_deleted"] = int(row["deleted"] if row else 0)

            row = session.run(
                """
                MATCH ()-[r]->()
                WHERE r.customer_id = $customer_id AND r.tenant_id = $tenant_id
                  AND r.domain_id = $domain_id
                  AND $product_line_id IN coalesce(r.ingestion_product_line_ids, [])
                  AND size(coalesce(r.ingestion_product_line_ids, [])) > 1
                SET r.ingestion_product_line_ids = [
                    value IN r.ingestion_product_line_ids WHERE value <> $product_line_id
                ]
                RETURN count(r) AS retained_shared_relationships
                """,
                **params,
            ).single()
            counts["shared_relationships_retained"] = int(
                row["retained_shared_relationships"] if row else 0
            )

            # Customer ProductLine/Function/SubFunction nodes are the persisted ontology spine.
            # Keep them intact; remove only this ingestion scope from extracted entities.
            row = session.run(
                """
                MATCH (n)
                WHERE n.customer_id = $customer_id AND n.tenant_id = $tenant_id
                  AND n.domain_id = $domain_id
                  AND $product_line_id IN coalesce(n.ingestion_product_line_ids, [])
                  AND none(label IN labels(n) WHERE label IN $protected_labels)
                WITH n, [
                    value IN coalesce(n.ingestion_product_line_ids, [])
                    WHERE value <> $product_line_id
                ] AS remaining_product_lines
                SET n.ingestion_product_line_ids = remaining_product_lines
                WITH n, remaining_product_lines
                OPTIONAL MATCH (n)-[remaining_relationship]-(other)
                WHERE NOT other:DomainRoot
                WITH n, remaining_product_lines, count(remaining_relationship) AS non_domain_relationships
                WHERE size(remaining_product_lines) = 0 AND non_domain_relationships = 0
                DETACH DELETE n
                RETURN count(*) AS orphan_entities_deleted
                """,
                **params,
            ).single()
            counts["orphan_entities_deleted"] = int(
                row["orphan_entities_deleted"] if row else 0
            )

            # Document records are shared by deterministic source identity. Retain the record
            # and remove only the scalar product-line tag when it matches this scope.
            row = session.run(
                """
                MATCH (d:Document)
                WHERE d.customer_id = $customer_id AND d.tenant_id = $tenant_id
                  AND d.domain_id = $domain_id
                  AND d.ingestion_product_line_id = $product_line_id
                REMOVE d.ingestion_product_line_id
                RETURN count(d) AS documents_unscoped
                """,
                **params,
            ).single()
            counts["documents_unscoped"] = int(row["documents_unscoped"] if row else 0)

        self.logger.info(
            "Cleared Neo4j product-line scope",
            customer_id=customer_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            product_line_id=product_line_id,
            counts=counts,
        )
        return counts

    _ENTITY_DISPLAY_MAX_LEN = 80
    _EXCLUDED_EMBEDDING_LABELS = frozenset({"Block", "Document", "DomainRoot"})

    @staticmethod
    def _embedding_on_create_clause(node_var: str, param: str) -> str:
        """Set embedding on CREATE only when a non-empty vector is provided."""
        return (
            f"{node_var}.embedding = CASE "
            f"WHEN {param} IS NOT NULL AND size({param}) > 0 THEN {param} "
            f"ELSE null END"
        )

    @staticmethod
    def _embedding_on_match_clause(node_var: str, param: str) -> str:
        """Preserve existing embeddings; fill in only when missing or empty."""
        return (
            f"{node_var}.embedding = CASE "
            f"WHEN {node_var}.embedding IS NULL OR size({node_var}.embedding) = 0 "
            f"THEN CASE WHEN {param} IS NOT NULL AND size({param}) > 0 THEN {param} ELSE {node_var}.embedding END "
            f"ELSE {node_var}.embedding END"
        )

    def _entity_embedding_text(
        self, entity_type: str, name: str, name_display: str | None = None,
    ) -> str:
        """Build stable, compact text for entity embedding."""
        display = (name_display or name or "").strip()
        label = (entity_type or "Entity").strip()
        if display:
            return f"{label}: {display}"
        return label

    def _embed_entity_texts(self, embedding_requests: dict[str, str]) -> dict[str, list[float]]:
        """Embed texts keyed by deterministic entity_id."""
        if not embedding_requests or not self.llm_client:
            return {}
        entity_ids = list(embedding_requests.keys())
        texts = [embedding_requests[eid] for eid in entity_ids]
        try:
            vectors = self.llm_client.embed_text(texts)
            return {
                eid: vec for eid, vec in zip(entity_ids, vectors)
                if isinstance(vec, list) and len(vec) > 0
            }
        except Exception as e:
            self.logger.warning("Failed to generate entity embeddings", error=str(e))
            return {}

    def _get_vector_index_labels(self) -> list[str]:
        """Labels that receive entity/chunk embeddings and need vector indexes."""
        ontology_labels = [
            nt.get("name")
            for nt in self._runtime_ontology().get("node_types", [])
            if nt.get("name") and nt.get("name") not in self._EXCLUDED_EMBEDDING_LABELS
        ]
        base_labels = [
            "Block", "ProductLine", "Function", "SubFunction",
            "Requirement", "TestCase", "TestScenario", "TestDataSet",
            "Defect", "Incident", "Control", "Regulation", "Process",
            "FormCapture", "Rule", "System", "Risk", "Actor",
        ]
        seen: set[str] = set()
        labels: list[str] = []
        for label in base_labels + ontology_labels:
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
        return labels

    @staticmethod
    def _normalize_entity_value(value: str) -> str:
        if not value:
            return ""
        normalized = value.strip().lower()
        normalized = re.sub(r'^(the|a|an)\s+', '', normalized)
        normalized = re.sub(r'\s+(the|a|an)$', '', normalized)
        normalized = re.sub(r'[.,;:!?"\(\)\[\]{}]', '', normalized)
        return ' '.join(normalized.split())

    def _sanitize_entity_display_value(self, value: str) -> str | None:
        if not value or not isinstance(value, str):
            return None
        raw = value.strip()
        if len(raw) < 2:
            return None
        if '=' in raw:
            return None
        if len(raw) > self._ENTITY_DISPLAY_MAX_LEN:
            raw = raw[:self._ENTITY_DISPLAY_MAX_LEN].rstrip()
            if len(raw) < 2:
                return None
        if not re.search(r'[a-zA-Z0-9]', raw):
            return None
        parts = [p.strip() for p in re.split(r'[,;]', raw) if p.strip()]
        if not parts:
            return None
        if len(parts) == 1:
            return raw if len(raw) >= 2 else None
        if any(len(p) == 1 for p in parts):
            return None
        best = max((p for p in parts if len(p) >= 2), key=len, default=None)
        return best if best else None

    @staticmethod
    def _normalize_to_camelcase(name: str, entity_type: str = "") -> str:
        if not name:
            return ""
        name = name.strip()
        id_types = {"TestCase", "Defect", "Incident"}
        if entity_type in id_types:
            if re.match(r'^[A-Za-z0-9_\-\.]+$', name) and len(name) <= 80:
                return name
            return ' '.join(name.split())[:80]
        sentence_types = {"UserStory", "Requirement", "AcceptanceCriteria"}
        if entity_type in sentence_types:
            return ' '.join(name.split())[:80]
        spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
        spaced = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', spaced)
        words = spaced.replace('-', ' ').replace('_', ' ').split()
        return ' '.join(w.capitalize() for w in words if w)[:80]

    def _compute_entity_id(
        self,
        domain: str,
        entity_type: str,
        normalized_value: str,
        spine_lookup: dict[tuple[str, str], str] | None = None,
        customer_id: str | None = None,
    ) -> str:
        if spine_lookup:
            spine_id = spine_lookup.get((entity_type, normalized_value))
            if spine_id:
                return spine_id
        from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_entity_id
        run_customer_id = customer_id or (
            self._run_context.tenant_context.customer_id if self._run_context else None
        )
        run_tenant_id = self._run_context.tenant_context.tenant_id if self._run_context else self.config.get("tenant_id")
        return scoped_entity_id(customer_scope_id(run_customer_id, run_tenant_id), domain, entity_type, normalized_value)

    def _collect_block_embedding_requests(self, blocks: list) -> dict[str, str]:
        """Collect entity_id -> embedding text for all blocks before Neo4j write."""
        from kbfabric_ingest.ingest.ontology_prompt import normalize_to_ontology, get_allowed_node_types

        allowed_node_types = get_allowed_node_types(self._runtime_ontology())
        domains = {
            block.block.domain for block in blocks
            if block and block.block and block.block.domain
        }
        spine_lookup = {d: self._get_spine_entity_ids(d) for d in domains}
        requests: dict[str, str] = {}

        for block in blocks:
            if not block or not block.block or not block.block.domain:
                continue
            domain = block.block.domain
            domain_spine = spine_lookup.get(domain, {})

            for entity in block.candidate_entities or []:
                entity_type = entity.get("type", "Entity")
                entity_value = self._sanitize_entity_display_value(entity.get("value", ""))
                if entity_value is None:
                    continue
                entity_type_normalized = normalize_to_ontology(
                    entity_type.strip(), allowed_node_types,
                )
                if entity_type_normalized.startswith("_"):
                    continue
                entity_value_camelcase = self._normalize_to_camelcase(
                    entity_value, entity_type_normalized,
                )
                entity_value_normalized = self._normalize_entity_value(entity_value_camelcase)
                if entity_type_normalized in ("Function", "SubFunction", "ProductLine"):
                    if domain_spine.get((entity_type_normalized, entity_value_normalized)):
                        continue
                entity_id = self._compute_entity_id(
                    domain, entity_type_normalized, entity_value_normalized, domain_spine,
                    customer_id=(block.block.metadata or {}).get("customer_id"),
                )
                requests[entity_id] = self._entity_embedding_text(
                    entity_type_normalized, entity_value_camelcase, entity_value,
                )

            for rel in block.candidate_relationships or []:
                from_type = rel.get("from_type", "Entity")
                to_type = rel.get("to_type", "Entity")
                from_value = self._sanitize_entity_display_value(rel.get("from_value", ""))
                to_value = self._sanitize_entity_display_value(rel.get("to_value", ""))
                if not (from_value and to_value):
                    continue
                from_type_normalized = normalize_to_ontology(
                    from_type.strip(), allowed_node_types,
                )
                to_type_normalized = normalize_to_ontology(
                    to_type.strip(), allowed_node_types,
                )
                if from_type_normalized.startswith("_") or to_type_normalized.startswith("_"):
                    continue
                from_value_camelcase = self._normalize_to_camelcase(
                    from_value, from_type_normalized,
                )
                to_value_camelcase = self._normalize_to_camelcase(
                    to_value, to_type_normalized,
                )
                from_value_normalized = self._normalize_entity_value(from_value_camelcase)
                to_value_normalized = self._normalize_entity_value(to_value_camelcase)
                customer_id = (block.block.metadata or {}).get("customer_id")
                from_id = rel.get("from_entity_id") or self._compute_entity_id(
                    domain, from_type_normalized, from_value_normalized, domain_spine,
                    customer_id=customer_id,
                )
                to_id = rel.get("to_entity_id") or self._compute_entity_id(
                    domain, to_type_normalized, to_value_normalized, domain_spine,
                    customer_id=customer_id,
                )
                requests[from_id] = self._entity_embedding_text(
                    from_type_normalized, from_value_camelcase, from_value,
                )
                requests[to_id] = self._entity_embedding_text(
                    to_type_normalized, to_value_camelcase, to_value,
                )

        return requests

    def get_entity_embedding_stats(self, domain: str) -> dict[str, int]:
        """Count KG entity embedding coverage for a domain."""
        query = """
        MATCH (e)
        WHERE e.domain = $domain
          AND e.kg_node_type IS NOT NULL
          AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
        RETURN
          count(e) AS total,
          sum(CASE WHEN e.embedding IS NULL THEN 1 ELSE 0 END) AS embedding_null,
          sum(CASE WHEN e.embedding IS NOT NULL AND size(e.embedding) = 0 THEN 1 ELSE 0 END) AS embedding_empty,
          sum(CASE WHEN e.embedding IS NOT NULL AND size(e.embedding) > 0 THEN 1 ELSE 0 END) AS embedding_populated
        """
        try:
            with self.driver.session() as session:
                record = session.run(query, domain=domain).single()
            if not record:
                return {"total": 0, "embedding_null": 0, "embedding_empty": 0, "embedding_populated": 0}
            return {
                "total": int(record["total"] or 0),
                "embedding_null": int(record["embedding_null"] or 0),
                "embedding_empty": int(record["embedding_empty"] or 0),
                "embedding_populated": int(record["embedding_populated"] or 0),
            }
        except Exception as e:
            self.logger.warning("Failed to query entity embedding stats", domain=domain, error=str(e))
            return {"total": 0, "embedding_null": 0, "embedding_empty": 0, "embedding_populated": 0}

    def log_entity_embedding_stats(self, domain: str, stage: str) -> dict[str, int]:
        """Log KG entity embedding coverage for diagnostics."""
        stats = self.get_entity_embedding_stats(domain)
        self.logger.info(
            "KG entity embedding stats",
            domain=domain,
            stage=stage,
            **stats,
        )
        print(
            f"[KG][embeddings][{stage}] domain={domain} "
            f"total={stats['total']} null={stats['embedding_null']} "
            f"empty={stats['embedding_empty']} populated={stats['embedding_populated']}",
            flush=True,
        )
        return stats

    def ensure_missing_entity_embeddings(
        self, domain: str, batch_size: int = 100, force: bool = False,
    ) -> dict[str, int]:
        """
        Backfill embeddings for KG entity nodes missing vectors in Neo4j.

        Excludes Block, Document, and DomainRoot nodes.
        """
        stats = {"scanned": 0, "embedded": 0, "skipped": 0, "failed": 0}
        if not self.llm_client:
            self.logger.warning("No LLM client; skipping entity embedding backfill", domain=domain)
            return stats

        find_query = """
        MATCH (e)
        WHERE e.domain = $domain
          AND e.kg_node_type IS NOT NULL
          AND e.name IS NOT NULL
          AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
          AND ($force OR e.embedding IS NULL OR size(e.embedding) = 0)
        RETURN e.entity_id AS entity_id,
               e.kg_node_type AS kg_node_type,
               coalesce(e.name_display, e.name) AS display_name
        """
        write_query = """
        UNWIND $rows AS row
        MATCH (e {entity_id: row.entity_id, domain: $domain})
        WHERE NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
        SET e.embedding = CASE
          WHEN $force OR e.embedding IS NULL OR size(e.embedding) = 0 THEN row.embedding
          ELSE e.embedding
        END,
        e.updated_at = datetime()
        """

        try:
            with self.driver.session() as session:
                rows = list(session.run(find_query, domain=domain, force=force))
                stats["scanned"] = len(rows)
                if not rows:
                    return stats

                for i in range(0, len(rows), batch_size):
                    batch = rows[i:i + batch_size]
                    requests = {
                        rec["entity_id"]: self._entity_embedding_text(
                            rec["kg_node_type"] or "Entity",
                            rec["display_name"] or "",
                        )
                        for rec in batch
                        if rec.get("entity_id")
                    }
                    stats["skipped"] += len(batch) - len(requests)
                    if not requests:
                        continue

                    vectors = self._embed_entity_texts(requests)
                    write_rows = []
                    for entity_id in requests:
                        vector = vectors.get(entity_id)
                        if vector:
                            write_rows.append({"entity_id": entity_id, "embedding": vector})
                        else:
                            stats["failed"] += 1

                    if write_rows:
                        session.run(write_query, rows=write_rows, domain=domain, force=force)
                        stats["embedded"] += len(write_rows)

            self.logger.info("Entity embedding backfill complete", domain=domain, **stats)
        except Exception as e:
            self.logger.warning(
                "ensure_missing_entity_embeddings failed (non-fatal)",
                domain=domain,
                error=str(e),
            )
            stats["failed"] = stats.get("failed", 0) + 1
        return stats

    def ensure_indexes(self) -> None:
        """Create vector indexes and property indexes for node labels."""
        indexes_obs_start = log_stage_start(self.logger, "neo4j.ensure_indexes",
                                            neo4j_uri=redact_uri(getattr(self, "uri", "")))
        with self.driver.session() as session:
            vector_labels = self._get_vector_index_labels()
            app_cfg = get_app_config()
            embedding_dim = app_cfg["embeddings"]["dimension"]
            neo_index = app_cfg["neo4j"]["vector_index"]
            similarity_function = neo_index["similarity_function"]

            for label in vector_labels:
                # Create vector index (Neo4j 5.x syntax)
                # Note: Vector indexes may require specific Neo4j version and configuration
                try:
                    create_query = f"""
                    CREATE VECTOR INDEX vector_{label.lower()}_embedding IF NOT EXISTS
                    FOR (n:{label})
                    ON n.embedding
                    OPTIONS {{
                        indexConfig: {{
                            `vector.dimensions`: {embedding_dim},
                            `vector.similarity_function`: '{similarity_function}'
                        }}
                    }}
                    """
                    session.run(create_query)
                    self.logger.info("Created vector index", label=label)
                except Exception as e:
                    # Vector indexes may not be available in all Neo4j versions
                    # Fall back to regular property index
                    self.logger.warning(
                        "Failed to create vector index, using property index",
                        label=label,
                        error=str(e)
                    )
                    try:
                        prop_query = f"""
                        CREATE INDEX {label.lower()}_embedding IF NOT EXISTS
                        FOR (n:{label})
                        ON (n.embedding)
                        """
                        session.run(prop_query)
                    except Exception as e2:
                        self.logger.warning("Failed to create property index", label=label, error=str(e2))
            
            # Create property indexes
            property_indexes = [
                ("Block", "block_id"),
                ("Block", "domain"),
                ("Block", "block_category"),
                ("Document", "source_file"),
                ("CustomerRoot", "customer_id"),
                ("CustomerRoot", "tenant_id"),
                ("DomainRoot", "domain"),
                ("DomainRoot", "customer_id"),
            ]
            
            for label, property_name in property_indexes:
                check_query = f"""
                SHOW INDEXES
                WHERE name = '{label.lower()}_{property_name}'
                """
                result = session.run(check_query)
                if not list(result):
                    create_query = f"""
                    CREATE INDEX {label.lower()}_{property_name} IF NOT EXISTS
                    FOR (n:{label})
                    ON (n.{property_name})
                    """
                    try:
                        session.run(create_query)
                        self.logger.info("Created property index", label=label, property=property_name)
                    except Exception as e:
                        self.logger.warning("Failed to create property index", label=label, property=property_name, error=str(e))

        log_stage_end(self.logger, "neo4j.ensure_indexes", indexes_obs_start)

    def validate_indexes(self) -> dict[str, Any]:
        """Validate expected Neo4j indexes are present and ONLINE."""
        required_indexes = []
        vector_labels = self._get_vector_index_labels()
        for label in vector_labels:
            required_indexes.append(f"vector_{label.lower()}_embedding")
            required_indexes.append(f"{label.lower()}_embedding")
        property_indexes = [
            ("Block", "block_id"),
            ("Block", "domain"),
            ("Block", "block_category"),
            ("Document", "source_file"),
            ("CustomerRoot", "customer_id"),
            ("CustomerRoot", "tenant_id"),
            ("DomainRoot", "domain"),
            ("DomainRoot", "customer_id"),
        ]
        for label, prop in property_indexes:
            required_indexes.append(f"{label.lower()}_{prop}")
        results: dict[str, Any] = {"indexes": {}, "status": "ok"}
        try:
            with self.driver.session() as session:
                rows = session.run("SHOW INDEXES YIELD name, state")
                index_states = {r["name"]: r["state"] for r in rows}
            for name in required_indexes:
                state = index_states.get(name)
                results["indexes"][name] = state or "MISSING"
                if state != "ONLINE":
                    results["status"] = "warning"
            return results
        except Exception as e:
            self.logger.warning("Neo4j index validation failed", error=str(e))
            return {"status": "error", "error": str(e)}

    def pre_seed_spine(self, domain: str, tenant_id: str = "default") -> dict[str, int]:
        """
        Pre-seed the domain spine (DomainRoot→ProductLine→Function→SubFunction)
        into Neo4j before IE processing. Uses MERGE for idempotency.

        Returns dict with counts of nodes created at each level.
        """
        ontology_domains = self._runtime_ontology().get("domains", [])
        domain_def = next((d for d in ontology_domains if d["name"] == domain), None)
        if not domain_def or "spine" not in domain_def:
            self.logger.warning("No spine defined for domain", domain=domain)
            return {"product_lines": 0, "functions": 0, "subfunctions": 0}

        spine = domain_def["spine"]
        run_context = self._run_context
        customer_id = (run_context.tenant_context.customer_id or tenant_id) if run_context else tenant_id
        # The frozen run context is authoritative. The legacy caller can still
        # pass a static tenant label (for example, "tenant bank 1"), which must
        # never be written onto customer-scoped spine nodes.
        tenant_id = run_context.tenant_context.tenant_id if run_context else tenant_id
        ontology_id = str(run_context.ontology.customer_ontology_id) if run_context else None
        customer_ontology_version = run_context.ontology.customer_ontology_version if run_context else None
        canonical_ontology_version = run_context.ontology.ontology_version if run_context else None
        industry_id = run_context.tenant_context.industry_id if run_context else None
        counts = {"product_lines": 0, "functions": 0, "subfunctions": 0}
        from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_entity_id
        scope_id = customer_scope_id(customer_id, tenant_id)

        embedding_requests: dict[str, str] = {}
        for pl_def in spine:
            pl_name = pl_def["ProductLine"]
            pl_id = scoped_entity_id(scope_id, domain, "ProductLine", pl_name.strip().lower())
            embedding_requests[pl_id] = self._entity_embedding_text("ProductLine", pl_name)
            for fn_def in pl_def.get("functions", []):
                fn_name = fn_def["Function"]
                fn_id = scoped_entity_id(scope_id, domain, "Function", fn_name.strip().lower())
                embedding_requests[fn_id] = self._entity_embedding_text("Function", fn_name)
                for sf_name in fn_def.get("subfunctions", []):
                    sf_id = scoped_entity_id(scope_id, domain, "SubFunction", sf_name.strip().lower())
                    embedding_requests[sf_id] = self._entity_embedding_text("SubFunction", sf_name)

        spine_embeddings = self._embed_entity_texts(embedding_requests)

        try:
            with self.driver.session() as session:
                def _seed(tx):
                    tx.run(
                        """
                        MERGE (customer:CustomerRoot {customer_id: $customer_id, tenant_id: $tenant_id})
                        ON CREATE SET customer.name = $customer_id,
                                      customer.kg_node_type = 'CustomerRoot',
                                      customer.layer = 'customer_structure',
                                      customer.created_at = datetime()
                        SET customer.industry_id = coalesce($industry_id, customer.industry_id),
                            customer.kg_node_type = 'CustomerRoot'
                        """,
                        customer_id=customer_id, tenant_id=tenant_id, industry_id=industry_id,
                    )
                    tx.run(
                        """
                        MERGE (dom:DomainRoot {domain: $domain, customer_id: $customer_id})
                        ON CREATE SET dom.created_at = datetime(), dom.tenant_id = $tenant_id,
                                      dom.customer_id = $customer_id, dom.domain_id = $domain,
                                      dom.industry_id = $industry_id,
                                      dom.customer_ontology_id = $ontology_id,
                                      dom.customer_ontology_version = $customer_ontology_version,
                                      dom.canonical_ontology_version = $canonical_ontology_version,
                                      dom.kg_node_type = 'DomainRoot'
                        ON MATCH SET dom.tenant_id = $tenant_id,
                                     dom.customer_id = coalesce($customer_id, dom.customer_id),
                                     dom.domain_id = $domain,
                                     dom.industry_id = coalesce($industry_id, dom.industry_id),
                                     dom.customer_ontology_id = coalesce($ontology_id, dom.customer_ontology_id),
                                     dom.customer_ontology_version = coalesce($customer_ontology_version, dom.customer_ontology_version),
                                     dom.canonical_ontology_version = coalesce($canonical_ontology_version, dom.canonical_ontology_version),
                                     dom.kg_node_type = 'DomainRoot'
                        """,
                        domain=domain, tenant_id=tenant_id, customer_id=customer_id,
                        industry_id=industry_id,
                        ontology_id=ontology_id, customer_ontology_version=customer_ontology_version,
                        canonical_ontology_version=canonical_ontology_version,
                    )
                    tx.run(
                        """
                        MATCH (customer:CustomerRoot {customer_id: $customer_id, tenant_id: $tenant_id})
                        MATCH (dom:DomainRoot {domain: $domain, customer_id: $customer_id})
                        MERGE (dom)-[:PART_OF]->(customer)
                        """,
                        customer_id=customer_id, tenant_id=tenant_id, domain=domain,
                    )
                    if industry_id:
                        # Narrow migration of the obsolete per-customer industry
                        # root; the domain now belongs to CustomerRoot.
                        tx.run(
                            """
                            MATCH (customer:CustomerRoot {customer_id: $customer_id, tenant_id: $tenant_id})
                            MATCH (obsolete:IndustryRoot {customer_id: $customer_id, tenant_id: $tenant_id, industry_id: $industry_id})
                            OPTIONAL MATCH (dom:DomainRoot)-[:PART_OF]->(obsolete)
                            WITH customer, obsolete, collect(dom) AS domains
                            FOREACH (dom IN domains | MERGE (dom)-[:PART_OF]->(customer))
                            DETACH DELETE obsolete
                            """,
                            customer_id=customer_id, tenant_id=tenant_id, industry_id=industry_id,
                        )

                    for pl_def in spine:
                        pl_name = pl_def["ProductLine"]
                        pl_id = scoped_entity_id(scope_id, domain, "ProductLine", pl_name.strip().lower())
                        pl_embedding = spine_embeddings.get(pl_id, [])
                        pl_emb_create = self._embedding_on_create_clause("pl", "$embedding")
                        pl_emb_match = self._embedding_on_match_clause("pl", "$embedding")
                        tx.run(
                            f"""
                            MERGE (pl:ProductLine {{entity_id: $pl_id}})
                            ON CREATE SET pl.name = $name, pl.domain = $domain,
                                          pl.domain_id = $domain, pl.product_line_id = $product_line_id,
                                          pl.customer_id = $customer_id, pl.customer_ontology_id = $ontology_id,
                                          pl.customer_ontology_version = $customer_ontology_version,
                                          pl.canonical_ontology_version = $canonical_ontology_version,
                                          pl.tenant_id = $tenant_id,
                                          pl.created_at = datetime(), pl.layer = 'domain_structure',
                                          pl.kg_node_type = 'ProductLine',
                                          {pl_emb_create}
                            ON MATCH SET pl.kg_node_type = 'ProductLine',
                                         pl.domain_id = $domain,
                                         pl.product_line_id = $product_line_id,
                                         pl.tenant_id = $tenant_id,
                                         pl.customer_id = coalesce($customer_id, pl.customer_id),
                                         pl.customer_ontology_id = coalesce($ontology_id, pl.customer_ontology_id),
                                         pl.customer_ontology_version = coalesce($customer_ontology_version, pl.customer_ontology_version),
                                         pl.canonical_ontology_version = coalesce($canonical_ontology_version, pl.canonical_ontology_version),
                                         {pl_emb_match}
                            WITH pl
                            MATCH (dom:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                            MERGE (pl)-[:PART_OF]->(dom)
                            MERGE (pl)-[:NODE_IN_DOMAIN]->(dom)
                            """,
                            pl_id=pl_id, name=pl_name, domain=domain, tenant_id=tenant_id,
                            customer_id=customer_id, ontology_id=ontology_id,
                            customer_ontology_version=customer_ontology_version,
                            canonical_ontology_version=canonical_ontology_version,
                            product_line_id=f"DOMAIN:{domain}|PRODUCT_LINE:{pl_name}",
                            embedding=pl_embedding,
                        )
                        counts["product_lines"] += 1

                        for fn_def in pl_def.get("functions", []):
                            fn_name = fn_def["Function"]
                            fn_id = scoped_entity_id(scope_id, domain, "Function", fn_name.strip().lower())
                            fn_embedding = spine_embeddings.get(fn_id, [])
                            fn_emb_create = self._embedding_on_create_clause("fn", "$embedding")
                            fn_emb_match = self._embedding_on_match_clause("fn", "$embedding")
                            tx.run(
                                f"""
                                MERGE (fn:Function {{entity_id: $fn_id}})
                                ON CREATE SET fn.name = $name, fn.domain = $domain,
                                              fn.domain_id = $domain, fn.product_line_id = $product_line_id,
                                              fn.customer_id = $customer_id, fn.customer_ontology_id = $ontology_id,
                                              fn.customer_ontology_version = $customer_ontology_version,
                                              fn.canonical_ontology_version = $canonical_ontology_version,
                                              fn.tenant_id = $tenant_id,
                                              fn.created_at = datetime(), fn.layer = 'domain_structure',
                                              fn.kg_node_type = 'Function',
                                              {fn_emb_create}
                                ON MATCH SET fn.kg_node_type = 'Function',
                                             fn.domain_id = $domain,
                                             fn.product_line_id = $product_line_id,
                                             fn.tenant_id = $tenant_id,
                                             fn.customer_id = coalesce($customer_id, fn.customer_id),
                                             fn.customer_ontology_id = coalesce($ontology_id, fn.customer_ontology_id),
                                             fn.customer_ontology_version = coalesce($customer_ontology_version, fn.customer_ontology_version),
                                             fn.canonical_ontology_version = coalesce($canonical_ontology_version, fn.canonical_ontology_version),
                                             {fn_emb_match}
                                WITH fn
                                MATCH (pl:ProductLine {{entity_id: $pl_id}})
                                MERGE (fn)-[:PART_OF]->(pl)
                                WITH fn
                                MATCH (dom:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                                MERGE (fn)-[:NODE_IN_DOMAIN]->(dom)
                                """,
                                fn_id=fn_id, name=fn_name, domain=domain,
                                tenant_id=tenant_id, pl_id=pl_id,
                                customer_id=customer_id, ontology_id=ontology_id,
                                customer_ontology_version=customer_ontology_version,
                                canonical_ontology_version=canonical_ontology_version,
                                product_line_id=f"DOMAIN:{domain}|PRODUCT_LINE:{pl_name}",
                                embedding=fn_embedding,
                            )
                            counts["functions"] += 1

                            for sf_name in fn_def.get("subfunctions", []):
                                sf_id = scoped_entity_id(scope_id, domain, "SubFunction", sf_name.strip().lower())
                                sf_embedding = spine_embeddings.get(sf_id, [])
                                sf_emb_create = self._embedding_on_create_clause("sf", "$embedding")
                                sf_emb_match = self._embedding_on_match_clause("sf", "$embedding")
                                tx.run(
                                    f"""
                                    MERGE (sf:SubFunction {{entity_id: $sf_id}})
                                    ON CREATE SET sf.name = $name, sf.domain = $domain,
                                                  sf.domain_id = $domain, sf.product_line_id = $product_line_id,
                                                  sf.customer_id = coalesce($customer_id, sf.customer_id), sf.customer_ontology_id = $ontology_id,
                                                  sf.customer_ontology_version = $customer_ontology_version,
                                                  sf.canonical_ontology_version = $canonical_ontology_version,
                                                  sf.tenant_id = $tenant_id,
                                                  sf.created_at = datetime(), sf.layer = 'domain_structure',
                                                  sf.kg_node_type = 'SubFunction',
                                                  {sf_emb_create}
                                    ON MATCH SET sf.kg_node_type = 'SubFunction',
                                                 sf.domain_id = $domain,
                                                 sf.product_line_id = $product_line_id,
                                                 sf.tenant_id = $tenant_id,
                                                 sf.customer_id = coalesce($customer_id, sf.customer_id),
                                                 sf.customer_ontology_id = coalesce($ontology_id, sf.customer_ontology_id),
                                                 sf.customer_ontology_version = coalesce($customer_ontology_version, sf.customer_ontology_version),
                                                 sf.canonical_ontology_version = coalesce($canonical_ontology_version, sf.canonical_ontology_version),
                                                 {sf_emb_match}
                                    WITH sf
                                    MATCH (fn:Function {{entity_id: $fn_id}})
                                    MERGE (sf)-[:PART_OF]->(fn)
                                    WITH sf
                                    MATCH (dom:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                                    MERGE (sf)-[:NODE_IN_DOMAIN]->(dom)
                                    """,
                                    sf_id=sf_id, name=sf_name, domain=domain,
                                    tenant_id=tenant_id, fn_id=fn_id,
                                    customer_id=customer_id, ontology_id=ontology_id,
                                    customer_ontology_version=customer_ontology_version,
                                    canonical_ontology_version=canonical_ontology_version,
                                    product_line_id=f"DOMAIN:{domain}|PRODUCT_LINE:{pl_name}",
                                    embedding=sf_embedding,
                                )
                                counts["subfunctions"] += 1

                session.execute_write(_seed)
            self.logger.info("Pre-seeded spine", domain=domain, counts=counts)
        except Exception as e:
            self.logger.error("Failed to pre-seed spine", domain=domain, error=str(e))
            raise
        return counts

    def _get_spine_entity_ids(self, domain: str) -> dict[tuple[str, str], str]:
        """
        Build lookup (label, name_lower) -> entity_id for spine nodes.
        Used to resolve LLM-extracted Function/SubFunction/ProductLine to existing spine
        and prevent duplicate nodes.
        """
        import hashlib
        result: dict[tuple[str, str], str] = {}
        from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_entity_id
        run_context = self._run_context
        customer_id = run_context.tenant_context.customer_id if run_context else None
        tenant_id = run_context.tenant_context.tenant_id if run_context else self.config.get("tenant_id")
        scope_id = customer_scope_id(customer_id, tenant_id)
        domain_def = next((d for d in self._runtime_ontology().get("domains", []) if d.get("name") == domain), None)
        if not domain_def or "spine" not in domain_def:
            return result
        for pl_def in domain_def["spine"]:
            pl_name = pl_def.get("ProductLine", "")
            if pl_name:
                eid = scoped_entity_id(scope_id, domain, "ProductLine", pl_name.strip().lower())
                result[("ProductLine", pl_name.strip().lower())] = eid
            for fn_def in pl_def.get("functions", []):
                fn_name = fn_def.get("Function", "")
                if fn_name:
                    eid = scoped_entity_id(scope_id, domain, "Function", fn_name.strip().lower())
                    result[("Function", fn_name.strip().lower())] = eid
                for sf_name in fn_def.get("subfunctions", []):
                    if sf_name:
                        eid = scoped_entity_id(scope_id, domain, "SubFunction", sf_name.strip().lower())
                        result[("SubFunction", sf_name.strip().lower())] = eid
        return result

    @circuit_breaker(failure_threshold=3, recovery_timeout=120, service_name="neo4j_write")
    def write_blocks(
        self, blocks: list[EnrichedBlock], kb_ids: dict[str, str], *,
        write_unit_callback=None,
    ) -> dict[str, Any]:
        """
        Write enriched blocks to Neo4j as nodes and relationships.
        
        Uses batched transactions (50 blocks per batch) to prevent timeouts on large documents.
        
        Args:
            blocks: List of EnrichedBlock instances
            kb_ids: Dictionary mapping block.block.block_id to Weaviate UUID (chunk_id)
        """
        if not blocks:
            self.logger.warning("No blocks to write to Neo4j")
            return {"nodes": 0, "relationships": 0}
        
        # Get domain for telemetry
        domain = blocks[0].block.domain if blocks and blocks[0].block else "unknown"
        tracer = get_tracer()
        start_time = time.time()
        write_obs_start = log_stage_start(
            self.logger, "neo4j.write", domain=domain,
            block_count=len(blocks), neo4j_uri=redact_uri(getattr(self, "uri", "")),
        )
        
        # BATCH CONFIGURATION: Read from config with fallback to default (50)
        BATCH_SIZE = self.config["batch_size"]
        total_batches = (len(blocks) + BATCH_SIZE - 1) // BATCH_SIZE
        self.logger.info("Starting batched Neo4j write", total_blocks=len(blocks), batch_size=BATCH_SIZE, total_batches=total_batches)
        
        self.logger.info("Starting Neo4j write", block_count=len(blocks), kb_ids_count=len(kb_ids))
        
        # Pre-compute entity embeddings BEFORE the transaction (keyed by entity_id)
        entity_embeddings: dict[str, list[float]] = {}
        if self.llm_client:
            embedding_requests = self._collect_block_embedding_requests(blocks)
            if embedding_requests:
                self.logger.info(
                    "Generating embeddings for entity nodes",
                    count=len(embedding_requests),
                )
                entity_embeddings = self._embed_entity_texts(embedding_requests)
                self.logger.info(
                    "Entity embeddings generated",
                    count=len(entity_embeddings),
                )
        
        try:
            with self.driver.session() as session:
                # NOTE: Unused write_transaction function (590 lines) was deleted
                # It was never called - batch_write_transaction is the actual code path
                
                # Process blocks in batches to prevent transaction timeouts
                total_nodes = 0
                total_nodes_merged = 0
                total_relationships = 0
                all_errors = []
                
                # Calculate total entities processed for deduplication stats
                total_entities_processed = sum(
                    len(block.candidate_entities) if block and block.candidate_entities else 0
                    for block in blocks
                )
                
                for batch_idx in range(total_batches):
                    batch_start = batch_idx * BATCH_SIZE
                    batch_end = min(batch_start + BATCH_SIZE, len(blocks))
                    batch_blocks = blocks[batch_start:batch_end]
                    import hashlib
                    batch_unit_digest = hashlib.sha256(
                        "\n".join(sorted(
                            block.block.block_id for block in batch_blocks
                            if block and block.block
                        )).encode("utf-8")
                    ).hexdigest()[:24]
                    batch_unit_key = f"blocks:{batch_idx + 1}:{batch_unit_digest}"
                    if write_unit_callback:
                        write_unit_callback("neo4j", batch_unit_key, "IN_PROGRESS", {
                            "batch": batch_idx + 1, "total_batches": total_batches,
                            "block_count": len(batch_blocks),
                        }, None)
                    
                    # Create a closure with the current batch
                    def create_batch_writer(batch):
                        domains_in_batch = {block.block.domain for block in batch if block.block and block.block.domain}
                        run_customer_id = (
                            self._run_context.tenant_context.customer_id
                            if self._run_context else None
                        )
                        run_tenant_id = (
                            self._run_context.tenant_context.tenant_id
                            if self._run_context else get_app_config()["tenant_id"]
                        )
                        batch_customer_id = run_customer_id or next((
                            (block.block.metadata or {}).get("customer_id")
                            for block in batch if block and block.block and (block.block.metadata or {}).get("customer_id")
                        ), None) or run_tenant_id
                        spine_lookup = {d: self._get_spine_entity_ids(d) for d in domains_in_batch}

                        def batch_write_transaction(tx):
                            # Use the same write_transaction logic but with batch instead of all blocks
                            nodes_created = 0
                            nodes_merged = 0  # Track entities that already existed (deduplication)
                            relationships_created = 0
                            errors = []
                            entity_collisions = []  # Track entity ID collisions for logging
                            
                            doc_domain_pairs_created = set()
                            domains = domains_in_batch
                            
                            for domain in domains:
                                try:
                                    domain_query = """
                                    MERGE (dom:DomainRoot {domain: $domain, customer_id: $customer_id})
                                    ON CREATE SET dom.created_at = datetime()
                                    """
                                    tx.run(domain_query, domain=domain, customer_id=batch_customer_id)
                                except Exception as e:
                                    errors.append(f"Failed to create DomainRoot for {domain}: {str(e)}")
                            
                            # Process the batch (same logic as write_transaction but for batch)
                            for idx, block in enumerate(batch):
                                try:
                                    if not block or not block.block:
                                        continue
                                    domain = block.block.domain
                                    if not domain:
                                        continue
                                    chunk_id = kb_ids.get(block.block.block_id, "")
                                    source_block_ids = block.block.source_block_ids or [block.block.block_id]
                                    semantic_unit_id = block.block.semantic_unit_id or block.block.block_id
                                    block_metadata = block.block.metadata or {}
                                    entity_tenant_id = block_metadata.get("tenant_id") or get_app_config()["tenant_id"]
                                    from kbfabric_ingest.ingest.ingestion_metadata import document_id_for_source
                                    from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_block_id
                                    scope_id = customer_scope_id(
                                        block_metadata.get("customer_id"),
                                        block_metadata.get("tenant_id") or get_app_config()["tenant_id"],
                                    )
                                    stored_block_id = scoped_block_id(scope_id, block.block.block_id)
                                    document_id = document_id_for_source(
                                        scope_id, block.block.source_file or ""
                                    )
                                    evidence_context = json.dumps({
                                        "ingestion_batch_id": block_metadata.get("ingestion_batch_id"),
                                        "tenant_id": block_metadata.get("tenant_id"),
                                        "customer_id": block_metadata.get("customer_id") or entity_tenant_id,
                                        "domain_id": block_metadata.get("domain_id", domain),
                                        "ingestion_product_line_id": block_metadata.get("ingestion_product_line_id"),
                                        "customer_ontology_id": block_metadata.get("customer_ontology_id"),
                                        "customer_ontology_version": block_metadata.get("customer_ontology_version"),
                                        "canonical_ontology_version": block_metadata.get("canonical_ontology_version"),
                                        "industry_extraction_policy_version": block_metadata.get("industry_extraction_policy_version"),
                                        "document_id": document_id,
                                        "source_file": block.block.source_file,
                                        "source_page": block.block.page_number,
                                        "source_block_ids": source_block_ids,
                                        "project_ids": block_metadata.get("project_ids", []),
                                        "context_tags": block_metadata.get("context_tags", []),
                                        "document_nature": block_metadata.get("document_nature"),
                                    }, sort_keys=True)
                                    # Ensure every entity/relationship has source_file; report shows "Unknown source" when null
                                    _source_file = (block.block.source_file or "").strip() or "unknown"
                                    from datetime import datetime, timezone
                                    now_iso = datetime.now(timezone.utc).isoformat()
                                    
                                    # LEAN MODE: Skip Document/Block scaffolding
                                    if not self.lean_mode:
                                        # Create Document node
                                        doc_query = """
                                        MERGE (d:Document {document_id: $document_id})
                                        ON CREATE SET d.created_at = datetime(), d.domain = $domain
                                        ON MATCH SET d.domain = $domain
                                        SET d.source_file = $source_file,
                                            d.tenant_id = $tenant_id,
                                            d.customer_id = $customer_id,
                                            d.domain_id = $domain_id,
                                            d.ingestion_product_line_id = $product_line_id,
                                            d.customer_ontology_id = $customer_ontology_id,
                                            d.customer_ontology_version = $customer_ontology_version,
                                            d.canonical_ontology_version = $canonical_ontology_version,
                                            d.industry_extraction_policy_version = $policy_version,
                                            d.document_nature = $document_nature,
                                            d.project_ids = $project_ids,
                                            d.context_tags = $context_tags,
                                            d.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(d.ingestion_batch_ids, []) THEN coalesce(d.ingestion_batch_ids, []) ELSE coalesce(d.ingestion_batch_ids, []) + $ingestion_batch_id END
                                        RETURN id(d) as doc_id
                                        """
                                        doc_result = tx.run(
                                            doc_query,
                                            source_file=_source_file,
                                            document_id=document_id,
                                            domain=domain,
                                            tenant_id=entity_tenant_id,
                                            customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                            domain_id=block_metadata.get("domain_id", domain),
                                            product_line_id=block_metadata.get("ingestion_product_line_id"),
                                            customer_ontology_id=block_metadata.get("customer_ontology_id"),
                                            customer_ontology_version=block_metadata.get("customer_ontology_version"),
                                            canonical_ontology_version=block_metadata.get("canonical_ontology_version"),
                                            policy_version=block_metadata.get("industry_extraction_policy_version"),
                                            document_nature=block_metadata.get("document_nature"),
                                            project_ids=block_metadata.get("project_ids", []),
                                            context_tags=block_metadata.get("context_tags", []),
                                            ingestion_batch_id=block_metadata.get("ingestion_batch_id"),
                                        )
                                        doc_record = doc_result.single()
                                        if not doc_record:
                                            continue
                                        
                                        # Link Document to DomainRoot
                                        doc_domain_pair = (_source_file, domain)
                                        doc_domain_rel_query = """
                                        MATCH (d:Document {document_id: $document_id})
                                        MATCH (dom:DomainRoot {domain: $domain, customer_id: $customer_id})
                                        MERGE (d)-[:DOCUMENT_IN_DOMAIN]->(dom)
                                        """
                                        tx.run(doc_domain_rel_query, document_id=document_id, domain=domain, customer_id=scope_id)
                                        if doc_domain_pair not in doc_domain_pairs_created:
                                            doc_domain_pairs_created.add(doc_domain_pair)
                                            relationships_created += 1
                                        
                                        # Create Block node
                                        block_props = {
                                            "block_id": stored_block_id,
                                            "source_block_id": block.block.block_id,
                                            "document_id": document_id,
                                            "ingestion_batch_id": block_metadata.get("ingestion_batch_id"),
                                            "ingestion_batch_ids": [block_metadata["ingestion_batch_id"]] if block_metadata.get("ingestion_batch_id") else [],
                                            "tenant_id": block_metadata.get("tenant_id") or get_app_config()["tenant_id"],
                                            "customer_id": block_metadata.get("customer_id") or entity_tenant_id,
                                            "domain": domain,
                                            "domain_id": block_metadata.get("domain_id", domain),
                                            "ingestion_product_line_id": block_metadata.get("ingestion_product_line_id"),
                                            "customer_ontology_id": block_metadata.get("customer_ontology_id"),
                                            "customer_ontology_version": block_metadata.get("customer_ontology_version"),
                                            "canonical_ontology_version": block_metadata.get("canonical_ontology_version"),
                                            "industry_extraction_policy_version": block_metadata.get("industry_extraction_policy_version"),
                                            "project_ids": block_metadata.get("project_ids", []),
                                            "context_tags": block_metadata.get("context_tags", []),
                                            "raw_project_ids": block_metadata.get("raw_project_ids"),
                                            "raw_context_tags": block_metadata.get("raw_context_tags"),
                                            "document_nature": block_metadata.get("document_nature"),
                                            "block_category": block.block_category.value,
                                            "kg_node_type": block.kg_node_type,
                                            "masked_text": block.masked_text[:200] if len(block.masked_text) > 200 else block.masked_text,
                                            "block_type": block.block.block_type,
                                            "source_file": _source_file,
                                            "file_type": block.block.file_type,
                                            "page_number": block.block.page_number,
                                            "pdf_page_number": (block.block.metadata or {}).get("pdf_page_number"),
                                            "source_docx_file": (block.block.metadata or {}).get("source_docx_file"),
                                            "converted_pdf_file": (block.block.metadata or {}).get("converted_pdf_file"),
                                            "conversion_method": (block.block.metadata or {}).get("conversion_method"),
                                            "conversion_timestamp": (block.block.metadata or {}).get("conversion_timestamp"),
                                            "block_index": block.block.block_index,
                                            "metadata": str(block.block.metadata),
                                            "semantic_unit_id": semantic_unit_id,
                                            "source_block_ids": source_block_ids,
                                            "unit_type": block.block.unit_type,
                                            "unit_part_index": block.block.unit_part_index,
                                            "unit_part_total": block.block.unit_part_total,
                                            "embedding": block.embedding,
                                            "chunk_id": chunk_id,
                                            "created_at": now_iso,
                                            "updated_at": now_iso
                                        }
                                        
                                        merge_block_query = """
                                        MERGE (b:Block {block_id: $block_id})
                                        ON CREATE SET b = $props
                                        ON MATCH SET b += $props
                                        RETURN id(b) as block_id
                                        """
                                        block_result = tx.run(merge_block_query, block_id=stored_block_id, props=block_props)
                                        if block_result.single():
                                            nodes_created += 1
                                            
                                            # Link Block to DomainRoot and Document
                                            tx.run("""
                                            MATCH (b:Block {block_id: $block_id})
                                            MATCH (dom:DomainRoot {domain: $domain, customer_id: $customer_id})
                                            MERGE (b)-[:NODE_IN_DOMAIN]->(dom)
                                            """, block_id=stored_block_id, domain=domain, customer_id=scope_id)
                                            relationships_created += 1
                                            
                                            tx.run("""
                                            MATCH (b:Block {block_id: $block_id})
                                            MATCH (d:Document {document_id: $document_id})
                                            MERGE (b)-[:BLOCK_BELONGS_TO_DOCUMENT]->(d)
                                            """, block_id=stored_block_id, document_id=document_id)
                                            relationships_created += 1
                                        
                                    # ENTITY/RELATIONSHIP PROCESSING
                                    # In lean mode: always process; in scaffolding mode: only if block created
                                    from kbfabric_ingest.ingest.ontology_prompt import normalize_to_ontology, get_allowed_node_types
                                    allowed_node_types = get_allowed_node_types(self._runtime_ontology())
                                    entity_emb_on_create = self._embedding_on_create_clause("e", "$embedding")
                                    entity_emb_on_match = self._embedding_on_match_clause("e", "$embedding")
                                    rel_from_emb_on_create = self._embedding_on_create_clause("f", "$from_embedding")
                                    rel_from_emb_on_match = self._embedding_on_match_clause("f", "$from_embedding")
                                    rel_to_emb_on_create = self._embedding_on_create_clause("t", "$to_embedding")
                                    rel_to_emb_on_match = self._embedding_on_match_clause("t", "$to_embedding")
                                    

                                    # Semantic-only categories (ontology maps_to_node: _SKIP_KG).
                                    # TechnicalSpec maps to System and MUST be written.
                                    _SKIP_KG_CATEGORIES = {"Narrative", "Reference", "TechnicalOps"}
                                    block_category_str = block.block_category.value if block.block_category else ""
                                    if block_category_str not in _SKIP_KG_CATEGORIES:
                                        # Log entity extraction for vision blocks
                                        if block.block.block_type == "image":
                                            self.logger.info(
                                                "Processing vision IE entities",
                                                block_id=block.block.block_id[:8],
                                                entity_count=len(block.candidate_entities),
                                                source_file=_source_file
                                            )
                                        
                                        # Process entities
                                        entities_extracted_count = len(block.candidate_entities)
                                        entities_written_count = 0
                                        entities_filtered_count = 0
                                        
                                        for entity in block.candidate_entities:
                                            entity_type = entity.get("type", "Entity")
                                            entity_value_raw = entity.get("value", "")
                                            entity_confidence = entity.get("confidence", 0.0)
                                            
                                            entity_value = self._sanitize_entity_display_value(entity_value_raw)
                                            if entity_value is None:
                                                entities_filtered_count += 1
                                                continue
                                            
                                            # Filter low-confidence entities
                                            if entity_confidence < 0.5:
                                                entities_filtered_count += 1
                                                record_ie_entity_filtered(domain, entity_type, "low_confidence")
                                                continue
                                            
                                            # Normalize with type-awareness
                                            entity_type_normalized = normalize_to_ontology(entity_type.strip(), allowed_node_types)

                                            if entity_type_normalized.startswith("_"):
                                                entities_filtered_count += 1
                                                continue

                                            entity_value_camelcase = self._normalize_to_camelcase(entity_value, entity_type_normalized)
                                            entity_value_normalized = self._normalize_entity_value(entity_value_camelcase)

                                            # Resolve domain_structure types to spine — do NOT create duplicate spine nodes
                                            domain_spine = spine_lookup.get(domain, {})
                                            if entity_type_normalized in ("Function", "SubFunction", "ProductLine"):
                                                spine_id = domain_spine.get((entity_type_normalized, entity_value_normalized))
                                                if spine_id:
                                                    entities_filtered_count += 1
                                                    continue  # Spine node exists; skip creation

                                            entity_id_hash = self._compute_entity_id(
                                                domain, entity_type_normalized, entity_value_normalized, domain_spine,
                                                customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                            )
                                            # Shared mapping utility may pin an existing Neo4j entity_id
                                            mapping_decision = None
                                            if isinstance(entity, dict):
                                                mapping_decision = entity.get("mapping_decision")
                                                selected_existing = entity.get("selected_entity_id")
                                                if selected_existing and mapping_decision in (
                                                    "REUSE_EXISTING",
                                                    "CREATE_DELTA_RELATIONSHIP",
                                                    "CREATE_DELTA_NODE_AND_RELATIONSHIP",
                                                    "NO_CHANGE",
                                                ):
                                                    entity_id_hash = str(selected_existing)
                                                    self.logger.info(
                                                        "kg_writer.reuse_mapped_entity",
                                                        lane=entity.get("lane"),
                                                        mapping_decision=mapping_decision,
                                                        reused_node_id=entity_id_hash,
                                                        block_id=block.block.block_id,
                                                        entity_type=entity_type_normalized,
                                                        mapping_confidence=entity.get("mapping_confidence"),
                                                    )
                                                elif mapping_decision == "REQUIRES_REVIEW":
                                                    # Do not block ingestion — create via hash id, flag for review
                                                    self.logger.info(
                                                        "kg_writer.mapping_requires_review",
                                                        lane=entity.get("lane"),
                                                        block_id=block.block.block_id,
                                                        entity_type=entity_type_normalized,
                                                        entity_value=entity_value[:80],
                                                        mapping_reason=entity.get("mapping_reason_code"),
                                                    )
                                            entity_embedding = entity_embeddings.get(entity_id_hash, [])
                                            
                                            # Create entity node
                                            entity_type_escaped = f"`{entity_type_normalized}`" if " " in entity_type_normalized else entity_type_normalized
                                            
                                            # Track entity collisions within batch (same entity ID seen multiple times)
                                            if entity_id_hash in entity_collisions:
                                                # This entity was already processed in this batch - intentional deduplication
                                                nodes_merged += 1
                                            else:
                                                entity_collisions.append(entity_id_hash)
                                            
                                            aliases = entity.get("aliases") if isinstance(entity, dict) else None
                                            if not isinstance(aliases, list):
                                                aliases = []
                                            entity_description = ""
                                            if isinstance(entity, dict):
                                                raw_desc = entity.get("description")
                                                if isinstance(raw_desc, str):
                                                    entity_description = raw_desc.strip()
                                            source_block_id = source_block_ids[0] if source_block_ids else None

                                            # LEAN MODE: Store provenance on entity nodes
                                            if self.lean_mode:
                                                entity_query = f"""
                                                MERGE (e:{entity_type_escaped} {{entity_id: $entity_id}})
                                                ON CREATE SET 
                                                    e.name = $value, 
                                                    e.name_display = $value_display, 
                                                    e.description = CASE WHEN $description <> '' THEN $description ELSE null END,
                                                    e.domain = $domain,
                                                    e.domain_id = $domain_id,
                                                    e.tenant_id = $tenant_id,
                                                    e.customer_id = coalesce($customer_id, e.customer_id),
                                                    e.kg_node_type = $entity_type,
                                                    e.entity_id = $entity_id, 
                                                    {entity_emb_on_create},
                                                    e.aliases = $aliases,
                                                    e.source_file = $source_file,
                                                    e.source_block_id = $source_block_id,
                                                    e.source_document_ids = CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END,
                                                    e.source_blocks = $source_block_ids,
                                                    e.source_files = [$source_file],
                                                    e.source_pages = [$source_page],
                                                    e.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL THEN [] ELSE [$ingestion_batch_id] END,
                                                    e.created_at = datetime(), 
                                                    e.updated_at = datetime()
                                                ON MATCH SET 
                                                    e.domain = $domain,
                                                    e.domain_id = $domain_id,
                                                    e.tenant_id = $tenant_id,
                                                    e.customer_id = coalesce($customer_id, e.customer_id),
                                                    e.kg_node_type = $entity_type,
                                                    e.name_display = $value_display, 
                                                    e.description = CASE WHEN (e.description IS NULL OR trim(e.description) = '') AND $description <> '' THEN $description ELSE e.description END,
                                                    {entity_emb_on_match},
                                                    e.aliases = reduce(s = coalesce(e.aliases, []), x IN $aliases |
                                                        CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.source_file = coalesce(e.source_file, $source_file),
                                                    e.source_block_id = coalesce(e.source_block_id, $source_block_id),
                                                    e.source_document_ids = CASE WHEN $document_id IS NULL OR $document_id IN coalesce(e.source_document_ids, []) THEN coalesce(e.source_document_ids, []) ELSE coalesce(e.source_document_ids, []) + $document_id END,
                                                    e.source_blocks = reduce(s = coalesce(e.source_blocks, []), x IN $source_block_ids |
                                                        CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.source_files = CASE WHEN NOT $source_file IN coalesce(e.source_files, []) THEN coalesce(e.source_files, []) + $source_file ELSE e.source_files END,
                                                    e.source_pages = CASE WHEN NOT $source_page IN coalesce(e.source_pages, []) THEN coalesce(e.source_pages, []) + $source_page ELSE e.source_pages END,
                                                    e.ingestion_batch_ids = CASE
                                                        WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(e.ingestion_batch_ids, []) THEN coalesce(e.ingestion_batch_ids, [])
                                                        ELSE coalesce(e.ingestion_batch_ids, []) + $ingestion_batch_id
                                                    END,

                                                    e.ingestion_product_line_ids = reduce(s = coalesce(e.ingestion_product_line_ids, []), x IN CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.customer_ontology_ids = reduce(s = coalesce(e.customer_ontology_ids, []), x IN CASE WHEN $customer_ontology_id IS NULL THEN [] ELSE [$customer_ontology_id] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.customer_ontology_versions = reduce(s = coalesce(e.customer_ontology_versions, []), x IN CASE WHEN $customer_ontology_version IS NULL THEN [] ELSE [$customer_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.canonical_ontology_versions = reduce(s = coalesce(e.canonical_ontology_versions, []), x IN CASE WHEN $canonical_ontology_version IS NULL THEN [] ELSE [$canonical_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.industry_extraction_policy_versions = reduce(s = coalesce(e.industry_extraction_policy_versions, []), x IN CASE WHEN $policy_version IS NULL THEN [] ELSE [$policy_version] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.project_ids = reduce(s = coalesce(e.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.context_tags = reduce(s = coalesce(e.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    e.updated_at = datetime()
                                                RETURN elementId(e) as node_id
                                                """
                                                tx.run(entity_query, 
                                                    entity_id=entity_id_hash, 
                                                    value=entity_value_camelcase, 
                                                    value_display=entity_value,
                                                    description=entity_description,
                                                    domain=domain, 
                                                    entity_type=entity_type_normalized, 
                                                    embedding=entity_embedding,
                                                    aliases=aliases,
                                                    ingestion_batch_id=block_metadata.get("ingestion_batch_id"),
                                                    document_id=document_id,
                                                    source_block_ids=source_block_ids,
                                                    source_block_id=source_block_id,
                                                    source_file=_source_file,
                                                    # Neo4j properties cannot store arrays containing null; use 0 sentinel for unknown page.
                                                    source_page=block.block.page_number or 0,
                                                    domain_id=block_metadata.get("domain_id", domain),
                                                    tenant_id=entity_tenant_id,
                                                    customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                                    product_line_id=block_metadata.get("ingestion_product_line_id"),
                                                    customer_ontology_id=block_metadata.get("customer_ontology_id"),
                                                    customer_ontology_version=block_metadata.get("customer_ontology_version"),
                                                    canonical_ontology_version=block_metadata.get("canonical_ontology_version"),
                                                    policy_version=block_metadata.get("industry_extraction_policy_version"),
                                                    project_ids=block_metadata.get("project_ids", []),
                                                    context_tags=block_metadata.get("context_tags", []))
                                                entities_written_count += 1
                                            else:
                                                entity_query = f"""
                                                MERGE (e:{entity_type_escaped} {{entity_id: $entity_id}})
                                                ON CREATE SET e.name = $value, e.name_display = $value_display, e.description = CASE WHEN $description <> '' THEN $description ELSE null END, e.domain = $domain, e.domain_id = $domain_id, e.customer_id = coalesce($customer_id, e.customer_id), e.kg_node_type = $entity_type, e.entity_id = $entity_id, {entity_emb_on_create}, e.aliases = $aliases, e.tenant_id = $tenant_id, e.source_document_ids = CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END, e.source_blocks = $source_block_ids, e.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL THEN [] ELSE [$ingestion_batch_id] END, e.created_at = datetime(), e.updated_at = datetime()
                                                ON MATCH SET e.domain = $domain, e.domain_id = $domain_id, e.customer_id = coalesce($customer_id, e.customer_id), e.kg_node_type = $entity_type, e.name_display = $value_display, e.description = CASE WHEN (e.description IS NULL OR trim(e.description) = '') AND $description <> '' THEN $description ELSE e.description END, {entity_emb_on_match}, e.tenant_id = $tenant_id, e.source_document_ids = CASE WHEN $document_id IS NULL OR $document_id IN coalesce(e.source_document_ids, []) THEN coalesce(e.source_document_ids, []) ELSE coalesce(e.source_document_ids, []) + $document_id END, e.source_blocks = reduce(s = coalesce(e.source_blocks, []), x IN $source_block_ids | CASE WHEN x IN s THEN s ELSE s + x END), e.aliases = reduce(s = coalesce(e.aliases, []), x IN $aliases | CASE WHEN x IN s THEN s ELSE s + x END), e.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(e.ingestion_batch_ids, []) THEN coalesce(e.ingestion_batch_ids, []) ELSE coalesce(e.ingestion_batch_ids, []) + $ingestion_batch_id END
                                                SET e.ingestion_product_line_ids = reduce(s = coalesce(e.ingestion_product_line_ids, []), x IN CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END | CASE WHEN x IN s THEN s ELSE s + x END), e.customer_ontology_ids = reduce(s = coalesce(e.customer_ontology_ids, []), x IN CASE WHEN $customer_ontology_id IS NULL THEN [] ELSE [$customer_ontology_id] END | CASE WHEN x IN s THEN s ELSE s + x END), e.customer_ontology_versions = reduce(s = coalesce(e.customer_ontology_versions, []), x IN CASE WHEN $customer_ontology_version IS NULL THEN [] ELSE [$customer_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), e.canonical_ontology_versions = reduce(s = coalesce(e.canonical_ontology_versions, []), x IN CASE WHEN $canonical_ontology_version IS NULL THEN [] ELSE [$canonical_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), e.industry_extraction_policy_versions = reduce(s = coalesce(e.industry_extraction_policy_versions, []), x IN CASE WHEN $policy_version IS NULL THEN [] ELSE [$policy_version] END | CASE WHEN x IN s THEN s ELSE s + x END), e.project_ids = reduce(s = coalesce(e.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END), e.context_tags = reduce(s = coalesce(e.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END), e.updated_at = datetime()
                                                RETURN elementId(e) as node_id
                                                """
                                                tx.run(entity_query, entity_id=entity_id_hash, value=entity_value_camelcase, value_display=entity_value, description=entity_description, domain=domain, domain_id=block_metadata.get("domain_id", domain), customer_id=block_metadata.get("customer_id") or entity_tenant_id, entity_type=entity_type_normalized, embedding=entity_embedding, aliases=aliases, tenant_id=entity_tenant_id, ingestion_batch_id=block_metadata.get("ingestion_batch_id"), document_id=document_id, source_block_ids=source_block_ids, product_line_id=block_metadata.get("ingestion_product_line_id"), customer_ontology_id=block_metadata.get("customer_ontology_id"), customer_ontology_version=block_metadata.get("customer_ontology_version"), canonical_ontology_version=block_metadata.get("canonical_ontology_version"), policy_version=block_metadata.get("industry_extraction_policy_version"), project_ids=block_metadata.get("project_ids", []), context_tags=block_metadata.get("context_tags", []))
                                                
                                                # SCAFFOLDING MODE: Link entity to Block
                                                mention_query = f"""
                                                MATCH (b:Block {{block_id: $block_id}})
                                                MATCH (e:{entity_type_escaped} {{entity_id: $entity_id}})
                                                MERGE (b)-[m:BLOCK_MENTIONS_ENTITY]->(e)
                                                SET m.ingestion_batch_id = $ingestion_batch_id,
                                                    m.tenant_id = $tenant_id,
                                                    m.customer_id = $customer_id,
                                                    m.domain_id = $domain_id,
                                                    m.ingestion_product_line_id = $product_line_id,
                                                    m.customer_ontology_id = $customer_ontology_id,
                                                    m.customer_ontology_version = $customer_ontology_version,
                                                    m.canonical_ontology_version = $canonical_ontology_version,
                                                    m.industry_extraction_policy_version = $policy_version,
                                                    m.document_id = $document_id,
                                                    m.project_ids = $project_ids,
                                                    m.context_tags = $context_tags
                                                """
                                                tx.run(
                                                    mention_query,
                                                    block_id=stored_block_id,
                                                    entity_id=entity_id_hash,
                                                    ingestion_batch_id=block_metadata.get("ingestion_batch_id"),
                                                    tenant_id=entity_tenant_id,
                                                    customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                                    domain_id=block_metadata.get("domain_id", domain),
                                                    product_line_id=block_metadata.get("ingestion_product_line_id"),
                                                    customer_ontology_id=block_metadata.get("customer_ontology_id"),
                                                    customer_ontology_version=block_metadata.get("customer_ontology_version"),
                                                    canonical_ontology_version=block_metadata.get("canonical_ontology_version"),
                                                    policy_version=block_metadata.get("industry_extraction_policy_version"),
                                                    document_id=document_id,
                                                    project_ids=block_metadata.get("project_ids", []),
                                                    context_tags=block_metadata.get("context_tags", []),
                                                )
                                                relationships_created += 1
                                                entities_written_count += 1
                                            
                                            nodes_created += 1
                                        
                                        # Link entity to DomainRoot (both modes)
                                        entity_domain_query = f"""
                                        MATCH (e:{entity_type_escaped} {{entity_id: $entity_id}})
                                        MATCH (dom:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                                        MERGE (e)-[:NODE_IN_DOMAIN]->(dom)
                                        """
                                        tx.run(entity_domain_query, entity_id=entity_id_hash, domain=domain, customer_id=scope_id)
                                        relationships_created += 1

                                        # Log entity write summary for vision blocks
                                        if block.block.block_type == "image":
                                            self.logger.info(
                                                "Vision IE entity write summary",
                                                block_id=block.block.block_id[:8],
                                                extracted=entities_extracted_count,
                                                written=entities_written_count,
                                                filtered=entities_filtered_count,
                                                deduplicated=(entities_extracted_count - entities_written_count - entities_filtered_count),
                                                source_file=_source_file
                                            )
                                            
                                            # Diagnostic logging for entity creation
                                            self.logger.debug("Entity node created",
                                                entity_type=entity_type_normalized,
                                                entity_value=entity_value_camelcase[:40],
                                                entity_id=entity_id_hash,
                                                block_category=block_category_str,
                                                lean_mode=self.lean_mode)
                                        
                                        # NOTE: Block-level entity creation (ID-pattern matching) DELETED
                                        # It was brittle, assuming formats like INC-XXXX, DEF-XXXX
                                        # Trust IE LLM to extract entities properly instead
                                        # Entities auto-created during relationship MERGE if needed
                                        
                                        # Process relationships between entities
                                        allowed_rel_types = [rt.get("name", "") for rt in self._runtime_ontology().get("relationship_types", [])]
                                        for rel in block.candidate_relationships:
                                                from_type = rel.get("from_type", "Entity")
                                                to_type = rel.get("to_type", "Entity")
                                                rel_type = rel.get("rel_type", "")
                                                from_value = self._sanitize_entity_display_value(rel.get("from_value", ""))
                                                to_value = self._sanitize_entity_display_value(rel.get("to_value", ""))
                                                
                                                if not (from_value and to_value and rel_type):
                                                    continue
                                                if rel_type not in allowed_rel_types:
                                                    continue
                                                
                                                # Normalize entity values and types with type-awareness
                                                from_type_normalized = normalize_to_ontology(from_type.strip(), allowed_node_types)
                                                to_type_normalized = normalize_to_ontology(to_type.strip(), allowed_node_types)

                                                if from_type_normalized.startswith("_") or to_type_normalized.startswith("_"):
                                                    continue

                                                from_value_camelcase = self._normalize_to_camelcase(from_value, from_type_normalized)
                                                from_value_normalized = self._normalize_entity_value(from_value_camelcase)

                                                to_value_camelcase = self._normalize_to_camelcase(to_value, to_type_normalized)
                                                to_value_normalized = self._normalize_entity_value(to_value_camelcase)

                                                # Resolve spine nodes to existing entity_id (prevents duplicate Function/SubFunction/ProductLine)
                                                domain_spine = spine_lookup.get(domain, {})
                                                from_id = rel.get("from_entity_id") or self._compute_entity_id(
                                                    domain, from_type_normalized, from_value_normalized, domain_spine,
                                                    customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                                )
                                                to_id = rel.get("to_entity_id") or self._compute_entity_id(
                                                    domain, to_type_normalized, to_value_normalized, domain_spine,
                                                    customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                                )
                                                from_embedding = entity_embeddings.get(from_id, [])
                                                to_embedding = entity_embeddings.get(to_id, [])
                                                rel_id = hashlib.sha256(f"{from_id}:{rel_type}:{to_id}".encode()).hexdigest()[:16]
                                                relationship_evidence = json.loads(evidence_context)
                                                relationship_evidence["relationship"] = {
                                                    "from_type": from_type_normalized,
                                                    "from_value": from_value,
                                                    "rel_type": rel_type,
                                                    "to_type": to_type_normalized,
                                                    "to_value": to_value,
                                                }
                                                relationship_evidence["confidence"] = rel.get("confidence")
                                                relationship_evidence_context = json.dumps(
                                                    relationship_evidence, sort_keys=True
                                                )

                                                # Create relationship with provenance
                                                from_type_escaped = f"`{from_type_normalized}`" if " " in from_type_normalized else from_type_normalized
                                                to_type_escaped = f"`{to_type_normalized}`" if " " in to_type_normalized else to_type_normalized
                                                rel_type_escaped = rel_type.replace(" ", "_").upper()
                                                
                                                # Use MERGE for entities to auto-create if missing
                                                # This ensures relationships succeed even if entity wasn't extracted separately
                                                rel_query = f"""
                                                MERGE (f:{from_type_escaped} {{entity_id: $from_id}})
                                                ON CREATE SET 
                                                    f.name = $from_value, 
                                                    f.name_display = $from_value_display,
                                                    f.domain = $domain,
                                                    f.domain_id = $domain_id,
                                                    f.tenant_id = $tenant_id,
                                                    f.customer_id = coalesce($customer_id, f.customer_id),
                                                    f.kg_node_type = $from_type,
                                                    {rel_from_emb_on_create},
                                                    f.source_document_ids = CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END,
                                                    f.source_blocks = $source_block_ids,
                                                    f.source_files = [$source_file],
                                                    f.source_pages = [$source_page],
                                                    f.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL THEN [] ELSE [$ingestion_batch_id] END,
                                                    f.created_at = datetime(),
                                                    f.updated_at = datetime()
                                                ON MATCH SET
                                                    {rel_from_emb_on_match},
                                                    f.domain = $domain,
                                                    f.domain_id = $domain_id,
                                                    f.tenant_id = $tenant_id,
                                                    f.customer_id = coalesce($customer_id, f.customer_id),
                                                    f.source_document_ids = CASE WHEN $document_id IS NULL OR $document_id IN coalesce(f.source_document_ids, []) THEN coalesce(f.source_document_ids, []) ELSE coalesce(f.source_document_ids, []) + $document_id END,
                                                    f.source_blocks = reduce(s = coalesce(f.source_blocks, []), x IN $source_block_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    f.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(f.ingestion_batch_ids, []) THEN coalesce(f.ingestion_batch_ids, []) ELSE coalesce(f.ingestion_batch_ids, []) + $ingestion_batch_id END,
                                                    f.updated_at = datetime()
                                                SET f.ingestion_product_line_ids = reduce(s = coalesce(f.ingestion_product_line_ids, []), x IN CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END | CASE WHEN x IN s THEN s ELSE s + x END), f.customer_ontology_ids = reduce(s = coalesce(f.customer_ontology_ids, []), x IN CASE WHEN $customer_ontology_id IS NULL THEN [] ELSE [$customer_ontology_id] END | CASE WHEN x IN s THEN s ELSE s + x END), f.customer_ontology_versions = reduce(s = coalesce(f.customer_ontology_versions, []), x IN CASE WHEN $customer_ontology_version IS NULL THEN [] ELSE [$customer_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), f.canonical_ontology_versions = reduce(s = coalesce(f.canonical_ontology_versions, []), x IN CASE WHEN $canonical_ontology_version IS NULL THEN [] ELSE [$canonical_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), f.industry_extraction_policy_versions = reduce(s = coalesce(f.industry_extraction_policy_versions, []), x IN CASE WHEN $policy_version IS NULL THEN [] ELSE [$policy_version] END | CASE WHEN x IN s THEN s ELSE s + x END), f.project_ids = reduce(s = coalesce(f.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END), f.context_tags = reduce(s = coalesce(f.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END)
                                                MERGE (t:{to_type_escaped} {{entity_id: $to_id}})
                                                ON CREATE SET 
                                                    t.name = $to_value, 
                                                    t.name_display = $to_value_display,
                                                    t.domain = $domain,
                                                    t.domain_id = $domain_id,
                                                    t.tenant_id = $tenant_id,
                                                    t.customer_id = coalesce($customer_id, t.customer_id),
                                                    t.kg_node_type = $to_type,
                                                    {rel_to_emb_on_create},
                                                    t.source_document_ids = CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END,
                                                    t.source_blocks = $source_block_ids,
                                                    t.source_files = [$source_file],
                                                    t.source_pages = [$source_page],
                                                    t.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL THEN [] ELSE [$ingestion_batch_id] END,
                                                    t.created_at = datetime(),
                                                    t.updated_at = datetime()
                                                ON MATCH SET
                                                    {rel_to_emb_on_match},
                                                    t.domain = $domain,
                                                    t.domain_id = $domain_id,
                                                    t.tenant_id = $tenant_id,
                                                    t.customer_id = coalesce($customer_id, t.customer_id),
                                                    t.source_document_ids = CASE WHEN $document_id IS NULL OR $document_id IN coalesce(t.source_document_ids, []) THEN coalesce(t.source_document_ids, []) ELSE coalesce(t.source_document_ids, []) + $document_id END,
                                                    t.source_blocks = reduce(s = coalesce(t.source_blocks, []), x IN $source_block_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    t.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(t.ingestion_batch_ids, []) THEN coalesce(t.ingestion_batch_ids, []) ELSE coalesce(t.ingestion_batch_ids, []) + $ingestion_batch_id END,
                                                    t.updated_at = datetime()
                                                SET t.ingestion_product_line_ids = reduce(s = coalesce(t.ingestion_product_line_ids, []), x IN CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END | CASE WHEN x IN s THEN s ELSE s + x END), t.customer_ontology_ids = reduce(s = coalesce(t.customer_ontology_ids, []), x IN CASE WHEN $customer_ontology_id IS NULL THEN [] ELSE [$customer_ontology_id] END | CASE WHEN x IN s THEN s ELSE s + x END), t.customer_ontology_versions = reduce(s = coalesce(t.customer_ontology_versions, []), x IN CASE WHEN $customer_ontology_version IS NULL THEN [] ELSE [$customer_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), t.canonical_ontology_versions = reduce(s = coalesce(t.canonical_ontology_versions, []), x IN CASE WHEN $canonical_ontology_version IS NULL THEN [] ELSE [$canonical_ontology_version] END | CASE WHEN x IN s THEN s ELSE s + x END), t.industry_extraction_policy_versions = reduce(s = coalesce(t.industry_extraction_policy_versions, []), x IN CASE WHEN $policy_version IS NULL THEN [] ELSE [$policy_version] END | CASE WHEN x IN s THEN s ELSE s + x END), t.project_ids = reduce(s = coalesce(t.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END), t.context_tags = reduce(s = coalesce(t.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END)
                                                MERGE (f)-[r:{rel_type_escaped} {{rel_id: $rel_id}}]->(t)
                                                ON CREATE SET 
                                                    r.created_at = datetime(), 
                                                    r.updated_at = datetime(),
                                                    r.domain = $domain,
                                                    r.domain_id = $domain_id,
                                                    r.tenant_id = $tenant_id,
                                                    r.customer_id = $customer_id,
                                                    r.ingestion_product_line_ids = CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END,
                                                    r.customer_ontology_id = $customer_ontology_id,
                                                    r.customer_ontology_version = $customer_ontology_version,
                                                    r.canonical_ontology_version = $canonical_ontology_version,
                                                    r.industry_extraction_policy_version = $policy_version,
                                                    r.project_ids = $project_ids,
                                                    r.context_tags = $context_tags,
                                                    r.evidence_contexts = [$relationship_evidence_context],
                                                    r.document_id = $document_id,
                                                    r.document_ids = CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END,
                                                    r.source_block_id = $source_block_id,
                                                    r.source_block_ids = $source_block_ids,
                                                    r.source_files = [$source_file],
                                                    r.source_pages = CASE WHEN $source_page IS NULL THEN [] ELSE [$source_page] END,
                                                    r.semantic_unit_id = $semantic_unit_id,
                                                    r.source_file = $source_file,
                                                    r.source_page = $source_page,
                                                    r.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL THEN [] ELSE [$ingestion_batch_id] END
                                                ON MATCH SET
                                                    r.updated_at = datetime(),
                                                    r.domain_id = $domain_id,
                                                    r.tenant_id = $tenant_id,
                                                    r.customer_id = $customer_id,
                                                    r.ingestion_product_line_ids = reduce(s = coalesce(r.ingestion_product_line_ids, []), x IN CASE WHEN $product_line_id IS NULL THEN [] ELSE [$product_line_id] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    r.customer_ontology_id = $customer_ontology_id,
                                                    r.customer_ontology_version = $customer_ontology_version,
                                                    r.canonical_ontology_version = $canonical_ontology_version,
                                                    r.industry_extraction_policy_version = $policy_version,
                                                    r.project_ids = reduce(s = coalesce(r.project_ids, []), x IN $project_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    r.context_tags = reduce(s = coalesce(r.context_tags, []), x IN $context_tags | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    r.evidence_contexts = CASE WHEN $relationship_evidence_context IN coalesce(r.evidence_contexts, []) THEN coalesce(r.evidence_contexts, []) ELSE coalesce(r.evidence_contexts, []) + $relationship_evidence_context END,
                                                    r.document_id = $document_id,
                                                    r.document_ids = reduce(s = coalesce(r.document_ids, []), x IN CASE WHEN $document_id IS NULL THEN [] ELSE [$document_id] END | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    r.source_block_id = $source_block_id,
                                                    r.source_block_ids = reduce(s = coalesce(r.source_block_ids, []), x IN $source_block_ids | CASE WHEN x IN s THEN s ELSE s + x END),
                                                    r.source_files = CASE WHEN $source_file IN coalesce(r.source_files, []) THEN coalesce(r.source_files, []) ELSE coalesce(r.source_files, []) + $source_file END,
                                                    r.source_pages = CASE WHEN $source_page IN coalesce(r.source_pages, []) THEN coalesce(r.source_pages, []) ELSE coalesce(r.source_pages, []) + $source_page END,
                                                    r.semantic_unit_id = $semantic_unit_id,
                                                    r.source_file = $source_file,
                                                    r.source_page = $source_page,
                                                    r.ingestion_batch_ids = CASE WHEN $ingestion_batch_id IS NULL OR $ingestion_batch_id IN coalesce(r.ingestion_batch_ids, []) THEN coalesce(r.ingestion_batch_ids, []) ELSE coalesce(r.ingestion_batch_ids, []) + $ingestion_batch_id END
                                                RETURN count(*) as created
                                                """
                                                tx.run(rel_query, 
                                                       from_id=from_id, 
                                                       to_id=to_id, 
                                                       rel_id=rel_id, 
                                                       domain=domain,
                                                       relationship_evidence_context=relationship_evidence_context,
                                                       ingestion_batch_id=block_metadata.get("ingestion_batch_id"),
                                                       from_value=from_value_camelcase,
                                                       from_value_display=from_value,
                                                       from_type=from_type_normalized,
                                                       from_embedding=from_embedding,
                                                       to_value=to_value_camelcase,
                                                       to_value_display=to_value,
                                                       to_type=to_type_normalized,
                                                       to_embedding=to_embedding,
                                                       source_block_id=block.block.block_id,
                                                       source_block_ids=source_block_ids,
                                                       semantic_unit_id=semantic_unit_id,
                                                       source_file=_source_file,
                                                       source_page=block.block.page_number or 0,
                                                       domain_id=block_metadata.get("domain_id", domain),
                                                       tenant_id=entity_tenant_id,
                                                       customer_id=block_metadata.get("customer_id") or entity_tenant_id,
                                                       product_line_id=block_metadata.get("ingestion_product_line_id"),
                                                       customer_ontology_id=block_metadata.get("customer_ontology_id"),
                                                       customer_ontology_version=block_metadata.get("customer_ontology_version"),
                                                       canonical_ontology_version=block_metadata.get("canonical_ontology_version"),
                                                       policy_version=block_metadata.get("industry_extraction_policy_version"),
                                                       project_ids=block_metadata.get("project_ids", []),
                                                       context_tags=block_metadata.get("context_tags", []),
                                                       document_id=document_id)
                                                relationships_created += 1

                                                # Ensure NODE_IN_DOMAIN for relationship-created entities (at creation time)
                                                for e_type_escaped, e_id in [
                                                    (from_type_escaped, from_id),
                                                    (to_type_escaped, to_id),
                                                ]:
                                                    entity_domain_query = f"""
                                                    MATCH (e:{e_type_escaped} {{entity_id: $entity_id}})
                                                    MATCH (dom:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                                                    MERGE (e)-[:NODE_IN_DOMAIN]->(dom)
                                                    """
                                                    tx.run(entity_domain_query, entity_id=e_id, domain=domain, customer_id=scope_id)
                                                    relationships_created += 1
                                        
                                except Exception as e:
                                    errors.append(f"Batch block error: {str(e)}")
                            
                            return {
                                "nodes": nodes_created, 
                                "nodes_merged": nodes_merged,
                                "relationships": relationships_created, 
                                "errors": errors,
                                "entity_collisions": len(entity_collisions)
                            }
                        return batch_write_transaction
                    
                    try:
                        batch_result = session.execute_write(create_batch_writer(batch_blocks))
                    except Exception as batch_error:
                        if write_unit_callback:
                            write_unit_callback("neo4j", batch_unit_key, "UNKNOWN", {
                                "batch": batch_idx + 1, "total_batches": total_batches,
                                "block_count": len(batch_blocks),
                            }, {"code": "DEPENDENCY_FAILURE", "message": str(batch_error)[:500]})
                        raise
                    total_nodes += batch_result.get("nodes", 0)
                    total_nodes_merged += batch_result.get("nodes_merged", 0)
                    total_relationships += batch_result.get("relationships", 0)
                    all_errors.extend(batch_result.get("errors", []))
                    if write_unit_callback:
                        unit_status = "UNKNOWN" if batch_result.get("errors") else "SUCCEEDED"
                        write_unit_callback("neo4j", batch_unit_key, unit_status, {
                            "batch": batch_idx + 1, "total_batches": total_batches,
                            "block_count": len(batch_blocks),
                            "nodes": batch_result.get("nodes", 0),
                            "relationships": batch_result.get("relationships", 0),
                            "errors": len(batch_result.get("errors", [])),
                        }, ({"code": "STORE_WRITE_PARTIAL", "message": "Some blocks in the transaction batch failed."}
                            if batch_result.get("errors") else None))
                    
                    self.logger.debug(
                        "Batch completed", 
                        batch=batch_idx+1, 
                        total_batches=total_batches, 
                        nodes=batch_result.get("nodes", 0),
                        nodes_merged=batch_result.get("nodes_merged", 0),
                        entity_collisions=batch_result.get("entity_collisions", 0)
                    )
                
                nodes_created = total_nodes
                relationships_created = total_relationships
                errors = all_errors
                
                # Calculate deduplication stats
                dedup_pct = (total_nodes_merged / total_entities_processed * 100) if total_entities_processed > 0 else 0.0
                
                self.logger.info(
                    "Wrote blocks to Neo4j (batched)",
                    nodes_created=nodes_created,
                    nodes_merged=total_nodes_merged,
                    relationships_created=relationships_created,
                    error_count=len(errors),
                    batches_processed=total_batches,
                    total_entities_processed=total_entities_processed,
                    deduplication_pct=f"{dedup_pct:.1f}%"
                )
                
                if total_nodes_merged > 0:
                    self.logger.info(
                        "Entity deduplication detected",
                        nodes_merged=total_nodes_merged,
                        deduplication_pct=f"{dedup_pct:.1f}%",
                        note="Entities with same normalized ID were merged (intentional behavior)"
                    )
                
                if errors:
                    self.logger.warning("Neo4j write completed with errors", errors=errors[:5])  # Log first 5 errors
                elif nodes_created == 0:
                    self.logger.error("Neo4j write completed but NO NODES WERE CREATED", block_count=len(blocks))
                
                # Keep run-local writer counters here. Domain-wide totals include
                # prior customers/runs and must never replace this run's result.
                print(f"[KG][DIAG] Final write summary: nodes_created={nodes_created}, relationships_created={relationships_created}", flush=True)
                self.logger.info(
                    "KG write final summary",
                    domain=domain,
                    nodes_created=nodes_created,
                    relationships_created=relationships_created,
                    total_blocks_processed=len(blocks)
                )
                
                # Record telemetry with OTel Database conventions
                duration = time.time() - start_time
                server_addr = self.uri.replace("bolt://", "").replace("neo4j://", "")
                record_neo4j_write(domain, "write", duration, server_address=server_addr)
                record_neo4j_node(domain, "total", nodes_created)
                record_neo4j_relationship(domain, "total", relationships_created)

                log_stage_end(
                    self.logger, "neo4j.write", write_obs_start,
                    domain=domain,
                    block_count=len(blocks),
                    nodes_created=nodes_created,
                    relationships_created=relationships_created,
                    neo4j_uri=redact_uri(getattr(self, "uri", "")),
                )

                self.log_entity_embedding_stats(domain, "after_write")
                
                # Return counts for reporting
                return {
                    "nodes": nodes_created, "relationships": relationships_created,
                    "errors": errors,
                }
        except Exception as e:
            # Record telemetry for failure
            duration = time.time() - start_time
            server_addr = self.uri.replace("bolt://", "").replace("neo4j://", "")
            record_neo4j_write(domain, "write", duration, server_address=server_addr)
            self.logger.exception("Fatal error writing to Neo4j", error=str(e))
            log_stage_error(self.logger, "neo4j.write", e, domain=domain,
                            block_count=len(blocks), started_at=write_obs_start)
            raise
        
        # Fallback return if try block doesn't return (shouldn't happen)
        return {"nodes": 0, "relationships": 0}
    
    def verify_write(
        self,
        *,
        domain: str,
        customer_id: str,
        product_line_id: str,
        ingestion_batch_id: str,
        expected_nodes: int = 0,
        expected_relationships: int = 0,
    ) -> dict[str, Any]:
        """Count distinct graph objects touched by one customer/product-line batch.

        The counts are deliberately not compared with global/domain totals. Shared
        nodes can be touched by several batches; provenance arrays attribute a
        distinct node/relationship to this run without counting older graph data.
        """
        actual_nodes = 0
        actual_relationships = 0
        verification_status = "UNAVAILABLE"
        
        try:
            with self.driver.session() as session:
                scope = {
                    "domain": domain,
                    "customer_id": customer_id,
                    "product_line_id": product_line_id,
                    "ingestion_batch_id": ingestion_batch_id,
                }
                node_result = session.run(
                    """MATCH (n)
                       WHERE n.domain_id = $domain
                         AND n.customer_id = $customer_id
                         AND $product_line_id IN coalesce(n.ingestion_product_line_ids, [])
                         AND $ingestion_batch_id IN coalesce(n.ingestion_batch_ids, [])
                         AND NOT n:Block AND NOT n:Document AND NOT n:DomainRoot
                       RETURN count(DISTINCT n) AS count""",
                    **scope,
                )
                node_record = node_result.single()
                actual_nodes = int(node_record["count"] if node_record else 0)
                rel_result = session.run(
                    """MATCH ()-[r]->()
                       WHERE r.domain_id = $domain
                         AND r.customer_id = $customer_id
                         AND $product_line_id IN coalesce(r.ingestion_product_line_ids, [])
                         AND $ingestion_batch_id IN coalesce(r.ingestion_batch_ids, [])
                       RETURN count(DISTINCT r) AS count""",
                    **scope,
                )
                rel_record = rel_result.single()
                actual_relationships = int(rel_record["count"] if rel_record else 0)
                verification_status = "VERIFIED"
                
                self.logger.info(
                    "Neo4j run-scoped writes verified",
                    **scope,
                    actual_nodes=actual_nodes,
                    expected_nodes=expected_nodes,
                    actual_relationships=actual_relationships,
                    expected_relationships=expected_relationships
                )
                
        except Exception as e:
            self.logger.warning("Failed to verify Neo4j run-scoped write (non-fatal)", error=str(e), domain=domain)
        
        if verification_status == "VERIFIED":
            return {
                "nodes": actual_nodes,
                "relationships": actual_relationships,
                "verification_status": verification_status,
            }
        return {
            "nodes": int(expected_nodes or 0),
            "relationships": int(expected_relationships or 0),
            "verification_status": "UNAVAILABLE",
        }

    def compute_graph_metrics(self, domain: str) -> dict[str, any]:
        """
        Compute and store graph metrics (degree, PageRank) on entity nodes.
        
        Called after write_blocks() to enable importance-based retrieval.
        Uses Neo4j GDS plugin for PageRank if available, otherwise computes degree only.
        
        Args:
            domain: Domain to compute metrics for
            
        Returns:
            dict with status and counts
        """
        self.logger.info("Computing graph metrics", domain=domain)
        
        try:
            with self.driver.session() as session:
                # 1. Compute and store DEGREE on all entity nodes
                # Degree = number of direct connections (both directions)
                # Note: Neo4j 5.x requires COUNT {} instead of size() for pattern expressions
                degree_query = """
                MATCH (e)
                WHERE e.domain = $domain AND NOT e:Block AND NOT e:Document AND NOT e:DomainRoot
                SET e.degree = COUNT { (e)--() }
                RETURN count(e) as updated
                """
                degree_result = session.run(degree_query, domain=domain)
                degree_count = degree_result.single()["updated"]
                self.logger.info("Updated degree on entities", domain=domain, count=degree_count)
                
                # 2. Try to compute PageRank using GDS plugin
                # PageRank measures entity importance based on who links to it
                pagerank_count = 0
                try:
                    # Check if GDS is available
                    gds_check = session.run("RETURN gds.version() AS version")
                    gds_version = gds_check.single()["version"]
                    self.logger.info("GDS plugin available", version=gds_version)
                    
                    # Create a named graph projection for the domain
                    graph_name = f"kf_graph_{domain}"
                    
                    # Drop existing graph if it exists
                    try:
                        session.run(f"CALL gds.graph.drop('{graph_name}', false)")
                    except Exception:
                        pass  # Graph doesn't exist, OK
                    
                    # Create graph projection - include all entity nodes and their relationships
                    # Exclude Block, Document, DomainRoot (structural nodes)
                    # Also exclude scaffolding relationships that cross to Block/Document nodes
                    scaffolding_rels = ['BLOCK_MENTIONS_ENTITY', 'BLOCK_BELONGS_TO_DOCUMENT', 
                                       'NODE_IN_DOMAIN', 'DOCUMENT_IN_DOMAIN']
                    scaffolding_filter = " AND ".join([f"NOT type(r) = '{rel}'" for rel in scaffolding_rels])
                    
                    project_query = f"""
                    CALL gds.graph.project.cypher(
                        $graph_name,
                        'MATCH (n) WHERE n.domain = $domain AND NOT n:Block AND NOT n:Document AND NOT n:DomainRoot RETURN id(n) AS id',
                        'MATCH (n)-[r]->(m) WHERE n.domain = $domain AND m.domain = $domain AND NOT n:Block AND NOT m:Block AND NOT n:Document AND NOT m:Document AND NOT n:DomainRoot AND NOT m:DomainRoot AND {scaffolding_filter} RETURN id(n) AS source, id(m) AS target',
                        {{parameters: {{domain: $domain}}}}
                    )
                    YIELD graphName, nodeCount, relationshipCount
                    RETURN graphName, nodeCount, relationshipCount
                    """
                    project_result = session.run(project_query, graph_name=graph_name, domain=domain)
                    project_record = project_result.single()
                    if project_record:
                        node_count = project_record["nodeCount"]
                        rel_count = project_record["relationshipCount"]
                        self.logger.info("Created GDS graph projection", graph_name=graph_name, nodes=node_count, relationships=rel_count)
                        
                        if node_count > 0:
                            # Run PageRank and write results back to nodes
                            pagerank_query = """
                            CALL gds.pageRank.write($graph_name, {
                                writeProperty: 'pagerank',
                                maxIterations: 20,
                                dampingFactor: 0.85
                            })
                            YIELD nodePropertiesWritten, ranIterations
                            RETURN nodePropertiesWritten, ranIterations
                            """
                            pagerank_result = session.run(pagerank_query, graph_name=graph_name)
                            pagerank_record = pagerank_result.single()
                            pagerank_count = pagerank_record["nodePropertiesWritten"]
                            iterations = pagerank_record["ranIterations"]
                            self.logger.info("Computed PageRank", domain=domain, nodes=pagerank_count, iterations=iterations)
                        
                        # Clean up graph projection
                        session.run(f"CALL gds.graph.drop('{graph_name}', false)")
                
                except Exception as gds_error:
                    error_msg = str(gds_error)
                    self.logger.warning(
                        "GDS not available, computing PageRank in Python",
                        domain=domain,
                        error=error_msg[:120],
                    )
                    print(f"[KG] GDS unavailable — computing PageRank in Python", flush=True)
                    pagerank_count = self._compute_pagerank_python(session, domain)
                
                return {
                    "status": "success",
                    "degree_updated": degree_count,
                    "pagerank_updated": pagerank_count
                }
                
        except Exception as e:
            self.logger.error("Failed to compute graph metrics", domain=domain, error=str(e))
            return {"status": "error", "error": str(e)}

    def _compute_pagerank_python(
        self, session, domain: str,
        damping: float = 0.85, max_iter: int = 40, tol: float = 1e-6,
    ) -> int:
        """
        Pure-Python PageRank when Neo4j GDS is unavailable.

        Pulls the adjacency list from Neo4j, runs the iterative algorithm
        in-memory, and writes scores back.  Runs in <1 s for graphs up to
        ~50 k edges, which comfortably covers ingestion-scale graphs.
        """
        scaffolding = [
            "BLOCK_MENTIONS_ENTITY", "BLOCK_BELONGS_TO_DOCUMENT",
            "NODE_IN_DOMAIN", "DOCUMENT_IN_DOMAIN",
        ]
        edges_query = """
        MATCH (a)-[r]->(b)
        WHERE a.domain = $domain AND b.domain = $domain
          AND NOT a:Block AND NOT a:Document
          AND NOT b:Block AND NOT b:Document
          AND NOT type(r) IN $scaffolding
        RETURN id(a) AS src, id(b) AS tgt
        """
        records = list(session.run(edges_query, domain=domain, scaffolding=scaffolding))

        node_ids: set[int] = set()
        out_links: dict[int, list[int]] = {}
        in_links: dict[int, list[int]] = {}
        for rec in records:
            s, t = rec["src"], rec["tgt"]
            node_ids.update((s, t))
            out_links.setdefault(s, []).append(t)
            in_links.setdefault(t, []).append(s)

        n = len(node_ids)
        if n == 0:
            return 0

        pr: dict[int, float] = {nid: 1.0 / n for nid in node_ids}
        base = (1.0 - damping) / n

        for iteration in range(max_iter):
            new_pr: dict[int, float] = {}
            dangling_sum = sum(pr[nid] for nid in node_ids if nid not in out_links)
            dangling_contrib = damping * dangling_sum / n

            for nid in node_ids:
                rank = base + dangling_contrib
                for src in in_links.get(nid, []):
                    rank += damping * pr[src] / len(out_links[src])
                new_pr[nid] = rank

            diff = sum(abs(new_pr[nid] - pr[nid]) for nid in node_ids)
            pr = new_pr
            if diff < tol:
                self.logger.info("PageRank converged", iterations=iteration + 1, diff=f"{diff:.2e}")
                break

        written = 0
        batch: list[tuple[int, float]] = [(nid, score) for nid, score in pr.items()]
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            params = [{"nid": nid, "pr": round(score, 6)} for nid, score in chunk]
            session.run(
                "UNWIND $rows AS row "
                "MATCH (n) WHERE id(n) = row.nid "
                "SET n.pagerank = row.pr",
                rows=params,
            )
            written += len(chunk)

        self.logger.info("Python PageRank written", domain=domain, nodes=written)
        print(f"[KG] PageRank computed in Python: {written} nodes, {iteration + 1} iterations", flush=True)
        return written

    def link_content_to_spine(self, domain: str, enriched_blocks: list | None = None) -> dict:
        """
        Post-write step: link IE-extracted content entities to spine nodes.

        Strategy (in priority order):
          1. LLM-provided ``spine_node`` on the enriched block (exact match,
             case-insensitive against Neo4j spine names).
          2. Word-overlap fallback — tokenise the entity name *and* its source
             block text and score against spine node name tokens.  Handles
             rule-based IE blocks (TestCase, Defect) that never reach the LLM.
          3. DomainRoot — last resort so the entity is always reachable.
        """
        import re as _re

        stats = {"linked": 0, "linked_overlap": 0, "linked_to_root": 0, "unlinked": 0}

        def _stem(w: str) -> str:
            """Minimal suffix stripping for overlap matching."""
            if w.endswith("ing") and len(w) > 5:
                return w[:-3]
            if w.endswith("tion") and len(w) > 6:
                return w[:-4]
            if w.endswith("ment") and len(w) > 6:
                return w[:-4]
            if w.endswith("ness") and len(w) > 6:
                return w[:-4]
            if w.endswith("ies") and len(w) > 4:
                return w[:-3] + "y"
            if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
                return w[:-1]
            return w

        def _tokenize(text: str) -> set[str]:
            spaced = _re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
            raw = {w.lower() for w in _re.split(r'[\s_\-/]+', spaced) if len(w) > 2}
            return {_stem(w) for w in raw}

        block_spine_map: dict[str, str] = {}
        block_text_map: dict[str, str] = {}

        run_product_line = self._run_product_line_name()
        run_customer_id = (
            self._run_context.tenant_context.customer_id
            or self._run_context.tenant_context.tenant_id
        )
        if not run_product_line:
            raise RuntimeError(
                "KGWriter requires a frozen run product line for context resolution"
            )

        if enriched_blocks:
            for eb in enriched_blocks:
                bid = getattr(eb.block, "block_id", None)
                spine = getattr(eb, "spine_node", None) or "domain_root"
                text = getattr(eb.block, "text", "") or ""

                if bid:
                    block_spine_map[bid] = spine
                    block_text_map[bid] = text

                for sbid in (getattr(eb.block, "source_block_ids", None) or []):
                    if sbid and sbid not in block_text_map:
                        block_spine_map[sbid] = spine
                        block_text_map[sbid] = text
                        # Product line is authoritative at run scope.

        def _link(session, c_label, c_eid, target_label, target_eid):
            c_esc = f"`{c_label}`" if " " in c_label else c_label
            s_esc = f"`{target_label}`" if " " in target_label else target_label
            session.run(f"""
                MATCH (e:{c_esc} {{entity_id: $cid}})
                MATCH (s:{s_esc} {{entity_id: $sid}})
                MERGE (e)-[:PART_OF]->(s)
            """, cid=c_eid, sid=target_eid)

        def _link_to_root(session, c_label, c_eid, domain):
            c_esc = f"`{c_label}`" if " " in c_label else c_label
            session.run(f"""
                MATCH (e:{c_esc} {{entity_id: $cid}})
                MATCH (dr:DomainRoot {{domain: $domain, customer_id: $customer_id}})
                MERGE (e)-[:PART_OF]->(dr)
            """, cid=c_eid, domain=domain, customer_id=run_customer_id)

        def _best_overlap(tokens: set[str], spine_entries: list[dict]) -> dict | None:
            """Return the best spine entry by word-overlap score, or None."""
            if not tokens:
                return None
            best, best_score = None, 0.0
            for sp in spine_entries:
                if not sp["tokens"]:
                    continue
                overlap = len(tokens & sp["tokens"])
                if overlap == 0:
                    continue
                score = overlap / len(sp["tokens"])
                if score > best_score or (score == best_score and best and sp["priority"] > best["priority"]):
                    best_score, best = score, sp
            return best if best_score >= 0.35 else None

        try:
            with self.driver.session() as session:
                spine_rows = list(session.run("""
                    MATCH (s {domain: $domain, customer_id: $customer_id})
                    WHERE s:SubFunction OR s:Function OR s:ProductLine
                    OPTIONAL MATCH path=(s)-[:PART_OF*1..2]->(pl:ProductLine {domain: $domain, customer_id: $customer_id})
                    RETURN
                        labels(s)[0] AS label,
                        s.name AS name,
                        s.entity_id AS eid,
                        CASE
                            WHEN s:ProductLine THEN s.name
                            ELSE pl.name
                        END AS product_line
                """, domain=domain, customer_id=run_customer_id))

                spine_by_scope: dict[str, dict[str, dict]] = {}
                priority = {"SubFunction": 3, "Function": 2, "ProductLine": 1}

                for r in spine_rows:
                    product_line = (r["product_line"] or "").strip()
                    key = (r["name"] or "").strip().lower()

                    entry = {
                        "label": r["label"],
                        "name": r["name"],
                        "eid": r["eid"],
                        "product_line": product_line,
                        "tokens": _tokenize(r["name"] or ""),
                        "priority": priority.get(r["label"], 0),
                    }

                    scope = product_line.lower()
                    scoped = spine_by_scope.setdefault(scope, {})

                    existing = scoped.get(key)
                    if not existing or entry["priority"] > existing["priority"]:
                        scoped[key] = entry

                def _spine_scope(product_line: str | None) -> dict[str, dict]:
                    """
                    Return spine candidates for the selected ProductLine.

                    A selected ProductLine is a hard scope boundary. We do not
                    fall back to another ProductLine in the same domain.
                    """
                    normalized = (product_line or "").strip().lower()

                    if normalized:
                        return spine_by_scope.get(normalized, {})

                    # Preserve legacy behavior for blocks that have no
                    # document-level ProductLine context yet.
                    merged: dict[str, dict] = {}
                    for scoped in spine_by_scope.values():
                        for key, entry in scoped.items():
                            existing = merged.get(key)
                            if not existing or entry["priority"] > existing["priority"]:
                                merged[key] = entry
                    return merged

                content_rows = list(session.run("""
                    MATCH (e {domain: $domain, customer_id: $customer_id})
                    WHERE NOT e:DomainRoot AND NOT e:ProductLine AND NOT e:Function
                      AND NOT e:SubFunction AND NOT e:Block AND NOT e:Document
                    RETURN labels(e)[0] AS label, e.name AS name,
                           e.entity_id AS eid, e.source_block_id AS sbid
                """, domain=domain, customer_id=run_customer_id))

                _text_hits = sum(
                    1 for c in content_rows if block_text_map.get(c["sbid"] or "")
                )
                _spine_hits = sum(
                    1 for c in content_rows
                    if block_spine_map.get(c["sbid"] or "", "domain_root").lower()
                    not in ("domain_root", "")
                )

                product_lines = [run_product_line]

                self.logger.info(
                    "Context Resolution spine scope prepared",
                    domain=domain,
                    product_lines=product_lines,
                    entities=len(content_rows),
                    text_hits=_text_hits,
                    spine_hits=_spine_hits,
                )

                print(
                    f"[KG][spine-link] {len(content_rows)} entities, "
                    f"block_text_map={len(block_text_map)} entries, "
                    f"text-hit={_text_hits}, spine-hit={_spine_hits}, "
                    f"product_lines={product_lines}",
                    flush=True,
                )

                for c in content_rows:
                    sbid = c["sbid"] or ""
                    spine_name = block_spine_map.get(sbid, "domain_root")
                    product_line = run_product_line

                    scoped_spine = _spine_scope(product_line)

                    self.logger.info(
                        "Context Resolution spine candidate scope",
                        domain=domain,
                        product_line=product_line,
                        spine_node=spine_name,
                        entity=c["name"],
                    )

                    # --- Strategy 1: spine_node from LLM or rule-based IE ---
                    if spine_name.lower() not in ("domain_root", ""):
                        target = scoped_spine.get(spine_name.strip().lower())

                        if not target:
                            scoped_entries = list(scoped_spine.values())
                            target = _best_overlap(
                                _tokenize(spine_name),
                                scoped_entries,
                            )

                        if target:
                            try:
                                _link(
                                    session,
                                    c["label"],
                                    c["eid"],
                                    target["label"],
                                    target["eid"],
                                )
                                stats["linked"] += 1

                                self.logger.info(
                                    "Context Resolution spine link completed",
                                    domain=domain,
                                    product_line=product_line,
                                    spine_node=spine_name,
                                    resolved_target=target["name"],
                                    resolved_type=target["label"],
                                    entity=c["name"],
                                )
                                continue
                            except Exception:
                                pass

                    # --- Strategy 2: Word-overlap on entity name + block text ---
                    entity_tokens = _tokenize(c["name"] or "")
                    block_text = block_text_map.get(sbid, "")
                    combined_tokens = entity_tokens | _tokenize(block_text)

                    scoped_entries = list(scoped_spine.values())
                    match = _best_overlap(combined_tokens, scoped_entries)

                    if match:
                        try:
                            _link(
                                session,
                                c["label"],
                                c["eid"],
                                match["label"],
                                match["eid"],
                            )
                            stats["linked_overlap"] += 1

                            self.logger.info(
                                "Context Resolution overlap link completed",
                                domain=domain,
                                product_line=product_line,
                                resolved_target=match["name"],
                                resolved_type=match["label"],
                                entity=c["name"],
                            )
                            continue
                        except Exception:
                            pass

                    # --- Strategy 3: DomainRoot fallback ---
                    try:
                        _link_to_root(session, c["label"], c["eid"], domain)
                        stats["linked_to_root"] += 1

                        self.logger.info(
                            "Context Resolution domain root fallback",
                            domain=domain,
                            product_line=product_line,
                            spine_node=spine_name,
                            entity=c["name"],
                        )
                    except Exception:
                        stats["unlinked"] += 1

            self.logger.info(
                "product_line_context.context_resolution_scope.completed",
                domain=domain,
                product_lines=product_lines,
                **stats,
            )
        except Exception as e:
            self.logger.warning("link_content_to_spine failed (non-fatal)", error=str(e))

        return stats

    def attach_orphans_to_spine(self, domain: str) -> dict:
        """
        Post-write step: find orphan entities (no meaningful relationships)
        and link them to an "Unlinked" catch-all Function node under DomainRoot.

        No data is deleted.  Orphans remain in the graph for retrieval and
        can be manually re-categorised later.
        """
        all_rels = [
            "PART_OF", "IMPLEMENTS", "COVERS", "VALIDATES", "IMPACTS",
            "GOVERNED_BY", "USES", "VIOLATES", "MISSED_BY", "LINKED_TO",
            "PRODUCES", "CROSS_DOMAIN_IMPACTS", "PERFORMS",
        ]
        rel_pattern = "|".join(all_rels)

        find_orphans = f"""
        MATCH (n {{domain: $domain, customer_id: $customer_id}})
        WHERE NOT n:DomainRoot AND NOT n:ProductLine AND NOT n:Function AND NOT n:SubFunction
          AND NOT n:Block AND NOT n:Document
          AND NOT (n)-[:{rel_pattern}]->()
          AND NOT ()-[:{rel_pattern}]->(n)
        RETURN id(n) AS nid, labels(n)[0] AS lbl, n.name AS name
        """

        stats: dict[str, int] = {"orphans_found": 0, "orphans_linked_to_unlinked": 0}
        try:
            with self.driver.session() as session:
                run_customer_id = (
                    self._run_context.tenant_context.customer_id
                    or self._run_context.tenant_context.tenant_id
                )
                session.run("""
                    MERGE (u:Function {name: 'Unlinked', domain: $domain, customer_id: $customer_id})
                    ON CREATE SET u.entity_id = 'unlinked:' + $customer_id + ':' + $domain,
                                  u.kg_node_type = 'Function',
                                  u.tenant_id = $tenant_id,
                                  u.domain_id = $domain,
                                  u.created_at = datetime()
                    WITH u
                    MATCH (dr:DomainRoot {domain: $domain, customer_id: $customer_id})
                    MERGE (u)-[:PART_OF]->(dr)
                """, domain=domain, customer_id=run_customer_id,
                     tenant_id=self._run_context.tenant_context.tenant_id)
                session.run("""
                    MATCH (u:Function {name: 'Unlinked', domain: $domain, customer_id: $customer_id})
                    MATCH (dr:DomainRoot {domain: $domain, customer_id: $customer_id})
                    MERGE (u)-[:NODE_IN_DOMAIN]->(dr)
                """, domain=domain, customer_id=run_customer_id)

                orphans = list(session.run(find_orphans, domain=domain, customer_id=run_customer_id))
                stats["orphans_found"] = len(orphans)

                if orphans:
                    self.logger.info("Orphan entities found — linking to Unlinked", domain=domain, count=len(orphans))

                for rec in orphans:
                    nid = rec["nid"]
                    name = (rec["name"] or "").strip()
                    node_type = rec["lbl"] or "Unknown"
                    self.logger.info(
                        f"Orphan entity: {name} ({node_type}) — linked to Unlinked"
                    )
                    try:
                        session.run(
                            """
                            MATCH (n) WHERE id(n) = $nid
                            MATCH (u:Function {name: 'Unlinked', domain: $domain, customer_id: $customer_id})
                            MERGE (n)-[:PART_OF]->(u)
                            """,
                            nid=nid, domain=domain, customer_id=run_customer_id,
                        )
                        stats["orphans_linked_to_unlinked"] += 1
                    except Exception:
                        pass

                self.logger.info(
                    "Orphan linking complete", domain=domain,
                    found=stats["orphans_found"],
                    linked=stats["orphans_linked_to_unlinked"],
                )
        except Exception as e:
            self.logger.warning("attach_orphans_to_spine failed (non-fatal)", error=str(e))
        return stats

    def sample_kg_paths(
        self,
        *,
        domain: str,
        customer_id: str,
        product_line_id: str,
        ingestion_batch_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        Query Neo4j for a diverse sample of paths written by one scoped batch.

        Returns paths in the same shape the retriever will serve, so the
        ingestion report can preview how the KG looks to downstream agents.
        Excludes scaffolding rels (NODE_IN_DOMAIN, BLOCK_*, DOCUMENT_*).
        """
        scaffolding = [
            "NODE_IN_DOMAIN", "BLOCK_MENTIONS_ENTITY",
            "BLOCK_BELONGS_TO_DOCUMENT", "DOCUMENT_IN_DOMAIN",
        ]
        if not all((domain, customer_id, product_line_id, ingestion_batch_id)):
            raise ValueError("Run-scoped KG path sampling requires the frozen customer, product line, and batch")
        query = """
        MATCH (a)-[r]->(b)
        WHERE a.customer_id = $customer_id AND a.domain_id = $domain
          AND b.customer_id = $customer_id AND b.domain_id = $domain
          AND r.customer_id = $customer_id AND r.domain_id = $domain
          AND $product_line_id IN coalesce(a.ingestion_product_line_ids, [])
          AND $product_line_id IN coalesce(b.ingestion_product_line_ids, [])
          AND $product_line_id IN coalesce(r.ingestion_product_line_ids, [])
          AND $ingestion_batch_id IN coalesce(r.ingestion_batch_ids, [])
          AND NOT type(r) IN $scaffolding
          AND NOT a:Block AND NOT a:Document AND NOT a:DomainRoot
          AND NOT b:Block AND NOT b:Document AND NOT b:DomainRoot
        WITH a, r, b, rand() AS rnd
        ORDER BY rnd
        LIMIT $limit
        RETURN
            labels(a)[0]  AS from_type,
            COALESCE(a.name_display, a.name, labels(a)[0]) AS from_name,
            type(r)        AS rel_type,
            labels(b)[0]  AS to_type,
            COALESCE(b.name_display, b.name, labels(b)[0]) AS to_name,
            r.source_file  AS source_file,
            r.source_page  AS source_page,
            r.source_block_id AS source_block_id,
            r.evidence     AS evidence,
            a.pagerank     AS from_pagerank,
            a.degree       AS from_degree
        """
        paths: list[dict] = []
        try:
            with self.driver.session() as session:
                result = session.run(
                    query,
                    domain=domain,
                    customer_id=customer_id,
                    product_line_id=product_line_id,
                    ingestion_batch_id=ingestion_batch_id,
                    scaffolding=scaffolding,
                    limit=limit,
                )
                for rec in result:
                    paths.append(dict(rec))
            self.logger.info("Sampled KG paths", domain=domain, count=len(paths))
        except Exception as e:
            self.logger.warning("sample_kg_paths failed (non-fatal)", error=str(e))
        return paths

    def close(self) -> None:
        """Close Neo4j driver connection."""
        if hasattr(self, 'driver') and self.driver:
            try:
                self.driver.close()
                logger.info("KG writer Neo4j driver closed")
            except Exception as e:
                logger.warning("Error closing KG writer Neo4j driver", error=str(e))
            finally:
                # Clear reference to help GC
                self.driver = None

    def __del__(self) -> None:
        """Destructor as safety net for cleanup."""
        try:
            self.close()
        except Exception:
            # Ignore errors in destructor
            pass



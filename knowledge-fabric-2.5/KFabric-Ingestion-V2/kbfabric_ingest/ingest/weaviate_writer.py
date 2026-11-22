"""Weaviate writer for KBBlock objects.

This module handles writing enriched blocks to Weaviate with retry logic
for reliability during transient connection issues.
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import weaviate
from requests.exceptions import ConnectionError, Timeout

from kbfabric_ingest._common.telemetry import get_tracer, record_weaviate_upsert
from kbfabric_ingest.ingest.config import get_app_config, get_domain_to_class
from kbfabric_ingest.ingest.models import EnrichedBlock
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import log_stage_start, log_stage_end, log_stage_error
from kbfabric_ingest.utils.retry import retry_with_backoff, circuit_breaker
from kbfabric_ingest.constants import RetryConstants, DatabaseConstants

logger = get_logger(__name__)

# Schema version for audit trail and migration tracking
# v1.0: Vision IE fields; v1.1: frozen run scope and optional project/tag filters;
# v1.2: stable CustomerRoot scope key on vector blocks.
SCHEMA_VERSION = "1.2"


class WeaviateWriter:
    """Writer for storing EnrichedBlocks in Weaviate.
    
    Features:
    - Retry with exponential backoff for reliability
    - Configurable timeout settings
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize Weaviate writer with retry support.
        
        Args:
            config: Weaviate configuration dictionary
        """
        self.config = config
        self.url = config["url"]
        self.api_key = config.get("api_key") or None
        self.logger = get_logger(__name__)
        
        # Domain to class name mapping from ontology (no default class; unknown domain raises)
        self.domain_to_class: dict[str, str] = get_domain_to_class()
        self._run_context = None
        self._tenant_id = get_app_config()["tenant_id"]

        # Initialize Weaviate client with retry logic
        self.client = self._create_client_with_retry()

    @staticmethod
    def _class_name(domain: str) -> str:
        return domain[:1].upper() + domain[1:]

    def configure_run_context(self, run_context: Any) -> None:
        """Resolve Weaviate classes from the frozen industry's domain catalog.

        A per-run writer must not rely on the module's legacy banking ontology
        cache when processing another industry.
        """
        runtime = run_context.ontology
        domains = runtime.domain_definition.get("domains", [])
        mapping = {
            item["name"]: self._class_name(item["name"])
            for item in domains
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
        }
        if runtime.domain_id not in mapping:
            raise ValueError(f"Frozen industry ontology does not contain domain {runtime.domain_id!r}")
        self._run_context = run_context
        self.domain_to_class = mapping

    def class_name_for_domain(self, domain: str) -> str:
        """Resolve a class in the configured industry without global cache bleed."""
        try:
            return self.domain_to_class[domain]
        except KeyError as exc:
            if self._run_context is not None:
                raise ValueError(f"Domain {domain!r} is outside the frozen industry ontology") from exc
            raise ValueError(f"Unknown domain: {domain!r}") from exc

    def ensure_domain_schema(self, domain: str) -> str:
        """Create/extend only the selected runtime domain class, preserving data."""
        class_name = self.class_name_for_domain(domain)
        template = self._get_schema_template()
        if not self.client.schema.exists(class_name):
            self.client.schema.create_class({"class": class_name, **template})
        else:
            self._ensure_class_properties(class_name, template["properties"])
        return class_name

    @retry_with_backoff(
        max_retries=RetryConstants.DB_CONNECTION_MAX_RETRIES,
        base_delay=RetryConstants.DEFAULT_BASE_DELAY_SEC,
        max_delay=RetryConstants.DEFAULT_MAX_DELAY_SEC,
        exceptions=(ConnectionError, Timeout, OSError)
    )
    def _create_client_with_retry(self) -> weaviate.Client:
        """
        Create Weaviate client with retry logic.
        
        Uses exponential backoff (3 attempts, ~3s total max wait) for
        transient connection failures.
        
        Returns:
            Weaviate client instance
        """
        self.logger.info(
            "Creating Weaviate client",
            url=self.url,
            has_api_key=bool(self.api_key)
        )
        
        # Timeouts from .env via weaviate.timeout.* (no in-code defaults)
        timeout_cfg = self.config["timeout"]
        query_timeout = timeout_cfg["query_sec"]
        insert_timeout = timeout_cfg["insert_sec"]
        timeout_config = (query_timeout, insert_timeout)
        self.logger.info(f"Weaviate timeout config: query={query_timeout}s, insert={insert_timeout}s")
        
        if self.api_key:
            try:
                return weaviate.Client(
                    self.url,
                    auth_client_secret=weaviate.AuthApiKey(self.api_key),
                    timeout_config=timeout_config,
                )
            except TypeError:
                # Older weaviate-client versions don't support timeout_config
                return weaviate.Client(
                    self.url,
                    auth_client_secret=weaviate.AuthApiKey(self.api_key)
                )
        else:
            try:
                return weaviate.Client(
                    self.url,
                    timeout_config=timeout_config,
                )
            except TypeError:
                # Older weaviate-client versions don't support timeout_config
                return weaviate.Client(self.url)

    def clear_domain(self, domain: str) -> dict[str, Any]:
        """
        Clear all data for a specific domain from Weaviate and recreate the schema.
        
        Uses the correct class name mapping from config.
        After clearing, recreates the class schema so ingestion can proceed.
        
        Args:
            domain: Domain name (e.g., "consumer_retail")
            
        Returns:
            dict with status and count of deleted items
        """
        try:
            class_name = self.class_name_for_domain(domain)
        except ValueError as e:
            self.logger.warning("Unknown domain for Weaviate clear", domain=domain, error=str(e))
            return {"status": "error", "reason": str(e)}
        
        result = {"class_name": class_name, "deleted": False, "recreated": False}
        
        try:
            # Step 1: Delete the class if it exists
            if self.client.schema.exists(class_name):
                self.client.schema.delete_class(class_name)
                self.logger.info("Deleted Weaviate class", class_name=class_name, domain=domain)
                result["deleted"] = True
            else:
                self.logger.info("Weaviate class did not exist", class_name=class_name, domain=domain)
            
            # Step 2: Recreate the class schema so ingestion can proceed
            self._create_class_schema(class_name)
            result["recreated"] = True
            result["status"] = "cleared_and_recreated"
            
            return result
            
        except Exception as e:
            self.logger.error("Failed to clear/recreate Weaviate class", class_name=class_name, error=str(e))
            return {"status": "error", "error": str(e), "class_name": class_name}
    
    def clear_product_line(
        self,
        *,
        customer_id: str,
        tenant_id: str,
        domain_id: str,
        product_line_id: str,
    ) -> dict[str, Any]:
        """Delete only chunks carrying the exact frozen customer/domain/product-line scope."""
        scope = (customer_id, tenant_id, domain_id, product_line_id)
        if not all(isinstance(value, str) and value.strip() for value in scope):
            raise ValueError("Product-line cleanup requires customer, tenant, domain, and product-line IDs")
        class_name = self.class_name_for_domain(domain_id)
        where = {
            "operator": "And",
            "operands": [
                {"path": ["customer_id"], "operator": "Equal", "valueString": customer_id},
                {"path": ["tenant_id"], "operator": "Equal", "valueString": tenant_id},
                {"path": ["domain_id"], "operator": "Equal", "valueString": domain_id},
                {
                    "path": ["ingestion_product_line_id"],
                    "operator": "Equal",
                    "valueString": product_line_id,
                },
            ],
        }
        response = self.client.batch.delete_objects(
            class_name=class_name,
            where=where,
            output="verbose",
        )
        failures = response.get("failed", 0) if isinstance(response, dict) else 0
        if failures:
            raise RuntimeError(
                f"Weaviate product-line cleanup reported {failures} failed deletions"
            )
        deleted = response.get("matches", 0) if isinstance(response, dict) else 0
        self.logger.info(
            "Cleared Weaviate product-line scope",
            class_name=class_name,
            customer_id=customer_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            product_line_id=product_line_id,
            deleted=deleted,
        )
        return {"status": "cleared", "class_name": class_name, "deleted": deleted}

    def _create_class_schema(self, class_name: str) -> None:
        """Create schema for a single Weaviate class using canonical template."""
        schema_template = self._get_schema_template()
        class_schema = {
            "class": class_name,
            **schema_template
        }
        self.client.schema.create_class(class_schema)
        self.logger.info("Created Weaviate class schema", class_name=class_name, version=SCHEMA_VERSION)
    
    def _get_schema_template(self) -> dict[str, Any]:
        """Return the schema template for domain chunk classes (vectorIndexConfig from weaviate.index)."""
        index_cfg = self.config.get("index", {}) or {}
        distance = index_cfg.get("distance", "cosine")
        ef_construction = index_cfg.get("ef_construction", 128)
        max_connections = index_cfg.get("max_connections", 64)
        vector_index_config: dict[str, Any] = {
            "distance": distance,
            "efConstruction": ef_construction,
            "maxConnections": max_connections,
        }
        return {
            "description": f"Knowledge base blocks with embeddings (schema v{SCHEMA_VERSION})",
            "vectorizer": "none",
            "properties": [
                {"name": "domain", "dataType": ["string"], "description": "Domain name"},
                {"name": "domain_id", "dataType": ["string"], "description": "Frozen run domain ID"},
                {"name": "industry_id", "dataType": ["string"], "description": "Frozen customer industry ID"},
                {"name": "tenant_id", "dataType": ["string"], "description": "Tenant scope"},
                {"name": "customer_id", "dataType": ["string"], "description": "Customer scope"},
                {"name": "customer_root_id", "dataType": ["string"], "description": "Stable customer-root scope key; industry remains metadata"},
                {"name": "ingestion_product_line_id", "dataType": ["string"], "description": "Product line selected for this ingestion"},
                {"name": "customer_ontology_id", "dataType": ["string"], "description": "Frozen customer ontology ID"},
                {"name": "customer_ontology_version", "dataType": ["number"], "description": "Frozen customer ontology version"},
                {"name": "canonical_ontology_version", "dataType": ["string"], "description": "Canonical ontology version"},
                {"name": "industry_extraction_policy_version", "dataType": ["string"], "description": "Industry extraction policy version"},
                {"name": "document_id", "dataType": ["string"], "description": "Stable customer-scoped source document ID"},
                {"name": "block_id", "dataType": ["string"], "description": "Stable parsed chunk identifier"},
                {"name": "file_type", "dataType": ["string"], "description": "Source file type"},
                {"name": "ingestion_batch_id", "dataType": ["string"], "description": "Ingestion run identifier"},
                {"name": "semantic_unit_id", "dataType": ["string"], "description": "Semantic unit identifier"},
                {"name": "source_block_ids", "dataType": ["text[]"], "description": "Underlying parser block IDs"},
                {"name": "unit_type", "dataType": ["string"], "description": "Semantic unit type"},
                {"name": "unit_part_index", "dataType": ["number"], "description": "Part index within a semantic unit"},
                {"name": "unit_part_total", "dataType": ["number"], "description": "Number of parts in semantic unit"},
                {"name": "project_ids", "dataType": ["text[]"], "description": "Optional caller-supplied project IDs"},
                {"name": "context_tags", "dataType": ["text[]"], "description": "Optional caller-supplied tags"},
                {"name": "raw_project_ids", "dataType": ["string"], "description": "Raw project ID input"},
                {"name": "raw_context_tags", "dataType": ["string"], "description": "Raw tag input"},
                {"name": "document_nature", "dataType": ["string"], "description": "Ingestion Studio lane: qe_assets or technology_artifacts"},
                {"name": "block_category", "dataType": ["string"], "description": "Block category"},
                {"name": "kg_node_type", "dataType": ["string"], "description": "KG node type"},
                {"name": "aliases", "dataType": ["text[]"], "description": "Aliases extracted for entities in this chunk"},
                {"name": "masked_text", "dataType": ["text"], "description": "PII-masked text content"},
                {"name": "ocr_text_raw", "dataType": ["text"], "description": "Diagram OCR text (raw)"},
                {"name": "ocr_text_masked", "dataType": ["text"], "description": "Diagram OCR text (masked)"},
                {"name": "vision_description_raw", "dataType": ["text"], "description": "Diagram vision description (raw)"},
                {"name": "vision_description_masked", "dataType": ["text"], "description": "Diagram vision description (masked)"},
                {"name": "diagram_caption_text_raw", "dataType": ["text"], "description": "Nearby diagram caption (raw)"},
                {"name": "diagram_caption_text_masked", "dataType": ["text"], "description": "Nearby diagram caption (masked)"},
                {"name": "ocr_available", "dataType": ["boolean"], "description": "Whether OCR was available (pytesseract/Pillow installed)"},
                {"name": "ocr_error", "dataType": ["text"], "description": "OCR error message if OCR failed at runtime"},
                {"name": "vision_model", "dataType": ["string"], "description": "Vision model name used for diagram analysis"},
                {"name": "vision_duration_ms", "dataType": ["number"], "description": "Approx per-image vision analysis duration (ms)"},
                {"name": "extracted_fields", "dataType": ["text"], "description": "Vision IE extracted form fields (JSON string)"},
                {"name": "extracted_knowledge", "dataType": ["text"], "description": "Vision IE extracted knowledge items (JSON string)"},
                {"name": "semantic_chunks", "dataType": ["text"], "description": "Vision IE semantic chunks (JSON string)"},
                {"name": "source_file", "dataType": ["string"], "description": "Source file path"},
                {"name": "page_number", "dataType": ["number"], "description": "Page number if applicable"},
                {"name": "block_index", "dataType": ["number"], "description": "Block index within file"},
                {"name": "block_type", "dataType": ["string"], "description": "Block type (heading, paragraph, etc.)"},
                {"name": "created_at", "dataType": ["date"], "description": "Timestamp when block was first parsed"},
                {"name": "updated_at", "dataType": ["date"], "description": "Timestamp when block was last updated"},
                {
                    "name": "metadata",
                    "dataType": ["object"],
                    "description": "Additional metadata",
                    "nestedProperties": [
                        {"name": "row_index", "dataType": ["int"], "description": "Row index for table/XLSX blocks"},
                        {"name": "sheet", "dataType": ["text"], "description": "Sheet name for XLSX blocks"},
                        {"name": "table_row_index", "dataType": ["int"], "description": "Table row index for DOCX table blocks"},
                        {"name": "page_number", "dataType": ["int"], "description": "Page number"},
                        {"name": "pdf_page_number", "dataType": ["int"], "description": "PDF page number when parsed from PDF or converted DOCX"},
                        {"name": "source_docx_file", "dataType": ["text"], "description": "Original DOCX filename when converted to PDF"},
                        {"name": "converted_pdf_file", "dataType": ["text"], "description": "Generated PDF filename after conversion"},
                        {"name": "conversion_method", "dataType": ["text"], "description": "Conversion tool or method used"},
                        {"name": "conversion_timestamp", "dataType": ["text"], "description": "Conversion timestamp (ISO format)"},
                        {"name": "list", "dataType": ["boolean"], "description": "Whether block is part of a list"},
                        {"name": "source", "dataType": ["text"], "description": "Source information"},
                        {"name": "ingestion_timestamp", "dataType": ["text"], "description": "Ingestion timestamp"},
                        {"name": "last_updated_timestamp", "dataType": ["text"], "description": "Last updated timestamp"},
                        {"name": "processing_duration_ms", "dataType": ["number"], "description": "Processing duration in ms"},
                        {"name": "extra", "dataType": ["text"], "description": "Additional dynamic metadata as JSON"}
                    ]
                },
            ],
            "vectorIndexConfig": vector_index_config,
        }

    def ensure_schema(self) -> None:
        """
        Ensure domain-specific Weaviate classes exist in schema (one per ontology domain).
        
        SAFE BEHAVIOR:
        - Only creates classes if they don't exist
        - Never deletes existing classes
        - Uses single source of truth schema template
        """
        schema_obs_start = log_stage_start(self.logger, "weaviate.ensure_schema")
        schema_template = self._get_schema_template()
        
        # Ensure schema for each domain-specific class only (no default/fallback class)
        all_class_names = list(self.domain_to_class.values())
        for class_name in all_class_names:
            # Check if class exists - only create if missing (true "ensure" behavior)
            if not self.client.schema.exists(class_name):
                # Create class schema only if it doesn't exist
                class_schema = {
                    "class": class_name,
                    **schema_template
                }

                try:
                    self.client.schema.create_class(class_schema)
                    self.logger.info("Created Weaviate schema class", class_name=class_name, version=SCHEMA_VERSION)
                except Exception as e:
                    self.logger.warning(
                        "Failed to create Weaviate schema class",
                        class_name=class_name,
                        error=str(e)
                    )
            else:
                self._ensure_class_properties(class_name, schema_template["properties"])
                self.logger.info("Weaviate schema class already exists", class_name=class_name)

        log_stage_end(self.logger, "weaviate.ensure_schema", schema_obs_start,
                      class_count=len(all_class_names))

    def _ensure_class_properties(self, class_name: str, desired_properties: list[dict[str, Any]]) -> None:
        """Add newly declared properties to existing classes without deleting data."""
        try:
            schema = self.client.schema.get(class_name)
            existing = {item.get("name") for item in schema.get("properties", [])}
            for prop in desired_properties:
                if prop.get("name") not in existing:
                    self.client.schema.property.create(class_name, prop)
                    self.logger.info(
                        "Added Weaviate schema property",
                        class_name=class_name,
                        property_name=prop.get("name"),
                    )
        except Exception as exc:
            self.logger.warning(
                "Failed to extend existing Weaviate class schema",
                class_name=class_name,
                error=str(exc),
            )

    def validate_schema(self) -> dict[str, Any]:
        """Validate Weaviate schema for required classes and properties."""
        results: dict[str, Any] = {"classes": {}, "status": "ok"}
        try:
            schema = self.client.schema.get()
            classes = {c.get("class"): c for c in schema.get("classes", [])}
            required_props = {
                "domain", "tenant_id", "customer_id", "customer_root_id", "domain_id", "ingestion_product_line_id",
                "customer_ontology_id", "customer_ontology_version", "canonical_ontology_version",
                "industry_extraction_policy_version", "document_id", "block_id", "file_type", "ingestion_batch_id",
                "semantic_unit_id", "source_block_ids", "project_ids", "context_tags",
                "raw_project_ids", "raw_context_tags", "document_nature", "block_category", "kg_node_type",
                "masked_text", "source_file", "block_index", "block_type", "metadata"
            }
            for domain, class_name in self.domain_to_class.items():
                class_info = classes.get(class_name)
                if not class_info:
                    results["classes"][class_name] = {"missing": True, "missing_properties": sorted(required_props)}
                    results["status"] = "warning"
                    continue
                props = {p.get("name") for p in class_info.get("properties", []) if p.get("name")}
                missing = sorted(required_props - props)
                results["classes"][class_name] = {"missing": False, "missing_properties": missing}
                if missing:
                    results["status"] = "warning"
            return results
        except Exception as e:
            self.logger.warning("Weaviate schema validation failed", error=str(e))
            return {"status": "error", "error": str(e)}

    @circuit_breaker(failure_threshold=3, recovery_timeout=120, service_name="weaviate_write")
    def write_blocks(
        self, blocks: list[EnrichedBlock], *, write_unit_callback=None,
    ) -> dict[str, str | dict]:
        """
        Write enriched blocks to Weaviate.
        
        Args:
            blocks: List of EnrichedBlock instances
            
        Returns:
            Dictionary with:
            - block_id_to_uuid: mapping of block.block.block_id to Weaviate UUID
            - upsert_metrics: {"before_count": X, "after_count": Y, "new": Z, "updated": W}
        """
        if not blocks:
            return {"block_id_to_uuid": {}, "upsert_metrics": {"before_count": 0, "after_count": 0, "new": 0, "updated": 0}}
        
        # Get domain for telemetry
        domain = blocks[0].block.domain if blocks and blocks[0].block else "unknown"
        start_time = time.time()
        write_obs_start = log_stage_start(
            self.logger, "weaviate.write", domain=domain, block_count=len(blocks),
        )
        
        block_id_to_uuid: dict[str, str] = {}
        import hashlib
        unit_digest = hashlib.sha256(
            "\n".join(sorted(block.block.block_id for block in blocks if block and block.block)).encode("utf-8")
        ).hexdigest()[:24]
        unit_key = f"blocks:{unit_digest}"
        if write_unit_callback:
            write_unit_callback("weaviate", unit_key, "IN_PROGRESS", {
                "block_count": len(blocks), "unit_type": "bounded_store_call",
            }, None)
        # Read batch_size from config with fallback to default
        batch_size = self.config.get("batch_size", 100)
        
        # Track unique classes to be written (unknown domain raises via get_store_name_for_domain)
        classes_to_write: set[str] = set()
        for block in blocks:
            domain = block.block.domain
            class_name = self.ensure_domain_schema(domain)
            classes_to_write.add(class_name)
        
        # Count objects BEFORE write for each class
        counts_before: dict[str, int] = {}
        for class_name in classes_to_write:
            try:
                result = self.client.query.aggregate(class_name).with_meta_count().do()
                count = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
                counts_before[class_name] = count
                self.logger.info(f"Weaviate before-write count: {class_name} has {count} objects")
            except Exception as e:
                self.logger.warning(f"Failed to get before-count for {class_name}", error=str(e))
                counts_before[class_name] = 0
        
        total_before = sum(counts_before.values())
        
        try:
            with self.client.batch as batch:
                batch.batch_size = batch_size
                
                for block in blocks:
                    # Choose class name based on domain
                    domain = block.block.domain
                    class_name = self.class_name_for_domain(domain)
                    
                    # Prepare metadata with timing fields
                    now = datetime.now(timezone.utc)
                    metadata_dict = block.block.metadata.copy() if block.block.metadata else {}
                    
                    # Add timing fields
                    metadata_dict["ingestion_timestamp"] = now.isoformat()
                    metadata_dict["last_updated_timestamp"] = now.isoformat()
                    if self._run_context is not None:
                        metadata_dict["industry_id"] = self._run_context.tenant_context.industry_id
                    
                    # Store any extra metadata fields not in schema as JSON string in 'extra'
                    known_fields = {
                        "row_index",
                        "sheet",
                        "table_row_index",
                        "table_header_candidate",
                        "table_header_is_self",
                        "page_number",
                        "pdf_page_number",
                        "source_docx_file",
                        "converted_pdf_file",
                        "conversion_method",
                        "conversion_timestamp",
                        "list",
                        "source",
                        "ingestion_timestamp",
                        "last_updated_timestamp",
                        "processing_duration_ms"
                    }
                    extra_fields = {
                        k: v for k, v in metadata_dict.items() 
                        if k not in known_fields
                    }
                    if extra_fields:
                        import json
                        metadata_dict["extra"] = json.dumps(extra_fields)
                        # Remove extra fields from main dict
                        for key in extra_fields:
                            metadata_dict.pop(key, None)
                    
                    # Prepare object properties with provenance timestamps
                    block_metadata = block.block.metadata or {}
                    customer_id = block_metadata.get("customer_id")
                    source_file = block.block.source_file
                    from kbfabric_ingest.ingest.ingestion_metadata import (
                        customer_scope_id,
                        document_id_for_source,
                    )
                    scope_id = customer_scope_id(
                        customer_id,
                        block_metadata.get("tenant_id") or self._tenant_id,
                    )
                    document_id = document_id_for_source(
                        scope_id,
                        source_file,
                    )
                    properties = {
                        "domain": domain,
                        "domain_id": block_metadata.get("domain_id", domain),
                        "tenant_id": block_metadata.get("tenant_id", self._tenant_id),
                        "customer_id": customer_id,
                        "customer_root_id": scope_id,
                        "ingestion_product_line_id": block_metadata.get("ingestion_product_line_id"),
                        "customer_ontology_id": block_metadata.get("customer_ontology_id"),
                        "customer_ontology_version": block_metadata.get("customer_ontology_version"),
                        "canonical_ontology_version": block_metadata.get("canonical_ontology_version"),
                        "industry_extraction_policy_version": block_metadata.get("industry_extraction_policy_version"),
                        "document_id": document_id,
                        "block_id": block.block.block_id,
                        "file_type": block.block.file_type,
                        "block_category": block.block_category.value if block.block_category else "Narrative",
                        "kg_node_type": block.kg_node_type,
                        "aliases": sorted({a for e in block.candidate_entities for a in (e.get("aliases") or [])}) if block.candidate_entities else [],
                        "masked_text": block.masked_text,
                        "ocr_text_raw": getattr(block, "ocr_text_raw", None),
                        "ocr_text_masked": getattr(block, "ocr_text_masked", None),
                        "vision_description_raw": getattr(block, "vision_description_raw", None),
                        "vision_description_masked": getattr(block, "vision_description_masked", None),
                        "diagram_caption_text_raw": getattr(block, "diagram_caption_text_raw", None),
                        "diagram_caption_text_masked": getattr(block, "diagram_caption_text_masked", None),
                        "ocr_available": getattr(block, "ocr_available", None),
                        "ocr_error": getattr(block, "ocr_error", None),
                        "vision_model": getattr(block, "vision_model", None),
                        "vision_duration_ms": getattr(block, "vision_duration_ms", None),
                        "extracted_fields": json.dumps(block.extracted_fields) if getattr(block, "extracted_fields", None) else None,
                        "extracted_knowledge": json.dumps(block.extracted_knowledge) if getattr(block, "extracted_knowledge", None) else None,
                        "semantic_chunks": json.dumps(block.semantic_chunks) if getattr(block, "semantic_chunks", None) else None,
                        "source_file": source_file,
                        "ingestion_batch_id": block_metadata.get("ingestion_batch_id"),
                        "semantic_unit_id": block.block.semantic_unit_id,
                        "source_block_ids": block.block.source_block_ids or [block.block.block_id],
                        "unit_type": block.block.unit_type,
                        "unit_part_index": block.block.unit_part_index,
                        "unit_part_total": block.block.unit_part_total,
                        "project_ids": block_metadata.get("project_ids", []),
                        "context_tags": block_metadata.get("context_tags", []),
                        "raw_project_ids": block_metadata.get("raw_project_ids"),
                        "raw_context_tags": block_metadata.get("raw_context_tags"),
                        "document_nature": block_metadata.get("document_nature"),
                        "block_index": block.block.block_index,
                        "block_type": block.block.block_type,
                        "semantic_unit_id": block.block.semantic_unit_id,
                        "source_block_ids": block.block.source_block_ids,
                        "unit_type": block.block.unit_type,
                        "unit_part_index": block.block.unit_part_index,
                        "unit_part_total": block.block.unit_part_total,
                        "created_at": block.block.created_at.isoformat(),  # From CanonicalBlock
                        "updated_at": now.isoformat(),  # Current time for this upsert
                        "metadata": metadata_dict,
                    }

                    # Remove diagram audit fields if not present (keep schema sparse for non-diagram blocks)
                    # Note: OCR fields kept in schema but set to None/False (vision-only approach)
                    for k in [
                        "ocr_text_raw", "ocr_text_masked",  # OCR deprecated - vision only
                        "vision_description_raw", "vision_description_masked",
                        "diagram_caption_text_raw", "diagram_caption_text_masked",
                        "ocr_available", "ocr_error",  # Set to False/None - no OCR processing
                        "vision_model", "vision_duration_ms",
                        "extracted_fields", "extracted_knowledge", "semantic_chunks", "aliases"
                    ]:
                        value = properties.get(k)
                        if value in (None, "") or value == []:
                            properties.pop(k, None)
                    # Ensure OCR fields are set to False/None (vision-only)
                    if "ocr_available" not in properties:
                        properties["ocr_available"] = False
                    if "ocr_error" not in properties:
                        properties["ocr_error"] = None
                    
                    # Add page_number if present
                    if block.block.page_number is not None:
                        properties["page_number"] = block.block.page_number
                    
                    # Scope the stored object UUID by customer; preserve source block_id as a property.
                    from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_block_id
                    scope_id = customer_scope_id(customer_id, block_metadata.get("tenant_id") or self._tenant_id)
                    weaviate_uuid = scoped_block_id(scope_id, block.block.block_id)
                    
                    # Add to batch (Weaviate will upsert automatically if UUID exists)
                    batch.add_data_object(
                        data_object=properties,
                        class_name=class_name,
                        uuid=weaviate_uuid,
                        vector=block.embedding
                    )
                    
                    block_id_to_uuid[block.block.block_id] = weaviate_uuid
                
                # Batch will auto-flush
                batch.flush()
            
            # Post-write verification and metrics calculation (with retry for eventual consistency)
            counts_after: dict[str, int] = {}
            for class_name in classes_to_write:
                try:
                    # Retry count query with exponential backoff (Weaviate indexes asynchronously)
                    count = 0
                    expected_blocks = len([b for b in blocks if self.class_name_for_domain(b.block.domain) == class_name])
                    for attempt in range(5):  # Max 5 attempts (total ~15.5s)
                        result = self.client.query.aggregate(class_name).with_meta_count().do()
                        count = result.get("data", {}).get("Aggregate", {}).get(class_name, [{}])[0].get("meta", {}).get("count", 0)
                        
                        # If count matches expected or increased, or last attempt, accept it
                        before = counts_before.get(class_name, 0)
                        if count >= before + expected_blocks or attempt == 4:
                            break
                        
                        # Otherwise, wait with exponential backoff
                        wait_time = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s, 4s, 8s
                        self.logger.debug(f"Weaviate count for {class_name} = {count} (expected {before + expected_blocks}), retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    
                    counts_after[class_name] = count
                    before = counts_before.get(class_name, 0)
                    new_objects = count - before
                    self.logger.info(f"Weaviate after-write: {class_name} has {count} objects (was {before}, +{new_objects} new)")
                    print(f"[WEAVIATE] Verified: {class_name} has {count} objects (was {before}, +{new_objects} new)", flush=True)
                except Exception as verify_error:
                    self.logger.warning(f"Failed to verify Weaviate class {class_name}", error=str(verify_error))
                    print(f"[WEAVIATE] WARNING: Verification failed for {class_name}: {verify_error}", flush=True)
                    counts_after[class_name] = counts_before.get(class_name, 0) + len([b for b in blocks if self.class_name_for_domain(b.block.domain) == class_name])
            
            total_after = sum(counts_after.values())
            total_new = total_after - total_before
            # Updated = blocks attempted - newly created (upserts that matched existing)
            total_updated = len(blocks) - total_new if total_new <= len(blocks) else 0
            
            upsert_metrics = {
                "before_count": total_before,
                "after_count": total_after,
                "new": total_new,
                "updated": total_updated,
                "attempted": len(blocks)
            }
            
            self.logger.info(
                "Wrote blocks to Weaviate",
                count=len(blocks),
                classes_written=list(classes_to_write),
                upsert_metrics=upsert_metrics
            )
            print(f"[WEAVIATE] Upsert metrics: {upsert_metrics}", flush=True)
            
            # Record telemetry with OTel Database conventions
            duration = time.time() - start_time
            server_addr = self.url.replace("http://", "").replace("https://", "")
            record_weaviate_upsert(domain, "batch_create", duration, len(blocks), server_address=server_addr)

            log_stage_end(
                self.logger, "weaviate.write", write_obs_start,
                domain=domain,
                class_name=",".join(sorted(classes_to_write)),
                attempted=len(blocks),
                succeeded=len(block_id_to_uuid),
                failed=len(blocks) - len(block_id_to_uuid),
                new=upsert_metrics.get("new"),
                updated=upsert_metrics.get("updated"),
                after_count=upsert_metrics.get("after_count"),
            )
            if write_unit_callback:
                write_unit_callback("weaviate", unit_key, "SUCCEEDED", {
                    "block_count": len(block_id_to_uuid),
                    "attempted": len(blocks),
                    "class_names": sorted(classes_to_write),
                    "upsert_metrics": upsert_metrics,
                }, None)
            
        except Exception as e:
            if write_unit_callback:
                write_unit_callback("weaviate", unit_key, "UNKNOWN", {
                    "attempted": len(blocks),
                    "confirmed": len(block_id_to_uuid),
                }, {"code": "DEPENDENCY_FAILURE", "message": str(e)[:500]})
            # Record telemetry for failure
            duration = time.time() - start_time
            server_addr = self.url.replace("http://", "").replace("https://", "")
            record_weaviate_upsert(domain, "batch_create", duration, 0, server_address=server_addr)
            self.logger.exception(
                "Failed to write blocks to Weaviate",
                error=str(e),
                blocks_count=len(blocks)
            )
            log_stage_error(self.logger, "weaviate.write", e, domain=domain,
                            attempted=len(blocks), started_at=write_obs_start)
            raise
        
        return {"block_id_to_uuid": block_id_to_uuid, "upsert_metrics": upsert_metrics}

    def close(self) -> None:
        """Close Weaviate client connection."""
        if hasattr(self, 'client') and self.client:
            try:
                # Check if Weaviate client has a close method
                if hasattr(self.client, 'close'):
                    self.client.close()
                    self.logger.info("Weaviate writer client closed")
                else:
                    self.logger.info("Weaviate writer client cleanup completed (HTTP-based)")
            except Exception as e:
                self.logger.warning("Error during Weaviate writer client cleanup", error=str(e))
            finally:
                # Clear reference to help GC
                self.client = None

    def __del__(self) -> None:
        """Destructor as safety net for cleanup."""
        try:
            self.close()
        except Exception:
            # Ignore errors in destructor
            pass


